"""Boxes: the payloads robots move around the warehouse."""
from __future__ import annotations

from typing import Any, Dict, Optional

from .models import BoxStatus, Cell, cell_dict, cell_tuple, now_iso


class Box:
    def __init__(
        self,
        box_id: str,
        name: str,
        position: Cell,
        weight: float = 1.0,
        source: Optional[str] = None,
        destination: Optional[str] = None,
        status: BoxStatus = BoxStatus.STORED,
    ) -> None:
        self.id = box_id
        self.name = name
        self.position: Cell = position
        self.weight = float(weight)
        self.source = source
        self.destination = destination
        self.status = status
        self.assigned_robot: Optional[str] = None
        self.assigned_task: Optional[str] = None
        self.pick_count = 0
        self.delivery_count = 0
        self.created_at = now_iso()
        self.updated_at = now_iso()

    # ------------------------------------------------------------------ #
    def touch(self) -> None:
        self.updated_at = now_iso()

    def set_status(self, status: BoxStatus) -> BoxStatus:
        previous = self.status
        self.status = status
        self.touch()
        return previous

    @property
    def is_available(self) -> bool:
        return self.status in (BoxStatus.STORED, BoxStatus.DELIVERED) and self.assigned_robot is None

    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "weight": self.weight,
            "position": cell_dict(self.position),
            "source": self.source,
            "destination": self.destination,
            "status": self.status.value,
            "assigned_robot": self.assigned_robot,
            "assigned_task": self.assigned_task,
            "pick_count": self.pick_count,
            "delivery_count": self.delivery_count,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Box":
        box = Box(
            box_id=data["id"],
            name=data["name"],
            position=cell_tuple(data["position"]) or (1, 1),
            weight=data.get("weight", 1.0),
            source=data.get("source"),
            destination=data.get("destination"),
            status=BoxStatus(data.get("status", "STORED")),
        )
        box.assigned_robot = data.get("assigned_robot")
        box.assigned_task = data.get("assigned_task")
        box.pick_count = data.get("pick_count", 0)
        box.delivery_count = data.get("delivery_count", 0)
        box.created_at = data.get("created_at", box.created_at)
        box.updated_at = data.get("updated_at", box.updated_at)
        return box
