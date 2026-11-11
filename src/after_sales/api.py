"""Loopback-only authenticated API; no model calls from polling endpoints."""

from __future__ import annotations

import asyncio
import ipaddress
import json
import math
import re
from contextlib import asynccontextmanager
from pathlib import Path
from urllib.parse import urlsplit

from fastapi import Depends, FastAPI, Request
from fastapi.exceptions import RequestValidationError
from fastapi.responses import FileResponse, JSONResponse
from pydantic import ValidationError
from starlette.concurrency import run_in_threadpool

from .schemas import (
    Command,
    DomainError,
    MessageCommand,
    NoteCommand,
    ProposalCommand,
    ReconcileCommand,
    ReviewCommand,
    VersionedCommand,
)
from .service import Service

MAX_BODY_BYTES = 65_536
BODY_TIMEOUT = 5.0
MAX_JSON_DEPTH = 20
_SECURITY_HEADERS = {
    "content-security-policy": "default-src 'none'; script-src 'self'; style-src 'self'; "
    "connect-src 'self'; img-src 'self'; base-uri 'none'; form-action 'none'; frame-ancestors 'none'",
    "x-content-type-options": "nosniff", "x-frame-options": "DENY",
    "referrer-policy": "no-referrer", "cache-control": "no-store",
    "permissions-policy": "camera=(), microphone=(), geolocation=()",
    "cross-origin-opener-policy": "same-origin", "cross-origin-resource-policy": "same-origin",
}
_SENSITIVE = {
    b"host", b"authorization", b"origin", b"content-type", b"content-length", b"content-encoding",
    b"transfer-encoding", b"sec-fetch-site", b"sec-fetch-mode", b"sec-fetch-dest", b"cookie",
}
_PROXY_HEADERS = {b"forwarded", b"x-real-ip", b"x-original-url", b"x-rewrite-url",
                  b"proxy-authorization", b"proxy-connection"}


def _error(status: int, code: str, message: str) -> JSONResponse:
    return JSONResponse({"error": {"code": code, "message": message}}, status_code=status)


def _origin_host(origin: str) -> str:
    try:
        parsed = urlsplit(origin)
        address = ipaddress.ip_address(parsed.hostname or "")
        host = f"[{address.compressed}]" if address.version == 6 else str(address)
        port = parsed.port
        netloc = host + (f":{port}" if port is not None else "")
        if (parsed.scheme != "http" or not address.is_loopback or parsed.netloc != netloc
                or parsed.path or parsed.query or parsed.fragment or (port is not None and not 1 <= port <= 65535)
                or origin != "http://" + netloc):
            raise ValueError
        return netloc
    except (ValueError, TypeError, AttributeError):
        raise ValueError("origin必须是规范的数字回环HTTP源，不含路径或凭据") from None


def _json_object(raw: bytes) -> dict:
    def pairs(entries):
        result = {}
        for key, value in entries:
            if key in result:
                raise ValueError
            result[key] = value
        return result

    def reject(_):
        raise ValueError

    value = json.loads(raw.decode("utf-8"), object_pairs_hook=pairs, parse_constant=reject)
    nodes = 0

    def visit(item, depth=0):
        nonlocal nodes
        nodes += 1
        if depth > MAX_JSON_DEPTH or nodes > 4096:
            raise ValueError
        if isinstance(item, dict):
            for key, child in item.items():
                key.encode("utf-8")
                visit(child, depth + 1)
        elif isinstance(item, list):
            for child in item:
                visit(child, depth + 1)
        elif isinstance(item, str):
            item.encode("utf-8")
        elif isinstance(item, float) and not math.isfinite(item):
            raise ValueError

    visit(value)
    if not isinstance(value, dict):
        raise ValueError
    return value


class LocalBoundary:
    """ASGI boundary validates raw headers/paths and bounds the entire input stream."""

    def __init__(self, app, *, origin: str):
        self.app, self.origin, self.host = app, origin, _origin_host(origin).encode("ascii")

    async def __call__(self, scope, receive, send):
        if scope["type"] != "http":
            if scope["type"] == "websocket":
                await send({"type": "websocket.close", "code": 1008})
            else:
                await self.app(scope, receive, send)
            return

        started = False

        async def secured_send(message):
            nonlocal started
            if message["type"] == "http.response.start":
                started = True
                additions = [(name.encode(), value.encode()) for name, value in _SECURITY_HEADERS.items()]
                blocked = {name for name, _ in additions}
                headers = [(name, value) for name, value in message.get("headers", []) if name not in blocked]
                message = {**message, "headers": headers + additions}
            await send(message)

        async def fail(status, code, message):
            await _error(status, code, message)(scope, receive, secured_send)

        headers = {}
        for name, value in scope.get("headers", []):
            name = name.lower()
            if name in _PROXY_HEADERS or name.startswith(b"x-forwarded-"):
                return await fail(400, "proxy_header", "不接受代理转发头")
            if name in _SENSITIVE and name in headers:
                return await fail(400, "duplicate_header", "敏感请求头不可重复")
            headers[name] = value
        if headers.get(b"host") != self.host:
            return await fail(400, "invalid_host", "Host必须与配置的数字回环地址一致")
        raw_path = scope.get("raw_path", b"")
        if (scope.get("query_string") or b"%" in raw_path or b"\\" in raw_path or b"//" in raw_path
                or any(part in {b".", b".."} for part in raw_path.split(b"/"))
                or not re.fullmatch(rb"/[A-Za-z0-9_./-]*", raw_path)):
            return await fail(400, "invalid_path", "不接受查询参数、编码或歧义路径")
        origin, site = headers.get(b"origin"), headers.get(b"sec-fetch-site")
        if (origin is not None and origin != self.origin.encode()) or site in {b"cross-site", b"same-site"}:
            return await fail(403, "cross_origin", "只允许严格同源请求")
        method = scope["method"]
        if method not in {"GET", "HEAD", "POST"}:
            return await fail(405, "method_not_allowed", "请求方法不受支持")
        length = headers.get(b"content-length")
        transfer = headers.get(b"transfer-encoding")
        if ((length is not None and not re.fullmatch(rb"0|[1-9][0-9]{0,9}", length))
                or (length is not None and transfer is not None)
                or (transfer is not None and transfer != b"chunked")):
            return await fail(400, "invalid_framing", "请求体边界不明确")
        if method == "POST":
            if (origin != self.origin.encode() or site != b"same-origin"
                    or headers.get(b"sec-fetch-mode") not in {b"cors", b"same-origin"}
                    or headers.get(b"sec-fetch-dest") != b"empty"):
                return await fail(403, "mutation_origin", "写操作须具备严格同源Origin及Fetch Metadata")
            if (headers.get(b"content-type", b"").lower() not in
                    {b"application/json", b"application/json; charset=utf-8"}
                    or b"content-encoding" in headers):
                return await fail(415, "json_required", "只接受未压缩的UTF-8 JSON")
            if length is not None and int(length) > MAX_BODY_BYTES:
                return await fail(413, "body_limit", "请求体超过大小限制")
            raw = bytearray()
            try:
                async with asyncio.timeout(BODY_TIMEOUT):
                    while True:
                        event = await receive()
                        if event["type"] == "http.disconnect":
                            return
                        if event["type"] != "http.request":
                            return await fail(400, "invalid_body", "请求体不合法")
                        raw.extend(event.get("body", b""))
                        if len(raw) > MAX_BODY_BYTES:
                            return await fail(413, "body_limit", "请求体超过大小限制")
                        if not event.get("more_body", False):
                            break
            except TimeoutError:
                return await fail(408, "body_timeout", "读取请求体超时，未执行操作")
            if length is not None and len(raw) != int(length):
                return await fail(400, "invalid_framing", "请求体长度不一致")
            try:
                scope.setdefault("state", {})["command_body"] = _json_object(bytes(raw))
            except (ValueError, UnicodeError, RecursionError, OverflowError):
                return await fail(400, "invalid_json", "JSON须为有限、无重复字段且深度受限的对象")
        elif (length is not None and length != b"0") or transfer is not None:
            return await fail(400, "unexpected_body", "只读请求不可携带请求体")
        try:
            await self.app(scope, receive, secured_send)
        except Exception:
            # Do not expose request data through exception tracebacks or generic server logs.
            if not started:
                await fail(500, "internal_error", "内部处理结果不明，请保留原action_id核实，勿重复创建操作")


def create_app(store, provider, origin: str = "http://127.0.0.1:8000") -> FastAPI:
    _origin_host(origin)

    @asynccontextmanager
    async def lifespan(app):
        app.state.service = await run_in_threadpool(Service, store, provider)
        try:
            yield
        finally:
            await run_in_threadpool(app.state.service.close)

    app = FastAPI(lifespan=lifespan, docs_url=None, redoc_url=None, openapi_url=None, redirect_slashes=False)
    app.add_middleware(LocalBoundary, origin=origin)
    test_mode = getattr(provider, "is_test_provider", False) is True

    @app.exception_handler(DomainError)
    async def domain_error(request, exc):
        return _error(exc.status, exc.code, exc.message)

    @app.exception_handler(RequestValidationError)
    async def invalid_request(request, exc):
        return _error(422, "invalid_command", "请求字段不符合严格结构")

    def principal(request: Request):
        authorization = request.headers.get("authorization", "")
        if not re.fullmatch(r"Bearer [A-Za-z0-9._~+/-]{1,128}={0,2}", authorization):
            raise DomainError("unauthorized", "请提供Bearer身份令牌", 401)
        return store.authenticate(authorization[7:])

    def command(request, model):
        try:
            return model.model_validate(request.state.command_body)
        except ValidationError:
            raise DomainError("invalid_command", "请求字段不符合严格结构", 422) from None

    @app.get("/api/me")
    def me(user=Depends(principal)):
        return {**user.model_dump(), "test_mode": test_mode}

    @app.get("/api/sessions")
    def sessions(user=Depends(principal)):
        with store.transaction() as db:
            store.require(db, user, "customer")
            return store.list(db, "session", owner=user.user_id)

    @app.post("/api/sessions")
    def create_session(request: Request, user=Depends(principal)):
        return request.app.state.service.actions.create_session(user, command(request, Command))

    @app.get("/api/sessions/{sid}")
    def session(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.view(user, sid)

    @app.get("/api/sessions/{sid}/metrics")
    def metrics(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.runner.metrics(user, sid)

    @app.get("/api/unknown-calls")
    def unknown_calls(user=Depends(principal)):
        with store.transaction() as db:
            store.require(db, user, "operator")
            rows = db.execute("SELECT id,run_id,tokens,state,usage_kind FROM calls WHERE state='unknown' "
                              "ORDER BY id").fetchall()
            return [dict(row) for row in rows]

    @app.post("/api/reconcile")
    def reconcile(request: Request, user=Depends(principal)):
        return request.app.state.service.runner.reconcile(user, command(request, ReconcileCommand))

    @app.post("/api/sessions/{sid}/messages")
    def message(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.submit(user, sid, command(request, MessageCommand))

    @app.post("/api/sessions/{sid}/cancel")
    def cancel(sid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.cancel(user, sid, command(request, VersionedCommand))

    @app.post("/api/proposals/{pid}/decision")
    def decision(pid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.decide_proposal(user, pid, command(request, ProposalCommand))

    @app.post("/api/applications/{aid}/withdraw")
    def withdraw(aid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.withdraw(user, aid, command(request, VersionedCommand))

    @app.get("/api/queue")
    def queue(request: Request, user=Depends(principal)):
        return request.app.state.service.actions.queue(user)

    @app.get("/api/tickets/{tid}")
    def ticket(tid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.ticket_detail(user, tid)

    @app.post("/api/tickets/{tid}/claim")
    def claim(tid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.claim(user, tid, command(request, VersionedCommand))

    @app.post("/api/tickets/{tid}/review")
    def review(tid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.review(user, tid, command(request, ReviewCommand))

    @app.post("/api/tickets/{tid}/end")
    def end(tid: str, request: Request, user=Depends(principal)):
        return request.app.state.service.actions.end_handoff(user, tid, command(request, NoteCommand))

    assets = Path(__file__).parent

    @app.get("/", include_in_schema=False)
    def index():
        return FileResponse(assets / "templates" / "index.html", media_type="text/html")

    @app.get("/static/app.js", include_in_schema=False)
    def javascript():
        return FileResponse(assets / "static" / "app.js", media_type="text/javascript")

    @app.get("/static/style.css", include_in_schema=False)
    def stylesheet():
        return FileResponse(assets / "static" / "style.css", media_type="text/css")

    return app
