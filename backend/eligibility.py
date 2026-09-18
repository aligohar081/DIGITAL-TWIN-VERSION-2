"""Shared eligibility rules for the three actor classes — robot, AI agent,
human operator — against their toy "assurance passport" baselines (see
TRUST_LAYER.md).

This is the single place the rule lives. Two very different callers need
the exact same rule and must never be allowed to drift apart:

    backend/task_manager.py::validate()          — the PRE-execution gate.
                                                     Blocks task creation
                                                     outright (HTTP 422)
                                                     before a robot/agent/
                                                     operator ever touches
                                                     the work.
    backend/eval_engine.py::check_entities_valid  — the POST-execution
                                                     grade. Reads the
                                                     "before" state
                                                     snapshot already
                                                     attached to a
                                                     finished task's log
                                                     and grades it FAIL/
                                                     WARN/PASS.

Each function takes plain primitives (not the live Robot/Agent/Operator
objects, and not the JSON snapshot dicts) so both callers can feed it
whatever shape they already have on hand. A return value of ``None`` means
eligible; any other value is a plain-language reason it isn't.
"""
from __future__ import annotations

from typing import Optional, Sequence

try:  # pragma: no cover - defensive fallback only, mirrors eval_engine.py
    from .models import APPROVED_AGENT_MODELS, APPROVED_FIRMWARE_VERSIONS, CONFIG
except Exception:  # pragma: no cover
    APPROVED_FIRMWARE_VERSIONS = ["2.1.0", "2.1.1", "2.2.0"]
    APPROVED_AGENT_MODELS = ["groq/gpt-oss-20b", "groq/gpt-oss-120b"]
    CONFIG = {"BATTERY_CRITICAL": 8}


def robot_eligibility(
    status: str,
    firmware_version: Optional[str],
    battery: Optional[float],
    battery_critical: Optional[float] = None,
    task_type: Optional[str] = None,
    allowed_task_types: Optional[Sequence[str]] = None,
) -> Optional[str]:
    """None if the robot may be trusted with a task; else why not.

    Covers ERROR/STOPPED status, unapproved firmware, an out-of-config
    task type, and (only if a ``battery`` value is actually passed) a
    critically-low battery.

    task_manager.py's pre-execution gate deliberately calls this with
    ``battery=None`` — a critical battery already has a real recovery
    path (TaskPlanner prepends a recharge detour), so it must not be
    treated as a hard "never trust this robot" disqualifier the way bad
    firmware or an ERROR status are. eval_engine.check_entities_valid,
    grading a task after it's finished, does pass the real battery value:
    a robot that was still critical once the task actually *started*
    (i.e. the detour didn't happen, or wasn't enough) is worth flagging
    even though it wasn't worth refusing up front.

    ``allowed_task_types`` is a robot's own configured capability list
    (``Robot.allowed_task_types`` — see backend/robot.py). ``None`` or an
    empty sequence means "unrestricted" (the default for every robot
    unless explicitly configured), matching how every robot behaved
    before this existed — so old callers that never pass ``task_type``/
    ``allowed_task_types`` at all keep working unchanged.
    """
    if status in ("ERROR", "STOPPED"):
        return f"is in {status} status"
    if firmware_version and firmware_version not in APPROVED_FIRMWARE_VERSIONS:
        return (
            f"is running unapproved firmware {firmware_version!r} "
            f"(approved: {APPROVED_FIRMWARE_VERSIONS})"
        )
    if task_type and allowed_task_types:
        allowed = list(allowed_task_types)
        if task_type not in allowed:
            return f"is not configured to run {task_type} tasks (allowed: {allowed})"
    threshold = CONFIG.get("BATTERY_CRITICAL", 8) if battery_critical is None else battery_critical
    if isinstance(battery, (int, float)) and battery <= threshold:
        return f"has a critical battery ({battery}%)"
    return None


def agent_eligibility(status: str, model_version: Optional[str]) -> Optional[str]:
    """None if the AI agent may be trusted with a task; else why not."""
    if status == "ERROR":
        return "is in ERROR status"
    if model_version and model_version not in APPROVED_AGENT_MODELS:
        return (
            f"is running an unapproved model {model_version!r} "
            f"(approved: {APPROVED_AGENT_MODELS})"
        )
    return None


def operator_eligibility(
    status: str,
    certifications: Optional[Sequence[str]],
    required_certification: Optional[str],
) -> Optional[str]:
    """None if the human operator may be trusted with a task; else why not."""
    if status == "OFF_DUTY":
        return "is off duty"
    certs = certifications or []
    if required_certification and required_certification not in certs:
        return (
            f"does not hold the required {required_certification!r} "
            f"certification (has: {list(certs)})"
        )
    return None
