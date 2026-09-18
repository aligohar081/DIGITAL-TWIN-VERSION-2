"""Policy-as-code: the tunable trust-layer values, editable without
touching Python.

Everything this module can override already lived as hardcoded values in
backend/models.py (``CONFIG``, ``APPROVED_FIRMWARE_VERSIONS``,
``APPROVED_AGENT_MODELS``, ``CERTIFICATION_REQUIREMENTS``,
``ROBOT_CLASS_PRESETS``, ``OPERATOR_ROLE_PRESETS``). This module's only job is to let an optional
``policies.yaml`` at the project root override them at process start (via
the call at the bottom of models.py) or on demand (``POST
/api/policies/reload``), while every other module keeps importing the
exact same names from ``backend.models`` completely unchanged.

That only works because the containers below are never reassigned, only
mutated in place (``list[:] = ...``, ``dict.clear(); dict.update(...)``).
A module that already did ``from .models import APPROVED_AGENT_MODELS``
keeps a reference to the very same list object — mutating it in place is
visible there too; replacing it with a new list (``APPROVED_AGENT_MODELS
= [...]``) would silently orphan every module that imported the name
before the reassignment. See apply_policy().

See ``policies.example.yaml`` for the file shape. A missing, empty, or
unparsable policies.yaml is never fatal — it just means "use the
built-in defaults", the same values that shipped before this existed.
"""
from __future__ import annotations

import os
from typing import Any, Dict

try:
    import yaml  # PyYAML — see requirements.txt
except Exception:  # pragma: no cover - defensive: policies.yaml just won't load
    yaml = None

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_POLICY_PATH = os.path.join(BASE_DIR, "policies.yaml")


def read_policy_file(path: str = DEFAULT_POLICY_PATH) -> Dict[str, Any]:
    """The raw dict from `path`, or {} if it doesn't exist, can't be
    parsed, or PyYAML isn't installed."""
    if yaml is None or not os.path.exists(path):
        return {}
    try:
        with open(path, "r", encoding="utf-8") as fh:
            data = yaml.safe_load(fh) or {}
        return data if isinstance(data, dict) else {}
    except Exception:
        return {}


def apply_policy(data: Dict[str, Any]) -> None:
    """Mutate backend.models's shared containers in place from `data`.
    Idempotent and safe to call more than once (e.g. a live reload)."""
    from . import models  # deferred: models.py itself calls load_policies() at import time

    config = data.get("config") or {}
    for key, value in config.items():
        if key in models.CONFIG:
            models.CONFIG[key] = value

    firmware = data.get("approved_firmware_versions")
    if firmware:
        models.APPROVED_FIRMWARE_VERSIONS[:] = list(firmware)

    agent_models = data.get("approved_agent_models")
    if agent_models:
        models.APPROVED_AGENT_MODELS[:] = list(agent_models)

    certs = data.get("certification_requirements")
    if certs:
        models.CERTIFICATION_REQUIREMENTS.clear()
        models.CERTIFICATION_REQUIREMENTS.update(certs)

    classes = data.get("robot_classes")
    if classes:
        models.ROBOT_CLASS_PRESETS.clear()
        models.ROBOT_CLASS_PRESETS.update(classes)

    roles = data.get("operator_roles")
    if roles:
        models.OPERATOR_ROLE_PRESETS.clear()
        models.OPERATOR_ROLE_PRESETS.update(roles)


def load_policies(path: str = DEFAULT_POLICY_PATH) -> Dict[str, Any]:
    """Load `path` (if present) over the built-in defaults, and return the
    resulting effective policy. Called once at import time by
    backend/models.py; call again any time (e.g. from
    POST /api/policies/reload) to pick up edits without a restart."""
    apply_policy(read_policy_file(path))
    return effective_policy()


def effective_policy() -> Dict[str, Any]:
    """The policy values actually in effect right now — for transparency
    (GET /api/policies), not just what's on disk."""
    from . import models

    return {
        "config": dict(models.CONFIG),
        "approved_firmware_versions": list(models.APPROVED_FIRMWARE_VERSIONS),
        "approved_agent_models": list(models.APPROVED_AGENT_MODELS),
        "certification_requirements": dict(models.CERTIFICATION_REQUIREMENTS),
        "robot_classes": dict(models.ROBOT_CLASS_PRESETS),
        "operator_roles": dict(models.OPERATOR_ROLE_PRESETS),
        "source_file": DEFAULT_POLICY_PATH if os.path.exists(DEFAULT_POLICY_PATH) else None,
    }
