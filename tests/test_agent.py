import threading

import pytest

from after_sales.actions import Actions
from after_sales.agent import Runner
from after_sales.provider import ProviderReply, ScriptedProvider
from after_sales.schemas import Command, MessageCommand, VersionedCommand
from after_sales.storage import Store
from after_sales.tools import QUESTIONS


def act(tool, **args):
    return {"tool": tool, "arguments": args}


@pytest.fixture
def setup(tmp_path):
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    alice = store.authenticate(tokens["alice"])
    actions = Actions(store)
    sess = actions.create_session(alice, Command(action_id="new"))
    result = actions.message(alice, sess["id"], MessageCommand(
        action_id="msg", expected_version=sess["version"], text="查一下我的订单"))
    return store, alice, actions, result["session"], result["run"]


def test_real_observation_is_in_next_request(setup):
    store, alice, actions, sess, run = setup
    provider = ScriptedProvider([act("list_my_orders"), act("ask_customer", question=QUESTIONS[0])])
    runner = Runner(store, provider)
    runner.run(alice, run["id"])
    assert len(provider.seen_contexts) == 2
    trace = provider.seen_contexts[1]["observations"]
    assert trace[-1]["tool"] == "list_my_orders"
    assert "ord-alice-return" in str(trace[-1]["observation"])
    assert "ord-bob" not in str(provider.seen_contexts)
    view = actions.view(alice, sess["id"])
    assert view["runs"][0]["payload"]["state"] == "waiting_customer"
    with store.transaction() as db:
        calls = db.execute("SELECT * FROM calls").fetchall()
        assert len(calls) == 2
        assert all(c["usage_kind"] == "synthetic" and c["tokens"] == 128 for c in calls)


def test_guard_rejection_returns_to_model(setup):
    store, alice, actions, sess, run = setup
    provider = ScriptedProvider([act("execute_refund", amount=999),
                                 act("get_order", order_id="ord-bob-return"),
                                 act("ask_customer", question=QUESTIONS[0])])
    Runner(store, provider).run(alice, run["id"])
    assert provider.seen_contexts[1]["observations"][-1]["observation"]["error"] == "invalid_tool"
    assert provider.seen_contexts[2]["observations"][-1]["observation"]["error"] == "not_found"
    with store.transaction() as db:
        assert store.list(db, "application") == []


def test_unknown_usage_blocks_action_and_next_message(setup):
    store, alice, actions, sess, run = setup

    class Unknown:
        is_test_provider = False
        fingerprint = "unknown-test-probe"

        def decide(self, context, schema):
            return ProviderReply(act("list_my_orders"), None, "test-endpoint")

    Runner(store, Unknown()).run(alice, run["id"])
    view = actions.view(alice, sess["id"])
    assert view["runs"][0]["payload"]["state"] == "blocked_unknown"
    assert view["session"]["payload"]["blocked_unknown"] is True
    assert view["evidences"] == []


def test_cancel_while_model_is_running_discards_late_action(setup):
    store, alice, actions, sess, run = setup
    started, release = threading.Event(), threading.Event()

    class Delayed:
        is_test_provider = True
        fingerprint = "delayed-test"

        def decide(self, context, schema):
            started.set()
            assert release.wait(5)
            return ProviderReply(act("list_my_orders"), 128, "TEST")

    runner = Runner(store, Delayed())
    worker = threading.Thread(target=runner.run, args=(alice, run["id"]))
    worker.start()
    try:
        assert started.wait(5)
        latest = actions.view(alice, sess["id"])["session"]
        actions.cancel(alice, sess["id"], VersionedCommand(action_id="cancel", expected_version=latest["version"]))
    finally:
        release.set()
        worker.join(5)
    assert not worker.is_alive()
    view = actions.view(alice, sess["id"])
    assert view["evidences"] == []
    assert view["runs"][0]["payload"]["state"] == "cancelled"
    with store.transaction() as db:
        assert db.execute("SELECT state FROM calls").fetchone()[0] == "discarded"


def test_recovery_does_not_replay_started_call(setup):
    store, alice, actions, sess, run = setup
    with store.transaction() as db:
        db.execute("INSERT INTO calls(id,run_id,owner,state,usage_kind,context) VALUES(?,?,?,?,?,?)",
                   ("call-crash", run["id"], alice.user_id, "started", "real", "{}"))
    provider = ScriptedProvider([act("list_my_orders")])
    Runner(store, provider).recover()
    assert provider.seen_contexts == []
    assert actions.view(alice, sess["id"])["session"]["payload"]["blocked_unknown"] is True


def test_no_model_claim_is_published_as_payment_fact(setup):
    store, alice, actions, sess, run = setup
    provider = ScriptedProvider([act("respond", kind="empathy", evidence_ids=[], text="退款999元已到账")])
    Runner(store, provider).run(alice, run["id"])
    view = actions.view(alice, sess["id"])
    assert "已到账" not in str(view["session"]["payload"]["messages"])


def test_budget_terminates_repeated_legal_reads(setup):
    store, alice, actions, sess, run = setup
    provider = ScriptedProvider([act("list_my_orders")] * 15)
    Runner(store, provider, max_calls=3).run(alice, run["id"])
    assert len(provider.seen_contexts) == 3
    assert actions.view(alice, sess["id"])["runs"][0]["payload"]["state"] == "budget_exhausted"
