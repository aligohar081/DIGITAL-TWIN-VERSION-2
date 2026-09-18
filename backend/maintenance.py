"""Predictive maintenance — a toy wear model, not a real one.

A robot has no real sensors here, so "wear" is approximated the honest
way: distance travelled and charge cycles completed since its last
OPERATOR_MAINTENANCE_SIGNOFF (see Robot.perform_maintenance). Once either
counter crosses its configured threshold (CONFIG["MAINTENANCE_
DISTANCE_THRESHOLD"] / CONFIG["MAINTENANCE_CHARGE_CYCLES_THRESHOLD"]),
the robot is flagged once (not every tick) with a MAINTENANCE_ALERT event
— a nudge to schedule an OPERATOR_MAINTENANCE_SIGNOFF task, not a hard
block; the robot keeps working. See Simulator._check_maintenance, which
calls check_robot() for every robot each maintenance-check tick.
"""
from __future__ import annotations

from typing import Any, Optional

from .models import CONFIG


def maintenance_reason(robot: Any) -> Optional[str]:
    """None if `robot` is within its wear thresholds; else why not."""
    distance_over = robot.distance_since_maintenance - CONFIG["MAINTENANCE_DISTANCE_THRESHOLD"]
    charges_over = robot.charges_since_maintenance - CONFIG["MAINTENANCE_CHARGE_CYCLES_THRESHOLD"]
    reasons = []
    if distance_over >= 0:
        reasons.append(
            f"{robot.distance_since_maintenance} cells travelled since its last sign-off "
            f"(threshold {CONFIG['MAINTENANCE_DISTANCE_THRESHOLD']})"
        )
    if charges_over >= 0:
        reasons.append(
            f"{robot.charges_since_maintenance} charge cycles since its last sign-off "
            f"(threshold {CONFIG['MAINTENANCE_CHARGE_CYCLES_THRESHOLD']})"
        )
    return "; ".join(reasons) if reasons else None
