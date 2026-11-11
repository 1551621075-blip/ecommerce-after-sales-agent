import threading

import pytest

from after_sales.actions import Actions
from after_sales.agent import Runner
from after_sales.provider import ProviderReply, ScriptedProvider
from after_sales.schemas import (
    Command,
    DomainError,
    MessageCommand,
    ReconcileCommand,
    VersionedCommand,
)
from after_sales.service import Service
from after_sales.storage import Store


def prepare(tmp_path):
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    people = {key: store.authenticate(value) for key, value in tokens.items()}
    actions = Actions(store)
    sess = actions.create_session(people["alice"], Command(action_id="new"))
    return store, people, actions, sess


def test_cancel_does_not_allow_new_call_while_old_http_is_inflight(tmp_path):
    store, people, actions, sess = prepare(tmp_path)
    started, release = threading.Event(), threading.Event()

    class Slow:
        is_test_provider = True

        def decide(self, context, schema):
            started.set()
            assert release.wait(5)
            return ProviderReply({"tool": "list_my_orders", "arguments": {}}, 128, "TEST")

    submitted = actions.message(people["alice"], sess["id"], MessageCommand(
        action_id="msg", expected_version=sess["version"], text="查订单"))
    runner = Runner(store, Slow())
    worker = threading.Thread(target=runner.run, args=(people["alice"], submitted["run"]["id"]))
    worker.start()
    try:
        assert started.wait(5)
        latest = actions.view(people["alice"], sess["id"])["session"]
        cancelled = actions.cancel(people["alice"], sess["id"], VersionedCommand(
            action_id="cancel", expected_version=latest["version"]))
        with pytest.raises(DomainError, match="在途"):
            actions.message(people["alice"], sess["id"], MessageCommand(
                action_id="again", expected_version=cancelled["version"], text="再查一下"))
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()


def test_unknown_reconciliation_is_audited_and_never_resets_usage(tmp_path):
    store, people, actions, sess = prepare(tmp_path)
    submitted = actions.message(people["alice"], sess["id"], MessageCommand(
        action_id="msg", expected_version=sess["version"], text="查订单"))
    with store.transaction() as db:
        db.execute("INSERT INTO calls(id,run_id,owner,state,usage_kind,context) VALUES(?,?,?,?,?,?)",
                   ("call-lost", submitted["run"]["id"], "alice", "started", "real", "{}"))
    provider = ScriptedProvider([])
    runner = Runner(store, provider)
    runner.recover()
    command = ReconcileCommand(action_id="reconcile", call_id="call-lost", tokens=410,
                               note="本地模型服务账本核对，记录410 tokens且没有可用结果", outcome="no_usable_result")
    with pytest.raises(DomainError):
        runner.reconcile(people["alice"], command)
    first = runner.reconcile(people["operator"], command)
    assert runner.reconcile(people["operator"], command) == first
    assert first["resumed"] is False
    assert provider.seen_contexts == []
    metrics = runner.metrics(people["alice"], sess["id"])
    assert metrics["real_tokens"] == 410
    assert metrics["model_calls"] == 1
    assert metrics["unknown_calls"] == 0
    view = actions.view(people["alice"], sess["id"])
    assert view["session"]["payload"]["blocked_unknown"] is False
    with store.transaction() as db:
        assert db.execute("SELECT count(*) FROM audit WHERE action='reconcile'").fetchone()[0] >= 1


def test_only_one_service_may_recover_a_database(tmp_path):
    store, people, actions, sess = prepare(tmp_path)
    one = Service(store, ScriptedProvider([]))
    try:
        with pytest.raises(DomainError, match="已有服务"):
            Service(store, ScriptedProvider([]))
    finally:
        one.close()
    two = Service(store, ScriptedProvider([]))
    two.close()


def test_model_is_not_invoked_by_read_only_views(tmp_path, monkeypatch):
    monkeypatch.setattr("after_sales.storage.time.time", lambda: 1900000000)
    store, people, actions, sess = prepare(tmp_path)
    provider = ScriptedProvider([])
    service = Service(store, provider)
    try:
        before = actions.view(people["alice"], sess["id"])
        initial_now = before.pop("now")
        with store.transaction() as db:
            writes_before = {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                             for table in ("objects", "receipts", "audit", "calls")}
        for elapsed in range(1, 5):
            monkeypatch.setattr("after_sales.storage.time.time", lambda elapsed=elapsed: 1900000000 + elapsed)
            current = actions.view(people["alice"], sess["id"])
            assert current.pop("now") == initial_now + elapsed
            assert current == before
        with store.transaction() as db:
            assert {table: [tuple(row) for row in db.execute(f"SELECT * FROM {table}")]
                    for table in writes_before} == writes_before
        assert provider.seen_contexts == []
    finally:
        service.close()
