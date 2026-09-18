#!/usr/bin/env bash
# Regrade every real task your system has actually run, right now.
#
# The promptfoo suites in this directory only ever look at whatever test
# list is already sitting on disk (evals/tests.generated.yaml,
# evals/tests.groq.generated.yaml) — running new tasks through the
# digital twin doesn't refresh that list by itself. Forgetting to
# regenerate it before `npx promptfoo eval` is the single most common
# reason "nothing shows FAIL" even when a real task actually did fail
# (e.g. FALSE_SUCCESS_RISK/COLLISION_RISK caught one). This script does
# both steps together, every time.
#
# Exits non-zero if any real task graded out unhealthy (a real FAIL —
# see evals/shared/asserts/no_regressions.py) or, with --groq, if the
# LLM judge disagreed with the rule-based engine — so it's safe to wire
# into CI as a gate, not just a local convenience.
#
# Usage:
#   evals/regrade.sh            # default suite: rule-based, no LLM, no API key needed
#   evals/regrade.sh --groq     # also re-judge the same real logs with the
#                                 Groq LLM-as-judge suite (needs GROQ_API_KEY,
#                                 or evals/.env — see evals/README.md)
set -uo pipefail
cd "$(dirname "$0")/.."

if [ -f .venv/bin/activate ]; then
  # shellcheck disable=SC1091
  source .venv/bin/activate
fi

status=0

echo "==> Regenerating evals/tests.generated.yaml from logs/tasks/*.json"
python3 evals/generate_tests.py || exit 1

echo
echo "==> Grading real logs (rule-based eval_engine, no LLM)"
npx promptfoo eval -c evals/promptfooconfig.yaml --no-cache
[ $? -eq 0 ] || status=1

if [ "${1:-}" = "--groq" ]; then
  echo
  echo "==> Regenerating evals/tests.groq.generated.yaml"
  python3 evals/generate_tests_groq.py || exit 1

  echo
  echo "==> Grading the same real logs with a Groq LLM-as-judge"
  npx promptfoo eval -c evals/promptfooconfig.groq.yaml --env-file evals/.env -j 1 --no-cache
  [ $? -eq 0 ] || status=1
fi

echo
if [ "$status" -eq 0 ]; then
  echo "==> All real tasks graded out healthy (PASS or WARN)."
else
  echo "==> At least one real task graded FAIL — see the [FAIL] rows above."
fi
echo "==> npx promptfoo view    # browse the full results in the browser"

exit "$status"
