"""Recurring tasks — a lightweight in-process scheduler.

A ScheduledTask remembers a task-creation payload — the exact same shape
TaskManager.create_task already takes — and an interval. Simulator.tick()
calls Scheduler.tick() once per tick; whenever a schedule's next_run_tick
has arrived, it creates a real task via twin.tasks.create_task(payload,
internal=True) and reschedules itself, same as an automatic recharge
already does (see DigitalTwin.request_charge).

Intervals are tracked in TICKS, not wall-clock seconds, so a schedule
fires deterministically along with the rest of the tick-driven simulation
(see simulator.py's own docstring on why nothing here uses real sleeps).
`interval_seconds` is accepted everywhere as a convenience and converted
once via CONFIG["TICK_DT"].
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .models import CONFIG, EventType, LogCategory, now_iso


class ScheduledTask:
    def __init__(
        self, schedule_id: str, payload: Dict[str, Any], interval_ticks: int, enabled: bool = True
    ) -> None:
        self.id = schedule_id
        self.payload = dict(payload)
        self.interval_ticks = max(1, int(interval_ticks))
        self.enabled = enabled
        self.next_run_tick = 0  # set by Scheduler.add()
        self.run_count = 0
        self.last_run_at: Optional[str] = None
        self.last_task_id: Optional[str] = None
        self.created_at = now_iso()

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "payload": dict(self.payload),
            "interval_ticks": self.interval_ticks,
            "interval_seconds": round(self.interval_ticks * CONFIG["TICK_DT"], 2),
            "enabled": self.enabled,
            "next_run_tick": self.next_run_tick,
            "run_count": self.run_count,
            "last_run_at": self.last_run_at,
            "last_task_id": self.last_task_id,
            "created_at": self.created_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "ScheduledTask":
        schedule = ScheduledTask(
            schedule_id=data["id"],
            payload=data.get("payload", {}),
            interval_ticks=data.get("interval_ticks", 1),
            enabled=data.get("enabled", True),
        )
        schedule.next_run_tick = data.get("next_run_tick", 0)
        schedule.run_count = data.get("run_count", 0)
        schedule.last_run_at = data.get("last_run_at")
        schedule.last_task_id = data.get("last_task_id")
        schedule.created_at = data.get("created_at", schedule.created_at)
        return schedule


class Scheduler:
    def __init__(self, twin: Any) -> None:
        self.twin = twin
        self.schedules: Dict[str, ScheduledTask] = {}
        self._sequence = 0

    def add(
        self,
        payload: Dict[str, Any],
        interval_seconds: Optional[float] = None,
        interval_ticks: Optional[int] = None,
        run_immediately: bool = False,
    ) -> ScheduledTask:
        if not payload.get("type"):
            raise ValueError("A scheduled task needs a 'type'")
        if interval_ticks is None:
            if interval_seconds is None:
                raise ValueError("Provide interval_seconds or interval_ticks")
            interval_ticks = max(1, round(float(interval_seconds) / CONFIG["TICK_DT"]))
        self._sequence += 1
        schedule = ScheduledTask(f"schedule_{self._sequence:04d}", payload, int(interval_ticks))
        schedule.next_run_tick = (
            self.twin.tick_count if run_immediately else self.twin.tick_count + schedule.interval_ticks
        )
        self.schedules[schedule.id] = schedule
        self.twin.events.emit(
            EventType.SCHEDULE_CREATED,
            f"{schedule.id} created: {payload.get('type')} every {schedule.to_dict()['interval_seconds']}s",
            category=LogCategory.TASK,
            data={"schedule_id": schedule.id, "payload": payload},
        )
        return schedule

    def remove(self, schedule_id: str) -> None:
        if schedule_id not in self.schedules:
            raise KeyError(f"Schedule '{schedule_id}' does not exist")
        del self.schedules[schedule_id]
        self.twin.events.emit(
            EventType.SCHEDULE_REMOVED, f"{schedule_id} removed",
            category=LogCategory.TASK, data={"schedule_id": schedule_id},
        )

    def set_enabled(self, schedule_id: str, enabled: bool) -> ScheduledTask:
        schedule = self.schedules.get(schedule_id)
        if schedule is None:
            raise KeyError(f"Schedule '{schedule_id}' does not exist")
        schedule.enabled = bool(enabled)
        return schedule

    def list(self) -> List[Dict[str, Any]]:
        return [s.to_dict() for s in self.schedules.values()]

    def tick(self) -> None:
        """Called once per simulator tick (see Simulator.tick) — fires any
        due, enabled schedule. A failed task-creation payload is logged,
        not raised — one bad schedule must never stop the simulation."""
        twin = self.twin
        for schedule in list(self.schedules.values()):
            if not schedule.enabled or twin.tick_count < schedule.next_run_tick:
                continue
            schedule.next_run_tick = twin.tick_count + schedule.interval_ticks
            schedule.run_count += 1
            schedule.last_run_at = now_iso()
            try:
                task = twin.tasks.create_task(dict(schedule.payload), internal=True)
                schedule.last_task_id = task.id
                twin.events.emit(
                    EventType.SCHEDULE_TRIGGERED,
                    f"{schedule.id} fired → {task.id} ({schedule.payload.get('type')})",
                    category=LogCategory.TASK, task_id=task.id,
                    data={"schedule_id": schedule.id},
                )
            except ValueError as exc:
                twin.logger.error(
                    LogCategory.TASK, f"Schedule {schedule.id} failed to create a task: {exc}",
                    data={"schedule_id": schedule.id},
                )

    # ------------------------------------------------------------------ #
    # Persistence — see DigitalTwin.to_dict/from_dict (state save/load)
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {"schedules": [s.to_dict() for s in self.schedules.values()], "sequence": self._sequence}

    def load_dict(self, data: Dict[str, Any]) -> None:
        self.schedules = {}
        for raw in data.get("schedules", []):
            schedule = ScheduledTask.from_dict(raw)
            self.schedules[schedule.id] = schedule
        self._sequence = data.get("sequence", len(self.schedules))

    def clear(self) -> None:
        self.schedules = {}
        self._sequence = 0
