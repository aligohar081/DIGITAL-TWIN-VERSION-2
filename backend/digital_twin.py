"""The Digital Twin — the single source of truth for the whole system.

Everything the dashboard shows comes from here. The frontend never invents robot
positions or task states; it renders snapshots produced by this object.
"""
from __future__ import annotations

import json
import os
import threading
import time
from collections import deque
from typing import Any, Deque, Dict, List, Optional, Set

from .agent import Agent
from .box import Box
from .event_system import EventSystem
from .logger import WarehouseLogger
from .models import (
    ACTIVE_TASK_STATES,
    CONFIG,
    AgentStatus,
    BoxStatus,
    Cell,
    CellType,
    EventType,
    IdFactory,
    KNOWN_CERTIFICATIONS,
    LogCategory,
    LogLevel,
    OPERATOR_ROLE_PRESETS,
    OperatorStatus,
    Priority,
    PRIORITY_RANK,
    ROBOT_CLASS_PRESETS,
    RobotStatus,
    SimulationStatus,
    TaskStatus,
    TaskType,
    cell_dict,
    now_iso,
)
from .navigation import NavigationEngine
from .operator import Operator
from .robot import Robot
from .task_manager import Task, TaskManager
from .task_planner import TaskPlanner
from .warehouse import Warehouse

#: One demo agent and two demo operators — one fully certified, one only
#: partially, so HUMAN_INSPECTION / MIXED_MAINTENANCE_MISSION have a
#: realistic mix of eligible and ineligible operators to pick from when
#: assigned "AUTO", the same way DEMO_ROBOTS/DEMO_BOXES seed a realistic
#: starting warehouse.
DEMO_AGENTS = [
    ("Ada", "groq/gpt-oss-20b"),
]

DEMO_OPERATORS = [
    ("Sam", ["safety_inspection", "electrical_safety"]),
    ("Lee", ["safety_inspection"]),
]

DEMO_ROBOTS = [
    ("Robo-01", (3, 8)),
    ("Robo-02", (16, 8)),
]

DEMO_BOXES = [
    ("Box-A", (3, 5), 12.5, "shelf_a", "loading_zone"),
    ("Box-B", (9, 5), 8.0, "shelf_b", "packing_area"),
    ("Box-C", (15, 5), 20.0, "shelf_c", "unloading_zone"),
    ("Box-D", (5, 5), 4.5, "shelf_a", "packing_area"),
    ("Box-E", (11, 5), 15.0, "shelf_b", "loading_zone"),
]

DEMO_TASKS = [
    {"type": "PICK_AND_DELIVER", "robot": "Robo-01", "box": "Box-A",
     "source": "shelf_a", "destination": "loading_zone", "priority": "HIGH"},
    {"type": "PICK_AND_DELIVER", "robot": "Robo-02", "box": "Box-B",
     "source": "shelf_b", "destination": "packing_area", "priority": "NORMAL"},
]


class DigitalTwin:
    # Re-exported so collaborators do not need their own enum imports.
    RobotStatus = RobotStatus
    BoxStatus = BoxStatus
    TaskStatus = TaskStatus

    def __init__(
        self,
        log_dir: str = "logs",
        data_dir: str = "data",
        persist_logs: bool = True,
        demo: bool = True,
        demo_tasks: bool = True,
    ) -> None:
        self.lock = threading.RLock()
        self.log_dir = log_dir
        self.data_dir = data_dir
        self.state_path = os.path.join(data_dir, "warehouse_state.json")
        os.makedirs(data_dir, exist_ok=True)

        self.logger = WarehouseLogger(log_dir=log_dir, persist=persist_logs)
        self.events = EventSystem(self.logger)
        self.ids = IdFactory()

        self.warehouse = Warehouse()
        self.navigation = NavigationEngine(self.warehouse)
        self.planner = TaskPlanner(self)
        self.tasks = TaskManager(self)

        self.robots: Dict[str, Robot] = {}
        self.boxes: Dict[str, Box] = {}
        self.agents: Dict[str, Agent] = {}
        self.operators: Dict[str, Operator] = {}

        self.simulation_status = SimulationStatus.STOPPED
        self.simulation_speed = 1.0
        self.tick_count = 0
        self.simulation_time = 0.0
        self.boot_time = time.time()
        self.last_sync = now_iso()
        self.ci_result: Optional[Dict[str, Any]] = None

        # Rolling samples for the dashboard's trend charts — see
        # record_history()/history_snapshot() and GET /api/statistics/history.
        self.history: Deque[Dict[str, Any]] = deque(maxlen=CONFIG["HISTORY_MAX_SAMPLES"])

        # Recurring task definitions — see backend/scheduler.py.
        from .scheduler import Scheduler  # deferred: scheduler.py imports nothing from here at module level

        self.scheduler = Scheduler(self)

        self.statistics: Dict[str, Any] = {
            "completed_tasks": 0,
            "failed_tasks": 0,
            "collisions": 0,
            "collisions_avoided": 0,
            "boxes_delivered": 0,
            "distance_travelled": 0,
            "charging_sessions": 0,
            "emergency_stops": 0,
            "ci_runs": 0,
        }

        self.events.emit(
            EventType.WAREHOUSE_INITIALIZED,
            f"Warehouse initialised: {self.warehouse.width}x{self.warehouse.height} grid, "
            f"{len(self.warehouse.zones)} zones",
            category=LogCategory.WAREHOUSE,
        )

        if demo:
            self.load_demo(create_tasks=demo_tasks)

    # ------------------------------------------------------------------ #
    # Demo / bootstrap
    # ------------------------------------------------------------------ #
    def load_demo(self, create_tasks: bool = True) -> None:
        for name, position in DEMO_ROBOTS:
            self.add_robot(name=name, position=position)
        for name, position, weight, source, destination in DEMO_BOXES:
            self.add_box(name=name, position=position, weight=weight,
                         source=source, destination=destination)
        for name, model_version in DEMO_AGENTS:
            self.add_agent(name=name, model_version=model_version)
        for name, certifications in DEMO_OPERATORS:
            self.add_operator(name=name, certifications=certifications)
        if create_tasks:
            for payload in DEMO_TASKS:
                try:
                    self.tasks.create_task(payload)
                except ValueError as exc:
                    self.logger.error(LogCategory.TASK, f"Demo task rejected: {exc}")

    # ------------------------------------------------------------------ #
    # Lookups
    # ------------------------------------------------------------------ #
    def find_robot(self, spec: Optional[str]) -> Optional[Robot]:
        if not spec:
            return None
        spec = str(spec).strip()
        if spec in self.robots:
            return self.robots[spec]
        low = spec.lower()
        for robot in self.robots.values():
            if robot.name.lower() == low or robot.id.lower() == low:
                return robot
        return None

    def find_box(self, spec: Optional[str]) -> Optional[Box]:
        if not spec:
            return None
        spec = str(spec).strip()
        if spec in self.boxes:
            return self.boxes[spec]
        low = spec.lower()
        for box in self.boxes.values():
            if box.name.lower() == low or box.id.lower() == low:
                return box
        return None

    def find_agent(self, spec: Optional[str]) -> Optional[Agent]:
        if not spec:
            return None
        spec = str(spec).strip()
        if spec in self.agents:
            return self.agents[spec]
        low = spec.lower()
        for agent in self.agents.values():
            if agent.name.lower() == low or agent.id.lower() == low:
                return agent
        return None

    def find_operator(self, spec: Optional[str]) -> Optional[Operator]:
        if not spec:
            return None
        spec = str(spec).strip()
        if spec in self.operators:
            return self.operators[spec]
        low = spec.lower()
        for operator in self.operators.values():
            if operator.name.lower() == low or operator.id.lower() == low:
                return operator
        return None

    def robot_cells(self) -> Dict[Cell, str]:
        return {r.position: r.id for r in self.robots.values()}

    def other_robot_cells(self, exclude_id: str, include_reservations: bool = True) -> Set[Cell]:
        """Cells occupied (or about to be occupied) by robots other than one."""
        cells: Set[Cell] = set()
        for robot in self.robots.values():
            if robot.id == exclude_id:
                continue
            cells.add(robot.position)
            if include_reservations and robot.next_cell is not None:
                cells.add(robot.next_cell)
        return cells

    def box_at(self, cell: Cell) -> Optional[Box]:
        for box in self.boxes.values():
            if box.position == cell and box.status != BoxStatus.CARRIED:
                return box
        return None

    # ------------------------------------------------------------------ #
    # Mutations
    # ------------------------------------------------------------------ #
    def add_robot(
        self,
        name: Optional[str] = None,
        position: Optional[Cell] = None,
        speed: Optional[float] = None,
        allowed_task_types: Optional[List[str]] = None,
        robot_class: Optional[str] = None,
    ) -> Robot:
        with self.lock:
            robot_id = self.ids.next("robot", 2)
            name = name or f"Robo-{robot_id.split('_')[-1]}"
            if self.find_robot(name) is not None:
                raise ValueError(f"A robot called '{name}' already exists")

            candidate = position or self.warehouse.resolve_zone("parking_area").cells[0]
            if not self.warehouse.is_inside(*candidate):
                raise ValueError(f"Position {candidate} is outside the warehouse")
            occupied = set(self.robot_cells().keys())
            spawn = candidate if (self.warehouse.is_walkable(*candidate) and candidate not in occupied) \
                else self.warehouse.nearest_walkable(candidate, occupied)
            if spawn is None:
                raise ValueError("No free drivable cell available for a new robot")

            # A robot class (see models.ROBOT_CLASS_PRESETS) is only a
            # convenience default applied at creation time — explicit
            # `speed`/`allowed_task_types` always win, and either can be
            # changed independently afterwards through the normal
            # endpoints, same as a hand-configured robot.
            normalized_class = (robot_class or "AMR").upper()
            if normalized_class not in ROBOT_CLASS_PRESETS:
                raise ValueError(
                    f"Unknown robot_class '{robot_class}' (known: {sorted(ROBOT_CLASS_PRESETS)})"
                )
            preset = ROBOT_CLASS_PRESETS[normalized_class]
            effective_speed = speed if speed is not None else preset.get("speed")
            effective_allowed = allowed_task_types if allowed_task_types else preset.get("allowed_task_types")

            robot = Robot(
                robot_id, name, spawn, speed=effective_speed,
                allowed_task_types=effective_allowed,
                robot_class=normalized_class,
            )
            self.robots[robot.id] = robot

        self.events.emit(
            EventType.ROBOT_CREATED,
            f"{robot.name} created at ({spawn[0]},{spawn[1]}) with speed {robot.speed} cells/s",
            category=LogCategory.ROBOT,
            robot_id=robot.id,
            position=cell_dict(spawn),
        )
        return robot

    def set_robot_capabilities(self, robot_id: str, allowed_task_types: Optional[List[str]]) -> Robot:
        """(Re)configure which TaskType values `robot_id` may be assigned.

        An empty list or None clears the restriction (unrestricted — the
        default). See backend/eligibility.py's robot_eligibility() for
        where this is actually enforced: TaskManager.validate() (an
        explicitly-named robot outside its own configured task types is
        rejected outright, 422) and select_robot() (AUTO assignment skips
        it the same way it already skips busy/ineligible robots).
        """
        robot = self.find_robot(robot_id)
        if robot is None:
            raise KeyError(f"Robot '{robot_id}' does not exist")
        valid = {t.value for t in TaskType}
        cleaned = sorted({str(t).upper().strip() for t in (allowed_task_types or []) if str(t).strip()})
        unknown = [t for t in cleaned if t not in valid]
        if unknown:
            raise ValueError(f"Unknown task type(s): {unknown}")
        with self.lock:
            robot.allowed_task_types = cleaned or None
            robot.touch()
        self.events.emit(
            EventType.ROBOT_CAPABILITIES_UPDATED,
            f"{robot.name} capabilities set to {cleaned or 'unrestricted'}",
            category=LogCategory.ROBOT,
            robot_id=robot.id,
            data={"allowed_task_types": robot.allowed_task_types},
        )
        return robot

    def add_box(
        self,
        name: Optional[str] = None,
        position: Optional[Cell] = None,
        weight: float = 1.0,
        source: Optional[str] = None,
        destination: Optional[str] = None,
    ) -> Box:
        with self.lock:
            box_id = self.ids.next("box")
            name = name or f"Box-{box_id.split('_')[-1]}"
            if self.find_box(name) is not None:
                raise ValueError(f"A box called '{name}' already exists")

            candidate = position or self.warehouse.resolve_zone("shelf_a").cells[0]
            if not self.warehouse.is_inside(*candidate):
                raise ValueError(f"Position {candidate} is outside the warehouse")
            spot = candidate if self.warehouse.is_walkable(*candidate) \
                else self.warehouse.nearest_walkable(candidate)
            if spot is None:
                raise ValueError("No reachable cell available for a new box")

            zone = self.warehouse.zone_of_cell(spot)
            box = Box(
                box_id=box_id,
                name=name,
                position=spot,
                weight=weight,
                source=source or (zone.key if zone else None),
                destination=destination,
            )
            self.boxes[box.id] = box

        self.events.emit(
            EventType.BOX_CREATED,
            f"{box.name} created at ({spot[0]},{spot[1]}), {box.weight} kg",
            category=LogCategory.BOX,
            box_id=box.id,
            position=cell_dict(spot),
        )
        return box

    def add_agent(self, name: Optional[str] = None, model_version: Optional[str] = None) -> Agent:
        with self.lock:
            agent_id = self.ids.next("agent")
            name = name or f"Agent-{agent_id.split('_')[-1]}"
            if self.find_agent(name) is not None:
                raise ValueError(f"An agent called '{name}' already exists")
            agent = Agent(agent_id, name, model_version=model_version)
            self.agents[agent.id] = agent

        self.events.emit(
            EventType.AGENT_CREATED,
            f"{agent.name} created (model {agent.model_version})",
            category=LogCategory.TASK,
            data={"agent_id": agent.id, "model_version": agent.model_version},
        )
        return agent

    def add_operator(
        self,
        name: Optional[str] = None,
        certifications: Optional[List[str]] = None,
        shift_start_hour: Optional[int] = None,
        shift_end_hour: Optional[int] = None,
        role: Optional[str] = None,
    ) -> Operator:
        with self.lock:
            operator_id = self.ids.next("operator")
            name = name or f"Operator-{operator_id.split('_')[-1]}"
            if self.find_operator(name) is not None:
                raise ValueError(f"An operator called '{name}' already exists")

            # A role (see models.OPERATOR_ROLE_PRESETS) only supplies
            # default certifications/shift hours — explicit values here
            # always win, and either can be changed independently
            # afterwards, exactly like robot_class does for a robot.
            normalized_role = role.upper() if role else None
            preset: Dict[str, Any] = {}
            if normalized_role:
                if normalized_role not in OPERATOR_ROLE_PRESETS:
                    raise ValueError(
                        f"Unknown operator role '{role}' (known: {sorted(OPERATOR_ROLE_PRESETS)})"
                    )
                preset = OPERATOR_ROLE_PRESETS[normalized_role]
            effective_certs = certifications if certifications else preset.get("certifications")
            effective_start = shift_start_hour if shift_start_hour is not None else preset.get("shift_start_hour")
            effective_end = shift_end_hour if shift_end_hour is not None else preset.get("shift_end_hour")

            operator = Operator(
                operator_id, name, certifications=effective_certs,
                shift_start_hour=effective_start, shift_end_hour=effective_end,
                role=normalized_role,
            )
            self.operators[operator.id] = operator

        self.events.emit(
            EventType.OPERATOR_CREATED,
            f"{operator.name} created ({', '.join(operator.certifications) or 'no certifications'})",
            category=LogCategory.TASK,
            data={"operator_id": operator.id, "certifications": list(operator.certifications), "role": normalized_role},
        )
        return operator

    def set_operator_shift(
        self, operator_id: str, shift_start_hour: Optional[int], shift_end_hour: Optional[int]
    ) -> Operator:
        """(Re)configure an operator's automatic shift window (real
        wall-clock hours 0-23) — see Simulator._check_operator_shifts and
        Operator.is_within_shift. Pass both as None to go back to manual
        status control only."""
        operator = self.find_operator(operator_id)
        if operator is None:
            raise KeyError(f"Operator '{operator_id}' does not exist")
        for hour in (shift_start_hour, shift_end_hour):
            if hour is not None and not (0 <= int(hour) <= 23):
                raise ValueError("Shift hours must be between 0 and 23")
        with self.lock:
            operator.shift_start_hour = int(shift_start_hour) if shift_start_hour is not None else None
            operator.shift_end_hour = int(shift_end_hour) if shift_end_hour is not None else None
            operator.touch()
        message = (
            f"{operator.name} shift set to {operator.shift_start_hour}-{operator.shift_end_hour}"
            if operator.has_shift else
            f"{operator.name} shift cleared — manual status control only"
        )
        self.logger.info(LogCategory.TASK, message)
        return operator

    # ---- robot control ------------------------------------------------ #
    def stop_robot(self, robot_id: str, reason: str = "user") -> Robot:
        robot = self.find_robot(robot_id)
        if robot is None:
            raise KeyError(f"Robot '{robot_id}' does not exist")
        if robot.status != RobotStatus.STOPPED:
            robot.status_before_stop = robot.status
        robot.set_status(RobotStatus.STOPPED)
        self.events.emit(
            EventType.ROBOT_STOPPED,
            f"{robot.name} stopped ({reason})",
            category=LogCategory.ROBOT,
            level=LogLevel.WARNING,
            robot_id=robot.id,
            task_id=robot.current_task,
            position=cell_dict(robot.position),
        )
        return robot

    def resume_robot(self, robot_id: str, reason: str = "user") -> Robot:
        robot = self.find_robot(robot_id)
        if robot is None:
            raise KeyError(f"Robot '{robot_id}' does not exist")
        restored = robot.status_before_stop or (
            RobotStatus.PLANNING if robot.current_task else RobotStatus.IDLE
        )
        if restored in (RobotStatus.STOPPED, RobotStatus.ERROR):
            restored = RobotStatus.PLANNING if robot.current_task else RobotStatus.IDLE
        robot.status_before_stop = None
        robot.last_error = None
        robot.set_status(restored)
        task = self.tasks.get(robot.current_task) if robot.current_task else None
        if task is not None and task.status == TaskStatus.PAUSED:
            task.record(TaskStatus.IN_PROGRESS, "Resumed with robot")
        self.events.emit(
            EventType.ROBOT_RESUMED,
            f"{robot.name} resumed ({reason}) → {restored.value}",
            category=LogCategory.ROBOT,
            robot_id=robot.id,
            task_id=robot.current_task,
            position=cell_dict(robot.position),
        )
        return robot

    def request_charge(self, robot_id: str, priority: Priority = Priority.HIGH,
                      internal: bool = False) -> Task:
        robot = self.find_robot(robot_id)
        if robot is None:
            raise KeyError(f"Robot '{robot_id}' does not exist")
        existing = self.tasks.task_for_robot(robot.id)
        if existing is not None and existing.type == TaskType.CHARGE_ROBOT:
            return existing
        return self.tasks.create_task(
            {"type": "CHARGE_ROBOT", "robot_id": robot.id, "priority": priority.value},
            internal=internal,
        )

    def reset_robot(self, robot_id: str) -> Robot:
        robot = self.find_robot(robot_id)
        if robot is None:
            raise KeyError(f"Robot '{robot_id}' does not exist")
        task = self.tasks.get(robot.current_task) if robot.current_task else None
        if task is not None and not task.is_terminal:
            self.tasks.fail_task(task, f"{robot.name} was reset by the operator")
        if robot.carrying_box:
            box = self.find_box(robot.carrying_box)
            if box is not None:
                box.position = robot.position
                box.set_status(BoxStatus.STORED)
                box.assigned_robot = None
                box.assigned_task = None
            robot.carrying_box = None
        occupied = {r.position for r in self.robots.values() if r.id != robot.id}
        home = robot.home if robot.home not in occupied else self.warehouse.nearest_walkable(
            robot.home, occupied
        )
        robot.position = home or robot.position
        robot.battery = 100.0
        robot.clear_path()
        robot.current_task = None
        robot.moves_since_drain = 0
        robot.move_accumulator = 0.0
        robot.wait_ticks = 0
        robot.status_before_stop = None
        robot.last_error = None
        robot.set_status(RobotStatus.IDLE)
        self.events.emit(
            EventType.ROBOT_RESET,
            f"{robot.name} reset to ({robot.position[0]},{robot.position[1]}) with a full battery",
            category=LogCategory.ROBOT,
            level=LogLevel.WARNING,
            robot_id=robot.id,
            position=cell_dict(robot.position),
        )
        return robot

    def register_collision(self, robot_a_id: str, robot_b_id: str) -> Dict[str, Any]:
        """The one place a real collision (two robots actually occupying
        the same cell) turns into a consequence: both robots halt to
        ERROR and — the "task disconnected" behaviour — whatever task
        either of them was running fails immediately, exactly like a
        battery-depletion or controller-error halt already does (see
        Simulator._check_battery_thresholds / Simulator.tick's exception
        handler). Called by Simulator._detect_collisions whenever a real
        overlap actually happens — see CONFIG["COLLISION_RISK"] for why
        that's rare (0 by default) rather than never. Recovery is the
        existing path: POST /api/robots/{id}/reset sends each robot home
        with a full battery and drops its cargo.
        """
        robot_a = self.find_robot(robot_a_id)
        robot_b = self.find_robot(robot_b_id)
        if robot_a is None or robot_b is None:
            raise KeyError("Both robots must exist")
        if robot_a.id == robot_b.id:
            raise ValueError("A robot cannot collide with itself")

        with self.lock:
            self.statistics["collisions"] += 1
            robot_a.collision_count += 1
            robot_b.collision_count += 1

        self.events.emit(
            EventType.COLLISION_DETECTED,
            f"Collision: {robot_a.name} and {robot_b.name} both occupy "
            f"({robot_a.position[0]},{robot_a.position[1]})",
            category=LogCategory.COLLISION,
            level=LogLevel.CRITICAL,
            robot_id=robot_a.id,
            position=cell_dict(robot_a.position),
            data={"robot_a": robot_a.id, "robot_b": robot_b.id},
        )

        disconnected_tasks = []
        for robot, other in ((robot_a, robot_b), (robot_b, robot_a)):
            robot.last_error = f"Collided with {other.name}"
            robot.set_status(RobotStatus.ERROR)
            robot.clear_path()
            self.events.emit(
                EventType.ROBOT_ERROR,
                f"{robot.name} halted after colliding with {other.name}",
                category=LogCategory.COLLISION,
                level=LogLevel.CRITICAL,
                robot_id=robot.id,
                task_id=robot.current_task,
                position=cell_dict(robot.position),
            )
            task = self.tasks.get(robot.current_task) if robot.current_task else None
            if task is not None and not task.is_terminal:
                self.tasks.fail_task(task, f"{robot.name} collided with {other.name} — task disconnected")
                disconnected_tasks.append(task.id)

        return {
            "robot_a": robot_a.id, "robot_b": robot_b.id,
            "position": cell_dict(robot_a.position), "disconnected_tasks": disconnected_tasks,
        }

    # ---- simulation control ------------------------------------------ #
    def set_simulation_status(self, status: SimulationStatus, message: str,
                             event: EventType, level: LogLevel = LogLevel.INFO) -> None:
        self.simulation_status = status
        self.events.emit(event, message, category=LogCategory.SYSTEM, level=level)

    def emergency_stop(self) -> None:
        for robot in self.robots.values():
            if robot.status not in (RobotStatus.STOPPED, RobotStatus.ERROR):
                robot.status_before_stop = robot.status
                robot.set_status(RobotStatus.STOPPED)
        self.simulation_status = SimulationStatus.EMERGENCY_STOP
        self.statistics["emergency_stops"] += 1
        self.events.emit(
            EventType.EMERGENCY_STOP,
            "Emergency stop activated by user — all robots halted, tasks preserved",
            category=LogCategory.SYSTEM,
            level=LogLevel.CRITICAL,
        )

    def release_emergency_stop(self) -> None:
        for robot in self.robots.values():
            if robot.status == RobotStatus.STOPPED:
                self.resume_robot(robot.id, reason="emergency stop released")
        self.simulation_status = SimulationStatus.RUNNING
        self.events.emit(
            EventType.SIMULATION_STARTED,
            "Emergency stop released — robots resuming their tasks",
            category=LogCategory.SYSTEM,
            level=LogLevel.WARNING,
        )

    def reset(self, demo_tasks: bool = True) -> None:
        with self.lock:
            self.robots.clear()
            self.boxes.clear()
            self.agents.clear()
            self.operators.clear()
            self.tasks.clear()
            self.events.clear()
            self.ids = IdFactory()
            self.tick_count = 0
            self.simulation_time = 0.0
            self.simulation_status = SimulationStatus.STOPPED
            self.ci_result = None
            self.history.clear()
            self.scheduler.clear()
            for key in self.statistics:
                self.statistics[key] = 0
            self.navigation = NavigationEngine(self.warehouse)
            self.planner = TaskPlanner(self)
        self.events.emit(
            EventType.SIMULATION_RESET,
            "Warehouse reset to the initial demo configuration",
            category=LogCategory.SYSTEM,
            level=LogLevel.WARNING,
        )
        self.load_demo(create_tasks=demo_tasks)

    def synchronize(self) -> None:
        self.last_sync = now_iso()

    # ------------------------------------------------------------------ #
    # Statistics & snapshots
    # ------------------------------------------------------------------ #
    def environment_state(self) -> Dict[str, Any]:
        robots = list(self.robots.values())
        boxes = list(self.boxes.values())
        counts = self.tasks.counts()
        batteries = [r.battery for r in robots] or [0.0]
        return {
            "warehouse_status": "OPERATIONAL" if self.robots else "EMPTY",
            "simulation_status": self.simulation_status.value,
            "simulation_speed": self.simulation_speed,
            "simulation_time": round(self.simulation_time, 2),
            "tick": self.tick_count,
            "robot_count": len(robots),
            "box_count": len(boxes),
            "active_tasks": counts["active"],
            "pending_tasks": counts["pending"],
            "completed_tasks": counts["completed"],
            "failed_tasks": counts["failed"],
            "collision_count": self.statistics["collisions"],
            "collisions_avoided": self.statistics["collisions_avoided"],
            "system_uptime": round(time.time() - self.boot_time, 1),
            "last_sync": self.last_sync,
        }

    def statistics_snapshot(self) -> Dict[str, Any]:
        robots = list(self.robots.values())
        boxes = list(self.boxes.values())
        counts = self.tasks.counts()
        batteries = [r.battery for r in robots]
        return {
            "robots": len(robots),
            "active_robots": sum(
                1
                for r in robots
                if r.status in (RobotStatus.MOVING, RobotStatus.PICKING, RobotStatus.CARRYING,
                                RobotStatus.DELIVERING, RobotStatus.PLANNING, RobotStatus.WAITING)
            ),
            "idle_robots": sum(1 for r in robots if r.status == RobotStatus.IDLE),
            "charging_robots": sum(1 for r in robots if r.status == RobotStatus.CHARGING),
            "stopped_robots": sum(1 for r in robots if r.is_halted),
            "boxes": len(boxes),
            "boxes_stored": sum(1 for b in boxes if b.status == BoxStatus.STORED),
            "boxes_in_transit": sum(
                1 for b in boxes if b.status in (BoxStatus.CARRIED, BoxStatus.PICKING,
                                                 BoxStatus.DELIVERING, BoxStatus.RESERVED)
            ),
            "boxes_delivered": sum(1 for b in boxes if b.status == BoxStatus.DELIVERED),
            "tasks_total": counts["total"],
            "active_tasks": counts["active"],
            "pending_tasks": counts["pending"],
            "completed_tasks": counts["completed"],
            "failed_tasks": counts["failed"],
            "cancelled_tasks": counts["cancelled"],
            "collisions": self.statistics["collisions"],
            "collisions_avoided": self.statistics["collisions_avoided"],
            "average_battery": round(sum(batteries) / len(batteries), 1) if batteries else 0.0,
            "total_distance": sum(r.total_distance for r in robots),
            "charging_sessions": sum(r.charging_sessions for r in robots),
            "emergency_stops": self.statistics["emergency_stops"],
            "ci_runs": self.statistics["ci_runs"],
            "navigation": self.navigation.stats(),
            "planner": self.planner.stats(),
        }

    def record_history(self) -> None:
        """Append one compact sample of live statistics for the
        dashboard's trend charts (see GET /api/statistics/history).
        Called once per tick from Simulator.tick(); bounded by
        CONFIG["HISTORY_MAX_SAMPLES"] — older samples just roll off."""
        stats = self.statistics_snapshot()
        self.history.append({
            "tick": self.tick_count,
            "time": round(self.simulation_time, 1),
            "timestamp": now_iso(),
            "average_battery": stats["average_battery"],
            "active_tasks": stats["active_tasks"],
            "pending_tasks": stats["pending_tasks"],
            "completed_tasks": stats["completed_tasks"],
            "failed_tasks": stats["failed_tasks"],
            "collisions": stats["collisions"],
        })

    def history_snapshot(self) -> List[Dict[str, Any]]:
        return list(self.history)

    def robot_statistics(self) -> List[Dict[str, Any]]:
        out = []
        for robot in self.robots.values():
            task = self.tasks.get(robot.current_task) if robot.current_task else None
            out.append(
                {
                    "id": robot.id,
                    "name": robot.name,
                    "total_distance": robot.total_distance,
                    "tasks_completed": robot.completed_tasks,
                    "tasks_failed": robot.failed_tasks,
                    "boxes_delivered": robot.boxes_delivered,
                    "battery_consumed": round(robot.battery_consumed, 1),
                    "charging_sessions": robot.charging_sessions,
                    "collisions": robot.collision_count,
                    "wait_events": robot.wait_events,
                    "replans": robot.replan_count,
                    "average_speed": robot.speed,
                    "position": cell_dict(robot.position),
                    "current_task": robot.current_task,
                    "current_task_summary": task.summary() if task else None,
                }
            )
        return out

    def options(self) -> Dict[str, Any]:
        return {
            "robots": [{"id": r.id, "label": r.name} for r in self.robots.values()],
            "boxes": [
                {"id": b.id, "label": b.name, "status": b.status.value}
                for b in self.boxes.values()
            ],
            "agents": [
                {"id": a.id, "label": f"{a.name} ({a.model_version})", "status": a.status.value}
                for a in self.agents.values()
            ],
            "operators": [
                {
                    "id": o.id,
                    "label": f"{o.name} ({', '.join(o.certifications) or 'no certs'})",
                    "status": o.status.value,
                    "certifications": list(o.certifications),
                }
                for o in self.operators.values()
            ],
            "locations": self.warehouse.location_options(),
            "task_types": [
                {"id": TaskType.PICK_AND_DELIVER.value, "label": "Pick & deliver"},
                {"id": TaskType.MOVE_ROBOT.value, "label": "Move robot"},
                {"id": TaskType.PICK_BOX.value, "label": "Pick box"},
                {"id": TaskType.DELIVER_BOX.value, "label": "Deliver box"},
                {"id": TaskType.MOVE_BOX.value, "label": "Move box"},
                {"id": TaskType.CHARGE_ROBOT.value, "label": "Charge robot"},
                {"id": TaskType.STOP_ROBOT.value, "label": "Stop robot"},
                {"id": TaskType.RESUME_ROBOT.value, "label": "Resume robot"},
                {"id": TaskType.AGENT_INSPECTION.value, "label": "Agent inspection"},
                {"id": TaskType.HUMAN_INSPECTION.value, "label": "Human inspection"},
                {"id": TaskType.MIXED_MAINTENANCE_MISSION.value, "label": "Mixed maintenance mission"},
                {"id": TaskType.AGENT_REPLAN.value, "label": "Agent replan review"},
                {"id": TaskType.AGENT_AUDIT.value, "label": "Agent audit"},
                {"id": TaskType.OPERATOR_APPROVAL.value, "label": "Operator approval"},
                {"id": TaskType.OPERATOR_MAINTENANCE_SIGNOFF.value, "label": "Operator maintenance sign-off"},
                {"id": TaskType.BATCH_DELIVER.value, "label": "Batch deliver (multiple boxes)"},
            ],
            # The task types it actually makes sense to restrict a robot
            # to (i.e. the ones a robot is really dispatched for) — what
            # the dashboard's per-robot capability editor offers as
            # checkboxes. STOP_ROBOT/RESUME_ROBOT are deliberately
            # excluded: they're safety controls, not dispatched work, and
            # never go through robot_eligibility at all (see
            # TaskManager.IMMEDIATE).
            "robot_capability_task_types": [
                {"id": TaskType.PICK_AND_DELIVER.value, "label": "Pick & deliver"},
                {"id": TaskType.MOVE_ROBOT.value, "label": "Move robot"},
                {"id": TaskType.PICK_BOX.value, "label": "Pick box"},
                {"id": TaskType.DELIVER_BOX.value, "label": "Deliver box"},
                {"id": TaskType.MOVE_BOX.value, "label": "Move box"},
                {"id": TaskType.CHARGE_ROBOT.value, "label": "Charge robot"},
                {"id": TaskType.MIXED_MAINTENANCE_MISSION.value, "label": "Mixed maintenance mission"},
                {"id": TaskType.BATCH_DELIVER.value, "label": "Batch deliver (multiple boxes)"},
            ],
            "robot_classes": [
                {"id": key, "label": preset.get("label", key)}
                for key, preset in ROBOT_CLASS_PRESETS.items()
            ],
            "operator_roles": [
                {"id": key, "label": preset.get("label", key)}
                for key, preset in OPERATOR_ROLE_PRESETS.items()
            ],
            "known_certifications": list(KNOWN_CERTIFICATIONS),
            "priorities": [p.value for p in Priority],
            "speeds": CONFIG["SIMULATION_SPEEDS"],
        }

    def snapshot(self, include_layout: bool = False, task_limit: int = 60) -> Dict[str, Any]:
        with self.lock:
            state: Dict[str, Any] = {
                "environment": self.environment_state(),
                "robots": [r.to_dict() for r in self.robots.values()],
                "boxes": [b.to_dict() for b in self.boxes.values()],
                "agents": [a.to_dict() for a in self.agents.values()],
                "operators": [o.to_dict() for o in self.operators.values()],
                "tasks": self.tasks.list_tasks(limit=task_limit),
                "statistics": self.statistics_snapshot(),
                "robot_statistics": self.robot_statistics(),
                "ci": self.ci_result,
                "options": self.options(),
                "timestamp": now_iso(),
            }
            if include_layout:
                state["warehouse"] = self.warehouse.to_dict()
                state["config"] = {
                    key: CONFIG[key]
                    for key in (
                        "GRID_WIDTH", "GRID_HEIGHT", "TICK_DT", "BATTERY_LOW",
                        "BATTERY_CRITICAL", "SIMULATION_SPEEDS",
                    )
                }
        return state

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def serialize(self) -> Dict[str, Any]:
        with self.lock:
            return {
                "version": 1,
                "saved_at": now_iso(),
                "warehouse": {"width": self.warehouse.width, "height": self.warehouse.height},
                "robots": [r.to_dict() for r in self.robots.values()],
                "boxes": [b.to_dict() for b in self.boxes.values()],
                "agents": [a.to_dict() for a in self.agents.values()],
                "operators": [o.to_dict() for o in self.operators.values()],
                "tasks": [t.to_dict() for t in self.tasks.tasks.values()],
                "schedules": self.scheduler.to_dict(),
                "statistics": dict(self.statistics),
                "simulation": {
                    "status": self.simulation_status.value,
                    "speed": self.simulation_speed,
                    "tick": self.tick_count,
                    "time": self.simulation_time,
                },
                "counters": {
                    "robot": self.ids.peek("robot"),
                    "box": self.ids.peek("box"),
                    "task": self.ids.peek("task"),
                    "agent": self.ids.peek("agent"),
                    "operator": self.ids.peek("operator"),
                },
            }

    def save_state(self, path: Optional[str] = None) -> str:
        path = path or self.state_path
        payload = self.serialize()
        os.makedirs(os.path.dirname(path) or ".", exist_ok=True)
        with open(path, "w", encoding="utf-8") as handle:
            json.dump(payload, handle, indent=2)
        self.events.emit(
            EventType.STATE_SAVED,
            f"Digital twin state saved to {path}",
            category=LogCategory.DIGITAL_TWIN,
        )
        return path

    def load_state(self, path: Optional[str] = None) -> None:
        path = path or self.state_path
        if not os.path.exists(path):
            raise FileNotFoundError(f"No saved state at {path}")
        with open(path, "r", encoding="utf-8") as handle:
            payload = json.load(handle)

        with self.lock:
            self.robots.clear()
            self.boxes.clear()
            self.agents.clear()
            self.operators.clear()
            self.tasks.clear()
            for data in payload.get("robots", []):
                robot = Robot.from_dict(data)
                self.robots[robot.id] = robot
            for data in payload.get("boxes", []):
                box = Box.from_dict(data)
                self.boxes[box.id] = box
            for data in payload.get("agents", []):
                agent = Agent.from_dict(data)
                self.agents[agent.id] = agent
            for data in payload.get("operators", []):
                operator = Operator.from_dict(data)
                self.operators[operator.id] = operator
            sequence = 0
            for data in payload.get("tasks", []):
                task = Task.from_dict(data)
                sequence += 1
                task.sequence = sequence
                self.tasks.tasks[task.id] = task
            self.tasks._sequence = sequence
            self.scheduler.load_dict(payload.get("schedules", {}))
            self.statistics.update(payload.get("statistics", {}))
            simulation = payload.get("simulation", {})
            self.simulation_status = SimulationStatus(simulation.get("status", "STOPPED"))
            self.simulation_speed = simulation.get("speed", 1.0)
            self.tick_count = simulation.get("tick", 0)
            self.simulation_time = simulation.get("time", 0.0)
            counters = payload.get("counters", {})
            for prefix, value in counters.items():
                self.ids.reserve(prefix, int(value or 0))

        self.events.emit(
            EventType.STATE_LOADED,
            f"Digital twin state loaded from {path}: "
            f"{len(self.robots)} robots, {len(self.boxes)} boxes, {len(self.tasks.tasks)} tasks",
            category=LogCategory.DIGITAL_TWIN,
        )
        self.synchronize()
