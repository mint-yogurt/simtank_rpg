"""Map YAML loader.

Maps are the authoritative source for static layout — tile grid, NPC spawn
points, warp tiles, palette.  The DB stores only mutable runtime state that
changes during play (chest opened, NPC flag, etc.).

Loading a map never writes its tile layout to the DB.  This means editing a
YAML and reloading gives you the updated map immediately — essential for the
map editor workflow.

# Warp / entry / exit tile system
# ─────────────────────────────────
# A "warp" is any tile that, when stepped on, transitions the player to a
# different map.  The YAML declares WHAT to warp to; the runtime decides WHERE
# to return.
#
# The engine keeps a warp stack: when the player enters a warp, the engine
# pushes (current_map_path, current_tile_row, current_tile_col) onto the stack.
# When the player steps on an exit warp (target == "__return__"), the engine
# pops the stack and places the player back at the saved tile.
#
# This handles arbitrary nesting at zero YAML cost:
#
#   overworld → [enter town]   push (overworld, row, col)
#   town      → [enter house]  push (town, row, col)
#   house     → [exit]         pop  → lands at town tile they entered from
#   town      → [exit]         pop  → lands at overworld tile they entered from
#
# The key insight: the YAML never needs to know where it was entered from.
# The map just says "this tile exits" and the stack handles the rest.
#
# Special target values in warps:
#   "__return__"  — pop the warp stack (standard exit)
#   "maps/foo.yaml"  — push current position and load foo.yaml at its spawn
#
# Inter-map entry points (e.g. which tile inside a town to spawn at when
# entering from the overworld) are handled by the target map's `spawn` field,
# which can be overridden per-warp if needed.
"""

from __future__ import annotations

import re
from dataclasses import dataclass, field
from pathlib import Path

import yaml


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class NpcPlacement:
    """Where an NPC spawns on this map and how it behaves.

    `npc_id` references a future NPC definition file (data/npcs/*.yaml).
    For now it's also usable as a sprite key directly.
    """
    npc_id:    str
    row:       int
    col:       int
    behavior:  str = "static"      # "static" | "wander" | "pace"
    facing:    str = "S"
    dialogue:  str | None = None   # dialogue tree ID; None = no interaction


@dataclass
class WarpPoint:
    """A tile on this map that transitions the player to another map.

    See module docstring for the full warp stack design.
    """
    row:    int
    col:    int
    target: str          # map path relative to repo root, or "__return__"
    # Spawn override: which tile to land on in the target map.
    # None means use the target map's default spawn point.
    target_row: int | None = None
    target_col: int | None = None
    label:  str = ""     # human-readable, e.g. "Town Gate", "House A Door"


@dataclass
class MapData:
    """Everything loaded from a map YAML file.

    `tile_grid` is a 2-D list of tile name strings (same format the engine and
    canvas renderer already use).  Each cell is either a plain string or a
    two-element list [ground_tile, overlay_tile] for transparent overlays.
    """
    # Identity
    name:      str
    source:    Path                     # absolute path of the YAML that was loaded

    # Rendering
    tileset:   str                      # key: "town" | "cave" | "overworld" | ...
    palette:   list[list[int]] | None   # list of [r,g,b] triples; None = default

    # Layout
    tile_grid: list[list]               # 2-D, tile name strings (or [ground, overlay])
    rows:      int
    cols:      int

    # Player start position for this map (used when warping in without an override)
    spawn:     tuple[int, int]          # (row, col)

    # Content
    npcs:      list[NpcPlacement] = field(default_factory=list)
    warps:     list[WarpPoint]    = field(default_factory=list)

    # Freeform metadata the engine or editor can read
    meta:      dict               = field(default_factory=dict)


# ── Loader ────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent


def load_map(path: str | Path) -> MapData:
    """Load a map YAML and return a MapData.

    `path` can be absolute or relative to the repo root.
    Raises FileNotFoundError or yaml.YAMLError on bad input.
    """
    path = Path(path)
    if not path.is_absolute():
        path = _REPO_ROOT / path

    with open(path) as f:
        raw = yaml.safe_load(f)

    # ── tile grid ─────────────────────────────────────────────────────────────
    grid = raw["tiles"]
    rows = len(grid)
    cols = max((len(r) for r in grid), default=0)

    # ── spawn ─────────────────────────────────────────────────────────────────
    sp = raw.get("spawn", [rows - 1, 0])
    spawn = (int(sp[0]), int(sp[1]))

    # ── NPCs ──────────────────────────────────────────────────────────────────
    npcs = [
        NpcPlacement(
            npc_id   = n["npc_id"],
            row      = int(n["row"]),
            col      = int(n["col"]),
            behavior = n.get("behavior", "static"),
            facing   = n.get("facing", "S"),
            dialogue = n.get("dialogue"),
        )
        for n in raw.get("npcs", [])
    ]

    # ── warps ─────────────────────────────────────────────────────────────────
    warps = [
        WarpPoint(
            row        = int(w["row"]),
            col        = int(w["col"]),
            target     = w["target"],
            target_row = w.get("target_row"),
            target_col = w.get("target_col"),
            label      = w.get("label", ""),
        )
        for w in raw.get("warps", [])
    ]

    # ── palette ───────────────────────────────────────────────────────────────
    palette = raw.get("palette")   # None or list of [r,g,b]

    return MapData(
        name      = raw.get("name", path.stem),
        source    = path,
        tileset   = raw["tileset"],
        palette   = palette,
        tile_grid = grid,
        rows      = rows,
        cols      = cols,
        spawn     = spawn,
        npcs      = npcs,
        warps     = warps,
        meta      = raw.get("meta", {}),
    )


# ── CSV + tilerules loader (current hub map format) ────────────────────────────
#
# The hub map isn't authored as YAML yet — it's a raw CSV of tileset-sheet
# coordinates plus a tilerules text file mapping those coordinates to tile
# names. This is the format actually in use today; the YAML loader above is
# for future authored maps and isn't wired into any scene yet.

_HUB_CSV    = _REPO_ROOT / 'assets' / 'tiles' / 'hub_map_coords_bracketed.csv'
_TOWN_RULES = _REPO_ROOT / 'assets' / 'tiles' / 'tiles_town_rules.txt'


def _parse_coord_to_name(rules_path: Path) -> dict:
    """Build {(tileset_col, tileset_row): (tilename, is_transparentbg)} from town tilerules."""
    result = {}
    with open(rules_path) as f:
        for line in f:
            line = line.strip()
            if not line or '=' not in line:
                continue
            eq = line.index('=')
            key = line[:eq].strip()
            rest = line[eq + 1:]
            if '#' in rest:
                rest = rest[:rest.index('#')]
            try:
                col_str, row_str = key.split(',')
                tc, tr = int(col_str.strip()), int(row_str.strip())
            except ValueError:
                continue
            parts = [p.strip().rstrip('_').replace(' ', '') for p in rest.split(',')]
            name = parts[0]
            if name:
                transparent = 'transparentbg' in parts
                result[(tc, tr)] = (name, transparent)
    return result


def load_hub_coords() -> list[list[tuple[int, int]]]:
    """Return raw tile-sheet coords: grid[row][col] = (tileset_col, tileset_row)."""
    coords: list[list[tuple[int, int]]] = []
    with open(_HUB_CSV) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row: list[tuple[int, int]] = []
            for m in re.finditer(r'\[(\d+),(\d+)\]', line):
                row.append((int(m.group(1)), int(m.group(2))))
            if row:
                coords.append(row)
    return coords


def load_hub_grid() -> list[list]:
    """Parse hub CSV into grid[row][col] of tile name strings.

    Transparentbg tiles are returned as [ground, overlay] so the renderer
    composites them over grass. Unknown coords are skipped (render nothing)
    rather than replaced with grass.
    """
    coord_to_name = _parse_coord_to_name(_TOWN_RULES)
    coords = load_hub_coords()
    grid: list[list] = []
    unknowns: list[tuple[int, int]] = []
    for coord_row in coords:
        row: list = []
        for (tc, tr) in coord_row:
            entry = coord_to_name.get((tc, tr))
            if entry:
                name, transparent = entry
                row.append(['grass1', name] if transparent else name)
            else:
                row.append('grass1')
                unknowns.append((tc, tr))
        grid.append(row)
    if unknowns:
        unique = sorted(set(unknowns))
        print(f"  [map_loader] unknown hub tile coords (no label in rules): {unique}", flush=True)
    return grid


def hub_str_grid(grid: list) -> list[list[str]]:
    """Return a string-only grid suitable for passability checks.

    Hub cells can be lists ['grass1', 'overlay_name'] for transparent tiles.
    The overlay (last element) determines passability, not the ground beneath.
    """
    result = []
    for row in grid:
        r = []
        for cell in row:
            r.append(cell[-1] if isinstance(cell, list) else cell)
        result.append(r)
    return result


def hub_spawn_point(grid: list) -> tuple[int, int]:
    """Player spawn tile: a few rows up from the bottom edge, centered.

    Keeping the spawn off the bottom edge avoids an immediate map-edge
    collision on the very first step if the player moves south.
    """
    bottom_row = len(grid) - 1
    spawn_row = max(bottom_row - 4, 0)
    cols = len(grid[0]) if grid else 16
    return (spawn_row, cols // 2)
