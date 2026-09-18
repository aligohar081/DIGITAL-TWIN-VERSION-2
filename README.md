# Warehouse Digital Twin — Autonomous Multi-Robot Control Centre

A complete, runnable warehouse digital twin: a grid warehouse, autonomous mobile
robots, a task planner, A\* navigation with live collision avoidance, a battery
model, structured event logging, a mock CI validation pipeline, and a real-time
web dashboard.

You give high-level instructions. The robots work out how to carry them out.

```
USER → TASK MANAGER → TASK PLANNER → ROBOT CONTROLLER → ROBOT ACTS
     → WAREHOUSE CHANGES → DIGITAL TWIN UPDATES → EVENTS → LOGS → DASHBOARD
```

Runs on plain Ubuntu with CPU only. No GPU, CUDA, ROS 2, Gazebo or Isaac Sim
required — but the architecture leaves clean seams for all of them.

---

## Overview

Create a task such as *"pick Box-A from Shelf-A and deliver it to the Loading
Zone"* and the system:

1. validates the request against the live world state,
2. picks a robot (or you name one),
3. expands the request into low-level actions,
4. checks the robot has enough charge, inserting a recharge detour if not,
5. computes an A\* route, replanning whenever another robot gets in the way,
6. drives the robot, picks the box up, carries it, sets it down,
7. updates the digital twin and emits an event and a log line at every step.

Everything is visible on the dashboard while it happens.

### Feature checklist

| Area | What you get |
|---|---|
| Environment | 20×15 typed grid, walls, racking, pick faces, charging, loading, unloading, packing, parking, restricted zone |
| Robots | Two by default, add more at runtime; ten-state status machine; per-robot statistics |
| Tasks | Fifteen task types across robots, AI agents and human operators; four priority levels; thirteen-state lifecycle; validation; AUTO robot/agent/operator selection; per-robot task-type restriction |
| Navigation | A\* with dynamic obstacles, cell reservation, waiting, replanning, deadlock sidestep |
| Battery | Distance-based drain, handling costs, low/critical events, automatic recharge, battery-aware planning |
| Logging | Five levels, thirteen categories, structured records, disk persistence, search/filter/export |
| Mock CI | Twelve validation checks against the live twin, per-check timings, CI logs |
| Eval Engine | Eight checks graded against a finished task's own JSON log — PASS/WARN/FAIL with plain-language reasons, CLI + HTTP |
| Realtime | Server-Sent Events; no polling, no page refreshes |
| Persistence | Save / load / reset the whole twin as JSON |
| Tests | 253 pytest tests driving the simulation deterministically |

---

## Architecture

```
                         USER
                          │
                          ▼
                  WEB DASHBOARD  (frontend/)
                          │  REST + SSE
             ┌────────────┴────────────┐
             ▼                         ▼
        TASK MANAGER              ENVIRONMENT
      task_manager.py             warehouse.py
             │                         │
             ▼                         ▼
        TASK PLANNER             DIGITAL TWIN
      task_planner.py           digital_twin.py
             │                         │
             ▼                         │
       ROBOT CONTROLLER                │
        simulator.py                   │
        ┌────┴────┐                    │
        ▼         ▼                    │
     ROBO-01   ROBO-02   robot.py      │
        │         │                    │
        └────┬────┘                    │
             ▼                         │
      NAVIGATION ENGINE                │
       navigation.py                   │
             │                         │
             ▼                         │
     COLLISION AVOIDANCE               │
      (simulator.py)                   │
             └────────────┬────────────┘
                          ▼
                    EVENT SYSTEM
                  event_system.py
                          │
                 ┌────────┴────────┐
                 ▼                 ▼
              LOGGER            MOCK CI
             logger.py        ci_engine.py
                 │
                 ▼
             DASHBOARD
```

### Modules

| File | Responsibility |
|---|---|
| `backend/models.py` | Every enum, the tuning `CONFIG`, `Action`, id factory |
| `backend/warehouse.py` | The grid, cell types, named zones and their aliases, reachability helpers |
| `backend/navigation.py` | A\* pathfinding, dynamic obstacle support, zone-cell selection, distance queries |
| `backend/robot.py` | Robot physical state, motion accumulator, battery arithmetic, statistics |
| `backend/box.py` | Box entity and its seven-state lifecycle |
| `backend/task_planner.py` | High-level task → ordered action list; target resolution; battery estimation |
| `backend/task_manager.py` | Task model, validation (incl. the pre-execution authorization gate), priority queue, AUTO assignment, lifecycle transitions |
| `backend/eligibility.py` | Shared robot/agent/operator eligibility rule — used by the pre-execution gate and by the eval engine's `entities_valid` grade |
| `backend/simulator.py` | The tick: dispatch, per-robot control, traffic resolution, energy, streaming |
| `backend/digital_twin.py` | Single source of truth; snapshots; statistics; save/load; robot commands |
| `backend/event_system.py` | Semantic event bus plus the SSE fan-out broadcaster |
| `backend/logger.py` | Structured logging, disk persistence, query/filter/export |
| `backend/ci_engine.py` | Twelve consistency checks against the live twin |
| `backend/app.py` | Flask REST API, SSE endpoint, static dashboard |
| `backend/tests.py` | The pytest suite |
| `frontend/index.html` | Dashboard structure |
| `frontend/style.css` | Control-room styling |
| `frontend/app.js` | SSE consumer, canvas floor-plan renderer, all controls |

**The digital twin is the only source of truth.** The frontend never decides
where a robot is, what a box's status is, or whether a task advanced. It renders
snapshots and posts commands. Robot motion originates in `Simulator.tick`, never
in JavaScript.

---

## Installation

Ubuntu 22.04 / 24.04, Python 3.11 or newer, 8 GB RAM, integrated graphics.

```bash
sudo apt update
sudo apt install -y python3 python3-venv python3-pip

cd warehouse-digital-twin
python3 -m venv .venv
source .venv/bin/activate
pip install --upgrade pip
pip install -r requirements.txt
```

Dependencies are just Flask, pytest and PyYAML (for optional policy-as-code — see [Policy-as-code](#policy-as-code)).

## Running

```bash
./run.sh
```

`run.sh` creates the virtual environment if needed, installs dependencies, and
starts the server. Then open <http://127.0.0.1:5000>.

Manual start:

```bash
source .venv/bin/activate
python -m backend.app
```

Change host or port:

```bash
WAREHOUSE_HOST=0.0.0.0 WAREHOUSE_PORT=8080 python -m backend.app
```

On first start the warehouse is seeded with Robo-01, Robo-02, Box-A…Box-E and
two demo tasks, and the simulation is already running — robots start working the
moment you open the page.

---

## The warehouse floor

```
   01234567890123456789
 0 ████████████████████
 1 █··················█
 2 █··················█
 3 █XX▓▓▓▓··▓▓▓▓··▓▓▓·█   ▓ racking (Shelf-A, Shelf-B, Shelf-C)
 4 █XX▓▓▓▓··▓▓▓▓··▓▓▓·█   X restricted area
 5 █XXBsBs··BsBs··Bss·█   s pick face (storage)   B box
 6 █··················█
 7 █··················█
 8 █ppR············R··█   p parking   R robot
 9 █pp··██······██····█   █ internal partition
10 █··················█
11 █······PPPP·····LLL█   P packing area   L loading zone
12 █CCC···PPPP·UU··LLL█   C charging station   U unloading zone
13 █CCC···PPPP·UU··LLL█
14 ████████████████████
```

Robots drive through `EMPTY`, `STORAGE`, `CHARGING`, `LOADING`, `UNLOADING`,
`PACKING` and `PARKING` cells. `WALL`, `SHELF` and `RESTRICTED` are solid. Boxes
sit on the pick faces in front of the racking so a robot can reach them.

Named locations you can use as a source or destination:
`shelf_a`, `shelf_b`, `shelf_c` (also addressable as `storage_a/b/c`),
`packing_area`, `loading_zone`, `unloading_zone`, `charging_station`,
`parking_area` — or raw coordinates like `"12,7"`.

---

## Task creation

Use the **Create task** panel in the left rail: choose a type, a robot (or
`AUTO`), a box, a source, a destination and a priority, then press **Assign
task**. The form shows only the fields the chosen type needs.

The same thing over HTTP:

```bash
curl -X POST http://127.0.0.1:5000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"type":"PICK_AND_DELIVER","robot_id":"Robo-01","box_id":"Box-A",
       "source":"shelf_a","destination":"loading_zone","priority":"HIGH"}'
```

Robots and boxes can be referenced by id (`robot_01`, `box_001`) or by name
(`Robo-01`, `Box-A`).

### Supported task types

| Type | Meaning | Needs |
|---|---|---|
| `PICK_AND_DELIVER` | Fetch a box and drop it at a destination | box, destination |
| `MOVE_ROBOT` | Drive to a zone or coordinate | destination |
| `PICK_BOX` | Fetch a box and hold it | box |
| `DELIVER_BOX` | Drop a held box; fetches it first if not held | box, destination |
| `MOVE_BOX` | Relocate a box, robot chosen automatically | box, destination |
| `CHARGE_ROBOT` | Drive to the charger and charge to 100% | robot |
| `STOP_ROBOT` | Halt safely, immediately | robot |
| `RESUME_ROBOT` | Continue the previous task | robot |
| `AGENT_INSPECTION` | An AI agent reviews current conditions and produces a recommendation — no physical movement, resolves immediately | agent (or `AUTO`) |
| `HUMAN_INSPECTION` | A human operator inspects/approves something — no physical movement, resolves immediately | operator (or `AUTO`), `required_certification` (defaults per type — see `models.CERTIFICATION_REQUIREMENTS`) |
| `MIXED_MAINTENANCE_MISSION` | All three actor classes in one task, modeled on the reference document in `TRUST_LAYER.md`: an agent recommends dispatching a robot, the robot really navigates to the target zone, then an operator signs off on the outcome | robot, agent, operator (all `AUTO`-assignable), destination |
| `AGENT_REPLAN` | An AI agent reviews every currently-pending task and every robot's status/battery/workload, and recommends which robot should take each one — the same scoring `AUTO` assignment uses, just reported instead of acted on. No physical movement, no actual reassignment, resolves immediately | agent (or `AUTO`) |
| `AGENT_AUDIT` | An AI agent reviews the most recent WARNING-and-above log records plus the last Mock CI result and reports what it found. No physical movement, resolves immediately | agent (or `AUTO`) |
| `OPERATOR_APPROVAL` | A human operator authorizes something flagged as needing sign-off — e.g. clearing a robot for continued operation. Optionally names a `robot_id` the approval concerns; that robot's own eligibility is deliberately **not** re-checked (the point is authorizing a currently-ineligible robot). No physical movement, resolves immediately | operator (or `AUTO`), `required_certification` (defaults to `safety_inspection`) |
| `OPERATOR_MAINTENANCE_SIGNOFF` | A human operator signs off maintenance on a robot (optionally named via `robot_id`), reporting whether its firmware is on the approved baseline — and resets its predictive-maintenance wear counters (see [Predictive maintenance](#predictive-maintenance)). No physical movement, resolves immediately | operator (or `AUTO`), `required_certification` (defaults to `electrical_safety`) |
| `BATCH_DELIVER` | Deliver several boxes to the same destination as one tracked job — see [Batch delivery](#batch-delivery) | `box_ids` (2+), destination |

Every operator-instant task type above (`OPERATOR_APPROVAL`,
`OPERATOR_MAINTENANCE_SIGNOFF`, `MIXED_MAINTENANCE_MISSION`) can also opt
into a second sign-off — see [Two-person sign-off](#two-person-sign-off).

The `AGENT_*`/`OPERATOR_*` types are both **gated and graded** — see
[Pre-execution authorization gate](#pre-execution-authorization-gate)
below and `entities_valid` under [Eval Engine](#eval-engine). An
explicitly-named robot/agent/operator that's already in a bad status,
running unapproved firmware/model, or (for a human operator) missing the
certification the task requires is rejected outright at creation
(`422`), before any actor ever touches the work; `AUTO` selection simply
skips ineligible candidates the same way it already skips busy ones. The
eval engine's `entities_valid` check still re-grades the same conditions
after a task finishes — it's the safety net for an actor that became
ineligible mid-task, or a log from before the gate existed, not the only
line of defense anymore.

### Pre-execution authorization gate

`backend/eligibility.py` holds the one rule both sides share:

```
robot_eligibility(status, firmware_version, battery, task_type, allowed_task_types)  # battery=None from the gate — see below
agent_eligibility(status, model_version)
operator_eligibility(status, certifications, required_certification)
```

`task_type`/`allowed_task_types` cover a robot's own configured work restriction — see
[Per-robot task restriction](#per-robot-task-restriction) below.

`TaskManager.validate()` (`backend/task_manager.py`) calls these for an
explicitly-requested robot/agent/operator and fails the task immediately
if any of them return a reason; `select_robot()` and the `AUTO`
agent/operator branches in `validate()` call the same functions to skip
ineligible candidates when picking one automatically. `eval_engine.
check_entities_valid` imports the exact same functions for its
after-the-fact grade, so the gate and the grade can never quietly
disagree about what "eligible" means.

**One deliberate asymmetry:** a critically-low battery is *not* part of
the hard gate. The planner already has a real recovery path for it — it
prepends a recharge detour (see [Battery](#robot-behaviour)) — so
refusing the task outright would break that feature. The eval engine
still grades battery after the fact: a robot still critical once the
task actually starts (the detour didn't happen, or wasn't enough) is
worth flagging even though it wasn't worth refusing up front.

### Priorities

`LOW` → `NORMAL` → `HIGH` → `CRITICAL`. The dispatcher drains the queue in
priority order, and priority also decides right of way when two robots want the
same cell.

### Validation

Before a task is queued it must pass every check: the robot exists and is not in
an error state, the box exists and is not already reserved by another task, the
source and destination resolve to real reachable cells, and an A\* route exists.
A failing task goes straight to `FAILED` with a plain-language reason and an
`ERROR` log line.

### AUTO assignment

Set the robot to `AUTO` and the manager scores every available robot on route
distance, current status, battery level and queued workload, then logs the
decision with the score of every candidate:

```
AUTO assignment for task_003 → Robo-02 (Robo-01=score 21.25, Robo-02=score 7.5)
```

### Per-robot task restriction

By default every robot is **unrestricted** — it may be assigned any task
type, exactly as before this existed. Configure a robot with its own
allowlist and it may only run the task types you name; everything else
is rejected the same way bad firmware or a `STOPPED` status is:

```bash
# Robo-01 may only ever be used to move (never to pick/deliver/charge)
curl -X POST http://127.0.0.1:5000/api/robots/Robo-01/capabilities \
  -H 'Content-Type: application/json' -d '{"allowed_task_types":["MOVE_ROBOT"]}'

# Naming it for a task outside its list -> 422, reason spelled out
curl -X POST http://127.0.0.1:5000/api/tasks \
  -H 'Content-Type: application/json' \
  -d '{"type":"PICK_AND_DELIVER","robot_id":"Robo-01","box_id":"Box-A","destination":"loading_zone"}'
# -> 422: "Robo-01 is not configured to run PICK_AND_DELIVER tasks (allowed: ['MOVE_ROBOT'])"

# AUTO simply skips it for that task type and picks another eligible robot instead

# Clear the restriction (back to unrestricted) with an empty list
curl -X POST http://127.0.0.1:5000/api/robots/Robo-01/capabilities \
  -H 'Content-Type: application/json' -d '{"allowed_task_types":[]}'
```

`Robot.allowed_task_types` (`backend/robot.py`) is the field; `robot_eligibility()`
(`backend/eligibility.py`) is where it's actually enforced — the exact
same function that already checks status/firmware, called from the same
two places: `TaskManager.validate()`'s pre-execution gate (an
explicitly-named robot outside its own configured list is rejected
outright, `422`) and `select_robot()`'s `AUTO` scoring (skips it the same
way it already skips busy/ineligible robots). `eval_engine.
check_entities_valid` grades the same condition after the fact, using
the task type and the robot's `allowed_task_types` captured in the
task's own "before" state snapshot — so a robot that somehow ran a task
outside its configured list is still flagged, even from an older log.
On the dashboard, each robot card has a **Capabilities** checklist —
toggle a box and the new list is saved immediately, no separate save
step. Unchecking everything (or checking everything) both mean
"unrestricted", the same as an empty list over the API.

---

## Robot behaviour

You never drive a robot manually. Given `PICK_AND_DELIVER`, the planner produces:

```
1 NAVIGATE  Navigate to Box-A            → (3,5)
2 PICK      Pick Box-A
3 NAVIGATE  Carry Box-A to Loading-Zone  → (16,11)
4 DELIVER   Deliver Box-A at Loading-Zone
5 COMPLETE  Complete task
```

Each tick the controller executes one step of the current action.

**Statuses:** `IDLE`, `PLANNING`, `MOVING`, `WAITING`, `PICKING`, `CARRYING`,
`DELIVERING`, `CHARGING`, `STOPPED`, `ERROR`.

**Traffic.** Before entering a cell a robot checks whether another robot holds
it or has reserved it as its own next cell. If so it stops, logs
`COLLISION_AVOIDED` and `ROBOT_WAITING`, and marks its task `BLOCKED`. After
three waiting ticks it replans with the blocker's cells treated as obstacles
(`PATH_RECALCULATED`). Head-on conflicts replan after two. If a robot is still
stuck after twenty ticks it sidesteps to a free neighbour
(`DEADLOCK_RESOLVED`). Actual overlaps are counted as collisions — under normal
operation the counter stays at zero.

**Battery.** One percent per six cells travelled, one percent per pick and per
delivery, plus two percent per tick while charging. `BATTERY_LOW` fires at 20%,
`BATTERY_CRITICAL` at 8%, and a flat battery puts the robot into `ERROR` and
fails its task. An idle robot below 20% takes itself to the charger. If the
planner estimates a task needs more charge than the robot has, it prepends a
recharge detour and says so in the log.

All of these numbers live in one place — `CONFIG` in `backend/models.py`.

---

## Digital twin

`DigitalTwin` owns the warehouse, every robot, every box, the task manager, the
event system, the logger and the statistics. All mutation happens under one
re-entrant lock, so the simulation thread and HTTP requests never observe a
half-updated world.

`snapshot()` produces the frame the dashboard renders: environment state,
robots, boxes, tasks, statistics, per-robot statistics, the last CI result and
the form option lists. `snapshot(include_layout=True)` adds the floor plan and
tuning config, which the browser fetches once at boot.

State is saved to and loaded from `data/warehouse_state.json` — robots, boxes,
tasks, statistics, simulation state and the id counters, so ids never collide
after a reload.

---

## Logging

Every log record is structured:

```json
{
  "seq": 412,
  "timestamp": "2026-08-13T14:30:00",
  "time": "14:30:00",
  "level": "INFO",
  "category": "ROBOT",
  "event": "BOX_PICKED",
  "robot_id": "robot_01",
  "task_id": "task_001",
  "box_id": "box_001",
  "position": {"x": 8, "y": 4},
  "message": "Robo-01 picked Box-A",
  "data": {}
}
```

**Levels:** `DEBUG`, `INFO`, `WARNING`, `ERROR`, `CRITICAL`.
**Categories:** `SYSTEM`, `ENVIRONMENT`, `ROBOT`, `BOX`, `TASK`, `PLANNER`,
`NAVIGATION`, `COLLISION`, `BATTERY`, `WAREHOUSE`, `USER`, `CI`, `DIGITAL_TWIN`.

Records go to three places: an in-memory ring buffer for the dashboard,
`logs/warehouse.log` as readable lines, and `logs/events.json` as JSON Lines.
On restart the tail of the previous run is re-loaded, so history survives.

The **Live system logs** panel streams records as they happen with auto-scroll,
free-text search and filters by level, category, robot and task. **Clear** wipes
both memory and disk; **.txt** and **.json** download the full history.

A real delivery, end to end:

```
14:30:01 [USER]        task_001 created: PICK_AND_DELIVER — box_001 → loading_zone
14:30:01 [TASK]        task_001 validation started
14:30:01 [TASK]        task_001 validated successfully
14:30:01 [PLANNER]     Planning PICK_AND_DELIVER for Robo-01
14:30:01 [PLANNER]     Source Box-A found at (3,5)
14:30:01 [PLANNER]     Destination Loading-Zone found at (16,11)
14:30:01 [BOX]         Box-A reserved for task_001 (STORED → RESERVED)
14:30:01 [TASK]        task_001 assigned to Robo-01
14:30:01 [NAVIGATION]  A* path for Robo-01 → Box-A: 3 cells
14:30:02 [ROBOT]       Robo-01 moved from (3,7) to (3,6)
14:30:03 [ROBOT]       Robo-01 reached Box-A
14:30:03 [ROBOT]       Robo-01 started picking Box-A (RESERVED → PICKING)
14:30:04 [BOX]         Robo-01 picked Box-A (PICKING → CARRIED)
14:30:04 [NAVIGATION]  A* path for Robo-01 → Loading-Zone: 19 cells
14:30:09 [COLLISION]   Potential collision at (11,10): Robo-02 is in the way of Robo-01
14:30:09 [ROBOT]       Robo-01 waiting for Robo-02
14:30:10 [NAVIGATION]  Alternative path for Robo-01 → Loading-Zone: 8 cells
14:30:14 [ROBOT]       Robo-01 reached Loading-Zone
14:30:15 [BOX]         Box-A delivered to Loading-Zone (DELIVERING → DELIVERED)
14:30:15 [TASK]        task_001 COMPLETED — Task completed
```

### Events

`ROBOT_CREATED`, `ROBOT_MOVED`, `ROBOT_STOPPED`, `ROBOT_RESUMED`, `ROBOT_RESET`,
`ROBOT_CHARGING`, `ROBOT_CHARGED`, `ROBOT_WAITING`, `ROBOT_ERROR`,
`ROBOT_CAPABILITIES_UPDATED`,
`AGENT_CREATED`, `AGENT_RECOMMENDATION`, `AGENT_ERROR`,
`OPERATOR_CREATED`, `OPERATOR_APPROVED`, `OPERATOR_REJECTED`,
`BOX_CREATED`, `BOX_RESERVED`, `BOX_PICKED`, `BOX_MOVED`, `BOX_DELIVERED`,
`TASK_CREATED`, `TASK_VALIDATED`, `TASK_ASSIGNED`, `TASK_PLANNED`,
`TASK_STARTED`, `TASK_PROGRESS`, `TASK_COMPLETED`, `TASK_FAILED`,
`TASK_CANCELLED`, `TASK_PAUSED`, `TASK_RESUMED`, `PATH_CREATED`,
`PATH_RECALCULATED`, `PATH_NOT_FOUND`, `COLLISION_AVOIDED`,
`COLLISION_DETECTED`, `DEADLOCK_RESOLVED`, `BATTERY_LOW`, `BATTERY_CRITICAL`,
`EMERGENCY_STOP`, `SIMULATION_STARTED/PAUSED/STOPPED/RESET`, `STATE_SAVED`,
`STATE_LOADED`, `TWIN_SYNCHRONIZED`, `CI_STARTED`, `CI_COMPLETED`,
`WAREHOUSE_INITIALIZED`.

---

## Mock CI

Press **Run CI** to assert that the simulated world is internally consistent
right now. Twelve checks, each timed, none of them mutating state:

```
✓ Environment validation              1.42 ms
✓ Robot state validation              0.06 ms
✓ Robot position validation           0.04 ms
✓ Navigation validation               0.05 ms
✓ Collision validation                0.03 ms
✓ Battery validation                  0.05 ms
✓ Box state validation                0.09 ms
✓ Task validation                     0.07 ms
✓ Task planner validation             0.06 ms
✓ Digital twin synchronisation        0.05 ms
✓ Environment boundary validation     0.04 ms
✓ Logging validation                  0.31 ms

#101 PASSED
```

A failure names the problem in place:

```
✗ Collision validation
  Robo-01 and Robo-02 both occupy (8,5)
```

The checks cover: perimeter integrity and full floor connectivity; unique robot
names, valid batteries and live references; robots standing on drivable cells;
paths that are contiguous and drivable; no overlapping robots; charging only at
the charging station; carried boxes matching their carrier's position; tasks
pointing at entities that exist and terminal tasks carrying timestamps; active
tasks having well-formed plans with in-bounds targets; robot ↔ task ↔ box
agreement; every entity inside the grid; and log records carrying the required
fields with both files on disk.

Every run writes `[CI]` log lines and raises `CI_STARTED` / `CI_COMPLETED`.

---

## Eval Engine

> `entities_valid`, `state_transition`, the toy firmware baseline, and the
> Mission Authorization Record below are a scoped-down implementation of
> ideas from an external "Physical AI Trust Layer" reference document —
> see `TRUST_LAYER.md` for the honest breakdown of what applies to this
> project, what doesn't, and what's still planned.

Mock CI checks whether the *live* world is internally consistent right now.
The **eval engine** answers a different question: given the JSON log a task
already produced (`logs/tasks/<task_id>.json` — the same file `.txt`/`.json`
export and `GET /api/tasks/{id}` use), *did that task actually succeed, and
if not, why?*

It never re-runs the simulation. It reads the finished record the same way
a person would scroll the **Live system logs** panel, and grades it against
ten checks:

| Check | Flags |
|---|---|
| `terminal_state` | No `TASK_COMPLETED` — the task `FAILED`, was `CANCELLED`, or never reached a terminal state at all |
| `path_exists` | `PATH_NOT_FOUND` — no route to the target |
| `battery` | Battery hit 0% (`FAIL`) or dropped to the low/critical thresholds and recovered (`WARN`) |
| `collision_safety` | `COLLISION_DETECTED` — two robots actually occupied the same cell |
| `stuck_or_deadlock` | The robot waited on blocked traffic past `DEADLOCK_WAIT_TICKS` with no `DEADLOCK_RESOLVED` and no recovery — it's stuck |
| `controller_errors` | The robot controller raised an exception |
| `external_interruption` | An emergency stop or an operator-triggered robot reset cut the task short |
| `sequence_integrity` | The log itself looks malformed — seq numbers out of order, or a box "delivered" before it was ever "picked" |
| `entities_valid` | Any actor assigned to this task was already ineligible *before* it even started — robot: `ERROR`/`STOPPED` status, critical battery, unapproved firmware, or a task type outside its own `allowed_task_types`; AI agent: `ERROR` status or an unapproved model version; human operator (and, for a `dual_signoff` task, its second signer): off duty, or missing the certification the task required. Also flags (as a `WARN`, not a `FAIL`) any `TASK_AUTHORIZATION_CHANGED` in the log — the robot became ineligible *after* the task had already started (see [Mid-task authorization changes](#mid-task-authorization-changes)) |
| `state_transition` | The event trail says one thing, but the actual before/after world state (robot/box position+status, zone occupancy) says another — e.g. `TASK_COMPLETED` fired but the box never actually left its source shelf |

Each check returns `PASS`, `WARN`, or `FAIL` with a plain-language message
and the `seq` numbers of the log lines that triggered it. The task's overall
verdict is the worst of the ten (`FAIL` > `WARN` > `PASS`). A clean
`PASS` means the task completed and nothing else was worth flagging; `WARN`
means it completed but something's worth a look (e.g. battery ran low but
recovered); `FAIL` means the task did not complete cleanly, and the reason
is spelled out.

`entities_valid` and `state_transition` are the odd ones out: neither reads
the event trail at all. `TaskManager` (`backend/task_manager.py`) takes a
snapshot of the physical world — the assigned robot's and box's
position/status/firmware, and whether the task's source/destination zones
hold this box — once right when a task starts and once again at its
terminal event, and attaches both snapshots directly to those two log
records (`data.state`). `entities_valid` grades the "before" half: was the
robot actually eligible to be trusted with this task in the first place — a
toy stand-in for a real Robot Assurance Passport / SBOM check (see
`models.APPROVED_FIRMWARE_VERSIONS`), rather than proof it's running real
vendor-verified firmware. `state_transition` grades the "before vs after"
diff: for a completed task, the box's own zone should now match the
destination and its id should appear in that zone's `boxes_present`; for a
failed/cancelled task, nothing should have moved at all. A task can
mechanically finish cleanly and still fail overall if the robot itself
wasn't trustworthy to begin with — see `task_110_entities_invalid_firmware`
below for exactly that case. Logs written before this existed simply have
no `state` in `data` and both checks skip cleanly (`PASS`, "nothing to
check") rather than flagging a false problem.

Both checks' evidence — the task as created, the entities involved, and
the state diff — is also available as one consolidated object via
`backend.eval_engine.build_mission_record(events, report)`, a lightweight
stand-in for the "Mission Authorization Record" idea: one artifact naming
exactly who/what was involved in a task and whether it was trustworthy.
`evals/generate_tests_groq.py` builds the Groq suite's prompt from exactly
these same three pieces.

### Running it

```bash
# Grade every task this project has actually run (logs/tasks/*.json)
python -m backend.run_evals

# Grade one task
python -m backend.run_evals logs/tasks/task_006.json

# Grade the bundled examples — one fixture per failure case, generated
# once and checked in under logs/eval_examples/, used by the test suite
python -m backend.run_evals --demo

# Save the full machine-readable report alongside the console summary
python -m backend.run_evals --out logs/evals/report.json

# CI-friendly: exit 1 if fewer than N tasks come back as a clean PASS
python -m backend.run_evals --fail-under 5

# Aggregate metrics: mission success rate, assurance (state-diff)
# coverage, and which check fails most often
python -m backend.run_evals --stats
```

Console output looks like:

```
✗ task_006: FAIL
    ✓ sequence_integrity     Event ordering is consistent.
    ✗ terminal_state         Task failed: task_006 FAILED — Box-E is already reserved by task_005
    ✓ path_exists            A route to the target was always available.
    ✓ battery                Battery stayed at a healthy level throughout.
    ✓ collision_safety       No two robots ever overlapped.
    ✓ stuck_or_deadlock      No traffic contention observed.
    ✓ controller_errors      No controller exceptions were raised.
    ✓ external_interruption  No external interruption during the task.
    ✓ entities_valid         No state snapshot recorded for this log (older log, or the task never started) — nothing to check.
    ✓ state_transition       No state snapshot recorded for this log (older log, or the task never reached a terminal event) — nothing to check.

8 task(s) evaluated — 7 passed, 0 warned, 1 failed.
```

`--stats` adds a summary like:

```
Metrics
  mission success rate   27.3%  (3/11 PASS or WARN)
  clean pass rate        18.2%  (2/11 strict PASS)
  state-diff coverage    18.2%  (logs carrying a real before/after snapshot to grade)
  failures by check:
    ✗ terminal_state         6
    ✗ entities_valid         1
    ...
```

### Example logs

`logs/eval_examples/` holds one hand-built (but schema-accurate) log per
failure case, plus a real clean pass and the project's own real
box-conflict failure, so every check has a concrete example to point at
and a regression test in `backend/test_eval_engine.py`:

| File | Demonstrates |
|---|---|
| `task_100_clean_pass.json` | A normal `PICK_AND_DELIVER` — real log, copied from `logs/tasks/task_003.json` |
| `task_101_battery_depleted.json` | Battery reaches 0% mid-transport → `ROBOT_ERROR` + `TASK_FAILED` |
| `task_102_robot_stuck.json` | A robot waits on blocked traffic 25+ times and never recovers — no `DEADLOCK_RESOLVED`, no completion |
| `task_103_path_not_found.json` | `PATH_NOT_FOUND` — the destination is unreachable |
| `task_104_collision_detected.json` | A real `COLLISION_DETECTED` slips through even though the task otherwise completes |
| `task_105_box_already_reserved.json` | Real validation failure — copied from `logs/tasks/task_006.json` |
| `task_106_controller_error.json` | The robot controller raises an exception |
| `task_107_task_cancelled.json` | An operator cancels the task mid-flight |
| `task_108_battery_low_recovered.json` | Battery dips under 20% but the task still completes — a `WARN`, not a `FAIL` |
| `task_109_state_pick_and_deliver.json` | Real simulator run (not hand-built) — Robo-01 starts in `parking_area`, Box-D starts on `shelf_a`, task destination is `shelf_b`. Carries real `TaskManager.snapshot_state` data, so `state_transition` has an actual diff to grade instead of skipping |
| `task_110_entities_invalid_firmware.json` | Real simulator run — same as `task_109`, but Robo-01's `firmware_version` was set to an unapproved value before the task ran. The delivery itself completes cleanly (`state_transition` passes), but `entities_valid` still fails the whole task: the robot should never have been trusted with the work, regardless of the outcome |
| `task_111_mixed_mission_clean_pass.json` | Real simulator run of `MIXED_MAINTENANCE_MISSION` — an AI agent recommends a robot inspection, the robot really navigates to the target zone, and a fully-certified human operator signs off. All three actors, one task, a clean `PASS` |
| `task_112_mixed_mission_uncertified_operator.json` | Same mission, but the operator only holds `safety_inspection`, not the `electrical_safety` the mission actually requires. The robot's physical inspection still succeeds — `entities_valid` is what catches the mismatch |
| `task_113_dual_signoff_clean_pass.json` | Real run — `OPERATOR_MAINTENANCE_SIGNOFF` with `dual_signoff: true`; both Sam (primary) and Kim (second signer) hold `electrical_safety` |
| `task_114_batch_deliver_clean_pass.json` | Real run — `BATCH_DELIVER` with two boxes, both delivered to the same destination under one task id |
| `task_115_mid_task_authorization_changed.json` | Real run — a robot's firmware is downgraded to an unapproved version mid-route; `TASK_AUTHORIZATION_CHANGED` fires but the task still completes — `entities_valid` grades this a `WARN`, not a `FAIL` (unlike being ineligible from the start) |
| `task_116_dual_signoff_second_signer_uncertified.json` | Hand-built from task_113 (the pre-execution gate already blocks this combination at creation, so it can't occur in a real run): the second signer is missing the required certification — `entities_valid` still fails the task over it |
| `task_117_false_success_box_never_arrived.json` | Hand-edited from task_109: the event trail is untouched — `BOX_DELIVERED`, `TASK_COMPLETED`, the same success message, so every event-trail check (`terminal_state` included) reports a clean `PASS`. Only the physical `state.state` snapshot on that same `TASK_COMPLETED` record tells the truth: Box-D never left Shelf-A. A confidently-wrong completion — `state_transition` is the one check that reads ground truth instead of the narrative, so it's the only thing that catches it |

### Promptfoo suite

`eval_engine` also runs under [promptfoo](https://www.promptfoo.dev/) —
see `evals/README.md`. The default suite grades your system's own real
logs in `logs/tasks/` using `eval_engine` itself (no LLM); a second suite
grades the curated `logs/eval_examples/` fixtures the same way; a third
sends real logs to a Groq-hosted LLM as an independent judge and checks
it agrees:

```bash
python3 evals/generate_tests.py            # pick up new logs/tasks/*.json
npx promptfoo eval -c evals/promptfooconfig.yaml
npx promptfoo view

# LLM-as-judge via Groq (needs GROQ_API_KEY — see evals/README.md)
python3 evals/generate_tests_groq.py
npx promptfoo eval -c evals/promptfooconfig.groq.yaml
```

Both suites above grade a *snapshot* — regenerate the test list before
every eval, or promptfoo keeps grading whatever `logs/tasks/` looked
like last time. `evals/regrade.sh` does both steps together and exits
non-zero if anything graded FAIL:

```bash
./evals/regrade.sh            # regenerate + grade the live suite
./evals/regrade.sh --groq      # ...and the Groq suite too
```

### Programmatic use

```python
from backend.eval_engine import evaluate_file, evaluate_events

report = evaluate_file("logs/tasks/task_001.json")
report.verdict     # Verdict.PASS / Verdict.WARN / Verdict.FAIL
report.ok          # True only for a clean PASS
report.passed      # True unless it's a FAIL (WARN still counts as passed)
report.reasons     # ["[battery] Battery ran low ...", ...]
report.to_dict()   # the full JSON-serialisable report
```

### Over HTTP

| Method | Path | Returns |
|---|---|---|
| GET | `/api/tasks/{id}/eval` | Grades one task's full persisted log (the task must still be known to the running twin) |
| POST | `/api/evals/run` | Grades every `logs/tasks/*.json` file on disk and returns pass/warn/fail totals plus the full per-task reports |

---

## Fleet management & governance extensions

A second layer on top of the core simulation and the trust-layer gate
(see `TRUST_LAYER.md`) — still toy-scale, but each of these is real,
working code, not a mock.

### Robot classes

`backend/models.py::ROBOT_CLASS_PRESETS` — six preset bundles of speed
and `allowed_task_types` (see
[Per-robot task restriction](#per-robot-task-restriction)) applied when
a robot is created with `robot_class`:

| Class | Speed | Restricted to |
|---|---|---|
| `AMR` | 2.0 | Unrestricted (general purpose, the default) |
| `FORKLIFT` | 1.2 | pick/deliver/move-box/move-robot/charge |
| `SCOUT` | 3.0 | move-robot, mixed maintenance mission, charge |
| `HEAVY_HAULER` | 0.8 | Same as Forklift, plus `BATCH_DELIVER` |
| `DRONE` | 4.0 | Same as Scout, but faster |
| `PICKER` | 2.5 | pick/deliver/move-box only (no free-roam `MOVE_ROBOT`) |

It's only a convenience default — an explicit `speed` or
`allowed_task_types` still wins, and either can be changed afterwards
through the normal endpoints exactly like a hand-configured robot.

```bash
curl -X POST http://127.0.0.1:5000/api/robots \
  -H 'Content-Type: application/json' -d '{"name":"Fork-1","robot_class":"FORKLIFT"}'
```

**If you add a class of your own**, always include `CHARGE_ROBOT` in its
`allowed_task_types` (or leave it `None`/unrestricted) — a robot's own
automatic recharge (`Simulator._auto_charge`) is dispatched as a normal,
gated `CHARGE_ROBOT` task, so a class that can't run one can never
charge itself and will eventually strand in `ERROR`.

### Operator roles

`backend/models.py::OPERATOR_ROLE_PRESETS` — the exact same idea as
robot classes above, for the human actor class: a preset bundle of
`certifications` and a shift window, applied when an operator is created
with `role`:

| Role | Certifications | Shift |
|---|---|---|
| `SAFETY_INSPECTOR` | `safety_inspection` | 9-17 |
| `MAINTENANCE_TECH` | `electrical_safety`, `equipment_maintenance` | 9-17 |
| `SENIOR_OPERATOR` | Every known certification | Always available (no fixed shift) — the reliable `AUTO` fallback and a natural second signer for `dual_signoff` |
| `NIGHT_SHIFT` | `safety_inspection` | 22-6 (wraps past midnight) |
| `REMOTE_ONCALL` | `safety_inspection`, `electrical_safety` | Always available |
| `TRAINEE` | None yet | 9-17 — a clean, intentional "not yet certified" demo case |

Same convenience-default philosophy as robot classes: an explicit
`certifications` list or `shift_start_hour`/`shift_end_hour` still wins
over the role's preset, and any of them can be changed afterwards
through the normal endpoints exactly like a hand-configured operator. No
role (the default, and every operator's behaviour before roles existed)
means no shift window and whatever certifications you explicitly gave
it.

```bash
curl -X POST http://127.0.0.1:5000/api/operators \
  -H 'Content-Type: application/json' -d '{"name":"Priya","role":"MAINTENANCE_TECH"}'
```

`backend/models.py::KNOWN_CERTIFICATIONS` lists every certification this
project's toy trust-layer actually knows the name of (the six named
above) — purely for the dashboard's certification suggestions and the
role presets; `Operator.certifications` stays a plain list of strings,
never validated against it, so a hand-typed certification still works.

Deliberately **not** touched: the demo seed (`Sam`/`Lee`, still exactly
as they were) — several existing behaviours (like `dual_signoff` having
no eligible second signer by default) depend on exactly those two
existing, so new roles are opt-in via the dashboard or the API, not
baked into the demo state.

### Predictive maintenance

`backend/maintenance.py` — a toy wear model: distance travelled and
charge cycles completed since a robot's last `OPERATOR_MAINTENANCE_SIGNOFF`
(`Robot.perform_maintenance` resets both counters). Once either crosses
its threshold (`CONFIG["MAINTENANCE_DISTANCE_THRESHOLD"]` /
`MAINTENANCE_CHARGE_CYCLES_THRESHOLD`), the robot is flagged once — a
`MAINTENANCE_ALERT` `WARNING` event, not a hard stop — nudging you to
schedule a sign-off. `GET /api/maintenance/alerts` lists everything
currently over threshold.

### Operator shifts

Give an operator a real wall-clock shift window
(`Operator.shift_start_hour`/`shift_end_hour`, 0-23, wraps past midnight)
and the simulator automatically flips them `AVAILABLE`↔`OFF_DUTY` outside
it (`Simulator._check_operator_shifts`, every `CONFIG["SHIFT_CHECK_
EVERY_TICKS"]` ticks) — never while they're `ON_TASK`, and never at all
for an operator with no shift configured (manual control only, the
default). `POST /api/operators/{id}/shift` configures or clears it.

### Two-person sign-off

Any operator-instant task (`OPERATOR_APPROVAL`, `OPERATOR_MAINTENANCE_
SIGNOFF`, and `MIXED_MAINTENANCE_MISSION`) can opt into requiring a
**second**, genuinely different, equally-certified operator —
`{"dual_signoff": true}` (AUTO-picks the second signer) or an explicit
`{"second_operator_id": "..."}`. Both signers are graded by the exact
same `operator_eligibility()` rule as the first; the second signer's
approval is its own logged `OPERATOR_APPROVED` event (`"co_signer":
true` in its data).

### Mid-task authorization changes

`Simulator._check_authorization_changes` re-checks every active task's
robot against the same eligibility rule the pre-execution gate uses,
every `CONFIG["AUTHORIZATION_CHECK_EVERY_TICKS"]` ticks — but here it
**flags**, it never gates: the robot may already be mid-route, so
forcibly killing the task would trade one safety problem for another.
One `TASK_AUTHORIZATION_CHANGED` `WARNING` event per task
(`Task.authorization_flagged`, visible on the task record), never
repeated.

### Collision risk

Under normal operation two robots never actually occupy the same cell —
the traffic system (`Simulator._handle_block` / `_break_deadlock`)
actively avoids it, replanning around and sidestepping other robots
before it can happen, and `CONFIG["COLLISION_RISK"]` defaults to `0.0`
(perfect avoidance). Raise it — live, from the **Policies** panel's
**Collision risk** slider, or `POST /api/policies/collision-risk`, or
durably via `policies.yaml` — and robots start *genuinely* failing to
avoid each other while carrying out their real work: each time a robot
would normally wait, replan, or sidestep around another, there's now
that percentage chance it drives straight into the conflict instead
(`Simulator._act_navigate`), simulating imperfect real-time sensing —
not a manual "make two robots crash" button.

```bash
curl -X POST http://127.0.0.1:5000/api/policies/collision-risk \
  -H 'Content-Type: application/json' -d '{"risk": 0.2}'
```

When an overlap actually results, `DigitalTwin.register_collision` gives
it a real consequence: both robots halt to `ERROR`, and whatever task
either was running fails immediately — **the task is disconnected from
the robot**, the same way a battery depletion or a controller exception
already halts a robot mid-mission. The usual `COLLISION_DETECTED` event
still fires (and is still what `eval_engine.check_collision_safety`
grades as an automatic `FAIL`); it's only flagged once per incident, not
re-flagged every tick while the wreckage sits there waiting to be
cleared. Recovery is the existing **Reset** button / `POST
/api/robots/{id}/reset`: full battery, cargo dropped, back home. Turn
the risk back down to `0` to go back to perfect avoidance.

### False success risk

The delivery-completion equivalent of collision risk, for a different
kind of fault: instead of two robots visibly failing to avoid each
other, a single delivery silently fails to actually place its payload —
and nothing in the live narrative says so. `CONFIG["FALSE_SUCCESS_RISK"]`
defaults to `0.0` (every completed delivery really landed the box).
Raise it — live, from the **Policies** panel's **False success risk**
slider, or `POST /api/policies/false-success-risk`, or durably via
`policies.yaml` — and that percentage of real deliveries slip off the
fork right at the final release and land back where the box was picked
up, while the robot, `BOX_DELIVERED`, `TASK_COMPLETED` and the task
lifecycle all still report a completely ordinary success
(`Simulator._act_deliver`) — the system is fully "sure" it did the job
right.

```bash
curl -X POST http://127.0.0.1:5000/api/policies/false-success-risk \
  -H 'Content-Type: application/json' -d '{"risk": 0.2}'
```

Nothing about this is flagged live — by design, that's the point. It's
only ever caught after the fact, by `eval_engine.check_state_transition`
comparing the task's own before/after world-state snapshot against what
`TASK_COMPLETED` claimed: every other check (`terminal_state` included)
still grades a clean `PASS`, because they only read the narrative.
Whether you run that grade via `python -m backend.run_evals`,
`GET /api/tasks/{id}/eval`, or the default promptfoo suite in `evals/`
against real `logs/tasks/*.json`, `state_transition` is the one thing
that catches it — see `task_117_false_success_box_never_arrived.json`
under [Example logs](#example-logs) for a hand-built demonstration of
exactly this outcome. Turn the risk back down to `0` to go back to every
completed delivery being real.

### Recurring tasks

`backend/scheduler.py` — a schedule remembers a task-creation payload
and an interval; the simulator fires it automatically
(`Scheduler.tick()`, called once per simulation tick, so it stays
deterministic like everything else here — no real timers). Persists
through save/load and simulation reset.

```bash
# Run an AI audit every 30 seconds
curl -X POST http://127.0.0.1:5000/api/schedules \
  -H 'Content-Type: application/json' \
  -d '{"payload":{"type":"AGENT_AUDIT","agent_id":"Ada"},"interval_seconds":30}'

curl http://127.0.0.1:5000/api/schedules
curl -X POST http://127.0.0.1:5000/api/schedules/schedule_0001/toggle
curl -X DELETE http://127.0.0.1:5000/api/schedules/schedule_0001
```

### Batch delivery

`BATCH_DELIVER` groups several boxes to the same destination under one
tracked task id — `{"type":"BATCH_DELIVER","box_ids":["Box-A","Box-B"],
"destination":"loading_zone"}`. The robot still only ever carries one
box at a time (`Robot.carrying_box` is a single id, not a list); the
planner just repeats NAVIGATE/PICK/NAVIGATE/DELIVER per box in one plan.

### Policy-as-code

An optional `policies.yaml` at the project root (see
`policies.example.yaml`) overrides the values that used to be hardcoded
in `backend/models.py` — `CONFIG` (battery thresholds, whether live LLM
narration is on, maintenance thresholds, ...), `APPROVED_FIRMWARE_
VERSIONS`, `APPROVED_AGENT_MODELS`, `CERTIFICATION_REQUIREMENTS`, and
`ROBOT_CLASS_PRESETS`. Loaded once at process start; `POST
/api/policies/reload` re-reads it live, no restart needed. `GET
/api/policies` shows the values actually in effect right now.

### Decision history search

`backend/decision_graph.py` — a toy stand-in for the reference
document's "Decision Graph" idea, without a real graph database: every
`logs/tasks/*.json` file already is the decision history, and
`eval_engine.build_mission_record` already turns one into a single
record naming who/what was involved and whether it was trustworthy.
This just filters that collection by robot / agent / operator / task
type / verdict / a battery-below threshold, all ANDed.

```bash
python -m backend.decision_graph --robot Robo-01 --battery-below 20
python -m backend.decision_graph --operator Sam --verdict FAIL
curl "http://127.0.0.1:5000/api/decisions?operator=Sam&verdict=FAIL"
```

### Optional live AI narration

`backend/llm.py` — off by default (`CONFIG["AGENT_LLM_ENABLED"]`, see
`policies.example.yaml`). When turned on **and** a Groq API key is
configured (`GROQ_API_KEY`, or reuses `evals/.env`), `AGENT_REPLAN` and
`AGENT_AUDIT` ask a real Groq-hosted model to phrase their
recommendation in place of the built-in computed wording. Every event
still carries `"llm_generated": true/false` so you always know which one
produced it. Any failure — feature off, no key, network error, timeout —
quietly falls back to the computed message; a live model is a narration
layer over real computed data here, never a hard dependency, and never
what decides whether a task passes or fails.

### Chat agent

`backend/agent_chat.py` + `backend/agent_tools.py` — genuinely different
from the narration above: a real tool-use loop, where the model itself
decides what to look up or do, using the same seven tools a person would
reach through the REST API (`create_task`, `cancel_task`, `get_task`,
`list_entities`, `get_fleet_load`, `get_statistics`, `query_decisions`).
Every action it takes goes through the exact same code path as any other
client — `create_task` calls the identical `TaskManager.create_task()`
`POST /api/tasks` does, so it's bound by the same pre-execution
eligibility gate as anyone else, and every tool call is logged
(`[agent-chat]` prefix) for the same traceability a human clicking a
button already has. Unlike the narration layer, there's no computed
fallback — a live model *is* the feature — so it always needs a
configured Groq key, regardless of `AGENT_LLM_ENABLED`.

On the dashboard: the 🤖 chat launcher (bottom-right, every page). Type
a command:

> move Box-A to loading zone using Robo-01, then charge Robo-02

or ask a question:

> why did task_042 fail? / which robot is busiest right now?

```bash
curl -X POST http://127.0.0.1:5000/api/agent/chat \
  -H 'Content-Type: application/json' \
  -d '{"message":"which robot is most overworked right now?"}'
```
Returns `{"reply", "actions", "iterations"}` — `actions` is the full list
of tool calls made along the way, so you can see exactly what it looked
up or did, not just its final answer. `history` (a list of `{"role",
"content"}` from earlier turns) can be included in the request body to
keep a multi-turn conversation coherent.

The `POST` itself is still synchronous (its response is the
authoritative result), but a long, many-step command doesn't just sit
behind one static "thinking…" — each step is also narrated live over the
existing SSE stream (`GET /api/stream`, event name `agent-chat`:
`{"phase": "thinking", "iteration"}` before each call to the model,
`{"phase": "action", "tool", "args", "result"}` right after each tool
call completes) as `run_chat()`'s `on_progress` callback fires. The
dashboard's chat panel listens for these and grows its pending bubble's
action list one entry at a time instead of waiting for the whole turn to
finish before showing anything.

### Fleet load & trend visibility

`GET /api/fleet/load` — active + queued work per robot, so it's obvious
who's overworked vs idle. `GET /api/statistics/history` — a rolling
sample of key statistics recorded every tick (`DigitalTwin.
record_history`, capped at `CONFIG["HISTORY_MAX_SAMPLES"]`), for the
dashboard's trend sparklines. On the dashboard: the **Fleet load** panel,
the **Trends** panel, and a notification bell that surfaces every
`WARNING`-and-above event (battery low, a maintenance alert, a mid-task
authorization change, an operator's shift toggling, ...) so something
noteworthy isn't missed just because you were looking elsewhere.

### Mission reports

`GET /api/reports/missions.csv` / `GET /api/reports/missions.html` — the
same Mission Authorization Record data (`decision_graph.query_decisions`
with no filters) as a downloadable spreadsheet or a clean printable page
(open it and use the browser's own Print → Save as PDF — no server-side
PDF library added, keeping dependencies to Flask, pytest and PyYAML).

---

## Dashboard

- **Header** — system lamp, robot and box counts, active and completed tasks, collisions, average battery, simulation clock and tick, and a notification bell surfacing every `WARNING`-and-above event.
- **Chat agent** — 🤖 launcher, bottom-right, on every page. Give it a plain-English command or a question — see [Chat agent](#chat-agent).
- **Warehouse floor** — canvas floor plan with hatched racking, hazard-striped restricted area, zone labels, live routes drawn as marching dashes toward a target reticle, robots with heading, battery gauge and status, and carried boxes riding on the chassis.
- **Tasks** — one card per task with priority stripe, status, progress bar and current action. Click a card for the full plan, the execution history and pause / resume / cancel.
- **Robots** — position, heading, task, cargo, target, remaining route, distance, completed and failed tasks, boxes delivered, waits, replans, charge cycles and a per-robot **Capabilities** checklist, plus **Stop**, **Resume**, **Charge** and **Reset**.
- **Fleet load** — active + queued work per robot, so it's obvious who's overworked vs idle.
- **Boxes** — status, position, weight, destination and current holder.
- **Statistics** — twenty-one live counters.
- **Trends** — sparklines for average battery, active tasks and collisions over time.
- **Mission report** — download the graded task history as a `.csv` or open a printable `.html` report.
- **Mock CI** — the pipeline and per-check results.
- **Event timeline** — the selected task's state transitions in order.
- **Live system logs** — the streaming log with search, filters and export.
- **Decision history search** — filter every graded task ever logged by robot, agent, operator, task type, verdict or a battery-below threshold.
- **Recurring tasks** — add/pause/remove a schedule that fires a task on an interval.
- **Policies** — the trust-layer values in effect right now, a live AI-narration toggle, and the **Collision risk** slider (see [Collision risk](#collision-risk)).
- **Simulation** — Start, Pause, Stop, Reset, speed 0.5× to 10×, Save / Load state, and a prominent **Emergency stop**.

**Emergency stop** halts every robot, freezes the simulation, keeps tasks
intact, and writes `[CRITICAL] Emergency stop activated by user`. **Resume
simulation** restores each robot's previous status and its work continues.

---

## API

### Reads

| Method | Path | Returns |
|---|---|---|
| GET | `/api/state` | Full snapshot including the floor plan and config |
| GET | `/api/warehouse` | Floor plan only |
| GET | `/api/robots` | All robots |
| GET | `/api/robots/{id}` | One robot |
| GET | `/api/boxes` | All boxes |
| GET | `/api/tasks` | Tasks and counts (`?status=`, `?limit=`) |
| GET | `/api/tasks/{id}` | One task with its events and logs |
| GET | `/api/tasks/{id}/eval` | Eval engine verdict for one task's full persisted log (see [Eval Engine](#eval-engine)) |
| GET | `/api/logs` | Logs (`?level=&category=&robot_id=&task_id=&search=&limit=&since=`) |
| GET | `/api/events` | Events (`?task_id=&robot_id=&event=&limit=`) |
| GET | `/api/statistics` | Statistics, environment state, per-robot statistics |
| GET | `/api/statistics/history` | Rolling per-tick samples for the dashboard's trend charts |
| GET | `/api/fleet/load` | Active + queued work per robot |
| GET | `/api/maintenance/alerts` | Every robot currently over a predictive-maintenance threshold |
| GET | `/api/decisions` | Decision history search (`?robot=&agent=&operator=&type=&verdict=&battery_below=`) |
| GET | `/api/policies` | The policy values actually in effect right now |
| GET | `/api/reports/missions.csv` | Every graded task as a downloadable spreadsheet |
| GET | `/api/reports/missions.html` | The same report as a clean, printable page |
| GET | `/api/ci/status` | Last CI run, check names, recent history |
| GET | `/api/health` | Liveness, simulation status, tick, connected clients |
| GET | `/api/stream` | Server-Sent Events (`state`, `log`, `event`, `ci`) |

### Robots

| Method | Path | Body |
|---|---|---|
| POST | `/api/robots` | `{"name","speed","x","y","allowed_task_types","robot_class"}` |
| POST | `/api/robots/{id}/capabilities` | `{"allowed_task_types"}` — empty list/omitted clears the restriction |
| POST | `/api/robots/{id}/stop` | — |
| POST | `/api/robots/{id}/resume` | — |
| POST | `/api/robots/{id}/charge` | — |
| POST | `/api/robots/{id}/reset` | — |

### Boxes

| Method | Path | Body |
|---|---|---|
| POST | `/api/boxes` | `{"name","weight","x","y","destination"}` |

### Agents / operators

| Method | Path | Body |
|---|---|---|
| POST | `/api/agents` | `{"name","model_version"}` |
| POST | `/api/operators` | `{"name","certifications","shift_start_hour","shift_end_hour","role"}` |
| POST | `/api/operators/{id}/shift` | `{"shift_start_hour","shift_end_hour"}` — omit/null both to clear |

### Tasks

| Method | Path | Body |
|---|---|---|
| POST | `/api/tasks` | `{"type","robot_id","box_id","box_ids","source","destination","priority","agent_id","operator_id","dual_signoff","second_operator_id"}` |
| POST | `/api/tasks/{id}/cancel` | — |
| POST | `/api/tasks/{id}/pause` | — |
| POST | `/api/tasks/{id}/resume` | — |

### Recurring tasks

| Method | Path | Body |
|---|---|---|
| GET | `/api/schedules` | — |
| POST | `/api/schedules` | `{"payload": {"type": ...}, "interval_seconds"}` |
| POST | `/api/schedules/{id}/toggle` | `{"enabled"}` — omit to just flip it |
| DELETE | `/api/schedules/{id}` | — |

### Policy-as-code

| Method | Path | Body |
|---|---|---|
| POST | `/api/policies/reload` | Re-reads `policies.yaml` live, no restart |
| POST | `/api/policies/llm-narration` | `{"enabled"}` — live, in-memory only (see [Optional live AI narration](#optional-live-ai-narration)) |
| POST | `/api/policies/collision-risk` | `{"risk"}` (0-1) — live, in-memory only (see [Collision risk](#collision-risk)) |

### Chat agent

| Method | Path | Body |
|---|---|---|
| POST | `/api/agent/chat` | `{"message","history"}` — see [Chat agent](#chat-agent) |

### Simulation

| Method | Path | Body |
|---|---|---|
| POST | `/api/simulation/start` | — |
| POST | `/api/simulation/pause` | — |
| POST | `/api/simulation/stop` | — |
| POST | `/api/simulation/reset` | `{"demo_tasks": true}` |
| POST | `/api/simulation/emergency-stop` | — |
| POST | `/api/simulation/resume` | — |
| POST | `/api/simulation/speed` | `{"speed": 5}` |
| POST | `/api/simulation/tick` | Advance exactly one tick |

### CI, logs, persistence, export

| Method | Path | Notes |
|---|---|---|
| POST | `/api/ci/run` | Runs the pipeline, returns the summary |
| POST | `/api/evals/run` | Runs the eval engine over every `logs/tasks/*.json` file on disk, returns pass/warn/fail totals plus full per-task reports |
| POST | `/api/logs/clear` | Clears memory and both log files |
| GET | `/api/logs/export?format=txt\|json` | `warehouse_logs.txt` / `.json` |
| GET | `/api/export/state` | `warehouse_state.json` |
| GET | `/api/export/tasks` | `tasks.json` |
| POST | `/api/state/save` | Writes `data/warehouse_state.json` |
| POST | `/api/state/load` | Restores from that file |
| POST | `/api/state/reset` | Back to the demo configuration |

### Errors

Failures return a JSON body and a matching status: `400` for a malformed or
impossible request, `404` for something that does not exist, `422` for a task
that was created but failed validation.

```json
{"ok": false, "error": "Box-A is already reserved by task_001", "field": null}
```

Covered cases include unknown robot, box, task, destination or task type;
out-of-grid coordinates; no path available; robot or box unavailable; battery
insufficient; blocked path; and invalid state transitions such as cancelling a
finished task.

---

## Testing

```bash
source .venv/bin/activate
python -m pytest
```

253 tests (156 in `backend/tests.py`, 68 in
`backend/test_eval_engine.py`, and 29 in `backend/test_agent_chat.py`),
roughly one second — including coverage for the chat agent's tools (every
tool's schema has a matching executor, task creation/cancellation, eval
inclusion, its own guard rails for no API key / an empty message,
rate-limit backoff parsing and a real retry-past-a-429 run, the
empty-final-message and step-cap fallback summaries, and `on_progress`
firing for each step without ever being able to break the chat turn),
every operator role (preset certifications/shift, explicit overrides,
an unknown role rejected, and the demo seed staying exactly Sam/Lee),
per-robot task-type restriction, the four agent/operator "own work"
task types (`AGENT_REPLAN`, `AGENT_AUDIT`, `OPERATOR_APPROVAL`,
`OPERATOR_MAINTENANCE_SIGNOFF`), all six robot classes (and that every
one can still charge itself), collision risk actually causing a
collision (and disconnecting a task) during normal work, predictive
maintenance, operator shifts, two-person
sign-off (including its second signer's own `entities_valid` grade),
`BATCH_DELIVER`, mid-task authorization changes (both the simulator's
flag and the eval engine's `WARN`), the scheduler, policy-as-code's
in-place-mutation contract, decision history search, and
optional LLM narration staying off (and network-free) by default. The
simulation is driven tick-by-tick — no threads, no sleeps, no flakiness.

```bash
python -m pytest -v                                   # every test named
python -m pytest -k "collision or battery"            # a subset
python -m pytest backend/tests.py::test_api_ci_run    # one test
python -m pytest backend/test_eval_engine.py          # just the eval engine
```

Coverage spans the warehouse layout and reachability; robot creation, movement,
boundaries and reset; A\* correctness, shelf avoidance, blocked goals, dynamic
obstacles; box pickup, carrying and delivery; task creation, every validation
rule, planning, all eight task types, cancel, pause and resume; priority
ordering and AUTO selection; simultaneous operation, waiting and replanning;
battery drain, low-battery events, recharge detours and self-charging;
emergency stop and release; snapshot integrity and mid-mission twin
consistency; structured logs, filters, export and disk persistence; event
generation; save/load round trips; the CI pipeline passing when healthy and
failing on injected faults; every HTTP endpoint including error paths; and
the eval engine's eight checks, each verified against small hand-built
event lists, the bundled example logs in `logs/eval_examples/`, and the
project's own real logs in `logs/tasks/`.

---

## Troubleshooting

**Port 5000 is already in use.**
`WAREHOUSE_PORT=8080 ./run.sh`, or find the holder with `ss -ltnp | grep 5000`.

**The dashboard loads but nothing moves.**
Check the header lamp. If it says *stopped* or *paused*, press **Start**. If it
says *reconnecting*, the SSE connection dropped — reload the page. `curl
http://127.0.0.1:5000/api/health` should report `"simulation": "RUNNING"`.

**A task went straight to FAILED.**
Open it from the task board; the reason is on the card and in the log at `ERROR`
level. The usual causes are a box already reserved by another task, a
destination that does not resolve, or a robot in an error state.

**A robot sits in WAITING.**
It is yielding to a higher-priority robot. It replans after three ticks and
sidesteps after twenty. Filter the log to the `COLLISION` category to watch it.

**A robot is stuck in ERROR.**
Its battery hit zero, or its controller raised. Press **Reset** on the robot
card: it returns home with a full battery and drops any cargo.

**Fonts look wrong.**
The dashboard loads Saira Condensed, Archivo and JetBrains Mono from Google
Fonts. Offline, it falls back to local condensed and monospace faces; nothing
else changes.

**Everything is confused after editing the code.**
`curl -X POST http://127.0.0.1:5000/api/state/reset` or press **Reset** to
rebuild the demo warehouse, and delete `logs/` and `data/` if you want a clean
slate.

**Tests fail with an import error.**
Run pytest from the project root so `backend` is importable, with the virtual
environment active.

---

## Future extensions

The seams are already in place.

**ROS 2** — `Simulator._tick_robot` is the only place that moves a robot.
Replace the `step_to` call with a `nav2` `NavigateToPose` goal and let the
digital twin consume `/odom` instead of writing positions itself. `Robot` and
`Task` stay unchanged.

**Gazebo / Gz-Sim** — `Warehouse.to_dict()` already emits the floor plan as
data; generate SDF from it so the simulated world matches the twin exactly.

**NVIDIA Isaac Sim** — keep this service as the task and mission layer and
publish action lists over its Python API; the fidelity of the physics layer is
independent of the planner.

**LiDAR and cameras** — add a `sensors.py` publishing readings into the event
system; `NavigationEngine.find_path` already accepts an arbitrary `blocked` set,
so perceived obstacles slot straight into replanning.

**SLAM** — swap the fixed grid for an occupancy grid behind the `Warehouse`
interface (`is_walkable`, `neighbors`, `nearest_walkable`). Nothing above it
needs to know.

**Computer vision** — resolve `Action.box_id` from a detection instead of a
lookup, and let box states be confirmed by perception rather than assumed.

**MQTT / IoT** — `EventSystem.subscribe` takes any callable; a bridge that
republishes events to an MQTT topic is a dozen lines.

**Real robots and hardware-in-the-loop** — implement a `RobotAdapter` with the
same surface as `Robot.step_to`, `consume_battery` and `charge`, then run some
robots simulated and others physical inside the same twin.

**Cloud** — swap the in-process stores for Postgres or Redis behind
`DigitalTwin`, and put the SSE endpoint behind a message broker for horizontal
scale.

---

## Layout

```
warehouse-digital-twin/
├── backend/
│   ├── __init__.py
│   ├── app.py              Flask API, SSE, static hosting
│   ├── models.py           Enums, CONFIG, Action, id factory
│   ├── warehouse.py        Grid and named zones
│   ├── robot.py            Robot entity
│   ├── box.py              Box entity
│   ├── navigation.py       A* engine
│   ├── task_manager.py     Intake, validation (+ pre-execution auth gate), queue, assignment
│   ├── eligibility.py      Shared robot/agent/operator eligibility rule (gate + eval engine)
│   ├── task_planner.py     Task → actions, battery estimation
│   ├── simulator.py        The simulation loop
│   ├── digital_twin.py     Single source of truth
│   ├── event_system.py     Event bus + SSE broadcaster
│   ├── logger.py           Structured logging
│   ├── ci_engine.py        Mock CI
│   ├── eval_engine.py      Eval engine — grades a task's JSON log, PASS/WARN/FAIL + reasons
│   ├── policy.py           Policy-as-code — policies.yaml overrides models.py's defaults
│   ├── maintenance.py      Predictive maintenance wear model
│   ├── scheduler.py        Recurring tasks
│   ├── decision_graph.py   Decision history search over logs/tasks/*.json
│   ├── llm.py              Optional live Groq narration for AGENT_REPLAN/AGENT_AUDIT
│   ├── agent_tools.py      The chat agent's tools — create/cancel/query, real actions
│   ├── agent_chat.py       The chat agent's tool-use loop (POST /api/agent/chat)
│   ├── run_evals.py        CLI scenario runner for the eval engine
│   ├── tests.py            Pytest suite
│   ├── test_eval_engine.py Pytest suite for the eval engine
│   └── test_agent_chat.py  Pytest suite for the chat agent's tools
├── evals/                  Promptfoo suite for the eval engine (see evals/README.md)
├── frontend/
│   ├── index.html
│   ├── style.css
│   └── app.js
├── logs/
│   ├── warehouse.log       Readable lines
│   ├── events.json         JSON Lines
│   ├── tasks/               One JSON file per task — the eval engine's input
│   └── eval_examples/       One example log per failure case, for --demo and the tests
├── data/
│   └── warehouse_state.json
├── policies.example.yaml   Copy to policies.yaml to override the built-in defaults
├── requirements.txt
├── pytest.ini
├── run.sh
└── README.md
```

Log and state files are created on first run.
