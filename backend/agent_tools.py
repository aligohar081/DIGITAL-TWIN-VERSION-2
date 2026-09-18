"""The chat agent's tools — the action space backend/agent_chat.py's
tool-use loop is allowed to call.

Each tool is a thin, honest wrapper around something that already exists
elsewhere in this codebase (TaskManager.create_task, decision_graph.
query_decisions, eval_engine.evaluate_events, ...) — the agent never gets
a shortcut around the pre-execution eligibility gate or any other rule
the rest of the app already enforces; POST /api/tasks and this module's
create_task tool both end up calling exactly the same
TaskManager.create_task(). Every tool call is logged (LogCategory.TASK,
"[agent-chat]" prefix) so an autonomous action is exactly as traceable in
the log stream as a human clicking a button.
"""
from __future__ import annotations

from typing import Any, Dict, List, Optional

from .decision_graph import flatten_record, query_decisions
from .eval_engine import evaluate_events
from .models import LogCategory, TaskType, Priority

MAX_QUERY_RESULTS = 15

# Kept deliberately compact (this whole module's TOOLS list is resent on
# every single turn of the tool-use loop, uncached — see backend/
# agent_chat.py — and Groq's free tier is only 8000 tokens/minute, so a
# verbose schema is a real reliability problem, not just noise).
TASK_TYPE_GUIDE = (
    "PICK_AND_DELIVER:box+dest | MOVE_ROBOT:dest | PICK_BOX:box | "
    "DELIVER_BOX:box+dest | MOVE_BOX:box+dest(robot auto-picked) | "
    "BATCH_DELIVER:box_ids(2+)+dest | CHARGE_ROBOT:robot_id | "
    "STOP_ROBOT/RESUME_ROBOT:robot_id | AGENT_INSPECTION:agent_id | "
    "HUMAN_INSPECTION:operator_id(needs safety_inspection) | "
    "MIXED_MAINTENANCE_MISSION:dest(agent+robot+operator, needs electrical_safety) | "
    "AGENT_REPLAN:agent_id(reviews queue, doesn't act) | "
    "AGENT_AUDIT:agent_id(reviews recent logs/CI) | "
    "OPERATOR_APPROVAL:operator_id[+robot_id](needs safety_inspection) | "
    "OPERATOR_MAINTENANCE_SIGNOFF:operator_id[+robot_id](needs electrical_safety, resets wear)"
)


# --------------------------------------------------------------------------- #
# Tool schemas — OpenAI/Groq function-calling format
# --------------------------------------------------------------------------- #
def _opt(type_: Any, **rest: Any) -> Dict[str, Any]:
    """An OPTIONAL parameter's schema. Groq's tool-call validator is
    strict: some models fill every declared property, including the ones
    they have nothing to say, with a literal JSON null rather than
    omitting the key — a bare {"type": "string"} then rejects the whole
    call. Every non-required property below must go through this (or
    otherwise list "null" as an allowed type) so that's accepted."""
    types = type_ if isinstance(type_, list) else [type_]
    return {"type": types + ["null"], **rest}


TOOLS: List[Dict[str, Any]] = [
    {
        "type": "function",
        "function": {
            "name": "create_task",
            "description": (
                "Create a real task in the warehouse — this actually dispatches work, "
                "it is not a preview. Task types and what each needs: " + TASK_TYPE_GUIDE +
                " Robots/boxes/agents/operators may be given by name (e.g. 'Robo-01') or id; "
                "omit robot_id/agent_id/operator_id (or pass 'AUTO') to let the system choose."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "type": {"type": "string", "enum": [t.value for t in TaskType]},
                    "robot_id": _opt("string", description="Robot name or id, or 'AUTO'"),
                    "box_id": _opt("string", description="Box name or id (single-box task types)"),
                    "box_ids": _opt(
                        "array", items={"type": "string"},
                        description="Two or more box names/ids — BATCH_DELIVER only",
                    ),
                    "source": _opt("string", description="Named zone or 'x,y' coordinate"),
                    "destination": _opt("string", description="Named zone or 'x,y' coordinate"),
                    "priority": _opt("string", enum=[p.value for p in Priority]),
                    "agent_id": _opt("string", description="Agent name or id, or 'AUTO'"),
                    "operator_id": _opt("string", description="Operator name or id, or 'AUTO'"),
                    "dual_signoff": _opt("boolean", description="Require a second, different operator's sign-off"),
                    "second_operator_id": _opt("string", description="Explicit second signer; omit for AUTO"),
                },
                "required": ["type"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "cancel_task",
            "description": "Cancel a task that hasn't finished yet.",
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_task",
            "description": (
                "One task's status/progress/error/plan/recent events, plus its eval verdict "
                "(PASS/WARN/FAIL) if finished. Use for 'why did task_X fail/complete?'."
            ),
            "parameters": {
                "type": "object",
                "properties": {"task_id": {"type": "string"}},
                "required": ["task_id"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "list_entities",
            "description": "List all robots, boxes, agents, or operators and their current state.",
            "parameters": {
                "type": "object",
                "properties": {"kind": {"type": "string", "enum": ["robots", "boxes", "agents", "operators"]}},
                "required": ["kind"],
            },
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_fleet_load",
            "description": "Active + queued work per robot. Use for 'which robot is busiest/idle?'.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "get_statistics",
            "description": "Warehouse-wide counters: robots/boxes/tasks by state, collisions, avg battery.",
            "parameters": {"type": "object", "properties": {}},
        },
    },
    {
        "type": "function",
        "function": {
            "name": "query_decisions",
            "description": (
                "Search every graded task ever logged, filtered by any of robot/agent/operator/"
                "task type/verdict/battery_below. Use for 'show failed tasks', 'what has X done'."
            ),
            "parameters": {
                "type": "object",
                "properties": {
                    "robot": _opt("string"),
                    "agent": _opt("string"),
                    "operator": _opt("string"),
                    "task_type": _opt("string", enum=[t.value for t in TaskType]),
                    "verdict": _opt("string", enum=["PASS", "WARN", "FAIL"]),
                    "battery_below": _opt("number", description="0-100"),
                },
            },
        },
    },
]

TOOL_NAMES = {t["function"]["name"] for t in TOOLS}


# --------------------------------------------------------------------------- #
# Execution — each function takes (twin, args) and returns a small,
# JSON-serialisable dict; never raises (errors come back as {"error": ...}
# so the model can see and react to them, same as a real tool-use API).
# --------------------------------------------------------------------------- #
def _create_task(twin: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    payload = {k: v for k, v in args.items() if v is not None}
    try:
        task = twin.tasks.create_task(payload)
    except ValueError as exc:  # unknown task type / priority
        return {"error": str(exc)}
    twin.logger.info(
        LogCategory.TASK,
        f"[agent-chat] created {task.id} ({task.type.value}) — {task.status.value}",
        task_id=task.id,
    )
    # "error" only present when there actually is one — a consistent
    # contract across every tool here (its absence means success), rather
    # than a key that's sometimes None.
    out = {"task_id": task.id, "type": task.type.value, "status": task.status.value, "summary": task.summary()}
    if task.error:
        out["error"] = task.error
    return out


def _cancel_task(twin: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = args.get("task_id")
    if not task_id:
        return {"error": "task_id is required"}
    try:
        task = twin.tasks.cancel_task(task_id)
    except (KeyError, ValueError) as exc:
        return {"error": str(exc)}
    twin.logger.info(LogCategory.TASK, f"[agent-chat] cancelled {task.id}", task_id=task.id)
    return {"task_id": task.id, "status": task.status.value}


def _get_task(twin: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    task_id = args.get("task_id")
    task = twin.tasks.get(task_id) if task_id else None
    if task is None:
        return {"error": f"Task '{task_id}' does not exist"}
    out: Dict[str, Any] = {
        "task_id": task.id, "type": task.type.value, "status": task.status.value,
        "progress": task.progress, "summary": task.summary(), "error": task.error,
        "robot_id": task.robot_id, "authorization_flagged": task.authorization_flagged,
        "history": task.history[-8:],
    }
    events = twin.logger.get_task_logs(task_id)
    if events:
        report = evaluate_events(events, task_id=task_id)
        out["eval"] = {"verdict": report.verdict.value, "reasons": report.reasons}
    return out


def _list_entities(twin: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    kind = args.get("kind")
    if kind == "robots":
        return {"robots": [
            {
                "id": r.id, "name": r.name, "class": r.robot_class, "status": r.status.value,
                "battery": round(r.battery, 1), "position": {"x": r.position[0], "y": r.position[1]},
                "current_task": r.current_task, "allowed_task_types": r.allowed_task_types,
            }
            for r in twin.robots.values()
        ]}
    if kind == "boxes":
        return {"boxes": [
            {
                "id": b.id, "name": b.name, "status": b.status.value,
                "position": {"x": b.position[0], "y": b.position[1]},
                "destination": b.destination, "assigned_robot": b.assigned_robot,
            }
            for b in twin.boxes.values()
        ]}
    if kind == "agents":
        return {"agents": [
            {"id": a.id, "name": a.name, "model_version": a.model_version, "status": a.status.value}
            for a in twin.agents.values()
        ]}
    if kind == "operators":
        return {"operators": [
            {
                "id": o.id, "name": o.name, "role": o.role, "certifications": list(o.certifications),
                "status": o.status.value, "shift_start_hour": o.shift_start_hour,
                "shift_end_hour": o.shift_end_hour,
            }
            for o in twin.operators.values()
        ]}
    return {"error": "kind must be one of: robots, boxes, agents, operators"}


def _get_fleet_load(twin: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    rows = []
    for robot in twin.robots.values():
        queued = twin.tasks.queued_count_for_robot(robot.id)
        active = 1 if robot.current_task else 0
        rows.append({
            "robot": robot.name, "status": robot.status.value,
            "active_task": robot.current_task, "queued_tasks": queued,
            "workload": active + queued, "battery": round(robot.battery, 1),
        })
    rows.sort(key=lambda r: -r["workload"])
    return {"fleet_load": rows}


def _get_statistics(twin: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    return twin.statistics_snapshot()


def _query_decisions(twin: Any, args: Dict[str, Any]) -> Dict[str, Any]:
    records = query_decisions(
        robot=args.get("robot"), agent=args.get("agent"), operator=args.get("operator"),
        task_type=args.get("task_type"), verdict=args.get("verdict"),
        battery_below=args.get("battery_below"), tasks_dir=twin.logger.tasks_dir,
    )
    rows = [flatten_record(r) for r in records[:MAX_QUERY_RESULTS]]
    return {"matches": rows, "total_matches": len(records), "shown": len(rows)}


_HANDLERS = {
    "create_task": _create_task,
    "cancel_task": _cancel_task,
    "get_task": _get_task,
    "list_entities": _list_entities,
    "get_fleet_load": _get_fleet_load,
    "get_statistics": _get_statistics,
    "query_decisions": _query_decisions,
}


def execute_tool(twin: Any, name: str, args: Optional[Dict[str, Any]]) -> Dict[str, Any]:
    """Run one tool call by name. Never raises — a bad call comes back as
    {"error": ...} so the model sees it and can react (e.g. retry with a
    corrected argument), same as any real tool-use API."""
    handler = _HANDLERS.get(name)
    if handler is None:
        return {"error": f"Unknown tool '{name}'"}
    try:
        return handler(twin, args or {})
    except Exception as exc:  # a malformed/unexpected call must not crash the chat loop
        return {"error": f"{name} failed: {exc!r}"}
