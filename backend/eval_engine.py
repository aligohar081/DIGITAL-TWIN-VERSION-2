"""Evaluation ("eval") engine for warehouse digital twin task logs.

This module grades the JSON event log produced for a single task — the
same array of structured records the system already writes to
``logs/tasks/<task_id>.json`` — against a fixed battery of checks, and
reports whether the task actually finished cleanly, and if not, why.

Nothing here re-runs the simulation. It only reads the log a task already
produced and reasons about it, the same way a human would scroll through
the "Live system logs" panel looking for trouble.

Three ways to use it:

    # 1. Programmatically, on an event list already in memory
    from backend.eval_engine import evaluate_events
    report = evaluate_events(events, task_id="task_001")

    # 2. On a JSON file on disk
    from backend.eval_engine import evaluate_file
    report = evaluate_file("logs/tasks/task_001.json")

    # 3. From the command line (see backend/run_evals.py)
    python -m backend.run_evals logs/tasks

    # 4. Over HTTP (see backend/app.py)
    GET /api/tasks/<task_id>/eval
    POST /api/evals/run
"""
from __future__ import annotations

import json
import os
from dataclasses import dataclass, field
from enum import Enum
from typing import Any, Callable, Dict, List, Optional, Sequence

try:  # pure-stdlib module, but keep eval_engine importable standalone too
    from .models import CONFIG as _MODEL_CONFIG
except Exception:  # pragma: no cover - defensive fallback only
    _MODEL_CONFIG = {}

try:
    from .models import APPROVED_FIRMWARE_VERSIONS
except Exception:  # pragma: no cover - defensive fallback only
    APPROVED_FIRMWARE_VERSIONS = ["2.1.0", "2.1.1", "2.2.0"]

try:
    from .models import APPROVED_AGENT_MODELS
except Exception:  # pragma: no cover - defensive fallback only
    APPROVED_AGENT_MODELS = ["groq/gpt-oss-20b", "groq/gpt-oss-120b"]

try:  # the exact same rule backend/task_manager.py's pre-execution gate
    # uses, so the gate and this after-the-fact grade can't quietly
    # disagree about what "eligible" means. See backend/eligibility.py.
    from .eligibility import agent_eligibility, operator_eligibility, robot_eligibility
except Exception:  # pragma: no cover - defensive fallback only

    def robot_eligibility(
        status, firmware_version, battery, battery_critical=None,
        task_type=None, allowed_task_types=None,
    ):
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
        threshold = 8 if battery_critical is None else battery_critical
        if isinstance(battery, (int, float)) and battery <= threshold:
            return f"has a critical battery ({battery}%)"
        return None

    def agent_eligibility(status, model_version):
        if status == "ERROR":
            return "is in ERROR status"
        if model_version and model_version not in APPROVED_AGENT_MODELS:
            return (
                f"is running an unapproved model {model_version!r} "
                f"(approved: {APPROVED_AGENT_MODELS})"
            )
        return None

    def operator_eligibility(status, certifications, required_certification):
        if status == "OFF_DUTY":
            return "is off duty"
        certs = certifications or []
        if required_certification and required_certification not in certs:
            return (
                f"does not hold the required {required_certification!r} "
                f"certification (has: {list(certs)})"
            )
        return None

# Fallback thresholds mirror backend/models.py CONFIG so this module still
# behaves sensibly if it's ever used outside the backend package.
DEFAULT_CONFIG: Dict[str, Any] = {
    "BATTERY_LOW": 20,
    "BATTERY_CRITICAL": 8,
    "REPLAN_AFTER_WAIT_TICKS": 3,
    "DEADLOCK_WAIT_TICKS": 20,
}
DEFAULT_CONFIG.update({k: v for k, v in _MODEL_CONFIG.items() if k in DEFAULT_CONFIG})


# --------------------------------------------------------------------------- #
# Result types
# --------------------------------------------------------------------------- #
class Verdict(str, Enum):
    PASS = "PASS"
    WARN = "WARN"
    FAIL = "FAIL"


_RANK = {Verdict.PASS: 0, Verdict.WARN: 1, Verdict.FAIL: 2}


@dataclass
class CheckResult:
    """The outcome of a single grading check."""

    name: str
    verdict: Verdict
    message: str
    evidence: List[int] = field(default_factory=list)  # seq numbers into the log

    def to_dict(self) -> Dict[str, Any]:
        return {
            "check": self.name,
            "verdict": self.verdict.value,
            "message": self.message,
            "evidence_seq": self.evidence,
        }


@dataclass
class EvalReport:
    """The overall grade for one task's log."""

    task_id: Optional[str]
    verdict: Verdict
    checks: List[CheckResult]
    reasons: List[str]
    summary: Dict[str, Any]

    @property
    def ok(self) -> bool:
        """True only for a clean PASS — no failures and nothing to flag."""
        return self.verdict == Verdict.PASS

    @property
    def passed(self) -> bool:
        """True unless the task actually FAILED (WARN still counts as passed)."""
        return self.verdict != Verdict.FAIL

    def to_dict(self) -> Dict[str, Any]:
        return {
            "task_id": self.task_id,
            "verdict": self.verdict.value,
            "ok": self.ok,
            "passed": self.passed,
            "reasons": self.reasons,
            "checks": [c.to_dict() for c in self.checks],
            "summary": self.summary,
        }


# --------------------------------------------------------------------------- #
# Small helpers for reading the event list
# --------------------------------------------------------------------------- #
def _by_event(events: Sequence[Dict[str, Any]], *event_types: str) -> List[Dict[str, Any]]:
    wanted = set(event_types)
    return [e for e in events if e.get("event") in wanted]


def _seqs(events: Sequence[Dict[str, Any]]) -> List[int]:
    return [e.get("seq") for e in events if e.get("seq") is not None]


def _contains(events: Sequence[Dict[str, Any]], needle: str) -> List[Dict[str, Any]]:
    needle = needle.lower()
    return [e for e in events if needle in (e.get("message") or "").lower()]


def _task_id_of(events: Sequence[Dict[str, Any]]) -> Optional[str]:
    for e in events:
        if e.get("task_id"):
            return e["task_id"]
    return None


def _robot_id_of(events: Sequence[Dict[str, Any]]) -> Optional[str]:
    for e in events:
        if e.get("robot_id"):
            return e["robot_id"]
    return None


# --------------------------------------------------------------------------- #
# Checks
#
# Each check takes the full, seq-sorted event list for one task plus the
# tuning CONFIG, and returns a single CheckResult. New checks just need to
# be added to DEFAULT_CHECKS below.
# --------------------------------------------------------------------------- #
def check_sequence_integrity(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    if not events:
        return CheckResult("sequence_integrity", Verdict.FAIL, "The log is empty — nothing to grade.")

    seqs = _seqs(events)
    if seqs != sorted(seqs):
        return CheckResult(
            "sequence_integrity",
            Verdict.FAIL,
            "Log records are not in increasing seq order — the file may be "
            "corrupted, truncated oddly, or merged from more than one run.",
            evidence=seqs[:8],
        )

    picked = _by_event(events, "BOX_PICKED")
    delivered = _by_event(events, "BOX_DELIVERED")
    if picked and delivered and picked[0]["seq"] > delivered[0]["seq"]:
        return CheckResult(
            "sequence_integrity",
            Verdict.FAIL,
            "A box shows as delivered before it was ever picked up — the "
            "event ordering is inconsistent with a real mission.",
            evidence=[picked[0]["seq"], delivered[0]["seq"]],
        )

    created = _by_event(events, "TASK_CREATED")
    if created and events[0].get("seq") is not None and created[0]["seq"] != events[0]["seq"]:
        return CheckResult(
            "sequence_integrity",
            Verdict.WARN,
            "The log does not start at TASK_CREATED — it may be a partial "
            "excerpt rather than the full task history.",
            evidence=[events[0]["seq"], created[0]["seq"]],
        )

    return CheckResult("sequence_integrity", Verdict.PASS, "Event ordering is consistent.")


def check_terminal_state(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    completed = _by_event(events, "TASK_COMPLETED")
    failed = _by_event(events, "TASK_FAILED")
    cancelled = _by_event(events, "TASK_CANCELLED")

    if failed:
        e = failed[-1]
        return CheckResult(
            "terminal_state", Verdict.FAIL,
            f"Task failed: {e.get('message')}",
            evidence=[e["seq"]],
        )
    if cancelled:
        e = cancelled[-1]
        return CheckResult(
            "terminal_state", Verdict.FAIL,
            f"Task was cancelled before it finished: {e.get('message')}",
            evidence=[e["seq"]],
        )
    if completed:
        e = completed[-1]
        return CheckResult(
            "terminal_state", Verdict.PASS,
            "Task reached TASK_COMPLETED.",
            evidence=[e["seq"]],
        )
    return CheckResult(
        "terminal_state", Verdict.FAIL,
        "The log never reaches a terminal state (no TASK_COMPLETED, "
        "TASK_FAILED or TASK_CANCELLED) — the task may still be in "
        "progress, stuck, or the log was cut off early.",
    )


def check_path_exists(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    missing = _by_event(events, "PATH_NOT_FOUND")
    if missing:
        e = missing[-1]
        return CheckResult(
            "path_exists", Verdict.FAIL,
            f"No route could be found to the target: {e.get('message')}",
            evidence=_seqs(missing),
        )
    return CheckResult("path_exists", Verdict.PASS, "A route to the target was always available.")


def check_battery(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    depleted = _contains(_by_event(events, "ROBOT_ERROR"), "battery")
    if depleted:
        e = depleted[0]
        return CheckResult(
            "battery", Verdict.FAIL,
            f"The robot's battery ran out mid-task: {e.get('message')}",
            evidence=_seqs(depleted),
        )

    critical = _by_event(events, "BATTERY_CRITICAL")
    if critical:
        e = critical[-1]
        return CheckResult(
            "battery", Verdict.WARN,
            f"Battery reached a critical level (<= {cfg.get('BATTERY_CRITICAL', 8)}%) "
            f"during the task: {e.get('message')}",
            evidence=_seqs(critical),
        )

    low = _by_event(events, "BATTERY_LOW")
    if low:
        e = low[-1]
        return CheckResult(
            "battery", Verdict.WARN,
            f"Battery ran low (<= {cfg.get('BATTERY_LOW', 20)}%) during the "
            f"task: {e.get('message')}",
            evidence=_seqs(low),
        )

    return CheckResult("battery", Verdict.PASS, "Battery stayed at a healthy level throughout.")


def check_collision_safety(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    hits = _by_event(events, "COLLISION_DETECTED")
    if hits:
        detail = "; ".join(h.get("message", "") for h in hits)
        return CheckResult(
            "collision_safety", Verdict.FAIL,
            f"{len(hits)} real collision(s) detected — two robots actually "
            f"occupied the same cell (this should never happen under normal "
            f"operation): {detail}",
            evidence=_seqs(hits),
        )
    return CheckResult("collision_safety", Verdict.PASS, "No two robots ever overlapped.")


def check_stuck_or_deadlock(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    waiting = _by_event(events, "ROBOT_WAITING")
    resolved = _by_event(events, "DEADLOCK_RESOLVED")
    replans = _by_event(events, "PATH_RECALCULATED")
    deadlock_ticks = cfg.get("DEADLOCK_WAIT_TICKS", 20)

    if len(waiting) >= deadlock_ticks and not resolved:
        completed_after = any(
            e.get("event") == "TASK_COMPLETED" and e["seq"] > waiting[-1]["seq"] for e in events
        )
        if not completed_after:
            return CheckResult(
                "stuck_or_deadlock", Verdict.FAIL,
                f"The robot waited on blocked traffic {len(waiting)} times "
                f"without ever recovering (no DEADLOCK_RESOLVED sidestep, no "
                f"successful replan reaching completion) — it looks stuck.",
                evidence=_seqs(waiting)[-5:],
            )

    if resolved:
        e = resolved[-1]
        return CheckResult(
            "stuck_or_deadlock", Verdict.WARN,
            f"The robot got blocked long enough to trigger a deadlock "
            f"sidestep: {e.get('message')}",
            evidence=_seqs(resolved),
        )

    if waiting:
        return CheckResult(
            "stuck_or_deadlock", Verdict.PASS,
            f"Robot yielded to traffic {len(waiting)} time(s) and recovered "
            f"normally ({len(replans)} replan(s)).",
        )

    return CheckResult("stuck_or_deadlock", Verdict.PASS, "No traffic contention observed.")


def check_controller_errors(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    errs = _contains(_by_event(events, "ROBOT_ERROR"), "controller error")
    if errs:
        e = errs[-1]
        return CheckResult(
            "controller_errors", Verdict.FAIL,
            f"The robot's controller raised an exception: {e.get('message')}",
            evidence=_seqs(errs),
        )
    return CheckResult("controller_errors", Verdict.PASS, "No controller exceptions were raised.")


def check_external_interruption(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    emergency = _by_event(events, "EMERGENCY_STOP")
    reset = _contains(events, "reset by the operator")
    if emergency:
        e = emergency[-1]
        return CheckResult(
            "external_interruption", Verdict.WARN,
            f"An emergency stop was active during this task: {e.get('message')}",
            evidence=_seqs(emergency),
        )
    if reset:
        e = reset[-1]
        return CheckResult(
            "external_interruption", Verdict.WARN,
            f"The task ended because an operator reset the robot, not "
            f"because of the robot's own logic: {e.get('message')}",
            evidence=_seqs(reset),
        )
    return CheckResult("external_interruption", Verdict.PASS, "No external interruption during the task.")


def _last_state(events: Sequence[Dict[str, Any]], *event_types: str) -> tuple:
    """The world-state snapshot (see TaskManager.snapshot_state) attached
    to the last matching event, plus that event itself — or (None, None)
    if no matching event carries one. Logs produced before this feature
    existed simply have no "state" key in ``data``, which every caller
    here treats as "nothing to say," not an error."""
    matches = _by_event(events, *event_types)
    for e in reversed(matches):
        state = (e.get("data") or {}).get("state")
        if state:
            return state, e
    return None, None


# --------------------------------------------------------------------------- #
# Mission Authorization Record
#
# A single consolidated artifact naming exactly who/what was involved in a
# task and whether it was trustworthy — task definition, entities, state
# diff, and (once graded) the verdict, in one place. check_entities_valid
# and check_state_transition above grade two slices of this same evidence;
# these functions expose the whole thing for anything that wants to show
# or ship it (see evals/generate_tests_groq.py, which builds the Groq
# suite's prompt from exactly these three pieces).
# --------------------------------------------------------------------------- #
_TERMINAL_EVENTS = ("TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED")


def extract_state_diff(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The full before/after world-state picture for one task log —
    always safe to serialize, even when no state was recorded (older
    logs, in which case this carries a "note" instead)."""
    before, _ = _last_state(events, "TASK_STARTED")
    after, after_evt = _last_state(events, *_TERMINAL_EVENTS)
    if before is None and after is None:
        return {
            "note": (
                "No physical world-state snapshot was recorded for this "
                "run (an older log, from before that feature existed)."
            )
        }
    return {
        "terminal_event": after_evt.get("event") if after_evt else None,
        "before": before,
        "after": after,
    }


def extract_task(events: Sequence[Dict[str, Any]]) -> Dict[str, Any]:
    """The task AS CREATED — what was asked for, not what happened.
    Pulled from TASK_CREATED's data (see TaskManager.create_task's
    ``data=`` for the fields new logs carry); older logs fall back to
    whatever's still available (the id and free-text message)."""
    created = next((e for e in events if e.get("event") == "TASK_CREATED"), None)
    if created is None:
        return {"note": "No TASK_CREATED event found in this log."}
    data = created.get("data") or {}
    return {
        "task_id": created.get("task_id"),
        "type": data.get("task_type"),
        "requested_robot": data.get("requested_robot"),
        "requested_agent": data.get("requested_agent"),
        "requested_operator": data.get("requested_operator"),
        "required_certification": data.get("required_certification"),
        "box_id": data.get("box_id", created.get("box_id")),
        "source": data.get("source"),
        "destination": data.get("destination"),
        "priority": data.get("priority"),
        "summary": data.get("summary"),
        "message": created.get("message"),
    }


def extract_entities(task: Dict[str, Any], state_diff: Dict[str, Any]) -> Dict[str, Any]:
    """Which real-world things this task involves — identity only (ids,
    names, which zones), never their state. Built from whichever state
    snapshot is available; falls back to the bare ids off the task
    definition when no snapshot was recorded."""
    snapshot = state_diff.get("before") or state_diff.get("after")
    if not snapshot:
        return {
            "robot_id": task.get("requested_robot"),
            "box_id": task.get("box_id"),
            "agent_id": task.get("requested_agent"),
            "operator_id": task.get("requested_operator"),
            "note": (
                "No state snapshot recorded for this log — zone/name "
                "detail isn't available, only the bare ids from the task."
            ),
        }
    entities: Dict[str, Any] = {}
    robot = snapshot.get("robot")
    if robot:
        entities["robot"] = {"id": robot.get("id"), "name": robot.get("name")}
    box = snapshot.get("box")
    if box:
        entities["box"] = {"id": box.get("id"), "name": box.get("name")}
    agent = snapshot.get("agent")
    if agent:
        entities["agent"] = {"id": agent.get("id"), "name": agent.get("name")}
    operator = snapshot.get("operator")
    if operator:
        entities["operator"] = {"id": operator.get("id"), "name": operator.get("name")}
    source_zone = snapshot.get("source_zone")
    if source_zone:
        entities["source_zone"] = {"key": source_zone.get("key"), "label": source_zone.get("label")}
    destination_zone = snapshot.get("destination_zone")
    if destination_zone:
        entities["destination_zone"] = {
            "key": destination_zone.get("key"),
            "label": destination_zone.get("label"),
        }
    return entities


def build_mission_record(
    events: Sequence[Dict[str, Any]], report: Optional["EvalReport"] = None
) -> Dict[str, Any]:
    """Consolidate the task definition, the entities involved, and the
    state diff into one object — optionally with a verdict, if a report
    for the same events is supplied."""
    task = extract_task(events)
    state_diff = extract_state_diff(events)
    entities = extract_entities(task, state_diff)
    record: Dict[str, Any] = {"task": task, "entities": entities, "state_diff": state_diff}
    if report is not None:
        record["verdict"] = report.verdict.value
        record["reasons"] = report.reasons
    return record


def check_entities_valid(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    """Were the entities this task was actually handed to eligible to do
    the work — not just resolved, but in a fit state to be trusted with
    it? Covers all three actor classes the same way, each against its own
    toy "assurance passport" (see TRUST_LAYER.md):

        robot     ERROR/STOPPED status, critical/low battery, firmware
                  outside APPROVED_FIRMWARE_VERSIONS, or a task type
                  outside the robot's own configured allowed_task_types
        agent     ERROR status, or a model_version outside
                  APPROVED_AGENT_MODELS
        operator  OFF_DUTY status, or missing the certification the task
                  actually required (CERTIFICATION_REQUIREMENTS) — graded
                  the same way for a `dual_signoff` task's second signer
                  (state.second_operator)

    All of this is checked *before* the task even started, against the
    "before" world-state snapshot (see TaskManager.snapshot_state) — the
    same evidence check_state_transition uses for the "after" picture, so
    a log with no snapshot skips both checks the same way. TaskManager.
    validate() now also blocks a task outright over the same conditions
    (see backend/eligibility.py) — this check is the *grade*, not the
    only line of defense: it still runs so an ineligible actor that
    somehow made it past the gate (e.g. it became ineligible mid-task, or
    the log predates the gate) is still caught here, after the fact.

    It also scans the whole event trail (not just the "before" snapshot)
    for TASK_AUTHORIZATION_CHANGED — Simulator._check_authorization_changes
    flagging that the assigned robot became ineligible AFTER the task
    already started. That's a softer signal than being ineligible from
    the start (the mission wasn't doomed from minute one), so it's a WARN
    here, not a FAIL — the same PASS/WARN/FAIL distinction battery
    already draws between "started critical" and "dropped low along the
    way"."""
    before, _ = _last_state(events, "TASK_STARTED")
    if before is None:
        return CheckResult(
            "entities_valid", Verdict.PASS,
            "No state snapshot recorded for this log (older log, or the "
            "task never started) — nothing to check.",
        )

    robot = before.get("robot")
    problems: List[str] = []
    worst = Verdict.PASS

    def _flag(verdict: Verdict, message: str) -> None:
        nonlocal worst
        problems.append(message)
        if _RANK[verdict] > _RANK[worst]:
            worst = verdict

    if robot:
        robot_id = robot.get("id")
        battery = robot.get("battery")
        reason = robot_eligibility(
            robot.get("status"), robot.get("firmware_version"), battery,
            battery_critical=cfg.get("BATTERY_CRITICAL", 8),
            task_type=before.get("task_type"), allowed_task_types=robot.get("allowed_task_types"),
        )
        if reason:
            _flag(Verdict.FAIL, f"robot {robot_id} {reason} when the task started")
        elif isinstance(battery, (int, float)) and battery <= cfg.get("BATTERY_LOW", 20):
            _flag(
                Verdict.WARN,
                f"robot {robot_id} already had a low battery "
                f"({battery}%) before the task started",
            )

    agent = before.get("agent")
    if agent:
        agent_id = agent.get("id")
        reason = agent_eligibility(agent.get("status"), agent.get("model_version"))
        if reason:
            _flag(Verdict.FAIL, f"agent {agent_id} {reason} when the task started")

    operator = before.get("operator")
    if operator:
        operator_id = operator.get("id")
        reason = operator_eligibility(
            operator.get("status"), operator.get("certifications"),
            before.get("required_certification"),
        )
        if reason:
            _flag(Verdict.FAIL, f"operator {operator_id} {reason} when the task started")

    second_operator = before.get("second_operator")
    if second_operator:
        second_id = second_operator.get("id")
        reason = operator_eligibility(
            second_operator.get("status"), second_operator.get("certifications"),
            before.get("required_certification"),
        )
        if reason:
            _flag(Verdict.FAIL, f"second signer {second_id} {reason} when the task started")

    authorization_changes = [e for e in events if e.get("event") == "TASK_AUTHORIZATION_CHANGED"]
    for change in authorization_changes:
        _flag(
            Verdict.WARN,
            f"authorization changed mid-task (seq {change.get('seq')}): {change.get('message')}",
        )

    if not problems:
        return CheckResult(
            "entities_valid", Verdict.PASS,
            "The entities assigned to this task were all in a valid, "
            "eligible state when it started.",
        )
    return CheckResult("entities_valid", worst, "; ".join(problems))


def check_state_transition(events: Sequence[Dict[str, Any]], cfg: Dict[str, Any]) -> CheckResult:
    """Compare the world state right before the task ran against the
    world state at its terminal event — not just *that* something
    happened, but whether the box/robot actually ended up where the task
    intended. A completed PICK_AND_DELIVER should leave the box sitting
    in its destination zone with the source zone freed up; a completed
    BATCH_DELIVER should leave *every* one of its boxes there the same
    way; a task that failed or was cancelled should leave nothing moved
    at all."""
    before, _ = _last_state(events, "TASK_STARTED")
    after, after_evt = _last_state(
        events, "TASK_COMPLETED", "TASK_FAILED", "TASK_CANCELLED"
    )

    if after is None:
        return CheckResult(
            "state_transition", Verdict.PASS,
            "No state snapshot recorded for this log (older log, or the "
            "task never reached a terminal event) — nothing to check.",
        )
    if before is None:
        # The task never actually got past creation/validation (e.g. it
        # failed before start_task ever ran) — nothing could have moved,
        # so the terminal snapshot doubles as its own "before" picture.
        before = after

    # A task carries its box(es) as either a single state["box"] (most
    # task types — see TaskManager.snapshot_state) or a state["boxes"]
    # list (BATCH_DELIVER's box_ids), never both. Normalize whichever
    # shape this log has into one {box_id: box} mapping so every check
    # below runs identically no matter which task type produced the log.
    # Before this normalization, this function only ever read
    # state["box"], so a BATCH_DELIVER log's boxes were silently ignored
    # entirely — every completed batch delivery fell through to the "no
    # box was ever part of this task" PASS below regardless of whether
    # the boxes actually arrived, which meant a CONFIG["FALSE_SUCCESS_
    # RISK"] fault on a batch delivery could never be caught here.
    if before.get("box") or after.get("box"):
        boxes_before = {before["box"]["id"]: before["box"]} if before.get("box") else {}
        boxes_after = {after["box"]["id"]: after["box"]} if after.get("box") else {}
    else:
        boxes_before = {b["id"]: b for b in (before.get("boxes") or []) if b.get("id")}
        boxes_after = {b["id"]: b for b in (after.get("boxes") or []) if b.get("id")}

    source = after.get("source_zone") or before.get("source_zone")
    destination = after.get("destination_zone") or before.get("destination_zone")

    if after_evt.get("event") == "TASK_COMPLETED":
        problems = []
        dest_after = after.get("destination_zone") or {}
        src_after = after.get("source_zone") or {}
        # Every box this task ever mentioned (before, after, or both) gets
        # checked — a box that vanished from the "after" snapshot entirely
        # is exactly as suspicious as one that's merely in the wrong zone.
        for box_id in sorted(set(boxes_before) | set(boxes_after)):
            box_after = boxes_after.get(box_id)
            if box_id in boxes_before and destination:
                if not box_after:
                    problems.append(
                        f"box {box_id} has no state recorded after delivery"
                    )
                elif box_after.get("zone") != destination.get("key"):
                    problems.append(
                        f"box {box_id} ended in zone {box_after.get('zone')!r}, "
                        f"not the intended destination {destination.get('key')!r}"
                    )
                # Check *membership* in each zone's box list, not a coarse
                # occupied flag — a shelf can hold several boxes at once, so
                # "still occupied" alone would never catch this specific box
                # failing to actually move.
                if box_id not in (dest_after.get("boxes_present") or []):
                    problems.append(
                        f"box {box_id} isn't listed in destination zone "
                        f"{destination.get('key')!r} after delivery"
                    )
            if source and destination and source.get("key") != destination.get("key"):
                if box_id in (src_after.get("boxes_present") or []):
                    problems.append(
                        f"box {box_id} is still listed in source zone "
                        f"{source.get('key')!r} after delivery"
                    )
        if problems:
            return CheckResult(
                "state_transition", Verdict.FAIL,
                "Task completed, but the world state doesn't match a real "
                "delivery: " + "; ".join(problems),
            )
        if boxes_before or boxes_after:
            return CheckResult(
                "state_transition", Verdict.PASS,
                "World state matches a completed delivery — every box "
                "ended up at its intended destination and zone occupancy "
                "updated correctly.",
            )
        # No box was ever part of this task (an agent/human-only task, or
        # a mission whose robot leg was purely an inspection) — there's
        # simply nothing here to contradict a clean completion.
        return CheckResult(
            "state_transition", Verdict.PASS,
            "Task completed and no state evidence contradicts it (no "
            "box/zone movement was part of this task).",
        )

    # FAILED or CANCELLED: nothing should have partially happened.
    changed = [
        box_id for box_id, b_before in boxes_before.items()
        if box_id in boxes_after
        and (b_before.get("position") != boxes_after[box_id].get("position")
             or b_before.get("status") != boxes_after[box_id].get("status"))
    ]
    if changed:
        return CheckResult(
            "state_transition", Verdict.WARN,
            f"The task did not complete, but box(es) {', '.join(sorted(changed))} "
            f"still changed state — a partial side effect worth a second look.",
        )
    return CheckResult(
        "state_transition", Verdict.PASS,
        "The task did not complete, and the world state correctly shows "
        "nothing moved.",
    )


DEFAULT_CHECKS: List[Callable[[Sequence[Dict[str, Any]], Dict[str, Any]], CheckResult]] = [
    check_sequence_integrity,
    check_terminal_state,
    check_path_exists,
    check_battery,
    check_collision_safety,
    check_stuck_or_deadlock,
    check_controller_errors,
    check_external_interruption,
    check_entities_valid,
    check_state_transition,
]


# --------------------------------------------------------------------------- #
# Entry points
# --------------------------------------------------------------------------- #
def evaluate_events(
    events: Sequence[Dict[str, Any]],
    task_id: Optional[str] = None,
    config: Optional[Dict[str, Any]] = None,
    checks: Optional[Sequence[Callable[..., CheckResult]]] = None,
) -> EvalReport:
    """Grade a single task's event list and return the full report."""
    cfg = {**DEFAULT_CONFIG, **(config or {})}
    raw = list(events)
    ordered = sorted(raw, key=lambda e: e.get("seq") or 0)
    active_checks = list(checks) if checks is not None else DEFAULT_CHECKS

    # check_sequence_integrity looks at ordering itself, so it needs the
    # events exactly as given; every other check gets the seq-sorted list
    # so a caller passing records slightly out of order doesn't confuse them.
    results = [
        check(raw, cfg) if check is check_sequence_integrity else check(ordered, cfg)
        for check in active_checks
    ]

    overall = Verdict.PASS
    for r in results:
        if _RANK[r.verdict] > _RANK[overall]:
            overall = r.verdict

    reasons = [f"[{r.name}] {r.message}" for r in results if r.verdict is not Verdict.PASS]

    tid = task_id or _task_id_of(ordered)
    summary = {
        "event_count": len(ordered),
        "robot_id": _robot_id_of(ordered),
        "first_seq": ordered[0]["seq"] if ordered else None,
        "last_seq": ordered[-1]["seq"] if ordered else None,
        "checks_passed": sum(1 for r in results if r.verdict == Verdict.PASS),
        "checks_warned": sum(1 for r in results if r.verdict == Verdict.WARN),
        "checks_failed": sum(1 for r in results if r.verdict == Verdict.FAIL),
    }

    return EvalReport(task_id=tid, verdict=overall, checks=results, reasons=reasons, summary=summary)


def evaluate_file(path: str, config: Optional[Dict[str, Any]] = None) -> EvalReport:
    """Grade a task log stored on disk (e.g. ``logs/tasks/task_001.json``)."""
    with open(path, "r", encoding="utf-8") as fh:
        events = json.load(fh)
    if not isinstance(events, list):
        raise ValueError(f"{path} does not contain a JSON array of log records")

    # Prefer the task id actually recorded in the events (ground truth);
    # fall back to the filename only if the records don't carry one.
    embedded = _task_id_of(events)
    filename_id = None
    base = os.path.basename(path)
    if base.endswith(".json"):
        filename_id = base[: -len(".json")]

    return evaluate_events(events, task_id=embedded or filename_id, config=config)


# --------------------------------------------------------------------------- #
# Metrics
#
# Pure aggregation over a batch of already-graded reports — a lightweight
# stand-in for the kind of coverage/success-rate dashboard numbers the
# doc's "evidence collection becomes an operational by-product" idea
# describes. Doesn't touch disk or re-derive anything; see
# backend/run_evals.py --stats for the CLI presentation.
# --------------------------------------------------------------------------- #
def compute_metrics(reports: Sequence[EvalReport]) -> Dict[str, Any]:
    total = len(reports)
    if total == 0:
        return {"total": 0}

    passed = sum(1 for r in reports if r.verdict == Verdict.PASS)
    warned = sum(1 for r in reports if r.verdict == Verdict.WARN)
    failed = sum(1 for r in reports if r.verdict == Verdict.FAIL)
    healthy = sum(1 for r in reports if r.passed)  # PASS or WARN

    # How many logs actually carried state-diff evidence to grade
    # against, versus how many predate that instrumentation and got a
    # free "nothing to check" pass — i.e. assurance *coverage*, not just
    # the pass rate over whatever happened to be gradable.
    state_covered = sum(
        1 for r in reports
        for c in r.checks
        if c.name == "state_transition" and "no state snapshot" not in c.message.lower()
    )

    fail_counts: Dict[str, int] = {}
    warn_counts: Dict[str, int] = {}
    for r in reports:
        for c in r.checks:
            if c.verdict == Verdict.FAIL:
                fail_counts[c.name] = fail_counts.get(c.name, 0) + 1
            elif c.verdict == Verdict.WARN:
                warn_counts[c.name] = warn_counts.get(c.name, 0) + 1

    return {
        "total": total,
        "passed": passed,
        "warned": warned,
        "failed": failed,
        "mission_success_rate": round(100 * healthy / total, 1),
        "clean_pass_rate": round(100 * passed / total, 1),
        "state_diff_coverage": round(100 * state_covered / total, 1),
        "failures_by_check": dict(sorted(fail_counts.items(), key=lambda kv: -kv[1])),
        "warnings_by_check": dict(sorted(warn_counts.items(), key=lambda kv: -kv[1])),
    }
