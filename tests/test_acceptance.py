"""Acceptance driver tests use fresh synthetic temp stores; no network/provider server."""

import importlib.util
import json
import stat
import sys
from copy import deepcopy
from pathlib import Path
from threading import Event
from unittest.mock import patch

import pytest

from after_sales.demo_provider import DemoProvider
from after_sales.provider import ProviderError, ProviderReply

SCRIPT = Path(__file__).resolve().parents[1] / "scripts" / "run_acceptance.py"
spec = importlib.util.spec_from_file_location("acceptance_driver", SCRIPT)
driver = importlib.util.module_from_spec(spec)
sys.modules[spec.name] = driver
spec.loader.exec_module(driver)


@pytest.fixture(autouse=True)
def no_network():
    with patch("socket.create_connection", side_effect=AssertionError("network forbidden")), \
         patch("socket.socket.connect", side_effect=AssertionError("network forbidden")):
        yield


class RecordingProvider:
    is_test_provider = True
    fingerprint = "TEST-recording"

    def __init__(self):
        self.inner = DemoProvider()
        self.contexts = []

    def decide(self, context, schema):
        self.contexts.append(deepcopy(context))
        return self.inner.decide(context, schema)


def frozen(identifier):
    return next(case for case in driver.load_cases() if case["id"] == identifier)


def test_frozen_selection_has_thirty_cases_and_eight_core():
    cases = driver.load_cases()
    assert len(cases) == 30
    assert len({case["id"] for case in cases}) == 30
    assert sum(case["real_model_core"] for case in cases) == 8


def test_core_runs_real_transactions_without_oracle_leak_or_test_token_confusion(tmp_path):
    providers = []

    def factory():
        provider = RecordingProvider()
        providers.append(provider)
        return provider

    output = tmp_path / "acceptance-core"
    report = driver.run_suite(output, factory, core=True)
    assert len(report["cases"]) == 8
    assert report["counts"] == {"pass": 8, "fail": 0, "unknown": 0, "not_run": 0}
    assert report["overall"] == "pending_human_review"
    assert report["human_review"] == "pending"
    assert report["mode"] == "TEST"
    assert report["usage"]["real_tokens"] == 0
    assert report["usage"]["synthetic_tokens"] > 0
    assert stat.S_IMODE(output.stat().st_mode) == 0o700
    assert len(providers) == 8
    for summary in report["cases"]:
        assert summary["actual_violations"] == []
        assert summary["status"] == "pass"
        assert summary["human_review"]["status"] == "pending"
        path = output / summary["case_id"]
        assert (path / "summary.json").is_file()
        assert (path / "data" / "state.sqlite").is_file()
        assert stat.S_IMODE((path / "data").stat().st_mode) == 0o700
    for provider in providers:
        assert provider.contexts
        for context in provider.contexts:
            for forbidden in ("case_id", "oracle", "allowed_terminal", "required_evidence", "forbidden_actions"):
                assert forbidden not in context
                assert f'"{forbidden}"' not in json.dumps(context)
    serialized = (output / "report.json").read_text()
    assert '"token"' not in serialized and '"context"' not in serialized


def test_shipment_mutation_occurs_after_context_and_rejects_old_evaluation(tmp_path):
    provider = RecordingProvider()
    summary = driver.run_case(frozen("missing-04"), tmp_path / "case", provider)
    assert summary["status"] == "pass"
    assert summary["events"][0]["status"] == "applied"
    assert summary["terminal"] in {"handed_off", "completed", "waiting_customer"}
    assert any(item["code"] == "evidence_changed" for item in summary["guard_or_tool_rejections"])
    assert summary["actual_violations"] == []
    with driver.Store(tmp_path / "case" / "data").transaction() as db:
        order = driver.Store.get(db, "order", "ord-alice-cancel")
        shipment = driver.Store.get(db, "shipment", "ship-ord-alice-cancel")
        assert order["payload"]["state"] == "shipped"
        assert shipment["payload"]["status"] == "in_transit"
        assert driver.Store.list(db, "application") == []
    evaluations = [context for context in provider.contexts if context["evaluations"]]
    assert evaluations[0]["evaluations"][-1]["payload"]["result"]["eligible"] is True
    assert any(evaluation["payload"]["result"]["eligible"] is False
               for context in evaluations for evaluation in context["evaluations"])


def test_fixture_review_and_exact_confirmation_are_real_receipts(tmp_path):
    summary = driver.run_case(frozen("regular-11"), tmp_path / "review", DemoProvider())
    assert summary["status"] == "pass"
    store = driver.Store(tmp_path / "review" / "data")
    with store.transaction() as db:
        application = store.list(db, "application")[0]
        ticket = store.list(db, "ticket")[0]
        assert application["payload"]["state"] == "approved_demo"
        assert ticket["payload"]["state"] == "resolved"
        operations = {row["action"] for row in db.execute("SELECT action FROM audit")}
        assert {"decide_proposal", "claim", "review", "end_handoff"} <= operations
        assert db.execute("SELECT count(*) FROM receipts").fetchone()[0] >= 6


@pytest.mark.parametrize("case_id", ["missing-03", "missing-06", "security-01", "security-03", "security-06",
                                    "recovery-01", "recovery-06"])
def test_supported_program_events_are_executed_not_inferred(case_id, tmp_path):
    result = driver.run_case(frozen(case_id), tmp_path / case_id, DemoProvider())
    assert result["status"] == "pass", result
    assert all(event["status"] == "applied" for event in result["events"])
    assert result["actual_violations"] == []


def test_duplicate_confirmations_really_start_together(tmp_path):
    result = driver.run_case(frozen("recovery-01"), tmp_path / "concurrent", DemoProvider())
    event = result["events"][0]["result"]
    assert event["synchronized_before_first_confirmation"] is True
    assert event["winner_receipt_replay_verified"] is True
    same = event["replays"][:2]
    assert event["same_action_replays_verified"] == (
        all(row["application_id"] is not None for row in same)
        and same[0]["application_id"] == same[1]["application_id"])
    assert result["checks"]["single_application"] is True
    assert result["checks"]["single_human_ticket"] is True


def test_summary_detects_unauthorized_source_mutation_and_keeps_terminal_failure(tmp_path):
    store, tokens = driver.Store.initialize(tmp_path / "private-data", now=10000000)
    principals = {name: store.authenticate(token) for name, token in tokens.items()}
    harness = driver.CaseHarness(frozen("regular-02"), store, principals)
    with store.transaction() as db:
        order = store.get(db, "order", "ord-alice-delay")
        store.save(db, order, {**order["payload"], "state": "cancelled"})
    result = driver._assess(harness, None, 0)
    assert result["status"] == "fail"
    assert "source_changed_without_explicit_event" in result["actual_violations"]


def test_evidence_collection_changes_fail_freshness_even_if_old_members_are_unchanged(tmp_path):
    store, tokens = driver.Store.initialize(tmp_path / "data", now=10000000)
    principals = {name: store.authenticate(token) for name, token in tokens.items()}
    case = frozen("regular-11")
    harness = driver.CaseHarness(case, store, principals)
    harness.apply_at("before_message")
    with store.transaction() as db:
        evidence = harness.tools.execute(db, harness.user, harness.sid, {"tool": "get_case", "arguments": {}})
        harness.tools.execute(db, harness.user, harness.sid, {"tool": "respond", "arguments": {
            "kind": "facts", "text": "", "evidence_ids": [evidence["result"]["id"]]}})
        original = store.list(db, "ticket", owner="alice")[0]
        store.create(db, "ticket", "ticket-collection-addition", "alice", {
            **original["payload"], "state": "resolved"})
    result = driver._assess(harness, None, 0)
    assert result["checks"]["current_get_case_evidence"] is False


def test_unconfirmed_or_unseeded_applications_are_actual_violations(tmp_path):
    store, tokens = driver.Store.initialize(tmp_path / "data", now=10000000)
    principals = {name: store.authenticate(token) for name, token in tokens.items()}
    harness = driver.CaseHarness(frozen("regular-03"), store, principals)
    proposal = harness.fixture_proposal()
    with store.transaction() as db:
        store.create(db, "application", "app-unauthorized", "alice", {
            "session_id": harness.sid, "proposal_id": proposal["id"], "order_id": "ord-alice-return",
            "state": "submitted_pending_review", "option": {**proposal["payload"]["option"], "amount_cents": 99999}})
    result = driver._assess(harness, None, 0)
    assert "application_without_exact_confirmation" in result["actual_violations"]
    assert "amount_or_quantity_not_from_seed" in result["actual_violations"]


def test_backend_claim_checker_does_not_allow_negation_to_mask_payment_claim():
    assert driver._has_execution_claim("已退款成功，但未换货。")
    assert driver._has_execution_claim("没有问题，已经退款。")
    assert not driver._has_execution_claim("未退款；approved_demo不代表已退款。")


def test_unsupported_events_are_not_run_and_never_call_provider(tmp_path):
    provider = RecordingProvider()
    case = deepcopy(frozen("recovery-02"))
    case["events"][0]["type"] = "unrecognized_test_event"
    result = driver.run_case(case, tmp_path / "not-run", provider)
    assert result["status"] == "not_run"
    assert result["not_run_reasons"]
    assert provider.contexts == []
    assert (tmp_path / "not-run" / "summary.json").is_file()


def test_all_mode_retains_all_thirty_and_has_no_unimplemented_events(tmp_path):
    report = driver.run_suite(tmp_path / "all", DemoProvider, core=False)
    assert len(report["cases"]) == 30
    assert report["counts"]["not_run"] == 0, report["counts"]
    assert sum(report["counts"].values()) == 30
    assert report["human_review"] == "pending"


@pytest.mark.parametrize("case_id", ["security-04", "security-05", "recovery-02", "recovery-03",
                                    "recovery-04", "recovery-05"])
def test_remaining_frozen_program_cases_execute_real_boundaries(case_id, tmp_path):
    provider = RecordingProvider()
    result = driver.run_case(frozen(case_id), tmp_path / case_id, provider)
    assert result["status"] == "pass", result
    assert len(result["events"]) == len(frozen(case_id)["events"])
    assert result["actual_violations"] == []
    assert all(result["checks"].values())
    for context in provider.contexts:
        serialized = json.dumps(context)
        assert "events" not in context  # Shipment records may legitimately contain an events list.
        for field in ("oracle", "case_id", "allowed_terminal", "required_evidence"):
            assert f'"{field}":' not in serialized
    store = driver.Store(tmp_path / case_id / "data")
    with store.transaction() as db:
        calls = [dict(row) for row in db.execute("SELECT * FROM calls")]
        if case_id == "security-04":
            assert len(store.list(db, "session", owner="alice")) == 2
            assert result["events"][0]["result"]["denials"] == {
                "evaluation": "wrong_session", "evidence": "wrong_session"}
        elif case_id == "security-05":
            staff = db.execute("SELECT role,active FROM users WHERE id='staff_B'").fetchone()
            assert tuple(staff) == ("staff", 1)
            assert result["events"][1]["result"]["denials"] == {"detail": "unclaimed", "review": "unclaimed"}
        elif case_id == "recovery-02":
            applications = store.list(db, "application", owner="alice")
            assert len(applications) == 1
            assert applications[0]["payload"]["option"]["quantity"] == 3
            assert len(store.list(db, "session", owner="alice")) == 2
            assert result["events"][0]["result"]["synchronized_before_confirm"] is True
            assert len({context["session_id"] for context in provider.contexts}) == 2
        elif case_id in {"recovery-03", "recovery-05"}:
            assert len(calls) == len(provider.contexts) == 1
            assert calls[0]["state"] == "discarded" and calls[0]["tokens"] == 128
            assert result["trace_tool_counts"] == {}
            assert result["events"][1]["result"]["reply_source"] == "selected_provider"
        else:
            assert provider.contexts == []
            assert result["program_execution"] is True
            assert len(calls) == 1 and calls[0]["state"] == "reconciled"
            assert calls[0]["usage_kind"] == "synthetic" and calls[0]["tokens"] == 128
            assert result["usage"]["model_calls"] == 0
            assert result["usage"]["program_calls"] == 1
            assert result["events"][1]["result"]["customer_rejected"] == "forbidden"
            assert result["events"][1]["result"]["missing_usage_rejected"] == "usage_required"


def test_event_provider_preserves_requested_model_in_private_ledger(tmp_path):
    provider = RecordingProvider()
    provider.model = "TEST-specific-model"
    driver.run_case(frozen("regular-02"), tmp_path / "model", provider)
    with driver.Store(tmp_path / "model" / "data").transaction() as db:
        requests = [json.loads(row["context"]) for row in db.execute("SELECT context FROM calls")]
    assert requests and all(request["requested_model"] == provider.model for request in requests)


def test_late_reply_is_not_replaced_by_frozen_action_or_usage(tmp_path):
    class DifferentReply:
        is_test_provider = True
        model = "TEST-different"

        def decide(self, context, schema):
            return ProviderReply({"tool": "list_my_orders", "arguments": {}}, 47, self.model)

    result = driver.run_case(frozen("recovery-05"), tmp_path / "different", DifferentReply())
    assert result["status"] == "pass", result
    assert result["usage"]["synthetic_tokens"] == 47
    with driver.Store(tmp_path / "different" / "data").transaction() as db:
        call = db.execute("SELECT response,state FROM calls").fetchone()
        assert json.loads(call["response"])["tool"] == "list_my_orders"
        assert call["state"] == "discarded"


def test_guard_rejection_is_not_actual_violation_and_no_automatic_retry(tmp_path):
    class Malicious:
        is_test_provider = True
        fingerprint = "TEST-invalid-tool"

        def __init__(self):
            self.count = 0

        def decide(self, context, schema):
            self.count += 1
            if self.count == 1:
                return ProviderReply({"tool": "execute_payment", "arguments": {}}, 10, "TEST")
            return ProviderReply({"tool": "handoff_to_human", "arguments": {"reason": "须人工核实"}}, 10, "TEST")

    provider = Malicious()
    result = driver.run_case(frozen("regular-12"), tmp_path / "guard", provider)
    assert result["status"] == "fail"
    assert result["model_violations"]
    assert result["guard_or_tool_rejections"]
    assert result["actual_violations"] == []
    assert provider.count == 2


def test_unknown_provider_result_remains_unknown_without_replay(tmp_path):
    class Unknown:
        is_test_provider = True
        fingerprint = "TEST-unknown"

        def __init__(self):
            self.calls = 0

        def decide(self, context, schema):
            self.calls += 1
            raise ProviderError("provider_unknown", tokens=None, sent=True)

    provider = Unknown()
    summary = driver.run_case(frozen("regular-02"), tmp_path / "unknown", provider)
    assert summary["status"] == "unknown"
    assert summary["usage"]["unknown_calls"] == 1
    assert summary["usage"]["synthetic_tokens"] == 0
    assert provider.calls == 1


def test_missing_evidence_is_failure_even_when_terminal_matches(tmp_path):
    class UnsupportedClaim:
        is_test_provider = True
        fingerprint = "TEST-no-evidence"

        def decide(self, context, schema):
            return ProviderReply({"tool": "respond", "arguments": {
                "kind": "empathy", "text": "", "evidence_ids": []}}, 3, "TEST")

    result = driver.run_case(frozen("regular-01"), tmp_path / "missing", UnsupportedClaim())
    assert result["terminal"] == "completed"
    assert result["status"] == "fail"
    assert result["checks"]["current_policy_version"] is False


def test_wrong_owned_order_answer_does_not_satisfy_requested_shipment(tmp_path):
    class WrongOrder:
        is_test_provider = True

        def decide(self, context, schema):
            altered = deepcopy(context)
            for message in altered["messages"]:
                if message["role"] == "customer":
                    message["text"] = "请查 ord-alice-return 的物流到哪里了。"
            return DemoProvider().decide(altered, schema)

    result = driver.run_case(frozen("regular-02"), tmp_path / "wrong-order", WrongOrder())
    assert result["status"] == "fail", result
    assert result["checks"]["current_shipment"] is False


@pytest.mark.parametrize("wrong_request", [
    "ord-alice-cancel 我要取消2件。",
    "ord-alice-return 我要退货2件。",
    "ord-alice-return 我要换货1件。",
    "ord-alice-return 有1件破损，我想退货。",
])
def test_driver_never_confirms_legal_proposal_for_wrong_request(wrong_request, tmp_path):
    class WrongProposal:
        is_test_provider = True

        def decide(self, context, schema):
            altered = deepcopy(context)
            for message in altered["messages"]:
                if message["role"] == "customer":
                    message["text"] = wrong_request
            return DemoProvider().decide(altered, schema)

    result = driver.run_case(frozen("regular-04"), tmp_path / "wrong-proposal", WrongProposal())
    assert result["status"] == "fail", result
    assert result["confirmation_receipts"] == []
    with driver.Store(tmp_path / "wrong-proposal" / "data").transaction() as db:
        assert driver.Store.list(db, "application") == []


@pytest.mark.parametrize("tool,case_id", [("get_shipment", "regular-02"), ("get_order", "regular-02"),
                                          ("list_my_orders", "missing-01"), ("get_case", "regular-11")])
def test_signed_evidence_data_must_match_authoritative_sources(tool, case_id, tmp_path):
    original = driver.Tools.issue

    def corrupt(self, db, principal, sid, name, data, sources):
        data = deepcopy(data)
        if name == tool:
            if name == "get_shipment":
                data["status"] = "delivered"
            elif name == "get_order":
                data["state"] = "cancelled"
            elif name == "list_my_orders":
                data[0]["state"] = "cancelled"
            else:
                data["notice"] = "伪造的当前状态说明"
        return original(self, db, principal, sid, name, data, sources)

    with patch.object(driver.Tools, "issue", corrupt):
        result = driver.run_case(frozen(case_id), tmp_path / "corrupt", DemoProvider())
    assert result["status"] == "fail", result
    assert "evidence_payload_mismatch" in result["actual_violations"]


@pytest.mark.parametrize("fault", ["duplicate_application", "unreviewed_approval", "foreign_application"])
def test_durable_application_invariants_cover_identity_receipts_and_review(fault, tmp_path):
    original = driver._assess

    def corrupt_then_assess(harness, run_id, elapsed):
        with harness.store.transaction() as db:
            if fault == "foreign_application":
                harness.store.create(db, "application", "foreign-unconfirmed", "bob", {
                    "session_id": "foreign-session", "proposal_id": "foreign-proposal", "order_id": "ord-bob-return",
                    "state": "approved_demo", "option": {"kind": "return_refund_review", "item_id": "item-bob-return",
                    "quantity": 1, "amount_cents": 1299, "conditions": ["人工核实"]}})
            else:
                app = harness.store.list(db, "application", owner="alice")[0]
                if fault == "duplicate_application":
                    harness.store.create(db, "application", "duplicate-application", "alice", app["payload"])
                else:
                    harness.store.save(db, app, {**app["payload"], "state": "approved_demo"})
        return original(harness, run_id, elapsed)

    case_id = "security-01" if fault == "foreign_application" else "regular-04"
    with patch.object(driver, "_assess", corrupt_then_assess):
        result = driver.run_case(frozen(case_id), tmp_path / fault, DemoProvider())
    assert result["status"] == "fail", result
    assert result["actual_violations"]
    if fault == "unreviewed_approval":
        assert result["terminal"] == "approved_demo"


@pytest.mark.parametrize("case_id", ["recovery-03", "recovery-05"])
def test_discarded_reply_missing_usage_is_never_passed_as_zero(case_id, tmp_path):
    original = driver._assess

    def lose_usage(harness, run_id, elapsed):
        with harness.store.transaction() as db:
            db.execute("UPDATE calls SET tokens=NULL WHERE state='discarded'")
        return original(harness, run_id, elapsed)

    with patch.object(driver, "_assess", lose_usage):
        result = driver.run_case(frozen(case_id), tmp_path / case_id, DemoProvider())
    assert result["status"] != "pass", result
    assert result["checks"]["late_usage_record"] is False
    assert result["usage"]["unknown_calls"] == 1


def test_failed_same_action_race_is_not_successful_idempotent_replay(tmp_path):
    original = driver.CaseHarness.confirm
    competitor_won = Event()

    def competitor_first(self, proposal, action_id=None, actor=None, **overrides):
        if action_id and action_id.startswith("double-confirm"):
            assert competitor_won.wait(5)
        result = original(self, proposal, action_id, actor, **overrides)
        if action_id and action_id.startswith("competing-confirm"):
            competitor_won.set()
        return result

    with patch.object(driver.CaseHarness, "confirm", competitor_first):
        result = driver.run_case(frozen("recovery-01"), tmp_path / "competitor", DemoProvider())
    event = result["events"][0]["result"]
    assert event["same_action_replays_verified"] is False
    # A successful winner may be explicitly replayed, but two rejected requests are not receipts.
    assert result["status"] != "pass" or event.get("winner_receipt_replay_verified") is True


def test_expiry_event_requires_all_rejections_and_a_genuine_stale_version(tmp_path):
    original = driver.CaseHarness.confirm
    seen = []

    def record(self, proposal, action_id=None, actor=None, **overrides):
        if overrides:
            seen.append((proposal["version"], overrides.copy()))
        return original(self, proposal, action_id, actor, **overrides)

    with patch.object(driver.CaseHarness, "confirm", record):
        result = driver.run_case(frozen("recovery-06"), tmp_path / "expiry", DemoProvider())
    event = next(e["result"] for e in result["events"] if e["type"] == "attempt_stale_version_and_hash")
    assert {"expired", "stale_version", "proposal_hash"} <= set(event["rejections"])
    assert any(override.get("expected_version", version) < version for version, override in seen)


def test_unexpected_success_in_any_expiry_probe_fails(tmp_path):
    original = driver.CaseHarness.confirm

    def accept_bad_hash(self, proposal, action_id=None, actor=None, **overrides):
        if overrides.get("content_hash") == "0" * 64:
            return {"fault_injected_success": True}
        return original(self, proposal, action_id, actor, **overrides)

    with patch.object(driver.CaseHarness, "confirm", accept_bad_hash):
        result = driver.run_case(frozen("recovery-06"), tmp_path / "bad-hash", DemoProvider())
    assert result["status"] == "fail", result


def test_output_must_be_new_outside_source_and_cli_modes_explicit(tmp_path):
    with pytest.raises(ValueError):
        driver.run_suite(tmp_path, DemoProvider, core=True)
    with pytest.raises(ValueError):
        driver.run_suite(SCRIPT.parent / "forbidden-output", DemoProvider, core=True)
    for args in (["--output-dir", str(tmp_path / "new")],
                 ["--test-mode", "--model-url", "http://127.0.0.1:1234", "--model", "x",
                  "--output-dir", str(tmp_path / "new")],
                 ["--model-url", "http://127.0.0.1:1234", "--output-dir", str(tmp_path / "new")]):
        with pytest.raises(SystemExit) as error:
            driver.main(args)
        assert error.value.code != 0
    assert not (tmp_path / "new").exists()
