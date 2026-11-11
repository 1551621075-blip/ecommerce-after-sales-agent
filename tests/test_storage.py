import pytest

from after_sales.schemas import DomainError, Principal
from after_sales.storage import Store


def test_initialize_auth_and_no_overwrite(tmp_path):
    store, tokens = Store.initialize(tmp_path / "new", now=1900000000)
    assert store.authenticate(tokens["alice"]) == Principal(user_id="alice", role="customer")
    assert store.authenticate(tokens["staff"]).role == "staff"
    with pytest.raises(DomainError):
        store.authenticate("not-a-token")
    with pytest.raises(DomainError):
        Store.initialize(tmp_path / "new", now=1900000000)


def test_cas_receipt_and_rollback(tmp_path):
    store, tokens = Store.initialize(tmp_path / "new", now=1900000000)
    alice = store.authenticate(tokens["alice"])
    with store.transaction() as db:
        obj = store.create(db, "session", "s-one", "alice", {"control": "bot"})
    with store.transaction() as db:
        changed = store.save(db, obj, {"control": "closed"})
        assert changed["version"] == 2
        with pytest.raises(DomainError, match="版本"):
            store.save(db, obj, {"control": "bot"})
        store.record_receipt(db, alice, "cmd-one", {"op": "test"}, {"ok": True})
        assert store.receipt(db, alice, "cmd-one", {"op": "test"}) == {"ok": True}
        with pytest.raises(DomainError):
            store.receipt(db, alice, "cmd-one", {"op": "different"})
    with pytest.raises(RuntimeError), store.transaction() as db:
        store.create(db, "session", "s-rollback", "alice", {})
        raise RuntimeError("rollback")
    with store.transaction() as db:
        with pytest.raises(DomainError):
            store.get(db, "session", "s-rollback")


def test_owner_scope_and_revocation(tmp_path):
    store, tokens = Store.initialize(tmp_path / "new", now=1900000000)
    alice = store.authenticate(tokens["alice"])
    with store.transaction() as db:
        store.create(db, "session", "s-one", "alice", {})
        with pytest.raises(DomainError):
            store.get(db, "session", "s-one", owner="bob")
        db.execute("UPDATE users SET active=0 WHERE id='alice'")
    with store.transaction() as db, pytest.raises(DomainError):
        store.require(db, alice, "customer")
