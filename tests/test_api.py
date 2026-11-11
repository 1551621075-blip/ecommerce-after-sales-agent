"""Isolated API/CLI tests. TestClient uses ASGI, never a real network server."""

import asyncio
from contextlib import nullcontext
from unittest.mock import AsyncMock, Mock, mock_open, patch

import pytest
from fastapi.testclient import TestClient
from typer.testing import CliRunner

from after_sales.api import create_app
from after_sales.schemas import DomainError, MessageCommand, Principal

ORIGIN = "http://127.0.0.1:8765"
AUTH = {"Authorization": "Bearer synthetic-test-token"}
MUTATION = {**AUTH, "Origin": ORIGIN, "Sec-Fetch-Site": "same-origin",
            "Sec-Fetch-Mode": "cors", "Sec-Fetch-Dest": "empty"}


@pytest.fixture
def api():
    store = Mock()
    store.authenticate.return_value = Principal(user_id="alice", role="customer")
    store.transaction.side_effect = lambda: nullcontext("db")
    store.list.return_value = [{"id": "sess-alice", "owner": "alice", "version": 1,
                                "payload": {"messages": [], "control": "bot"}}]
    service = Mock()
    service.submit.return_value = {"receipt": "queued"}
    service.actions.create_session.return_value = {"id": "sess-new", "version": 1}
    service.actions.view.return_value = {"session": {"id": "sess-alice", "version": 1}, "proposals": []}
    for method in ("cancel", "decide_proposal", "withdraw", "claim", "review", "end_handoff"):
        getattr(service.actions, method).return_value = {"ok": True}
    service.actions.queue.return_value = [{"id": "ticket-one", "version": 1, "category": "申请审核"}]
    service.actions.ticket_detail.return_value = {"ticket": {"id": "ticket-one"}}
    with patch("after_sales.api.Service", return_value=service) as factory:
        app = create_app(store, Mock(is_test_provider=True), origin=ORIGIN)
        with TestClient(app, base_url=ORIGIN) as client:
            yield client, store, service, factory, app
        service.close.assert_called_once_with()


def test_lifespan_auth_identity_and_read_only_polling(api):
    client, store, service, factory, app = api
    factory.assert_called_once()
    assert app.state.service is service
    me = client.get("/api/me", headers=AUTH)
    assert me.status_code == 200
    assert me.json() == {"user_id": "alice", "role": "customer", "test_mode": True}
    store.authenticate.assert_called_with("synthetic-test-token")
    for _ in range(3):
        assert client.get("/api/sessions/sess-alice", headers=AUTH).status_code == 200
    assert service.actions.view.call_count == 3
    service.submit.assert_not_called()
    service.actions.message.assert_not_called()


def test_session_listing_is_filtered_by_authenticated_owner(api):
    client, store, _, _, _ = api
    response = client.get("/api/sessions", headers=AUTH)
    assert response.status_code == 200
    store.require.assert_called_with("db", Principal(user_id="alice", role="customer"), "customer")
    store.list.assert_called_with("db", "session", owner="alice")
    assert response.json()[0]["id"] == "sess-alice"


def test_create_and_message_use_strict_commands_and_service_submission(api):
    client, _, service, _, _ = api
    assert client.post("/api/sessions", headers=MUTATION, json={"action_id": "action-new"}).status_code == 200
    command = {"action_id": "action-message", "expected_version": 1, "text": "查询我的订单"}
    response = client.post("/api/sessions/sess-alice/messages", headers=MUTATION, json=command)
    assert response.status_code == 200
    principal, sid, actual = service.submit.call_args.args
    assert principal.user_id == "alice" and sid == "sess-alice"
    assert isinstance(actual, MessageCommand) and actual.model_dump() == command


@pytest.mark.parametrize(("path", "method", "extra"), [
    ("/api/sessions/sess-alice/cancel", "cancel", {}),
    ("/api/proposals/prop-one/decision", "decide_proposal", {"content_hash": "a" * 64, "decision": "confirm"}),
    ("/api/applications/app-one/withdraw", "withdraw", {}),
    ("/api/tickets/ticket-one/claim", "claim", {}),
    ("/api/tickets/ticket-one/review", "review", {"decision": "approved_demo", "note": "仅Demo审批"}),
    ("/api/tickets/ticket-one/end", "end_handoff", {"note": "审核已结束"}),
])
def test_mutations_delegate_exact_action_id_version_and_payload(api, path, method, extra):
    client, _, service, _, _ = api
    body = {"action_id": "stable-action-id", "expected_version": 3, **extra}
    response = client.post(path, headers=MUTATION, json=body)
    assert response.status_code == 200
    assert getattr(service.actions, method).call_args.args[-1].model_dump() == body


def test_staff_queue_and_detail_delegate_to_authorized_actions(api):
    client, store, service, _, _ = api
    store.authenticate.return_value = Principal(user_id="staff-one", role="staff")
    assert client.get("/api/queue", headers=AUTH).json() == service.actions.queue.return_value
    assert client.get("/api/tickets/ticket-one", headers=AUTH).status_code == 200
    service.actions.ticket_detail.assert_called_once()


@pytest.mark.parametrize("headers", [
    {}, {"Authorization": "Basic nope"}, {"Authorization": "Bearer "},
    {"Authorization": "Bearer token extra"},
])
def test_bearer_is_required(api, headers):
    assert api[0].get("/api/me", headers=headers).status_code == 401


def test_bad_tokens_and_business_conflicts_are_safe_errors(api):
    client, store, service, _, _ = api
    store.authenticate.side_effect = DomainError("unauthorized", "认证失败", 401)
    assert client.get("/api/me", headers=AUTH).status_code == 401
    store.authenticate.side_effect = None
    service.actions.cancel.side_effect = DomainError("stale_version", "请刷新版本", 409)
    response = client.post("/api/sessions/sess-alice/cancel", headers=MUTATION,
                           json={"action_id": "cancel-one", "expected_version": 1})
    assert response.status_code == 409
    assert response.json()["error"]["code"] == "stale_version"


@pytest.mark.parametrize("host", ["localhost:8765", "evil.example", "127.0.0.1:8888", "2130706433:8765",
                                  "127.0.0.1:8765@evil.example", "127.1:8765"])
def test_only_configured_numeric_loopback_host_is_allowed(api, host):
    client, store, _, _, _ = api
    assert client.get("/api/me", headers={**AUTH, "Host": host}).status_code == 400
    store.authenticate.assert_not_called()


@pytest.mark.parametrize("change", [
    {"Origin": "http://evil.example"}, {"Origin": "null"}, {"Origin": "http://localhost:8765"},
    {"Origin": None}, {"Sec-Fetch-Site": "cross-site"}, {"Sec-Fetch-Site": "same-site"},
    {"Sec-Fetch-Site": None}, {"Sec-Fetch-Mode": "navigate"}, {"Sec-Fetch-Dest": "iframe"},
])
def test_mutations_require_exact_origin_and_browser_fetch_metadata(api, change):
    headers = {**MUTATION, **change}
    headers = {name: value for name, value in headers.items() if value is not None}
    response = api[0].post("/api/sessions", headers=headers, json={"action_id": "new-session"})
    assert response.status_code == 403
    api[2].actions.create_session.assert_not_called()


@pytest.mark.parametrize("header", ["Forwarded", "X-Forwarded-Host", "X-Forwarded-For", "X-Forwarded-Proto",
                                    "X-Real-IP", "X-Original-URL", "X-Rewrite-URL", "Proxy-Authorization"])
def test_proxy_headers_are_rejected(api, header):
    assert api[0].get("/api/me", headers={**AUTH, header: "127.0.0.1"}).status_code == 400


@pytest.mark.parametrize("header", ["Host", "Authorization", "Origin", "Content-Type", "Content-Length",
                                    "Sec-Fetch-Site", "Sec-Fetch-Mode", "Sec-Fetch-Dest"])
def test_duplicate_sensitive_headers_are_rejected(api, header):
    values = {**MUTATION, "Host": "127.0.0.1:8765", "Content-Type": "application/json", "Content-Length": "2"}
    headers = list(values.items()) + [(header.lower(), values[header])]
    response = api[0].post("/api/sessions", headers=headers, content=b"{}")
    assert response.status_code == 400


@pytest.mark.parametrize("path", ["/api/me?token=a&token=b", "/api/me?token=secret", "/api/%6de",
                                  "/api/sessions/sess%2Falice", "/api//me"])
def test_queries_and_encoded_or_ambiguous_paths_are_rejected(api, path):
    assert api[0].get(path, headers=AUTH).status_code == 400


@pytest.mark.parametrize("body", [
    b'{"action_id":"one","action_id":"two"}',
    b'{"action_id":"one","nested":{"key":1,"key":2}}',
    b'{"action_id":"one","value":NaN}', b'{"value":Infinity}',
    b'{"action_id":"one",}', b'[]', b'null', b'"text"', b'\xff',
    b'{"nested":' + b'[' * 25 + b'0' + b']' * 25 + b'}', b'{"text":"' + b'a' * 66000 + b'"}',
])
def test_json_rejects_duplicate_keys_nonfinite_deep_large_or_malformed_inputs(api, body):
    headers = {**MUTATION, "Content-Type": "application/json"}
    response = api[0].post("/api/sessions", headers=headers, content=body)
    assert response.status_code in {400, 413}
    api[2].actions.create_session.assert_not_called()


@pytest.mark.parametrize("body", [{"action_id": "new", "extra": 1}, {"action_id": True},
                                  {"expected_version": True, "action_id": "new"}])
def test_schema_errors_do_not_echo_user_input(api, body):
    response = api[0].post("/api/sessions", headers=MUTATION, json=body)
    assert response.status_code == 422
    assert response.json() == {"error": {"code": "invalid_command", "message": "请求字段不符合严格结构"}}


def test_version_bool_and_invalid_proposal_hash_are_not_coerced(api):
    client = api[0]
    response = client.post("/api/sessions/sess-alice/cancel", headers=MUTATION,
                           json={"action_id": "cancel-bool", "expected_version": True})
    assert response.status_code == 422
    response = client.post("/api/proposals/prop-one/decision", headers=MUTATION,
                           json={"action_id": "confirm-bad", "expected_version": 1,
                                 "content_hash": "forged", "decision": "confirm"})
    assert response.status_code == 422


@pytest.mark.parametrize("length", ["-1", "+2", "02", "2,2", "not-an-int"])
def test_ambiguous_content_lengths_are_rejected(api, length):
    response = api[0].post("/api/sessions", headers={**MUTATION, "Content-Type": "application/json",
                                                   "Content-Length": length}, content=b"{}")
    assert response.status_code == 400


def test_content_type_encoding_and_empty_body_rules(api):
    client = api[0]
    assert client.post("/api/sessions", headers=MUTATION, content="{}").status_code == 415
    assert client.post("/api/sessions", headers={**MUTATION, "Content-Type": "application/json",
                                                "Content-Encoding": "gzip"}, content=b"{}").status_code == 415
    assert client.get("/api/me", headers={**AUTH, "Sec-Fetch-Site": "cross-site"}).status_code == 403


def test_mode_banner_is_derived_only_from_provider_flag(api):
    _, store, service, _, _ = api
    with patch("after_sales.api.Service", return_value=service):
        app = create_app(store, Mock(is_test_provider=False), origin=ORIGIN)
        with TestClient(app, base_url=ORIGIN) as client:
            result = client.get("/api/me", headers=AUTH)
            assert result.json()["test_mode"] is False
    service.close.reset_mock()


def test_static_assets_and_security_headers(api):
    client = api[0]
    for path in ("/", "/static/app.js", "/static/style.css"):
        response = client.get(path)
        assert response.status_code == 200
        assert response.headers["X-Content-Type-Options"] == "nosniff"
        assert response.headers["Cache-Control"] == "no-store"
        assert "default-src 'none'" in response.headers["Content-Security-Policy"]
        assert "frame-ancestors 'none'" in response.headers["Content-Security-Policy"]
    assert client.get("/docs").status_code == 404
    assert client.get("/openapi.json").status_code == 404
    assert "innerHTML" not in client.get("/static/app.js").text
    assert "localStorage" not in client.get("/static/app.js").text
    assert "https://" not in client.get("/").text


def test_frontend_fixed_selectors_exist_and_dynamic_text_uses_safe_dom(api):
    import re
    from html.parser import HTMLParser

    class Identifiers(HTMLParser):
        def __init__(self):
            super().__init__()
            self.ids = set()

        def handle_starttag(self, tag, attrs):
            self.ids.update(value for name, value in attrs if name == "id")

    parser = Identifiers()
    parser.feed(api[0].get("/").text)
    script = api[0].get("/static/app.js").text
    fixed_ids = set(re.findall(r'\$\("([a-z-]+)"\)', script))
    assert fixed_ids <= parser.ids
    for unsafe in ("innerHTML", "outerHTML", "insertAdjacentHTML", "localStorage", "sessionStorage", "eval("):
        assert unsafe not in script
    assert "textContent" in script and "credentials: \"omit\"" in script


def test_invalid_origin_configuration_is_rejected():
    for origin in ("https://example.com", "http://localhost:8765", "http://user@127.0.0.1:8765",
                   "http://127.0.0.1:8765/path", "http://127.0.0.1:8765?x=1"):
        with pytest.raises(ValueError):
            create_app(Mock(), Mock(), origin=origin)


def test_metrics_polling_and_operator_reconciliation_are_separate(api):
    client, store, service, _, _ = api
    service.runner.metrics.return_value = {"calls": 2, "unknown": 1}
    response = client.get("/api/sessions/sess-alice/metrics", headers=AUTH)
    assert response.status_code == 200
    service.runner.metrics.assert_called_once_with(Principal(user_id="alice", role="customer"), "sess-alice")
    service.runner.reconcile.assert_not_called()
    store.authenticate.return_value = Principal(user_id="operator", role="operator")
    db = Mock()
    db.execute.return_value.fetchall.return_value = [
        {"id": "call-one", "run_id": "run-one", "tokens": None, "state": "unknown", "usage_kind": "real"}
    ]
    store.transaction.side_effect = lambda: nullcontext(db)
    response = client.get("/api/unknown-calls", headers=AUTH)
    assert response.status_code == 200
    assert set(response.json()[0]) == {"id", "run_id", "tokens", "state", "usage_kind"}
    query = db.execute.call_args.args[0].lower()
    assert "context" not in query and "response" not in query and "unknown" in query
    store.require.assert_called_with(db, Principal(user_id="operator", role="operator"), "operator")
    service.runner.reconcile.return_value = {"ok": True}
    body = {"action_id": "reconcile-one", "call_id": "call-one", "tokens": 12,
            "note": "人工核实用量", "outcome": "no_usable_result"}
    response = client.post("/api/reconcile", headers=MUTATION, json=body)
    assert response.status_code == 200
    assert service.runner.reconcile.call_args.args[-1].model_dump() == body
    service.submit.assert_not_called()


def test_stream_timeout_and_chunk_limit_prevent_downstream_actions():
    from after_sales.api import LocalBoundary

    async def exercise(timeout=False):
        downstream, send = AsyncMock(), AsyncMock()
        headers = [(key.lower().encode(), value.encode()) for key, value in
                   {**MUTATION, "Host": "127.0.0.1:8765", "Content-Type": "application/json"}.items()]
        scope = {"type": "http", "method": "POST", "raw_path": b"/api/sessions", "query_string": b"",
                 "headers": headers}
        receive = AsyncMock(side_effect=TimeoutError if timeout else [
            {"type": "http.request", "body": b"a" * 40000, "more_body": True},
            {"type": "http.request", "body": b"b" * 40000, "more_body": False},
        ])
        await LocalBoundary(downstream, origin=ORIGIN)(scope, receive, send)
        downstream.assert_not_called()
        assert send.call_args_list[0].args[0]["status"] == (408 if timeout else 413)
    asyncio.run(exercise())
    asyncio.run(exercise(timeout=True))


def test_cli_init_exclusively_writes_credentials_without_printing_tokens():
    import os

    from after_sales.cli import app

    output = mock_open()
    with patch("after_sales.cli.os.open", return_value=7) as opened, \
         patch("after_sales.cli.os.fdopen", output), \
         patch("after_sales.cli.os.fchmod") as chmod, \
         patch("after_sales.cli.os.fsync"), \
         patch("after_sales.cli.Store.initialize", return_value=(Mock(), {"alice": "never-print-this-token"})):
        result = CliRunner().invoke(app, ["init", "--data-dir", "/tmp/new-demo", "--credentials", "/tmp/new-creds",
                                         "--now", "1800000000"])
    assert result.exit_code == 0, result.output
    flags = opened.call_args.args[1]
    assert flags & os.O_EXCL and flags & os.O_CREAT
    assert opened.call_args.args[2] == 0o600
    chmod.assert_called_once_with(7, 0o600)
    written = "".join(call.args[0] for call in output().write.call_args_list)
    assert "never-print-this-token" in written and "never-print-this-token" not in result.output


def test_cli_serve_modes_disable_proxy_and_access_logging_without_real_server():
    from after_sales.cli import app

    with patch("after_sales.cli.Store"), patch("after_sales.cli.create_app"), \
         patch("after_sales.cli.LocalHTTPProvider") as provider, patch("after_sales.cli.uvicorn.run") as run:
        result = CliRunner().invoke(app, ["serve", "--data-dir", "/tmp/local-demo", "--model-url",
                                         "http://127.0.0.1:1234", "--model", "local-model"])
    assert result.exit_code == 0, result.output
    provider.assert_called_once_with("http://127.0.0.1:1234", "local-model")
    assert run.call_args.kwargs["access_log"] is False
    assert run.call_args.kwargs["proxy_headers"] is False
    assert run.call_args.kwargs["forwarded_allow_ips"] == ""


def test_unexpected_failure_is_sanitized_and_keeps_security_headers(api):
    client, _, service, _, _ = api
    service.actions.view.side_effect = RuntimeError("private-internal-detail")
    response = client.get("/api/sessions/sess-alice", headers=AUTH)
    assert response.status_code == 500
    assert "private-internal-detail" not in response.text
    assert response.json()["error"]["code"] == "internal_error"
    assert response.headers["Cache-Control"] == "no-store"


def test_cli_existing_credentials_are_never_overwritten():
    from after_sales.cli import app

    with patch("after_sales.cli.os.open", side_effect=FileExistsError), \
         patch("after_sales.cli.Store.initialize") as initialize, \
         patch("after_sales.cli.Path.unlink") as unlink:
        result = CliRunner().invoke(app, ["init", "--data-dir", "/tmp/new-demo", "--credentials", "/tmp/existing"])
    assert result.exit_code == 1
    initialize.assert_not_called()
    unlink.assert_not_called()


def test_console_entry_point_calls_the_cli_application():
    from importlib.metadata import distribution

    entry = next(point for point in distribution("ecommerce-after-sales-agent").entry_points
                 if point.group == "console_scripts" and point.name == "after-sales")
    with patch("after_sales.cli.app") as app:
        entry.load()()
    app.assert_called_once_with()


def test_cli_requires_explicit_mode_and_loopback_without_starting_server():
    from after_sales.cli import app

    runner = CliRunner()
    cases = [[], ["--test-mode", "--model-url", "http://127.0.0.1:1234", "--model", "local"],
             ["--model-url", "http://127.0.0.1:1234"], ["--test-mode", "--host", "0.0.0.0"]]
    with patch("after_sales.cli.uvicorn.run") as run:
        for args in cases:
            result = runner.invoke(app, ["serve", "--data-dir", "/nonexistent-demo", *args])
            assert result.exit_code != 0
        run.assert_not_called()
