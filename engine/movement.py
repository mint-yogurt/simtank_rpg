"""Continuous cardinal-direction movement + collision -- pure logic, no pygame.

Shared by engine.enemy (which moves this way already) and engine.player
(which does too, now that player movement is free/continuous rather than
tile-discrete). A position is a (row, col) float pair in tile units -- the
value itself is the logical position, not a renderer-side visual tween.
Anything that needs a "which tile is this" answer for grid-keyed lookups
(interactions, warps, occupancy) rounds to the nearest tile itself; this
module only ever deals in continuous positions and 1-tile-square bounding
boxes -- exactly the size of the sprite drawn at that position, so the
hitbox never disagrees with what's on screen. (A continuously-moving
occupant squeezing through a 1-tile-wide gap doesn't get there by shrinking
this box below the sprite -- see engine.player.Player's grid-alignment
assist for how that's actually handled.)
"""

import math

_DIR_DELTA: dict[str, tuple[int, int]] = {
    "N": (-1,  0),
    "S": ( 1,  0),
    "E": ( 0,  1),
    "W": ( 0, -1),
}

OPPOSITE_DIR: dict[str, str] = {"N": "S", "S": "N", "E": "W", "W": "E"}

_EPS = 1e-6


def _covered_range(pos: float) -> range:
    """Integer tile indices the [pos, pos+1) span overlaps -- a bounding box
    is exactly 1 tile wide/tall, so this is at most 2 tiles per axis (when
    `pos` isn't grid-aligned)."""
    lo = math.floor(pos)
    hi = math.floor(pos + 1 - _EPS)
    return range(lo, hi + 1)


def _box_blocked(row: float, col: float, passable: list[list[bool]]) -> bool:
    rows = len(passable)
    cols = len(passable[0]) if rows else 0
    for r in _covered_range(row):
        for c in _covered_range(col):
            if not (0 <= r < rows and 0 <= c < cols) or not passable[r][c]:
                return True
    return False


def step_continuous(row: float, col: float, direction: str, dist: float,
                     passable: list[list[bool]]) -> tuple[float, float]:
    """Advance (row, col) by up to `dist` tiles along `direction`, hard-
    stopping at the boundary of the first non-walkable/out-of-bounds tile
    the bounding box would enter. Cardinal-only -- exactly one of row/col
    changes. Finds the stopping point by binary search on the fraction of
    the step that's still clear rather than hand-deriving the exact
    boundary per direction/edge -- simpler and just as exact in practice
    (20 halvings is far finer than a pixel over any one-frame distance)."""
    dr, dc = _DIR_DELTA[direction]
    new_row, new_col = row + dr * dist, col + dc * dist

    if not _box_blocked(new_row, new_col, passable):
        return new_row, new_col

    lo, hi = 0.0, dist
    for _ in range(20):
        mid = (lo + hi) / 2
        if _box_blocked(row + dr * mid, col + dc * mid, passable):
            hi = mid
        else:
            lo = mid
    return row + dr * lo, col + dc * lo
