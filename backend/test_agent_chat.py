"""Tests for backend/agent_tools.py and backend/agent_chat.py.

Only the parts that don't need a real network call: the tool executor
(runs directly against a live DigitalTwin, exactly like the chat loop
would call it) and run_chat's own guard rails (no API key configured, an
empty message). The tool-use loop's actual conversation with Groq is
exercised by hand against the real API, not in the deterministic suite —
same reasoning as backend/llm.py's narrate() staying untested for its
network path.
"""
from __future__ import annotations

import email.message
import io
import urllib.error

import pytest

from .agent_chat import ChatError, RATE_LIMIT_DEFAULT_WAIT_SECONDS, RATE_LIMIT_MAX_WAIT_SECONDS, _rate_limit_wait_seconds, run_chat
from .agent_tools import TOOLS, TOOL_NAMES, execute_tool
from .digital_twin import DigitalTwin


def _http_error(headers=None, body=b"{}"):
    hdrs = email.message.Message()
    for key, value in (headers or {}).items():
        hdrs.add_header(key, value)
    return urllib.error.HTTPError("https://api.groq.com/x", 429, "Too Many Requests", hdrs, io.BytesIO(body))


class _FakeResponse:
    """A minimal stand-in for urllib.request.urlopen's context-manager
    response, carrying one Groq chat-completions-shaped payload."""

    def __init__(self, message):
        import json as _json
        self._payload = _json.dumps({"choices": [{"message": message}]}).encode("utf-8")

    def read(self):
        return self._payload

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def _tool_call_message(name="get_fleet_load", args=None, call_id="call_1"):
    import json as _json
    return {
        "role": "assistant", "content": None,
        "tool_calls": [{"id": call_id, "type": "function",
                         "function": {"name": name, "arguments": _json.dumps(args or {})}}],
    }


@pytest.fixture
def twin(tmp_path) -> DigitalTwin:
    # persist_logs=True (unlike the main suite's twin fixture) — this
    # module needs a real logs/tasks/<id>.json on disk to exercise
    # get_task's eval-inclusion path; tmp_path keeps it isolated.
    return DigitalTwin(
        log_dir=str(tmp_path / "logs"), data_dir=str(tmp_path / "data"),
        persist_logs=True, demo=True, demo_tasks=False,
    )


def test_every_tool_has_a_matching_handler():
    # TOOLS (the schema sent to Groq) and _HANDLERS (the executor) must
    # never drift apart — a tool the model can see but execute_tool can't
    # run would look like a working tool and then silently fail.
    schema_names = {t["function"]["name"] for t in TOOLS}
    assert schema_names == TOOL_NAMES


def test_execute_tool_rejects_an_unknown_tool(twin):
    result = execute_tool(twin, "not_a_real_tool", {})
    assert "error" in result


def test_create_task_tool_creates_a_real_task(twin):
    result = execute_tool(twin, "create_task", {
        "type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box_id": "Box-A",
        "destination": "loading_zone",
    })
    assert "error" not in result
    assert result["task_id"] in twin.tasks.tasks
    assert twin.tasks.tasks[result["task_id"]].type.value == "PICK_AND_DELIVER"


def test_create_task_tool_surfaces_a_validation_failure(twin):
    result = execute_tool(twin, "create_task", {"type": "MOVE_ROBOT", "robot_id": "Ghost", "destination": "loading_zone"})
    assert result["status"] == "FAILED"
    assert "does not exist" in result["error"]


def test_create_task_tool_rejects_an_unknown_task_type(twin):
    result = execute_tool(twin, "create_task", {"type": "TELEPORT_ROBOT"})
    assert "error" in result


def test_cancel_task_tool(twin):
    created = execute_tool(twin, "create_task", {
        "type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box_id": "Box-A",
        "destination": "loading_zone",
    })
    result = execute_tool(twin, "cancel_task", {"task_id": created["task_id"]})
    assert result["status"] == "CANCELLED"


def test_get_task_tool_includes_eval_once_a_log_exists(twin):
    created = execute_tool(twin, "create_task", {"type": "AGENT_AUDIT", "agent_id": "Ada"})
    result = execute_tool(twin, "get_task", {"task_id": created["task_id"]})
    assert result["status"] == "COMPLETED"
    assert result["eval"]["verdict"] in ("PASS", "WARN", "FAIL")


def test_get_task_tool_unknown_id(twin):
    result = execute_tool(twin, "get_task", {"task_id": "task_999"})
    assert "error" in result


@pytest.mark.parametrize("kind,key", [
    ("robots", "robots"), ("boxes", "boxes"), ("agents", "agents"), ("operators", "operators"),
])
def test_list_entities_tool(twin, kind, key):
    result = execute_tool(twin, "list_entities", {"kind": kind})
    assert key in result
    assert len(result[key]) > 0


def test_list_entities_tool_rejects_an_unknown_kind(twin):
    result = execute_tool(twin, "list_entities", {"kind": "spaceships"})
    assert "error" in result


def test_get_fleet_load_tool(twin):
    result = execute_tool(twin, "get_fleet_load", {})
    names = {row["robot"] for row in result["fleet_load"]}
    assert {"Robo-01", "Robo-02"} <= names


def test_get_statistics_tool(twin):
    result = execute_tool(twin, "get_statistics", {})
    assert result["robots"] == 2


def test_query_decisions_tool_reads_the_projects_own_examples(twin):
    result = execute_tool(twin, "query_decisions", {"verdict": "FAIL"})
    # twin.logger.tasks_dir is an empty tmp_path fixture dir — nothing to
    # find there, which is itself the honest behaviour to check: an empty
    # (not erroring) result.
    assert result["matches"] == []
    assert result["total_matches"] == 0


# --------------------------------------------------------------------- #
# run_chat's own guard rails — no network involved
# --------------------------------------------------------------------- #
def test_run_chat_requires_an_api_key(twin, monkeypatch):
    monkeypatch.setattr("backend.agent_chat.find_api_key", lambda: None)
    with pytest.raises(ChatError, match="API key"):
        run_chat(twin, "do something")


def test_run_chat_rejects_an_empty_message(twin, monkeypatch):
    monkeypatch.setattr("backend.agent_chat.find_api_key", lambda: "fake-key")
    with pytest.raises(ChatError, match="Say something"):
        run_chat(twin, "   ")


# --------------------------------------------------------------------- #
# Rate-limit backoff parsing — real 429 payloads Groq has actually sent
# --------------------------------------------------------------------- #
def test_rate_limit_wait_prefers_the_retry_after_header():
    exc = _http_error(headers={"Retry-After": "3"})
    assert _rate_limit_wait_seconds(exc, "irrelevant body") == 3.0


def test_rate_limit_wait_falls_back_to_the_message_text():
    exc = _http_error()
    detail = (
        '{"error":{"message":"Rate limit reached ... Please try again in 6.4125s. '
        'Need more tokens? Upgrade to Dev Tier..."}}'
    )
    assert _rate_limit_wait_seconds(exc, detail) == pytest.approx(6.4125)


def test_rate_limit_wait_is_capped():
    exc = _http_error(headers={"Retry-After": "999"})
    assert _rate_limit_wait_seconds(exc, "") == RATE_LIMIT_MAX_WAIT_SECONDS


def test_rate_limit_wait_default_when_unparseable():
    exc = _http_error()
    assert _rate_limit_wait_seconds(exc, "no timing information here") == RATE_LIMIT_DEFAULT_WAIT_SECONDS


def test_run_chat_retries_past_a_rate_limit_then_succeeds(twin, monkeypatch):
    """The full retry path through run_chat -> _post, with urlopen mocked
    so no real network call happens — the first attempt 429s, the second
    succeeds, and the sleep in between is stubbed out so the test stays
    instant."""
    import backend.agent_chat as agent_chat_module

    monkeypatch.setattr(agent_chat_module, "find_api_key", lambda: "fake-key")
    monkeypatch.setattr(agent_chat_module.time, "sleep", lambda seconds: None)

    calls = {"n": 0}

    def fake_urlopen(request, timeout=None):
        calls["n"] += 1
        if calls["n"] == 1:
            raise _http_error(headers={"Retry-After": "0"})
        return _FakeResponse({"role": "assistant", "content": "All good."})

    monkeypatch.setattr(agent_chat_module.urllib.request, "urlopen", fake_urlopen)

    result = run_chat(twin, "ping")
    assert result["reply"] == "All good."
    assert calls["n"] == 2  # one 429, one success — proves the retry actually happened


# --------------------------------------------------------------------- #
# Fallback summaries — a many-step command (like a dozen chained
# create_task requests in one message) can end with the model's own
# final message carrying no text, or never stopping at all before the
# step cap. Neither should ever surface as "(no reply)" or a hard error
# when real actions were actually taken.
# --------------------------------------------------------------------- #
def test_fallback_summary_lists_created_tasks_and_errors():
    from backend.agent_chat import _fallback_summary

    actions = [
        {"tool": "create_task", "result": {"task_id": "task_003", "type": "MOVE_ROBOT", "status": "PLANNING"}},
        {"tool": "create_task", "result": {"error": "Robot 'Ghost' does not exist"}},
        {"tool": "get_fleet_load", "result": {"fleet_load": []}},
    ]
    summary = _fallback_summary(actions)
    assert "task_003" in summary and "PLANNING" in summary
    assert "✗" in summary and "does not exist" in summary
    assert "get_fleet_load" in summary


def test_fallback_summary_with_no_actions():
    from backend.agent_chat import _fallback_summary
    assert "didn't do anything" in _fallback_summary([])


def test_run_chat_falls_back_when_the_final_message_has_no_text(twin, monkeypatch):
    """One tool call, then a final message with tool_calls=None AND empty
    content — the exact shape that produced "(no reply)" for a real
    many-step command."""
    import backend.agent_chat as agent_chat_module

    monkeypatch.setattr(agent_chat_module, "find_api_key", lambda: "fake-key")
    responses = [
        _tool_call_message("get_fleet_load", {}),
        {"role": "assistant", "content": ""},
    ]

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(responses.pop(0))

    monkeypatch.setattr(agent_chat_module.urllib.request, "urlopen", fake_urlopen)

    result = run_chat(twin, "how busy is the fleet?")
    assert result["reply"] != ""
    assert "(no reply)" not in result["reply"]
    assert "get_fleet_load" in result["reply"]
    assert len(result["actions"]) == 1


def test_run_chat_degrades_gracefully_past_the_step_cap(twin, monkeypatch):
    """The model never stops calling tools within MAX_TOOL_ITERATIONS —
    run_chat must still return a real reply, not raise, since whatever
    it already did is real."""
    import backend.agent_chat as agent_chat_module

    monkeypatch.setattr(agent_chat_module, "find_api_key", lambda: "fake-key")
    monkeypatch.setattr(agent_chat_module, "MAX_TOOL_ITERATIONS", 3)

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(_tool_call_message("get_fleet_load", {}, call_id="c"))

    monkeypatch.setattr(agent_chat_module.urllib.request, "urlopen", fake_urlopen)

    result = run_chat(twin, "keep going forever")
    assert result["iterations"] == 3
    assert len(result["actions"]) == 3
    assert "stopped after 3 steps" in result["reply"]
    assert "get_fleet_load" in result["reply"]


# --------------------------------------------------------------------- #
# on_progress — live per-step narration (the chat panel's SSE feed)
# --------------------------------------------------------------------- #
def test_on_progress_fires_for_each_thinking_step_and_action(twin, monkeypatch):
    import backend.agent_chat as agent_chat_module

    monkeypatch.setattr(agent_chat_module, "find_api_key", lambda: "fake-key")
    responses = [
        _tool_call_message("get_fleet_load", {}),
        {"role": "assistant", "content": "Done."},
    ]

    def fake_urlopen(request, timeout=None):
        return _FakeResponse(responses.pop(0))

    monkeypatch.setattr(agent_chat_module.urllib.request, "urlopen", fake_urlopen)

    events = []
    result = run_chat(twin, "how busy is the fleet?", on_progress=events.append)

    assert result["reply"] == "Done."
    phases = [e["phase"] for e in events]
    assert phases == ["thinking", "action", "thinking"]
    assert events[1]["tool"] == "get_fleet_load"
    assert "fleet_load" in events[1]["result"]


def test_a_broken_on_progress_callback_never_breaks_the_chat_turn(twin, monkeypatch):
    import backend.agent_chat as agent_chat_module

    monkeypatch.setattr(agent_chat_module, "find_api_key", lambda: "fake-key")

    def fake_urlopen(request, timeout=None):
        return _FakeResponse({"role": "assistant", "content": "Fine anyway."})

    monkeypatch.setattr(agent_chat_module.urllib.request, "urlopen", fake_urlopen)

    def broken_callback(event):
        raise RuntimeError("boom")

    result = run_chat(twin, "hello", on_progress=broken_callback)
    assert result["reply"] == "Fine anyway."
