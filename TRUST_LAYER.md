# Trust Layer — mapping from "The Trust Layer for the Physical AI"

This project's `entities_valid` / `state_transition` checks, the toy robot
firmware baseline, and the Mission Authorization Record were inspired by a
reference document — "The Trust Layer for the Physical AI" (Cytex /
AICenturion) — describing an enterprise product for governing fleets of
robots, AI agents, and human workers across an organization: continuously
checking whether a given combination of agent, robot, and person can be
trusted to do a job, before and after it runs.

This file records, honestly, how much of that document actually applies to
*this* project, and exactly what got built.

## The honest scope

This document describes an enterprise-scale SaaS product for governing
fleets of robots, AI agents, and human workers across an entire
organization, with real firmware SBOMs, real HRIS integration, and real
model/tool governance. This project is a single-warehouse simulation — so
the honest thing to say up front: **the depth doesn't match** — there's no
real hardware, no real AI model calling the shots, no real employee
database. But the document's core structural idea — three distinct actor
classes (agent, robot, human), each with its own toy "assurance passport",
converging into one task record that's graded on both *who was involved*
and *what actually happened* — is now genuinely implemented, at toy scale,
for all three actors, not just the robot. What follows is the whole thing
from the document, broken down honestly by effort, alongside what's
actually been built.

## The full breakdown, tier by tier

### Tier 1 — Easy (implemented, now for all three actor classes)

| Concept in the doc | What it means for this project | Status |
|---|---|---|
| **Three actor classes** (AI agent, robot, human) | Each is a first-class entity with its own task type, not just robots-and-boxes | ✅ `Robot` (existing), `Agent` (`backend/agent.py`), `Operator` (`backend/operator.py`) |
| **"Entities check" / qualification validity, per actor** | Robot: not `ERROR`/`STOPPED`, not critically low battery, approved firmware. Agent: not `ERROR`, approved model version. Operator: not off duty, holds the certification the task actually required | ✅ `check_entities_valid` in `backend/eval_engine.py`, generalized to all three |
| **Robot Assurance Passport** (toy SBOM) | Firmware version checked against an approved baseline | ✅ `Robot.firmware_version` + `models.APPROVED_FIRMWARE_VERSIONS` |
| **Agent Assurance Passport** (model/tool version) | AI agent's `model_version` checked against an approved baseline | ✅ `Agent.model_version` + `models.APPROVED_AGENT_MODELS` |
| **Worker Qualification Passport** | Operator's `certifications` checked against what the task type requires | ✅ `Operator.certifications` + `models.CERTIFICATION_REQUIREMENTS` |
| **"Interaction in the environment"** — the document's own worked maintenance-mission example (agent recommends → robot physically inspects → human signs off) | One task type chaining all three actors, with a real robot navigating the grid via A* | ✅ `TaskType.MIXED_MAINTENANCE_MISSION` — see `TaskManager.start_task()`/`complete_task()` for the agent recommendation / operator sign-off hooks, `TaskPlanner.plan()` for the robot's real navigation |
| **Mission Authorization Record** | One consolidated artifact naming exactly who/what was involved in a task and whether it was trustworthy — now spanning all three actors | ✅ `extract_task` / `extract_entities` / `extract_state_diff` / `build_mission_record` in `backend/eval_engine.py` |
| **Evaluation Factory** | A repeatable, automated evaluation harness | ✅ Already existed — `evals/` (three promptfoo suites) |
| **Metrics** (mission success rate, blocked jobs, coverage %) | Aggregate numbers over graded task logs | ✅ `compute_metrics()` in `backend/eval_engine.py`, `python -m backend.run_evals --stats` |

### Tier 1 — also implemented

| Concept in the doc | What it means for this project | Status |
|---|---|---|
| **Pre-execution authorization gate** | `TaskManager.validate()` now hard-blocks a task at creation (`422`) if an explicitly-requested robot/agent/operator fails eligibility (bad status, unapproved firmware/model, missing certification) — no longer only graded after the fact. `AUTO` selection (`select_robot()`, and the agent/operator `AUTO` branches in `validate()`) skips ineligible candidates the same way it already skips busy ones. The rule lives once, in `backend/eligibility.py`, shared with `eval_engine.check_entities_valid` so the gate and the after-the-fact grade can't disagree. One deliberate exception: a critical battery is *not* part of the hard gate, because the planner already has a real recovery path for it (a prepended recharge detour) — gating it would break that feature; the eval engine still grades battery after the fact. | ✅ `backend/eligibility.py`, `TaskManager.validate()` / `select_robot()` in `backend/task_manager.py` |
| **Per-entity work authorization (which jobs, not just whether the entity is fit)** | `Robot.allowed_task_types` — an optional per-robot allowlist of task types, checked by the exact same `robot_eligibility()` the eligibility gate already used for status/firmware, both at creation (`422` for an explicitly-named robot outside its own configured list) and by `AUTO` scoring (skips it the same way). `None`/empty means unrestricted — every robot's behaviour before this existed. The doc's own framing is "can this combination of agent/robot/person be trusted with *this* job", not just "is this entity generally fit" — this is that distinction, scoped to robots only for now (agents/operators don't yet have an equivalent per-task-type allowlist). | ✅ `backend/robot.py`, `backend/eligibility.py`, `DigitalTwin.set_robot_capabilities` |
| **Agent/operator "own work", beyond inspect-and-approve** | `AGENT_REPLAN` (recommends `AUTO` assignment across the pending queue without acting on it), `AGENT_AUDIT` (reports on recent log severity + last Mock CI result), `OPERATOR_APPROVAL` (authorizes a robot back into service — deliberately does *not* re-check that robot's own eligibility), `OPERATOR_MAINTENANCE_SIGNOFF` (reports a named robot's firmware baseline status). All four are `INSTANT` like `AGENT_INSPECTION`/`HUMAN_INSPECTION` — no physical movement, gated/graded the same way — but each reads real, live twin state to produce its recommendation rather than being a bare rubber stamp. | ✅ `TaskType.AGENT_REPLAN` / `AGENT_AUDIT` / `OPERATOR_APPROVAL` / `OPERATOR_MAINTENANCE_SIGNOFF` in `backend/models.py`, `TaskManager._run_instant()` in `backend/task_manager.py` |

### Tier 2 — Medium (not yet built)

| Concept | What it'd look like here |
|---|---|
| **Authorization-changing events** | If a robot's simulated firmware or battery drops below policy mid-task, flag or pause the in-progress task |
| **Governance Layer (policy-as-code)** | A `policies.yaml` (`min_battery`, `restricted_zones`, `required_certifications`, ...) read by both the pre-execution gate and the eval engine, instead of hardcoded thresholds/tables in `models.py` |
| **Decision Graph (queryable history)** | A small script answering questions like "every task assigned to robot_01 while battery < 20%", or "every MIXED_MAINTENANCE_MISSION Lee signed off on", across all logs, without a real graph database |
| **Agent Assurance record for the Groq grader itself** | Record which model/prompt version graded each task alongside the verdict (distinct from `Agent`/`APPROVED_AGENT_MODELS` above, which model the *simulated* agents in the warehouse, not the real Groq model doing the grading) |

### Tier 3 — Complex or not applicable (not planned)

| Concept | Why it doesn't fit |
|---|---|
| Real SBOM/CycloneDX, vendor advisories, cryptographic attestation | No real hardware or firmware exists — robots are Python objects with a `status` enum |
| Real HRIS integration, credential issuers | No employees exist in this system, and no external identity system to integrate with |
| Full Physical Work Graph as an enterprise knowledge graph | Cross-org, multi-tenant graph database with ontology design — wildly out of scope for one warehouse |
| Decision Twin (counterfactual simulation of alternate routes) | Would need to re-run the simulator with an alternate assignment and diff outcomes — real engineering, not a quick add |
| Change-triggered evaluation reruns across dependent missions | Needs a dependency graph between tasks/evaluations and an event bus — real distributed-systems complexity for a single-process demo |
| Cyber-physical security evaluation dimension | Assumes real attackers and real command tampering — no real network/security boundary exists in a single-process simulation |
| Human-factors evaluation dimension (fatigue, alarm design, instruction clarity) | `Operator` now exists and is checked for *qualification*, but there's no simulated human behavior (workload, response time, error rate) to grade — that would need a much richer human model than a certifications list |

## Where Tier 1 lives in the codebase

```
backend/models.py         APPROVED_FIRMWARE_VERSIONS / APPROVED_AGENT_MODELS / CERTIFICATION_REQUIREMENTS
                           — the three toy approved baselines
                           TaskType.AGENT_INSPECTION / HUMAN_INSPECTION / MIXED_MAINTENANCE_MISSION
                           AgentStatus, OperatorStatus
backend/robot.py           Robot.firmware_version
backend/agent.py           Agent — the AI-agent actor class (model_version, status)
backend/operator.py        Operator — the human actor class (certifications, status)
backend/digital_twin.py    self.agents / self.operators, find_agent/find_operator, add_agent/add_operator,
                            DEMO_AGENTS / DEMO_OPERATORS (one agent, two operators — one fully
                            certified, one only partially, so AUTO-assignment has a real mix to pick from)
backend/eligibility.py     robot_eligibility/agent_eligibility/operator_eligibility — the one
                            shared rule the gate and the grade both call
backend/task_manager.py    Task.agent_id/operator_id/required_certification
                            TaskManager.validate() — resolves agent/operator/robot AND now hard-blocks
                             (422) on ineligibility via backend/eligibility.py, not just existence
                            TaskManager.select_robot() — AUTO scoring skips ineligible robots too
                            TaskManager._run_instant() — AGENT_INSPECTION/HUMAN_INSPECTION resolve immediately
                            TaskManager.start_task()/complete_task() — the MIXED_MAINTENANCE_MISSION
                             agent-recommends / operator-signs-off hooks
                            TaskManager.snapshot_state() — captures before/after world state for all
                             three actors, not just robot/box
backend/task_planner.py    TaskPlanner.plan() — MIXED_MAINTENANCE_MISSION's robot leg (real navigation)
backend/app.py             GET/POST /api/agents, GET/POST /api/operators
backend/eval_engine.py     check_entities_valid (now calls backend/eligibility.py), check_state_transition,
                           extract_task/extract_entities/extract_state_diff/build_mission_record,
                           compute_metrics
backend/run_evals.py       --stats
backend/tests.py           The pre-execution gate's own tests — explicit ineligible robot/agent/
                            operator rejected outright, AUTO skips ineligible candidates, and a
                            critical battery deliberately NOT gated (recharge detour still works)
evals/promptfooconfig.groq.yaml   the same checks, graded independently by an LLM
                                    (entities_check / completion_check)
logs/eval_examples/
  task_109_state_pick_and_deliver.json           real run — state_transition has a real diff to grade
  task_110_entities_invalid_firmware.json        real run — robot firmware fails entities_valid
  task_111_mixed_mission_clean_pass.json         real run — agent + robot + fully-certified operator, all pass
  task_112_mixed_mission_uncertified_operator.json   real run — robot inspection succeeds, but the
                                                       operator lacked the required certification
```

See `README.md`'s "Eval Engine" section and `evals/README.md` for the full
detail on each check and suite.
