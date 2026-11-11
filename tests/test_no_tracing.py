import os

import pytest
from langsmith import Client

from after_sales.actions import Actions
from after_sales.agent import Runner
from after_sales.provider import ScriptedProvider
from after_sales.schemas import Command, DomainError, MessageCommand
from after_sales.storage import Store


def test_environment_cannot_enable_tracing(tmp_path, monkeypatch):
    for key in ("LANGSMITH_TRACING", "LANGCHAIN_TRACING_V2"):
        monkeypatch.setenv(key, "true")
    monkeypatch.setenv("LANGSMITH_API_KEY", "synthetic-not-a-real-key")
    monkeypatch.setenv("LANGSMITH_ENDPOINT", "http://127.0.0.1:1")
    sent = []
    monkeypatch.setattr(Client, "create_run", lambda *args, **kwargs: sent.append("create_run"))
    monkeypatch.setattr(Client, "update_run", lambda *args, **kwargs: sent.append("update_run"))
    store, tokens = Store.initialize(tmp_path / "data", now=1900000000)
    alice = store.authenticate(tokens["alice"])
    actions = Actions(store)
    sess = actions.create_session(alice, Command(action_id="new"))
    result = actions.message(alice, sess["id"], MessageCommand(
        action_id="msg", expected_version=sess["version"], text="你好"))
    provider = ScriptedProvider([{"tool": "respond", "arguments": {
        "kind": "empathy", "evidence_ids": [], "text": "你好"}}])
    Runner(store, provider).run(alice, result["run"]["id"])
    assert sent == []
    assert len(provider.seen_contexts) == 1
    assert os.environ["LANGSMITH_TRACING"] == "true"


@pytest.mark.parametrize("key", ["LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"])
@pytest.mark.parametrize("value", ["true", "1", "FALSE", "synthetic-setting"])
def test_legacy_tracing_fails_before_creating_runner_without_changing_environment(monkeypatch, key, value):
    monkeypatch.delenv("LANGCHAIN_TRACING", raising=False)
    monkeypatch.delenv("LANGCHAIN_HANDLER", raising=False)
    monkeypatch.setenv(key, value)
    with pytest.raises(DomainError) as error:
        Runner(None, ScriptedProvider([]))
    assert error.value.code == "tracing_config"
    assert value not in error.value.message
    assert os.environ[key] == value


@pytest.mark.parametrize("value", [None, "", "0", "false", "False"])
def test_disabled_legacy_switches_are_accepted(monkeypatch, value):
    for key in ("LANGCHAIN_TRACING", "LANGCHAIN_HANDLER"):
        if value is None:
            monkeypatch.delenv(key, raising=False)
        else:
            monkeypatch.setenv(key, value)
    Runner(None, ScriptedProvider([]))


def test_cli_reports_legacy_tracing_before_service_or_model_starts(tmp_path, monkeypatch):
    from typer.testing import CliRunner

    from after_sales import cli

    monkeypatch.setenv("LANGCHAIN_HANDLER", "synthetic-enabled")
    started = []
    monkeypatch.setattr(cli.uvicorn, "run", lambda *args, **kwargs: started.append(True))
    result = CliRunner().invoke(cli.app, ["serve", "--data-dir", str(tmp_path / "unused"), "--test-mode"])
    assert result.exit_code != 0
    assert "LANGCHAIN_TRACING" in result.output and "LANGCHAIN_HANDLER" in result.output
    assert "synthetic-enabled" not in result.output
    assert started == []
    assert not (tmp_path / "unused").exists()
