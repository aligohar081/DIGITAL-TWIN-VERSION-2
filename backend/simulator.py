"""The simulation engine.

One :meth:`Simulator.tick` advances the whole warehouse by ``TICK_DT`` logical
seconds: queued tasks get dispatched, every robot executes one step of its plan,
collisions are resolved, batteries drain, the digital twin is synchronised and a
frame is pushed to the dashboard.

``tick`` is deliberately callable on its own so tests can drive the world
deterministically without threads.
"""
from __future__ import annotations

import random
import threading
import time
from typing import Any, Dict, List, Optional, Set, Tuple

from datetime import datetime

from .eligibility import robot_eligibility
from .maintenance import maintenance_reason
from .models import (
    CONFIG,
    ActionType,
    Cell,
    BoxStatus,
    EventType,
    LogCategory,
    LogLevel,
    OperatorStatus,
    PRIORITY_RANK,
    Priority,
    RobotStatus,
    SimulationStatus,
    TaskStatus,
    TaskType,
    cell_dict,
)

PICK_TICKS = 4
DELIVER_TICKS = 4


class Simulator:
    def __init__(self, twin: Any, broadcaster: Optional[Any] = None) -> None:
        self.twin = twin
        self.broadcaster = broadcaster
        self._thread: Optional[threading.Thread] = None
        self._stop = threading.Event()
        self.dt = CONFIG["TICK_DT"]

    # ------------------------------------------------------------------ #
    # Thread control
    # ------------------------------------------------------------------ #
    def start_thread(self) -> None:
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._thread = threading.Thread(target=self._run, name="simulation-loop", daemon=True)
        self._thread.start()

    def stop_thread(self) -> None:
        self._stop.set()
        if self._thread:
            self._thread.join(timeout=2.0)

    def _run(self) -> None:
        while not self._stop.is_set():
            started = time.time()
            try:
                if self.twin.simulation_status == SimulationStatus.RUNNING:
                    self.tick()
            except Exception as exc:  # keep the loop alive, but say so loudly
                self.twin.logger.critical(
                    LogCategory.SYSTEM, f"Simulation tick failed: {exc!r}"
                )
            speed = max(0.1, float(self.twin.simulation_speed or 1.0))
            budget = self.dt / speed
            elapsed = time.time() - started
            time.sleep(max(0.005, budget - elapsed))

    # ------------------------------------------------------------------ #
    # Public controls
    # ------------------------------------------------------------------ #
    def start(self) -> None:
        if self.twin.simulation_status == SimulationStatus.EMERGENCY_STOP:
            self.twin.release_emergency_stop()
            return
        self.twin.set_simulation_status(
            SimulationStatus.RUNNING, "Simulation started", EventType.SIMULATION_STARTED
        )

    def pause(self) -> None:
        self.twin.set_simulation_status(
            SimulationStatus.PAUSED, "Simulation paused", EventType.SIMULATION_PAUSED,
            level=LogLevel.WARNING,
        )

    def stop(self) -> None:
        for robot in self.twin.robots.values():
            robot.move_accumulator = 0.0
        self.twin.set_simulation_status(
            SimulationStatus.STOPPED, "Simulation stopped", EventType.SIMULATION_STOPPED,
            level=LogLevel.WARNING,
        )

    def set_speed(self, speed: float) -> float:
        speed = float(speed)
        if speed <= 0:
            raise ValueError("Speed must be greater than zero")
        self.twin.simulation_speed = speed
        self.twin.logger.info(LogCategory.SYSTEM, f"Simulation speed set to {speed}x")
        return speed

    # ------------------------------------------------------------------ #
    # The tick
    # ------------------------------------------------------------------ #
    def tick(self) -> None:
        twin = self.twin
        with twin.lock:
            twin.tick_count += 1
            twin.simulation_time = round(twin.simulation_time + self.dt, 3)

            twin.scheduler.tick()
            twin.tasks.dispatch()

            for robot in self._execution_order():
                try:
                    self._tick_robot(robot)
                except Exception as exc:
                    robot.last_error = repr(exc)
                    robot.set_status(RobotStatus.ERROR)
                    twin.events.emit(
                        EventType.ROBOT_ERROR,
                        f"{robot.name} controller error: {exc}",
                        category=LogCategory.ROBOT,
                        level=LogLevel.ERROR,
                        robot_id=robot.id,
                    )
                    task = twin.tasks.get(robot.current_task) if robot.current_task else None
                    if task is not None and not task.is_terminal:
                        twin.tasks.fail_task(task, f"Controller error: {exc}")

            self._detect_collisions()
            self._auto_charge()
            self._check_maintenance()
            if twin.tick_count % CONFIG["AUTHORIZATION_CHECK_EVERY_TICKS"] == 0:
                self._check_authorization_changes()
            if twin.tick_count % CONFIG["SHIFT_CHECK_EVERY_TICKS"] == 0:
                self._check_operator_shifts()
            twin.synchronize()
            twin.record_history()

        self._publish()

    def _execution_order(self) -> List[Any]:
        """Higher-priority missions move first, which also decides right of way."""
        twin = self.twin

        def key(robot: Any) -> Tuple[int, str]:
            task = twin.tasks.get(robot.current_task) if robot.current_task else None
            rank = PRIORITY_RANK[task.priority] if task else -1
            return (-rank, robot.id)

        return sorted(twin.robots.values(), key=key)

    # ------------------------------------------------------------------ #
    # Per-robot controller
    # ------------------------------------------------------------------ #
    def _tick_robot(self, robot: Any) -> None:
        twin = self.twin
        if robot.is_halted:
            return

        task = twin.tasks.get(robot.current_task) if robot.current_task else None

        if task is None:
            if robot.status == RobotStatus.CHARGING:
                self._continue_idle_charge(robot)
            elif robot.status != RobotStatus.IDLE:
                robot.set_status(RobotStatus.IDLE)
                robot.clear_path()
            return

        if task.status == TaskStatus.PAUSED:
            return
        if task.is_terminal:
            robot.current_task = None
            robot.clear_path()
            robot.set_status(RobotStatus.IDLE)
            return
        if task.status == TaskStatus.ASSIGNED:
            twin.tasks.start_task(task)

        action = task.current_action
        if action is None:
            twin.tasks.complete_task(task)
            return

        if action.type == ActionType.NAVIGATE:
            self._act_navigate(robot, task, action)
        elif action.type == ActionType.PICK:
            self._act_pick(robot, task, action)
        elif action.type == ActionType.DELIVER:
            self._act_deliver(robot, task, action)
        elif action.type == ActionType.CHARGE:
            self._act_charge(robot, task, action)
        elif action.type == ActionType.COMPLETE:
            action.done = True
            twin.tasks.complete_task(task)
        else:  # WAIT
            action.done = True
            self._advance(task)

    def _advance(self, task: Any) -> None:
        current = task.current_action
        if current is not None:
            current.done = True
        task.action_index += 1

    # ---- navigate ----------------------------------------------------- #
    def _act_navigate(self, robot: Any, task: Any, action: Any) -> None:
        twin = self.twin
        target = action.target
        if target is None:
            self._advance(task)
            return

        if robot.position == target:
            robot.clear_path()
            twin.logger.info(
                LogCategory.ROBOT,
                f"{robot.name} reached {action.target_name or 'target'}",
                robot_id=robot.id,
                task_id=task.id,
                position=cell_dict(robot.position),
            )
            self._advance(task)
            return

        if not action.started:
            action.started = True
            twin.tasks.set_status(
                task,
                TaskStatus.TRANSPORTING if robot.carrying_box else TaskStatus.IN_PROGRESS,
                action.description,
            )

        if not robot.current_path or robot.target_position != target:
            if not self._plan_path(robot, task, target, action.target_name, replan=False):
                return

        robot.set_status(RobotStatus.DELIVERING if robot.carrying_box else RobotStatus.MOVING)

        next_cell = robot.next_cell
        if next_cell is None:
            robot.clear_path()
            return

        blocker = self._blocking_robot(robot, next_cell)
        if blocker is not None:
            risk = CONFIG["COLLISION_RISK"]
            if risk <= 0 or random.random() >= risk:
                self._handle_block(robot, task, blocker, target, action)
                return
            # The risk roll landed — this robot fails to avoid `blocker`
            # this tick (imperfect real-time sensing/timing) and drives
            # straight into the conflict instead of waiting/replanning.
            # If that actually lands both robots on the same cell,
            # _detect_collisions() catches it right after this tick's
            # per-robot loop and applies the real consequence — see
            # DigitalTwin.register_collision.
            twin.logger.warning(
                LogCategory.COLLISION,
                f"{robot.name} failed to avoid {blocker.name} in time — proceeding anyway",
                robot_id=robot.id,
                task_id=task.id,
                position=cell_dict(next_cell),
            )

        robot.wait_ticks = 0
        robot.blocked_by = None
        if not robot.ready_to_step(self.dt):
            return

        previous = robot.position
        robot.step_to(next_cell)
        twin.statistics["distance_travelled"] += 1
        if robot.carrying_box:
            box = twin.find_box(robot.carrying_box)
            if box is not None:
                box.position = robot.position
                box.touch()
        twin.logger.debug(
            LogCategory.ROBOT,
            f"{robot.name} moved from ({previous[0]},{previous[1]}) "
            f"to ({robot.position[0]},{robot.position[1]})",
            robot_id=robot.id,
            task_id=task.id,
            position=cell_dict(robot.position),
        )
        twin.events.emit(
            EventType.ROBOT_MOVED,
            f"{robot.name} moved to ({robot.position[0]},{robot.position[1]})",
            category=LogCategory.NAVIGATION,
            level=LogLevel.DEBUG,
            robot_id=robot.id,
            task_id=task.id,
            position=cell_dict(robot.position),
            log=False,
        )
        self._apply_movement_battery(robot, task)

        if robot.position == target:
            robot.clear_path()
            twin.logger.info(
                LogCategory.ROBOT,
                f"{robot.name} reached {action.target_name or 'target'}",
                robot_id=robot.id,
                task_id=task.id,
                position=cell_dict(robot.position),
            )
            self._advance(task)

    def _plan_path(
        self,
        robot: Any,
        task: Any,
        target: Cell,
        target_name: Optional[str],
        replan: bool,
        extra_blocked: Optional[Set[Cell]] = None,
    ) -> bool:
        twin = self.twin
        blocked = twin.other_robot_cells(robot.id)
        if extra_blocked:
            blocked |= extra_blocked
        path = twin.navigation.find_path(
            robot.position, target, blocked=blocked, allow_goal_adjacent=False, is_replan=replan
        )
        if path is None:
            # Is the route only blocked by robots, or genuinely impossible?
            static_path = twin.navigation.find_path(
                robot.position, target, allow_goal_adjacent=False
            )
            if static_path is None:
                twin.events.emit(
                    EventType.PATH_NOT_FOUND,
                    f"No route from ({robot.position[0]},{robot.position[1]}) to "
                    f"({target[0]},{target[1]}) for {robot.name}",
                    category=LogCategory.NAVIGATION,
                    level=LogLevel.ERROR,
                    robot_id=robot.id,
                    task_id=task.id,
                )
                twin.tasks.fail_task(task, "No path available to the target")
                return False
            robot.set_status(RobotStatus.WAITING)
            twin.tasks.set_status(task, TaskStatus.BLOCKED, "Route temporarily blocked by traffic")
            return False

        robot.set_path(path, target_name)
        event = EventType.PATH_RECALCULATED if replan else EventType.PATH_CREATED
        twin.events.emit(
            event,
            f"{'Alternative' if replan else 'A*'} path for {robot.name} → "
            f"{target_name or f'({target[0]},{target[1]})'}: {len(path)} cells",
            category=LogCategory.NAVIGATION,
            robot_id=robot.id,
            task_id=task.id,
            data={"cells": len(path), "target": cell_dict(target)},
        )
        if replan:
            robot.replan_count += 1
            task.replans += 1
        return True

    # ---- traffic ------------------------------------------------------ #
    def _precedence(self, robot: Any) -> Tuple[int, str]:
        task = self.twin.tasks.get(robot.current_task) if robot.current_task else None
        rank = PRIORITY_RANK[task.priority] if task else -1
        return (rank, robot.id)

    def _blocking_robot(self, robot: Any, cell: Cell) -> Optional[Any]:
        mine = self._precedence(robot)
        for other in self.twin.robots.values():
            if other.id == robot.id:
                continue
            if other.position == cell:
                return other
            if other.next_cell == cell and self._precedence(other) > mine:
                return other
        return None

    def _handle_block(self, robot: Any, task: Any, blocker: Any, target: Cell, action: Any) -> None:
        twin = self.twin
        robot.wait_ticks += 1
        robot.blocked_by = blocker.id

        if robot.wait_ticks == 1:
            robot.wait_events += 1
            twin.statistics["collisions_avoided"] += 1
            robot.set_status(RobotStatus.WAITING)
            twin.events.emit(
                EventType.COLLISION_AVOIDED,
                f"Potential collision at ({robot.next_cell[0]},{robot.next_cell[1]}): "
                f"{blocker.name} is in the way of {robot.name}",
                category=LogCategory.COLLISION,
                level=LogLevel.WARNING,
                robot_id=robot.id,
                task_id=task.id,
                position=cell_dict(robot.next_cell),
                data={"blocked_by": blocker.name},
            )
            twin.events.emit(
                EventType.ROBOT_WAITING,
                f"{robot.name} waiting for {blocker.name}",
                category=LogCategory.ROBOT,
                level=LogLevel.WARNING,
                robot_id=robot.id,
                task_id=task.id,
            )
            twin.tasks.set_status(task, TaskStatus.BLOCKED, f"Waiting for {blocker.name}")
            return

        head_on = blocker.next_cell == robot.position and blocker.position == robot.next_cell

        if robot.wait_ticks == CONFIG["REPLAN_AFTER_WAIT_TICKS"] or (head_on and robot.wait_ticks == 2):
            avoid = {blocker.position}
            if blocker.next_cell:
                avoid.add(blocker.next_cell)
            if self._plan_path(robot, task, target, action.target_name, replan=True, extra_blocked=avoid):
                robot.wait_ticks = 0
                robot.set_status(RobotStatus.DELIVERING if robot.carrying_box else RobotStatus.MOVING)
                twin.tasks.set_status(task, TaskStatus.IN_PROGRESS, "Alternative route accepted")
            return

        if robot.wait_ticks >= CONFIG["DEADLOCK_WAIT_TICKS"]:
            self._break_deadlock(robot, task, blocker)

    def _break_deadlock(self, robot: Any, task: Any, blocker: Any) -> None:
        """Step aside so the higher-priority robot can pass."""
        twin = self.twin
        occupied = twin.other_robot_cells(robot.id)
        options = [
            cell
            for cell in twin.warehouse.neighbors(robot.position)
            if cell not in occupied and cell != blocker.position
        ]
        if not options:
            robot.wait_ticks = 0
            return
        options.sort(key=lambda c: -abs(c[0] - blocker.position[0]) - abs(c[1] - blocker.position[1]))
        sidestep = options[0]
        robot.step_to(sidestep)
        robot.clear_path()
        robot.wait_ticks = 0
        twin.events.emit(
            EventType.DEADLOCK_RESOLVED,
            f"{robot.name} stepped aside to ({sidestep[0]},{sidestep[1]}) to clear a deadlock "
            f"with {blocker.name}",
            category=LogCategory.COLLISION,
            level=LogLevel.WARNING,
            robot_id=robot.id,
            task_id=task.id,
            position=cell_dict(sidestep),
        )

    def _detect_collisions(self) -> None:
        """A real, natural overlap — under normal operation this should
        never actually fire (the traffic system in _handle_block/
        _break_deadlock actively avoids it); see
        DigitalTwin.force_collision for a deliberate way to trigger one
        on demand instead. Robots already in ERROR are excluded from the
        scan: DigitalTwin.register_collision already halted them and
        failed their task the moment they collided, so they'd otherwise
        get re-flagged as a "fresh" collision every single tick forever
        while they sit stacked on the same cell waiting to be Reset."""
        twin = self.twin
        seen: Dict[Cell, Any] = {}
        for robot in twin.robots.values():
            if robot.status == RobotStatus.ERROR:
                continue
            other = seen.get(robot.position)
            if other is not None:
                twin.register_collision(robot.id, other.id)
            else:
                seen[robot.position] = robot

    # ---- handling ----------------------------------------------------- #
    def _act_pick(self, robot: Any, task: Any, action: Any) -> None:
        twin = self.twin
        box = twin.find_box(action.box_id or task.box_id)
        if box is None:
            twin.tasks.fail_task(task, f"Box '{action.box_id}' vanished before pickup")
            return
        if robot.position != box.position:
            # The box moved; go and get it.
            action.target = box.position
            if not self._plan_path(robot, task, box.position, box.name, replan=True):
                return
            robot.set_status(RobotStatus.MOVING)
            return

        if not action.started:
            action.started = True
            robot.action_timer = 0
            robot.set_status(RobotStatus.PICKING)
            previous = box.set_status(BoxStatus.PICKING)
            twin.tasks.set_status(task, TaskStatus.PICKING, f"Picking {box.name}")
            twin.logger.info(
                LogCategory.ROBOT,
                f"{robot.name} started picking {box.name} ({previous.value} → PICKING)",
                robot_id=robot.id,
                task_id=task.id,
                box_id=box.id,
                position=cell_dict(robot.position),
            )
            return

        robot.action_timer += 1
        if robot.action_timer < PICK_TICKS:
            return

        previous = box.set_status(BoxStatus.CARRIED)
        box.assigned_robot = robot.id
        box.assigned_task = task.id
        box.position = robot.position
        # Remembered only so a FALSE_SUCCESS_RISK fault (see _act_deliver)
        # has somewhere real to snap the box back to — not read anywhere
        # else.
        box._picked_from_position = box.position
        box.pick_count += 1
        robot.carrying_box = box.id
        robot.set_status(RobotStatus.CARRYING)
        robot.consume_battery(CONFIG["BATTERY_PICK_COST"])
        twin.events.emit(
            EventType.BOX_PICKED,
            f"{robot.name} picked {box.name} ({previous.value} → CARRIED)",
            category=LogCategory.BOX,
            robot_id=robot.id,
            task_id=task.id,
            box_id=box.id,
            position=cell_dict(robot.position),
        )
        self._check_battery_thresholds(robot, task)
        self._advance(task)

    def _act_deliver(self, robot: Any, task: Any, action: Any) -> None:
        twin = self.twin
        box = twin.find_box(action.box_id or task.box_id)
        if box is None:
            twin.tasks.fail_task(task, f"Box '{action.box_id}' vanished before delivery")
            return
        if robot.carrying_box != box.id:
            twin.tasks.fail_task(task, f"{robot.name} is not carrying {box.name}")
            return

        if not action.started:
            action.started = True
            robot.action_timer = 0
            robot.set_status(RobotStatus.DELIVERING)
            box.set_status(BoxStatus.DELIVERING)
            twin.tasks.set_status(task, TaskStatus.DELIVERING, f"Delivering {box.name}")
            twin.logger.info(
                LogCategory.ROBOT,
                f"{robot.name} started delivering {box.name} at {action.target_name}",
                robot_id=robot.id,
                task_id=task.id,
                box_id=box.id,
                position=cell_dict(robot.position),
            )
            return

        robot.action_timer += 1
        if robot.action_timer < DELIVER_TICKS:
            return

        previous = box.set_status(BoxStatus.DELIVERED)
        # CONFIG["FALSE_SUCCESS_RISK"] (0.0 by default — see models.py):
        # the payload actually slips off the fork right at the final
        # release and lands back where it was picked up — no independent
        # sensor confirms a real seat at the destination, only the same
        # controller that now believes it succeeded. Everything below
        # this still runs exactly as if the delivery had gone perfectly
        # (status, counters, BOX_DELIVERED, task advancing) — a
        # confidently-wrong success, on purpose, that only eval_engine.
        # check_state_transition's after-the-fact before/after diff can
        # ever catch. A quiet DEBUG breadcrumb is left for anyone reading
        # the full log on purpose; it never raises the log level, so it
        # never reaches the dashboard's WARNING+ notification bell the
        # way a real fault would.
        false_success = random.random() < CONFIG.get("FALSE_SUCCESS_RISK", 0.0)
        if false_success:
            box.position = getattr(box, "_picked_from_position", box.position)
            twin.logger.debug(
                LogCategory.BOX,
                f"{box.name} delivery payload placement went unverified — "
                "internal state will not reflect the reported delivery",
                robot_id=robot.id,
                task_id=task.id,
                box_id=box.id,
            )
        else:
            box.position = robot.position
        box.destination = action.target_name or box.destination
        box.delivery_count += 1
        box.assigned_robot = None
        box.assigned_task = None
        robot.carrying_box = None
        robot.boxes_delivered += 1
        robot.consume_battery(CONFIG["BATTERY_DELIVER_COST"])
        twin.statistics["boxes_delivered"] += 1
        twin.events.emit(
            EventType.BOX_DELIVERED,
            f"{box.name} delivered to {action.target_name} ({previous.value} → DELIVERED)",
            category=LogCategory.BOX,
            robot_id=robot.id,
            task_id=task.id,
            box_id=box.id,
            position=cell_dict(robot.position),
        )
        self._check_battery_thresholds(robot, task)
        self._advance(task)

    # ---- energy ------------------------------------------------------- #
    def _act_charge(self, robot: Any, task: Any, action: Any) -> None:
        twin = self.twin
        if not action.started:
            action.started = True
            robot.charging_sessions += 1
            twin.statistics["charging_sessions"] += 1
            robot.set_status(RobotStatus.CHARGING)
            twin.events.emit(
                EventType.ROBOT_CHARGING,
                f"{robot.name} started charging at {robot.battery:.0f}%",
                category=LogCategory.BATTERY,
                robot_id=robot.id,
                task_id=task.id,
                position=cell_dict(robot.position),
            )
            return

        robot.charge(CONFIG["BATTERY_CHARGE_RATE"])
        if robot.battery >= 100.0:
            robot.low_battery_warned = False
            twin.events.emit(
                EventType.ROBOT_CHARGED,
                f"{robot.name} fully charged (CHARGING → IDLE)",
                category=LogCategory.BATTERY,
                robot_id=robot.id,
                task_id=task.id,
            )
            robot.set_status(RobotStatus.IDLE)
            self._advance(task)

    def _continue_idle_charge(self, robot: Any) -> None:
        robot.charge(CONFIG["BATTERY_CHARGE_RATE"])
        if robot.battery >= 100.0:
            robot.low_battery_warned = False
            robot.set_status(RobotStatus.IDLE)
            self.twin.events.emit(
                EventType.ROBOT_CHARGED,
                f"{robot.name} fully charged (CHARGING → IDLE)",
                category=LogCategory.BATTERY,
                robot_id=robot.id,
            )

    def _apply_movement_battery(self, robot: Any, task: Any) -> None:
        before = robot.drain_for_movement()
        if before is None:
            return
        self.twin.logger.debug(
            LogCategory.BATTERY,
            f"{robot.name} battery {before:.0f}% → {robot.battery:.0f}%",
            robot_id=robot.id,
            task_id=task.id if task else None,
        )
        self._check_battery_thresholds(robot, task)

    def _check_battery_thresholds(self, robot: Any, task: Any) -> None:
        twin = self.twin
        if robot.battery <= 0:
            robot.last_error = "Battery depleted"
            robot.set_status(RobotStatus.ERROR)
            twin.events.emit(
                EventType.ROBOT_ERROR,
                f"{robot.name} battery depleted — robot halted",
                category=LogCategory.BATTERY,
                level=LogLevel.CRITICAL,
                robot_id=robot.id,
                task_id=task.id if task else None,
            )
            if task is not None and not task.is_terminal:
                twin.tasks.fail_task(task, "Battery depleted mid-mission")
            return
        if robot.battery <= CONFIG["BATTERY_CRITICAL"] and not robot.low_battery_warned:
            robot.low_battery_warned = True
            twin.events.emit(
                EventType.BATTERY_CRITICAL,
                f"{robot.name} battery critical at {robot.battery:.0f}%",
                category=LogCategory.BATTERY,
                level=LogLevel.CRITICAL,
                robot_id=robot.id,
                task_id=task.id if task else None,
            )
        elif robot.battery <= CONFIG["BATTERY_LOW"] and not robot.low_battery_warned:
            robot.low_battery_warned = True
            twin.events.emit(
                EventType.BATTERY_LOW,
                f"{robot.name} battery low at {robot.battery:.0f}%",
                category=LogCategory.BATTERY,
                level=LogLevel.WARNING,
                robot_id=robot.id,
                task_id=task.id if task else None,
            )

    def _auto_charge(self) -> None:
        """Idle robots with a low battery take themselves to the charger."""
        twin = self.twin
        for robot in list(twin.robots.values()):
            if robot.is_halted or robot.current_task or robot.carrying_box:
                continue
            if robot.status == RobotStatus.CHARGING or robot.battery > CONFIG["BATTERY_LOW"]:
                continue
            if twin.warehouse.cell_type(*robot.position).value == "CHARGING":
                robot.charging_sessions += 1
                robot.set_status(RobotStatus.CHARGING)
                twin.events.emit(
                    EventType.ROBOT_CHARGING,
                    f"{robot.name} is already on the charger and started charging "
                    f"at {robot.battery:.0f}%",
                    category=LogCategory.BATTERY,
                    robot_id=robot.id,
                )
                continue
            twin.logger.info(
                LogCategory.BATTERY,
                f"{robot.name} is idle at {robot.battery:.0f}% — heading to the charging station",
                robot_id=robot.id,
            )
            try:
                twin.request_charge(robot.id, priority=Priority.HIGH, internal=True)
            except (KeyError, ValueError) as exc:
                twin.logger.error(
                    LogCategory.BATTERY, f"Auto-charge for {robot.name} failed: {exc}",
                    robot_id=robot.id,
                )

    # ---- governance / trust-layer checks ------------------------------ #
    def _check_maintenance(self) -> None:
        """Predictive maintenance (backend/maintenance.py) — flags a
        robot once (not every tick) when it crosses a wear threshold.
        Purely a nudge: doesn't touch the robot's status or task, just a
        WARNING event pointing at scheduling an
        OPERATOR_MAINTENANCE_SIGNOFF task, which is what actually resets
        the counters (see Robot.perform_maintenance)."""
        twin = self.twin
        for robot in twin.robots.values():
            reason = maintenance_reason(robot)
            if reason and not robot.maintenance_alerted:
                robot.maintenance_alerted = True
                twin.events.emit(
                    EventType.MAINTENANCE_ALERT,
                    f"{robot.name} is due for maintenance: {reason}",
                    category=LogCategory.ROBOT,
                    level=LogLevel.WARNING,
                    robot_id=robot.id,
                )

    def _check_authorization_changes(self) -> None:
        """Re-check every ACTIVE task's assigned robot against the exact
        same eligibility rule the pre-execution gate uses (backend/
        eligibility.py) — but here it FLAGS, it never gates: the robot is
        already mid-route, so forcibly killing the task would trade one
        safety problem for another. One flag per task (never re-flags
        the same task twice) — see Task.authorization_flagged and
        TRUST_LAYER.md's 'Authorization-changing events' entry."""
        twin = self.twin
        for task in twin.tasks.active():
            if not task.robot_id or task.authorization_flagged:
                continue
            robot = twin.find_robot(task.robot_id)
            if robot is None:
                continue
            reason = robot_eligibility(
                robot.status.value, robot.firmware_version, battery=None,
                task_type=task.type.value, allowed_task_types=robot.allowed_task_types,
            )
            if reason:
                task.authorization_flagged = reason
                twin.events.emit(
                    EventType.TASK_AUTHORIZATION_CHANGED,
                    f"{task.id}: {robot.name} {reason} — flagged mid-task, not halted",
                    category=LogCategory.TASK,
                    level=LogLevel.WARNING,
                    task_id=task.id,
                    robot_id=robot.id,
                )

    def _check_operator_shifts(self) -> None:
        """Automatic on/off-duty toggling for operators with a configured
        shift window (Operator.shift_start_hour/shift_end_hour) — real
        wall-clock hour, not simulated time, so a demo genuinely shows an
        off-duty operator outside real working hours. Never touches an
        operator with no shift configured (manual status control only,
        every operator's behaviour before this existed) or one currently
        ON_TASK — only flips between AVAILABLE and OFF_DUTY."""
        twin = self.twin
        hour = datetime.now().hour
        for operator in twin.operators.values():
            if not operator.has_shift or operator.status == OperatorStatus.ON_TASK:
                continue
            should_be = OperatorStatus.AVAILABLE if operator.is_within_shift(hour) else OperatorStatus.OFF_DUTY
            if operator.status != should_be:
                previous = operator.set_status(should_be)
                twin.events.emit(
                    EventType.OPERATOR_SHIFT_CHANGED,
                    f"{operator.name} {previous.value} → {should_be.value} "
                    f"(shift {operator.shift_start_hour}-{operator.shift_end_hour})",
                    category=LogCategory.TASK,
                )

    # ------------------------------------------------------------------ #
    # Streaming
    # ------------------------------------------------------------------ #
    def _publish(self) -> None:
        if self.broadcaster is None:
            return
        if self.twin.tick_count % CONFIG["STATE_EMIT_EVERY_TICKS"] != 0:
            return
        self.broadcaster.publish("state", self.twin.snapshot())
