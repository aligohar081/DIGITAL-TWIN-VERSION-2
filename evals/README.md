# Promptfoo evals — eval_engine and Groq over real and example logs

There are **three** suites here:

| Suite | Config | Grades | Grader | Assertion |
|---|---|---|---|---|
| **Live** (default) | `promptfooconfig.yaml` | Every real `logs/tasks/task_*.json` your system has actually produced | `backend/eval_engine.py` (rule-based, no LLM) | `shared/asserts/no_regressions.py` — flags any real task whose own log grades out as FAIL |
| **Examples** | `promptfooconfig.examples.yaml` | The curated, hand-labelled logs in `logs/eval_examples/` | `backend/eval_engine.py` (rule-based, no LLM) | `shared/asserts/verdict_matches.py` — checks the verdict (and failure reason) matches the known-correct answer for that fixture |
| **Groq** | `promptfooconfig.groq.yaml` | The same real logs as the Live suite | A Groq-hosted LLM (`openai/gpt-oss-20b`), used as an LLM-as-judge | `is-json` (checks the model replied with the required JSON shape) + `shared/asserts/groq_matches_engine.py` (checks the LLM's verdict agrees with the rule-based engine) |

The Groq suite hands the model four pieces of evidence per task, and
walks it through the same **three-step review** a person would actually
do — read the request, sanity-check who/what it's about, then judge
whether it got done — reporting all three as separate fields, not just a
final label:

**Evidence:**
1. **The task** (`task_json`) — what was asked for: type, requested
   robot, box, source/destination, priority. Pulled from `TASK_CREATED`
   (see `TaskManager.create_task`'s `data=`).
2. **Entities** (`entities_json`) — identity only: which robot, which
   box, which zones this task involves (ids, names, zone keys/labels).
   No state here on purpose.
3. **State difference** (`state_diff_json`) — the world before the task
   ran versus after it hit a terminal event: robot/box position+status,
   and whether the source/destination zones held this box. Captured by
   `TaskManager.snapshot_state` (`backend/task_manager.py`) and embedded
   on the log's `TASK_STARTED` and terminal events.
4. **Logs** (`task_log_json`) — the trimmed event trail, as supporting
   context for *why*.

**Review steps, and where each lands in the output:**
1. **Review the task** — read evidence (1) for context. Not itself
   graded.
2. **Validate the entities** — do the robot/box/zones in (2) actually
   resolve, and do they correspond to what (1) asked for (not missing,
   not mismatched)? → `entities_check: {valid, reason}`.
3. **Check completion** — using (3) as primary evidence, did the box
   actually end up where the task intended? → `completion_check:
   {completed, reason}`.

Overall `verdict` is PASS only if both `entities_check.valid` and
`completion_check.completed` are true — so a disagreement with the
rule-based engine is traceable to *which* step it came from, not just
"the label differs." Evidence (1)–(3) all degrade gracefully — a
`"note"` explaining what couldn't be resolved, never an error — for logs
recorded before the relevant instrumentation existed, in which case (4)
becomes the deciding evidence for step 3.

Both LLM review steps have a deterministic counterpart in
`backend/eval_engine.py`, graded the same evidence without an LLM:
`check_entities_valid` (step 2) and `check_state_transition` (step 3) —
the Live and Examples suites above pick both up automatically, no
suite-level change needed, since they're just two more checks inside
`evaluate_file()`. Evidence (1)–(3) themselves are built once, in
`backend/eval_engine.py` (`extract_task`, `extract_entities`,
`extract_state_diff`, and `build_mission_record` to combine all three) —
`generate_tests_groq.py` imports them rather than duplicating the
extraction logic, so the LLM and the rule-based engine are always
judging identical data.

The first two share `shared/providers/eval_engine_provider.py`, which just
runs `evaluate_file()` on whatever `task_log` path a test case gives it —
no LLM involved. The Groq suite instead sends the log to a real model and
grades what comes back.

## Layout

```
promptfooconfig.yaml                       live suite config (default) — grades logs/tasks/
tests.generated.yaml                       one test per logs/tasks/*.json (generated — don't hand-edit)
generate_tests.py                          regenerates tests.generated.yaml from logs/tasks/
regrade.sh                                 regenerate + eval the live suite (and, with --groq, the
                                             Groq suite too) in one command — the two steps below,
                                             together, so a freshly-run task never gets graded stale

promptfooconfig.examples.yaml              examples suite config — grades logs/eval_examples/
tests.examples.generated.yaml              one test per logs/eval_examples/*.json (generated)
generate_tests_examples.py                 regenerates tests.examples.generated.yaml

promptfooconfig.groq.yaml                  Groq LLM-as-judge suite config — grades logs/tasks/ by state diff
tests.groq.generated.yaml                  one test per logs/tasks/*.json: task + entities + state diff + logs (generated)
generate_tests_groq.py                     regenerates tests.groq.generated.yaml
.env.example                               copy to .env and add your GROQ_API_KEY (gitignored)

shared/providers/eval_engine_provider.py   runs evaluate_file() instead of calling a model
shared/asserts/no_regressions.py           live suite: flags a real FAIL
shared/asserts/verdict_matches.py          examples suite: checks verdict + reason keyword
shared/asserts/groq_matches_engine.py      groq suite: checks the LLM's verdict against the engine's
```

## Run the live suite (your system's own logs)

After running one or more tasks through the digital twin:

```bash
cd warehouse-digital-twin
python3 evals/generate_tests.py          # pick up whatever's newly in logs/tasks/
npx promptfoo eval -c evals/promptfooconfig.yaml
npx promptfoo view                       # interactive results viewer
```

A FAIL here means a real task's log graded unhealthy — worth reading its
`reasons` (shown in the results table) before you dismiss it. A WARN
(e.g. battery running low but the task still completed) still counts as
passed, same as `EvalReport.passed` in `backend/eval_engine.py`.

**The two commands above have to be run together, every time** —
`tests.generated.yaml` is a snapshot of whatever was in `logs/tasks/` the
last time you ran `generate_tests.py`; promptfoo itself never looks at
the log directory, only at that snapshot. Run a new task (or flip
`FALSE_SUCCESS_RISK` / `COLLISION_RISK` up and run one) without
regenerating first, and promptfoo will keep grading the *old* snapshot —
which reads as "nothing's ever FAIL" for the wrong reason. `regrade.sh`
does both steps in one shot, exits non-zero if anything graded FAIL (safe
to use as a CI gate), and takes `--groq` to re-judge the same real logs
through the Groq suite too:

```bash
./evals/regrade.sh            # regenerate + grade the live suite
./evals/regrade.sh --groq      # ...and the Groq LLM-as-judge suite too
```

## Run the examples suite (curated fixtures)

```bash
python3 evals/generate_tests_examples.py   # only needed after editing logs/eval_examples/
npx promptfoo eval -c evals/promptfooconfig.examples.yaml
```

This is the fixed regression fixture — one example log per failure case
the engine knows how to catch — mirroring
`backend/test_eval_engine.py::test_bundled_examples_grade_as_expected`.
It won't tell you anything about your system's real runs; use the live
suite for that.

## Run the Groq suite (LLM-as-judge)

One-time setup — you need your own [Groq](https://console.groq.com/keys)
API key:

```bash
cp evals/.env.example evals/.env
# edit evals/.env and paste your real key in place of the placeholder
```

Then, same as the live suite, regenerate after new tasks and run:

```bash
python3 evals/generate_tests_groq.py
npx promptfoo eval -c evals/promptfooconfig.groq.yaml --env-file evals/.env -j 1
npx promptfoo view
```

`-j 1` matters — the configured model's `on_demand` tier caps at 8000
tokens/minute, and the default concurrency (4) queues requests faster
than that budget allows.

For each real task log, the model is shown all four sections — task,
entities, state diff, logs — and asked to reply with **strict JSON**:

```json
{"task_id": "task_004", "verdict": "PASS", "reason": "box_001 was delivered to loading_zone and robot_01 ended idle at that zone, matching the destination zone in the after state."}
```

Three things are checked per test:
1. **Shape** — did the model actually return that exact JSON shape
   (`is-json` with a schema)?
2. **Agreement** — does its PASS/FAIL match what `backend/eval_engine.py`
   already decided for the same log via `check_state_transition`
   (`groq_matches_engine.py`)? A disagreement fails the test — not
   because the LLM is assumed wrong, but because it means one of the two
   graders needs a closer look.
3. Implicitly, whether the model actually reasoned from the *state* —
   for logs recorded before `TaskManager.snapshot_state` existed,
   `state_diff_json` is just a `"note"` explaining none was captured,
   and the model is told to fall back to the event trail instead.

This calls the real Groq API — every run costs a handful of tokens per
log, and `promptfoo view` will show you the request/response, latency,
and (if pricing is configured) cost for each one.

## Add a new example scenario

1. Drop a new `task_*.json` log in `logs/eval_examples/`.
2. Add its expected `Verdict` to `EXPECTED_EXAMPLE_VERDICTS` in
   `backend/test_eval_engine.py` (pytest checks this table too).
3. Optionally add a `REASON_KEYWORDS` entry in `generate_tests_examples.py`
   so the suite also confirms the *right* check fired, not just the final
   label.
4. `python3 evals/generate_tests_examples.py`, then re-run that suite.
