"""Core enums, configuration and shared primitives for the warehouse digital twin."""
from __future__ import annotations

import enum
import itertools
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, List, Optional, Tuple

Cell = Tuple[int, int]


# --------------------------------------------------------------------------- #
# Configuration
# --------------------------------------------------------------------------- #
CONFIG: Dict[str, Any] = {
    "GRID_WIDTH": 20,
    "GRID_HEIGHT": 15,
    # Logical seconds advanced per simulation tick.
    "TICK_DT": 0.15,
    # Cells per logical second.
    "DEFAULT_ROBOT_SPEED": 2.0,
    # One percent of battery is consumed every N cells moved.
    "BATTERY_DRAIN_MOVES": 1,
    "BATTERY_PICK_COST": 1,
    "BATTERY_DELIVER_COST": 1,
    "BATTERY_CHARGE_RATE": 2,
    "BATTERY_LOW": 20,
    "BATTERY_CRITICAL": 8,
    "BATTERY_RESERVE": 5,
    # Waiting ticks before a blocked robot tries an alternative route.
    "REPLAN_AFTER_WAIT_TICKS": 3,
    # Waiting ticks before a robot yields to break a deadlock.
    "DEADLOCK_WAIT_TICKS": 20,
    "MAX_LOGS_IN_MEMORY": 5000,
    "MAX_EVENTS_IN_MEMORY": 5000,
    # Push a full state frame to the browser every N ticks.
    "STATE_EMIT_EVERY_TICKS": 2,
    "SIMULATION_SPEEDS": [0.5, 1, 2, 5, 10],
    # Off by default — see backend/llm.py. When True (and a Groq API key
    # is configured), AGENT_REPLAN/AGENT_AUDIT ask a real hosted model to
    # phrase their recommendation instead of using the built-in computed
    # wording. Any failure (no key, network, timeout) falls back to the
    # computed wording either way, so this is purely a narration layer.
    "AGENT_LLM_ENABLED": False,
    # Predictive maintenance (see backend/maintenance.py): a robot is
    # flagged once it's travelled this many cells, or completed this many
    # charge cycles, since its last OPERATOR_MAINTENANCE_SIGNOFF.
    "MAINTENANCE_DISTANCE_THRESHOLD": 150,
    "MAINTENANCE_CHARGE_CYCLES_THRESHOLD": 5,
    # How many ticks between automatic operator shift checks (see
    # Simulator._check_operator_shifts) and mid-task authorization
    # re-checks (see Simulator._check_authorization_changes).
    "SHIFT_CHECK_EVERY_TICKS": 20,
    "AUTHORIZATION_CHECK_EVERY_TICKS": 5,
    # How many samples of statistics history to keep for the dashboard's
    # trend charts (see DigitalTwin.record_history / GET /api/statistics/history).
    "HISTORY_MAX_SAMPLES": 300,
    # 0.0-1.0 — the probability that a robot fails to avoid another robot
    # it would otherwise wait/replan/sidestep around (see Simulator.
    # _act_navigate), simulating imperfect real-time sensing/timing.
    # 0.0 (the default) means the traffic system's avoidance is perfect —
    # collisions never happen on their own, matching this project's
    # behaviour before this existed. Raise it (live, via POST
    # /api/policies/collision-risk, or durably via policies.yaml) to let
    # robots actually collide with each other while carrying out real
    # work, instead of avoiding every conflict — see
    # DigitalTwin.register_collision for the real consequence that
    # follows (both robots halt, their tasks disconnect).
    "COLLISION_RISK": 0.0,
    # 0.0-1.0 — the probability that a delivery's payload silently fails
    # to actually seat at the destination (see Simulator._act_deliver): a
    # dropped/mis-placed box with no independent sensor to catch it, so
    # the robot, the event trail (BOX_DELIVERED, TASK_COMPLETED) and the
    # task lifecycle all proceed exactly as if it had succeeded — the
    # system is fully "sure" it did the job right. 0.0 (the default)
    # means every delivery that completes really did land the box where
    # it was supposed to, matching this project's behaviour before this
    # existed. Raise it (live, via POST /api/policies/false-success-risk,
    # or durably via policies.yaml) to let that gap between "what the
    # system reported" and "what actually happened" occur during real
    # work. Nothing about the live narrative flags it — by design, that's
    # the point — it's only ever caught after the fact, by
    # eval_engine.check_state_transition comparing the task's own
    # before/after world-state snapshot against what TASK_COMPLETED
    # claimed, whether you run that via `python -m backend.run_evals`,
    # `GET /api/tasks/{id}/eval`, or the promptfoo suite in evals/.
    "FALSE_SUCCESS_RISK": 0.0,
}


# --------------------------------------------------------------------------- #
# Toy "assurance" baselines — see TRUST_LAYER.md for the honest scope of
# what these do and don't represent. There's no real hardware, no real AI
# models, and no real employees here, so each of the three actor classes
# (robot, agent, human operator) gets the simplest possible stand-in for
# the real thing, checked by backend/eval_engine.py::check_entities_valid:
#
#   robot     firmware_version must be one of APPROVED_FIRMWARE_VERSIONS
#             (backend/robot.py) — a stand-in for a real SBOM baseline.
#   AI agent  model_version must be one of APPROVED_AGENT_MODELS
#             (backend/agent.py) — a stand-in for a real Agent Assurance
#             Passport (model/tool/policy version).
#   human     certifications must include whatever CERTIFICATION_REQUIREMENTS
#             names for the task type (backend/operator.py) — a stand-in
#             for a real Worker Qualification Passport.
#
# These now gate task creation too: TaskManager.validate() (backend/
# task_manager.py) blocks a task outright — before any actor ever touches
# the work — if an explicitly-requested robot/agent/operator fails any of
# these checks, and AUTO selection (select_robot() / the agent/operator
# AUTO branches in validate()) simply skips ineligible candidates. The
# rule itself lives in one place, backend/eligibility.py, shared between
# that pre-execution gate and eval_engine.check_entities_valid's
# after-the-fact grade, so the two can't quietly disagree.
# --------------------------------------------------------------------------- #
APPROVED_FIRMWARE_VERSIONS: List[str] = ["2.1.0", "2.1.1", "2.2.0"]
APPROVED_AGENT_MODELS: List[str] = ["groq/gpt-oss-20b", "groq/gpt-oss-120b"]

#: Which certification a task type requires an assigned operator to hold.
#: Keyed by TaskType value (a plain string, not the enum, so this table
#: doesn't need to import TaskType before it's defined below).
CERTIFICATION_REQUIREMENTS: Dict[str, str] = {
    "HUMAN_INSPECTION": "safety_inspection",
    "MIXED_MAINTENANCE_MISSION": "electrical_safety",
    "OPERATOR_APPROVAL": "safety_inspection",
    "OPERATOR_MAINTENANCE_SIGNOFF": "electrical_safety",
}

#: Built-in robot "classes" — a preset bundle of speed and (optionally) an
#: allowed_task_types restriction (see Robot.allowed_task_types /
#: backend/eligibility.py), applied when a robot is created with
#: `robot_class=`. Purely a convenience default: the resulting speed and
#: capability list can still be changed afterwards through the normal
#: robot/capabilities endpoints, exactly like a hand-configured robot.
#: "AMR" (the default) is unrestricted, matching every robot's behaviour
#: before robot classes existed.
#:
#: IMPORTANT if you add a class: always include "CHARGE_ROBOT" in its
#: allowed_task_types (or leave allowed_task_types as None/unrestricted).
#: Simulator._auto_charge dispatches an idle low-battery robot's own
#: recharge as a normal, gated CHARGE_ROBOT task (DigitalTwin.
#: request_charge) — a class that can't run CHARGE_ROBOT can never charge
#: itself and will eventually drain to 0% and get stranded in ERROR.
ROBOT_CLASS_PRESETS: Dict[str, Dict[str, Any]] = {
    "AMR": {
        "label": "AMR (general purpose)",
        "speed": 2.0,
        "allowed_task_types": None,
    },
    "FORKLIFT": {
        "label": "Forklift (heavy handling)",
        "speed": 1.2,
        "allowed_task_types": [
            "PICK_AND_DELIVER", "PICK_BOX", "DELIVER_BOX", "MOVE_BOX",
            "MOVE_ROBOT", "CHARGE_ROBOT",
        ],
    },
    "SCOUT": {
        "label": "Scout (inspection only)",
        "speed": 3.0,
        "allowed_task_types": ["MOVE_ROBOT", "MIXED_MAINTENANCE_MISSION", "CHARGE_ROBOT"],
    },
    "HEAVY_HAULER": {
        "label": "Heavy Hauler (slow, high-capacity)",
        "speed": 0.8,
        "allowed_task_types": [
            "PICK_AND_DELIVER", "PICK_BOX", "DELIVER_BOX", "MOVE_BOX",
            "MOVE_ROBOT", "CHARGE_ROBOT", "BATCH_DELIVER",
        ],
    },
    "DRONE": {
        "label": "Drone (fast, aerial/overhead)",
        "speed": 4.0,
        "allowed_task_types": ["MOVE_ROBOT", "MIXED_MAINTENANCE_MISSION", "CHARGE_ROBOT"],
    },
    "PICKER": {
        "label": "Picker (fast, pick & deliver only)",
        "speed": 2.5,
        "allowed_task_types": [
            "PICK_AND_DELIVER", "PICK_BOX", "DELIVER_BOX", "MOVE_BOX", "CHARGE_ROBOT",
        ],
    },
}

#: Every certification this project's toy trust-layer actually knows the
#: name of. Operator.certifications stays a plain list of strings, never
#: validated against this — a hand-typed certification still works fine
#: — this list only populates the dashboard's certification checklist
#: and the OPERATOR_ROLE_PRESETS below. See CERTIFICATION_REQUIREMENTS
#: above for which task types actually require which of these.
KNOWN_CERTIFICATIONS: List[str] = [
    "safety_inspection", "electrical_safety", "equipment_maintenance",
    "heavy_equipment", "hazmat_handling", "quality_control",
]

#: Built-in operator "roles" — a preset bundle of certifications and a
#: shift window (see backend/operator.py), applied when an operator is
#: created with `role=`. Purely a convenience default, exactly like
#: ROBOT_CLASS_PRESETS above: explicit certifications/shift hours still
#: win over the preset, and either can be changed afterwards through the
#: normal endpoints just like a hand-configured operator. No role (the
#: default) means no shift window (always available, manual status
#: control only) and whatever certifications were explicitly given —
#: every operator's behaviour before roles existed.
OPERATOR_ROLE_PRESETS: Dict[str, Dict[str, Any]] = {
    "SAFETY_INSPECTOR": {
        "label": "Safety Inspector",
        "certifications": ["safety_inspection"],
        "shift_start_hour": 9,
        "shift_end_hour": 17,
    },
    "MAINTENANCE_TECH": {
        "label": "Maintenance Technician",
        "certifications": ["electrical_safety", "equipment_maintenance"],
        "shift_start_hour": 9,
        "shift_end_hour": 17,
    },
    "SENIOR_OPERATOR": {
        "label": "Senior Operator / Shift Lead",
        "certifications": list(KNOWN_CERTIFICATIONS),  # every certification — the reliable AUTO fallback
        "shift_start_hour": None,  # no fixed shift — always available
        "shift_end_hour": None,
    },
    "NIGHT_SHIFT": {
        "label": "Night-Shift Operator",
        "certifications": ["safety_inspection"],
        "shift_start_hour": 22,  # wraps past midnight — see Operator.is_within_shift
        "shift_end_hour": 6,
    },
    "REMOTE_ONCALL": {
        "label": "Remote / On-call Operator",
        "certifications": ["safety_inspection", "electrical_safety"],
        "shift_start_hour": None,  # not tied to being physically on the floor
        "shift_end_hour": None,
    },
    "TRAINEE": {
        "label": "Trainee / Probationary Operator",
        "certifications": [],  # deliberately empty — a clean, intentional "not yet certified" demo case
        "shift_start_hour": 9,
        "shift_end_hour": 17,
    },
}


def now_iso() -> str:
    return datetime.now().isoformat(timespec="seconds")


def now_hms() -> str:
    return datetime.now().strftime("%H:%M:%S")


def now_hms_ms() -> str:
    return datetime.now().strftime("%H:%M:%S.%f")[:-3]


# --------------------------------------------------------------------------- #
# Enumerations
# --------------------------------------------------------------------------- #
class CellType(str, enum.Enum):
    EMPTY = "EMPTY"
    WALL = "WALL"
    SHELF = "SHELF"
    STORAGE = "STORAGE"
    CHARGING = "CHARGING"
    LOADING = "LOADING"
    UNLOADING = "UNLOADING"
    PACKING = "PACKING"
    RESTRICTED = "RESTRICTED"
    PARKING = "PARKING"
    ROBOT = "ROBOT"
    BOX = "BOX"


#: Cell types a robot is allowed to drive through.
WALKABLE_CELLS = {
    CellType.EMPTY,
    CellType.STORAGE,
    CellType.CHARGING,
    CellType.LOADING,
    CellType.UNLOADING,
    CellType.PACKING,
    CellType.PARKING,
}


class RobotStatus(str, enum.Enum):
    IDLE = "IDLE"
    PLANNING = "PLANNING"
    MOVING = "MOVING"
    WAITING = "WAITING"
    PICKING = "PICKING"
    CARRYING = "CARRYING"
    DELIVERING = "DELIVERING"
    CHARGING = "CHARGING"
    STOPPED = "STOPPED"
    ERROR = "ERROR"


class AgentStatus(str, enum.Enum):
    """The AI-agent actor class — the ones that interpret information and
    produce recommendations, not move through the warehouse."""
    IDLE = "IDLE"
    THINKING = "THINKING"
    ERROR = "ERROR"


class OperatorStatus(str, enum.Enum):
    """The human actor class — people who inspect, supervise, and approve
    work a robot or agent can't authorize on its own."""
    AVAILABLE = "AVAILABLE"
    ON_TASK = "ON_TASK"
    OFF_DUTY = "OFF_DUTY"


class BoxStatus(str, enum.Enum):
    STORED = "STORED"
    RESERVED = "RESERVED"
    PICKING = "PICKING"
    CARRIED = "CARRIED"
    DELIVERING = "DELIVERING"
    DELIVERED = "DELIVERED"
    FAILED = "FAILED"


class TaskStatus(str, enum.Enum):
    CREATED = "CREATED"
    VALIDATING = "VALIDATING"
    PLANNING = "PLANNING"
    ASSIGNED = "ASSIGNED"
    IN_PROGRESS = "IN_PROGRESS"
    PICKING = "PICKING"
    TRANSPORTING = "TRANSPORTING"
    DELIVERING = "DELIVERING"
    COMPLETED = "COMPLETED"
    FAILED = "FAILED"
    CANCELLED = "CANCELLED"
    BLOCKED = "BLOCKED"
    PAUSED = "PAUSED"


TERMINAL_TASK_STATES = {
    TaskStatus.COMPLETED,
    TaskStatus.FAILED,
    TaskStatus.CANCELLED,
}

ACTIVE_TASK_STATES = {
    TaskStatus.ASSIGNED,
    TaskStatus.IN_PROGRESS,
    TaskStatus.PICKING,
    TaskStatus.TRANSPORTING,
    TaskStatus.DELIVERING,
    TaskStatus.BLOCKED,
}


class TaskType(str, enum.Enum):
    PICK_AND_DELIVER = "PICK_AND_DELIVER"
    MOVE_ROBOT = "MOVE_ROBOT"
    PICK_BOX = "PICK_BOX"
    DELIVER_BOX = "DELIVER_BOX"
    MOVE_BOX = "MOVE_BOX"
    CHARGE_ROBOT = "CHARGE_ROBOT"
    STOP_ROBOT = "STOP_ROBOT"
    RESUME_ROBOT = "RESUME_ROBOT"
    # An AI agent's own task — it reviews current conditions and produces
    # a recommendation. No physical movement; resolves immediately.
    AGENT_INSPECTION = "AGENT_INSPECTION"
    # A human operator's own task — they inspect/approve something. No
    # physical movement; resolves immediately. Requires a certification
    # (see CERTIFICATION_REQUIREMENTS) that isn't checked until grading.
    HUMAN_INSPECTION = "HUMAN_INSPECTION"
    # All three actor classes in one task, modeled on the reference
    # document's worked example: an agent recommends sending a robot to
    # physically inspect a zone, then a human operator signs off on the
    # outcome. Needs a robot (real movement, real ticks), an agent, and
    # an operator.
    MIXED_MAINTENANCE_MISSION = "MIXED_MAINTENANCE_MISSION"
    # An AI agent's own task — reviews the current pending-task queue and
    # every robot's status/battery/workload, and recommends which robot
    # should take each pending task, using the exact same scoring
    # TaskManager.select_robot() uses for real AUTO assignment. No
    # physical movement, no actual reassignment — a recommendation only.
    # Resolves immediately.
    AGENT_REPLAN = "AGENT_REPLAN"
    # An AI agent's own task — reviews the recent warning-and-above log
    # records plus the last Mock CI result and reports what it found. No
    # physical movement; resolves immediately.
    AGENT_AUDIT = "AGENT_AUDIT"
    # A human operator's own task — authorizes something a robot or agent
    # flagged as needing sign-off (e.g. clearing a robot to return to
    # service). Optionally names a `robot_id` the approval concerns; that
    # robot's own eligibility is deliberately NOT re-checked here — the
    # whole point is a human can authorize a robot that's currently
    # ineligible. No physical movement; resolves immediately. Requires a
    # certification (see CERTIFICATION_REQUIREMENTS).
    OPERATOR_APPROVAL = "OPERATOR_APPROVAL"
    # A human operator's own task — signs off maintenance on a robot
    # (optionally named via `robot_id`), reporting whether its firmware is
    # on the approved baseline. No physical movement; resolves
    # immediately. Requires a certification (see
    # CERTIFICATION_REQUIREMENTS).
    OPERATOR_MAINTENANCE_SIGNOFF = "OPERATOR_MAINTENANCE_SIGNOFF"
    # Deliver several boxes to the SAME destination as one tracked job —
    # under the hood the robot still handles them one at a time (Robot
    # only ever carries a single box — see backend/robot.py), it's just
    # NAVIGATE/PICK/NAVIGATE/DELIVER repeated per box under one task id
    # instead of one task per box. See `box_ids` on Task.
    BATCH_DELIVER = "BATCH_DELIVER"


class Priority(str, enum.Enum):
    LOW = "LOW"
    NORMAL = "NORMAL"
    HIGH = "HIGH"
    CRITICAL = "CRITICAL"


PRIORITY_RANK = {
    Priority.LOW: 0,
    Priority.NORMAL: 1,
    Priority.HIGH: 2,
    Priority.CRITICAL: 3,
}


class LogLevel(str, enum.Enum):
    DEBUG = "DEBUG"
    INFO = "INFO"
    WARNING = "WARNING"
    ERROR = "ERROR"
    CRITICAL = "CRITICAL"


LOG_LEVEL_RANK = {
    LogLevel.DEBUG: 10,
    LogLevel.INFO: 20,
    LogLevel.WARNING: 30,
    LogLevel.ERROR: 40,
    LogLevel.CRITICAL: 50,
}


class LogCategory(str, enum.Enum):
    SYSTEM = "SYSTEM"
    ENVIRONMENT = "ENVIRONMENT"
    ROBOT = "ROBOT"
    BOX = "BOX"
    TASK = "TASK"
    PLANNER = "PLANNER"
    NAVIGATION = "NAVIGATION"
    COLLISION = "COLLISION"
    BATTERY = "BATTERY"
    WAREHOUSE = "WAREHOUSE"
    USER = "USER"
    CI = "CI"
    DIGITAL_TWIN = "DIGITAL_TWIN"


class EventType(str, enum.Enum):
    WAREHOUSE_INITIALIZED = "WAREHOUSE_INITIALIZED"
    ROBOT_CREATED = "ROBOT_CREATED"
    ROBOT_MOVED = "ROBOT_MOVED"
    ROBOT_STOPPED = "ROBOT_STOPPED"
    ROBOT_RESUMED = "ROBOT_RESUMED"
    ROBOT_RESET = "ROBOT_RESET"
    ROBOT_CHARGING = "ROBOT_CHARGING"
    ROBOT_CHARGED = "ROBOT_CHARGED"
    ROBOT_WAITING = "ROBOT_WAITING"
    ROBOT_ERROR = "ROBOT_ERROR"
    # A robot's `allowed_task_types` was (re)configured — see
    # backend/eligibility.py and DigitalTwin.set_robot_capabilities.
    ROBOT_CAPABILITIES_UPDATED = "ROBOT_CAPABILITIES_UPDATED"
    # A robot crossed a wear threshold (distance or charge cycles) since
    # its last maintenance sign-off — see backend/maintenance.py.
    MAINTENANCE_ALERT = "MAINTENANCE_ALERT"
    # An OPERATOR_MAINTENANCE_SIGNOFF reset a robot's wear counters.
    MAINTENANCE_PERFORMED = "MAINTENANCE_PERFORMED"
    # A task's assigned robot became ineligible (bad status/firmware/
    # capability) AFTER the task already started — flagged, not gated
    # (the task already has a real robot mid-route) — see
    # Simulator._check_authorization_changes.
    TASK_AUTHORIZATION_CHANGED = "TASK_AUTHORIZATION_CHANGED"
    # An operator's on/off-duty status changed automatically because of
    # its configured shift hours — see Simulator._check_operator_shifts.
    OPERATOR_SHIFT_CHANGED = "OPERATOR_SHIFT_CHANGED"
    # backend/scheduler.py — a recurring task definition was added,
    # removed, or fired and created a real task.
    SCHEDULE_CREATED = "SCHEDULE_CREATED"
    SCHEDULE_REMOVED = "SCHEDULE_REMOVED"
    SCHEDULE_TRIGGERED = "SCHEDULE_TRIGGERED"
    BOX_CREATED = "BOX_CREATED"
    BOX_RESERVED = "BOX_RESERVED"
    BOX_PICKED = "BOX_PICKED"
    BOX_MOVED = "BOX_MOVED"
    BOX_DELIVERED = "BOX_DELIVERED"
    AGENT_CREATED = "AGENT_CREATED"
    AGENT_RECOMMENDATION = "AGENT_RECOMMENDATION"
    AGENT_ERROR = "AGENT_ERROR"
    OPERATOR_CREATED = "OPERATOR_CREATED"
    OPERATOR_APPROVED = "OPERATOR_APPROVED"
    OPERATOR_REJECTED = "OPERATOR_REJECTED"
    TASK_CREATED = "TASK_CREATED"
    TASK_VALIDATED = "TASK_VALIDATED"
    TASK_ASSIGNED = "TASK_ASSIGNED"
    TASK_PLANNED = "TASK_PLANNED"
    TASK_STARTED = "TASK_STARTED"
    TASK_PROGRESS = "TASK_PROGRESS"
    TASK_COMPLETED = "TASK_COMPLETED"
    TASK_FAILED = "TASK_FAILED"
    TASK_CANCELLED = "TASK_CANCELLED"
    TASK_PAUSED = "TASK_PAUSED"
    TASK_RESUMED = "TASK_RESUMED"
    PATH_CREATED = "PATH_CREATED"
    PATH_RECALCULATED = "PATH_RECALCULATED"
    PATH_NOT_FOUND = "PATH_NOT_FOUND"
    COLLISION_AVOIDED = "COLLISION_AVOIDED"
    COLLISION_DETECTED = "COLLISION_DETECTED"
    DEADLOCK_RESOLVED = "DEADLOCK_RESOLVED"
    BATTERY_LOW = "BATTERY_LOW"
    BATTERY_CRITICAL = "BATTERY_CRITICAL"
    EMERGENCY_STOP = "EMERGENCY_STOP"
    SIMULATION_STARTED = "SIMULATION_STARTED"
    SIMULATION_PAUSED = "SIMULATION_PAUSED"
    SIMULATION_STOPPED = "SIMULATION_STOPPED"
    SIMULATION_RESET = "SIMULATION_RESET"
    STATE_SAVED = "STATE_SAVED"
    STATE_LOADED = "STATE_LOADED"
    TWIN_SYNCHRONIZED = "TWIN_SYNCHRONIZED"
    CI_STARTED = "CI_STARTED"
    CI_COMPLETED = "CI_COMPLETED"


class ActionType(str, enum.Enum):
    NAVIGATE = "NAVIGATE"
    PICK = "PICK"
    DELIVER = "DELIVER"
    CHARGE = "CHARGE"
    WAIT = "WAIT"
    COMPLETE = "COMPLETE"


class SimulationStatus(str, enum.Enum):
    STOPPED = "STOPPED"
    RUNNING = "RUNNING"
    PAUSED = "PAUSED"
    EMERGENCY_STOP = "EMERGENCY_STOP"


# --------------------------------------------------------------------------- #
# Plan actions
# --------------------------------------------------------------------------- #
@dataclass
class Action:
    """One low-level step produced by the task planner."""

    type: ActionType
    description: str
    target: Optional[Cell] = None
    target_name: Optional[str] = None
    box_id: Optional[str] = None
    started: bool = False
    done: bool = False

    def to_dict(self) -> Dict[str, Any]:
        return {
            "type": self.type.value,
            "description": self.description,
            "target": {"x": self.target[0], "y": self.target[1]} if self.target else None,
            "target_name": self.target_name,
            "box_id": self.box_id,
            "started": self.started,
            "done": self.done,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Action":
        target = data.get("target")
        return Action(
            type=ActionType(data["type"]),
            description=data["description"],
            target=(target["x"], target["y"]) if target else None,
            target_name=data.get("target_name"),
            box_id=data.get("box_id"),
            started=data.get("started", False),
            done=data.get("done", False),
        )


# --------------------------------------------------------------------------- #
# ID generation
# --------------------------------------------------------------------------- #
class IdFactory:
    """Deterministic, restart-safe sequential id generator."""

    def __init__(self) -> None:
        self._counters: Dict[str, itertools.count] = {}

    def next(self, prefix: str, width: int = 3) -> str:
        counter = self._counters.setdefault(prefix, itertools.count(1))
        return f"{prefix}_{next(counter):0{width}d}"

    def reserve(self, prefix: str, value: int) -> None:
        """Ensure future ids for ``prefix`` start above ``value``."""
        self._counters[prefix] = itertools.count(value + 1)

    def peek(self, prefix: str) -> int:
        counter = self._counters.get(prefix)
        if counter is None:
            return 0
        value = next(counter)
        self._counters[prefix] = itertools.count(value)
        return value - 1


def manhattan(a: Cell, b: Cell) -> int:
    return abs(a[0] - b[0]) + abs(a[1] - b[1])


def cell_dict(cell: Optional[Cell]) -> Optional[Dict[str, int]]:
    if cell is None:
        return None
    return {"x": cell[0], "y": cell[1]}


def cell_tuple(data: Optional[Dict[str, int]]) -> Optional[Cell]:
    if not data:
        return None
    return (int(data["x"]), int(data["y"]))


# --------------------------------------------------------------------------- #
# Policy-as-code — an optional policies.yaml at the project root overrides
# CONFIG / APPROVED_FIRMWARE_VERSIONS / APPROVED_AGENT_MODELS /
# CERTIFICATION_REQUIREMENTS / ROBOT_CLASS_PRESETS above. See
# backend/policy.py for how (in-place mutation, so every module that
# already imported these names sees the update) and policies.example.yaml
# for the file shape. Nothing here changes if the file doesn't exist.
# --------------------------------------------------------------------------- #
from .policy import load_policies  # noqa: E402  (must come after the constants above)

load_policies()
