"""The warehouse environment: a typed grid plus named zones.

The layout lives here as structured data, not in the HTML. The frontend renders
whatever this module reports, so changing the floor plan only means editing
``_build_layout``.
"""
from __future__ import annotations

from collections import deque
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

from .models import CONFIG, Cell, CellType, WALKABLE_CELLS, manhattan


class Zone:
    """A named, addressable area of the warehouse floor."""

    def __init__(self, key: str, label: str, cell_type: CellType, cells: Sequence[Cell]) -> None:
        self.key = key
        self.label = label
        self.cell_type = cell_type
        self.cells: List[Cell] = list(cells)

    @property
    def center(self) -> Cell:
        xs = [c[0] for c in self.cells]
        ys = [c[1] for c in self.cells]
        return (sum(xs) // len(xs), sum(ys) // len(ys))

    def to_dict(self) -> Dict[str, Any]:
        return {
            "key": self.key,
            "label": self.label,
            "type": self.cell_type.value,
            "cells": [{"x": x, "y": y} for x, y in self.cells],
            "center": {"x": self.center[0], "y": self.center[1]},
        }


def _rect(x0: int, x1: int, y0: int, y1: int) -> List[Cell]:
    return [(x, y) for y in range(y0, y1 + 1) for x in range(x0, x1 + 1)]


class Warehouse:
    def __init__(self, width: Optional[int] = None, height: Optional[int] = None) -> None:
        self.width = width or CONFIG["GRID_WIDTH"]
        self.height = height or CONFIG["GRID_HEIGHT"]
        self.grid: List[List[CellType]] = [
            [CellType.EMPTY for _ in range(self.width)] for _ in range(self.height)
        ]
        self.zones: Dict[str, Zone] = {}
        self.aliases: Dict[str, str] = {}
        self._build_layout()

    # ------------------------------------------------------------------ #
    # Layout
    # ------------------------------------------------------------------ #
    def _fill(self, cells: Iterable[Cell], cell_type: CellType) -> None:
        for x, y in cells:
            if self.is_inside(x, y):
                self.grid[y][x] = cell_type

    def _add_zone(self, key: str, label: str, cell_type: CellType, cells: Sequence[Cell]) -> Zone:
        zone = Zone(key, label, cell_type, cells)
        self.zones[key] = zone
        self.aliases[key] = key
        self.aliases[label.lower()] = key
        self.aliases[label.lower().replace("-", " ")] = key
        self.aliases[label.lower().replace("-", "_")] = key
        return zone

    def _build_layout(self) -> None:
        w, h = self.width, self.height

        # Perimeter walls.
        self._fill([(x, 0) for x in range(w)], CellType.WALL)
        self._fill([(x, h - 1) for x in range(w)], CellType.WALL)
        self._fill([(0, y) for y in range(h)], CellType.WALL)
        self._fill([(w - 1, y) for y in range(h)], CellType.WALL)

        # Racking. Each shelf block is solid; the aisle row below it is the
        # pick face where boxes actually sit and robots can drive.
        racks = [
            ("shelf_a", "Shelf-A", 3, 6),
            ("shelf_b", "Shelf-B", 9, 12),
            ("shelf_c", "Shelf-C", 15, 17),
        ]
        for key, label, x0, x1 in racks:
            self._fill(_rect(x0, x1, 3, 4), CellType.SHELF)
            face = _rect(x0, x1, 5, 5)
            self._fill(face, CellType.STORAGE)
            self._add_zone(key, label, CellType.STORAGE, face)
            # Storage-A / Storage-B / Storage-C address the same pick faces.
            self.aliases[key.replace("shelf", "storage")] = key
            self.aliases[label.lower().replace("shelf", "storage")] = key

        # Internal partitions, leaving deliberate gaps for aisles.
        self._fill([(5, 9), (6, 9), (13, 9), (14, 9)], CellType.WALL)

        # No-go area around the electrical cabinet.
        self._fill(_rect(1, 2, 3, 5), CellType.RESTRICTED)
        self._add_zone("restricted_area", "Restricted-Area", CellType.RESTRICTED, _rect(1, 2, 3, 5))

        # Service areas.
        charging = _rect(1, 3, 12, 13)
        self._fill(charging, CellType.CHARGING)
        self._add_zone("charging_station", "Charging-Station", CellType.CHARGING, charging)

        parking = _rect(1, 2, 8, 9)
        self._fill(parking, CellType.PARKING)
        self._add_zone("parking_area", "Parking-Area", CellType.PARKING, parking)

        packing = _rect(7, 10, 11, 13)
        self._fill(packing, CellType.PACKING)
        self._add_zone("packing_area", "Packing-Area", CellType.PACKING, packing)

        unloading = _rect(12, 13, 12, 13)
        self._fill(unloading, CellType.UNLOADING)
        self._add_zone("unloading_zone", "Unloading-Zone", CellType.UNLOADING, unloading)

        loading = _rect(16, 18, 11, 13)
        self._fill(loading, CellType.LOADING)
        self._add_zone("loading_zone", "Loading-Zone", CellType.LOADING, loading)

    # ------------------------------------------------------------------ #
    # Queries
    # ------------------------------------------------------------------ #
    def is_inside(self, x: int, y: int) -> bool:
        return 0 <= x < self.width and 0 <= y < self.height

    def cell_type(self, x: int, y: int) -> CellType:
        if not self.is_inside(x, y):
            return CellType.WALL
        return self.grid[y][x]

    def is_walkable(self, x: int, y: int) -> bool:
        return self.cell_type(x, y) in WALKABLE_CELLS

    def walkable_cells(self) -> List[Cell]:
        return [
            (x, y)
            for y in range(self.height)
            for x in range(self.width)
            if self.is_walkable(x, y)
        ]

    def neighbors(self, cell: Cell) -> List[Cell]:
        x, y = cell
        return [
            (nx, ny)
            for nx, ny in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1))
            if self.is_walkable(nx, ny)
        ]

    def resolve_zone(self, name: Optional[str]) -> Optional[Zone]:
        if not name:
            return None
        key = self.aliases.get(str(name).strip().lower())
        if key is None:
            key = self.aliases.get(str(name).strip().lower().replace(" ", "_"))
        return self.zones.get(key) if key else None

    def zone_of_cell(self, cell: Cell) -> Optional[Zone]:
        for zone in self.zones.values():
            if cell in zone.cells:
                return zone
        return None

    def label_for_cell(self, cell: Cell) -> str:
        zone = self.zone_of_cell(cell)
        if zone:
            return zone.label
        return f"({cell[0]},{cell[1]})"

    def nearest_walkable(self, cell: Cell, blocked: Optional[Set[Cell]] = None) -> Optional[Cell]:
        """Breadth-first search outwards for the closest drivable cell."""
        blocked = blocked or set()
        if self.is_walkable(*cell) and cell not in blocked:
            return cell
        seen = {cell}
        frontier: deque = deque([cell])
        while frontier:
            current = frontier.popleft()
            x, y = current
            for nxt in ((x + 1, y), (x - 1, y), (x, y + 1), (x, y - 1)):
                if nxt in seen or not self.is_inside(*nxt):
                    continue
                seen.add(nxt)
                if self.is_walkable(*nxt) and nxt not in blocked:
                    return nxt
                frontier.append(nxt)
        return None

    def zone_cells_sorted(self, zone: Zone, origin: Cell) -> List[Cell]:
        return sorted(zone.cells, key=lambda c: manhattan(c, origin))

    # ------------------------------------------------------------------ #
    # Serialisation
    # ------------------------------------------------------------------ #
    def to_dict(self) -> Dict[str, Any]:
        cells = [
            {"x": x, "y": y, "type": self.grid[y][x].value}
            for y in range(self.height)
            for x in range(self.width)
            if self.grid[y][x] is not CellType.EMPTY
        ]
        return {
            "width": self.width,
            "height": self.height,
            "cells": cells,
            "zones": [zone.to_dict() for zone in self.zones.values()],
            "walkable_types": sorted(t.value for t in WALKABLE_CELLS),
        }

    # Locations a user can pick as a task source or destination.
    def location_options(self) -> List[Dict[str, str]]:
        return [
            {"key": zone.key, "label": zone.label}
            for zone in self.zones.values()
            if zone.cell_type is not CellType.RESTRICTED
        ]
