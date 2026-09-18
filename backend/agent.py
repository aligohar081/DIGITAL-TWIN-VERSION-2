"""AI agents: the "cognitive" actor class.

An agent doesn't move through the warehouse — it interprets information
and produces recommendations (see TaskType.AGENT_INSPECTION and the
MIXED_MAINTENANCE_MISSION flow in task_manager.py, which has an agent
recommend a robot inspection before the robot actually moves). See
models.APPROVED_AGENT_MODELS for the toy "Agent Assurance Passport"
baseline this is graded against, in eval_engine.check_entities_valid.
"""
from __future__ import annotations

from typing import Any, Dict, Optional

from .models import APPROVED_AGENT_MODELS, AgentStatus, now_iso


class Agent:
    def __init__(
        self,
        agent_id: str,
        name: str,
        model_version: Optional[str] = None,
    ) -> None:
        self.id = agent_id
        self.name = name
        self.model_version: str = model_version or APPROVED_AGENT_MODELS[0]
        self.status: AgentStatus = AgentStatus.IDLE
        self.current_task: Optional[str] = None
        self.completed_tasks = 0
        self.failed_tasks = 0
        self.created_at = now_iso()
        self.updated_at = now_iso()

    # ------------------------------------------------------------------ #
    def touch(self) -> None:
        self.updated_at = now_iso()

    def set_status(self, status: AgentStatus) -> AgentStatus:
        previous = self.status
        self.status = status
        self.touch()
        return previous

    @property
    def is_available(self) -> bool:
        return self.status == AgentStatus.IDLE and self.current_task is None

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        return {
            "id": self.id,
            "name": self.name,
            "model_version": self.model_version,
            "status": self.status.value,
            "current_task": self.current_task,
            "completed_tasks": self.completed_tasks,
            "failed_tasks": self.failed_tasks,
            "created_at": self.created_at,
            "updated_at": self.updated_at,
        }

    @staticmethod
    def from_dict(data: Dict[str, Any]) -> "Agent":
        agent = Agent(
            agent_id=data["id"],
            name=data["name"],
            model_version=data.get("model_version"),
        )
        agent.status = AgentStatus(data.get("status", "IDLE"))
        agent.current_task = data.get("current_task")
        agent.completed_tasks = data.get("completed_tasks", 0)
        agent.failed_tasks = data.get("failed_tasks", 0)
        agent.created_at = data.get("created_at", agent.created_at)
        agent.updated_at = data.get("updated_at", agent.updated_at)
        return agent
