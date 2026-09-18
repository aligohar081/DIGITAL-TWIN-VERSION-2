"""Tests for backend/eval_engine.py.

Covers every check with small, hand-built event lists, then cross-checks
against the bundled example logs in logs/eval_examples/ and the project's
own real logs in logs/tasks/ so the engine's verdicts stay in sync with
whatever the simulator actually writes.
"""
import glob
import os

import pytest

from .eval_engine import (
    Verdict,
    evaluate_events,
    evaluate_file,
)

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
EXAMPLES_DIR = os.path.join(BASE_DIR, "logs", "eval_examples")
REAL_TASKS_DIR = os.path.join(BASE_DIR, "logs", "tasks")


def ev(seq, event=None, level="INFO", category="TASK", message="", **kw):
    """Minimal fixture record builder — only the fields the engine reads."""
    rec = {
        "seq": seq,
        "timestamp": f"2026-08-19T09:00:{seq:02d}",
        "time": f"09:00:{seq:02d}",
        "level": level,
        "category": category,
        "event": event,
        "robot_id": kw.pop("robot_id", "robot_01"),
        "task_id": kw.pop("task_id", "task_999"),
        "box_id": kw.pop("box_id", None),
        "position": kw.pop("position", None),
        "message": message,
        "data": kw.pop("data", {}),
    }
    rec.update(kw)
    return rec


def renumber(events):
    """Reassign seq 1..N in list order — for tests that splice extra
    events into the middle of a happy path and would otherwise end up
    with duplicate or out-of-order seq numbers by construction."""
    out = []
    for i, e in enumerate(events, start=1):
        e = dict(e)
        e["seq"] = i
        out.append(e)
    return out


def happy_path(task_id="task_999"):
    return [
        ev(1, "TASK_CREATED", message=f"{task_id} created", task_id=task_id),
        ev(2, message=f"{task_id} validation started", task_id=task_id),
        ev(3, "TASK_VALIDATED", message=f"{task_id} validated successfully", task_id=task_id),
        ev(4, "TASK_ASSIGNED", message=f"{task_id} assigned to Robo-01", task_id=task_id),
        ev(5, "TASK_STARTED", message=f"{task_id} started", task_id=task_id),
        ev(6, "BOX_PICKED", message="Robo-01 picked Box-A", box_id="box_001", task_id=task_id),
        ev(7, "BOX_DELIVERED", message="Box-A delivered", box_id="box_001", task_id=task_id),
        ev(8, "TASK_COMPLETED", message=f"{task_id} COMPLETED", task_id=task_id),
    ]


# --------------------------------------------------------------------- #
# Overall verdict / happy path
# --------------------------------------------------------------------- #
def test_happy_path_is_a_clean_pass():
    report = evaluate_events(happy_path())
    assert report.verdict == Verdict.PASS
    assert report.ok is True
    assert report.passed is True
    assert report.reasons == []
    assert all(c.verdict == Verdict.PASS for c in report.checks)


def test_report_serialises_to_dict():
    report = evaluate_events(happy_path(), task_id="task_999")
    d = report.to_dict()
    assert d["task_id"] == "task_999"
    assert d["verdict"] == "PASS"
    assert d["ok"] is True
    assert isinstance(d["checks"], list) and len(d["checks"]) >= 5
    assert d["summary"]["event_count"] == 8


def test_empty_log_fails():
    report = evaluate_events([])
    assert report.verdict == Verdict.FAIL
    assert any(c.name == "sequence_integrity" for c in report.checks)


# --------------------------------------------------------------------- #
# terminal_state
# --------------------------------------------------------------------- #
def test_task_failed_is_flagged_with_the_real_reason():
    events = happy_path()[:5] + [
        ev(6, "TASK_FAILED", level="ERROR",
           message="task_999 FAILED — Box-A is already reserved by task_998")
    ]
    report = evaluate_events(events)
    assert report.verdict == Verdict.FAIL
    term = next(c for c in report.checks if c.name == "terminal_state")
    assert term.verdict == Verdict.FAIL
    assert "already reserved" in term.message
    assert any("already reserved" in r for r in report.reasons)


def test_task_cancelled_counts_as_not_ok():
    events = happy_path()[:5] + [
        ev(6, "TASK_CANCELLED", level="WARNING", category="USER",
           message="task_999 cancelled by user")
    ]
    report = evaluate_events(events)
    assert report.verdict == Verdict.FAIL
    assert not report.passed


def test_no_terminal_event_at_all_fails():
    events = happy_path()[:5]  # started, never finishes
    report = evaluate_events(events)
    term = next(c for c in report.checks if c.name == "terminal_state")
    assert term.verdict == Verdict.FAIL
    assert report.verdict == Verdict.FAIL


# --------------------------------------------------------------------- #
# battery
# --------------------------------------------------------------------- #
def test_battery_depleted_fails():
    events = happy_path()[:5] + [
        ev(6, "ROBOT_ERROR", level="CRITICAL", category="BATTERY",
           message="Robo-01 battery depleted — robot halted"),
        ev(7, "TASK_FAILED", level="ERROR",
           message="task_999 FAILED — Battery depleted mid-mission"),
    ]
    report = evaluate_events(events)
    battery = next(c for c in report.checks if c.name == "battery")
    assert battery.verdict == Verdict.FAIL
    assert report.verdict == Verdict.FAIL


def test_battery_low_but_completed_is_a_warning_not_a_failure():
    events = renumber(happy_path()[:5] + [
        ev(6, "BATTERY_LOW", level="WARNING", category="BATTERY",
           message="Robo-01 battery low at 19%"),
    ] + happy_path()[5:])
    report = evaluate_events(events)
    battery = next(c for c in report.checks if c.name == "battery")
    assert battery.verdict == Verdict.WARN
    assert report.verdict == Verdict.WARN
    assert report.passed is True
    assert report.ok is False


def test_battery_critical_outranks_battery_low():
    events = [
        ev(1, "BATTERY_LOW", category="BATTERY", message="battery low at 18%"),
        ev(2, "BATTERY_CRITICAL", level="CRITICAL", category="BATTERY",
           message="battery critical at 7%"),
    ]
    battery = next(c for c in evaluate_events(events).checks if c.name == "battery")
    assert battery.verdict == Verdict.WARN
    assert "critical" in battery.message.lower()


# --------------------------------------------------------------------- #
# path_exists
# --------------------------------------------------------------------- #
def test_path_not_found_fails():
    events = happy_path()[:5] + [
        ev(6, "PATH_NOT_FOUND", level="ERROR", category="NAVIGATION",
           message="No route from (3,8) to (12,13) for Robo-01"),
        ev(7, "TASK_FAILED", level="ERROR",
           message="task_999 FAILED — No path available to the target"),
    ]
    report = evaluate_events(events)
    assert next(c for c in report.checks if c.name == "path_exists").verdict == Verdict.FAIL
    assert report.verdict == Verdict.FAIL


# --------------------------------------------------------------------- #
# collision_safety
# --------------------------------------------------------------------- #
def test_real_collision_fails_even_if_task_completes():
    events = happy_path()
    events.insert(5, ev(99, "COLLISION_DETECTED", level="CRITICAL", category="COLLISION",
                         message="Collision: Robo-01 and Robo-02 both occupy (3,6)"))
    events = renumber(events)
    report = evaluate_events(events)
    collision = next(c for c in report.checks if c.name == "collision_safety")
    assert collision.verdict == Verdict.FAIL
    assert report.verdict == Verdict.FAIL  # even though TASK_COMPLETED is present


def test_collision_avoided_is_not_a_real_collision():
    events = renumber(happy_path()[:5] + [
        ev(6, "COLLISION_AVOIDED", level="WARNING", category="COLLISION",
           message="Potential collision at (7,9): Robo-02 is in the way of Robo-01"),
    ] + happy_path()[5:])
    report = evaluate_events(events)
    collision = next(c for c in report.checks if c.name == "collision_safety")
    assert collision.verdict == Verdict.PASS


# --------------------------------------------------------------------- #
# stuck_or_deadlock
# --------------------------------------------------------------------- #
def test_robot_stuck_without_recovery_fails():
    events = happy_path()[:5] + [
        ev(6 + i, "ROBOT_WAITING", level="WARNING", category="ROBOT",
           message="Robo-01 waiting for Robo-02")
        for i in range(25)
    ]  # no DEADLOCK_RESOLVED, no TASK_COMPLETED afterwards
    report = evaluate_events(events)
    stuck = next(c for c in report.checks if c.name == "stuck_or_deadlock")
    assert stuck.verdict == Verdict.FAIL
    assert report.verdict == Verdict.FAIL


def test_deadlock_that_resolves_is_only_a_warning():
    events = renumber(happy_path()[:5] + [
        ev(6, "ROBOT_WAITING", level="WARNING", category="ROBOT", message="waiting"),
        ev(7, "DEADLOCK_RESOLVED", level="WARNING", category="COLLISION",
           message="Robo-01 stepped aside to (4,9) to clear a deadlock with Robo-02"),
    ] + happy_path()[5:])
    report = evaluate_events(events)
    stuck = next(c for c in report.checks if c.name == "stuck_or_deadlock")
    assert stuck.verdict == Verdict.WARN
    assert report.verdict == Verdict.WARN


def test_brief_wait_that_recovers_is_a_pass():
    events = renumber(happy_path()[:5] + [
        ev(6, "COLLISION_AVOIDED", category="COLLISION", message="avoided"),
        ev(7, "ROBOT_WAITING", category="ROBOT", message="waiting"),
        ev(8, "PATH_RECALCULATED", category="NAVIGATION", message="alternative path found"),
    ] + happy_path()[5:])
    report = evaluate_events(events)
    stuck = next(c for c in report.checks if c.name == "stuck_or_deadlock")
    assert stuck.verdict == Verdict.PASS


# --------------------------------------------------------------------- #
# controller_errors
# --------------------------------------------------------------------- #
def test_controller_exception_fails():
    events = happy_path()[:5] + [
        ev(6, "ROBOT_ERROR", level="ERROR", category="ROBOT",
           message="Robo-01 controller error: KeyError('x')"),
        ev(7, "TASK_FAILED", level="ERROR", message="task_999 FAILED — Controller error: KeyError('x')"),
    ]
    report = evaluate_events(events)
    assert next(c for c in report.checks if c.name == "controller_errors").verdict == Verdict.FAIL
    assert report.verdict == Verdict.FAIL


# --------------------------------------------------------------------- #
# external_interruption
# --------------------------------------------------------------------- #
def test_operator_reset_is_flagged_as_external_not_a_robot_bug():
    events = happy_path()[:5] + [
        ev(6, "ROBOT_RESET", level="WARNING", category="ROBOT",
           message="Robo-01 reset to (3,12) with a full battery"),
        ev(7, "TASK_FAILED", level="ERROR",
           message="task_999 FAILED — Robo-01 was reset by the operator"),
    ]
    report = evaluate_events(events)
    ext = next(c for c in report.checks if c.name == "external_interruption")
    assert ext.verdict == Verdict.WARN
    # still an overall FAIL because the task itself did not complete
    assert report.verdict == Verdict.FAIL


# --------------------------------------------------------------------- #
# entities_valid
#
# Toy "Robot Assurance Passport" check — was the entity assigned to this
# task actually eligible to do the work, before it even started? Uses
# the same TASK_STARTED "before" snapshot as state_transition.
# --------------------------------------------------------------------- #
def _robot_state(status="IDLE", battery=100.0, firmware="2.1.0", allowed_task_types=None):
    return {
        "id": "robot_01", "position": {"x": 1, "y": 1}, "zone": "parking_area",
        "status": status, "carrying_box": None, "battery": battery,
        "firmware_version": firmware, "allowed_task_types": allowed_task_types,
    }


def test_healthy_robot_at_task_start_passes_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"robot": _robot_state()}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.PASS


def test_robot_already_in_error_status_at_start_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"robot": _robot_state(status="ERROR")}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL
    assert "ERROR" in ent.message
    assert report.verdict == Verdict.FAIL


def test_unapproved_firmware_at_start_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"robot": _robot_state(firmware="0.9.0-beta")}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL
    assert "0.9.0-beta" in ent.message
    assert report.verdict == Verdict.FAIL


def test_critical_battery_at_start_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"robot": _robot_state(battery=5.0)}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL


def test_low_battery_at_start_warns_entities_valid_not_fails():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"robot": _robot_state(battery=15.0)}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.WARN


def test_no_state_snapshot_skips_entities_valid_cleanly():
    report = evaluate_events(happy_path())  # no data.state anywhere
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.PASS


def test_robot_outside_its_configured_task_types_fails_entities_valid():
    # A robot's own allowed_task_types (backend/robot.py) is enforced the
    # same way firmware/status are — see backend/eligibility.py.
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "task_type": "PICK_AND_DELIVER",
        "robot": _robot_state(allowed_task_types=["MOVE_ROBOT"]),
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL
    assert "not configured to run" in ent.message


def test_robot_within_its_configured_task_types_passes_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "task_type": "MOVE_ROBOT",
        "robot": _robot_state(allowed_task_types=["MOVE_ROBOT", "PICK_AND_DELIVER"]),
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.PASS


def test_unrestricted_robot_passes_entities_valid_for_any_task_type():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "task_type": "PICK_AND_DELIVER",
        "robot": _robot_state(),  # allowed_task_types=None — unrestricted
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.PASS


def _agent_state(status="IDLE", model="groq/gpt-oss-20b"):
    return {"id": "agent_001", "name": "Ada", "model_version": model, "status": status}


def _operator_state(status="AVAILABLE", certifications=None):
    return {
        "id": "operator_001", "name": "Sam", "status": status,
        "certifications": certifications if certifications is not None else ["safety_inspection"],
    }


def test_healthy_agent_and_operator_pass_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "agent": _agent_state(), "operator": _operator_state(),
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.PASS


def test_unapproved_agent_model_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"agent": _agent_state(model="fake/v0")}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL
    assert "fake/v0" in ent.message


def test_agent_already_in_error_status_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"agent": _agent_state(status="ERROR")}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL


def test_off_duty_operator_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"operator": _operator_state(status="OFF_DUTY")}})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL


def test_operator_missing_required_certification_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "operator": _operator_state(certifications=["safety_inspection"]),
        "required_certification": "electrical_safety",
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL
    assert "electrical_safety" in ent.message


def test_operator_holding_required_certification_passes_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "operator": _operator_state(certifications=["safety_inspection", "electrical_safety"]),
        "required_certification": "electrical_safety",
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.PASS


# --------------------------------------------------------------------- #
# entities_valid — dual sign-off's second signer (state.second_operator)
# and mid-task authorization changes (TASK_AUTHORIZATION_CHANGED)
# --------------------------------------------------------------------- #
def test_second_signer_missing_certification_fails_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "operator": _operator_state(certifications=["safety_inspection", "electrical_safety"]),
        "second_operator": _operator_state(certifications=["safety_inspection"]),
        "required_certification": "electrical_safety",
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.FAIL
    assert "second signer" in ent.message


def test_both_dual_signoff_operators_eligible_passes_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {
        "operator": _operator_state(certifications=["safety_inspection", "electrical_safety"]),
        "second_operator": _operator_state(certifications=["safety_inspection", "electrical_safety"]),
        "required_certification": "electrical_safety",
    }})
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.PASS


def test_mid_task_authorization_change_warns_entities_valid():
    events = happy_path()[:5]
    events[4] = ev(5, "TASK_STARTED", data={"state": {"robot": _robot_state()}})
    events.insert(5, ev(6, "TASK_AUTHORIZATION_CHANGED", level="WARNING",
                         message="task_999: Robo-01 is running unapproved firmware '0.9.0-beta'"))
    events = renumber(events)
    report = evaluate_events(events)
    ent = next(c for c in report.checks if c.name == "entities_valid")
    assert ent.verdict == Verdict.WARN
    assert "authorization changed mid-task" in ent.message


# --------------------------------------------------------------------- #
# state_transition
#
# These use the world-state snapshot TaskManager.snapshot_state attaches
# to TASK_STARTED (data.state = "before") and to the terminal event
# (data.state = "after") — see backend/task_manager.py.
# --------------------------------------------------------------------- #
def _box_state(zone, status="STORED"):
    return {"id": "box_001", "position": {"x": 1, "y": 1}, "zone": zone, "status": status}


def _zone_state(key, occupied):
    return {"key": key, "label": key.title(), "occupied": occupied, "boxes_present": ["box_001"] if occupied else []}


def test_completed_delivery_with_correct_state_passes():
    events = happy_path()[:5]
    events[4] = ev(
        5, "TASK_STARTED",
        data={"state": {
            "box": _box_state("shelf_a", "RESERVED"),
            "source_zone": _zone_state("shelf_a", True),
            "destination_zone": _zone_state("shelf_b", False),
        }},
    )
    events += [
        ev(6, "BOX_PICKED", message="picked", box_id="box_001"),
        ev(7, "BOX_DELIVERED", message="delivered", box_id="box_001"),
        ev(8, "TASK_COMPLETED", message="task_999 COMPLETED", data={"state": {
            "box": _box_state("shelf_b", "DELIVERED"),
            "source_zone": _zone_state("shelf_a", False),
            "destination_zone": _zone_state("shelf_b", True),
        }}),
    ]
    report = evaluate_events(events)
    st = next(c for c in report.checks if c.name == "state_transition")
    assert st.verdict == Verdict.PASS
    assert report.verdict == Verdict.PASS


def test_completed_task_that_never_actually_moved_the_box_fails():
    events = happy_path()[:5]
    events[4] = ev(
        5, "TASK_STARTED",
        data={"state": {
            "box": _box_state("shelf_a", "RESERVED"),
            "source_zone": _zone_state("shelf_a", True),
            "destination_zone": _zone_state("shelf_b", False),
        }},
    )
    events += [
        ev(6, "BOX_PICKED", message="picked", box_id="box_001"),
        ev(7, "BOX_DELIVERED", message="delivered", box_id="box_001"),
        # box's own state still shows it sitting in the source zone —
        # the event trail says "delivered" but the world never changed.
        ev(8, "TASK_COMPLETED", message="task_999 COMPLETED", data={"state": {
            "box": _box_state("shelf_a", "RESERVED"),
            "source_zone": _zone_state("shelf_a", True),
            "destination_zone": _zone_state("shelf_b", False),
        }}),
    ]
    report = evaluate_events(events)
    st = next(c for c in report.checks if c.name == "state_transition")
    assert st.verdict == Verdict.FAIL
    assert "shelf_a" in st.message  # names the zone the box was actually still in
    assert report.verdict == Verdict.FAIL


def test_failed_task_with_no_side_effects_passes():
    box = _box_state("shelf_a", "STORED")
    events = happy_path()[:4] + [
        ev(5, "TASK_STARTED", data={"state": {"box": box, "source_zone": _zone_state("shelf_a", True)}}),
        ev(6, "TASK_FAILED", level="ERROR", message="task_999 FAILED — no path",
           data={"state": {"box": box, "source_zone": _zone_state("shelf_a", True)}}),
    ]
    report = evaluate_events(events)
    st = next(c for c in report.checks if c.name == "state_transition")
    assert st.verdict == Verdict.PASS


def test_failed_task_that_still_moved_the_box_warns():
    events = happy_path()[:4] + [
        ev(5, "TASK_STARTED", data={"state": {"box": _box_state("shelf_a", "RESERVED")}}),
        ev(6, "TASK_FAILED", level="ERROR", message="task_999 FAILED — controller error",
           data={"state": {"box": _box_state("shelf_a", "PICKING")}}),
    ]
    report = evaluate_events(events)
    st = next(c for c in report.checks if c.name == "state_transition")
    assert st.verdict == Verdict.WARN


def test_logs_without_state_snapshots_skip_cleanly():
    report = evaluate_events(happy_path())  # no data.state anywhere — pre-dates the feature
    st = next(c for c in report.checks if c.name == "state_transition")
    assert st.verdict == Verdict.PASS
    assert report.verdict == Verdict.PASS


# --------------------------------------------------------------------- #
# sequence_integrity
# --------------------------------------------------------------------- #
def test_out_of_order_seq_fails():
    events = [ev(1), ev(3), ev(2)]
    report = evaluate_events(events)
    assert next(c for c in report.checks if c.name == "sequence_integrity").verdict == Verdict.FAIL


def test_delivered_before_picked_fails():
    events = [
        ev(1, "BOX_DELIVERED", message="delivered"),
        ev(2, "BOX_PICKED", message="picked"),
    ]
    report = evaluate_events(events)
    assert next(c for c in report.checks if c.name == "sequence_integrity").verdict == Verdict.FAIL


# --------------------------------------------------------------------- #
# evaluate_file() / task id resolution
# --------------------------------------------------------------------- #
def test_evaluate_file_reads_json_array_from_disk(tmp_path):
    import json

    p = tmp_path / "task_042.json"
    p.write_text(json.dumps(happy_path("task_042")))
    report = evaluate_file(str(p))
    assert report.task_id == "task_042"
    assert report.verdict == Verdict.PASS


def test_evaluate_file_prefers_embedded_task_id_over_filename(tmp_path):
    import json

    p = tmp_path / "renamed_example.json"
    p.write_text(json.dumps(happy_path("task_042")))
    report = evaluate_file(str(p))
    assert report.task_id == "task_042"


def test_evaluate_file_rejects_non_list_json(tmp_path):
    p = tmp_path / "task_043.json"
    p.write_text('{"not": "a list"}')
    with pytest.raises(ValueError):
        evaluate_file(str(p))


# --------------------------------------------------------------------- #
# Cross-check against the bundled example logs
# --------------------------------------------------------------------- #
EXPECTED_EXAMPLE_VERDICTS = {
    "task_100_clean_pass.json": Verdict.PASS,
    "task_101_battery_depleted.json": Verdict.FAIL,
    "task_102_robot_stuck.json": Verdict.FAIL,
    "task_103_path_not_found.json": Verdict.FAIL,
    "task_104_collision_detected.json": Verdict.FAIL,
    "task_105_box_already_reserved.json": Verdict.FAIL,
    "task_106_controller_error.json": Verdict.FAIL,
    "task_107_task_cancelled.json": Verdict.FAIL,
    "task_108_battery_low_recovered.json": Verdict.WARN,
    # Real run captured via the simulator itself (not hand-built): Robo-01
    # starts in parking_area, Box-D starts in shelf_a, task destination is
    # shelf_b. Carries real TaskManager.snapshot_state data on TASK_STARTED
    # and TASK_COMPLETED — the first example the state_transition check
    # actually has a state diff to grade instead of skipping.
    "task_109_state_pick_and_deliver.json": Verdict.PASS,
    # Real run: same as task_109, but Robo-01's firmware_version was set to
    # an unapproved value before the task ran. The task mechanically
    # completes — box delivered, state matches — but entities_valid still
    # fails the whole thing: the robot should never have been trusted with
    # the work in the first place, regardless of how it turned out.
    "task_110_entities_invalid_firmware.json": Verdict.FAIL,
    # Real runs of MIXED_MAINTENANCE_MISSION — all three actor classes
    # (agent recommends, robot physically inspects, operator signs off)
    # in one task. task_111 uses Sam, who holds every certification the
    # mission needs; task_112 uses Lee, who doesn't hold
    # 'electrical_safety' — the robot's physical inspection still
    # succeeds, but entities_valid still fails the whole task over it.
    "task_111_mixed_mission_clean_pass.json": Verdict.PASS,
    "task_112_mixed_mission_uncertified_operator.json": Verdict.FAIL,
    # Real run: OPERATOR_MAINTENANCE_SIGNOFF with dual_signoff=True — Sam
    # (primary) and Kim (second signer) both hold electrical_safety.
    "task_113_dual_signoff_clean_pass.json": Verdict.PASS,
    # Real run: BATCH_DELIVER — Box-A and Box-B both delivered to
    # loading_zone under one task id.
    "task_114_batch_deliver_clean_pass.json": Verdict.PASS,
    # Real run: a PICK_AND_DELIVER task's robot has its firmware
    # downgraded to an unapproved version mid-route. Simulator._check_
    # authorization_changes flags it (TASK_AUTHORIZATION_CHANGED) but
    # doesn't halt the task — it still completes. A WARN, not a FAIL: the
    # mission wasn't doomed from the start, just worth a second look.
    "task_115_mid_task_authorization_changed.json": Verdict.WARN,
    # Hand-built from task_113 (a dual_signoff task can't naturally reach
    # this state — the pre-execution gate already requires the second
    # signer to be certified before the task is even created): Kim's
    # electrical_safety certification is missing in the "before" snapshot.
    "task_116_dual_signoff_second_signer_uncertified.json": Verdict.FAIL,
    # Hand-edited from task_109: the event trail is left completely
    # untouched — BOX_DELIVERED, TASK_COMPLETED, the same success
    # message — so every event-trail check (terminal_state included)
    # reports a clean PASS; the system itself is fully "sure" it
    # delivered Box-D. Only the physical state.state snapshot attached
    # to that same TASK_COMPLETED record tells the truth: the box never
    # actually left Shelf-A. A confidently-wrong completion, and
    # state_transition is the one check that reads ground truth instead
    # of the narrative, so it's the only thing that catches it.
    "task_117_false_success_box_never_arrived.json": Verdict.FAIL,
    # Hand-edited from task_114 (same technique as task_117, applied to a
    # BATCH_DELIVER): the event trail is left completely untouched —
    # both BOX_DELIVERED events, TASK_COMPLETED, the same success message
    # — but Box-B's state.state snapshot on TASK_COMPLETED shows it never
    # left shelf_b (Box-A's is untouched, a genuine delivery). Before
    # check_state_transition learned to read state["boxes"] (BATCH_
    # DELIVER's list, not state["box"]), this fell through to "no box/
    # zone movement was part of this task" and graded a clean PASS —
    # a FALSE_SUCCESS_RISK fault on any batch delivery was invisible to
    # every consumer of this engine (the dashboard, /api/decisions, and
    # promptfoo's default suite alike).
    "task_118_batch_deliver_false_success.json": Verdict.FAIL,
}


@pytest.mark.skipif(not os.path.isdir(EXAMPLES_DIR), reason="example logs not present")
@pytest.mark.parametrize("filename,expected", sorted(EXPECTED_EXAMPLE_VERDICTS.items()))
def test_bundled_examples_grade_as_expected(filename, expected):
    path = os.path.join(EXAMPLES_DIR, filename)
    report = evaluate_file(path)
    assert report.verdict == expected, report.reasons


@pytest.mark.skipif(not os.path.isdir(EXAMPLES_DIR), reason="example logs not present")
def test_every_example_file_is_covered_by_the_expectation_table():
    on_disk = {os.path.basename(f) for f in glob.glob(os.path.join(EXAMPLES_DIR, "task_*.json"))}
    assert on_disk == set(EXPECTED_EXAMPLE_VERDICTS)


# --------------------------------------------------------------------- #
# Cross-check against the project's own real task logs, if present
# --------------------------------------------------------------------- #
@pytest.mark.skipif(not os.path.isdir(REAL_TASKS_DIR), reason="no real task logs in this checkout")
def test_real_task_logs_all_evaluate_without_error():
    files = glob.glob(os.path.join(REAL_TASKS_DIR, "task_*.json"))
    assert files, "expected at least one real task log to grade"
    for f in files:
        report = evaluate_file(f)
        assert report.verdict in (Verdict.PASS, Verdict.WARN, Verdict.FAIL)
        assert report.task_id


@pytest.mark.skipif(
    not os.path.exists(os.path.join(REAL_TASKS_DIR, "task_006.json")),
    reason="task_006.json (the real reserved-box failure) is not in this checkout",
)
def test_real_task_006_box_conflict_is_caught():
    report = evaluate_file(os.path.join(REAL_TASKS_DIR, "task_006.json"))
    assert report.verdict == Verdict.FAIL
    assert any("already reserved" in r for r in report.reasons)


# --------------------------------------------------------------------- #
# Decision history search (backend/decision_graph.py) — graded against
# the bundled example logs, not the live logs/tasks/ directory, so this
# stays stable regardless of what the running app has actually done.
# --------------------------------------------------------------------- #
def test_decision_graph_filters_by_operator():
    from .decision_graph import query_decisions

    results = query_decisions(operator="Sam", tasks_dir=EXAMPLES_DIR)
    assert results
    for record in results:
        entities = record.get("entities") or {}
        operator = (entities.get("operator") or {}).get("name")
        requested = record["task"].get("requested_operator")
        assert operator == "Sam" or requested == "Sam"


def test_decision_graph_filters_by_verdict():
    from .decision_graph import query_decisions

    results = query_decisions(verdict="FAIL", tasks_dir=EXAMPLES_DIR)
    assert results
    assert all(r.get("verdict") == "FAIL" for r in results)


def test_decision_graph_battery_below_filter_only_matches_low_battery_starts():
    from .decision_graph import query_decisions

    results = query_decisions(battery_below=25, tasks_dir=EXAMPLES_DIR)
    for record in results:
        before = (record.get("state_diff") or {}).get("before") or {}
        battery = (before.get("robot") or {}).get("battery")
        assert isinstance(battery, (int, float)) and battery < 25
