"""Shared promptfoo assertion for the Groq suite: does *this test case's own
row* come back healthy against the real task outcome — the deterministic
engine's verdict (backend/eval_engine.py) — not against whether Groq's
grading happened to be correct.

Without this, a task that genuinely failed (e.g. a FALSE_SUCCESS_RISK
fault, or any other real fault check_state_transition/check_battery/etc.
catches) could still show up as a fully green row in promptfoo's results:
is-json only checks the reply's shape, and groq_matches_engine.py (see
that file) only checks that Groq's verdict *agrees* with the engine's —
both of those can be perfectly satisfied by a grader that correctly
*recognizes* the failure, which says nothing about whether the task
itself is something worth calling a "Pass". A human skimming promptfoo's
summary table (or its "N passed" count) reads a green row as "this task
was fine" — this assertion makes that reading correct: the row's own
pass/fail glyph tracks the real task outcome, and the deciding-agreement
detail from groq_matches_engine.py stays available alongside it as a
separate signal (did the grader get it right), not conflated with this
one (did the task get it right).

``output`` is the raw text Groq returned — unused here, this assertion
only needs the task_log var to grade the real world, independent of
whatever Groq said.
"""
from __future__ import annotations

import os
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.eval_engine import evaluate_file  # noqa: E402


def get_assert(output, context):
    variables = (context or {}).get("vars", {}) or {}
    task_log = variables.get("task_log")
    if not task_log:
        # Nothing to check the real outcome against — leave the verdict to
        # the other assertions (shape, agreement) instead of failing blind.
        return {
            "pass": True,
            "score": 1.0,
            "reason": "no task_log var to check the real outcome against",
        }

    path = task_log if os.path.isabs(task_log) else os.path.join(PROJECT_ROOT, task_log)
    report = evaluate_file(path)
    healthy = report.passed  # PASS or WARN both count as healthy; FAIL doesn't

    if not healthy:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"{report.task_id or '(unknown task)'} did NOT actually "
                f"succeed — engine verdict {report.verdict.value}: {report.reasons}"
            ),
        }

    return {
        "pass": True,
        "score": 1.0 if report.verdict.value == "PASS" else 0.5,
        "reason": f"{report.task_id or '(unknown task)'} actually succeeded — engine verdict {report.verdict.value}",
    }
