"""The task planner.

Turns a high-level instruction ("pick Box-A and deliver it to the loading zone")
into an ordered list of low-level actions the robot controller can execute, and
checks up front whether the robot has the charge to finish the job.
"""
from __future__ import annotations

import math
from typing import Any, Dict, List, Optional, Set, Tuple

from .models import (
    CONFIG,
    Action,
    ActionType,
    Cell,
    LogCategory,
    LogLevel,
    TaskType,
)


class PlanningError(Exception):
    """Raised when a task cannot be turned into an executable plan."""


class TaskPlanner:
    def __init__(self, twin: Any) -> None:
        self.twin = twin
        self.plans_created = 0

    # ------------------------------------------------------------------ #
    # Target resolution
    # ------------------------------------------------------------------ #
    def resolve_target(
        self,
        spec: Any,
        origin: Cell,
        blocked: Optional[Set[Cell]] = None,
        prefer_free: bool = False,
    ) -> Tuple[Cell, str]:
        """Turn a location specification into a concrete grid cell."""
        warehouse = self.twin.warehouse
        nav = self.twin.navigation
        blocked = set(blocked or ())

        if spec is None:
            raise PlanningError("No destination given")

        # Explicit coordinates: {"x": .., "y": ..} or "12,4"
        cell: Optional[Cell] = None
        if isinstance(spec, dict) and "x" in spec and "y" in spec:
            cell = (int(spec["x"]), int(spec["y"]))
        elif isinstance(spec, (list, tuple)) and len(spec) == 2:
            cell = (int(spec[0]), int(spec[1]))
        elif isinstance(spec, str) and "," in spec:
            try:
                parts = [int(p.strip()) for p in spec.split(",")]
                cell = (parts[0], parts[1])
            except (ValueError, IndexError):
                cell = None

        if cell is not None:
            if not warehouse.is_inside(*cell):
                raise PlanningError(f"Coordinates {cell} are outside the warehouse")
            resolved = cell if warehouse.is_walkable(*cell) else warehouse.nearest_walkable(cell, blocked)
            if resolved is None:
                raise PlanningError(f"No drivable cell near {cell}")
            return resolved, warehouse.label_for_cell(resolved)

        # A named zone.
        zone = warehouse.resolve_zone(str(spec))
        if zone is not None:
            prefer = None
            if prefer_free:
                occupied = {b.position for b in self.twin.boxes.values()}
                prefer = {c for c in zone.cells if c not in occupied}
            chosen = nav.best_cell_in_zone(zone.cells, origin, blocked=blocked, prefer=prefer)
            if chosen is None:
                raise PlanningError(f"{zone.label} is not reachable right now")
            return chosen, zone.label

        # A box name or id.
        box = self.twin.find_box(str(spec))
        if box is not None:
            return box.position, box.name

        raise PlanningError(f"Unknown location '{spec}'")

    # ------------------------------------------------------------------ #
    # Battery estimation
    # ------------------------------------------------------------------ #
    def estimate_battery(self, robot: Any, waypoints: List[Cell], handling_ops: int = 0) -> float:
        """Estimate battery percentage required to drive a route and handle boxes."""
        nav = self.twin.navigation
        origin = robot.position
        total_cells = 0
        for waypoint in waypoints:
            distance = nav.distance(origin, waypoint)
            if distance is None:
                distance = abs(origin[0] - waypoint[0]) + abs(origin[1] - waypoint[1])
            total_cells += distance
            origin = waypoint
        movement_cost = math.ceil(total_cells / CONFIG["BATTERY_DRAIN_MOVES"])
        handling_cost = handling_ops * CONFIG["BATTERY_PICK_COST"]
        return float(movement_cost + handling_cost + CONFIG["BATTERY_RESERVE"])

    # ------------------------------------------------------------------ #
    # Planning
    # ------------------------------------------------------------------ #
    def plan(self, task: Any, robot: Any) -> List[Action]:
        """Build the action list for ``task`` executed by ``robot``."""
        logger = self.twin.logger
        logger.info(
            LogCategory.PLANNER,
            f"Planning {task.type.value} for {robot.name}",
            task_id=task.id,
            robot_id=robot.id,
        )

        blocked = self.twin.other_robot_cells(robot.id)
        actions: List[Action] = []
        waypoints: List[Cell] = []
        handling_ops = 0

        if task.type in (TaskType.PICK_AND_DELIVER, TaskType.MOVE_BOX):
            box = self.twin.find_box(task.box_id)
            if box is None:
                raise PlanningError(f"Box '{task.box_id}' does not exist")
            pick_cell, pick_label = self.resolve_target(box.id, robot.position, blocked)
            drop_cell, drop_label = self.resolve_target(
                task.destination, pick_cell, blocked, prefer_free=True
            )
            logger.info(
                LogCategory.PLANNER,
                f"Source {box.name} found at ({pick_cell[0]},{pick_cell[1]})",
                task_id=task.id,
                robot_id=robot.id,
                box_id=box.id,
            )
            logger.info(
                LogCategory.PLANNER,
                f"Destination {drop_label} found at ({drop_cell[0]},{drop_cell[1]})",
                task_id=task.id,
                robot_id=robot.id,
            )
            actions = [
                Action(ActionType.NAVIGATE, f"Navigate to {box.name}", pick_cell, pick_label, box.id),
                Action(ActionType.PICK, f"Pick {box.name}", pick_cell, pick_label, box.id),
                Action(ActionType.NAVIGATE, f"Carry {box.name} to {drop_label}", drop_cell, drop_label, box.id),
                Action(ActionType.DELIVER, f"Deliver {box.name} at {drop_label}", drop_cell, drop_label, box.id),
            ]
            waypoints = [pick_cell, drop_cell]
            handling_ops = 2

        elif task.type == TaskType.BATCH_DELIVER:
            # Same destination, multiple boxes — the robot still only
            # ever carries one at a time (Robot.carrying_box is a single
            # id, not a list), so this is NAVIGATE/PICK/NAVIGATE/DELIVER
            # repeated per box under one task id, not simultaneous
            # carrying. See TaskType.BATCH_DELIVER's docstring in models.py.
            for box_id in task.box_ids:
                box = self.twin.find_box(box_id)
                if box is None:
                    raise PlanningError(f"Box '{box_id}' does not exist")
                pick_cell, pick_label = self.resolve_target(box.id, robot.position, blocked)
                drop_cell, drop_label = self.resolve_target(
                    task.destination, pick_cell, blocked, prefer_free=True
                )
                actions.extend([
                    Action(ActionType.NAVIGATE, f"Navigate to {box.name}", pick_cell, pick_label, box.id),
                    Action(ActionType.PICK, f"Pick {box.name}", pick_cell, pick_label, box.id),
                    Action(ActionType.NAVIGATE, f"Carry {box.name} to {drop_label}", drop_cell, drop_label, box.id),
                    Action(ActionType.DELIVER, f"Deliver {box.name} at {drop_label}", drop_cell, drop_label, box.id),
                ])
                waypoints.extend([pick_cell, drop_cell])
                handling_ops += 2

        elif task.type == TaskType.MOVE_ROBOT:
            cell, label = self.resolve_target(task.destination, robot.position, blocked)
            actions = [Action(ActionType.NAVIGATE, f"Navigate to {label}", cell, label)]
            waypoints = [cell]

        elif task.type == TaskType.MIXED_MAINTENANCE_MISSION:
            # The robot's role in the mission: physically go inspect the
            # zone the agent recommended. The agent's recommendation and
            # the operator's sign-off are logged by TaskManager
            # (start_task/complete_task), not planned as actions here —
            # they're not physical steps a robot executes.
            cell, label = self.resolve_target(task.destination, robot.position, blocked)
            actions = [Action(ActionType.NAVIGATE, f"Inspect {label}", cell, label)]
            waypoints = [cell]

        elif task.type == TaskType.PICK_BOX:
            box = self.twin.find_box(task.box_id)
            if box is None:
                raise PlanningError(f"Box '{task.box_id}' does not exist")
            pick_cell, pick_label = self.resolve_target(box.id, robot.position, blocked)
            actions = [
                Action(ActionType.NAVIGATE, f"Navigate to {box.name}", pick_cell, pick_label, box.id),
                Action(ActionType.PICK, f"Pick {box.name}", pick_cell, pick_label, box.id),
            ]
            waypoints = [pick_cell]
            handling_ops = 1

        elif task.type == TaskType.DELIVER_BOX:
            box = self.twin.find_box(task.box_id)
            if box is None:
                raise PlanningError(f"Box '{task.box_id}' does not exist")
            if robot.carrying_box == box.id:
                drop_cell, drop_label = self.resolve_target(
                    task.destination, robot.position, blocked, prefer_free=True
                )
                actions = [
                    Action(ActionType.NAVIGATE, f"Carry {box.name} to {drop_label}", drop_cell, drop_label, box.id),
                    Action(ActionType.DELIVER, f"Deliver {box.name} at {drop_label}", drop_cell, drop_label, box.id),
                ]
                waypoints = [drop_cell]
                handling_ops = 1
            else:
                pick_cell, pick_label = self.resolve_target(box.id, robot.position, blocked)
                drop_cell, drop_label = self.resolve_target(
                    task.destination, pick_cell, blocked, prefer_free=True
                )
                actions = [
                    Action(ActionType.NAVIGATE, f"Navigate to {box.name}", pick_cell, pick_label, box.id),
                    Action(ActionType.PICK, f"Pick {box.name}", pick_cell, pick_label, box.id),
                    Action(ActionType.NAVIGATE, f"Carry {box.name} to {drop_label}", drop_cell, drop_label, box.id),
                    Action(ActionType.DELIVER, f"Deliver {box.name} at {drop_label}", drop_cell, drop_label, box.id),
                ]
                waypoints = [pick_cell, drop_cell]
                handling_ops = 2

        elif task.type == TaskType.CHARGE_ROBOT:
            cell, label = self.resolve_target("charging_station", robot.position, blocked)
            actions = [
                Action(ActionType.NAVIGATE, f"Navigate to {label}", cell, label),
                Action(ActionType.CHARGE, "Charge to 100%", cell, label),
            ]
            waypoints = [cell]

        else:
            raise PlanningError(f"{task.type.value} does not need a movement plan")

        # ---- battery-aware planning ---------------------------------- #
        if task.type != TaskType.CHARGE_ROBOT and waypoints:
            required = self.estimate_battery(robot, waypoints, handling_ops)
            task.battery_estimate = required
            if robot.battery < required:
                charge_cell, charge_label = self.resolve_target(
                    "charging_station", robot.position, blocked
                )
                logger.warning(
                    LogCategory.BATTERY,
                    f"{robot.name} has {robot.battery:.0f}% but needs ~{required:.0f}% — "
                    f"routing via {charge_label} first",
                    task_id=task.id,
                    robot_id=robot.id,
                )
                actions = [
                    Action(ActionType.NAVIGATE, f"Detour to {charge_label}", charge_cell, charge_label),
                    Action(ActionType.CHARGE, "Charge before starting the task", charge_cell, charge_label),
                ] + actions
                task.recharged_before_start = True
            else:
                logger.debug(
                    LogCategory.BATTERY,
                    f"{robot.name} battery check passed: {robot.battery:.0f}% available, "
                    f"~{required:.0f}% required",
                    task_id=task.id,
                    robot_id=robot.id,
                )

        actions.append(Action(ActionType.COMPLETE, "Complete task"))
        self.plans_created += 1
        logger.info(
            LogCategory.PLANNER,
            f"Plan for {task.id} contains {len(actions)} actions",
            task_id=task.id,
            robot_id=robot.id,
            data={"actions": [a.description for a in actions]},
        )
        return actions

    def stats(self) -> Dict[str, int]:
        return {"plans_created": self.plans_created}
