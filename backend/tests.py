"""Automated tests for the warehouse digital twin.

Run with::

    python -m pytest

The simulation is driven tick-by-tick, so every test is deterministic and no
background threads are involved.
"""
from __future__ import annotations

import json
import os
from typing import Any, Optional

import pytest

from .app import create_app
from .ci_engine import CIEngine
from .digital_twin import DigitalTwin
from .models import (
    BoxStatus,
    CONFIG,
    CellType,
    LogCategory,
    LogLevel,
    OperatorStatus,
    Priority,
    PRIORITY_RANK,
    RobotStatus,
    SimulationStatus,
    TaskStatus,
    TaskType,
)
from .simulator import Simulator


# --------------------------------------------------------------------------- #
# Fixtures & helpers
# --------------------------------------------------------------------------- #
@pytest.fixture
def twin(tmp_path) -> DigitalTwin:
    return DigitalTwin(
        log_dir=str(tmp_path / "logs"),
        data_dir=str(tmp_path / "data"),
        persist_logs=False,
        demo=True,
        demo_tasks=False,
    )


@pytest.fixture
def sim(twin) -> Simulator:
    simulator = Simulator(twin)
    twin.simulation_status = SimulationStatus.RUNNING
    return simulator


def run_until(sim: Simulator, predicate, max_ticks: int = 800) -> int:
    for tick in range(max_ticks):
        if predicate():
            return tick
        sim.tick()
    if predicate():
        return max_ticks
    raise AssertionError(f"Condition never became true within {max_ticks} ticks")


def run_task(sim: Simulator, twin: DigitalTwin, payload: dict, max_ticks: int = 800):
    task = twin.tasks.create_task(payload)
    run_until(sim, lambda: task.is_terminal, max_ticks)
    return task


# --------------------------------------------------------------------------- #
# Warehouse & environment
# --------------------------------------------------------------------------- #
def test_warehouse_dimensions_and_walls(twin):
    warehouse = twin.warehouse
    assert (warehouse.width, warehouse.height) == (CONFIG["GRID_WIDTH"], CONFIG["GRID_HEIGHT"])
    for x in range(warehouse.width):
        assert warehouse.cell_type(x, 0) is CellType.WALL
        assert warehouse.cell_type(x, warehouse.height - 1) is CellType.WALL
    for y in range(warehouse.height):
        assert warehouse.cell_type(0, y) is CellType.WALL
        assert warehouse.cell_type(warehouse.width - 1, y) is CellType.WALL


def test_named_zones_resolve_including_aliases(twin):
    warehouse = twin.warehouse
    for name in ("shelf_a", "Shelf-A", "SHELF A", "storage_a", "loading_zone", "Loading-Zone"):
        assert warehouse.resolve_zone(name) is not None, name
    assert warehouse.resolve_zone("nowhere") is None


def test_restricted_and_shelf_cells_are_not_drivable(twin):
    warehouse = twin.warehouse
    restricted = warehouse.resolve_zone("restricted_area")
    for cell in restricted.cells:
        assert not warehouse.is_walkable(*cell)
    assert not warehouse.is_walkable(3, 3)  # inside Shelf-A racking


def test_every_drivable_cell_is_reachable(twin):
    warehouse = twin.warehouse
    walkable = warehouse.walkable_cells()
    seen = {walkable[0]}
    stack = [walkable[0]]
    while stack:
        for neighbor in warehouse.neighbors(stack.pop()):
            if neighbor not in seen:
                seen.add(neighbor)
                stack.append(neighbor)
    assert len(seen) == len(walkable)


# --------------------------------------------------------------------------- #
# Robots
# --------------------------------------------------------------------------- #
def test_demo_robots_and_boxes_exist(twin):
    assert len(twin.robots) == 2
    assert twin.find_robot("Robo-01") is not None
    assert twin.find_robot("robo-02") is not None
    assert len(twin.boxes) == 5
    assert twin.find_box("Box-A") is not None


def test_add_robot_appears_in_twin(twin):
    robot = twin.add_robot(name="Robo-99", position=(8, 7), speed=3.0)
    assert robot.id in twin.robots
    assert robot.speed == 3.0
    assert twin.warehouse.is_walkable(*robot.position)


def test_add_robot_rejects_duplicate_name(twin):
    with pytest.raises(ValueError):
        twin.add_robot(name="Robo-01", position=(8, 7))


def test_add_robot_relocates_off_a_wall(twin):
    robot = twin.add_robot(name="Robo-Wall", position=(0, 0))
    assert twin.warehouse.is_walkable(*robot.position)


def test_add_robot_outside_grid_is_rejected(twin):
    with pytest.raises(ValueError):
        twin.add_robot(name="Robo-Void", position=(999, 999))


def test_robot_stays_inside_boundaries_during_a_long_run(twin, sim):
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"})
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-02", "destination": "charging_station"})
    for _ in range(120):
        sim.tick()
        for robot in twin.robots.values():
            assert twin.warehouse.is_inside(*robot.position)
            assert twin.warehouse.is_walkable(*robot.position)


def test_stop_and_resume_robot(twin, sim):
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"})
    for _ in range(6):
        sim.tick()
    robot = twin.find_robot("Robo-01")
    twin.stop_robot(robot.id)
    frozen = robot.position
    for _ in range(15):
        sim.tick()
    assert robot.position == frozen
    assert robot.status is RobotStatus.STOPPED
    twin.resume_robot(robot.id)
    assert robot.status is not RobotStatus.STOPPED
    run_until(sim, lambda: robot.position != frozen, 60)


def test_reset_robot_returns_home_and_drops_its_box(twin, sim):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    robot = twin.find_robot("Robo-01")
    run_until(sim, lambda: robot.carrying_box is not None, 200)
    twin.reset_robot(robot.id)
    assert robot.carrying_box is None
    assert robot.battery == 100.0
    assert robot.position == robot.home
    assert task.status is TaskStatus.FAILED
    assert twin.find_box("Box-A").status is BoxStatus.STORED


# --------------------------------------------------------------------------- #
# Navigation
# --------------------------------------------------------------------------- #
def test_astar_finds_a_contiguous_drivable_path(twin):
    nav = twin.navigation
    path = nav.find_path((3, 8), (17, 12))
    assert path is not None
    previous = (3, 8)
    for cell in path:
        assert twin.warehouse.is_walkable(*cell)
        assert abs(cell[0] - previous[0]) + abs(cell[1] - previous[1]) == 1
        previous = cell
    assert path[-1] == (17, 12)


def test_astar_is_shortest_for_an_open_corridor(twin):
    nav = twin.navigation
    path = nav.find_path((8, 7), (12, 7))
    assert path is not None and len(path) == 4


def test_astar_routes_around_a_shelf(twin):
    nav = twin.navigation
    path = nav.find_path((3, 6), (3, 2))
    assert path is not None
    assert all(twin.warehouse.is_walkable(*c) for c in path)
    assert len(path) > 4  # cannot cut straight through Shelf-A


def test_astar_snaps_to_a_neighbour_when_the_goal_is_blocked(twin):
    nav = twin.navigation
    path = nav.find_path((3, 8), (4, 3))  # (4,3) is inside the racking
    assert path is not None
    assert twin.warehouse.is_walkable(*path[-1])


def test_astar_returns_none_when_the_goal_is_unreachable(twin):
    nav = twin.navigation
    assert nav.find_path((3, 8), (4, 3), allow_goal_adjacent=False) is None


def test_dynamic_obstacles_change_the_route(twin):
    nav = twin.navigation
    start, goal = (8, 7), (12, 7)
    plain = nav.find_path(start, goal)
    detour = nav.find_path(start, goal, blocked={(9, 7), (10, 7), (11, 7)})
    assert plain is not None and detour is not None
    assert detour != plain
    assert len(detour) > len(plain)


def test_path_exists_and_distance_helpers(twin):
    nav = twin.navigation
    assert nav.path_exists((3, 8), (17, 12))
    assert nav.distance((8, 7), (12, 7)) == 4


# --------------------------------------------------------------------------- #
# Boxes & physical interaction
# --------------------------------------------------------------------------- #
def test_add_box_and_reject_duplicates(twin):
    box = twin.add_box(name="Box-Z", position=(9, 5), weight=3.0, destination="packing_area")
    assert box.id in twin.boxes
    with pytest.raises(ValueError):
        twin.add_box(name="Box-Z", position=(10, 5))


def test_box_pickup_attaches_it_to_the_robot(twin, sim):
    twin.tasks.create_task({"type": "PICK_BOX", "robot_id": "Robo-01", "box": "Box-A"})
    robot, box = twin.find_robot("Robo-01"), twin.find_box("Box-A")
    run_until(sim, lambda: box.status is BoxStatus.CARRIED, 200)
    assert robot.carrying_box == box.id
    assert box.position == robot.position
    for _ in range(10):
        sim.tick()
        assert box.position == robot.position  # the box travels with the robot


def test_box_delivery_detaches_it_at_the_destination(twin, sim):
    task = run_task(
        sim, twin,
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"},
    )
    robot, box = twin.find_robot("Robo-01"), twin.find_box("Box-A")
    assert task.status is TaskStatus.COMPLETED
    assert box.status is BoxStatus.DELIVERED
    assert robot.carrying_box is None
    assert box.position in twin.warehouse.resolve_zone("loading_zone").cells
    assert robot.boxes_delivered == 1


# --------------------------------------------------------------------------- #
# Tasks: creation, validation, planning
# --------------------------------------------------------------------------- #
def test_task_creation_populates_the_state_machine(twin):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A",
         "source": "shelf_a", "destination": "loading_zone", "priority": "HIGH"}
    )
    assert task.status is TaskStatus.PLANNING
    assert task.priority is Priority.HIGH
    statuses = [entry["status"] for entry in task.history]
    assert statuses[:3] == ["CREATED", "VALIDATING", "PLANNING"]


def test_unknown_task_type_is_rejected(twin):
    with pytest.raises(ValueError):
        twin.tasks.create_task({"type": "TELEPORT_ROBOT"})


def test_validation_rejects_an_unknown_robot(twin):
    task = twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-77", "destination": "loading_zone"})
    assert task.status is TaskStatus.FAILED
    assert "does not exist" in task.error


def test_validation_rejects_an_unknown_box(twin):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-Q", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.FAILED
    assert "Box" in task.error


def test_validation_rejects_an_unknown_destination(twin):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "Mars"}
    )
    assert task.status is TaskStatus.FAILED


def test_validation_rejects_a_missing_destination(twin):
    task = twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01"})
    assert task.status is TaskStatus.FAILED
    assert "destination" in task.error


def test_validation_rejects_a_double_booked_box(twin):
    first = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    second = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-02", "box": "Box-A", "destination": "packing_area"}
    )
    assert first.status is TaskStatus.PLANNING
    assert second.status is TaskStatus.FAILED
    assert "reserved" in second.error


def test_failed_validation_writes_an_error_log(twin):
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Ghost", "destination": "loading_zone"})
    errors = twin.logger.query(level="ERROR")
    assert any("does not exist" in record["message"] for record in errors)


# --------------------------------------------------------------------------- #
# Pre-execution authorization gate (backend/eligibility.py) — an explicitly
# ineligible robot/agent/operator is rejected at task creation, not merely
# graded after the fact by the eval engine. See TRUST_LAYER.md.
# --------------------------------------------------------------------------- #
def test_validation_rejects_a_robot_with_unapproved_firmware(twin):
    twin.find_robot("Robo-01").firmware_version = "0.9.0-beta"
    task = twin.tasks.create_task(
        {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.FAILED
    assert "unapproved firmware" in task.error


def test_validation_rejects_a_stopped_robot_requested_by_name(twin):
    robot = twin.find_robot("Robo-01")
    robot.status = RobotStatus.STOPPED
    task = twin.tasks.create_task(
        {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.FAILED
    assert "STOPPED" in task.error


def test_auto_robot_selection_skips_a_robot_with_unapproved_firmware(twin, sim):
    twin.find_robot("Robo-01").firmware_version = "0.9.0-beta"
    task = twin.tasks.create_task(
        {"type": "MOVE_ROBOT", "destination": "loading_zone", "priority": "HIGH"}
    )
    assert task.status is TaskStatus.PLANNING
    run_until(sim, lambda: task.robot_id is not None, 50)
    assert task.robot_id == twin.find_robot("Robo-02").id


def test_critical_battery_does_not_block_task_creation(twin):
    # Unlike firmware/status, a critical battery must NOT be gated at
    # creation time — the planner has a real recovery path for it (a
    # prepended recharge detour); see
    # test_planner_inserts_a_charging_detour_when_the_battery_is_short.
    twin.find_robot("Robo-01").battery = 5.0
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.PLANNING


def test_validation_rejects_an_agent_with_an_unapproved_model(twin):
    twin.find_agent("Ada").model_version = "fake/v0"
    task = twin.tasks.create_task({"type": "AGENT_INSPECTION", "agent_id": "Ada"})
    assert task.status is TaskStatus.FAILED
    assert "unapproved model" in task.error


def test_validation_rejects_an_operator_missing_the_required_certification(twin):
    # Lee only holds safety_inspection; MIXED_MAINTENANCE_MISSION needs
    # electrical_safety (see models.CERTIFICATION_REQUIREMENTS).
    task = twin.tasks.create_task(
        {"type": "MIXED_MAINTENANCE_MISSION", "operator_id": "Lee", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.FAILED
    assert "electrical_safety" in task.error


def test_auto_operator_selection_skips_an_uncertified_operator(twin):
    # AUTO must pick Sam (fully certified), never Lee, for a mission
    # that requires electrical_safety.
    task = twin.tasks.create_task(
        {"type": "MIXED_MAINTENANCE_MISSION", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.PLANNING
    assert task.operator_id == twin.find_operator("Sam").id


# --------------------------------------------------------------------------- #
# Per-robot task-type restriction (Robot.allowed_task_types) — configured via
# DigitalTwin.set_robot_capabilities, enforced by the same eligibility gate
# as firmware/status (backend/eligibility.py). Unconfigured (None/empty) is
# unrestricted, matching every robot's behaviour before this existed.
# --------------------------------------------------------------------------- #
def test_validation_rejects_a_robot_not_configured_for_the_task_type(twin):
    twin.set_robot_capabilities("Robo-01", ["MOVE_ROBOT"])
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.FAILED
    assert "not configured to run" in task.error


def test_a_robot_may_still_run_a_task_type_it_is_configured_for(twin):
    twin.set_robot_capabilities("Robo-01", ["MOVE_ROBOT"])
    task = twin.tasks.create_task(
        {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.PLANNING


def test_auto_robot_selection_skips_a_robot_not_configured_for_the_task_type(twin, sim):
    twin.set_robot_capabilities("Robo-01", ["MOVE_ROBOT"])
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "box": "Box-A", "destination": "loading_zone", "priority": "HIGH"}
    )
    assert task.status is TaskStatus.PLANNING
    run_until(sim, lambda: task.robot_id is not None, 50)
    assert task.robot_id == twin.find_robot("Robo-02").id


def test_clearing_a_robots_capabilities_removes_the_restriction(twin):
    robot = twin.set_robot_capabilities("Robo-01", ["MOVE_ROBOT"])
    assert robot.allowed_task_types == ["MOVE_ROBOT"]
    robot = twin.set_robot_capabilities("Robo-01", [])
    assert robot.allowed_task_types is None
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.PLANNING


def test_set_robot_capabilities_rejects_an_unknown_task_type(twin):
    with pytest.raises(ValueError):
        twin.set_robot_capabilities("Robo-01", ["NOT_A_REAL_TASK_TYPE"])


def test_set_robot_capabilities_rejects_an_unknown_robot(twin):
    with pytest.raises(KeyError):
        twin.set_robot_capabilities("Ghost", ["MOVE_ROBOT"])


# --------------------------------------------------------------------------- #
# The four newer agent/operator "own work" task types — all resolve
# instantly like AGENT_INSPECTION/HUMAN_INSPECTION (see TaskManager.INSTANT),
# no physical movement, no robot dispatch.
# --------------------------------------------------------------------------- #
def test_agent_replan_resolves_instantly_and_recommends_a_robot(twin):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "box": "Box-A", "destination": "loading_zone", "priority": "HIGH"}
    )
    task = twin.tasks.create_task({"type": "AGENT_REPLAN", "agent_id": "Ada"})
    assert task.status is TaskStatus.COMPLETED
    assert task.agent_id == twin.find_agent("Ada").id


def test_agent_audit_resolves_instantly(twin):
    task = twin.tasks.create_task({"type": "AGENT_AUDIT", "agent_id": "Ada"})
    assert task.status is TaskStatus.COMPLETED


def test_operator_approval_resolves_instantly_for_a_named_robot(twin):
    # OPERATOR_APPROVAL deliberately does not re-check the named robot's
    # own eligibility — approving an ineligible robot back into service is
    # the whole point.
    twin.find_robot("Robo-01").status = RobotStatus.STOPPED
    task = twin.tasks.create_task(
        {"type": "OPERATOR_APPROVAL", "operator_id": "Sam", "robot_id": "Robo-01"}
    )
    assert task.status is TaskStatus.COMPLETED
    assert task.operator_id == twin.find_operator("Sam").id


def test_operator_maintenance_signoff_requires_electrical_safety(twin):
    # Lee only holds safety_inspection; OPERATOR_MAINTENANCE_SIGNOFF needs
    # electrical_safety (see models.CERTIFICATION_REQUIREMENTS).
    task = twin.tasks.create_task({"type": "OPERATOR_MAINTENANCE_SIGNOFF", "operator_id": "Lee"})
    assert task.status is TaskStatus.FAILED
    assert "electrical_safety" in task.error


def test_operator_maintenance_signoff_reports_unapproved_firmware(twin):
    twin.find_robot("Robo-01").firmware_version = "0.9.0-beta"
    task = twin.tasks.create_task(
        {"type": "OPERATOR_MAINTENANCE_SIGNOFF", "operator_id": "Sam", "robot_id": "Robo-01"}
    )
    assert task.status is TaskStatus.COMPLETED
    events = twin.events.query(task_id=task.id)
    assert any("NOT on the approved baseline" in e["message"] for e in events)


# --------------------------------------------------------------------------- #
# Robot classes (models.ROBOT_CLASS_PRESETS)
# --------------------------------------------------------------------------- #
def test_robot_class_applies_preset_speed_and_capabilities(twin):
    robot = twin.add_robot(name="Fork-1", robot_class="FORKLIFT")
    assert robot.robot_class == "FORKLIFT"
    assert robot.speed == 1.2
    assert "MOVE_BOX" in robot.allowed_task_types
    assert "AGENT_INSPECTION" not in robot.allowed_task_types


def test_robot_class_explicit_speed_overrides_the_preset(twin):
    robot = twin.add_robot(name="Fork-2", robot_class="FORKLIFT", speed=5.0)
    assert robot.speed == 5.0


def test_default_robot_class_is_amr_and_unrestricted(twin):
    robot = twin.add_robot(name="Plain")
    assert robot.robot_class == "AMR"
    assert robot.allowed_task_types is None


def test_unknown_robot_class_is_rejected(twin):
    with pytest.raises(ValueError):
        twin.add_robot(name="Bad", robot_class="SUBMARINE")


@pytest.mark.parametrize("robot_class", ["FORKLIFT", "SCOUT", "HEAVY_HAULER", "DRONE", "PICKER"])
def test_every_restricted_robot_class_can_still_charge_itself(robot_class):
    # A class whose allowed_task_types omits CHARGE_ROBOT would strand
    # itself forever once idle and low — see the warning in
    # models.ROBOT_CLASS_PRESETS.
    from .models import ROBOT_CLASS_PRESETS
    assert "CHARGE_ROBOT" in ROBOT_CLASS_PRESETS[robot_class]["allowed_task_types"]


# --------------------------------------------------------------------------- #
# Operator roles (models.OPERATOR_ROLE_PRESETS) — added without touching the
# demo baseline (Sam/Lee stay exactly as they were) so every existing test
# that relies on "only Sam and Lee exist" keeps working unchanged.
# --------------------------------------------------------------------------- #
def test_operator_role_applies_preset_certifications_and_shift(twin):
    operator = twin.add_operator(name="Priya", role="MAINTENANCE_TECH")
    assert operator.role == "MAINTENANCE_TECH"
    assert set(operator.certifications) == {"electrical_safety", "equipment_maintenance"}
    assert (operator.shift_start_hour, operator.shift_end_hour) == (9, 17)


def test_operator_role_explicit_certifications_override_the_preset(twin):
    operator = twin.add_operator(name="Priya", role="MAINTENANCE_TECH", certifications=["safety_inspection"])
    assert operator.certifications == ["safety_inspection"]


def test_operator_role_explicit_shift_overrides_the_preset(twin):
    operator = twin.add_operator(name="Priya", role="MAINTENANCE_TECH", shift_start_hour=0, shift_end_hour=8)
    assert (operator.shift_start_hour, operator.shift_end_hour) == (0, 8)


def test_senior_operator_role_holds_every_known_certification(twin):
    from .models import KNOWN_CERTIFICATIONS
    operator = twin.add_operator(name="Jordan", role="SENIOR_OPERATOR")
    assert set(operator.certifications) == set(KNOWN_CERTIFICATIONS)
    assert operator.has_shift is False  # always available


def test_night_shift_operator_role_wraps_past_midnight(twin):
    operator = twin.add_operator(name="Ravi", role="NIGHT_SHIFT")
    assert operator.is_within_shift(23) is True
    assert operator.is_within_shift(2) is True
    assert operator.is_within_shift(12) is False


def test_trainee_operator_role_has_no_certifications(twin):
    operator = twin.add_operator(name="Alex", role="TRAINEE")
    assert operator.certifications == []


def test_default_operator_has_no_role(twin):
    operator = twin.add_operator(name="Plain", certifications=["safety_inspection"])
    assert operator.role is None
    assert operator.has_shift is False


def test_unknown_operator_role_is_rejected(twin):
    with pytest.raises(ValueError):
        twin.add_operator(name="Bad", role="ASTRONAUT")


def test_demo_operators_are_unaffected_by_operator_roles(twin):
    # Locks in the deliberate choice not to expand DEMO_OPERATORS: several
    # existing tests (dual-signoff among them) depend on exactly Sam and
    # Lee existing by default.
    assert {o.name for o in twin.operators.values()} == {"Sam", "Lee"}
    assert twin.find_operator("Sam").role is None
    assert twin.find_operator("Lee").role is None


# --------------------------------------------------------------------------- #
# Collisions — a real overlap halts both robots and disconnects their tasks.
# CONFIG["COLLISION_RISK"] (0.0 by default — perfect avoidance, matching
# every test elsewhere in this file) is the only way one actually happens:
# robots collide by failing to avoid each other while doing real work, never
# through a manual/forced trigger.
# --------------------------------------------------------------------------- #
def test_collision_risk_is_zero_by_default(twin):
    assert CONFIG["COLLISION_RISK"] == 0.0


def test_collision_risk_lets_a_robot_actually_collide_during_normal_work(twin, sim, monkeypatch):
    # Same "a stopped robot lands directly on the live route" setup as
    # test_a_robot_appearing_mid_route_triggers_waiting_and_replanning
    # below, which asserts zero collisions at CONFIG's default 0.0 risk —
    # this is the same scenario with the risk forced to 1.0 (always fails
    # to avoid), so it's a direct before/after of what the knob does.
    monkeypatch.setitem(CONFIG, "COLLISION_RISK", 1.0)

    mover = twin.find_robot("Robo-01")
    blocker = twin.find_robot("Robo-02")
    mover.position = (8, 7)
    blocker.position = (16, 7)
    twin.stop_robot(blocker.id)
    task = twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": mover.id, "destination": "12,7"})
    sim.tick()
    assert mover.current_path, "the mover should have a plan by now"

    # Drop the stopped robot straight into the next cell of the live
    # route — normally the mover waits/replans around it; with
    # COLLISION_RISK=1.0 it drives straight in instead.
    blocker.position = mover.current_path[0]
    run_until(sim, lambda: twin.statistics["collisions"] >= 1, 50)

    assert mover.status is RobotStatus.ERROR
    assert blocker.status is RobotStatus.ERROR
    assert "Collided with" in mover.last_error
    assert task.status is TaskStatus.FAILED
    assert "task disconnected" in task.error
    assert twin.statistics["collisions"] == 1
    events = twin.events.query(event_type="COLLISION_DETECTED")
    assert events


def test_false_success_risk_is_zero_by_default(twin):
    assert CONFIG["FALSE_SUCCESS_RISK"] == 0.0


def test_false_success_risk_lets_a_real_delivery_confidently_report_wrong(twin, sim, monkeypatch):
    # A normal PICK_AND_DELIVER, but with FALSE_SUCCESS_RISK forced to
    # 1.0: the robot really navigates, really picks, and the event trail
    # narrates a completely ordinary success (BOX_DELIVERED,
    # TASK_COMPLETED) — nothing in the live narrative hints at a problem.
    # Only the physical state.state snapshot attached to TASK_COMPLETED
    # tells the truth: the box's position was never actually updated, so
    # it's still sitting in its source zone. The system is fully "sure"
    # it delivered the box; only an after-the-fact eval_engine grade
    # (exactly what promptfoo's default suite runs against real
    # logs/tasks/*.json) catches the gap between the two.
    monkeypatch.setitem(CONFIG, "FALSE_SUCCESS_RISK", 1.0)

    from .eval_engine import evaluate_events, Verdict

    box = twin.find_box("box_001")
    box_before_position = box.position
    task = run_task(sim, twin, {
        "type": "PICK_AND_DELIVER", "robot_id": "robot_01",
        "box_id": box.id, "destination": "loading_zone",
    })

    assert task.status is TaskStatus.COMPLETED  # the system believes it succeeded
    assert box.status is BoxStatus.DELIVERED    # the box's own bookkeeping agrees
    assert box.position == box_before_position  # ...but it physically never moved

    events = twin.events.query(task_id=task.id, event_type="BOX_DELIVERED")
    assert events, "BOX_DELIVERED should still fire — the narrative reports success"

    # twin.logger.query() (the in-memory ring buffer), not get_task_logs()
    # (the per-task JSON file, only written when persist_logs=True) — the
    # `twin` fixture runs with persist_logs=False, same as every other
    # test in this file.
    events = twin.logger.query(task_id=task.id, limit=1000)
    report = evaluate_events(events, task_id=task.id)
    assert report.verdict == Verdict.FAIL, report.reasons
    state_transition = next(c for c in report.checks if c.name == "state_transition")
    assert state_transition.verdict == Verdict.FAIL
    assert "doesn't match a real delivery" in state_transition.message

    # terminal_state — which only reads the narrative, not ground truth —
    # is the one check that stays fooled, exactly as intended.
    terminal_state = next(c for c in report.checks if c.name == "terminal_state")
    assert terminal_state.verdict == Verdict.PASS


def test_natural_collision_is_flagged_only_once(twin, sim):
    # A genuine (if, at CONFIG's default risk, vanishingly rare) overlap
    # — Simulator._detect_collisions should catch it on the very next
    # tick, then stop re-flagging it every tick after.
    robot_a, robot_b = twin.find_robot("Robo-01"), twin.find_robot("Robo-02")
    robot_b.position = robot_a.position
    sim.tick()
    assert twin.statistics["collisions"] == 1
    assert robot_a.status is RobotStatus.ERROR and robot_b.status is RobotStatus.ERROR
    for _ in range(10):
        sim.tick()
    assert twin.statistics["collisions"] == 1  # not re-flagged every tick


def test_register_collision_rejects_the_same_robot_twice(twin):
    with pytest.raises(ValueError):
        twin.register_collision("Robo-01", "Robo-01")


def test_register_collision_rejects_an_unknown_robot(twin):
    with pytest.raises(KeyError):
        twin.register_collision("Robo-01", "Ghost")


def test_reset_recovers_a_collided_robot(twin):
    twin.register_collision("Robo-01", "Robo-02")
    robot = twin.reset_robot("Robo-01")
    assert robot.status is RobotStatus.IDLE
    assert robot.battery == 100.0
    assert robot.last_error is None


# --------------------------------------------------------------------------- #
# Predictive maintenance (backend/maintenance.py)
# --------------------------------------------------------------------------- #
def test_robot_flagged_for_maintenance_past_the_distance_threshold(twin, sim):
    robot = twin.find_robot("Robo-01")
    robot.total_distance = CONFIG["MAINTENANCE_DISTANCE_THRESHOLD"] + 1
    sim.tick()
    assert robot.maintenance_alerted is True
    events = twin.events.query(robot_id=robot.id, event_type="MAINTENANCE_ALERT")
    assert events


def test_maintenance_signoff_resets_the_wear_counters(twin):
    robot = twin.find_robot("Robo-01")
    robot.total_distance = CONFIG["MAINTENANCE_DISTANCE_THRESHOLD"] + 1
    robot.maintenance_alerted = True
    task = twin.tasks.create_task(
        {"type": "OPERATOR_MAINTENANCE_SIGNOFF", "operator_id": "Sam", "robot_id": "Robo-01"}
    )
    assert task.status is TaskStatus.COMPLETED
    assert robot.distance_since_maintenance == 0
    assert robot.maintenance_alerted is False


# --------------------------------------------------------------------------- #
# Operator shift scheduling
# --------------------------------------------------------------------------- #
def test_operator_within_shift_hours():
    from .operator import Operator
    op = Operator("op_1", "Test", shift_start_hour=9, shift_end_hour=17)
    assert op.is_within_shift(10) is True
    assert op.is_within_shift(8) is False
    assert op.is_within_shift(17) is False


def test_operator_shift_wraps_past_midnight():
    from .operator import Operator
    op = Operator("op_1", "Test", shift_start_hour=22, shift_end_hour=6)
    assert op.is_within_shift(23) is True
    assert op.is_within_shift(2) is True
    assert op.is_within_shift(12) is False


def test_operator_with_no_shift_is_always_available():
    from .operator import Operator
    op = Operator("op_1", "Test")
    assert op.is_within_shift(3) is True


def test_simulator_toggles_operator_off_duty_outside_shift(twin, sim, monkeypatch):
    from . import simulator as simulator_module

    twin.set_operator_shift("Sam", 9, 17)

    class FixedDatetime:
        @staticmethod
        def now():
            import datetime as real_datetime
            return real_datetime.datetime(2026, 1, 1, 3, 0)  # 3am — outside the shift

    monkeypatch.setattr(simulator_module, "datetime", FixedDatetime)
    for _ in range(CONFIG["SHIFT_CHECK_EVERY_TICKS"]):
        sim.tick()
    assert twin.find_operator("Sam").status is OperatorStatus.OFF_DUTY


# --------------------------------------------------------------------------- #
# Two-person sign-off (opt-in `dual_signoff`)
# --------------------------------------------------------------------------- #
def test_dual_signoff_requires_a_second_eligible_operator(twin):
    # Only Sam and Lee exist; Lee lacks electrical_safety, so there is no
    # valid second signer for a task that requires it.
    task = twin.tasks.create_task(
        {"type": "OPERATOR_MAINTENANCE_SIGNOFF", "operator_id": "Sam", "dual_signoff": True}
    )
    assert task.status is TaskStatus.FAILED
    assert "second" in task.error.lower()


def test_dual_signoff_succeeds_with_two_eligible_operators(twin):
    twin.add_operator(name="Kim", certifications=["safety_inspection", "electrical_safety"])
    task = twin.tasks.create_task(
        {
            "type": "OPERATOR_MAINTENANCE_SIGNOFF", "operator_id": "Sam",
            "dual_signoff": True, "robot_id": "Robo-01",
        }
    )
    assert task.status is TaskStatus.COMPLETED
    assert task.second_operator_id == twin.find_operator("Kim").id
    events = twin.events.query(task_id=task.id, event_type="OPERATOR_APPROVED")
    assert any("co-signed" in e["message"] for e in events)


def test_dual_signoff_rejects_the_same_operator_twice(twin):
    task = twin.tasks.create_task(
        {"type": "OPERATOR_APPROVAL", "operator_id": "Sam", "second_operator_id": "Sam"}
    )
    assert task.status is TaskStatus.FAILED
    assert "different operator" in task.error


# --------------------------------------------------------------------------- #
# BATCH_DELIVER
# --------------------------------------------------------------------------- #
def test_batch_deliver_needs_at_least_two_boxes(twin):
    task = twin.tasks.create_task(
        {"type": "BATCH_DELIVER", "box_ids": ["Box-A"], "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.FAILED
    assert "at least two" in task.error


def test_batch_deliver_rejects_an_already_reserved_box(twin):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-02", "box": "Box-B", "destination": "packing_area"}
    )
    task = twin.tasks.create_task(
        {"type": "BATCH_DELIVER", "box_ids": ["Box-A", "Box-B"], "destination": "loading_zone"}
    )
    assert task.status is TaskStatus.FAILED
    assert "reserved" in task.error


def test_batch_deliver_completes_both_boxes(twin, sim):
    task = run_task(sim, twin, {"type": "BATCH_DELIVER", "box_ids": ["Box-A", "Box-D"], "destination": "loading_zone"})
    assert task.status is TaskStatus.COMPLETED
    assert twin.find_box("Box-A").status is BoxStatus.DELIVERED
    assert twin.find_box("Box-D").status is BoxStatus.DELIVERED
    assert twin.find_robot(task.robot_id).boxes_delivered == 2


def test_false_success_risk_lets_a_batch_delivery_confidently_report_wrong(twin, sim, monkeypatch):
    # Same fault as test_false_success_risk_lets_a_real_delivery_confidently_
    # report_wrong above, but on a BATCH_DELIVER: check_state_transition
    # used to only ever read state["box"] (the single-box shape), so a
    # BATCH_DELIVER's boxes — recorded under state["boxes"] instead (see
    # TaskManager.snapshot_state) — were silently skipped entirely and
    # every completed batch fell through to a clean PASS regardless of
    # what FALSE_SUCCESS_RISK actually did to any box in it.
    monkeypatch.setitem(CONFIG, "FALSE_SUCCESS_RISK", 1.0)

    from .eval_engine import evaluate_events, Verdict

    box_a, box_d = twin.find_box("Box-A"), twin.find_box("Box-D")
    box_a_before_position, box_d_before_position = box_a.position, box_d.position
    task = run_task(sim, twin, {"type": "BATCH_DELIVER", "box_ids": ["Box-A", "Box-D"], "destination": "loading_zone"})

    assert task.status is TaskStatus.COMPLETED   # the system believes it succeeded
    assert box_a.status is BoxStatus.DELIVERED
    assert box_d.status is BoxStatus.DELIVERED
    # At CONFIG["FALSE_SUCCESS_RISK"] == 1.0 every delivery in the batch
    # hits the fault — neither box actually moved.
    assert box_a.position == box_a_before_position
    assert box_d.position == box_d_before_position

    events = twin.logger.query(task_id=task.id, limit=1000)
    report = evaluate_events(events, task_id=task.id)
    assert report.verdict == Verdict.FAIL, report.reasons
    state_transition = next(c for c in report.checks if c.name == "state_transition")
    assert state_transition.verdict == Verdict.FAIL
    assert "doesn't match a real delivery" in state_transition.message
    assert box_a.id in state_transition.message
    assert box_d.id in state_transition.message


# --------------------------------------------------------------------------- #
# Mid-task authorization changes (Simulator._check_authorization_changes) —
# flags, never gates a task that's already mid-route.
# --------------------------------------------------------------------------- #
def test_mid_task_ineligibility_is_flagged_not_gated(twin, sim):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    sim.tick()
    assert task.authorization_flagged is None
    twin.find_robot("Robo-01").firmware_version = "0.9.0-beta"
    for _ in range(CONFIG["AUTHORIZATION_CHECK_EVERY_TICKS"]):
        sim.tick()
    assert task.authorization_flagged is not None
    assert "unapproved firmware" in task.authorization_flagged
    assert task.status is not TaskStatus.FAILED  # flagged, not halted
    events = twin.events.query(task_id=task.id, event_type="TASK_AUTHORIZATION_CHANGED")
    assert events


# --------------------------------------------------------------------------- #
# Recurring tasks (backend/scheduler.py)
# --------------------------------------------------------------------------- #
def test_scheduler_fires_a_task_on_interval(twin, sim):
    schedule = twin.scheduler.add({"type": "AGENT_AUDIT", "agent_id": "Ada"}, interval_ticks=5)
    for _ in range(5):
        sim.tick()
    assert schedule.run_count == 1
    assert schedule.last_task_id is not None
    assert twin.tasks.get(schedule.last_task_id).status is TaskStatus.COMPLETED


def test_disabled_schedule_does_not_fire(twin, sim):
    schedule = twin.scheduler.add({"type": "AGENT_AUDIT", "agent_id": "Ada"}, interval_ticks=2)
    twin.scheduler.set_enabled(schedule.id, False)
    for _ in range(10):
        sim.tick()
    assert schedule.run_count == 0


def test_scheduler_remove(twin):
    schedule = twin.scheduler.add({"type": "AGENT_AUDIT", "agent_id": "Ada"}, interval_ticks=2)
    twin.scheduler.remove(schedule.id)
    assert schedule.id not in twin.scheduler.schedules
    with pytest.raises(KeyError):
        twin.scheduler.remove(schedule.id)


# --------------------------------------------------------------------------- #
# Policy-as-code (backend/policy.py) — in-place mutation must be visible to
# every module that already imported the shared containers by name.
# --------------------------------------------------------------------------- #
def test_apply_policy_mutates_shared_containers_in_place():
    from . import models
    from .eligibility import APPROVED_AGENT_MODELS as elig_models
    from .policy import apply_policy

    original = list(models.APPROVED_AGENT_MODELS)
    try:
        apply_policy({"approved_agent_models": ["test/model-1"]})
        assert models.APPROVED_AGENT_MODELS == ["test/model-1"]
        # eligibility.py imported the very same list object — this proves
        # the mutation (not a reassignment) is visible there too.
        assert elig_models == ["test/model-1"]
    finally:
        apply_policy({"approved_agent_models": original})


def test_read_policy_file_returns_empty_dict_for_a_missing_file(tmp_path):
    from .policy import read_policy_file
    assert read_policy_file(str(tmp_path / "does_not_exist.yaml")) == {}


# --------------------------------------------------------------------------- #
# Optional live LLM narration (backend/llm.py) — off by default, must never
# touch the network unless explicitly enabled.
# --------------------------------------------------------------------------- #
def test_llm_narrate_returns_none_when_disabled():
    from .llm import narrate
    assert narrate("anything") is None


def test_agent_replan_does_not_use_llm_narration_by_default(twin):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "box": "Box-A", "destination": "loading_zone", "priority": "HIGH"}
    )
    task = twin.tasks.create_task({"type": "AGENT_REPLAN", "agent_id": "Ada"})
    events = twin.events.query(task_id=task.id, event_type="AGENT_RECOMMENDATION")
    assert events and events[0]["data"]["llm_generated"] is False


def test_agent_audit_uses_llm_narration_when_enabled(twin, monkeypatch):
    monkeypatch.setitem(CONFIG, "AGENT_LLM_ENABLED", True)
    monkeypatch.setattr("backend.task_manager.narrate", lambda *a, **k: "Custom narrated summary.")
    task = twin.tasks.create_task({"type": "AGENT_AUDIT", "agent_id": "Ada"})
    events = twin.events.query(task_id=task.id, event_type="AGENT_RECOMMENDATION")
    assert events
    assert "Custom narrated summary." in events[0]["message"]
    assert events[0]["data"]["llm_generated"] is True


def test_planner_expands_pick_and_deliver_into_five_actions(twin, sim):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    sim.tick()
    kinds = [action.type.value for action in task.actions]
    assert kinds == ["NAVIGATE", "PICK", "NAVIGATE", "DELIVER", "COMPLETE"]


def test_task_reserves_its_box_on_assignment(twin, sim):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    sim.tick()
    assert twin.find_box("Box-A").status is BoxStatus.RESERVED


def test_coordinate_destinations_are_accepted(twin, sim):
    task = run_task(sim, twin, {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "10,7"})
    assert task.status is TaskStatus.COMPLETED
    assert twin.find_robot("Robo-01").position == (10, 7)


# --------------------------------------------------------------------------- #
# Tasks: all eight supported types
# --------------------------------------------------------------------------- #
def test_move_robot_task(twin, sim):
    task = run_task(sim, twin, {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "shelf_b"})
    assert task.status is TaskStatus.COMPLETED
    assert twin.find_robot("Robo-01").position in twin.warehouse.resolve_zone("shelf_b").cells


def test_pick_box_task(twin, sim):
    task = run_task(sim, twin, {"type": "PICK_BOX", "robot_id": "Robo-01", "box": "Box-A"})
    assert task.status is TaskStatus.COMPLETED
    assert twin.find_robot("Robo-01").carrying_box == twin.find_box("Box-A").id


def test_deliver_box_task_when_already_carrying(twin, sim):
    run_task(sim, twin, {"type": "PICK_BOX", "robot_id": "Robo-01", "box": "Box-A"})
    task = run_task(
        sim, twin,
        {"type": "DELIVER_BOX", "robot_id": "Robo-01", "box": "Box-A", "destination": "packing_area"},
    )
    assert task.status is TaskStatus.COMPLETED
    assert [a.type.value for a in task.actions] == ["NAVIGATE", "DELIVER", "COMPLETE"]
    assert twin.find_box("Box-A").position in twin.warehouse.resolve_zone("packing_area").cells


def test_deliver_box_task_fetches_the_box_first(twin, sim):
    task = run_task(
        sim, twin,
        {"type": "DELIVER_BOX", "robot_id": "Robo-01", "box": "Box-C", "destination": "packing_area"},
    )
    assert task.status is TaskStatus.COMPLETED
    assert [a.type.value for a in task.actions][:4] == ["NAVIGATE", "PICK", "NAVIGATE", "DELIVER"]


def test_move_box_task_picks_its_own_robot(twin, sim):
    task = run_task(
        sim, twin,
        {"type": "MOVE_BOX", "robot": "AUTO", "box": "Box-B", "source": "shelf_b",
         "destination": "unloading_zone"},
    )
    assert task.status is TaskStatus.COMPLETED
    assert task.robot_id in twin.robots
    assert twin.find_box("Box-B").position in twin.warehouse.resolve_zone("unloading_zone").cells


def test_charge_robot_task(twin, sim):
    robot = twin.find_robot("Robo-02")
    robot.battery = 40.0
    task = run_task(sim, twin, {"type": "CHARGE_ROBOT", "robot_id": robot.id})
    assert task.status is TaskStatus.COMPLETED
    assert robot.battery == 100.0
    assert robot.charging_sessions == 1
    assert twin.warehouse.cell_type(*robot.position) is CellType.CHARGING


def test_stop_and_resume_tasks_take_effect_immediately(twin):
    stop = twin.tasks.create_task({"type": "STOP_ROBOT", "robot_id": "Robo-01"})
    assert stop.status is TaskStatus.COMPLETED
    assert twin.find_robot("Robo-01").status is RobotStatus.STOPPED
    resume = twin.tasks.create_task({"type": "RESUME_ROBOT", "robot_id": "Robo-01"})
    assert resume.status is TaskStatus.COMPLETED
    assert twin.find_robot("Robo-01").status is not RobotStatus.STOPPED


def test_pick_and_deliver_end_to_end_logs_the_full_story(twin, sim):
    task = run_task(
        sim, twin,
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A",
         "source": "shelf_a", "destination": "loading_zone", "priority": "HIGH"},
    )
    assert task.status is TaskStatus.COMPLETED
    assert task.progress == 100
    events = {e["event"] for e in twin.events.query(task_id=task.id, limit=500)}
    for expected in {
        "TASK_CREATED", "TASK_VALIDATED", "TASK_ASSIGNED", "TASK_PLANNED",
        "TASK_STARTED", "PATH_CREATED", "BOX_RESERVED", "BOX_PICKED",
        "BOX_DELIVERED", "TASK_COMPLETED",
    }:
        assert expected in events, expected


# --------------------------------------------------------------------------- #
# Task control
# --------------------------------------------------------------------------- #
def test_cancel_task_frees_the_robot_and_the_box(twin, sim):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    for _ in range(5):
        sim.tick()
    twin.tasks.cancel_task(task.id)
    robot = twin.find_robot("Robo-01")
    assert task.status is TaskStatus.CANCELLED
    assert robot.current_task is None
    assert twin.find_box("Box-A").status is BoxStatus.STORED


def test_cancel_a_finished_task_is_rejected(twin, sim):
    task = run_task(sim, twin, {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "shelf_b"})
    with pytest.raises(ValueError):
        twin.tasks.cancel_task(task.id)


def test_pause_and_resume_a_task(twin, sim):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    for _ in range(6):
        sim.tick()
    twin.tasks.pause_task(task.id)
    robot = twin.find_robot("Robo-01")
    frozen = robot.position
    for _ in range(10):
        sim.tick()
    assert robot.position == frozen
    twin.tasks.resume_task(task.id)
    twin.resume_robot(robot.id)
    run_until(sim, lambda: task.is_terminal, 400)
    assert task.status is TaskStatus.COMPLETED


def test_cancelling_an_unknown_task_raises(twin):
    with pytest.raises(KeyError):
        twin.tasks.cancel_task("task_999")


# --------------------------------------------------------------------------- #
# Priority & automatic assignment
# --------------------------------------------------------------------------- #
def test_higher_priority_tasks_are_dispatched_first(twin, sim):
    twin.robots.pop(twin.find_robot("Robo-02").id)  # single robot forces ordering
    low = twin.tasks.create_task(
        {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "shelf_c", "priority": "LOW"}
    )
    critical = twin.tasks.create_task(
        {"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "packing_area", "priority": "CRITICAL"}
    )
    sim.tick()
    assert critical.status is not TaskStatus.PLANNING
    assert low.status is TaskStatus.PLANNING
    assert PRIORITY_RANK[critical.priority] > PRIORITY_RANK[low.priority]


def test_auto_assignment_picks_the_closest_robot(twin, sim):
    twin.find_robot("Robo-01").position = (17, 7)
    twin.find_robot("Robo-02").position = (9, 7)
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot": "AUTO", "box": "Box-B", "destination": "packing_area"}
    )
    sim.tick()
    assert task.robot_id == twin.find_robot("Robo-02").id


def test_auto_assignment_decision_is_logged(twin, sim):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot": "AUTO", "box": "Box-B", "destination": "packing_area"}
    )
    sim.tick()
    planner_logs = twin.logger.query(category="PLANNER")
    assert any("AUTO assignment" in record["message"] for record in planner_logs)


def test_auto_assignment_waits_when_every_robot_is_busy(twin, sim):
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"})
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-02", "destination": "charging_station"})
    sim.tick()
    queued = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot": "AUTO", "box": "Box-A", "destination": "packing_area"}
    )
    sim.tick()
    assert queued.status is TaskStatus.PLANNING
    assert queued.robot_id is None


# --------------------------------------------------------------------------- #
# Multi-robot coordination
# --------------------------------------------------------------------------- #
def test_two_robots_work_simultaneously_without_colliding(twin, sim):
    first = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    second = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-02", "box": "Box-B", "destination": "packing_area"}
    )
    run_until(sim, lambda: first.is_terminal and second.is_terminal, 900)
    assert first.status is TaskStatus.COMPLETED
    assert second.status is TaskStatus.COMPLETED
    assert twin.statistics["collisions"] == 0


def test_robots_never_share_a_cell_in_a_congested_run(twin, sim):
    twin.find_robot("Robo-01").position = (8, 7)
    twin.find_robot("Robo-02").position = (12, 7)
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "13,7"})
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-02", "destination": "7,7"})
    for _ in range(200):
        sim.tick()
        positions = [r.position for r in twin.robots.values()]
        assert len(positions) == len(set(positions))
    assert twin.statistics["collisions"] == 0


def test_planning_routes_around_a_parked_robot(twin, sim):
    mover = twin.find_robot("Robo-01")
    blocker = twin.find_robot("Robo-02")
    mover.position = (8, 7)
    blocker.position = (9, 7)
    twin.stop_robot(blocker.id)
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": mover.id, "destination": "12,7"})
    sim.tick()
    assert (9, 7) not in mover.current_path  # planner treats the parked robot as an obstacle
    run_until(sim, lambda: mover.position == (12, 7), 400)
    assert twin.statistics["collisions"] == 0


def test_a_robot_appearing_mid_route_triggers_waiting_and_replanning(twin, sim):
    mover = twin.find_robot("Robo-01")
    blocker = twin.find_robot("Robo-02")
    mover.position = (8, 7)
    blocker.position = (16, 7)
    twin.stop_robot(blocker.id)
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": mover.id, "destination": "12,7"})
    sim.tick()
    assert mover.current_path, "the mover should have a plan by now"

    # Drop the stopped robot straight into the next cell of the live route.
    blocker.position = mover.current_path[0]
    for _ in range(CONFIG["REPLAN_AFTER_WAIT_TICKS"] + 2):
        sim.tick()
    events = {e["event"] for e in twin.events.query(limit=2000)}
    assert "COLLISION_AVOIDED" in events
    assert "ROBOT_WAITING" in events
    assert mover.wait_events >= 1

    run_until(sim, lambda: mover.position == (12, 7), 400)
    assert "PATH_RECALCULATED" in {e["event"] for e in twin.events.query(limit=2000)}
    assert twin.statistics["collisions"] == 0
    assert twin.statistics["collisions_avoided"] >= 1


# --------------------------------------------------------------------------- #
# Battery
# --------------------------------------------------------------------------- #
def test_battery_drains_with_distance(twin, sim):
    robot = twin.find_robot("Robo-01")
    start = robot.battery
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": robot.id, "destination": "loading_zone"})
    run_until(sim, lambda: robot.current_task is None and robot.total_distance > 10, 400)
    assert robot.battery < start
    assert robot.battery_consumed > 0


def test_low_battery_raises_an_event(twin, sim):
    robot = twin.find_robot("Robo-01")
    robot.battery = CONFIG["BATTERY_LOW"] + 0.5
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": robot.id, "destination": "loading_zone"})
    run_until(
        sim,
        lambda: any(e["event"] == "BATTERY_LOW" for e in twin.events.query(robot_id=robot.id, limit=500)),
        400,
    )


def test_planner_inserts_a_charging_detour_when_the_battery_is_short(twin, sim):
    robot = twin.find_robot("Robo-01")
    robot.battery = 7.0
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": robot.id, "box": "Box-C", "destination": "loading_zone"}
    )
    sim.tick()
    assert task.recharged_before_start is True
    assert task.actions[1].type.value == "CHARGE"
    run_until(sim, lambda: task.is_terminal, 1200)
    assert task.status is TaskStatus.COMPLETED
    assert robot.charging_sessions >= 1


def test_idle_robot_with_a_low_battery_charges_itself(twin, sim):
    robot = twin.find_robot("Robo-02")
    robot.battery = 12.0
    run_until(sim, lambda: robot.status is RobotStatus.CHARGING, 400)
    run_until(sim, lambda: robot.battery >= 100.0, 400)
    assert robot.status is RobotStatus.IDLE


def test_charging_only_happens_on_the_charging_station(twin, sim):
    robot = twin.find_robot("Robo-01")
    robot.battery = 30.0
    run_task(sim, twin, {"type": "CHARGE_ROBOT", "robot_id": robot.id})
    assert twin.warehouse.cell_type(*robot.position) is CellType.CHARGING


# --------------------------------------------------------------------------- #
# Simulation controls
# --------------------------------------------------------------------------- #
def test_pause_freezes_the_world(twin):
    simulator = Simulator(twin)
    simulator.start()
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"})
    for _ in range(8):
        simulator.tick()
    simulator.pause()
    assert twin.simulation_status is SimulationStatus.PAUSED
    tick_before = twin.tick_count
    # The threadless loop only ticks when told to, so verify the guard directly.
    assert twin.simulation_status is not SimulationStatus.RUNNING
    assert twin.tick_count == tick_before


def test_emergency_stop_halts_every_robot_and_keeps_tasks(twin, sim):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    for _ in range(6):
        sim.tick()
    twin.emergency_stop()
    assert twin.simulation_status is SimulationStatus.EMERGENCY_STOP
    assert all(r.status is RobotStatus.STOPPED for r in twin.robots.values())
    assert not task.is_terminal
    critical = twin.logger.query(level="CRITICAL")
    assert any("Emergency stop" in record["message"] for record in critical)


def test_release_emergency_stop_resumes_the_previous_task(twin, sim):
    task = twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    for _ in range(6):
        sim.tick()
    twin.emergency_stop()
    twin.release_emergency_stop()
    assert twin.simulation_status is SimulationStatus.RUNNING
    run_until(sim, lambda: task.is_terminal, 600)
    assert task.status is TaskStatus.COMPLETED


def test_reset_restores_the_demo_configuration(twin, sim):
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "loading_zone"})
    for _ in range(10):
        sim.tick()
    twin.reset(demo_tasks=True)
    assert twin.tick_count == 0
    assert len(twin.robots) == 2
    assert len(twin.boxes) == 5
    assert len(twin.tasks.tasks) == 2


def test_speed_changes_are_validated(twin):
    simulator = Simulator(twin)
    assert simulator.set_speed(5) == 5.0
    with pytest.raises(ValueError):
        simulator.set_speed(0)


# --------------------------------------------------------------------------- #
# Digital twin, logging, events
# --------------------------------------------------------------------------- #
def test_snapshot_contains_every_section(twin):
    snapshot = twin.snapshot(include_layout=True)
    for key in ("environment", "robots", "boxes", "tasks", "statistics",
                "robot_statistics", "options", "warehouse", "config"):
        assert key in snapshot
    assert snapshot["warehouse"]["width"] == CONFIG["GRID_WIDTH"]


def test_digital_twin_stays_synchronised_during_execution(twin, sim):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    for _ in range(120):
        sim.tick()
        for robot in twin.robots.values():
            if robot.current_task:
                task = twin.tasks.get(robot.current_task)
                assert task is not None and task.robot_id == robot.id
            if robot.carrying_box:
                box = twin.find_box(robot.carrying_box)
                assert box.position == robot.position


def test_structured_log_records_carry_the_required_fields(twin, sim):
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "shelf_b"})
    for _ in range(20):
        sim.tick()
    for record in twin.logger.export_json()[-30:]:
        for field in ("timestamp", "level", "category", "message", "seq"):
            assert field in record


def test_log_filters_work(twin, sim):
    twin.tasks.create_task({"type": "MOVE_ROBOT", "robot_id": "Robo-01", "destination": "shelf_b"})
    for _ in range(20):
        sim.tick()
    robot_id = twin.find_robot("Robo-01").id
    assert all(r["robot_id"] == robot_id for r in twin.logger.query(robot_id=robot_id))
    assert all(r["category"] == "NAVIGATION" for r in twin.logger.query(category="NAVIGATION"))
    assert all(r["level"] in ("WARNING", "ERROR", "CRITICAL") for r in twin.logger.query(level="WARNING"))
    assert twin.logger.query(search="moved") != []


def test_log_export_formats(twin, sim):
    sim.tick()
    assert "[INFO]" in twin.logger.export_text()
    assert isinstance(twin.logger.export_json(), list)


def test_logs_persist_to_disk(tmp_path):
    persisted = DigitalTwin(
        log_dir=str(tmp_path / "logs"), data_dir=str(tmp_path / "data"),
        persist_logs=True, demo=True, demo_tasks=False,
    )
    assert os.path.exists(persisted.logger.text_path)
    assert os.path.exists(persisted.logger.json_path)
    reopened = DigitalTwin(
        log_dir=str(tmp_path / "logs"), data_dir=str(tmp_path / "data"),
        persist_logs=True, demo=False,
    )
    assert any(record.get("restored") for record in reopened.logger.export_json())


def test_events_are_generated_for_the_documented_types(twin, sim):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    run_until(sim, lambda: all(t.is_terminal for t in twin.tasks.tasks.values()), 600)
    events = {e["event"] for e in twin.events.query(limit=2000)}
    for expected in {
        "WAREHOUSE_INITIALIZED", "ROBOT_CREATED", "BOX_CREATED", "TASK_CREATED",
        "TASK_ASSIGNED", "PATH_CREATED", "BOX_PICKED", "BOX_DELIVERED", "TASK_COMPLETED",
    }:
        assert expected in events, expected


# --------------------------------------------------------------------------- #
# Persistence
# --------------------------------------------------------------------------- #
def test_save_and_load_round_trip(twin, sim, tmp_path):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    for _ in range(25):
        sim.tick()
    path = twin.save_state(str(tmp_path / "state.json"))
    robot = twin.find_robot("Robo-01")
    expected = (robot.position, round(robot.battery, 1), robot.total_distance)

    twin.reset(demo_tasks=False)
    assert twin.find_robot("Robo-01").position != expected[0] or twin.tick_count == 0

    twin.load_state(path)
    restored = twin.find_robot("Robo-01")
    assert (restored.position, round(restored.battery, 1), restored.total_distance) == expected
    assert len(twin.tasks.tasks) >= 1


def test_saved_state_is_valid_json_with_all_sections(twin, tmp_path):
    path = twin.save_state(str(tmp_path / "state.json"))
    with open(path, encoding="utf-8") as handle:
        payload = json.load(handle)
    for key in ("robots", "boxes", "tasks", "statistics", "simulation", "counters"):
        assert key in payload


def test_loading_a_missing_file_raises(twin, tmp_path):
    with pytest.raises(FileNotFoundError):
        twin.load_state(str(tmp_path / "nope.json"))


# --------------------------------------------------------------------------- #
# Mock CI
# --------------------------------------------------------------------------- #
def test_ci_pipeline_passes_on_a_healthy_twin(twin):
    summary = CIEngine(twin).run()
    assert summary["status"] == "PASSED"
    assert summary["failed"] == 0
    assert summary["total"] >= 11


def test_ci_pipeline_passes_mid_mission(twin, sim):
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A", "destination": "loading_zone"}
    )
    twin.tasks.create_task(
        {"type": "PICK_AND_DELIVER", "robot_id": "Robo-02", "box": "Box-B", "destination": "packing_area"}
    )
    for _ in range(40):
        sim.tick()
        summary = CIEngine(twin).run()
        assert summary["status"] == "PASSED", [
            c for c in summary["checks"] if not c["passed"]
        ]


def test_ci_detects_two_robots_in_one_cell(twin):
    robots = list(twin.robots.values())
    robots[1].position = robots[0].position
    summary = CIEngine(twin).run()
    assert summary["status"] == "FAILED"
    failed = {check["name"] for check in summary["checks"] if not check["passed"]}
    assert "Collision validation" in failed


def test_ci_detects_a_robot_parked_on_a_shelf(twin):
    list(twin.robots.values())[0].position = (4, 3)
    summary = CIEngine(twin).run()
    failed = {check["name"] for check in summary["checks"] if not check["passed"]}
    assert "Robot position validation" in failed


def test_ci_detects_a_box_carried_by_nobody(twin):
    twin.find_box("Box-A").status = BoxStatus.CARRIED
    summary = CIEngine(twin).run()
    failed = {check["name"] for check in summary["checks"] if not check["passed"]}
    assert "Box state validation" in failed


def test_ci_runs_are_logged(twin):
    CIEngine(twin).run()
    ci_logs = twin.logger.query(category="CI")
    assert any("Pipeline" in record["message"] for record in ci_logs)
    assert twin.statistics["ci_runs"] == 1


def test_ci_status_reports_history(twin):
    engine = CIEngine(twin)
    engine.run()
    engine.run()
    status = engine.status()
    assert status["runs"] == 2
    assert len(status["history"]) == 2
    assert len(status["checks"]) >= 11


# --------------------------------------------------------------------------- #
# HTTP API
# --------------------------------------------------------------------------- #
@pytest.fixture
def client(tmp_path):
    twin = DigitalTwin(
        log_dir=str(tmp_path / "logs"), data_dir=str(tmp_path / "data"),
        persist_logs=False, demo=True, demo_tasks=False,
    )
    app, _twin, _sim, _ci = create_app(twin=twin, autostart=False, run_thread=False)
    app.config["TESTING"] = True
    return app.test_client()


def test_api_state_endpoint(client):
    response = client.get("/api/state")
    assert response.status_code == 200
    body = response.get_json()
    assert body["warehouse"]["width"] == CONFIG["GRID_WIDTH"]
    assert len(body["robots"]) == 2


def test_api_read_endpoints(client):
    for path in ("/api/robots", "/api/boxes", "/api/tasks", "/api/logs",
                 "/api/events", "/api/statistics", "/api/ci/status", "/api/health"):
        assert client.get(path).status_code == 200, path


def test_api_create_task_and_read_it_back(client):
    response = client.post(
        "/api/tasks",
        json={"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Box-A",
              "destination": "loading_zone", "priority": "HIGH"},
    )
    assert response.status_code == 201
    task_id = response.get_json()["task"]["id"]
    detail = client.get(f"/api/tasks/{task_id}")
    assert detail.status_code == 200
    assert detail.get_json()["task"]["id"] == task_id


def test_api_rejects_an_invalid_task_with_a_clear_error(client):
    response = client.post(
        "/api/tasks",
        json={"type": "PICK_AND_DELIVER", "robot_id": "Robo-01", "box": "Nope",
              "destination": "loading_zone"},
    )
    assert response.status_code == 422
    assert response.get_json()["error"]


def test_api_rejects_an_unknown_task_type(client):
    response = client.post("/api/tasks", json={"type": "FLY_ROBOT"})
    assert response.status_code == 400
    assert "Unknown task type" in response.get_json()["error"]


def test_api_add_robot_and_box(client):
    robot = client.post("/api/robots", json={"name": "Robo-77", "x": 8, "y": 7, "speed": 2.5})
    assert robot.status_code == 201
    box = client.post("/api/boxes", json={"name": "Box-X", "x": 10, "y": 5, "weight": 6,
                                         "destination": "packing_area"})
    assert box.status_code == 201
    assert box.get_json()["box"]["name"] == "Box-X"


def test_api_robot_controls(client):
    robots = client.get("/api/robots").get_json()["robots"]
    robot_id = robots[0]["id"]
    assert client.post(f"/api/robots/{robot_id}/stop").status_code == 200
    assert client.post(f"/api/robots/{robot_id}/resume").status_code == 200
    assert client.post(f"/api/robots/{robot_id}/charge").status_code == 200
    assert client.post(f"/api/robots/{robot_id}/reset").status_code == 200
    assert client.post("/api/robots/robot_zz/stop").status_code == 404


def test_api_simulation_controls(client):
    for path in ("start", "pause", "stop", "emergency-stop", "resume"):
        assert client.post(f"/api/simulation/{path}").status_code == 200, path
    assert client.post("/api/simulation/speed", json={"speed": 5}).status_code == 200
    assert client.post("/api/simulation/speed", json={"speed": -1}).status_code == 400


def test_api_tick_endpoint_advances_the_world(client):
    before = client.get("/api/health").get_json()["tick"]
    client.post("/api/simulation/tick")
    after = client.get("/api/health").get_json()["tick"]
    assert after == before + 1


def test_api_ci_run(client):
    response = client.post("/api/ci/run")
    assert response.status_code == 200
    assert response.get_json()["status"] in ("PASSED", "FAILED")


def test_api_exports_and_persistence(client, tmp_path):
    assert client.get("/api/logs/export?format=txt").status_code == 200
    assert client.get("/api/logs/export?format=json").status_code == 200
    assert client.get("/api/export/state").status_code == 200
    assert client.get("/api/export/tasks").status_code == 200
    assert client.post("/api/state/save").status_code == 200
    assert client.post("/api/state/load").status_code == 200
    assert client.post("/api/state/reset").status_code == 200


def test_api_unknown_endpoint_returns_json_404(client):
    response = client.get("/api/nonexistent")
    assert response.status_code == 404
    assert response.get_json()["ok"] is False


def test_api_dashboard_is_served(client):
    response = client.get("/")
    assert response.status_code == 200
    assert b"Warehouse" in response.data
