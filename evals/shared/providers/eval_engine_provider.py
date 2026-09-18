"""promptfoo provider that runs a warehouse task log through the project's
own eval_engine (backend/eval_engine.py) instead of calling an LLM.

This lets promptfoo drive the same regression matrix as
backend/test_eval_engine.py — one declared test case per task log — so a
change that silently breaks a check shows up as a failed promptfoo eval,
not only as a failed pytest run.

promptfoo calls ``call_api(prompt, options, context)`` for every test case.
``context["vars"]`` carries whatever the test declared (task_log, plus the
expectations checked by shared/asserts/verdict_matches.py); the rendered
``prompt`` text is informational only — there is no model in the loop, so
it is never sent anywhere.
"""
from __future__ import annotations

import os
import sys

# evals/shared/providers/eval_engine_provider.py -> project root (three
# levels up) so `backend` can be imported as the real package it is
# (eval_engine.py uses a relative import, `from .models import CONFIG`).
PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.eval_engine import evaluate_file  # noqa: E402


def call_api(prompt, options, context):
    variables = (context or {}).get("vars", {}) or {}
    task_log = variables.get("task_log")
    if not task_log:
        return {"error": "test case is missing the required 'task_log' var"}

    path = task_log if os.path.isabs(task_log) else os.path.join(PROJECT_ROOT, task_log)
    if not os.path.isfile(path):
        return {"error": f"task log not found: {path}"}

    try:
        report = evaluate_file(path)
    except Exception as exc:  # malformed log, etc. — surface as a provider error, not a crash
        return {"error": f"evaluate_file({path!r}) raised {exc!r}"}

    # The full structured report (verdict, per-check results, reasons,
    # summary) is the "output" — shared/asserts/verdict_matches.py reads it
    # straight off, no separate extraction adapter needed since it's
    # already structured data rather than prose.
    return {"output": report.to_dict()}
