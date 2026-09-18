# RUN.md — setup & commands

Everything needed to get this project running on a new machine: the app
itself, its tests, and the three promptfoo eval suites. Run all commands
from the project root (the folder this file is in) unless stated
otherwise.

---

## 1. Prerequisites

| Tool | Version used | Notes |
|---|---|---|
| Python | 3.12 (3.10+ should work) | for the app, tests, eval engine |
| Node.js + npm | v22.23 / npm 10.9 (`^20.20.0 \|\| >=22.22.0` required) | to run promptfoo |
| Groq API key | — | only needed for the Groq LLM-as-judge suite; free at [console.groq.com/keys](https://console.groq.com/keys) |

`npm install -g promptfoo` once, then call it as `promptfoo` directly —
**avoid `npx promptfoo`**. `npx` re-resolves the package against the npm
registry over the network on every invocation instead of just using what's
installed; on a slow or flaky connection this has been observed to hang
for 10+ minutes before failing with `ERR_SOCKET_TIMEOUT`, which looks
exactly like a stuck eval but has nothing to do with promptfoo itself. If
you use `nvm`, make sure the right Node version is active first:

```bash
export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use v22.23.1
promptfoo --version   # confirm it resolves before running an eval
```

---

## 2. Backend setup

```bash
cd warehouse-digital-twin

python3 -m venv .venv
source .venv/bin/activate        # Windows: .venv\Scripts\activate
pip install -r requirements.txt  # Flask + pytest
```

Or just use the one-command launcher, which does all of the above itself:

```bash
./run.sh
```

## 3. Run the app

```bash
source .venv/bin/activate
python -m backend.app
```

Dashboard: `http://127.0.0.1:5000` (override with `WAREHOUSE_HOST` /
`WAREHOUSE_PORT` env vars). Stop with Ctrl+C.

## 4. Run the test suite (pytest)

```bash
source .venv/bin/activate
python -m pytest                                      # everything
python -m pytest -v                                   # verbose
python -m pytest -k "collision or battery"            # a subset by keyword
python -m pytest backend/test_eval_engine.py           # just the eval engine's own tests
```

## 5. Eval engine CLI (rule-based grading, no promptfoo, no API)

Grades task logs (`logs/tasks/*.json`) against fixed pass/warn/fail rules
in `backend/eval_engine.py`.

```bash
source .venv/bin/activate
python -m backend.run_evals                            # grade every real logs/tasks/*.json
python -m backend.run_evals logs/tasks/task_006.json    # grade one file
python -m backend.run_evals --demo                      # grade the curated logs/eval_examples/
python -m backend.run_evals --out logs/evals/report.json    # also save full JSON report
python -m backend.run_evals --fail-under 5               # CI-style: exit 1 if <5 tasks pass
python -m backend.run_evals --stats                      # mission success rate, assurance coverage, top failing checks
```

---

## 6. Promptfoo suites

Three separate suites live in `evals/`. Full details in
`evals/README.md`; the short version is below. All commands assume
`cd warehouse-digital-twin` (project root) unless noted.

### 6a. Live suite — real logs, rule-based, no API key needed

Grades every real `logs/tasks/task_*.json` your system has produced,
using the same rule-based engine as section 5, through promptfoo.

```bash
python3 evals/generate_tests.py
promptfoo eval -c evals/promptfooconfig.yaml
promptfoo view
```

### 6b. Examples suite — curated fixtures, rule-based, no API key needed

Grades the 11 hand-built example logs in `logs/eval_examples/` (one per
known failure type, plus real simulator runs demonstrating the
state-transition and entities-valid checks) against their known-correct
answers.

```bash
python3 evals/generate_tests_examples.py
promptfoo eval -c evals/promptfooconfig.examples.yaml
```

### 6c. Groq suite — real logs, graded by an actual LLM in three steps

For each real task log, sends the model four sections of evidence — (1)
**the task** as created (type, robot, box, source/destination), (2)
**entities** involved (which robot/box/zones, by id and name), (3) the
before/after **state diff** (position/status and zone occupancy — the
primary evidence), (4) the event trail (supporting context) — and has it
review in three steps, each reported separately in the output:

1. **Review the task** — read (1) for context.
2. **Validate the entities** — do (2)'s robot/box/zones actually resolve
   and correspond to what (1) asked for? → `entities_check: {valid, reason}`
3. **Check completion** — using (3), did the box end up where the task
   intended? → `completion_check: {completed, reason}`

Overall `verdict` is PASS only if both steps 2 and 3 pass, then gets
checked against the rule-based engine's own `check_state_transition`
result (a disagreement is flagged, not hidden — traceable to *which*
step it came from, not just "the label differs"). Any section without
enough data (older logs) degrades to a plain-English note instead of
erroring.

**One-time setup — add your Groq API key:**

```bash
cp evals/.env.example evals/.env
# edit evals/.env, replace the placeholder with a real key from
# https://console.groq.com/keys
```

`evals/.env` is gitignored — it never gets committed.

**Run it:**

```bash
python3 evals/generate_tests_groq.py
promptfoo eval -c evals/promptfooconfig.groq.yaml --env-file evals/.env -j 1
promptfoo view
```

Two flags matter here and are easy to forget:
- `--env-file evals/.env` — promptfoo only auto-loads `.env` from the
  directory you run it *in*; since we run from the project root and the
  key lives in `evals/`, this flag is required.
- `-j 1` — the Groq model configured (`openai/gpt-oss-20b`, on-demand
  tier) is capped at **8000 tokens/minute**. The default concurrency (4)
  throws requests at it faster than that budget allows, which causes
  requests to queue for minutes and eventually time out. `-j 1` keeps it
  under the cap.

**Expect it to be slow for the full batch, not just one log.** Even at
`-j 1`, the shared 8000-tokens/minute budget means consecutive calls
often wait out most of a minute between each other — 7 real logs took
about 19 minutes end to end in testing, versus ~3 seconds for a single
log run in isolation. For a quick demo (e.g. showing a supervisor), grade
just one log instead of the whole batch:

```bash
python3 -c "
import sys; sys.path.insert(0, 'evals')
from generate_tests_groq import build_tests
open('evals/tests.groq.generated.yaml', 'w').write(
    build_tests(['logs/eval_examples/task_109_state_pick_and_deliver.json'])
)"
promptfoo eval -c evals/promptfooconfig.groq.yaml --env-file evals/.env -j 1
promptfoo view

# afterwards, restore the full real-log suite:
python3 evals/generate_tests_groq.py
```

**Which model is configured, and why:** `evals/promptfooconfig.groq.yaml`
currently points at `groq:openai/gpt-oss-20b`. `llama-3.1-8b-instant` was
tried and confirmed (repeatedly, against the live
`api.groq.com/openai/v1/models` endpoint, with two different keys) to no
longer be served by Groq at all — it 404s regardless of which key is
used. If that ever changes, or your lead's account genuinely has Llama
access, the model id can be swapped back in that file — see the comment
directly above the `providers:` block in that file for exactly what to
change and why.

---

## 7. Troubleshooting

| Symptom | Cause | Fix |
|---|---|---|
| `promptfoo eval` (or any promptfoo command) hangs for 10+ minutes doing nothing visible, sometimes ending in `npm ERR! code ERR_SOCKET_TIMEOUT` | You ran `npx promptfoo` and `npx` is stuck re-resolving the package over the network instead of using the installed copy — often because `nvm`'s Node version silently dropped out of `PATH` in that shell | Run `export NVM_DIR="$HOME/.nvm"; . "$NVM_DIR/nvm.sh"; nvm use v22.23.1` then call `promptfoo` directly (no `npx`) — see section 1 |
| `Missing GROQ_API_KEY` | `.env` not picked up | Add `--env-file evals/.env` to the command |
| `API error: 404 ... model_not_found` | The configured model isn't served to this key | Use a model your key actually has — check with `curl https://api.groq.com/openai/v1/models -H "Authorization: Bearer $GROQ_API_KEY"` |
| `API error: 413 Payload Too Large` / requests hang then time out after 5 minutes | Prompt too big for the account's tokens-per-minute limit, or too many concurrent requests | Re-run `python3 evals/generate_tests_groq.py` (keeps prompts trimmed) and always pass `-j 1` |
| Eval looks "stuck" at some % with no progress for minutes, but is genuinely still running | Same TPM rate limit — requests are queued, not frozen (see the timing note in 6c) | Wait it out, or Ctrl+C and re-run against fewer logs |
| Any promptfoo run | — | A version-mismatch banner ("current version ... lower than latest") is just a nag, safe to ignore |
| `python -m pytest` fails with `ModuleNotFoundError: No module named 'yaml'` deep inside `launch_testing`/ROS | An unrelated ROS pytest plugin on this machine gets auto-discovered | Run with `PYTEST_DISABLE_PLUGIN_AUTOLOAD=1 python -m pytest -p no:cacheprovider` |
| `test_real_task_006_box_conflict_is_caught` fails | Pre-existing: whatever's currently in `logs/tasks/task_006.json` no longer contains the box-conflict scenario the test expects (real logs get overwritten by task-id reuse on every fresh app run) | Not caused by the state-transition feature — confirmed by neutralizing it and reproducing the same failure. Safe to ignore, or regenerate a real conflict scenario if you want the test green |
| `test_idle_robot_with_a_low_battery_charges_itself` fails/times out | Pre-existing simulator behavior: the robot's battery runs out before it reaches the charger from its demo spawn point | Same as above — confirmed unrelated to this change by isolating it |

---

## 8. Project layout (relevant parts)

```
warehouse-digital-twin/
├── run.sh                       one-command app launcher
├── requirements.txt
├── TRUST_LAYER.md                Tier 1/2/3 scope mapping from the "Physical AI Trust Layer" reference doc
├── backend/
│   ├── app.py                   Flask API / dashboard
│   ├── eval_engine.py           rule-based log grader (PASS/WARN/FAIL + reasons)
│   ├── run_evals.py             CLI for eval_engine.py
│   ├── task_manager.py          task lifecycle — also snapshots before/after world state per task
│   ├── tests.py                 main pytest suite
│   └── test_eval_engine.py      pytest suite for eval_engine.py
├── logs/
│   ├── tasks/                   real task logs your system has produced
│   └── eval_examples/           curated example logs, one per known failure case
└── evals/                       promptfoo suites (see evals/README.md)
    ├── promptfooconfig.yaml             live suite (section 6a)
    ├── promptfooconfig.examples.yaml    examples suite (section 6b)
    ├── promptfooconfig.groq.yaml        Groq LLM-as-judge suite (section 6c)
    ├── generate_tests*.py               regenerate each suite's test list
    ├── .env.example                     copy to .env, add GROQ_API_KEY
    └── shared/                          providers + assertions used by the suites above
```
