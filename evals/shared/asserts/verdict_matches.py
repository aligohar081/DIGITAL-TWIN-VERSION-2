"""Shared promptfoo assertion: does the eval_engine's verdict — and, for
non-PASS cases, the right reason — match what this test case declares?

Test cases set up to two vars (see evals/generate_tests.py):
    expected_verdict          "PASS" | "WARN" | "FAIL"
    expected_reason_contains  optional substring that must appear
                               (case-insensitive) somewhere in report.reasons

``output`` is whatever the provider returned as its "output" field — for
shared/providers/eval_engine_provider.py that's EvalReport.to_dict().
"""
from __future__ import annotations


def get_assert(output, context):
    variables = (context or {}).get("vars", {}) or {}
    expected_verdict = variables.get("expected_verdict")
    expected_reason = variables.get("expected_reason_contains")

    if not isinstance(output, dict) or "verdict" not in output:
        return {
            "pass": False,
            "score": 0.0,
            "reason": f"provider did not return a verdict report: {output!r}",
        }

    actual_verdict = output.get("verdict")
    if expected_verdict and actual_verdict != expected_verdict:
        return {
            "pass": False,
            "score": 0.0,
            "reason": (
                f"expected verdict {expected_verdict!r}, got {actual_verdict!r} "
                f"— reasons: {output.get('reasons')}"
            ),
        }

    if expected_reason:
        haystack = " ".join(output.get("reasons") or []).lower()
        if expected_reason.lower() not in haystack:
            return {
                "pass": False,
                "score": 0.0,
                "reason": (
                    f"expected a reason containing {expected_reason!r}, "
                    f"got reasons: {output.get('reasons')}"
                ),
            }

    return {
        "pass": True,
        "score": 1.0,
        "reason": f"verdict {actual_verdict} matched expectations",
    }
