"""Task manager: intake, validation, prioritisation and robot assignment.

Tasks are created by the user (or internally, e.g. an automatic recharge), run
through validation, wait in a priority queue, and are then matched to a robot.
The manager owns task lifecycle; the simulator owns execution.
"""
from __future__ import annotations

import threading
from typing import Any, Dict, List, Optional, Tuple

from .models import (
    ACTIVE_TASK_STATES,
    Action,
    ActionType,
    APPROVED_FIRMWARE_VERSIONS,
    CERTIFICATION_REQUIREMENTS,
    Cell,
    EventType,
    LogCategory,
    LogLevel,
    PRIORITY_RANK,
    Priority,
    TERMINAL_TASK_STATES,
    TaskStatus,
    TaskType,
    cell_dict,
    cell_tuple,
    now_hms,
    now_iso,
)
from .eligibility import agent_eligibility, operator_eligibility, robot_eligibility
from .llm import narrate
from .task_planner import PlanningError


class Task:
    def __init__(
        self,
        task_id: str,
        task_type: TaskType,
        robot_id: Optional[str] = None,
        box_id: Optional[str] = None,
        box_ids: Optional[List[str]] = None,
        source: Optional[str] = None,
        destination: Optional[str] = None,
        priority: Priority = Priority.NORMAL,
        internal: bool = False,
        sequence: int = 0,
        agent_id: Optional[str] = None,
        operator_id: Optional[str] = None,
        required_certification: Optional[str] = None,
        second_operator_id: Optional[str] = None,
        dual_signoff: bool = False,
    ) -> None:
        self.id = task_id
        self.type = task_type
        self.requested_robot = robot_id or "AUTO"
        self.robot_id: Optional[str] = None if self.requested_robot == "AUTO" else robot_id
        self.box_id = box_id
        # BATCH_DELIVER's list of boxes (see TaskType.BATCH_DELIVER); every
        # other task type leaves this empty and uses box_id instead.
        self.box_ids: List[str] = list(box_ids or [])
        self.source = source
        self.destination = destination
        self.priority = priority
        self.internal = internal
        self.sequence = sequence
        # The two non-robot actor classes (see backend/agent.py,
        # backend/operator.py) — only populated for task types that need
        # them (AGENT_INSPECTION, HUMAN_INSPECTION, MIXED_MAINTENANCE_MISSION).
        self.requested_agent = agent_id or "AUTO"
        self.agent_id: Optional[str] = None if self.requested_agent == "AUTO" else agent_id
        self.requested_operator = operator_id or "AUTO"
        self.operator_id: Optional[str] = None if self.requested_operator == "AUTO" else operator_id
        self.required_certification = required_certification or CERTIFICATION_REQUIREMENTS.get(task_type.value)
        # Two-person sign-off (opt-in, see `dual_signoff` in create_task's
        # payload handling and the matching block in validate()).
        self.dual_signoff = bool(dual_signoff) or bool(second_operator_id)
        self.requested_second_operator = second_operator_id or ("AUTO" if self.dual_signoff else None)
        self.second_operator_id: Optional[str] = (
            None if not self.requested_second_operator or self.requested_second_operator == "AUTO"
            else second_operator_id
        )
        self.status = TaskStatus.CREATED
        self.actions: List[Action] = []
        self.action_index = 0
        self.error: Optional[str] = None
        # Set once by Simulator._check_authorization_changes if the
        # assigned robot became ineligible AFTER this task started — see
        # TASK_AUTHORIZATION_CHANGED. Flags, never gates a running task.
        self.authorization_flagged: Optional[str] = None
        self.battery_estimate: Optional[float] = None
        self.recharged_before_start = False
        self.replans = 0
        self.created_at = now_iso()
        self.updated_at = now_iso()
        self.started_at: Optional[str] = None
        self.completed_at: Optional[str] = None
        self.history: List[Dict[str, Any]] = []

    # ------------------------------------------------------------------ #
    @property
    def current_action(self) -> Optional[Action]:
        if 0 <= self.action_index < len(self.actions):
            return self.actions[self.action_index]
        return None

    @property
    def progress(self) -> int:
        if self.status == TaskStatus.COMPLETED:
            return 100
        if not self.actions:
            return 0
        return int(round(100 * min(self.action_index, len(self.actions)) / len(self.actions)))

    @property
    def is_terminal(self) -> bool:
        return self.status in TERMINAL_TASK_STATES

    @property
    def is_active(self) -> bool:
        return self.status in ACTIVE_TASK_STATES

    def record(self, status: TaskStatus, message: str) -> None:
        self.status = status
        self.updated_at = now_iso()
        self.history.append({"time": now_hms(), "timestamp": now_iso(), "status": status.value, "message": message})

    def summary(self) -> str:
        if self.type in (TaskType.PICK_AND_DELIVER, TaskType.MOVE_BOX, TaskType.DELIVER_BOX):
            return f"{self.box_id or 'box'} → {self.destination or 'destination'}"
        if self.type == TaskType.PICK_BOX:
            return f"pick {self.box_id}"
        if self.type == TaskType.MOVE_ROBOT:
            return f"move to {self.destination}"
        if self.type == TaskType.CHARGE_ROBOT:
            return "charge"
        if self.type == TaskType.MIXED_MAINTENANCE_MISSION:
            return f"inspect {self.destination or 'target zone'} (agent + robot + operator)"
        if self.type == TaskType.BATCH_DELIVER:
            return f"{len(self.box_ids)} boxes → {self.destination or 'destination'}"
        if self.type == TaskType.AGENT_REPLAN:
            return "review the queue and recommend assignment"
        if self.type == TaskType.AGENT_AUDIT:
            return "audit recent logs and CI health"
        if self.type in (TaskType.OPERATOR_APPROVAL, TaskType.OPERATOR_MAINTENANCE_SIGNOFF):
            action = "approve" if self.type == TaskType.OPERATOR_APPROVAL else "maintenance sign-off"
            return f"{action} {self.robot_id}" if self.robot_id else action
        return self.type.value.lower().replace("_", " ")

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "type": self.type.value,
            "requested_robot": self.requested_robot,
            "robot_id": self.robot_id,
            "requested_agent": self.requested_agent,
            "agent_id": self.agent_id,
            "requested_operator": self.requested_operator,
            "operator_id": self.operator_id,
            "required_certification": self.required_certification,
            "dual_signoff": self.dual_signoff,
            "requested_second_operator": self.requested_second_operator,
            "second_operator_id": self.second_operator_id,
            "box_id": self.box_id,
            "box_ids": list(self.box_ids),
            "source": self.source,
            "destination": self.destination,
            "priority": self.priority.value,
            "priority_rank": PRIORITY_RANK[self.priority],
            "status": self.status.value,
            "summary": self.summary(),
            "actions": [a.to_dict() for a in self.actions],
            "action_index": self.action_index,
            "current_action": self.current_action.description if self.current_action else None,
            "progress": self.progress,
            "error": self.error,
            "authorization_flagged": self.authorization_flagged,
            "battery_estimate": self.battery_estimate,
            "recharged_before_start": self.recharged_before_start,
            "replans": self.replans,
            "internal": self.internal,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
            "started_at": self.started_at,
            "completed_at": self.completed_at,
            "history": self.history,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Task":
        task = Task(
            task_id=data["id"],
            task_type=TaskType(data["type"]),
            robot_id=data.get("requested_robot", "AUTO"),
            box_id=data.get("box_id"),
            box_ids=data.get("box_ids"),
            source=data.get("source"),
            destination=data.get("destination"),
            priority=Priority(data.get("priority", "NORMAL")),
            internal=data.get("internal", False),
            agent_id=data.get("requested_agent", "AUTO"),
            operator_id=data.get("requested_operator", "AUTO"),
            required_certification=data.get("required_certification"),
            second_operator_id=data.get("requested_second_operator"),
            dual_signoff=data.get("dual_signoff", False),
        )
        task.robot_id = data.get("robot_id")
        task.agent_id = data.get("agent_id")
        task.operator_id = data.get("operator_id")
        task.second_operator_id = data.get("second_operator_id")
        task.status = TaskStatus(data.get("status", "CREATED"))
        task.actions = [Action.from_dict(a) for a in data.get("actions", [])]
        task.action_index = data.get("action_index", 0)
        task.error = data.get("error")
        task.authorization_flagged = data.get("authorization_flagged")
        task.battery_estimate = data.get("battery_estimate")
        task.recharged_before_start = data.get("recharged_before_start", False)
        task.replans = data.get("replans", 0)
        task.created_at = data.get("created_at", task.created_at)
        task.updated_at = data.get("updated_at", task.updated_at)
        task.started_at = data.get("started_at")
        task.completed_at = data.get("completed_at")
        task.history = data.get("history", [])
        return task


class TaskManager:
    #: Task types that take effect the moment they are created.
    IMMEDIATE = {TaskType.STOP_ROBOT, TaskType.RESUME_ROBOT}

    #: Task types that resolve immediately after validation, with no
    #: physical movement and no robot dispatch — an agent "thinking" or a
    #: human "inspecting"/"approving" doesn't need navigation the way a
    #: robot task does. Unlike IMMEDIATE, these still go through
    #: validate() (an agent/operator has to actually exist, be eligible,
    #: and be available) — see _run_instant().
    INSTANT = {
        TaskType.AGENT_INSPECTION,
        TaskType.HUMAN_INSPECTION,
        TaskType.AGENT_REPLAN,
        TaskType.AGENT_AUDIT,
        TaskType.OPERATOR_APPROVAL,
        TaskType.OPERATOR_MAINTENANCE_SIGNOFF,
    }

    def __init__(self, twin: Any) -> None:
        self.twin = twin
        self.tasks: Dict[str, Task] = {}
        self._lock = threading.RLock()
        self._sequence = 0

    # ------------------------------------------------------------------ #
    # State snapshots
    #
    # A compact, structured picture of the physical world relevant to one
    # task — captured once when the task starts (before anything moves)
    # and once again at its terminal event (completed/failed/cancelled) —
    # so evals can grade the *outcome* (did the box actually end up at
    # its destination? did the source shelf free up and the destination
    # shelf take the box?) instead of only the event trail. See
    # backend/eval_engine.py::check_state_transition and
    # evals/promptfooconfig.groq.yaml.
    # ------------------------------------------------------------------ #
    def _resolve_task_zones(self, task: Task) -> None:
        """Cache which named zones count as this task's effective source
        and destination, resolved once (the first time a snapshot is
        taken) so the 'before' and 'after' snapshots always compare the
        occupancy of the *same* zones even if the box itself later moves.
        Most task payloads only ever set a destination — the source is
        implied by wherever the box already sits, so fall back to that."""
        if hasattr(task, "_source_zone_key"):
            return
        twin = self.twin
        source_zone = twin.warehouse.resolve_zone(task.source)
        if source_zone is None and task.box_id:
            box = twin.find_box(task.box_id)
            if box is not None:
                source_zone = twin.warehouse.zone_of_cell(box.position)
        destination_zone = twin.warehouse.resolve_zone(task.destination)
        task._source_zone_key = source_zone.key if source_zone else None
        task._destination_zone_key = destination_zone.key if destination_zone else None

    def _zone_snapshot(self, zone_key: Optional[str]) -> Optional[Dict[str, Any]]:
        if not zone_key:
            return None
        twin = self.twin
        zone = twin.warehouse.zones.get(zone_key)
        if zone is None:
            return None
        occupants = sorted(b.id for b in twin.boxes.values() if b.position in zone.cells)
        return {
            "key": zone.key,
            "label": zone.label,
            "occupied": bool(occupants),
            "boxes_present": occupants,
        }

    def snapshot_state(self, task: Task) -> Dict[str, Any]:
        self._resolve_task_zones(task)
        twin = self.twin
        state: Dict[str, Any] = {"task_type": task.type.value}

        robot = twin.find_robot(task.robot_id) if task.robot_id else None
        if robot is not None:
            zone = twin.warehouse.zone_of_cell(robot.position)
            state["robot"] = {
                "id": robot.id,
                "name": robot.name,
                "position": cell_dict(robot.position),
                "zone": zone.key if zone else None,
                "status": robot.status.value,
                "carrying_box": robot.carrying_box,
                "battery": round(robot.battery, 1),
                "firmware_version": robot.firmware_version,
                "allowed_task_types": list(robot.allowed_task_types) if robot.allowed_task_types else None,
            }

        box = twin.find_box(task.box_id) if task.box_id else None
        if box is not None:
            zone = twin.warehouse.zone_of_cell(box.position)
            state["box"] = {
                "id": box.id,
                "name": box.name,
                "position": cell_dict(box.position),
                "zone": zone.key if zone else None,
                "status": box.status.value,
            }

        if task.box_ids:  # BATCH_DELIVER — one entry per box, same shape as "box" above
            boxes = []
            for box_id in task.box_ids:
                b = twin.find_box(box_id)
                if b is None:
                    continue
                zone = twin.warehouse.zone_of_cell(b.position)
                boxes.append({
                    "id": b.id, "name": b.name, "position": cell_dict(b.position),
                    "zone": zone.key if zone else None, "status": b.status.value,
                })
            state["boxes"] = boxes

        agent = twin.find_agent(task.agent_id) if task.agent_id else None
        if agent is not None:
            state["agent"] = {
                "id": agent.id,
                "name": agent.name,
                "model_version": agent.model_version,
                "status": agent.status.value,
            }

        operator = twin.find_operator(task.operator_id) if task.operator_id else None
        if operator is not None:
            state["operator"] = {
                "id": operator.id,
                "name": operator.name,
                "certifications": list(operator.certifications),
                "status": operator.status.value,
            }

        # The co-signer for a `dual_signoff` task (see the `dual_signoff`
        # block in validate()) — graded by eval_engine.check_entities_valid
        # exactly like the primary operator, same required_certification.
        if task.dual_signoff and task.second_operator_id:
            second = twin.find_operator(task.second_operator_id)
            if second is not None:
                state["second_operator"] = {
                    "id": second.id,
                    "name": second.name,
                    "certifications": list(second.certifications),
                    "status": second.status.value,
                }

        if task.required_certification:
            state["required_certification"] = task.required_certification

        source = self._zone_snapshot(getattr(task, "_source_zone_key", None))
        destination = self._zone_snapshot(getattr(task, "_destination_zone_key", None))
        if source:
            state["source_zone"] = source
        if destination:
            state["destination_zone"] = destination

        return state

    # ------------------------------------------------------------------ #
    # Intake
    # ------------------------------------------------------------------ #
    def create_task(self, payload: Dict[str, Any], internal: bool = False) -> Task:
        twin = self.twin
        events, logger = twin.events, twin.logger

        raw_type = str(payload.get("type", "")).upper().strip()
        try:
            task_type = TaskType(raw_type)
        except ValueError:
            raise ValueError(f"Unknown task type '{payload.get('type')}'")

        priority_raw = str(payload.get("priority", "NORMAL")).upper().strip()
        try:
            priority = Priority(priority_raw)
        except ValueError:
            raise ValueError(f"Unknown priority '{payload.get('priority')}'")

        robot_spec = payload.get("robot_id") or payload.get("robot") or "AUTO"
        robot_spec = str(robot_spec).strip()
        if robot_spec.upper() != "AUTO":
            robot = twin.find_robot(robot_spec)
            robot_spec = robot.id if robot else robot_spec
        else:
            robot_spec = "AUTO"

        box_spec = payload.get("box_id") or payload.get("box")
        if box_spec:
            box = twin.find_box(str(box_spec))
            box_spec = box.id if box else str(box_spec)

        # BATCH_DELIVER's list of boxes (see TaskType.BATCH_DELIVER) —
        # each entry resolved by id/name the same way box_spec is above.
        box_ids_spec: List[str] = []
        for raw in payload.get("box_ids") or []:
            box = twin.find_box(str(raw))
            box_ids_spec.append(box.id if box else str(raw))

        second_operator_spec = payload.get("second_operator_id") or payload.get("second_operator")
        if second_operator_spec:
            second_operator_spec = str(second_operator_spec).strip()
            if second_operator_spec.upper() != "AUTO":
                second_operator = twin.find_operator(second_operator_spec)
                second_operator_spec = second_operator.id if second_operator else second_operator_spec
            else:
                second_operator_spec = "AUTO"
        elif payload.get("dual_signoff"):
            second_operator_spec = "AUTO"

        agent_spec = payload.get("agent_id") or payload.get("agent") or "AUTO"
        agent_spec = str(agent_spec).strip()
        if agent_spec.upper() != "AUTO":
            agent = twin.find_agent(agent_spec)
            agent_spec = agent.id if agent else agent_spec
        else:
            agent_spec = "AUTO"

        operator_spec = payload.get("operator_id") or payload.get("operator") or "AUTO"
        operator_spec = str(operator_spec).strip()
        if operator_spec.upper() != "AUTO":
            operator = twin.find_operator(operator_spec)
            operator_spec = operator.id if operator else operator_spec
        else:
            operator_spec = "AUTO"

        with self._lock:
            self._sequence += 1
            task = Task(
                task_id=twin.ids.next("task"),
                task_type=task_type,
                robot_id=robot_spec,
                box_id=box_spec,
                box_ids=box_ids_spec,
                source=payload.get("source"),
                destination=payload.get("destination"),
                priority=priority,
                internal=internal,
                sequence=self._sequence,
                agent_id=agent_spec,
                operator_id=operator_spec,
                required_certification=payload.get("required_certification"),
                second_operator_id=second_operator_spec,
                dual_signoff=bool(payload.get("dual_signoff")),
            )
            self.tasks[task.id] = task

        task.record(TaskStatus.CREATED, f"Task created ({task.type.value}, {priority.value} priority)")
        events.emit(
            EventType.TASK_CREATED,
            f"{task.id} created: {task.type.value} — {task.summary()}",
            category=LogCategory.USER if not internal else LogCategory.TASK,
            task_id=task.id,
            robot_id=task.robot_id,
            box_id=task.box_id,
            # The full task definition, not just priority/requested_robot —
            # this is "the task" input an eval reads (see
            # evals/generate_tests_groq.py::_extract_task) so it doesn't
            # have to regex the free-text message to know what was asked
            # for.
            data={
                "priority": priority.value,
                "requested_robot": task.requested_robot,
                "requested_agent": task.requested_agent,
                "requested_operator": task.requested_operator,
                "required_certification": task.required_certification,
                "task_type": task.type.value,
                "box_id": task.box_id,
                "box_ids": list(task.box_ids),
                "source": task.source,
                "destination": task.destination,
                "summary": task.summary(),
            },
        )

        # Immediate control tasks bypass the queue.
        if task_type in self.IMMEDIATE:
            self._run_immediate(task)
            return task

        task.record(TaskStatus.VALIDATING, "Validation started")
        logger.info(LogCategory.TASK, f"{task.id} validation started", task_id=task.id)
        ok, error = self.validate(task)
        if not ok:
            self.fail_task(task, error or "Validation failed")
            return task

        events.emit(
            EventType.TASK_VALIDATED,
            f"{task.id} validated successfully",
            category=LogCategory.TASK,
            task_id=task.id,
        )

        # AGENT_INSPECTION / HUMAN_INSPECTION resolve here, immediately —
        # no robot dispatch queue involved. Critically, this check has to
        # come *before* the task is ever marked PLANNING below: that's the
        # status pending()/dispatch() key off, and the simulator's tick
        # loop runs on its own thread — if an instant task were marked
        # PLANNING even briefly, a concurrent tick could race in, try to
        # plan it as a normal robot task (there's no movement plan for
        # these types), and fail it out from under _run_instant(). Caught
        # by actually running this against the live app, not a
        # single-threaded script — see TRUST_LAYER.md.
        if task_type in self.INSTANT:
            self._run_instant(task)
            return task

        task.record(TaskStatus.PLANNING, "Validated — waiting for a robot")
        return task

    def _run_immediate(self, task: Task) -> None:
        twin = self.twin
        robot = twin.find_robot(task.robot_id or task.requested_robot)
        if robot is None:
            self.fail_task(task, f"Robot '{task.requested_robot}' does not exist")
            return
        task.robot_id = robot.id
        if task.type == TaskType.STOP_ROBOT:
            twin.stop_robot(robot.id, reason="task")
        else:
            twin.resume_robot(robot.id, reason="task")
        task.action_index = 0
        task.actions = [Action(ActionType.COMPLETE, task.type.value.replace("_", " ").title(), done=True)]
        task.started_at = now_iso()
        self.complete_task(task, f"{task.type.value} applied to {robot.name}")

    def _emit_second_signoff(self, task: Task, subject: str) -> None:
        """The second signature for a `dual_signoff` OPERATOR_APPROVAL /
        OPERATOR_MAINTENANCE_SIGNOFF task (see the `dual_signoff` block in
        validate()) — a no-op for every other task."""
        if not task.dual_signoff or not task.second_operator_id:
            return
        twin = self.twin
        second = twin.find_operator(task.second_operator_id)
        if second is None:
            return
        second.completed_tasks += 1
        second.touch()
        twin.events.emit(
            EventType.OPERATOR_APPROVED,
            f"{second.name} co-signed {subject} (second sign-off)",
            category=LogCategory.TASK,
            task_id=task.id,
            data={
                "operator_id": second.id,
                "certifications": list(second.certifications),
                "co_signer": True,
            },
        )

    def _run_instant(self, task: Task) -> None:
        """AGENT_INSPECTION / HUMAN_INSPECTION resolve here, in one call —
        there's no physical movement to simulate, just a cognitive or
        administrative step. Reuses start_task()/complete_task() so the
        state snapshot and event trail look exactly like a robot task's,
        just compressed into one call instead of spread across simulator
        ticks. Certification/model eligibility is NOT checked here (only
        existence, in validate()) — that's graded after the fact by
        eval_engine.check_entities_valid, same as a robot's firmware."""
        twin = self.twin
        task.actions = [Action(ActionType.COMPLETE, "Complete task", done=True)]
        task.action_index = 1
        self.start_task(task)

        if task.type == TaskType.AGENT_INSPECTION:
            agent = twin.find_agent(task.agent_id) if task.agent_id else None
            if agent is not None:
                agent.completed_tasks += 1
                agent.touch()
            twin.events.emit(
                EventType.AGENT_RECOMMENDATION,
                f"{agent.name if agent else task.agent_id} reviewed current "
                f"conditions and produced a recommendation",
                category=LogCategory.TASK,
                task_id=task.id,
                data={
                    "agent_id": task.agent_id,
                    "model_version": agent.model_version if agent else None,
                },
            )
            self.complete_task(task, "Agent inspection completed")
            return

        if task.type == TaskType.HUMAN_INSPECTION:
            operator = twin.find_operator(task.operator_id) if task.operator_id else None
            if operator is not None:
                operator.completed_tasks += 1
                operator.touch()
            twin.events.emit(
                EventType.OPERATOR_APPROVED,
                f"{operator.name if operator else task.operator_id} approved the inspection",
                category=LogCategory.TASK,
                task_id=task.id,
                data={
                    "operator_id": task.operator_id,
                    "certifications": list(operator.certifications) if operator else [],
                },
            )
            self.complete_task(task, "Human inspection completed")
            return

        if task.type == TaskType.AGENT_REPLAN:
            agent = twin.find_agent(task.agent_id) if task.agent_id else None
            # Every other currently-queued task (not this AGENT_REPLAN
            # task itself, which never reaches the queue — it's INSTANT).
            pending = [t for t in self.pending() if t.id != task.id]
            recommendations = []
            for other in pending:
                scored, _ = self._score_candidates(other)
                if scored:
                    recommendations.append({"task_id": other.id, "robot_id": scored[0][1].id,
                                             "robot": scored[0][1].name, "score": scored[0][2]["score"]})
            if recommendations:
                breakdown = ", ".join(f"{r['task_id']}→{r['robot']}" for r in recommendations)
                message = f"reviewed {len(pending)} pending task(s) and recommends: {breakdown}"
            else:
                message = "reviewed the queue — no pending tasks currently need reassignment"
            # Optional live narration over this same computed data — see
            # backend/llm.py. Off unless CONFIG["AGENT_LLM_ENABLED"] is
            # true AND a Groq key is configured; falls back to `message`
            # on any failure, so this never changes what actually happened.
            narrated = narrate(
                f"A warehouse dispatcher AI reviewed {len(pending)} pending task(s) and "
                f"computed these robot recommendations: {breakdown if recommendations else 'none needed'}. "
                f"Phrase this as a short recommendation to a supervisor."
            ) if recommendations else None
            final_message = narrated or message
            if agent is not None:
                agent.completed_tasks += 1
                agent.touch()
            twin.events.emit(
                EventType.AGENT_RECOMMENDATION,
                f"{agent.name if agent else task.agent_id} {final_message}",
                category=LogCategory.TASK,
                task_id=task.id,
                data={
                    "agent_id": task.agent_id,
                    "model_version": agent.model_version if agent else None,
                    "pending_reviewed": len(pending),
                    "recommendations": recommendations,
                    "llm_generated": narrated is not None,
                },
            )
            self.complete_task(task, "Agent replan review completed")
            return

        if task.type == TaskType.AGENT_AUDIT:
            agent = twin.find_agent(task.agent_id) if task.agent_id else None
            # WARNING and up (i.e. WARNING/ERROR/CRITICAL) among the most
            # recent 200 records still in the in-memory ring buffer — the
            # same data the "Live system logs" panel filters to.
            flagged = twin.logger.query(level="WARNING", limit=200)
            error_count = sum(1 for r in flagged if r["level"] in ("ERROR", "CRITICAL"))
            warning_count = len(flagged) - error_count
            ci = twin.ci_result
            ci_note = f"last CI run {ci.get('status', 'UNKNOWN')} ({ci.get('pipeline')})" if ci else "no CI run yet"
            message = (
                f"audited the last {len(flagged)} warning-and-above log record(s) "
                f"({error_count} error/critical, {warning_count} warning) — {ci_note}"
            )
            narrated = narrate(
                f"A warehouse monitoring AI audited the last {len(flagged)} warning-and-above log "
                f"records: {error_count} error/critical, {warning_count} warning. {ci_note}. "
                f"Phrase this as a short health summary for a supervisor."
            )
            final_message = narrated or message
            if agent is not None:
                agent.completed_tasks += 1
                agent.touch()
            twin.events.emit(
                EventType.AGENT_RECOMMENDATION,
                f"{agent.name if agent else task.agent_id} {final_message}",
                category=LogCategory.TASK,
                task_id=task.id,
                data={
                    "agent_id": task.agent_id,
                    "model_version": agent.model_version if agent else None,
                    "error_count": error_count,
                    "warning_count": warning_count,
                    "ci_status": ci.get("status") if ci else None,
                    "llm_generated": narrated is not None,
                },
            )
            self.complete_task(task, "Agent audit completed")
            return

        if task.type == TaskType.OPERATOR_APPROVAL:
            operator = twin.find_operator(task.operator_id) if task.operator_id else None
            # `robot_id` is optional context only — deliberately not
            # existence- or eligibility-checked here (see TaskType.
            # OPERATOR_APPROVAL's docstring in models.py): the point of
            # this task type is authorizing a robot that may currently be
            # ineligible, so validate() never re-checks robot_eligibility
            # for it, only that the operator holds the certification.
            robot = twin.find_robot(task.robot_id) if task.robot_id else None
            subject = robot.name if robot else "the pending action"
            if operator is not None:
                operator.completed_tasks += 1
                operator.touch()
            twin.events.emit(
                EventType.OPERATOR_APPROVED,
                f"{operator.name if operator else task.operator_id} approved {subject} for continued operation",
                category=LogCategory.TASK,
                task_id=task.id,
                robot_id=robot.id if robot else None,
                data={
                    "operator_id": task.operator_id,
                    "certifications": list(operator.certifications) if operator else [],
                    "robot_id": robot.id if robot else None,
                },
            )
            self._emit_second_signoff(task, subject)
            self.complete_task(task, "Operator approval completed")
            return

        if task.type == TaskType.OPERATOR_MAINTENANCE_SIGNOFF:
            operator = twin.find_operator(task.operator_id) if task.operator_id else None
            robot = twin.find_robot(task.robot_id) if task.robot_id else None
            if robot is not None:
                approved = robot.firmware_version in APPROVED_FIRMWARE_VERSIONS
                firmware_note = (
                    f"firmware {robot.firmware_version} is on the approved baseline"
                    if approved else
                    f"firmware {robot.firmware_version} is NOT on the approved baseline — flagged for update"
                )
                subject = f"{robot.name} ({firmware_note})"
            else:
                subject = "the maintenance item"
            if operator is not None:
                operator.completed_tasks += 1
                operator.touch()
            twin.events.emit(
                EventType.OPERATOR_APPROVED,
                f"{operator.name if operator else task.operator_id} signed off maintenance on {subject}",
                category=LogCategory.TASK,
                task_id=task.id,
                robot_id=robot.id if robot else None,
                data={
                    "operator_id": task.operator_id,
                    "certifications": list(operator.certifications) if operator else [],
                    "robot_id": robot.id if robot else None,
                    "firmware_version": robot.firmware_version if robot else None,
                },
            )
            self._emit_second_signoff(task, subject)
            # A completed maintenance sign-off is what actually resets the
            # robot's wear counters — see backend/maintenance.py and
            # Robot.perform_maintenance.
            if robot is not None:
                robot.perform_maintenance()
                twin.events.emit(
                    EventType.MAINTENANCE_PERFORMED,
                    f"{robot.name} maintenance counters reset by {operator.name if operator else task.operator_id}",
                    category=LogCategory.ROBOT,
                    task_id=task.id,
                    robot_id=robot.id,
                )
            self.complete_task(task, "Maintenance sign-off completed")
            return

    # ------------------------------------------------------------------ #
    # Validation
    # ------------------------------------------------------------------ #
    def validate(self, task: Task) -> Tuple[bool, Optional[str]]:
        twin = self.twin
        planner = twin.planner

        # AI agent — existence/availability, AND eligibility (approved
        # model_version) are both gated here. An ineligible agent used to
        # only get flagged after the fact by eval_engine.check_entities_
        # valid; it's now a hard pre-execution block too, using the exact
        # same rule (see backend/eligibility.py) so the gate and the grade
        # can never disagree.
        needs_agent = task.type in (
            TaskType.AGENT_INSPECTION,
            TaskType.AGENT_REPLAN,
            TaskType.AGENT_AUDIT,
            TaskType.MIXED_MAINTENANCE_MISSION,
        )
        if needs_agent:
            if task.requested_agent != "AUTO":
                agent = twin.find_agent(task.agent_id or task.requested_agent)
                if agent is None:
                    return False, f"Agent '{task.requested_agent}' does not exist"
                reason = agent_eligibility(agent.status.value, agent.model_version)
                if reason:
                    return False, f"{agent.name} {reason}"
            else:
                agent = next(
                    (
                        a for a in twin.agents.values()
                        if a.is_available and agent_eligibility(a.status.value, a.model_version) is None
                    ),
                    None,
                )
                if agent is None:
                    return False, "No eligible AI agent available to handle this task"
            task.agent_id = agent.id

        # Human operator — same principle: existence/availability AND
        # eligibility (off-duty status, missing certification) are both
        # gated here now, not graded after the fact alone.
        needs_operator = task.type in (
            TaskType.HUMAN_INSPECTION,
            TaskType.OPERATOR_APPROVAL,
            TaskType.OPERATOR_MAINTENANCE_SIGNOFF,
            TaskType.MIXED_MAINTENANCE_MISSION,
        )
        if needs_operator:
            if task.requested_operator != "AUTO":
                operator = twin.find_operator(task.operator_id or task.requested_operator)
                if operator is None:
                    return False, f"Operator '{task.requested_operator}' does not exist"
                reason = operator_eligibility(
                    operator.status.value, operator.certifications, task.required_certification
                )
                if reason:
                    return False, f"{operator.name} {reason}"
            else:
                operator = next(
                    (
                        o for o in twin.operators.values()
                        if o.is_available
                        and operator_eligibility(
                            o.status.value, o.certifications, task.required_certification
                        ) is None
                    ),
                    None,
                )
                if operator is None:
                    return False, "No eligible operator available to handle this task"
            task.operator_id = operator.id

            # Two-person sign-off — opt-in per task (`dual_signoff: true`
            # or an explicit `second_operator_id`), not a system-wide
            # policy. The second operator needs the same certification as
            # the first and must be a genuinely different person.
            if task.dual_signoff:
                if task.requested_second_operator and task.requested_second_operator != "AUTO":
                    second = twin.find_operator(task.second_operator_id or task.requested_second_operator)
                    if second is None:
                        return False, f"Second operator '{task.requested_second_operator}' does not exist"
                    if second.id == operator.id:
                        return False, "The second sign-off must be a different operator from the first"
                    reason = operator_eligibility(
                        second.status.value, second.certifications, task.required_certification
                    )
                    if reason:
                        return False, f"{second.name} {reason}"
                else:
                    second = next(
                        (
                            o for o in twin.operators.values()
                            if o.id != operator.id and o.is_available
                            and operator_eligibility(
                                o.status.value, o.certifications, task.required_certification
                            ) is None
                        ),
                        None,
                    )
                    if second is None:
                        return False, "No second eligible operator available for dual sign-off"
                task.second_operator_id = second.id

        # AGENT_INSPECTION / HUMAN_INSPECTION need nothing else — no
        # robot, no box, no destination.
        if task.type in self.INSTANT:
            return True, None

        # Robot — existence/availability AND eligibility (unapproved
        # firmware, critical battery) are both gated here now. AUTO robot
        # selection itself happens later in select_robot(), which applies
        # the same eligibility filter when scoring candidates.
        robot = None
        if task.requested_robot != "AUTO":
            robot = twin.find_robot(task.robot_id or task.requested_robot)
            if robot is None:
                return False, f"Robot '{task.requested_robot}' does not exist"
            task.robot_id = robot.id
            # battery=None: a critical battery is deliberately NOT part of
            # the hard gate — the planner already has a real recovery
            # path for it (a prepended recharge detour, see
            # TaskPlanner.plan()), so blocking the task outright here
            # would break that feature. eval_engine.check_entities_valid
            # still grades battery after the fact, since a robot that's
            # still critical once the task actually starts is worth
            # flagging even though it wasn't worth refusing up front.
            reason = robot_eligibility(
                robot.status.value, robot.firmware_version, battery=None,
                task_type=task.type.value, allowed_task_types=robot.allowed_task_types,
            )
            if reason:
                return False, f"{robot.name} {reason}"
        elif not twin.robots:
            return False, "No robots exist in the warehouse"

        # Box
        needs_box = task.type in (
            TaskType.PICK_AND_DELIVER,
            TaskType.PICK_BOX,
            TaskType.DELIVER_BOX,
            TaskType.MOVE_BOX,
        )
        if needs_box:
            if not task.box_id:
                return False, "This task type needs a box"
            box = twin.find_box(task.box_id)
            if box is None:
                return False, f"Box '{task.box_id}' does not exist"
            task.box_id = box.id
            conflict = self.active_task_for_box(box.id, exclude=task.id)
            if conflict is not None:
                return False, f"{box.name} is already reserved by {conflict.id}"
            if box.assigned_robot and (robot is None or box.assigned_robot != robot.id):
                return False, f"{box.name} is held by another robot"

        if task.type == TaskType.BATCH_DELIVER:
            if len(task.box_ids) < 2:
                return False, "BATCH_DELIVER needs at least two box_ids (use PICK_AND_DELIVER for one)"
            resolved_ids: List[str] = []
            for raw_id in task.box_ids:
                box = twin.find_box(raw_id)
                if box is None:
                    return False, f"Box '{raw_id}' does not exist"
                conflict = self.active_task_for_box(box.id, exclude=task.id)
                if conflict is not None:
                    return False, f"{box.name} is already reserved by {conflict.id}"
                if box.assigned_robot and (robot is None or box.assigned_robot != robot.id):
                    return False, f"{box.name} is held by another robot"
                resolved_ids.append(box.id)
            if len(set(resolved_ids)) != len(resolved_ids):
                return False, "BATCH_DELIVER box_ids must all be different boxes"
            task.box_ids = resolved_ids

        # Destination
        needs_destination = task.type in (
            TaskType.PICK_AND_DELIVER,
            TaskType.DELIVER_BOX,
            TaskType.MOVE_BOX,
            TaskType.MOVE_ROBOT,
            TaskType.MIXED_MAINTENANCE_MISSION,
            TaskType.BATCH_DELIVER,
        )
        if needs_destination:
            if not task.destination:
                return False, "This task type needs a destination"
            origin = robot.position if robot else next(iter(twin.robots.values())).position
            try:
                planner.resolve_target(task.destination, origin)
            except PlanningError as exc:
                return False, str(exc)

        # Source (optional, but if given it must exist)
        if task.source:
            try:
                origin = robot.position if robot else next(iter(twin.robots.values())).position
                planner.resolve_target(task.source, origin)
            except PlanningError as exc:
                return False, f"Invalid source: {exc}"

        # Reachability
        if task.type != TaskType.CHARGE_ROBOT and robot is not None:
            try:
                target_cell, _ = self._primary_target(task, robot.position)
            except PlanningError as exc:
                return False, str(exc)
            if target_cell is not None and not twin.navigation.path_exists(robot.position, target_cell):
                return False, f"No path from {robot.name} to ({target_cell[0]},{target_cell[1]})"

        return True, None

    def _primary_target(self, task: Task, origin: Cell) -> Tuple[Optional[Cell], str]:
        planner = self.twin.planner
        if task.type in (TaskType.PICK_AND_DELIVER, TaskType.PICK_BOX, TaskType.MOVE_BOX):
            return planner.resolve_target(task.box_id, origin)
        if task.type == TaskType.BATCH_DELIVER:
            return planner.resolve_target(task.box_ids[0], origin) if task.box_ids else (None, "")
        if task.type == TaskType.DELIVER_BOX:
            robot = self.twin.find_robot(task.robot_id) if task.robot_id else None
            if robot is not None and robot.carrying_box == task.box_id:
                return planner.resolve_target(task.destination, origin)
            return planner.resolve_target(task.box_id, origin)
        if task.type in (TaskType.MOVE_ROBOT, TaskType.MIXED_MAINTENANCE_MISSION):
            return planner.resolve_target(task.destination, origin)
        if task.type == TaskType.CHARGE_ROBOT:
            return planner.resolve_target("charging_station", origin)
        return None, ""

    # ------------------------------------------------------------------ #
    # Queue & assignment
    # ------------------------------------------------------------------ #
    def pending(self) -> List[Task]:
        with self._lock:
            queue = [t for t in self.tasks.values() if t.status == TaskStatus.PLANNING]
        queue.sort(key=lambda t: (-PRIORITY_RANK[t.priority], t.sequence))
        return queue

    def active(self) -> List[Task]:
        with self._lock:
            return [t for t in self.tasks.values() if t.is_active]

    def active_task_for_box(self, box_id: str, exclude: Optional[str] = None) -> Optional[Task]:
        with self._lock:
            for task in self.tasks.values():
                if task.id == exclude:
                    continue
                if task.box_id != box_id and box_id not in task.box_ids:
                    continue
                if task.status in (TaskStatus.PLANNING, TaskStatus.VALIDATING) or task.is_active:
                    return task
        return None

    def task_for_robot(self, robot_id: str) -> Optional[Task]:
        with self._lock:
            for task in self.tasks.values():
                if task.robot_id == robot_id and task.is_active:
                    return task
        return None

    def queued_count_for_robot(self, robot_id: str) -> int:
        with self._lock:
            return sum(
                1
                for t in self.tasks.values()
                if t.robot_id == robot_id and t.status == TaskStatus.PLANNING
            )

    def _score_candidates(self, task: Task) -> Tuple[List[Tuple[float, Any, Dict[str, Any]]], str]:
        """Score every available, eligible robot for `task`, best (lowest
        score) first. The one piece of AUTO assignment logic, shared by
        select_robot() (which actually assigns the winner) and
        AGENT_REPLAN's read-only recommendation (which only reports it)."""
        twin = self.twin
        try:
            target_cell, target_label = self._primary_target(task, next(iter(twin.robots.values())).position)
        except (PlanningError, StopIteration):
            target_cell, target_label = None, ""

        scored: List[Tuple[float, Any, Dict[str, Any]]] = []
        for robot in twin.robots.values():
            if not robot.is_available or robot.is_halted:
                continue
            if robot_eligibility(
                robot.status.value, robot.firmware_version, battery=None,
                task_type=task.type.value, allowed_task_types=robot.allowed_task_types,
            ) is not None:
                # Ineligible (bad status / unapproved firmware / not
                # configured for this task type) — skip it the same way
                # validate() would block it if it had been requested by
                # name. Battery is excluded here too — see the matching
                # comment in validate() above.
                continue
            distance = None
            if target_cell is not None:
                distance = twin.navigation.distance(robot.position, target_cell)
                if distance is None:
                    continue
            distance = distance if distance is not None else 0
            battery_penalty = max(0.0, (100.0 - robot.battery) * 0.25)
            workload_penalty = self.queued_count_for_robot(robot.id) * 10
            status_penalty = 0 if robot.status.value == "IDLE" else 15
            score = distance + battery_penalty + workload_penalty + status_penalty
            scored.append(
                (
                    score,
                    robot,
                    {
                        "robot": robot.name,
                        "distance": distance,
                        "battery": robot.battery,
                        "workload": workload_penalty / 10,
                        "score": round(score, 2),
                    },
                )
            )
        scored.sort(key=lambda item: item[0])
        return scored, target_label

    def select_robot(self, task: Task) -> Optional[Any]:
        """Choose the best available robot for an AUTO task and log the decision."""
        scored, target_label = self._score_candidates(task)
        if not scored:
            return None
        winner = scored[0]
        breakdown = ", ".join(
            "{0}=score {1}".format(entry[2]["robot"], entry[2]["score"]) for entry in scored
        )
        self.twin.logger.info(
            LogCategory.PLANNER,
            f"AUTO assignment for {task.id} → {winner[1].name} ({breakdown})",
            task_id=task.id,
            robot_id=winner[1].id,
            data={"candidates": [c[2] for c in scored], "target": target_label},
        )
        return winner[1]

    def assign(self, task: Task, robot: Any) -> bool:
        """Attach a robot to a task and build its plan. Returns success."""
        twin = self.twin
        task.robot_id = robot.id
        try:
            actions = twin.planner.plan(task, robot)
        except PlanningError as exc:
            self.fail_task(task, str(exc))
            return False

        task.actions = actions
        task.action_index = 0
        task.record(TaskStatus.ASSIGNED, f"Assigned to {robot.name} with {len(actions)} actions")
        robot.current_task = task.id
        robot.set_status(twin.RobotStatus.PLANNING)
        robot.wait_ticks = 0

        reserve_ids = task.box_ids if task.type == TaskType.BATCH_DELIVER else ([task.box_id] if task.box_id else [])
        for box_id in reserve_ids:
            box = twin.find_box(box_id)
            if box is not None and box.status.value in ("STORED", "DELIVERED"):
                previous = box.set_status(twin.BoxStatus.RESERVED)
                box.assigned_robot = robot.id
                box.assigned_task = task.id
                twin.events.emit(
                    EventType.BOX_RESERVED,
                    f"{box.name} reserved for {task.id} ({previous.value} → RESERVED)",
                    category=LogCategory.BOX,
                    task_id=task.id,
                    robot_id=robot.id,
                    box_id=box.id,
                )

        twin.events.emit(
            EventType.TASK_ASSIGNED,
            f"{task.id} assigned to {robot.name}",
            category=LogCategory.TASK,
            task_id=task.id,
            robot_id=robot.id,
            box_id=task.box_id,
        )
        twin.events.emit(
            EventType.TASK_PLANNED,
            f"{task.id} planned: {len(actions)} actions",
            category=LogCategory.PLANNER,
            task_id=task.id,
            robot_id=robot.id,
            data={"actions": [a.description for a in actions]},
        )
        return True

    def dispatch(self) -> int:
        """Match queued tasks to free robots. Returns the number assigned."""
        assigned = 0
        for task in self.pending():
            if task.requested_robot != "AUTO":
                robot = self.twin.find_robot(task.robot_id or task.requested_robot)
                if robot is None:
                    self.fail_task(task, f"Robot '{task.requested_robot}' disappeared")
                    continue
                if robot.is_halted or not robot.is_available:
                    continue
            else:
                robot = self.select_robot(task)
                if robot is None:
                    continue
            if self.assign(task, robot):
                assigned += 1
        return assigned

    # ------------------------------------------------------------------ #
    # Lifecycle transitions
    # ------------------------------------------------------------------ #
    def start_task(self, task: Task) -> None:
        task.started_at = task.started_at or now_iso()
        task.record(TaskStatus.IN_PROGRESS, "Execution started")

        # Mirrors the reference document's worked example: the agent
        # recommends the physical step *before* the robot actually takes
        # it — logged here, right as execution begins, not gating it.
        if task.type == TaskType.MIXED_MAINTENANCE_MISSION and task.agent_id:
            agent = self.twin.find_agent(task.agent_id)
            if agent is not None:
                self.twin.events.emit(
                    EventType.AGENT_RECOMMENDATION,
                    f"{agent.name} recommends dispatching a robot to "
                    f"inspect {task.destination or 'the target zone'}",
                    category=LogCategory.TASK,
                    task_id=task.id,
                    robot_id=task.robot_id,
                    data={"agent_id": agent.id, "model_version": agent.model_version},
                )

        self.twin.events.emit(
            EventType.TASK_STARTED,
            f"{task.id} started",
            category=LogCategory.TASK,
            task_id=task.id,
            robot_id=task.robot_id,
            # "Before" snapshot — robot and box are both resolved, nothing
            # has moved yet.
            data={"state": self.snapshot_state(task)},
        )

    def set_status(self, task: Task, status: TaskStatus, message: str) -> None:
        if task.status == status:
            return
        task.record(status, message)
        self.twin.events.emit(
            EventType.TASK_PROGRESS,
            f"{task.id} → {status.value}: {message}",
            category=LogCategory.TASK,
            level=LogLevel.DEBUG,
            task_id=task.id,
            robot_id=task.robot_id,
            data={"progress": task.progress},
        )

    def complete_task(self, task: Task, message: str = "Task completed") -> None:
        twin = self.twin
        task.completed_at = now_iso()
        task.record(TaskStatus.COMPLETED, message)
        robot = twin.find_robot(task.robot_id) if task.robot_id else None
        if robot is not None:
            robot.completed_tasks += 1
            if robot.current_task == task.id:
                robot.current_task = None
                robot.clear_path()
                if not robot.is_halted and robot.status != twin.RobotStatus.CHARGING:
                    robot.set_status(twin.RobotStatus.IDLE)

        # The operator's sign-off — logged, not gating: the robot's
        # physical inspection already succeeded by the time this runs,
        # whether or not the operator was actually qualified to approve
        # it. That mismatch is exactly what eval_engine.check_entities_valid
        # catches after the fact, same as the robot firmware check.
        if task.type == TaskType.MIXED_MAINTENANCE_MISSION and task.operator_id:
            operator = twin.find_operator(task.operator_id)
            if operator is not None:
                operator.completed_tasks += 1
                operator.touch()
                qualified = (
                    not task.required_certification
                    or task.required_certification in operator.certifications
                )
                twin.events.emit(
                    EventType.OPERATOR_APPROVED if qualified else EventType.OPERATOR_REJECTED,
                    (
                        f"{operator.name} approved the inspection outcome"
                        if qualified else
                        f"{operator.name} signed off, but does not hold the "
                        f"required '{task.required_certification}' certification"
                    ),
                    category=LogCategory.TASK,
                    task_id=task.id,
                    data={
                        "operator_id": operator.id,
                        "certifications": list(operator.certifications),
                        "required_certification": task.required_certification,
                    },
                )

        twin.statistics["completed_tasks"] += 1
        twin.events.emit(
            EventType.TASK_COMPLETED,
            f"{task.id} COMPLETED — {message}",
            category=LogCategory.TASK,
            task_id=task.id,
            robot_id=task.robot_id,
            box_id=task.box_id,
            # "After" snapshot — did the world actually end up where the
            # task intended?
            data={"state": self.snapshot_state(task)},
        )
        twin.synchronize()

    def _release_boxes(self, task: Task, allow_status: Tuple[Any, ...]) -> None:
        """Un-reserve every box `task` was holding (task.box_ids for
        BATCH_DELIVER, else the single task.box_id) — shared by fail_task
        and cancel_task so BATCH_DELIVER doesn't strand its other boxes
        as permanently RESERVED when the task doesn't finish cleanly."""
        twin = self.twin
        ids = task.box_ids if task.type == TaskType.BATCH_DELIVER else ([task.box_id] if task.box_id else [])
        for box_id in ids:
            box = twin.find_box(box_id)
            if box is not None and box.assigned_task == task.id:
                box.assigned_robot = None
                box.assigned_task = None
                if box.status in allow_status:
                    box.set_status(twin.BoxStatus.STORED)

    def fail_task(self, task: Task, error: str) -> None:
        twin = self.twin
        task.error = error
        task.completed_at = now_iso()
        task.record(TaskStatus.FAILED, error)
        robot = twin.find_robot(task.robot_id) if task.robot_id else None
        if robot is not None:
            robot.failed_tasks += 1
            if robot.current_task == task.id:
                robot.current_task = None
                robot.clear_path()
                if not robot.is_halted:
                    robot.set_status(twin.RobotStatus.IDLE)
        self._release_boxes(task, allow_status=(twin.BoxStatus.RESERVED,))
        twin.statistics["failed_tasks"] += 1
        twin.events.emit(
            EventType.TASK_FAILED,
            f"{task.id} FAILED — {error}",
            category=LogCategory.TASK,
            level=LogLevel.ERROR,
            task_id=task.id,
            robot_id=task.robot_id,
            box_id=task.box_id,
            # "After" snapshot — a failure should mean nothing physically
            # moved; this is what lets an eval confirm that, not just
            # assume it.
            data={"state": self.snapshot_state(task)},
        )

    def cancel_task(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' does not exist")
        if task.is_terminal:
            raise ValueError(f"{task_id} already finished with status {task.status.value}")
        twin = self.twin
        task.record(TaskStatus.CANCELLED, "Cancelled by user")
        task.completed_at = now_iso()
        robot = twin.find_robot(task.robot_id) if task.robot_id else None
        if robot is not None and robot.current_task == task.id:
            robot.current_task = None
            robot.clear_path()
            if not robot.is_halted:
                robot.set_status(twin.RobotStatus.IDLE)
        self._release_boxes(task, allow_status=(twin.BoxStatus.RESERVED, twin.BoxStatus.PICKING))
        twin.events.emit(
            EventType.TASK_CANCELLED,
            f"{task.id} cancelled by user",
            category=LogCategory.USER,
            level=LogLevel.WARNING,
            task_id=task.id,
            robot_id=task.robot_id,
            # "After" snapshot — same reasoning as fail_task above.
            data={"state": self.snapshot_state(task)},
        )
        return task

    def pause_task(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' does not exist")
        if not task.is_active:
            raise ValueError(f"{task_id} is not running")
        task.record(TaskStatus.PAUSED, "Paused by user")
        robot = self.twin.find_robot(task.robot_id) if task.robot_id else None
        if robot is not None:
            robot.set_status(self.twin.RobotStatus.STOPPED)
        self.twin.events.emit(
            EventType.TASK_PAUSED,
            f"{task.id} paused",
            category=LogCategory.USER,
            level=LogLevel.WARNING,
            task_id=task.id,
            robot_id=task.robot_id,
        )
        return task

    def resume_task(self, task_id: str) -> Task:
        task = self.tasks.get(task_id)
        if task is None:
            raise KeyError(f"Task '{task_id}' does not exist")
        if task.status != TaskStatus.PAUSED:
            raise ValueError(f"{task_id} is not paused")
        task.record(TaskStatus.IN_PROGRESS, "Resumed by user")
        robot = self.twin.find_robot(task.robot_id) if task.robot_id else None
        if robot is not None:
            robot.set_status(self.twin.RobotStatus.PLANNING)
        self.twin.events.emit(
            EventType.TASK_RESUMED,
            f"{task.id} resumed",
            category=LogCategory.USER,
            task_id=task.id,
            robot_id=task.robot_id,
        )
        return task

    # ------------------------------------------------------------------ #
    def get(self, task_id: str) -> Optional[Task]:
        return self.tasks.get(task_id)

    def list_tasks(self, status: Optional[str] = None, limit: int = 200) -> List[Dict[str, Any]]:
        with self._lock:
            tasks = sorted(self.tasks.values(), key=lambda t: t.sequence, reverse=True)
        if status:
            tasks = [t for t in tasks if t.status.value == status.upper()]
        return [t.to_dict() for t in tasks[:limit]]

    def counts(self) -> Dict[str, int]:
        with self._lock:
            tasks = list(self.tasks.values())
        return {
            "total": len(tasks),
            "pending": sum(1 for t in tasks if t.status == TaskStatus.PLANNING),
            "active": sum(1 for t in tasks if t.is_active),
            "paused": sum(1 for t in tasks if t.status == TaskStatus.PAUSED),
            "completed": sum(1 for t in tasks if t.status == TaskStatus.COMPLETED),
            "failed": sum(1 for t in tasks if t.status == TaskStatus.FAILED),
            "cancelled": sum(1 for t in tasks if t.status == TaskStatus.CANCELLED),
        }

    def clear(self) -> None:
        with self._lock:
            self.tasks.clear()
            self._sequence = 0
