"""Tests for backend/carbon_client.py and the Carbon integration wired
into backend/app.py.

Split the same way backend/test_agent_chat.py splits backend/llm.py:
webhook-secret verification, payload mapping, and the on/off-by-
configuration logic are deterministic and fully covered here;
report_verdict()'s own HTTP construction is checked against a faked
urlopen (no real network call), the same pattern test_agent_chat.py
uses for Groq.
"""
from __future__ import annotations

import json
import time

import pytest

from . import carbon_client
from .app import create_app
from .digital_twin import DigitalTwin
from .models import CONFIG
from .simulator import Simulator


# --------------------------------------------------------------------------- #
# Configuration / credential discovery
# --------------------------------------------------------------------------- #
def test_disabled_by_default(monkeypatch):
    monkeypatch.delitem(CONFIG, "CARBON_ENABLED", raising=False)
    CONFIG["CARBON_ENABLED"] = False
    monkeypatch.delenv("CARBON_API_KEY", raising=False)
    monkeypatch.delenv("CARBON_BASE_URL", raising=False)
    monkeypatch.delenv("CARBON_WEBHOOK_SECRET", raising=False)
    assert carbon_client.enabled() is False
    assert carbon_client.webhook_configured() is False


def test_enabled_requires_both_the_config_flag_and_credentials(monkeypatch):
    monkeypatch.setenv("CARBON_API_KEY", "key")
    monkeypatch.setenv("CARBON_BASE_URL", "https://carbon.example.test")
    CONFIG["CARBON_ENABLED"] = False
    assert carbon_client.enabled() is False  # flag off -> off regardless of credentials
    CONFIG["CARBON_ENABLED"] = True
    assert carbon_client.enabled() is True
    CONFIG["CARBON_ENABLED"] = False  # restore


def test_find_base_url_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv("CARBON_BASE_URL", "https://carbon.example.test/")
    assert carbon_client.find_base_url() == "https://carbon.example.test"


# --------------------------------------------------------------------------- #
# Webhook secret verification — must fail CLOSED. A plain shared secret,
# not an HMAC signature: Carbon's outbound webhook is a no-code workflow
# action whose author types the URL/headers/body by hand (see
# carbon_client's module docstring) — there's no step in Carbon's
# workflow builder that could compute a signature over the body.
# --------------------------------------------------------------------------- #
def test_verify_webhook_secret_accepts_a_matching_value(monkeypatch):
    monkeypatch.setenv("CARBON_WEBHOOK_SECRET", "s3cret")
    assert carbon_client.verify_webhook_secret("s3cret") is True


def test_verify_webhook_secret_rejects_a_wrong_value(monkeypatch):
    monkeypatch.setenv("CARBON_WEBHOOK_SECRET", "s3cret")
    assert carbon_client.verify_webhook_secret("nope") is False


def test_verify_webhook_secret_fails_closed_with_none_configured(monkeypatch):
    monkeypatch.delenv("CARBON_WEBHOOK_SECRET", raising=False)
    assert carbon_client.verify_webhook_secret("anything") is False


def test_verify_webhook_secret_fails_closed_with_no_header(monkeypatch):
    monkeypatch.setenv("CARBON_WEBHOOK_SECRET", "s3cret")
    assert carbon_client.verify_webhook_secret(None) is False


# --------------------------------------------------------------------------- #
# Job operation -> task payload mapping (Carbon's real jobOperation
# fields — operationType, jobOperationId, jobId — see JOB_TYPE_MAP)
# --------------------------------------------------------------------------- #
def test_map_job_to_task_payload_translates_a_known_operation_type():
    job = {
        "operationType": "process",
        "jobOperationId": "jo_42",
        "jobId": "job_7",
        "itemId": "Box-A",
        "sourceLocation": "loading_zone",
        "workCenterId": "shelf_a",
        "priority": "HIGH",
    }
    payload = carbon_client.map_job_to_task_payload(job)
    assert payload["type"] == "PICK_AND_DELIVER"
    assert payload["box_id"] == "Box-A"
    assert payload["source"] == "loading_zone"
    assert payload["destination"] == "shelf_a"
    assert payload["priority"] == "HIGH"
    assert payload["external_ref"] == {
        "source": "carbon", "job_operation_id": "jo_42", "job_id": "job_7", "operation_type": "PROCESS",
    }


def test_map_job_to_task_payload_rejects_an_unmapped_operation_type():
    with pytest.raises(ValueError, match="Unmapped Carbon operationType"):
        carbon_client.map_job_to_task_payload({"operationType": "Teleport", "jobOperationId": "jo_1"})


def test_map_job_to_task_payload_requires_a_job_operation_id():
    with pytest.raises(ValueError, match="jobOperationId"):
        carbon_client.map_job_to_task_payload({"operationType": "Inspection"})


# --------------------------------------------------------------------------- #
# report_verdict()'s HTTP construction, against a faked urlopen — no real
# network call, same pattern backend/test_agent_chat.py uses for Groq.
# --------------------------------------------------------------------------- #
class _FakeOkResponse:
    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False


def test_report_verdict_noops_when_not_enabled(monkeypatch):
    monkeypatch.delenv("CARBON_API_KEY", raising=False)
    CONFIG["CARBON_ENABLED"] = False
    assert carbon_client.report_verdict({"job_operation_id": "jo_1"}, {"verdict": "PASS"}) is False


def test_report_verdict_marks_the_job_operation_done_on_pass(monkeypatch):
    monkeypatch.setenv("CARBON_API_KEY", "key-123")
    monkeypatch.setenv("CARBON_BASE_URL", "https://carbon.example.test")
    CONFIG["CARBON_ENABLED"] = True

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["headers"] = dict(request.header_items())
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeOkResponse()

    monkeypatch.setattr(carbon_client.urllib.request, "urlopen", fake_urlopen)

    ok = carbon_client.report_verdict(
        {"job_operation_id": "jo_42", "job_id": "job_7", "operation_type": "PROCESS"},
        {"task_id": "task_001", "verdict": "PASS", "reasons": [], "checks": []},
    )
    CONFIG["CARBON_ENABLED"] = False  # restore

    assert ok is True
    assert captured["url"] == "https://carbon.example.test/api/v1/production/updateJobOperationStatus"
    assert captured["headers"]["Authorization"] == "Bearer key-123"
    assert captured["body"] == {"id": "jo_42", "status": "Done"}


def test_report_verdict_files_an_issue_on_fail(monkeypatch):
    monkeypatch.setenv("CARBON_API_KEY", "key-123")
    monkeypatch.setenv("CARBON_BASE_URL", "https://carbon.example.test")
    monkeypatch.setenv("CARBON_DEFAULT_LOCATION_ID", "loc_1")
    monkeypatch.setenv("CARBON_DEFAULT_NC_TYPE_ID", "nctype_1")
    CONFIG["CARBON_ENABLED"] = True

    captured = {}

    def fake_urlopen(request, timeout=None):
        captured["url"] = request.full_url
        captured["body"] = json.loads(request.data.decode("utf-8"))
        return _FakeOkResponse()

    monkeypatch.setattr(carbon_client.urllib.request, "urlopen", fake_urlopen)

    ok = carbon_client.report_verdict(
        {"job_operation_id": "jo_42"},
        {"task_id": "task_001", "verdict": "FAIL", "reasons": ["collision detected"], "checks": []},
    )
    CONFIG["CARBON_ENABLED"] = False  # restore

    assert ok is True
    assert captured["url"] == "https://carbon.example.test/api/v1/quality/insertIssue"
    assert captured["body"]["jobOperationId"] == "jo_42"
    assert captured["body"]["locationId"] == "loc_1"
    assert captured["body"]["nonConformanceTypeId"] == "nctype_1"
    assert "collision detected" in captured["body"]["description"]


def test_report_verdict_skips_the_issue_without_location_and_nc_type_ids(monkeypatch):
    monkeypatch.setenv("CARBON_API_KEY", "key-123")
    monkeypatch.setenv("CARBON_BASE_URL", "https://carbon.example.test")
    monkeypatch.delenv("CARBON_DEFAULT_LOCATION_ID", raising=False)
    monkeypatch.delenv("CARBON_DEFAULT_NC_TYPE_ID", raising=False)
    CONFIG["CARBON_ENABLED"] = True

    def fake_urlopen(request, timeout=None):
        raise AssertionError("should never be called without location/nc-type ids")

    monkeypatch.setattr(carbon_client.urllib.request, "urlopen", fake_urlopen)
    ok = carbon_client.report_verdict({"job_operation_id": "jo_1"}, {"task_id": "t", "verdict": "FAIL", "reasons": []})
    CONFIG["CARBON_ENABLED"] = False  # restore
    assert ok is False


def test_report_verdict_returns_false_on_network_failure(monkeypatch):
    monkeypatch.setenv("CARBON_API_KEY", "key-123")
    monkeypatch.setenv("CARBON_BASE_URL", "https://carbon.example.test")
    CONFIG["CARBON_ENABLED"] = True

    def fake_urlopen(request, timeout=None):
        raise OSError("boom")

    monkeypatch.setattr(carbon_client.urllib.request, "urlopen", fake_urlopen)
    ok = carbon_client.report_verdict({"job_operation_id": "jo_1"}, {"verdict": "PASS"})
    CONFIG["CARBON_ENABLED"] = False  # restore
    assert ok is False


# --------------------------------------------------------------------------- #
# HTTP surface: POST /api/integrations/carbon/webhook
# --------------------------------------------------------------------------- #
@pytest.fixture
def carbon_client_app(tmp_path, monkeypatch):
    monkeypatch.setenv("CARBON_WEBHOOK_SECRET", "s3cret")
    monkeypatch.delenv("CARBON_API_KEY", raising=False)
    monkeypatch.delenv("CARBON_BASE_URL", raising=False)
    CONFIG["CARBON_ENABLED"] = True
    twin = DigitalTwin(
        log_dir=str(tmp_path / "logs"), data_dir=str(tmp_path / "data"),
        persist_logs=True, demo=True, demo_tasks=False,
    )
    app, twin, simulator, _ci = create_app(twin=twin, autostart=False, run_thread=False)
    app.config["TESTING"] = True
    yield app.test_client(), twin, simulator
    CONFIG["CARBON_ENABLED"] = False


def _signed_post(client, body: dict, secret: str = "s3cret"):
    return client.post(
        "/api/integrations/carbon/webhook",
        json=body,
        headers={"X-Carbon-Webhook-Secret": secret},
    )


def test_webhook_disabled_returns_503(tmp_path, monkeypatch):
    CONFIG["CARBON_ENABLED"] = False
    twin = DigitalTwin(
        log_dir=str(tmp_path / "logs"), data_dir=str(tmp_path / "data"),
        persist_logs=False, demo=True, demo_tasks=False,
    )
    app, _twin, _sim, _ci = create_app(twin=twin, autostart=False, run_thread=False)
    app.config["TESTING"] = True
    response = app.test_client().post("/api/integrations/carbon/webhook", json={"jobOperationId": "jo_1"})
    assert response.status_code == 503


def test_webhook_rejects_a_wrong_secret(carbon_client_app):
    client, _twin, _sim = carbon_client_app
    response = _signed_post(
        client,
        {"operationType": "Process", "jobOperationId": "jo_1", "itemId": "Box-A"},
        secret="wrong",
    )
    assert response.status_code == 401


def test_webhook_rejects_an_unmapped_operation_type(carbon_client_app):
    client, _twin, _sim = carbon_client_app
    response = _signed_post(client, {"operationType": "Teleport", "jobOperationId": "jo_1"})
    # Same convention as POST /api/tasks with an unknown type — a bad
    # request, not a 422 (that's reserved for a well-formed task that
    # failed validation against a live robot/agent/operator).
    assert response.status_code == 400


def test_webhook_creates_a_task_tagged_with_external_ref(carbon_client_app):
    client, twin, _sim = carbon_client_app
    response = _signed_post(client, {
        "operationType": "Process", "jobOperationId": "jo_1", "jobId": "job_1",
        "itemId": "Box-A", "workCenterId": "loading_zone",
    })
    assert response.status_code == 201, response.get_json()
    body = response.get_json()
    assert body["task"]["external_ref"] == {
        "source": "carbon", "job_operation_id": "jo_1", "job_id": "job_1", "operation_type": "PROCESS",
    }
    task = twin.tasks.get(body["task"]["id"])
    assert task.external_ref["job_operation_id"] == "jo_1"


def test_carbon_originated_task_reports_its_verdict_back(carbon_client_app, monkeypatch):
    client, twin, sim = carbon_client_app
    calls = []
    monkeypatch.setattr(
        carbon_client, "report_verdict",
        lambda external_ref, report: calls.append((external_ref, report)) or True,
    )
    # enabled() also needs real-looking credentials for the subscriber to
    # bother calling report_verdict at all — see app.py's
    # _carbon_verdict_subscriber.
    monkeypatch.setenv("CARBON_API_KEY", "key")
    monkeypatch.setenv("CARBON_BASE_URL", "https://carbon.example.test")

    response = _signed_post(client, {
        "operationType": "Process", "jobOperationId": "jo_9",
        "itemId": "Box-A", "workCenterId": "loading_zone",
    })
    assert response.status_code == 201, response.get_json()
    task_id = response.get_json()["task"]["id"]

    for _ in range(500):
        task = twin.tasks.get(task_id)
        if task.is_terminal:
            break
        sim.tick()
    else:
        raise AssertionError("task never reached a terminal state")

    # The verdict is posted from a background thread (see app.py) — give
    # it a moment to run.
    for _ in range(50):
        if calls:
            break
        time.sleep(0.02)

    assert calls, "report_verdict was never called for a Carbon-originated task"
    external_ref, report = calls[0]
    assert external_ref["job_operation_id"] == "jo_9"
    assert report["task_id"] == task_id
    assert report["verdict"] in ("PASS", "WARN", "FAIL")
