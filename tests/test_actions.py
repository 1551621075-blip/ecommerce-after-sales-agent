import pytest

from after_sales.actions import Actions
from after_sales.schemas import (
    Command,
    DomainError,
    MessageCommand,
    NoteCommand,
    ProposalCommand,
    ReviewCommand,
    VersionedCommand,
)
from after_sales.storage import Store


@pytest.fixture
def app(tmp_path):
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    actions = Actions(store)
    return actions, {key: store.authenticate(value) for key, value in tokens.items()}


def session(app):
    actions, people = app
    return actions.create_session(people["alice"], Command(action_id="new-session"))


def proposal(app, sid):
    actions, people = app
    with actions.store.transaction() as db:
        result = actions.evaluate(db, people["alice"], sid, {
            "order_id": "ord-alice-return", "item_id": "item-alice-return",
            "quantity": 1, "reason": "change_mind", "statement": "不想要了，未使用",
        })
        return actions.propose(db, people["alice"], sid, result["id"], "return_refund_review")


def confirm_command(obj, key="confirm-one"):
    return ProposalCommand(action_id=key, expected_version=obj["version"],
                           content_hash=obj["content_hash"], decision="confirm")


def test_proposal_is_not_application_and_confirm_is_idempotent(app):
    actions, people = app
    sess = session(app)
    prop = proposal(app, sess["id"])
    with actions.store.transaction() as db:
        assert actions.store.list(db, "application") == []
    receipt = actions.decide_proposal(people["alice"], prop["id"], confirm_command(prop))
    assert receipt["application"]["payload"]["state"] == "submitted_pending_review"
    assert receipt == actions.decide_proposal(people["alice"], prop["id"], confirm_command(prop))
    with pytest.raises(DomainError):
        actions.decide_proposal(people["alice"], prop["id"], confirm_command(prop, "another-key"))
    with pytest.raises(DomainError):
        actions.decide_proposal(people["bob"], prop["id"], confirm_command(prop))


def test_changed_evidence_requires_new_proposal(app):
    actions, people = app
    prop = proposal(app, session(app)["id"])
    with actions.store.transaction() as db:
        order = actions.store.get(db, "order", "ord-alice-return")
        changed = {**order["payload"], "state": "cancelled"}
        actions.store.save(db, order, changed)
    with pytest.raises(DomainError, match="证据"):
        actions.decide_proposal(people["alice"], prop["id"], confirm_command(prop))
    with actions.store.transaction() as db:
        assert actions.store.list(db, "application") == []


def test_cross_session_capacity_cannot_be_double_spent(app):
    actions, people = app
    first = session(app)
    second = actions.create_session(people["alice"], Command(action_id="session-two"))
    p1, p2 = proposal(app, first["id"]), proposal(app, second["id"])
    actions.decide_proposal(people["alice"], p1["id"], confirm_command(p1))
    with pytest.raises(DomainError):
        actions.decide_proposal(people["alice"], p2["id"], confirm_command(p2, "confirm-two"))


def test_staff_queue_claim_review_and_resume(app):
    actions, people = app
    sess = session(app)
    prop = proposal(app, sess["id"])
    receipt = actions.decide_proposal(people["alice"], prop["id"], confirm_command(prop))
    ticket = receipt["ticket"]
    queue = actions.queue(people["staff"])
    assert queue[0]["id"] == ticket["id"]
    assert "messages" not in str(queue)
    with pytest.raises(DomainError):
        actions.ticket_detail(people["staff"], ticket["id"])
    claimed = actions.claim(people["staff"], ticket["id"],
                            VersionedCommand(action_id="claim", expected_version=ticket["version"]))
    reviewed = actions.review(people["staff"], ticket["id"], ReviewCommand(
        action_id="review", expected_version=claimed["version"],
        decision="approved_demo", note="合成凭据核验完毕，仅批准Demo申请"))
    assert reviewed["application"]["payload"]["state"] == "approved_demo"
    assert "到账" not in reviewed["message"]
    actions.end_handoff(people["staff"], ticket["id"], NoteCommand(
        action_id="end", expected_version=reviewed["ticket"]["version"], note="已说明演示结果"))
    view = actions.view(people["alice"], sess["id"])
    assert view["session"]["payload"]["control"] == "bot"
    assert view["session"]["payload"]["active_run"] is None


def test_withdraw_wins_against_review(app):
    actions, people = app
    prop = proposal(app, session(app)["id"])
    receipt = actions.decide_proposal(people["alice"], prop["id"], confirm_command(prop))
    ticket = receipt["ticket"]
    claimed = actions.claim(people["staff"], ticket["id"],
                            VersionedCommand(action_id="claim", expected_version=ticket["version"]))
    application = receipt["application"]
    withdrawn = actions.withdraw(people["alice"], application["id"], VersionedCommand(
        action_id="withdraw", expected_version=application["version"]))
    assert withdrawn["payload"]["state"] == "withdrawn"
    with pytest.raises(DomainError):
        actions.review(people["staff"], ticket["id"], ReviewCommand(
            action_id="review", expected_version=claimed["version"], decision="approved_demo", note="通过"))


def test_message_and_command_conflicts(app):
    actions, people = app
    sess = session(app)
    command = MessageCommand(action_id="msg", expected_version=sess["version"], text="查订单")
    result = actions.message(people["alice"], sess["id"], command)
    assert actions.message(people["alice"], sess["id"], command) == result
    with pytest.raises(DomainError):
        actions.message(people["alice"], sess["id"], command.model_copy(update={"text": "换内容"}))
    with pytest.raises(DomainError):
        actions.view(people["operator"], sess["id"])


def test_natural_language_agreement_does_not_confirm(app):
    actions, people = app
    sess = session(app)
    prop = proposal(app, sess["id"])
    latest = actions.view(people["alice"], sess["id"])["session"]
    actions.message(people["alice"], sess["id"], MessageCommand(
        action_id="okay", expected_version=latest["version"], text="好的"))
    with actions.store.transaction() as db:
        assert actions.store.list(db, "application") == []
        assert actions.store.get(db, "proposal", prop["id"])["payload"]["state"] == "superseded"
