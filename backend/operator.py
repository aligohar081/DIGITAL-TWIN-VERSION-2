"""Human operators: the third actor class.

A person who supervises, inspects, and approves work a robot or agent
can't authorize on its own (see TaskType.HUMAN_INSPECTION and the
MIXED_MAINTENANCE_MISSION flow in task_manager.py, which has an operator
sign off once a robot's physical inspection completes). See
models.CERTIFICATION_REQUIREMENTS for which task types need which
certification, and eval_engine.check_entities_valid for how a task is
flagged if the assigned operator didn't actually hold it.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import OperatorStatus, now_iso


class Operator:
    def __init__(
        self,
        operator_id: str,
        name: str,
        certifications: Optional[List[str]] = None,
        shift_start_hour: Optional[int] = None,
        shift_end_hour: Optional[int] = None,
        role: Optional[str] = None,
    ) -> None:
        self.id = operator_id
        self.name = name
        self.certifications: List[str] = list(certifications or [])
        # A preset bundle applied at creation time (see
        # models.OPERATOR_ROLE_PRESETS) — purely informational after
        # that; certifications/shift can still be changed independently.
        self.role: Optional[str] = role
        self.status: OperatorStatus = OperatorStatus.AVAILABLE
        self.current_task: Optional[str] = None
        self.completed_tasks = 0
        self.failed_tasks = 0
        # Optional shift window, real (wall-clock) hours 0-23. Either both
        # set or neither — None/None means "always available", manual
        # status control only, the only behaviour that existed before
        # this was added. See Simulator._check_operator_shifts, which
        # only ever moves AVAILABLE<->OFF_DUTY automatically, never
        # touches ON_TASK, and never overrides a manual change within the
        # same shift state.
        self.shift_start_hour: Optional[int] = shift_start_hour
        self.shift_end_hour: Optional[int] = shift_end_hour
        self.created_at = now_iso()
        self.updated_at = now_iso()

    # ------------------------------------------------------------------ #
    def touch(self) -> None:
        self.updated_at = now_iso()

    def set_status(self, status: OperatorStatus) -> OperatorStatus:
        previous = self.status
        self.status = status
        self.touch()
        return previous

    @property
    def is_available(self) -> bool:
        return self.status == OperatorStatus.AVAILABLE and self.current_task is None

    @property
    def has_shift(self) -> bool:
        return self.shift_start_hour is not None and self.shift_end_hour is not None

    def is_within_shift(self, hour: int) -> bool:
        """Is `hour` (0-23) inside this operator's configured shift window?
        Always True if no shift is configured. Handles a shift that wraps
        past midnight (e.g. 22 -> 6)."""
        if not self.has_shift:
            return True
        start, end = self.shift_start_hour, self.shift_end_hour
        if start == end:
            return True  # a zero-width window means "always on shift"
        if start < end:
            return start <= hour < end
        return hour >= start or hour < end  # wraps past midnight

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "certifications": list(self.certifications),
            "role": self.role,
            "status": self.status.value,
            "current_task": self.current_task,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "shift_start_hour": self.shift_start_hour,
            "shift_end_hour": self.shift_end_hour,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Operator":
        operator = Operator(
            operator_id=data["id"],
            name=data["name"],
            certifications=data.get("certifications"),
            shift_start_hour=data.get("shift_start_hour"),
            shift_end_hour=data.get("shift_end_hour"),
            role=data.get("role"),
        )
        operator.status = OperatorStatus(data.get("status", "AVAILABLE"))
        operator.current_task = data.get("current_task")
        operator.completed_tasks = data.get("completed_tasks", 0)
        operator.failed_tasks = data.get("failed_tasks", 0)
        operator.created_at = data.get("created_at", operator.created_at)
        operator.updated_at = data.get("updated_at", operator.updated_at)
        return operator
