"""Optional integration with Carbon (carbon.ms) — an open-source
manufacturing ERP/MES/QMS (https://github.com/crbnos/carbon) — used here
as an external task source and quality system-of-record for this digital
twin.

Off by default (CONFIG["CARBON_ENABLED"], see policies.example.yaml).
Two independent directions, each with its own graceful "off" state:

  inbound   A Carbon workflow (its no-code "call a webhook" action) posts
            to POST /api/integrations/carbon/webhook when a job
            operation is ready for material handling; map_job_to_task_
            payload() below turns that into a normal TaskManager.
            create_task() payload, so it goes through the exact same
            eligibility gate (backend/eligibility.py) as a task created
            by a person or the agent chat. Requires CARBON_WEBHOOK_
            SECRET — with no secret configured this fails CLOSED (every
            inbound request is refused), never open. See
            verify_webhook_secret()'s docstring for why this is a plain
            shared-secret header, not an HMAC signature.

  outbound  once a Carbon-originated task reaches a terminal state, its
            eval_engine verdict is posted back to Carbon — see
            report_verdict(). Requires CARBON_API_KEY + CARBON_BASE_URL.
            On any failure (feature off, no credentials, network error,
            timeout, bad response) this quietly returns False — a
            verdict callback is a report, never a gate, the same
            "narration/side-effect only, never load-bearing" rule
            backend/llm.py follows for AGENT_REPLAN/AGENT_AUDIT. It
            never changes the task's own already-terminal outcome.

Grounded in Carbon's real, public source (not just its marketing page)
as of this writing:

  - Every Carbon API v1 operation is `POST {base}/api/v1/{module}/
    {operation}` — always POST, oRPC-style, not classic REST verbs (see
    crbnos/carbon apps/erp/app/routes/api+/v1+/$.ts and
    lib/router.server.ts). Auth is `Authorization: Bearer crbn_...`
    (lib/authenticate.server.ts) — a plain API key, not OAuth.
  - The job-operation entity is `jobOperation` (module "production"),
    with a real `operationType` enum: "Process", "Assembly",
    "Inspection", "Outside Processing" (packages/database/src/
    types.ts) — coarse manufacturing-step categories, not a warehouse
    logistics vocabulary. This digital twin's robots don't perform the
    operation itself (a person or a machine does, inside Carbon/MES);
    they handle the material logistics around it — staging material at
    the work center before it starts, or moving output afterward. See
    JOB_TYPE_MAP's mapping and comment.
  - What Carbon calls quality's NCR/CAPA workflow in its own marketing
    is internally the "Issue" domain (`quality.insertIssue`, table
    `nonConformance` — see apps/erp/app/modules/quality/
    quality.service.ts). report_verdict() files a real Issue on a FAIL
    verdict, linked via `jobOperationId`.
  - Carbon's outbound webhooks are a no-code workflow *action* the
    workflow's own author configures (packages/jobs/src/workflows/
    actions/webhook.ts) — arbitrary URL, method, headers and a body
    template they write themselves. Carbon does not compute an HMAC
    signature over that body for them; the realistic authentication
    primitive is a literal shared-secret value typed into one of the
    workflow's own header rows, which is what verify_webhook_secret()
    checks.

Two things still need filling in against your own tenant before this is
production-real, because they're tenant-specific and not something a
public repo checkout can tell you: CARBON_DEFAULT_LOCATION_ID and
CARBON_DEFAULT_NC_TYPE_ID (a `nonConformanceType` id — quality.
insertIssue requires both on every Issue). Confirm the exact request
body shape for any operation against your own instance's live
`GET /api/v1/openapi.json` before relying on the field names below —
that endpoint is generated from the same manifest the real server
dispatches against, so it's authoritative in a way this file can't be.
"""
from __future__ import annotations

import hmac
import json
import os
import urllib.error
import urllib.request
from typing import Any, Dict, Optional
from urllib.parse import quote

from .models import CONFIG

TIMEOUT_SECONDS = 6
API_VERSION_PATH = "/api/v1"

_BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
# Gitignored, same pattern as evals/.env for GROQ_API_KEY (see
# backend/llm.py) — holds CARBON_API_KEY / CARBON_BASE_URL /
# CARBON_WEBHOOK_SECRET / CARBON_DEFAULT_LOCATION_ID /
# CARBON_DEFAULT_NC_TYPE_ID when you'd rather not export them as real
# env vars.
_ENV_FALLBACK_PATH = os.path.join(_BASE_DIR, ".env")

#: Carbon jobOperation.operationType (a real enum — see module docstring)
#: -> this project's TaskType for the material-handling task that should
#: happen around that operation. Matched case-insensitively. Extend or
#: override this for your own tenant's material-flow conventions — an
#: operationType that isn't in this map is a 400 (map_job_to_task_payload
#: raises ValueError), never a silent guess.
#:
#:   Process / Assembly     stage the operation's material at its work
#:                           center before it can start.
#:   Inspection              a human inspection task in this twin, not a
#:                           physical move.
#:   Outside Processing      the job is leaving the building — move its
#:                           material to the outbound/loading zone.
JOB_TYPE_MAP: Dict[str, str] = {
    "PROCESS": "PICK_AND_DELIVER",
    "ASSEMBLY": "PICK_AND_DELIVER",
    "INSPECTION": "HUMAN_INSPECTION",
    "OUTSIDE PROCESSING": "DELIVER_BOX",
}


def _read_env_fallback(name: str) -> Optional[str]:
    try:
        with open(_ENV_FALLBACK_PATH, "r", encoding="utf-8") as fh:
            for line in fh:
                line = line.strip()
                if line.startswith(f"{name}="):
                    value = line.split("=", 1)[1].strip().strip('"').strip("'")
                    return value or None
    except OSError:
        pass
    return None


def _find(name: str) -> Optional[str]:
    return os.environ.get(name) or _read_env_fallback(name)


def find_api_key() -> Optional[str]:
    return _find("CARBON_API_KEY")


def find_base_url() -> Optional[str]:
    url = _find("CARBON_BASE_URL")
    return url.rstrip("/") if url else None


def find_webhook_secret() -> Optional[str]:
    return _find("CARBON_WEBHOOK_SECRET")


def find_default_location_id() -> Optional[str]:
    """quality.insertIssue requires a locationId; there's no sensible
    computed default, so it comes from your own tenant's configuration."""
    return _find("CARBON_DEFAULT_LOCATION_ID")


def find_default_nc_type_id() -> Optional[str]:
    """quality.insertIssue requires a nonConformanceTypeId — again, a
    real id from your own tenant's Quality settings, not something this
    project can guess."""
    return _find("CARBON_DEFAULT_NC_TYPE_ID")


def enabled() -> bool:
    """Outbound verdict reporting is active."""
    return bool(CONFIG.get("CARBON_ENABLED")) and bool(find_api_key()) and bool(find_base_url())


def webhook_configured() -> bool:
    """Inbound task creation is active — deliberately independent of
    enabled() so a tenant can push tasks in without necessarily wanting
    verdicts posted back, or vice versa."""
    return bool(CONFIG.get("CARBON_ENABLED")) and bool(find_webhook_secret())


def verify_webhook_secret(header_value: Optional[str]) -> bool:
    """Constant-time comparison against CARBON_WEBHOOK_SECRET.

    Deliberately NOT an HMAC signature check: Carbon's outbound webhook
    is a no-code workflow *action* whose author types in the URL,
    headers and body template by hand (see the module docstring) — there
    is no step in Carbon's workflow builder that computes a signature
    over the body, so verifying one here would check a security property
    Carbon cannot actually produce. The realistic setup is the workflow
    author adding one literal header row (e.g. name
    `X-Carbon-Webhook-Secret`, value the same string configured here) to
    their webhook action — a shared secret, checked in constant time.
    Returns False (fail CLOSED) if not configured or the header is
    missing."""
    secret = find_webhook_secret()
    if not secret or not header_value:
        return False
    return hmac.compare_digest(secret, header_value.strip())


def map_job_to_task_payload(job: Dict[str, Any]) -> Dict[str, Any]:
    """Translate one Carbon job-operation payload (whatever fields the
    workflow author's body template includes — see the module docstring)
    into a TaskManager.create_task() payload, tagged with external_ref so
    a completed/failed task can be traced back to the Carbon job
    operation it came from (see app.py's Carbon verdict subscriber).

    Expects `operationType` (Carbon's real jobOperation column — see
    JOB_TYPE_MAP) and `jobOperationId` (Carbon's real primary key for
    this row). Raises ValueError on an unmapped type or a missing id —
    the webhook route turns that into a 400, same as any other malformed
    /api/tasks request."""
    operation_type = str(job.get("operationType") or job.get("operation_type") or "").strip().upper()
    task_type = JOB_TYPE_MAP.get(operation_type)
    if not task_type:
        raise ValueError(
            f"Unmapped Carbon operationType '{operation_type}' — add it to carbon_client.JOB_TYPE_MAP"
        )
    # Accept either our own hand-built payload shape (jobOperationId) or
    # Carbon's raw jobOperation table row passed straight through by a
    # workflow (id) — a workflow's "Call an outside URL" body only needs
    # to drop in the whole record, no manual JSON typing required.
    job_operation_id = job.get("jobOperationId") or job.get("job_operation_id") or job.get("id")
    if not job_operation_id:
        raise ValueError("Carbon job payload is missing 'jobOperationId'")

    # itemId isn't a column on jobOperation itself (it comes from the
    # job's method/BOM), and workCenterId is a real Carbon id, not one of
    # this twin's zone keys — neither is safe to trust blindly from a raw
    # jobOperation row, so fall back to a fixed demo box/zone rather than
    # forwarding an unrecognized value into TaskManager.create_task().
    box_id = job.get("itemId") or job.get("item_id") or job.get("box_id") or "Box-D"
    destination = job.get("destination") or job.get("sourceLocation")
    if not destination or str(job.get("workCenterId", "")).startswith("wc_"):
        destination = destination or "packing_area"

    return {
        "type": task_type,
        "box_id": box_id,
        "source": job.get("sourceLocation") or job.get("source"),
        "destination": destination,
        "priority": job.get("priority", "NORMAL"),
        "external_ref": {
            "source": "carbon",
            "job_operation_id": str(job_operation_id),
            "job_id": job.get("jobId") or job.get("job_id"),
            "operation_type": operation_type,
        },
    }


def _post(module: str, operation: str, body: Dict[str, Any]) -> bool:
    """POST {base}/api/v1/{module}/{operation} — the real Carbon API v1
    call shape (see module docstring). Never raises."""
    base_url = find_base_url()
    api_key = find_api_key()
    if not base_url or not api_key:
        return False
    url = f"{base_url}{API_VERSION_PATH}/{quote(module, safe='')}/{quote(operation, safe='')}"
    request = urllib.request.Request(
        url,
        data=json.dumps(body).encode("utf-8"),
        headers={
            "Content-Type": "application/json",
            "Authorization": f"Bearer {api_key}",
        },
        method="POST",
    )
    try:
        with urllib.request.urlopen(request, timeout=TIMEOUT_SECONDS):
            return True
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        return False


def report_verdict(external_ref: Dict[str, Any], report: Dict[str, Any]) -> bool:
    """Post the eval_engine verdict for a Carbon-originated task back to
    Carbon. Never raises — on any failure (feature off, no credentials,
    network error, bad response) this just returns False. Called off the
    hot path (a background thread — see app.py), so blocking on the
    network here never delays the simulation tick loop or the request
    that triggered it.

    PASS/WARN -> production.updateJobOperationStatus marks the job
    operation "Done" (jobOperationStatus has no separate warn state; the
    WARN detail still lives in this project's own eval_engine report).
    FAIL -> quality.insertIssue files a real Issue (Carbon's NCR/CAPA
    entry point), linked back to the job operation via jobOperationId —
    skipped, not sent malformed, if CARBON_DEFAULT_LOCATION_ID /
    CARBON_DEFAULT_NC_TYPE_ID aren't configured (see their finders'
    docstrings)."""
    if not enabled():
        return False
    job_operation_id = external_ref.get("job_operation_id")
    if not job_operation_id:
        return False

    verdict = report.get("verdict")
    if verdict == "FAIL":
        location_id = find_default_location_id()
        nc_type_id = find_default_nc_type_id()
        if not location_id or not nc_type_id:
            return False
        reasons = report.get("reasons") or []
        return _post("quality", "insertIssue", {
            "name": f"Digital twin task {report.get('task_id')} failed",
            "priority": "High",
            "source": "Internal",
            "locationId": location_id,
            "nonConformanceTypeId": nc_type_id,
            "jobOperationId": job_operation_id,
            "description": "; ".join(str(r) for r in reasons) or "See the linked task's eval report.",
        })

    return _post("production", "updateJobOperationStatus", {
        "id": job_operation_id,
        "status": "Done",
    })
