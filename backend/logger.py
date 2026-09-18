"""Structured, centralised logging for the warehouse digital twin.

Every notable occurrence in the system flows through :class:`WarehouseLogger`,
which keeps an in-memory ring buffer for the dashboard, appends a human readable
line to ``logs/warehouse.log`` and appends a JSON record to ``logs/events.json``
(JSON Lines format so appends are cheap and the file survives crashes).
"""
from __future__ import annotations

import json
import os
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from .models import (
    CONFIG,
    LOG_LEVEL_RANK,
    LogCategory,
    LogLevel,
    now_hms,
    now_iso,
)


class WarehouseLogger:
    def __init__(self, log_dir: str = "logs", persist: bool = True) -> None:
        self.log_dir = log_dir
        self.persist = persist
        self.text_path = os.path.join(log_dir, "warehouse.log")
        self.json_path = os.path.join(log_dir, "events.json")
        self.tasks_dir = os.path.join(log_dir, "tasks")
        self._lock = threading.RLock()
        self._seq = 0
        self.records: Deque[Dict[str, Any]] = deque(maxlen=CONFIG["MAX_LOGS_IN_MEMORY"])
        self._sinks: List[Callable[[Dict[str, Any]], None]] = []
        # In-memory cache of the full record list for each task, mirrored to
        # its own JSON file on disk (see `_write_task_file`). Unlike
        # `records`, this is never trimmed, so it always holds the complete
        # history for that task even after it scrolls out of the ring buffer.
        self._task_cache: Dict[str, List[Dict[str, Any]]] = {}
        if self.persist:
            os.makedirs(self.log_dir, exist_ok=True)
            os.makedirs(self.tasks_dir, exist_ok=True)
            self._load_previous()

    # ------------------------------------------------------------------ #
    # Sinks
    # ------------------------------------------------------------------ #
    def add_sink(self, sink: Callable[[Dict[str, Any]], None]) -> None:
        """Register a callback invoked for every new log record."""
        self._sinks.append(sink)

    # ------------------------------------------------------------------ #
    # Writing
    # ------------------------------------------------------------------ #
    def log(
        self,
        level: LogLevel,
        category: LogCategory,
        message: str,
        event: Optional[str] = None,
        robot_id: Optional[str] = None,
        task_id: Optional[str] = None,
        box_id: Optional[str] = None,
        position: Optional[Dict[str, int]] = None,
        data: Optional[Dict[str, Any]] = None,
    ) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            record: Dict[str, Any] = {
                "seq": self._seq,
                "timestamp": now_iso(),
                "time": now_hms(),
                "level": level.value,
                "category": category.value,
                "event": event,
                "robot_id": robot_id,
                "task_id": task_id,
                "box_id": box_id,
                "position": position,
                "message": message,
                "data": data or {},
            }
            self.records.append(record)
            if self.persist:
                self._write(record)
        for sink in list(self._sinks):
            try:
                sink(record)
            except Exception:  # a broken dashboard client must never stop the sim
                pass
        return record

    def debug(self, category: LogCategory, message: str, **kw: Any) -> Dict[str, Any]:
        return self.log(LogLevel.DEBUG, category, message, **kw)

    def info(self, category: LogCategory, message: str, **kw: Any) -> Dict[str, Any]:
        return self.log(LogLevel.INFO, category, message, **kw)

    def warning(self, category: LogCategory, message: str, **kw: Any) -> Dict[str, Any]:
        return self.log(LogLevel.WARNING, category, message, **kw)

    def error(self, category: LogCategory, message: str, **kw: Any) -> Dict[str, Any]:
        return self.log(LogLevel.ERROR, category, message, **kw)

    def critical(self, category: LogCategory, message: str, **kw: Any) -> Dict[str, Any]:
        return self.log(LogLevel.CRITICAL, category, message, **kw)

    # ------------------------------------------------------------------ #
    # Persistence
    # ------------------------------------------------------------------ #
    def _write(self, record: Dict[str, Any]) -> None:
        try:
            with open(self.text_path, "a", encoding="utf-8") as handle:
                handle.write(self.format_line(record) + "\n")
            with open(self.json_path, "a", encoding="utf-8") as handle:
                handle.write(json.dumps(record) + "\n")
        except OSError:
            pass
        task_id = record.get("task_id")
        if task_id:
            self._write_task_file(task_id, record)

    # ------------------------------------------------------------------ #
    # Per-task JSON files
    # ------------------------------------------------------------------ #
    @staticmethod
    def _safe_task_filename(task_id: str) -> str:
        """Sanitise a task id so it can't escape the tasks directory."""
        safe = "".join(c for c in task_id if c.isalnum() or c in ("-", "_"))
        return f"{safe or 'unknown'}.json"

    def _task_file_path(self, task_id: str) -> str:
        return os.path.join(self.tasks_dir, self._safe_task_filename(task_id))

    def _load_task_file(self, task_id: str) -> List[Dict[str, Any]]:
        path = self._task_file_path(task_id)
        if not os.path.exists(path):
            return []
        try:
            with open(path, "r", encoding="utf-8") as handle:
                data = json.load(handle)
            if isinstance(data, list):
                return data
        except (OSError, json.JSONDecodeError):
            pass
        return []

    def _write_task_file(self, task_id: str, record: Dict[str, Any]) -> None:
        """Append `record` to the dedicated JSON file for this task.

        Keeps an in-memory cache per task so we don't have to re-read the
        file from disk on every log line, while still starting from
        whatever was already on disk (e.g. from a previous run) the first
        time a given task is touched in this process.
        """
        if task_id not in self._task_cache:
            self._task_cache[task_id] = self._load_task_file(task_id)
        self._task_cache[task_id].append(record)
        path = self._task_file_path(task_id)
        try:
            with open(path, "w", encoding="utf-8") as handle:
                json.dump(self._task_cache[task_id], handle, indent=2)
        except OSError:
            pass

    def get_task_logs(self, task_id: str) -> List[Dict[str, Any]]:
        """Return the complete, persisted log history for a single task.

        Unlike `query(task_id=...)`, this is not limited by the in-memory
        ring buffer size, so it always returns every log line ever recorded
        for that task, read straight from its dedicated JSON file.
        """
        with self._lock:
            if task_id in self._task_cache:
                return list(self._task_cache[task_id])
        return self._load_task_file(task_id)

    def _load_previous(self) -> None:
        """Re-hydrate the tail of the previous run so history survives restarts."""
        if not os.path.exists(self.json_path):
            return
        try:
            with open(self.json_path, "r", encoding="utf-8") as handle:
                lines = handle.readlines()[-CONFIG["MAX_LOGS_IN_MEMORY"]:]
        except OSError:
            return
        for line in lines:
            line = line.strip()
            if not line:
                continue
            try:
                record = json.loads(line)
            except json.JSONDecodeError:
                continue
            record["restored"] = True
            self.records.append(record)
            self._seq = max(self._seq, int(record.get("seq", 0)))

    # ------------------------------------------------------------------ #
    # Reading
    # ------------------------------------------------------------------ #
    @staticmethod
    def format_line(record: Dict[str, Any]) -> str:
        parts = [
            record.get("time") or record.get("timestamp", ""),
            f"[{record['level']}]",
            f"[{record['category']}]",
        ]
        for key, label in (("robot_id", "robot"), ("task_id", "task"), ("box_id", "box")):
            if record.get(key):
                parts.append(f"{label}={record[key]}")
        pos = record.get("position")
        if pos:
            parts.append(f"({pos['x']},{pos['y']})")
        parts.append(record["message"])
        return " ".join(parts)

    def query(
        self,
        level: Optional[str] = None,
        category: Optional[str] = None,
        robot_id: Optional[str] = None,
        task_id: Optional[str] = None,
        search: Optional[str] = None,
        limit: int = 500,
        since_seq: int = 0,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            records = list(self.records)
        min_rank = LOG_LEVEL_RANK[LogLevel(level)] if level else 0
        needle = search.lower() if search else None
        out: List[Dict[str, Any]] = []
        for record in records:
            if record.get("seq", 0) <= since_seq:
                continue
            if min_rank and LOG_LEVEL_RANK.get(LogLevel(record["level"]), 0) < min_rank:
                continue
            if category and record["category"] != category:
                continue
            if robot_id and record.get("robot_id") != robot_id:
                continue
            if task_id and record.get("task_id") != task_id:
                continue
            if needle and needle not in record["message"].lower():
                continue
            out.append(record)
        return out[-limit:]

    def export_text(self) -> str:
        with self._lock:
            return "\n".join(self.format_line(r) for r in self.records)

    def export_json(self) -> List[Dict[str, Any]]:
        with self._lock:
            return list(self.records)

    def clear(self) -> None:
        with self._lock:
            self.records.clear()
            self._task_cache.clear()
            if self.persist:
                for path in (self.text_path, self.json_path):
                    try:
                        open(path, "w", encoding="utf-8").close()
                    except OSError:
                        pass
                try:
                    for name in os.listdir(self.tasks_dir):
                        if name.endswith(".json"):
                            os.remove(os.path.join(self.tasks_dir, name))
                except OSError:
                    pass
