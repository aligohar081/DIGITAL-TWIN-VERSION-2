"""Flask application: REST API + Server-Sent Events + static dashboard."""
from __future__ import annotations

import csv
import html
import io
import json
import os
import queue
import threading
import time
from typing import Any, Dict, Optional, Tuple

from flask import Flask, Response, jsonify, request, send_from_directory

from .agent_chat import ChatError, run_chat
from . import carbon_client
from .ci_engine import CIEngine
from .decision_graph import flatten_record, query_decisions
from .digital_twin import DigitalTwin
from .eval_engine import evaluate_events
from .event_system import Broadcaster
from .maintenance import maintenance_reason
from .models import CONFIG, LogCategory, Priority, SimulationStatus, now_iso
from .policy import DEFAULT_POLICY_PATH, effective_policy, load_policies
from .simulator import Simulator

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
FRONTEND_DIR = os.path.join(BASE_DIR, "frontend")


class ApiError(Exception):
    def __init__(self, message: str, status: int = 400, field: Optional[str] = None) -> None:
        super().__init__(message)
        self.message = message
        self.status = status
        self.field = field


def _payload() -> Dict[str, Any]:
    data = request.get_json(silent=True)
    if data is None:
        data = request.form.to_dict() or {}
    if not isinstance(data, dict):
        raise ApiError("Request body must be a JSON object")
    return data


def _int(data: Dict[str, Any], key: str, default: Optional[int] = None) -> Optional[int]:
    value = data.get(key, default)
    if value is None or value == "":
        return default
    try:
        return int(value)
    except (TypeError, ValueError):
        raise ApiError(f"'{key}' must be a whole number", field=key)


def _float(data: Dict[str, Any], key: str, default: Optional[float] = None) -> Optional[float]:
    value = data.get(key, default)
    if value is None or value == "":
        return default
    try:
        return float(value)
    except (TypeError, ValueError):
        raise ApiError(f"'{key}' must be a number", field=key)


def _mission_report_rows(tasks_dir: str) -> list:
    """One flattened row per task in `tasks_dir`, built from the same
    Mission Authorization Record every other consumer of this data uses
    (see backend/decision_graph.py / backend/eval_engine.py) — the
    shared source for both /api/reports/missions.csv and .html."""
    return [flatten_record(r) for r in query_decisions(tasks_dir=tasks_dir)]


def create_app(
    twin: Optional[DigitalTwin] = None,
    autostart: bool = True,
    run_thread: bool = True,
) -> Tuple[Flask, DigitalTwin, Simulator, CIEngine]:
    app = Flask(__name__, static_folder=None)
    app.config["JSON_SORT_KEYS"] = False

    twin = twin or DigitalTwin(
        log_dir=os.path.join(BASE_DIR, "logs"),
        data_dir=os.path.join(BASE_DIR, "data"),
    )
    broadcaster = Broadcaster()
    simulator = Simulator(twin, broadcaster=broadcaster)
    ci = CIEngine(twin)

    twin.logger.add_sink(lambda record: broadcaster.publish("log", record))
    twin.events.subscribe(lambda event: broadcaster.publish("event", event))

    def _carbon_verdict_subscriber(event: Dict[str, Any]) -> None:
        """Once a Carbon-originated task (see carbon_webhook() below)
        reaches a terminal state, grade it and post the verdict back to
        Carbon — see backend/carbon_client.py. A plain no-op for every
        other task (the vast majority)."""
        if event.get("event") not in ("TASK_COMPLETED", "TASK_FAILED"):
            return
        if not carbon_client.enabled():
            return
        task_id = event.get("task_id")
        task = twin.tasks.get(task_id) if task_id else None
        external_ref = getattr(task, "external_ref", None) if task is not None else None
        if not external_ref or external_ref.get("source") != "carbon":
            return

        def _post() -> None:
            events_log = twin.logger.get_task_logs(task_id)
            report = evaluate_events(events_log, task_id=task_id)
            carbon_client.report_verdict(external_ref, report.to_dict())

        threading.Thread(target=_post, daemon=True).start()

    twin.events.subscribe(_carbon_verdict_subscriber)

    if autostart:
        simulator.start()
    if run_thread:
        simulator.start_thread()

    # ------------------------------------------------------------------ #
    # Errors
    # ------------------------------------------------------------------ #
    @app.errorhandler(ApiError)
    def _api_error(exc: ApiError):
        return jsonify({"ok": False, "error": exc.message, "field": exc.field}), exc.status

    @app.errorhandler(404)
    def _not_found(_exc):
        return jsonify({"ok": False, "error": "That endpoint does not exist"}), 404

    @app.errorhandler(500)
    def _server_error(exc):
        twin.logger.error(LogCategory.SYSTEM, f"Unhandled server error: {exc!r}")
        return jsonify({"ok": False, "error": "Internal server error"}), 500

    def guarded(fn):
        """Translate domain exceptions into clean JSON errors."""

        def wrapper(*args, **kwargs):
            try:
                return fn(*args, **kwargs)
            except ApiError:
                raise
            except KeyError as exc:
                raise ApiError(str(exc).strip("'\""), status=404)
            except (ValueError, FileNotFoundError) as exc:
                raise ApiError(str(exc))

        wrapper.__name__ = fn.__name__
        return wrapper

    # ------------------------------------------------------------------ #
    # Static dashboard
    # ------------------------------------------------------------------ #
    @app.route("/")
    def index():
        return send_from_directory(FRONTEND_DIR, "index.html")

    @app.route("/<path:filename>")
    def static_files(filename: str):
        return send_from_directory(FRONTEND_DIR, filename)

    # ------------------------------------------------------------------ #
    # Reads
    # ------------------------------------------------------------------ #
    @app.get("/api/state")
    def get_state():
        return jsonify(twin.snapshot(include_layout=True))

    @app.get("/api/warehouse")
    def get_warehouse():
        return jsonify(twin.warehouse.to_dict())

    @app.get("/api/robots")
    def get_robots():
        return jsonify({"robots": [r.to_dict() for r in twin.robots.values()]})

    @app.get("/api/robots/<robot_id>")
    @guarded
    def get_robot(robot_id: str):
        robot = twin.find_robot(robot_id)
        if robot is None:
            raise ApiError(f"Robot '{robot_id}' does not exist", status=404)
        return jsonify(robot.to_dict())

    @app.get("/api/boxes")
    def get_boxes():
        return jsonify({"boxes": [b.to_dict() for b in twin.boxes.values()]})

    @app.get("/api/agents")
    def get_agents():
        return jsonify({"agents": [a.to_dict() for a in twin.agents.values()]})

    @app.get("/api/operators")
    def get_operators():
        return jsonify({"operators": [o.to_dict() for o in twin.operators.values()]})

    @app.get("/api/tasks")
    def get_tasks():
        return jsonify(
            {
                "tasks": twin.tasks.list_tasks(
                    status=request.args.get("status"),
                    limit=int(request.args.get("limit", 200)),
                ),
                "counts": twin.tasks.counts(),
            }
        )

    @app.get("/api/tasks/<task_id>")
    def get_task(task_id: str):
        task = twin.tasks.get(task_id)
        if task is None:
            raise ApiError(f"Task '{task_id}' does not exist", status=404)
        return jsonify(
            {
                "task": task.to_dict(),
                "events": twin.events.query(task_id=task_id, limit=200),
                # Complete, persisted history for this task (not limited by
                # the in-memory ring buffer), read from its own JSON file
                # under logs/tasks/<task_id>.json.
                "logs": twin.logger.get_task_logs(task_id),
            }
        )

    @app.get("/api/tasks/<task_id>/eval")
    def eval_task(task_id: str):
        """Grade one task's persisted log against the eval engine.

        Reads the same complete history used by ``GET /api/tasks/<id>``
        (``logs/tasks/<id>.json`` on disk, not just the in-memory ring
        buffer) so a task graded long after it finished is judged on its
        full record, not a truncated tail.
        """
        task = twin.tasks.get(task_id)
        if task is None:
            raise ApiError(f"Task '{task_id}' does not exist", status=404)
        events = twin.logger.get_task_logs(task_id)
        report = evaluate_events(events, task_id=task_id)
        return jsonify(report.to_dict())

    @app.post("/api/evals/run")
    def eval_run():
        """Grade every task that has a persisted log on disk.

        Equivalent to running ``python -m backend.run_evals`` against
        ``logs/tasks``, but over HTTP so it can be triggered from the
        dashboard, a Postman collection, or a CI job.
        """
        tasks_dir = twin.logger.tasks_dir
        reports = []
        if os.path.isdir(tasks_dir):
            for name in sorted(os.listdir(tasks_dir)):
                if not name.endswith(".json"):
                    continue
                task_id = name[: -len(".json")]
                events = twin.logger.get_task_logs(task_id)
                if events:
                    reports.append(evaluate_events(events, task_id=task_id))
        passed = sum(1 for r in reports if r.verdict.value == "PASS")
        warned = sum(1 for r in reports if r.verdict.value == "WARN")
        failed = sum(1 for r in reports if r.verdict.value == "FAIL")
        return jsonify(
            {
                "totals": {"passed": passed, "warned": warned, "failed": failed, "total": len(reports)},
                "results": [r.to_dict() for r in reports],
            }
        )

    @app.get("/api/tasks/<task_id>/logs/export")
    def export_task_logs(task_id: str):
        task = twin.tasks.get(task_id)
        if task is None:
            raise ApiError(f"Task '{task_id}' does not exist", status=404)
        body = json.dumps(twin.logger.get_task_logs(task_id), indent=2)
        return Response(
            body,
            mimetype="application/json",
            headers={"Content-Disposition": f"attachment; filename=task_{task_id}.json"},
        )

    @app.get("/api/logs")
    def get_logs():
        return jsonify(
            {
                "logs": twin.logger.query(
                    level=request.args.get("level"),
                    category=request.args.get("category"),
                    robot_id=request.args.get("robot_id"),
                    task_id=request.args.get("task_id"),
                    search=request.args.get("search"),
                    limit=int(request.args.get("limit", 400)),
                    since_seq=int(request.args.get("since", 0)),
                )
            }
        )

    @app.get("/api/events")
    def get_events():
        return jsonify(
            {
                "events": twin.events.query(
                    task_id=request.args.get("task_id"),
                    robot_id=request.args.get("robot_id"),
                    event_type=request.args.get("event"),
                    limit=int(request.args.get("limit", 400)),
                )
            }
        )

    @app.get("/api/statistics")
    def get_statistics():
        return jsonify(
            {
                "statistics": twin.statistics_snapshot(),
                "environment": twin.environment_state(),
                "robots": twin.robot_statistics(),
            }
        )

    @app.get("/api/statistics/history")
    def get_statistics_history():
        """Rolling samples for the dashboard's trend charts — see
        DigitalTwin.record_history (CONFIG["HISTORY_MAX_SAMPLES"] caps
        how far back this goes)."""
        return jsonify({"history": twin.history_snapshot()})

    @app.get("/api/fleet/load")
    def get_fleet_load():
        """Per-robot workload — active task, queued tasks, lifetime
        completed/failed — so a fleet manager can see at a glance who's
        overworked vs idle. Built entirely from numbers already tracked
        elsewhere (TaskManager.queued_count_for_robot, Robot stats)."""
        rows = []
        for robot in twin.robots.values():
            queued = twin.tasks.queued_count_for_robot(robot.id)
            active = 1 if robot.current_task else 0
            rows.append({
                "robot_id": robot.id,
                "robot": robot.name,
                "status": robot.status.value,
                "active_task": robot.current_task,
                "queued_tasks": queued,
                "workload": active + queued,
                "completed_tasks": robot.completed_tasks,
                "failed_tasks": robot.failed_tasks,
                "battery": round(robot.battery, 1),
            })
        rows.sort(key=lambda r: -r["workload"])
        return jsonify({"fleet_load": rows})

    @app.get("/api/maintenance/alerts")
    def get_maintenance_alerts():
        """Every robot currently over a predictive-maintenance threshold
        — see backend/maintenance.py. Schedule an
        OPERATOR_MAINTENANCE_SIGNOFF task to clear one."""
        alerts = []
        for robot in twin.robots.values():
            reason = maintenance_reason(robot)
            if reason:
                alerts.append({"robot_id": robot.id, "robot": robot.name, "reason": reason})
        return jsonify({"alerts": alerts})

    # ------------------------------------------------------------------ #
    # Robots
    # ------------------------------------------------------------------ #
    @app.post("/api/robots")
    @guarded
    def create_robot():
        data = _payload()
        x, y = _int(data, "x"), _int(data, "y")
        position = (x, y) if x is not None and y is not None else None
        robot = twin.add_robot(
            name=(data.get("name") or "").strip() or None,
            position=position,
            speed=_float(data, "speed"),
            allowed_task_types=data.get("allowed_task_types"),
            robot_class=data.get("robot_class"),
        )
        return jsonify({"ok": True, "robot": robot.to_dict()}), 201

    @app.post("/api/robots/<robot_id>/capabilities")
    @guarded
    def set_robot_capabilities(robot_id: str):
        """Configure which task types `robot_id` may be assigned — see
        DigitalTwin.set_robot_capabilities / backend/eligibility.py.
        Body: {"allowed_task_types": [...]}; an empty list or omitted key
        clears the restriction (unrestricted)."""
        data = _payload()
        robot = twin.set_robot_capabilities(robot_id, data.get("allowed_task_types"))
        return jsonify({"ok": True, "robot": robot.to_dict()})

    @app.post("/api/robots/<robot_id>/stop")
    @guarded
    def stop_robot(robot_id: str):
        return jsonify({"ok": True, "robot": twin.stop_robot(robot_id).to_dict()})

    @app.post("/api/robots/<robot_id>/resume")
    @guarded
    def resume_robot(robot_id: str):
        return jsonify({"ok": True, "robot": twin.resume_robot(robot_id).to_dict()})

    @app.post("/api/robots/<robot_id>/charge")
    @guarded
    def charge_robot(robot_id: str):
        task = twin.request_charge(robot_id, priority=Priority.HIGH)
        return jsonify({"ok": True, "task": task.to_dict()})

    @app.post("/api/robots/<robot_id>/reset")
    @guarded
    def reset_robot(robot_id: str):
        return jsonify({"ok": True, "robot": twin.reset_robot(robot_id).to_dict()})

    # ------------------------------------------------------------------ #
    # Boxes
    # ------------------------------------------------------------------ #
    @app.post("/api/boxes")
    @guarded
    def create_box():
        data = _payload()
        x, y = _int(data, "x"), _int(data, "y")
        position = (x, y) if x is not None and y is not None else None
        box = twin.add_box(
            name=(data.get("name") or "").strip() or None,
            position=position,
            weight=_float(data, "weight", 1.0),
            source=data.get("source"),
            destination=data.get("destination"),
        )
        return jsonify({"ok": True, "box": box.to_dict()}), 201

    # ------------------------------------------------------------------ #
    # Agents / operators
    # ------------------------------------------------------------------ #
    @app.post("/api/agents")
    @guarded
    def create_agent():
        data = _payload()
        agent = twin.add_agent(
            name=(data.get("name") or "").strip() or None,
            model_version=data.get("model_version"),
        )
        return jsonify({"ok": True, "agent": agent.to_dict()}), 201

    @app.post("/api/operators")
    @guarded
    def create_operator():
        data = _payload()
        operator = twin.add_operator(
            name=(data.get("name") or "").strip() or None,
            certifications=data.get("certifications"),
            shift_start_hour=_int(data, "shift_start_hour"),
            shift_end_hour=_int(data, "shift_end_hour"),
            role=data.get("role"),
        )
        return jsonify({"ok": True, "operator": operator.to_dict()}), 201

    @app.post("/api/operators/<operator_id>/shift")
    @guarded
    def set_operator_shift(operator_id: str):
        """Configure (or clear) an operator's automatic shift window — see
        DigitalTwin.set_operator_shift / Simulator._check_operator_shifts.
        Body: {"shift_start_hour": 9, "shift_end_hour": 17}; omit both (or
        send nulls) to go back to manual status control only."""
        data = _payload()
        operator = twin.set_operator_shift(
            operator_id, _int(data, "shift_start_hour"), _int(data, "shift_end_hour")
        )
        return jsonify({"ok": True, "operator": operator.to_dict()})

    # ------------------------------------------------------------------ #
    # Tasks
    # ------------------------------------------------------------------ #
    @app.post("/api/tasks")
    @guarded
    def create_task():
        data = _payload()
        if not data.get("type"):
            raise ApiError("Choose a task type", field="type")
        task = twin.tasks.create_task(data)
        status = 201 if task.status.value != "FAILED" else 422
        return jsonify({"ok": status == 201, "task": task.to_dict(), "error": task.error}), status

    @app.post("/api/tasks/<task_id>/cancel")
    @guarded
    def cancel_task(task_id: str):
        return jsonify({"ok": True, "task": twin.tasks.cancel_task(task_id).to_dict()})

    @app.post("/api/tasks/<task_id>/pause")
    @guarded
    def pause_task(task_id: str):
        return jsonify({"ok": True, "task": twin.tasks.pause_task(task_id).to_dict()})

    @app.post("/api/tasks/<task_id>/resume")
    @guarded
    def resume_task(task_id: str):
        return jsonify({"ok": True, "task": twin.tasks.resume_task(task_id).to_dict()})

    @app.get("/api/decisions")
    @guarded
    def get_decisions():
        """Decision history search over this project's own real task logs
        (logs/tasks/*.json) — see backend/decision_graph.py. All filters
        are optional and ANDed together."""
        battery_below = request.args.get("battery_below")
        records = query_decisions(
            robot=request.args.get("robot"),
            agent=request.args.get("agent"),
            operator=request.args.get("operator"),
            task_type=request.args.get("type"),
            verdict=request.args.get("verdict"),
            battery_below=float(battery_below) if battery_below else None,
            tasks_dir=twin.logger.tasks_dir,
        )
        return jsonify({"decisions": records, "count": len(records)})

    # ------------------------------------------------------------------ #
    # Chat agent (backend/agent_chat.py) — a real tool-use loop, distinct
    # from the one-shot narration in backend/llm.py. Always needs a
    # configured Groq key; there's no computed fallback for "no AI" here.
    # ------------------------------------------------------------------ #
    @app.post("/api/agent/chat")
    @guarded
    def agent_chat():
        """Runs synchronously (the reply here is the authoritative
        result), but also narrates each step live over the existing SSE
        stream (event name "agent-chat") as it happens, so a long,
        many-step command doesn't just sit behind one static "thinking…"
        the whole time — see the chat panel in frontend/app.js."""
        data = _payload()
        message = data.get("message")
        if not message or not str(message).strip():
            raise ApiError("Provide 'message'", field="message")
        history = data.get("history")
        if not isinstance(history, list):
            history = None
        try:
            result = run_chat(
                twin, str(message), history,
                on_progress=lambda event: broadcaster.publish("agent-chat", event),
            )
        except ChatError as exc:
            raise ApiError(str(exc), status=503)
        return jsonify({"ok": True, **result})

    # ------------------------------------------------------------------ #
    # Recurring tasks (backend/scheduler.py)
    # ------------------------------------------------------------------ #
    @app.get("/api/schedules")
    def list_schedules():
        return jsonify({"schedules": twin.scheduler.list()})

    @app.post("/api/schedules")
    @guarded
    def create_schedule():
        data = _payload()
        payload = data.get("payload")
        if not isinstance(payload, dict) or not payload.get("type"):
            raise ApiError("Provide 'payload' — a task-creation body with at least a 'type'", field="payload")
        schedule = twin.scheduler.add(
            payload,
            interval_seconds=_float(data, "interval_seconds"),
            interval_ticks=_int(data, "interval_ticks"),
            run_immediately=bool(data.get("run_immediately")),
        )
        return jsonify({"ok": True, "schedule": schedule.to_dict()}), 201

    @app.post("/api/schedules/<schedule_id>/toggle")
    @guarded
    def toggle_schedule(schedule_id: str):
        data = _payload()
        enabled = data.get("enabled")
        current = twin.scheduler.schedules.get(schedule_id)
        if current is None:
            raise ApiError(f"Schedule '{schedule_id}' does not exist", status=404)
        schedule = twin.scheduler.set_enabled(schedule_id, not current.enabled if enabled is None else bool(enabled))
        return jsonify({"ok": True, "schedule": schedule.to_dict()})

    @app.delete("/api/schedules/<schedule_id>")
    @guarded
    def delete_schedule(schedule_id: str):
        twin.scheduler.remove(schedule_id)
        return jsonify({"ok": True})

    # ------------------------------------------------------------------ #
    # Policy-as-code (backend/policy.py)
    # ------------------------------------------------------------------ #
    @app.get("/api/policies")
    def get_policies():
        policy = effective_policy()
        policy["editable_file"] = DEFAULT_POLICY_PATH
        return jsonify(policy)

    @app.post("/api/policies/reload")
    def reload_policies():
        """Re-read policies.yaml from disk and apply it live, no restart
        needed — see backend/policy.py."""
        policy = load_policies()
        twin.logger.info(LogCategory.SYSTEM, "Policies reloaded from policies.yaml")
        return jsonify({"ok": True, **policy})

    @app.post("/api/policies/llm-narration")
    @guarded
    def set_llm_narration():
        """Flip CONFIG["AGENT_LLM_ENABLED"] live, in memory only — a quick
        dashboard toggle, distinct from policies.yaml (which is the
        durable, restart-surviving way to turn this on — see backend/
        llm.py). Reloading policies.yaml will overwrite whatever this
        sets."""
        data = _payload()
        if "enabled" not in data:
            raise ApiError("Provide 'enabled': true or false", field="enabled")
        CONFIG["AGENT_LLM_ENABLED"] = bool(data["enabled"])
        twin.logger.info(
            LogCategory.SYSTEM,
            f"Live LLM narration {'enabled' if CONFIG['AGENT_LLM_ENABLED'] else 'disabled'} (in-memory, not saved to policies.yaml)",
        )
        return jsonify({"ok": True, "enabled": CONFIG["AGENT_LLM_ENABLED"]})

    @app.post("/api/policies/collision-risk")
    @guarded
    def set_collision_risk():
        """Flip CONFIG["COLLISION_RISK"] live, in memory only — same
        pattern as /api/policies/llm-narration. 0.0 (the default) means
        the traffic system's avoidance is perfect; raise it to let robots
        actually fail to avoid each other while carrying out real work
        instead of a manual/forced collision — see Simulator._act_navigate
        and DigitalTwin.register_collision. Reloading policies.yaml will
        overwrite whatever this sets."""
        data = _payload()
        risk = _float(data, "risk")
        if risk is None:
            raise ApiError("Provide 'risk': a number between 0 and 1", field="risk")
        if not (0.0 <= risk <= 1.0):
            raise ApiError("'risk' must be between 0 and 1", field="risk")
        CONFIG["COLLISION_RISK"] = risk
        twin.logger.info(
            LogCategory.SYSTEM,
            f"Collision risk set to {risk:.0%} (in-memory, not saved to policies.yaml)",
        )
        return jsonify({"ok": True, "risk": CONFIG["COLLISION_RISK"]})

    @app.post("/api/policies/false-success-risk")
    @guarded
    def set_false_success_risk():
        """Flip CONFIG["FALSE_SUCCESS_RISK"] live, in memory only — same
        pattern as /api/policies/collision-risk. 0.0 (the default) means
        every completed delivery really did land the box where it was
        supposed to; raise it to let a real delivery silently fail to
        seat its payload while the event trail and task lifecycle still
        report a clean success — see Simulator._act_deliver and
        eval_engine.check_state_transition, the only check that ever
        catches it. Reloading policies.yaml will overwrite whatever this
        sets."""
        data = _payload()
        risk = _float(data, "risk")
        if risk is None:
            raise ApiError("Provide 'risk': a number between 0 and 1", field="risk")
        if not (0.0 <= risk <= 1.0):
            raise ApiError("'risk' must be between 0 and 1", field="risk")
        CONFIG["FALSE_SUCCESS_RISK"] = risk
        twin.logger.info(
            LogCategory.SYSTEM,
            f"False-success risk set to {risk:.0%} (in-memory, not saved to policies.yaml)",
        )
        return jsonify({"ok": True, "risk": CONFIG["FALSE_SUCCESS_RISK"]})

    # ------------------------------------------------------------------ #
    # Carbon (carbon.ms) MES integration (backend/carbon_client.py) —
    # optional in both directions. Inbound: Carbon posts a job/traveler
    # step here and it becomes a normal task, gated by the exact same
    # eligibility checks as any other task. Outbound: see the
    # _carbon_verdict_subscriber registered above.
    # ------------------------------------------------------------------ #
    @app.get("/api/integrations/carbon/status")
    def carbon_status():
        return jsonify({
            "enabled": bool(CONFIG.get("CARBON_ENABLED")),
            "outbound_configured": carbon_client.enabled(),
            "inbound_configured": carbon_client.webhook_configured(),
            "base_url": carbon_client.find_base_url(),
        })

    @app.post("/api/integrations/carbon/webhook")
    @guarded
    def carbon_webhook():
        if not carbon_client.webhook_configured():
            raise ApiError("Carbon integration is not configured", status=503)
        secret_header = request.headers.get("X-Carbon-Webhook-Secret")
        if not carbon_client.verify_webhook_secret(secret_header):
            raise ApiError("Invalid or missing webhook secret", status=401)
        job = _payload()
        try:
            payload = carbon_client.map_job_to_task_payload(job)
        except ValueError as exc:
            raise ApiError(str(exc), field="operationType")
        task = twin.tasks.create_task(payload)
        status = 201 if task.status.value != "FAILED" else 422
        return jsonify({"ok": status == 201, "task": task.to_dict(), "error": task.error}), status

    # ------------------------------------------------------------------ #
    # Mission reports (backend/eval_engine.py's Mission Authorization Record)
    # ------------------------------------------------------------------ #
    @app.get("/api/reports/missions.csv")
    def report_missions_csv():
        rows = _mission_report_rows(twin.logger.tasks_dir)
        buffer = io.StringIO()
        writer = csv.writer(buffer)
        writer.writerow(["task_id", "type", "verdict", "robot", "agent", "operator", "summary", "reasons"])
        for row in rows:
            writer.writerow([
                row["task_id"], row["type"], row["verdict"], row["robot"],
                row["agent"], row["operator"], row["summary"], row["reasons"],
            ])
        return Response(
            buffer.getvalue(),
            mimetype="text/csv",
            headers={"Content-Disposition": "attachment; filename=mission_report.csv"},
        )

    @app.get("/api/reports/missions.html")
    def report_missions_html():
        """A clean, printable page — open it and use the browser's own
        Print > Save as PDF for a PDF copy, with no server-side PDF
        library added (see requirements.txt's 'just Flask, pytest,
        PyYAML' philosophy)."""
        rows = _mission_report_rows(twin.logger.tasks_dir)

        def esc(value: Any) -> str:
            return html.escape(str(value))

        body_rows = "\n".join(
            f"<tr class='{esc(r['verdict'].lower())}'><td>{esc(r['task_id'])}</td><td>{esc(r['type'])}</td>"
            f"<td>{esc(r['verdict'])}</td><td>{esc(r['robot'])}</td><td>{esc(r['agent'])}</td>"
            f"<td>{esc(r['operator'])}</td><td>{esc(r['summary'])}</td><td>{esc(r['reasons'])}</td></tr>"
            for r in rows
        )
        page = f"""<!doctype html><html><head><meta charset="utf-8">
<title>Mission Authorization Report</title>
<style>
body {{ font-family: -apple-system, Arial, sans-serif; margin: 24px; color: #1a1a1a; }}
table {{ border-collapse: collapse; width: 100%; font-size: 13px; }}
th, td {{ border: 1px solid #ccc; padding: 6px 8px; text-align: left; vertical-align: top; }}
th {{ background: #f0f0f0; }}
tr.fail {{ background: #fdecea; }}
tr.warn {{ background: #fff8e1; }}
tr.pass {{ background: #eefaf0; }}
@media print {{ body {{ margin: 0; }} }}
</style></head><body>
<h1>Mission Authorization Report</h1>
<p>{len(rows)} task(s) — generated {now_iso()}</p>
<table><thead><tr><th>Task</th><th>Type</th><th>Verdict</th><th>Robot</th><th>Agent</th>
<th>Operator</th><th>Summary</th><th>Reasons</th></tr></thead>
<tbody>{body_rows}</tbody></table>
</body></html>"""
        return Response(page, mimetype="text/html")

    # ------------------------------------------------------------------ #
    # Simulation
    # ------------------------------------------------------------------ #
    @app.post("/api/simulation/start")
    def sim_start():
        simulator.start()
        return jsonify({"ok": True, "environment": twin.environment_state()})

    @app.post("/api/simulation/pause")
    def sim_pause():
        simulator.pause()
        return jsonify({"ok": True, "environment": twin.environment_state()})

    @app.post("/api/simulation/stop")
    def sim_stop():
        simulator.stop()
        return jsonify({"ok": True, "environment": twin.environment_state()})

    @app.post("/api/simulation/reset")
    @guarded
    def sim_reset():
        data = _payload()
        twin.reset(demo_tasks=bool(data.get("demo_tasks", True)))
        simulator.start()
        broadcaster.publish("state", twin.snapshot(include_layout=True))
        return jsonify({"ok": True, "state": twin.snapshot(include_layout=True)})

    @app.post("/api/simulation/emergency-stop")
    def sim_emergency_stop():
        twin.emergency_stop()
        broadcaster.publish("state", twin.snapshot())
        return jsonify({"ok": True, "environment": twin.environment_state()})

    @app.post("/api/simulation/resume")
    def sim_resume():
        if twin.simulation_status == SimulationStatus.EMERGENCY_STOP:
            twin.release_emergency_stop()
        else:
            simulator.start()
        return jsonify({"ok": True, "environment": twin.environment_state()})

    @app.post("/api/simulation/speed")
    @guarded
    def sim_speed():
        data = _payload()
        speed = _float(data, "speed")
        if speed is None:
            raise ApiError("Provide a speed multiplier", field="speed")
        return jsonify({"ok": True, "speed": simulator.set_speed(speed)})

    @app.post("/api/simulation/tick")
    def sim_tick():
        """Advance exactly one tick — useful when the loop is paused."""
        previous = twin.simulation_status
        twin.simulation_status = SimulationStatus.RUNNING
        simulator.tick()
        twin.simulation_status = previous
        return jsonify({"ok": True, "tick": twin.tick_count})

    # ------------------------------------------------------------------ #
    # Mock CI
    # ------------------------------------------------------------------ #
    @app.post("/api/ci/run")
    def ci_run():
        summary = ci.run()
        broadcaster.publish("ci", summary)
        return jsonify(summary)

    @app.get("/api/ci/status")
    def ci_status():
        return jsonify(ci.status())

    # ------------------------------------------------------------------ #
    # Logs & persistence
    # ------------------------------------------------------------------ #
    @app.post("/api/logs/clear")
    def clear_logs():
        twin.logger.clear()
        twin.events.clear()
        twin.logger.info(LogCategory.USER, "Log history cleared by the operator")
        return jsonify({"ok": True})

    @app.get("/api/logs/export")
    def export_logs():
        fmt = (request.args.get("format") or "txt").lower()
        if fmt == "json":
            body = json.dumps(twin.logger.export_json(), indent=2)
            return Response(
                body,
                mimetype="application/json",
                headers={"Content-Disposition": "attachment; filename=warehouse_logs.json"},
            )
        return Response(
            twin.logger.export_text(),
            mimetype="text/plain",
            headers={"Content-Disposition": "attachment; filename=warehouse_logs.txt"},
        )

    @app.get("/api/export/state")
    def export_state():
        return Response(
            json.dumps(twin.serialize(), indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=warehouse_state.json"},
        )

    @app.get("/api/export/tasks")
    def export_tasks():
        return Response(
            json.dumps({"tasks": twin.tasks.list_tasks(limit=10000)}, indent=2),
            mimetype="application/json",
            headers={"Content-Disposition": "attachment; filename=tasks.json"},
        )

    @app.post("/api/state/save")
    @guarded
    def save_state():
        path = twin.save_state()
        return jsonify({"ok": True, "path": path})

    @app.post("/api/state/load")
    @guarded
    def load_state():
        twin.load_state()
        broadcaster.publish("state", twin.snapshot(include_layout=True))
        return jsonify({"ok": True, "state": twin.snapshot(include_layout=True)})

    @app.post("/api/state/reset")
    @guarded
    def reset_state():
        twin.reset(demo_tasks=True)
        broadcaster.publish("state", twin.snapshot(include_layout=True))
        return jsonify({"ok": True, "state": twin.snapshot(include_layout=True)})

    # ------------------------------------------------------------------ #
    # Realtime stream
    # ------------------------------------------------------------------ #
    @app.get("/api/stream")
    def stream():
        client = broadcaster.register()

        def generate():
            try:
                yield "retry: 2000\n\n"
                yield f"event: state\ndata: {json.dumps(twin.snapshot(include_layout=True), default=str)}\n\n"
                last_beat = time.time()
                while True:
                    try:
                        yield client.get(timeout=1.0)
                    except queue.Empty:
                        if time.time() - last_beat > 10:
                            last_beat = time.time()
                            yield ": keep-alive\n\n"
            finally:
                broadcaster.unregister(client)

        return Response(
            generate(),
            mimetype="text/event-stream",
            headers={
                "Cache-Control": "no-cache, no-transform",
                "Connection": "keep-alive",
                "X-Accel-Buffering": "no",
            },
        )

    @app.get("/api/health")
    def health():
        return jsonify(
            {
                "ok": True,
                "simulation": twin.simulation_status.value,
                "tick": twin.tick_count,
                "clients": broadcaster.client_count,
            }
        )

    app.twin = twin  # type: ignore[attr-defined]
    app.simulator = simulator  # type: ignore[attr-defined]
    app.ci = ci  # type: ignore[attr-defined]
    app.broadcaster = broadcaster  # type: ignore[attr-defined]
    return app, twin, simulator, ci


def main() -> None:
    host = os.environ.get("WAREHOUSE_HOST", "127.0.0.1")
    port = int(os.environ.get("WAREHOUSE_PORT", "5000"))
    app, twin, simulator, _ci = create_app()
    twin.logger.info(
        LogCategory.SYSTEM,
        f"Warehouse control centre listening on http://{host}:{port}",
    )
    try:
        app.run(host=host, port=port, threaded=True, debug=False, use_reloader=False)
    finally:
        simulator.stop_thread()


if __name__ == "__main__":
    main()
