"""Command-line scenario runner for the eval engine.

Evaluates every ``task_*.json`` log file in a directory (or one/more files
named directly) against ``backend.eval_engine``, prints a human-readable
verdict for each, and can optionally write the full report as JSON.

Usage
-----
    # Grade every task this project has actually run
    python -m backend.run_evals

    # Grade one specific task log
    python -m backend.run_evals logs/tasks/task_001.json

    # Grade a different directory of task_*.json files
    python -m backend.run_evals path/to/dir

    # Grade the bundled examples that demonstrate every failure case
    python -m backend.run_evals --demo

    # Also save the full machine-readable report
    python -m backend.run_evals --out logs/evals/report.json

    # Use in CI: exit non-zero if fewer than N tasks come back healthy
    python -m backend.run_evals --fail-under 5

    # Print aggregate metrics (mission success rate, assurance coverage,
    # which check fails most often) instead of a per-check breakdown
    python -m backend.run_evals --stats
"""
from __future__ import annotations

import argparse
import glob
import json
import os
import sys
from typing import List

from .eval_engine import EvalReport, Verdict, compute_metrics, evaluate_file

BASE_DIR = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
DEFAULT_LOGS_DIR = os.path.join(BASE_DIR, "logs", "tasks")
DEMO_DIR = os.path.join(BASE_DIR, "logs", "eval_examples")

_ICON = {"PASS": "\u2713", "WARN": "!", "FAIL": "\u2717"}  # ✓ ! ✗


def discover(path: str) -> List[str]:
    if os.path.isfile(path):
        return [path]
    if os.path.isdir(path):
        return sorted(glob.glob(os.path.join(path, "task_*.json")))
    raise FileNotFoundError(f"No such file or directory: {path}")


def print_report(report: EvalReport, verbose: bool = True) -> None:
    icon = _ICON[report.verdict.value]
    label = report.task_id or "(unknown task)"
    print(f"{icon} {label}: {report.verdict.value}")
    if verbose:
        for check in report.checks:
            cicon = _ICON[check.verdict.value]
            print(f"    {cicon} {check.name:<22} {check.message}")
    elif report.verdict != Verdict.PASS:
        for reason in report.reasons:
            print(f"    - {reason}")
    print()


def print_metrics(metrics: dict) -> None:
    if metrics.get("total", 0) == 0:
        print("No reports to summarize.")
        return
    print("Metrics")
    print(f"  mission success rate   {metrics['mission_success_rate']}%  "
          f"({metrics['passed'] + metrics['warned']}/{metrics['total']} PASS or WARN)")
    print(f"  clean pass rate        {metrics['clean_pass_rate']}%  "
          f"({metrics['passed']}/{metrics['total']} strict PASS)")
    print(f"  state-diff coverage    {metrics['state_diff_coverage']}%  "
          f"(logs carrying a real before/after snapshot to grade)")
    if metrics["failures_by_check"]:
        print("  failures by check:")
        for name, count in metrics["failures_by_check"].items():
            print(f"    {_ICON['FAIL']} {name:<22} {count}")
    if metrics["warnings_by_check"]:
        print("  warnings by check:")
        for name, count in metrics["warnings_by_check"].items():
            print(f"    {_ICON['WARN']} {name:<22} {count}")
    print()


def run(files: List[str]) -> List[EvalReport]:
    reports = []
    for f in files:
        try:
            reports.append(evaluate_file(f))
        except Exception as exc:  # malformed file, etc. — report, don't crash the batch
            print(f"! Could not evaluate {f}: {exc}", file=sys.stderr)
    return reports


def main(argv: List[str] = None) -> int:
    parser = argparse.ArgumentParser(
        description="Evaluate warehouse digital twin task logs against the eval engine."
    )
    parser.add_argument(
        "paths", nargs="*", default=None,
        help="Task JSON file(s) or a directory of task_*.json files "
             f"(default: {DEFAULT_LOGS_DIR})",
    )
    parser.add_argument(
        "--demo", action="store_true",
        help=f"Evaluate the bundled example logs at {DEMO_DIR}, which "
             "include one of every failure case, instead of --paths",
    )
    parser.add_argument("--out", metavar="FILE", help="Also write the full JSON report to this path")
    parser.add_argument(
        "--quiet", action="store_true",
        help="Only print one line per task (plus reasons for anything that isn't a clean PASS)",
    )
    parser.add_argument(
        "--fail-under", type=int, default=None,
        help="Exit with status 1 if fewer than this many tasks come back as a clean PASS",
    )
    parser.add_argument(
        "--stats", action="store_true",
        help="Also print aggregate metrics — mission success rate, assurance "
             "(state-diff) coverage, and which check fails most often",
    )
    args = parser.parse_args(argv)

    if args.demo:
        targets = [DEMO_DIR]
    elif args.paths:
        targets = args.paths
    else:
        targets = [DEFAULT_LOGS_DIR]

    files: List[str] = []
    for t in targets:
        try:
            files.extend(discover(t))
        except FileNotFoundError as exc:
            print(str(exc), file=sys.stderr)
            return 2
    files = sorted(set(files))

    if not files:
        print(f"No task_*.json files found in {targets}.", file=sys.stderr)
        return 2

    reports = run(files)
    for r in reports:
        print_report(r, verbose=not args.quiet)

    passed = sum(1 for r in reports if r.verdict == Verdict.PASS)
    warned = sum(1 for r in reports if r.verdict == Verdict.WARN)
    failed = sum(1 for r in reports if r.verdict == Verdict.FAIL)
    print(f"{len(reports)} task(s) evaluated — {passed} passed, {warned} warned, {failed} failed.")

    if args.stats:
        print()
        print_metrics(compute_metrics(reports))

    if args.out:
        payload = {
            "generated_from": files,
            "totals": {"passed": passed, "warned": warned, "failed": failed, "total": len(reports)},
            "results": [r.to_dict() for r in reports],
        }
        if args.stats:
            payload["metrics"] = compute_metrics(reports)
        os.makedirs(os.path.dirname(os.path.abspath(args.out)) or ".", exist_ok=True)
        with open(args.out, "w", encoding="utf-8") as fh:
            json.dump(payload, fh, indent=2)
        print(f"Wrote full report to {args.out}")

    if args.fail_under is not None and passed < args.fail_under:
        return 1
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
