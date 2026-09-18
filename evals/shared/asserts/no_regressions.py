"""Shared promptfoo assertion for real task logs: does this log's own
grading come back healthy?

There's no pre-known "correct" verdict for a live run the way there is for
the curated logs/eval_examples/ fixtures (see verdict_matches.py) — a real
task might legitimately fail for reasons outside the eval engine's
control. So instead of matching a label, this flags an actual regression:
any real task whose own log grades out as FAIL is something worth
looking at, using the same PASS/WARN/FAIL semantics as
backend/eval_engine.py (WARN still counts as "passed", just not clean).

``output`` is whatever the provider returned as its "output" field — for
shared/providers/eval_engine_provider.py that's EvalReport.to_dict().
"""
from __future__ import annotations


def get_assert(output, context):
    if not isinstance(output, dict) or "verdict" not in output:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"provider did not return a verdict report: {output!r}",
        }

    task_id = output.get("task_id") or "(unknown task)"
    verdict = output.get("verdict")
    passed = output.get("passed", verdict != "FAIL")

    if not passed:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"{task_id} graded FAIL — reasons: {output.get('reasons')}",
        }

    if verdict == "WARN":
        return {
            "pass": True,
            "score": 0.5,
            "reason": f"{task_id} graded WARN — reasons: {output.get('reasons')}",
        }

    return {
        "pass": True,
        "score": 1.0,
        "reason": f"{task_id} graded a clean PASS",
    }
