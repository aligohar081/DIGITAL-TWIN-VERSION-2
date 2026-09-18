"""Decision history search — a toy stand-in for the reference document's
"Decision Graph" (see TRUST_LAYER.md's Tier 2 breakdown), without a real
graph database: every logs/tasks/*.json file already IS the decision
history, and eval_engine.build_mission_record already turns one into a
single record naming exactly who/what was involved and whether it was
trustworthy. This module just filters that same collection by the
questions a person would actually ask — "every task Robot 1 ran while
its battery was under 20%", "every mission Sam signed off on" — without
building a graph database for a single-process demo.

    python -m backend.decision_graph --robot Robo-01 --battery-below 20
    python -m backend.decision_graph --operator Sam
"""
from __future__ import annotations

import argparse
import glob
import json
import os
from typing import Any, Dict, List, Optional, Sequence

from .eval_engine import build_mission_record, evaluate_events

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
TASKS_DIR = os.path.join(BASE_DIR, "logs", "tasks")


def _load_events(path: str) -> List[Dict[str, Any]]:
    with open(path, "r", encoding="utf-8") as fh:
        data = json.load(fh)
    return data if isinstance(data, list) else data.get("events", [])


def _matches(
    record: Dict[str, Any],
    robot: Optional[str],
    agent: Optional[str],
    operator: Optional[str],
    task_type: Optional[str],
    verdict: Optional[str],
    battery_below: Optional[float],
) -> bool:
    task = record.get("task") or {}
    entities = record.get("entities") or {}
    state_diff = record.get("state_diff") or {}

    if task_type and str(task.get("type", "")).upper() != task_type.upper():
        return False
    if verdict and str(record.get("verdict", "")).upper() != verdict.upper():
        return False

    def _name_or_id_matches(spec: Optional[str], entity: Optional[Dict[str, Any]], requested: Optional[str]) -> bool:
        if not spec:
            return True
        spec_low = spec.lower()
        if entity and (
            str(entity.get("id", "")).lower() == spec_low or str(entity.get("name", "")).lower() == spec_low
        ):
            return True
        return bool(requested) and str(requested).lower() == spec_low

    if not _name_or_id_matches(robot, entities.get("robot"), task.get("requested_robot")):
        return False
    if not _name_or_id_matches(agent, entities.get("agent"), task.get("requested_agent")):
        return False
    if not _name_or_id_matches(operator, entities.get("operator"), task.get("requested_operator")):
        return False

    if battery_below is not None:
        before = (state_diff.get("before") or {})
        battery = (before.get("robot") or {}).get("battery")
        if not isinstance(battery, (int, float)) or battery >= battery_below:
            return False

    return True


def flatten_record(record: Dict[str, Any]) -> Dict[str, Any]:
    """One flattened row from a mission record (see
    eval_engine.build_mission_record) — task id/type/verdict/robot/agent/
    operator/summary/reasons. Shared by the CSV/HTML mission reports
    (backend/app.py) and the chat agent's query_decisions tool
    (backend/agent_tools.py) so both read the same shape."""
    task = record.get("task", {})
    entities = record.get("entities", {})
    return {
        "task_id": task.get("task_id") or "?",
        "type": task.get("type") or "?",
        "verdict": record.get("verdict", "N/A"),
        "robot": (entities.get("robot") or {}).get("name") or task.get("requested_robot") or "-",
        "agent": (entities.get("agent") or {}).get("name") or task.get("requested_agent") or "-",
        "operator": (entities.get("operator") or {}).get("name") or task.get("requested_operator") or "-",
        "summary": task.get("summary") or task.get("message") or "",
        "reasons": "; ".join(record.get("reasons") or []),
    }


def query_decisions(
    robot: Optional[str] = None,
    agent: Optional[str] = None,
    operator: Optional[str] = None,
    task_type: Optional[str] = None,
    verdict: Optional[str] = None,
    battery_below: Optional[float] = None,
    tasks_dir: str = TASKS_DIR,
) -> List[Dict[str, Any]]:
    """Every mission record (see eval_engine.build_mission_record) in
    `tasks_dir` matching every given filter — all filters are ANDed, and
    an omitted filter matches everything."""
    results: List[Dict[str, Any]] = []
    for path in sorted(glob.glob(os.path.join(tasks_dir, "task_*.json"))):
        try:
            events = _load_events(path)
        except (OSError, json.JSONDecodeError):
            continue
        if not events:
            continue
        report = evaluate_events(events)
        record = build_mission_record(events, report)
        record["log_file"] = os.path.basename(path)
        if _matches(record, robot, agent, operator, task_type, verdict, battery_below):
            results.append(record)
    return results


def _main(argv: Optional[Sequence[str]] = None) -> int:
    parser = argparse.ArgumentParser(description="Search this project's own task decision history.")
    parser.add_argument("--robot", help="Robot id or name")
    parser.add_argument("--agent", help="Agent id or name")
    parser.add_argument("--operator", help="Operator id or name")
    parser.add_argument("--type", dest="task_type", help="Task type, e.g. PICK_AND_DELIVER")
    parser.add_argument("--verdict", help="PASS, WARN, or FAIL")
    parser.add_argument("--battery-below", type=float, help="Robot's battery was under this %% when the task started")
    args = parser.parse_args(argv)

    results = query_decisions(
        robot=args.robot, agent=args.agent, operator=args.operator,
        task_type=args.task_type, verdict=args.verdict, battery_below=args.battery_below,
    )
    if not results:
        print("No matching tasks.")
        return 0
    for record in results:
        task = record["task"]
        print(f"{task.get('task_id')}  {task.get('type')}  verdict={record.get('verdict', 'n/a')}  "
              f"— {task.get('summary') or task.get('message')}")
    print(f"\n{len(results)} matching task(s).")
    return 0


if __name__ == "__main__":  # pragma: no cover
    raise SystemExit(_main())
