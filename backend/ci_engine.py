"""Mock CI engine.

A pipeline of assertions run against the live digital twin. It answers one
question: is the simulated world internally consistent right now? Each check is
independent, reports its own duration, and never mutates state.
"""
from __future__ import annotations

import os
import time
from typing import Any, Callable, Dict, List, Optional, Tuple

from .models import (
    ACTIVE_TASK_STATES,
    BoxStatus,
    CellType,
    EventType,
    LogCategory,
    LogLevel,
    RobotStatus,
    TERMINAL_TASK_STATES,
    TaskStatus,
    now_iso,
)

CheckResult = Tuple[bool, str, Dict[str, Any]]


class CIEngine:
    def __init__(self, twin: Any) -> None:
        self.twin = twin
        self.pipeline_number = 100
        self.history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    @property
    def checks(self) -> List[Tuple[str, Callable[[], CheckResult]]]:
        return [
            ("Environment validation", self._check_environment),
            ("Robot state validation", self._check_robot_state),
            ("Robot position validation", self._check_robot_positions),
            ("Navigation validation", self._check_navigation),
            ("Collision validation", self._check_collisions),
            ("Battery validation", self._check_battery),
            ("Box state validation", self._check_boxes),
            ("Task validation", self._check_tasks),
            ("Task planner validation", self._check_planner),
            ("Digital twin synchronisation", self._check_twin_sync),
            ("Environment boundary validation", self._check_boundaries),
            ("Logging validation", self._check_logging),
        ]

    def run(self) -> Dict[str, Any]:
        twin = self.twin
        self.pipeline_number += 1
        pipeline_id = f"#{self.pipeline_number}"
        started = time.time()

        twin.events.emit(
            EventType.CI_STARTED,
            f"[CI] Pipeline {pipeline_id} started ({len(self.checks)} checks)",
            category=LogCategory.CI,
        )

        results: List[Dict[str, Any]] = []
        for name, check in self.checks:
            twin.logger.debug(LogCategory.CI, f"[CI] Running {name}")
            check_started = time.time()
            try:
                passed, message, details = check()
            except Exception as exc:  # a broken check is a failed check
                passed, message, details = False, f"Check raised {exc!r}", {}
            duration_ms = round((time.time() - check_started) * 1000, 2)
            results.append(
                {
                    "name": name,
                    "passed": passed,
                    "message": message,
                    "duration_ms": duration_ms,
                    "details": details,
                }
            )
            twin.logger.log(
                LogLevel.INFO if passed else LogLevel.ERROR,
                LogCategory.CI,
                f"[CI] {name} {'PASSED' if passed else 'FAILED'} — {message}",
                data={"duration_ms": duration_ms},
            )

        failed = [r for r in results if not r["passed"]]
        summary = {
            "pipeline": pipeline_id,
            "status": "PASSED" if not failed else "FAILED",
            "checks": results,
            "passed": len(results) - len(failed),
            "failed": len(failed),
            "total": len(results),
            "duration_ms": round((time.time() - started) * 1000, 2),
            "finished_at": now_iso(),
        }
        twin.ci_result = summary
        twin.statistics["ci_runs"] += 1
        self.history.append(summary)
        self.history = self.history[-25:]

        twin.events.emit(
            EventType.CI_COMPLETED,
            f"[CI] Pipeline {pipeline_id} {summary['status']} "
            f"({summary['passed']}/{summary['total']} checks in {summary['duration_ms']} ms)",
            category=LogCategory.CI,
            level=LogLevel.INFO if not failed else LogLevel.ERROR,
            data={"failed": [r["name"] for r in failed]},
        )
        return summary

    def status(self) -> Dict[str, Any]:
        return {
            "last_run": self.twin.ci_result,
            "runs": self.twin.statistics["ci_runs"],
            "checks": [name for name, _ in self.checks],
            "history": [
                {k: v for k, v in run.items() if k != "checks"} for run in self.history[-10:]
            ],
        }

    # ------------------------------------------------------------------ #
    # Checks
    # ------------------------------------------------------------------ #
    def _check_environment(self) -> CheckResult:
        warehouse = self.twin.warehouse
        problems: List[str] = []
        if warehouse.width < 5 or warehouse.height < 5:
            problems.append("grid is too small to operate")
        for x in range(warehouse.width):
            if warehouse.cell_type(x, 0) is not CellType.WALL or \
               warehouse.cell_type(x, warehouse.height - 1) is not CellType.WALL:
                problems.append(f"perimeter breach at column {x}")
                break
        for y in range(warehouse.height):
            if warehouse.cell_type(0, y) is not CellType.WALL or \
               warehouse.cell_type(warehouse.width - 1, y) is not CellType.WALL:
                problems.append(f"perimeter breach at row {y}")
                break
        required = {"charging_station", "loading_zone", "packing_area", "shelf_a"}
        missing = required - set(warehouse.zones)
        if missing:
            problems.append(f"missing zones: {', '.join(sorted(missing))}")

        # Every drivable cell must be reachable from every other drivable cell.
        walkable = warehouse.walkable_cells()
        if walkable:
            seen = {walkable[0]}
            stack = [walkable[0]]
            while stack:
                for neighbor in warehouse.neighbors(stack.pop()):
                    if neighbor not in seen:
                        seen.add(neighbor)
                        stack.append(neighbor)
            unreachable = len(walkable) - len(seen)
            if unreachable:
                problems.append(f"{unreachable} drivable cells are walled off")
        details = {
            "grid": f"{warehouse.width}x{warehouse.height}",
            "zones": len(warehouse.zones),
            "drivable_cells": len(walkable),
        }
        if problems:
            return False, "; ".join(problems), details
        return True, f"{warehouse.width}x{warehouse.height} grid, {len(warehouse.zones)} zones, " \
                     f"{len(walkable)} drivable cells all connected", details

    def _check_robot_state(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        names: Dict[str, str] = {}
        for robot in twin.robots.values():
            if robot.name in names:
                problems.append(f"duplicate robot name {robot.name}")
            names[robot.name] = robot.id
            if not 0 <= robot.battery <= 100:
                problems.append(f"{robot.name} battery out of range ({robot.battery})")
            if not isinstance(robot.status, RobotStatus):
                problems.append(f"{robot.name} has an invalid status")
            if robot.speed <= 0:
                problems.append(f"{robot.name} has a non-positive speed")
            if robot.carrying_box and twin.find_box(robot.carrying_box) is None:
                problems.append(f"{robot.name} carries unknown box {robot.carrying_box}")
            if robot.current_task and twin.tasks.get(robot.current_task) is None:
                problems.append(f"{robot.name} references unknown task {robot.current_task}")
        details = {"robots": len(twin.robots)}
        if problems:
            return False, "; ".join(problems), details
        return True, f"{len(twin.robots)} robots hold valid state", details

    def _check_robot_positions(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        for robot in twin.robots.values():
            x, y = robot.position
            if not twin.warehouse.is_inside(x, y):
                problems.append(f"{robot.name} is outside the warehouse at ({x},{y})")
            elif not twin.warehouse.is_walkable(x, y):
                problems.append(
                    f"{robot.name} stands on a "
                    f"{twin.warehouse.cell_type(x, y).value} cell at ({x},{y})"
                )
        details = {
            "positions": {r.name: list(r.position) for r in twin.robots.values()},
        }
        if problems:
            return False, "; ".join(problems), details
        return True, "All robots occupy drivable cells", details

    def _check_navigation(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        for robot in twin.robots.values():
            path = robot.current_path
            if not path:
                continue
            previous = robot.position
            for cell in path:
                if not twin.warehouse.is_walkable(*cell):
                    problems.append(f"{robot.name} path crosses a blocked cell {cell}")
                    break
                if abs(cell[0] - previous[0]) + abs(cell[1] - previous[1]) != 1:
                    problems.append(f"{robot.name} path jumps from {previous} to {cell}")
                    break
                previous = cell
        details = {
            "active_paths": sum(1 for r in twin.robots.values() if r.current_path),
            **twin.navigation.stats(),
        }
        if problems:
            return False, "; ".join(problems), details
        return True, f"{details['active_paths']} active path(s) are contiguous and drivable", details

    def _check_collisions(self) -> CheckResult:
        twin = self.twin
        seen: Dict[tuple, str] = {}
        clashes: List[str] = []
        for robot in twin.robots.values():
            if robot.position in seen:
                clashes.append(
                    f"{robot.name} and {seen[robot.position]} both occupy "
                    f"({robot.position[0]},{robot.position[1]})"
                )
            seen[robot.position] = robot.name
        details = {
            "collisions_recorded": twin.statistics["collisions"],
            "collisions_avoided": twin.statistics["collisions_avoided"],
        }
        if clashes:
            return False, "; ".join(clashes), details
        return True, (
            f"No overlapping robots; {details['collisions_avoided']} conflicts were avoided "
            f"and {details['collisions_recorded']} collisions recorded"
        ), details

    def _check_battery(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        for robot in twin.robots.values():
            if robot.battery < 0 or robot.battery > 100:
                problems.append(f"{robot.name} battery is {robot.battery}")
            if robot.battery == 0 and robot.status != RobotStatus.ERROR:
                problems.append(f"{robot.name} is flat but not in an error state")
            if robot.status == RobotStatus.CHARGING and \
                    twin.warehouse.cell_type(*robot.position) is not CellType.CHARGING:
                problems.append(f"{robot.name} is charging away from the charging station")
        batteries = [r.battery for r in twin.robots.values()]
        details = {
            "average": round(sum(batteries) / len(batteries), 1) if batteries else 0,
            "lowest": min(batteries) if batteries else 0,
        }
        if problems:
            return False, "; ".join(problems), details
        return True, f"Average charge {details['average']}%, lowest {details['lowest']}%", details

    def _check_boxes(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        carried_by_robot = {
            r.carrying_box: r for r in twin.robots.values() if r.carrying_box
        }
        for box in twin.boxes.values():
            if not twin.warehouse.is_inside(*box.position):
                problems.append(f"{box.name} is outside the warehouse")
            if box.status == BoxStatus.CARRIED:
                holder = carried_by_robot.get(box.id)
                if holder is None:
                    problems.append(f"{box.name} is CARRIED but no robot holds it")
                elif holder.position != box.position:
                    problems.append(
                        f"{box.name} is at {box.position} but {holder.name} is at {holder.position}"
                    )
            elif box.id in carried_by_robot:
                problems.append(
                    f"{carried_by_robot[box.id].name} holds {box.name} but its status is "
                    f"{box.status.value}"
                )
        details = {
            "boxes": len(twin.boxes),
            "carried": len(carried_by_robot),
            "delivered": sum(1 for b in twin.boxes.values() if b.status == BoxStatus.DELIVERED),
        }
        if problems:
            return False, "; ".join(problems), details
        return True, (
            f"{details['boxes']} boxes consistent ({details['carried']} in transit, "
            f"{details['delivered']} delivered)"
        ), details

    def _check_tasks(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        for task in twin.tasks.tasks.values():
            if task.robot_id and twin.find_robot(task.robot_id) is None:
                problems.append(f"{task.id} points at unknown robot {task.robot_id}")
            if task.box_id and twin.find_box(task.box_id) is None:
                problems.append(f"{task.id} points at unknown box {task.box_id}")
            if task.status in TERMINAL_TASK_STATES and not task.completed_at:
                problems.append(f"{task.id} finished without a completion timestamp")
            if task.status in ACTIVE_TASK_STATES and not task.robot_id:
                problems.append(f"{task.id} is active without a robot")
            if task.status == TaskStatus.FAILED and not task.error:
                problems.append(f"{task.id} failed without an error message")
        details = twin.tasks.counts()
        if problems:
            return False, "; ".join(problems), details
        return True, (
            f"{details['total']} tasks tracked ({details['active']} active, "
            f"{details['completed']} completed, {details['failed']} failed)"
        ), details

    def _check_planner(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        for task in twin.tasks.tasks.values():
            if task.status not in ACTIVE_TASK_STATES:
                continue
            if not task.actions:
                problems.append(f"{task.id} is active without a plan")
                continue
            if not 0 <= task.action_index <= len(task.actions):
                problems.append(f"{task.id} action index {task.action_index} is out of range")
            for action in task.actions:
                if action.target is not None and not twin.warehouse.is_inside(*action.target):
                    problems.append(f"{task.id} targets {action.target} outside the warehouse")
        details = {
            "plans_created": twin.planner.plans_created,
            "active_plans": sum(1 for t in twin.tasks.tasks.values() if t.is_active),
        }
        if problems:
            return False, "; ".join(problems), details
        return True, f"{details['active_plans']} active plan(s) well formed " \
                     f"({details['plans_created']} produced so far)", details

    def _check_twin_sync(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        for robot in twin.robots.values():
            task = twin.tasks.get(robot.current_task) if robot.current_task else None
            if task is not None and task.robot_id != robot.id:
                problems.append(f"{robot.name} runs {task.id} but the task belongs to {task.robot_id}")
            if task is not None and task.is_terminal:
                problems.append(f"{robot.name} still holds finished task {task.id}")
        for box in twin.boxes.values():
            if box.assigned_robot and twin.find_robot(box.assigned_robot) is None:
                problems.append(f"{box.name} assigned to unknown robot {box.assigned_robot}")
            if box.assigned_task and twin.tasks.get(box.assigned_task) is None:
                problems.append(f"{box.name} assigned to unknown task {box.assigned_task}")
        details = {"last_sync": twin.last_sync, "tick": twin.tick_count}
        if problems:
            return False, "; ".join(problems), details
        return True, f"Robots, boxes and tasks agree at tick {twin.tick_count}", details

    def _check_boundaries(self) -> CheckResult:
        twin = self.twin
        problems: List[str] = []
        for entity in list(twin.robots.values()) + list(twin.boxes.values()):
            x, y = entity.position
            if not (0 <= x < twin.warehouse.width and 0 <= y < twin.warehouse.height):
                problems.append(f"{entity.name} is at ({x},{y}), outside the grid")
        details = {"entities": len(twin.robots) + len(twin.boxes)}
        if problems:
            return False, "; ".join(problems), details
        return True, f"All {details['entities']} entities are inside the grid", details

    def _check_logging(self) -> CheckResult:
        twin = self.twin
        logger = twin.logger
        problems: List[str] = []
        records = logger.export_json()
        if not records:
            problems.append("no log records exist")
        required_fields = {"timestamp", "level", "category", "message"}
        for record in records[-50:]:
            missing = required_fields - set(record)
            if missing:
                problems.append(f"record {record.get('seq')} missing {', '.join(sorted(missing))}")
                break
        if logger.persist:
            for path in (logger.text_path, logger.json_path):
                if not os.path.exists(path):
                    problems.append(f"{path} was not created")
        details = {
            "records_in_memory": len(records),
            "events_in_memory": len(twin.events.events),
            "text_log": logger.text_path,
            "json_log": logger.json_path,
        }
        if problems:
            return False, "; ".join(problems), details
        return True, (
            f"{details['records_in_memory']} log records and "
            f"{details['events_in_memory']} events are well formed"
        ), details
