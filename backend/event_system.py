"""Event bus and Server-Sent-Events broadcaster.

The event system is the seam between the simulation and everything that watches
it: the logger, the mock CI engine and the browser. Simulation code raises
semantic events; subscribers decide what to do with them.
"""
from __future__ import annotations

import json
import queue
import threading
from collections import deque
from typing import Any, Callable, Deque, Dict, List, Optional

from .models import CONFIG, EventType, LogCategory, LogLevel, now_hms, now_iso


class EventSystem:
    def __init__(self, logger: Optional[Any] = None) -> None:
        self.logger = logger
        self._lock = threading.RLock()
        self._seq = 0
        self.events: Deque[Dict[str, Any]] = deque(maxlen=CONFIG["MAX_EVENTS_IN_MEMORY"])
        self._subscribers: List[Callable[[Dict[str, Any]], None]] = []

    def subscribe(self, callback: Callable[[Dict[str, Any]], None]) -> None:
        self._subscribers.append(callback)

    def emit(
        self,
        event_type: EventType,
        message: str,
        category: LogCategory = LogCategory.ENVIRONMENT,
        level: LogLevel = LogLevel.INFO,
        robot_id: Optional[str] = None,
        task_id: Optional[str] = None,
        box_id: Optional[str] = None,
        position: Optional[Dict[str, int]] = None,
        data: Optional[Dict[str, Any]] = None,
        log: bool = True,
    ) -> Dict[str, Any]:
        with self._lock:
            self._seq += 1
            event = {
                "seq": self._seq,
                "timestamp": now_iso(),
                "time": now_hms(),
                "event": event_type.value,
                "category": category.value,
                "level": level.value,
                "robot_id": robot_id,
                "task_id": task_id,
                "box_id": box_id,
                "position": position,
                "message": message,
                "data": data or {},
            }
            self.events.append(event)
        if log and self.logger is not None:
            self.logger.log(
                level,
                category,
                message,
                event=event_type.value,
                robot_id=robot_id,
                task_id=task_id,
                box_id=box_id,
                position=position,
                data=data,
            )
        for subscriber in list(self._subscribers):
            try:
                subscriber(event)
            except Exception:
                pass
        return event

    def query(
        self,
        task_id: Optional[str] = None,
        robot_id: Optional[str] = None,
        event_type: Optional[str] = None,
        limit: int = 500,
    ) -> List[Dict[str, Any]]:
        with self._lock:
            events = list(self.events)
        out = [
            e
            for e in events
            if (task_id is None or e.get("task_id") == task_id)
            and (robot_id is None or e.get("robot_id") == robot_id)
            and (event_type is None or e.get("event") == event_type)
        ]
        return out[-limit:]

    def clear(self) -> None:
        with self._lock:
            self.events.clear()


class Broadcaster:
    """Fan-out hub for Server-Sent Events.

    Each connected browser gets its own bounded queue. Slow clients drop frames
    instead of stalling the simulation thread.
    """

    def __init__(self, max_queue: int = 200) -> None:
        self._lock = threading.RLock()
        self._clients: List["queue.Queue[str]"] = []
        self.max_queue = max_queue

    def register(self) -> "queue.Queue[str]":
        client: "queue.Queue[str]" = queue.Queue(maxsize=self.max_queue)
        with self._lock:
            self._clients.append(client)
        return client

    def unregister(self, client: "queue.Queue[str]") -> None:
        with self._lock:
            if client in self._clients:
                self._clients.remove(client)

    @property
    def client_count(self) -> int:
        with self._lock:
            return len(self._clients)

    def publish(self, name: str, payload: Any) -> None:
        frame = f"event: {name}\ndata: {json.dumps(payload, default=str)}\n\n"
        with self._lock:
            clients = list(self._clients)
        for client in clients:
            try:
                client.put_nowait(frame)
            except queue.Full:
                try:
                    client.get_nowait()
                    client.put_nowait(frame)
                except Exception:
                    pass
