import pytest

from after_sales.actions import Actions
from after_sales.schemas import Command, DomainError
from after_sales.storage import Store
from after_sales.tools import Tools


def setup(tmp_path):
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    alice = store.authenticate(tokens["alice"])
    actions = Actions(store)
    sess = actions.create_session(alice, Command(action_id="new"))
    return store, alice, sess, Tools(actions)


def test_new_policy_invalidates_prior_search_snapshot(tmp_path):
    store, alice, sess, tools = setup(tmp_path)
    with store.transaction() as db:
        result = tools.execute(db, alice, sess["id"], {
            "tool": "search_policies", "arguments": {"query": "退货", "category": "standard"}})
        ev = result["result"]
        existing = store.list(db, "policy")[0]
        added = {**existing["payload"], "id": "new-policy"}
        store.create(db, "policy", "new-policy", "system", added)
        with pytest.raises(DomainError, match="变化"):
            tools.current_evidence(db, alice, sess["id"], ev["id"])


def test_evidence_cross_session_reference_is_rejected(tmp_path):
    store, alice, sess, tools = setup(tmp_path)
    other = tools.actions.create_session(alice, Command(action_id="another"))
    with store.transaction() as db:
        ev = tools.execute(db, alice, sess["id"], {"tool": "list_my_orders", "arguments": {}})["result"]
        with pytest.raises(DomainError):
            tools.current_evidence(db, alice, other["id"], ev["id"])


def test_empty_case_evidence_expires_when_new_case_object_exists(tmp_path):
    store, alice, sess, tools = setup(tmp_path)
    with store.transaction() as db:
        ev = tools.execute(db, alice, sess["id"], {"tool": "get_case", "arguments": {}})["result"]
        store.create(db, "proposal", "prop-new", "alice", {"session_id": sess["id"], "state": "declined"})
        with pytest.raises(DomainError, match="变化"):
            tools.current_evidence(db, alice, sess["id"], ev["id"])
