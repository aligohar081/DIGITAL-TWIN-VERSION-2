"""Navigation: A* pathfinding over the warehouse grid.

The engine is deliberately stateless — it takes the static grid plus a set of
dynamic obstacles (other robots, temporary blockages) and returns a path. That
makes replanning mid-mission a plain function call.
"""
from __future__ import annotations

import heapq
from typing import Dict, Iterable, List, Optional, Set, Tuple

from .models import Cell, manhattan
from .warehouse import Warehouse


class NavigationEngine:
    def __init__(self, warehouse: Warehouse) -> None:
        self.warehouse = warehouse
        self.paths_computed = 0
        self.replans = 0
        self.failures = 0

    # ------------------------------------------------------------------ #
    # A*
    # ------------------------------------------------------------------ #
    def find_path(
        self,
        start: Cell,
        goal: Cell,
        blocked: Optional[Iterable[Cell]] = None,
        allow_goal_adjacent: bool = True,
        is_replan: bool = False,
    ) -> Optional[List[Cell]]:
        """Return the cells to traverse from ``start`` to ``goal``, exclusive of
        ``start``. ``None`` means no route exists.
        """
        blocked_set: Set[Cell] = set(blocked or ())
        blocked_set.discard(start)

        target = goal
        if not self.warehouse.is_walkable(*goal) or goal in blocked_set:
            if not allow_goal_adjacent:
                self.failures += 1
                return None
            replacement = self.warehouse.nearest_walkable(goal, blocked_set)
            if replacement is None:
                self.failures += 1
                return None
            target = replacement

        if start == target:
            self.paths_computed += 1
            return []

        open_heap: List[Tuple[int, int, Cell]] = [(manhattan(start, target), 0, start)]
        came_from: Dict[Cell, Optional[Cell]] = {start: None}
        g_score: Dict[Cell, int] = {start: 0}
        closed: Set[Cell] = set()

        while open_heap:
            _, cost, current = heapq.heappop(open_heap)
            if current in closed:
                continue
            closed.add(current)
            if current == target:
                path = self._reconstruct(came_from, current)
                self.paths_computed += 1
                if is_replan:
                    self.replans += 1
                return path
            for neighbor in self.warehouse.neighbors(current):
                if neighbor in blocked_set or neighbor in closed:
                    continue
                tentative = cost + 1
                if tentative < g_score.get(neighbor, 1 << 30):
                    g_score[neighbor] = tentative
                    came_from[neighbor] = current
                    heapq.heappush(
                        open_heap,
                        (tentative + manhattan(neighbor, target), tentative, neighbor),
                    )
        self.failures += 1
        return None

    @staticmethod
    def _reconstruct(came_from: Dict[Cell, Optional[Cell]], end: Cell) -> List[Cell]:
        path: List[Cell] = []
        node: Optional[Cell] = end
        while node is not None:
            path.append(node)
            node = came_from[node]
        path.reverse()
        return path[1:]  # drop the start cell

    # ------------------------------------------------------------------ #
    # Helpers
    # ------------------------------------------------------------------ #
    def path_exists(self, start: Cell, goal: Cell, blocked: Optional[Iterable[Cell]] = None) -> bool:
        return self.find_path(start, goal, blocked=blocked) is not None

    def distance(
        self, start: Cell, goal: Cell, blocked: Optional[Iterable[Cell]] = None
    ) -> Optional[int]:
        path = self.find_path(start, goal, blocked=blocked)
        return None if path is None else len(path)

    def best_cell_in_zone(
        self,
        zone_cells: Iterable[Cell],
        origin: Cell,
        blocked: Optional[Iterable[Cell]] = None,
        prefer: Optional[Set[Cell]] = None,
    ) -> Optional[Cell]:
        """Pick the reachable cell of a zone that is cheapest to drive to.

        ``prefer`` cells are considered first (used to avoid stacking two boxes
        on the same drop point).
        """
        blocked_set = set(blocked or ())
        candidates = list(zone_cells)
        if prefer:
            preferred = [c for c in candidates if c in prefer]
            if preferred:
                candidates = preferred
        candidates.sort(key=lambda c: manhattan(c, origin))
        for cell in candidates:
            if cell in blocked_set:
                continue
            if cell == origin or self.find_path(origin, cell, blocked=blocked_set,
                                                allow_goal_adjacent=False) is not None:
                return cell
        return None

    def stats(self) -> Dict[str, int]:
        return {
            "paths_computed": self.paths_computed,
            "replans": self.replans,
            "failures": self.failures,
        }
