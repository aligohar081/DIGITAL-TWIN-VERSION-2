"""Shared promptfoo assertion for the Groq-graded suite: does the LLM's
overall PASS/FAIL verdict agree with our own deterministic engine
(backend/eval_engine.py) for the same log?

This is a sanity check on the grader itself, not a JSON-shape check (see
the ``is-json`` assertion in promptfooconfig.groq.yaml for that, which
also enforces the three-step shape: entities_check, completion_check,
verdict). If Groq's judgment disagrees with the rule-based engine, that's
worth a human reading — whichever one turns out to be right — and the
reason string here surfaces *which* step it was (entity validity vs task
completion), not just the final label.

``output`` is the raw text Groq returned (a plain chat provider's output
is a string as far as promptfoo assertions are concerned, even when the
model was asked to reply with JSON) — this parses it directly.
"""
from __future__ import annotations

import json
import os
import re
import sys

PROJECT_ROOT = os.path.abspath(os.path.join(os.path.dirname(__file__), "..", "..", ".."))
if PROJECT_ROOT not in sys.path:
    sys.path.insert(0, PROJECT_ROOT)

from backend.eval_engine import evaluate_file  # noqa: E402

_FENCE_RE = re.compile(r"^```(?:json)?\s*(.*?)\s*```$", re.DOTALL)


def _extract_json(text: str):
    text = text.strip()
    fence = _FENCE_RE.match(text)
    if fence:  # model added ```json fences despite being told not to
        text = fence.group(1)
    return json.loads(text)


def _step_summary(llm: dict) -> str:
    """Compact 'step 2 / step 3' summary for the assertion's reason
    string, so a disagreement is traceable to *which* step it came from
    without having to open the raw response."""
    entities = llm.get("entities_check") or {}
    completion = llm.get("completion_check") or {}
    parts = []
    if "valid" in entities:
        parts.append(f"entities_valid={entities.get('valid')} ({entities.get('reason')!r})")
    if "completed" in completion:
        parts.append(f"completed={completion.get('completed')} ({completion.get('reason')!r})")
    return "; ".join(parts) if parts else "(no step breakdown in response)"


def get_assert(output, context):
    variables = (context or {}).get("vars", {}) or {}
    task_log = variables.get("task_log")

    raw = output if isinstance(output, str) else json.dumps(output)
    try:
        llm = _extract_json(raw)
    except Exception as exc:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"Groq's response was not parseable JSON: {exc} — raw: {raw!r}",
        }

    llm_verdict = str(llm.get("verdict", "")).strip().upper()
    if llm_verdict not in ("PASS", "FAIL"):
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"Groq returned an unrecognised verdict {llm_verdict!r} (expected PASS or FAIL)",
        }

    if not task_log:
        # Nothing to cross-check against — just confirm the shape is sane.
        return {
            "pass": True,
            "score": 1.0,
            "reason": f"Groq verdict: {llm_verdict} — {llm.get('reason')} — {_step_summary(llm)}",
        }

    path = task_log if os.path.isabs(task_log) else os.path.join(PROJECT_ROOT, task_log)
    engine_report = evaluate_file(path)
    engine_healthy = engine_report.passed  # PASS or WARN both count as healthy
    llm_healthy = llm_verdict == "PASS"

    if engine_healthy != llm_healthy:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"Disagreement — engine: {engine_report.verdict.value} "
                f"({engine_report.reasons}); Groq: {llm_verdict} "
                f"({llm.get('reason')!r}) — {_step_summary(llm)}"
            ),
        }

    return {
        "pass": True,
        "score": 1.0,
        "reason": (
            f"Agree — engine: {engine_report.verdict.value}; "
            f"Groq: {llm_verdict} ({llm.get('reason')!r}) — {_step_summary(llm)}"
        ),
    }
