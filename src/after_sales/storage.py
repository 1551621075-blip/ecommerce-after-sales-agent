from __future__ import annotations

import hashlib
import json
import os
import re
import secrets
import sqlite3
import time
from contextlib import contextmanager
from pathlib import Path

from .schemas import DomainError, Principal, canonical, digest

DDL = """
CREATE TABLE settings(key TEXT PRIMARY KEY, value TEXT NOT NULL);
CREATE TABLE users(id TEXT PRIMARY KEY, role TEXT NOT NULL, token_hash TEXT UNIQUE NOT NULL,
                   active INTEGER NOT NULL CHECK(active IN (0,1)));
CREATE TABLE objects(kind TEXT NOT NULL, id TEXT NOT NULL, owner TEXT NOT NULL,
                     version INTEGER NOT NULL CHECK(version>0), payload TEXT NOT NULL,
                     content_hash TEXT NOT NULL, PRIMARY KEY(kind,id));
CREATE INDEX objects_owner ON objects(kind,owner);
CREATE TABLE receipts(user_id TEXT NOT NULL, action_id TEXT NOT NULL, request_hash TEXT NOT NULL,
                      response TEXT NOT NULL, PRIMARY KEY(user_id,action_id));
CREATE TABLE audit(seq INTEGER PRIMARY KEY, actor TEXT NOT NULL, action TEXT NOT NULL,
                   object_id TEXT NOT NULL, detail TEXT NOT NULL);
CREATE TABLE calls(id TEXT PRIMARY KEY, run_id TEXT NOT NULL, owner TEXT NOT NULL,
                   state TEXT NOT NULL, tokens INTEGER, usage_kind TEXT NOT NULL,
                   context TEXT NOT NULL, response TEXT, error TEXT, elapsed REAL NOT NULL DEFAULT 0);
CREATE INDEX calls_run ON calls(run_id);
"""


def _safe_id(value):
    if type(value) is not str or re.fullmatch(r"[A-Za-z0-9][A-Za-z0-9_-]{0,79}", value) is None:
        raise DomainError("invalid_id", "标识格式不合法")
    return value


def _object_hash(kind, object_id, owner, version, payload):
    return digest([kind, object_id, owner, version, payload])


class Store:
    def __init__(self, directory: str | Path):
        self.root = Path(directory).absolute()
        if self.root.is_symlink() or not self.root.is_dir():
            raise DomainError("data_directory", "需要本实例已初始化的普通数据目录")
        self.path = self.root / "state.sqlite"
        if self.path.is_symlink() or not self.path.is_file():
            raise DomainError("database_missing", "本地数据库尚未初始化")
        with self.transaction() as db:
            row = db.execute("SELECT value FROM settings WHERE key='schema_version'").fetchone()
            if row is None or row[0] != "1":
                raise DomainError("database_version", "数据库版本不受支持")

    @classmethod
    def initialize(cls, directory: str | Path, *, now: int):
        from .synthetic import seed_data

        if type(now) is not int or now < 0:
            raise DomainError("clock", "合成时钟必须是非负整数秒")
        root = Path(directory).absolute()
        if not root.parent.is_dir() or root.exists() or root.is_symlink():
            raise DomainError("data_exists", "初始化需要全新目录，父目录须已存在")
        try:
            data = seed_data(now)
        except ValueError:
            raise DomainError("clock", "合成初始化时间不足以容纳订单历史") from None
        root.mkdir(mode=0o700)
        path = root / "state.sqlite"
        tokens = {}
        with sqlite3.connect(path) as db:
            os.chmod(path, 0o600)
            db.executescript(DDL)
            db.execute("INSERT INTO settings VALUES('schema_version','1')")
            db.execute("INSERT INTO settings VALUES('now',?)", (str(now),))
            db.execute("INSERT INTO settings VALUES('wall_anchor',?)", (str(int(time.time())),))
            for user_id, role in (("alice", "customer"), ("bob", "customer"),
                                  ("staff", "staff"), ("operator", "operator")):
                token = secrets.token_urlsafe(32)
                tokens[user_id] = token
                db.execute("INSERT INTO users VALUES(?,?,?,1)",
                           (user_id, role, hashlib.sha256(token.encode()).hexdigest()))
            for kind, rows in (("order", data["orders"]), ("shipment", data["shipments"]),
                               ("policy", data["policies"])):
                for payload in rows:
                    owner = payload.get("customer_id", "system")
                    if kind == "shipment":
                        owner = next(o["customer_id"] for o in data["orders"]
                                     if o["id"] == payload["order_id"])
                    cls.create(db, kind, payload["id"], owner, payload)
        return cls(root), tokens

    @contextmanager
    def transaction(self):
        if self.path.is_symlink() or not self.path.is_file():
            raise DomainError("database_missing", "数据库不存在或不是普通文件")
        db = sqlite3.connect(f"file:{self.path}?mode=rw", uri=True, timeout=10)
        db.row_factory = sqlite3.Row
        try:
            db.execute("PRAGMA journal_mode=WAL")
            db.execute("PRAGMA foreign_keys=ON")
            db.execute("BEGIN IMMEDIATE")
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    def authenticate(self, token: str) -> Principal:
        if type(token) is not str or not 32 <= len(token) <= 128:
            raise DomainError("unauthorized", "身份令牌无效", 401)
        with self.transaction() as db:
            row = db.execute("SELECT id,role FROM users WHERE token_hash=? AND active=1",
                             (hashlib.sha256(token.encode()).hexdigest(),)).fetchone()
            if row is None:
                raise DomainError("unauthorized", "身份令牌无效", 401)
            return Principal(user_id=row["id"], role=row["role"])

    @staticmethod
    def require(db, principal: Principal, *roles):
        row = db.execute("SELECT role,active FROM users WHERE id=?", (principal.user_id,)).fetchone()
        if row is None or row["active"] != 1 or row["role"] != principal.role:
            raise DomainError("unauthorized", "身份已失效，请重新认证", 401)
        if roles and principal.role not in roles:
            raise DomainError("forbidden", "当前身份无权执行此操作", 403)

    @staticmethod
    def now(db):
        row = db.execute("SELECT value FROM settings WHERE key='now'").fetchone()
        try:
            value = int(row[0])
            anchor = int(db.execute("SELECT value FROM settings WHERE key='wall_anchor'").fetchone()[0])
            if value < 0 or anchor < 0:
                raise ValueError
            return value + max(0, int(time.time()) - anchor)
        except (TypeError, ValueError, AssertionError):
            raise DomainError("clock", "合成时钟损坏") from None

    @staticmethod
    def _decode(row):
        try:
            payload = json.loads(row["payload"])
            expected = _object_hash(row["kind"], row["id"], row["owner"], row["version"], payload)
            if expected != row["content_hash"] or type(payload) is not dict:
                raise ValueError
            return {"kind": row["kind"], "id": row["id"], "owner": row["owner"],
                    "version": row["version"], "payload": payload, "content_hash": expected}
        except (KeyError, ValueError, TypeError):
            raise DomainError("integrity", "本地业务对象完整性异常", 409) from None

    @classmethod
    def get(cls, db, kind: str, object_id: str, *, owner: str | None = None):
        _safe_id(object_id)
        row = db.execute("SELECT * FROM objects WHERE kind=? AND id=?", (kind, object_id)).fetchone()
        if row is None or (owner is not None and row["owner"] != owner):
            raise DomainError("not_found", "对象不存在或无访问权限", 404)
        return cls._decode(row)

    @classmethod
    def list(cls, db, kind, *, owner=None):
        if owner is None:
            rows = db.execute("SELECT * FROM objects WHERE kind=? ORDER BY id", (kind,)).fetchall()
        else:
            rows = db.execute("SELECT * FROM objects WHERE kind=? AND owner=? ORDER BY id",
                              (kind, owner)).fetchall()
        return [cls._decode(row) for row in rows]

    @staticmethod
    def create(db, kind, object_id, owner, payload):
        _safe_id(object_id)
        content_hash = _object_hash(kind, object_id, owner, 1, payload)
        try:
            db.execute("INSERT INTO objects VALUES(?,?,?,?,?,?)",
                       (kind, object_id, owner, 1, canonical(payload), content_hash))
        except sqlite3.IntegrityError:
            raise DomainError("exists", "对象已存在，不能覆盖", 409) from None
        return {"kind": kind, "id": object_id, "owner": owner, "version": 1,
                "payload": payload, "content_hash": content_hash}

    @staticmethod
    def save(db, obj, payload):
        version = obj["version"] + 1
        content_hash = _object_hash(obj["kind"], obj["id"], obj["owner"], version, payload)
        result = db.execute(
            "UPDATE objects SET payload=?,version=?,content_hash=? "
            "WHERE kind=? AND id=? AND owner=? AND version=? AND content_hash=?",
            (canonical(payload), version, content_hash, obj["kind"], obj["id"], obj["owner"],
             obj["version"], obj["content_hash"]))
        if result.rowcount != 1:
            raise DomainError("stale_version", "对象版本已变化，请刷新", 409)
        return {**obj, "version": version, "payload": payload, "content_hash": content_hash}

    @staticmethod
    def audit(db, principal, action, object_id, detail=None):
        db.execute("INSERT INTO audit(actor,action,object_id,detail) VALUES(?,?,?,?)",
                   (principal.user_id, action, object_id, canonical(detail or {})))

    @classmethod
    def receipt(cls, db, principal, action_id, request):
        cls.require(db, principal)
        _safe_id(action_id)
        row = db.execute("SELECT * FROM receipts WHERE user_id=? AND action_id=?",
                         (principal.user_id, action_id)).fetchone()
        if row is None:
            return None
        if row["request_hash"] != digest(request):
            raise DomainError("idempotency_conflict", "操作标识已绑定不同请求", 409)
        return json.loads(row["response"])

    @classmethod
    def record_receipt(cls, db, principal, action_id, request, response):
        old = cls.receipt(db, principal, action_id, request)
        if old is not None:
            if old != response:
                raise DomainError("idempotency_conflict", "操作回执冲突", 409)
            return
        db.execute("INSERT INTO receipts VALUES(?,?,?,?)",
                   (principal.user_id, action_id, digest(request), canonical(response)))
