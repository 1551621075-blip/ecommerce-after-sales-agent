"""Frozen evaluation definitions only: these tests do not claim real-model execution."""
import json
from collections import Counter
from pathlib import Path

import pytest

CASES_FILE = Path(__file__).resolve().parents[1] / "evals" / "cases.json"
CORE_IDS = {"regular-01", "regular-02", "regular-04", "regular-07", "regular-11",
            "missing-01", "missing-04", "missing-05"}
GROUPS = {"regular": 12, "missing_or_changed": 6, "permissions_or_injection": 6,
          "idempotency_or_recovery": 6}
REQUIRED = {"id", "group", "message", "setup", "events", "allowed_terminal", "required_evidence",
            "forbidden_actions", "real_model_core", "oracle", "coverage", "driver"}
UNSAFE = {"execute_payment", "approve_refund", "cross_user_access", "invent_evidence", "auto_confirm"}


def unique_object(pairs):
    value = {}
    for key, item in pairs:
        assert key not in value, f"duplicate JSON key: {key}"
        value[key] = item
    return value


@pytest.fixture
def cases():
    assert CASES_FILE.is_file(), "Frozen evaluation cases are not implemented"
    return json.loads(CASES_FILE.read_text(encoding="utf-8"), object_pairs_hook=unique_object,
                      parse_constant=lambda _: pytest.fail("nonfinite JSON"))


def test_eval_cases_exist_red():
    assert CASES_FILE.is_file(), "Frozen evaluation cases are not implemented"


def test_frozen_counts_and_unique_ids(cases):
    assert type(cases) is list and len(cases) == 30
    assert len({case["id"] for case in cases}) == 30
    assert Counter(case["group"] for case in cases) == GROUPS
    assert {case["id"] for case in cases if case["real_model_core"]} == CORE_IDS


def test_case_contract_and_oracle_is_not_customer_dialogue(cases):
    for case in cases:
        assert REQUIRED <= set(case)
        assert type(case["real_model_core"]) is bool
        assert type(case["message"]) is str and case["message"].strip()
        assert type(case["setup"]) is dict
        assert case["setup"]["seed"] == "synthetic-v1"
        assert case["setup"]["customer"] in {"alice", "bob"}
        assert case["setup"]["now"] == 10_000_000
        assert case["driver"] in {"dialogue", "program"}
        assert type(case["events"]) is list
        for event in case["events"]:
            assert {"at", "type", "actor", "payload"} <= set(event)
            assert type(event["payload"]) is dict
            assert "oracle" not in event and "expected_action" not in event["payload"]
        for field in ("allowed_terminal", "required_evidence", "forbidden_actions", "coverage"):
            assert type(case[field]) is list and case[field]
            assert all(type(value) is str and value for value in case[field])
            assert len(case[field]) == len(set(case[field]))
        assert UNSAFE <= set(case["forbidden_actions"])
        assert type(case["oracle"]) is dict
        assert case["oracle"]["assertions"]
        assert case["oracle"]["tool_order"] == "not_fixed"
        assert case["oracle"]["model_input"] is False
        assert "oracle" not in case["message"].lower()
        assert "oracle" not in case["setup"]
        assert "expected_action" not in case and "tool_sequence" not in case


def test_core_set_covers_required_capabilities(cases):
    tags = set().union(*(set(case["coverage"]) for case in cases if case["real_model_core"]))
    assert {"query", "clarification", "observation_change", "ineligible", "human_submission", "status_followup"} <= tags
    assert sum(case["real_model_core"] for case in cases) == 8


def test_concurrent_security_and_recovery_negatives_require_program_driver(cases):
    program_tags = {"parallel_quantity", "double_confirm", "human_takeover", "unknown_recovery",
                    "stale_confirmation", "cross_session_evidence", "staff_authorization", "late_result"}
    found = set()
    for case in cases:
        matched = set(case["coverage"]) & program_tags
        if matched:
            found |= matched
            assert case["driver"] == "program"
            assert case["events"]
            assert case["oracle"]["requires_program_assertions"] is True
        if case["group"] == "idempotency_or_recovery":
            assert case["driver"] == "program" and case["events"]
    assert found == program_tags


def test_scenarios_reference_real_synthetic_business_cases(cases):
    from after_sales.synthetic import seed_data

    orders = {order["id"] for order in seed_data(10_000_000)["orders"]}
    for case in cases:
        assert set(case["setup"].get("orders", [])) <= orders
    coverage = set().union(*(set(case["coverage"]) for case in cases))
    assert {"return", "exchange", "not_received", "cancel", "policy", "shipment", "damaged",
            "missing_order", "missing_quantity", "observation_injection", "customer_injection"} <= coverage


def test_eval_definitions_do_not_claim_execution(cases):
    for case in cases:
        assert "passed" not in case and "real_model_result" not in case
        assert case["oracle"]["status"] == "specified_not_executed"
