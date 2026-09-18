"""Autonomous mobile robots.

A robot owns its physical state (where it is, what it carries, how much charge
is left) and its motion bookkeeping. It does not decide *what* to do — that is
the task planner's job — but it knows how to execute one step of a plan.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional, Sequence

from .models import (
    APPROVED_FIRMWARE_VERSIONS,
    CONFIG,
    Cell,
    RobotStatus,
    cell_dict,
    cell_tuple,
    now_iso,
)


class Robot:
    def __init__(
        self,
        robot_id: str,
        name: str,
        position: Cell,
        speed: float = None,
        battery: float = 100.0,
        firmware_version: Optional[str] = None,
        allowed_task_types: Optional[Sequence[str]] = None,
        robot_class: Optional[str] = None,
    ) -> None:
        self.id = robot_id
        self.name = name
        self.position: Cell = position
        self.home: Cell = position
        self.orientation: str = "EAST"
        self.speed: float = float(speed if speed is not None else CONFIG["DEFAULT_ROBOT_SPEED"])
        self.battery: float = float(battery)
        # A preset bundle applied at creation time (see
        # models.ROBOT_CLASS_PRESETS) — purely informational after that;
        # speed/allowed_task_types can still be changed independently.
        self.robot_class: str = robot_class or "AMR"
        # Toy stand-in for a real SBOM-verified firmware baseline — see
        # models.APPROVED_FIRMWARE_VERSIONS and
        # eval_engine.check_entities_valid.
        self.firmware_version: str = firmware_version or APPROVED_FIRMWARE_VERSIONS[0]
        # Which TaskType values this robot may be assigned. None/empty =
        # unrestricted (every robot's default, and the only behaviour that
        # existed before this was added) — see backend/eligibility.py's
        # robot_eligibility() for where this is actually enforced, both at
        # task creation and by eval_engine.check_entities_valid after the
        # fact.
        self.allowed_task_types: Optional[List[str]] = (
            list(allowed_task_types) if allowed_task_types else None
        )
        self.status: RobotStatus = RobotStatus.IDLE
        self.status_before_stop: Optional[RobotStatus] = None
        self.current_task: Optional[str] = None
        self.target_position: Optional[Cell] = None
        self.target_name: Optional[str] = None
        self.current_path: List[Cell] = []
        self.planned_path: List[Cell] = []
        self.carrying_box: Optional[str] = None

        # Statistics
        self.completed_tasks = 0
        self.failed_tasks = 0
        self.boxes_delivered = 0
        self.total_distance = 0
        self.collision_count = 0
        self.wait_events = 0
        self.charging_sessions = 0
        self.battery_consumed = 0.0
        self.replan_count = 0
        self.moves_since_drain = 0
        self.move_accumulator = 0.0
        self.wait_ticks = 0
        self.action_timer = 0
        self.low_battery_warned = False
        self.blocked_by: Optional[str] = None
        self.last_error: Optional[str] = None

        # Predictive maintenance (see backend/maintenance.py): the
        # odometer/charge-cycle reading at the last maintenance sign-off
        # (OPERATOR_MAINTENANCE_SIGNOFF) — wear since then is
        # total_distance/charging_sessions minus these baselines.
        self.maintenance_baseline_distance: int = 0
        self.maintenance_baseline_charges: int = 0
        self.last_maintenance_at: Optional[str] = None
        self.maintenance_alerted: bool = False

        self.created_at = now_iso()
        self.updated_at = now_iso()

    # ------------------------------------------------------------------ #
    # State helpers
    # ------------------------------------------------------------------ #
    def touch(self) -> None:
        self.updated_at = now_iso()

    def set_status(self, status: RobotStatus) -> RobotStatus:
        previous = self.status
        self.status = status
        self.touch()
        return previous

    @property
    def next_cell(self) -> Optional[Cell]:
        return self.current_path[0] if self.current_path else None

    @property
    def is_available(self) -> bool:
        return self.status in (RobotStatus.IDLE, RobotStatus.CHARGING) and self.current_task is None

    @property
    def is_halted(self) -> bool:
        return self.status in (RobotStatus.STOPPED, RobotStatus.ERROR)

    def clear_path(self) -> None:
        self.current_path = []
        self.planned_path = []
        self.target_position = None
        self.target_name = None

    def set_path(self, path: List[Cell], target_name: Optional[str] = None) -> None:
        self.current_path = list(path)
        self.planned_path = [self.position] + list(path)
        self.target_position = path[-1] if path else self.position
        self.target_name = target_name
        self.touch()

    def face_towards(self, cell: Cell) -> None:
        dx = cell[0] - self.position[0]
        dy = cell[1] - self.position[1]
        if abs(dx) >= abs(dy):
            self.orientation = "EAST" if dx > 0 else "WEST" if dx < 0 else self.orientation
        else:
            self.orientation = "SOUTH" if dy > 0 else "NORTH"

    # ------------------------------------------------------------------ #
    # Motion & energy
    # ------------------------------------------------------------------ #
    def ready_to_step(self, dt: float) -> bool:
        """Accumulate travel credit; returns True when one cell may be traversed."""
        self.move_accumulator += self.speed * dt
        if self.move_accumulator >= 1.0:
            self.move_accumulator -= 1.0
            return True
        return False

    def step_to(self, cell: Cell) -> None:
        self.face_towards(cell)
        self.position = cell
        if self.current_path and self.current_path[0] == cell:
            self.current_path.pop(0)
        self.total_distance += 1
        self.moves_since_drain += 1
        self.wait_ticks = 0
        self.blocked_by = None
        self.touch()

    def consume_battery(self, amount: float) -> float:
        before = self.battery
        self.battery = max(0.0, round(self.battery - amount, 2))
        self.battery_consumed = round(self.battery_consumed + (before - self.battery), 2)
        self.touch()
        return before

    def charge(self, amount: float) -> float:
        before = self.battery
        self.battery = min(100.0, round(self.battery + amount, 2))
        self.touch()
        return before

    def drain_for_movement(self) -> Optional[float]:
        """Apply distance-based battery drain. Returns the previous level if drained."""
        if self.moves_since_drain >= CONFIG["BATTERY_DRAIN_MOVES"]:
            self.moves_since_drain = 0
            return self.consume_battery(1.0)
        return None

    # ------------------------------------------------------------------ #
    # Predictive maintenance — see backend/maintenance.py
    # ------------------------------------------------------------------ #
    @property
    def distance_since_maintenance(self) -> int:
        return max(0, self.total_distance - self.maintenance_baseline_distance)

    @property
    def charges_since_maintenance(self) -> int:
        return max(0, self.charging_sessions - self.maintenance_baseline_charges)

    def perform_maintenance(self) -> None:
        """Reset wear counters — called when an OPERATOR_MAINTENANCE_SIGNOFF
        actually completes for this robot (see TaskManager._run_instant)."""
        self.maintenance_baseline_distance = self.total_distance
        self.maintenance_baseline_charges = self.charging_sessions
        self.last_maintenance_at = now_iso()
        self.maintenance_alerted = False
        self.touch()

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "position": cell_dict(self.position),
            "home": cell_dict(self.home),
            "orientation": self.orientation,
            "speed": self.speed,
            "battery": round(self.battery, 1),
            "firmware_version": self.firmware_version,
            "allowed_task_types": list(self.allowed_task_types) if self.allowed_task_types else None,
            "robot_class": self.robot_class,
            # Raw baselines, for save/load round-tripping...
            "maintenance_baseline_distance": self.maintenance_baseline_distance,
            "maintenance_baseline_charges": self.maintenance_baseline_charges,
            "maintenance_alerted": self.maintenance_alerted,
            # ...and the derived, human-readable numbers the dashboard uses.
            "distance_since_maintenance": self.distance_since_maintenance,
            "charges_since_maintenance": self.charges_since_maintenance,
            "last_maintenance_at": self.last_maintenance_at,
            "status": self.status.value,
            "current_task": self.current_task,
            "target_position": cell_dict(self.target_position),
            "target_name": self.target_name,
            "current_path": [cell_dict(c) for c in self.current_path],
            "planned_path": [cell_dict(c) for c in self.planned_path],
            "carrying_box": self.carrying_box,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "boxes_delivered": self.boxes_delivered,
            "total_distance": self.total_distance,
            "collision_count": self.collision_count,
            "wait_events": self.wait_events,
            "charging_sessions": self.charging_sessions,
            "battery_consumed": round(self.battery_consumed, 1),
            "replan_count": self.replan_count,
            "blocked_by": self.blocked_by,
            "last_error": self.last_error,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Robot":
        robot = Robot(
            robot_id=data["id"],
            name=data["name"],
            position=cell_tuple(data["position"]) or (1, 1),
            speed=data.get("speed"),
            battery=data.get("battery", 100.0),
            firmware_version=data.get("firmware_version"),
            allowed_task_types=data.get("allowed_task_types"),
            robot_class=data.get("robot_class"),
        )
        robot.maintenance_baseline_distance = data.get("maintenance_baseline_distance", 0)
        robot.maintenance_baseline_charges = data.get("maintenance_baseline_charges", 0)
        robot.last_maintenance_at = data.get("last_maintenance_at")
        robot.maintenance_alerted = data.get("maintenance_alerted", False)
        robot.home = cell_tuple(data.get("home")) or robot.position
        robot.orientation = data.get("orientation", "EAST")
        robot.status = RobotStatus(data.get("status", "IDLE"))
        robot.current_task = data.get("current_task")
        robot.target_position = cell_tuple(data.get("target_position"))
        robot.target_name = data.get("target_name")
        robot.current_path = [cell_tuple(c) for c in data.get("current_path", []) if c]
        robot.planned_path = [cell_tuple(c) for c in data.get("planned_path", []) if c]
        robot.carrying_box = data.get("carrying_box")
        robot.completed_tasks = data.get("completed_tasks", 0)
        robot.failed_tasks = data.get("failed_tasks", 0)
        robot.boxes_delivered = data.get("boxes_delivered", 0)
        robot.total_distance = data.get("total_distance", 0)
        robot.collision_count = data.get("collision_count", 0)
        robot.wait_events = data.get("wait_events", 0)
        robot.charging_sessions = data.get("charging_sessions", 0)
        robot.battery_consumed = data.get("battery_consumed", 0.0)
        robot.replan_count = data.get("replan_count", 0)
        robot.created_at = data.get("created_at", robot.created_at)
        robot.updated_at = data.get("updated_at", robot.updated_at)
        return robot
