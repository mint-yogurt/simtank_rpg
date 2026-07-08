"""In-memory enemy runtime state for simtank_rpg overworld.

DB rows (engine/worlddb.py enemies table) hold static config: name, stats,
sprite, behavior type. This module is the live layer: placement on screen load,
per-step movement update for each behavior type.

Behavior types:
  1 — wandering chaser: 65% step toward party, 35% random walkable step
  2 — pacer: walks H or V indefinitely, reversing at impassable tiles/edges
  3 — sentinel: frozen until party enters cardinal LOS (8 tiles), then chases
"""

import random
from dataclasses import dataclass, field

from engine.tiles import is_passable

_DIRS = [(-1, 0), (1, 0), (0, -1), (0, 1)]  # N S W E as (drow, dcol)


@dataclass
class EnemyAgent:
    index:         int
    row:           int
    col:           int
    behavior_type: int
    behavior_axis: str | None   # 'H' or 'V'; meaningful for type 2 only
    pace_dir:      int          # +1 or -1 along the pacing axis (type 2)
    activated:     bool         # type 3: True once LOS has triggered
    npc_sprite:    str
    name:          str
    # Combat stats (from DB — populated at placement so no DB re-lookup at battle time)
    iq:     int = 80
    weight: int = 200
    sweat:  int = 4
    hair:   int = 0
    level:  int = 1


# =============================================================================
# HELPERS
# =============================================================================

def _in_bounds(row, col, grid):
    return 0 <= row < len(grid) and 0 <= col < len(grid[0])


def _walkable(row, col, grid):
    return _in_bounds(row, col, grid) and is_passable(grid[row][col])


def _step_toward(from_row, from_col, to_row, to_col, grid):
    """One-tile step that reduces Manhattan distance to target.

    Tries the axis with the greater gap first; falls back to the other axis.
    Returns (new_row, new_col) or None if both directions are blocked.
    """
    dr = to_row - from_row
    dc = to_col - from_col

    # Build candidate steps ordered by which axis has larger gap
    steps = []
    if abs(dr) >= abs(dc):
        if dr != 0:
            steps.append((from_row + (1 if dr > 0 else -1), from_col))
        if dc != 0:
            steps.append((from_row, from_col + (1 if dc > 0 else -1)))
    else:
        if dc != 0:
            steps.append((from_row, from_col + (1 if dc > 0 else -1)))
        if dr != 0:
            steps.append((from_row + (1 if dr > 0 else -1), from_col))

    for nr, nc in steps:
        if _walkable(nr, nc, grid):
            return (nr, nc)
    return None


def _cardinal_los(er, ec, pr, pc, grid, max_dist=8):
    """True if enemy at (er,ec) has unobstructed cardinal sightline to party at (pr,pc).

    Requires: same row or same column, within max_dist tiles, no blocking tile
    between them (exclusive of the enemy's own tile; the party tile is the end).
    """
    if er == pr:
        dist = abs(ec - pc)
        if dist == 0 or dist > max_dist:
            return False
        step = 1 if pc > ec else -1
        for c in range(ec + step, pc, step):
            if not _walkable(er, c, grid):
                return False
        return True
    if ec == pc:
        dist = abs(er - pr)
        if dist == 0 or dist > max_dist:
            return False
        step = 1 if pr > er else -1
        for r in range(er + step, pr, step):
            if not _walkable(r, ec, grid):
                return False
        return True
    return False


def _random_step(row, col, grid, rng):
    """Random passable adjacent tile. Returns current position if all blocked."""
    options = [(row + dr, col + dc) for dr, dc in _DIRS
               if _walkable(row + dr, col + dc, grid)]
    return rng.choice(options) if options else (row, col)


# =============================================================================
# PLACEMENT
# =============================================================================

def place_enemies(db_rows, grid, seed):
    """Place enemies at deterministic random walkable positions.

    db_rows: list of dicts from db.list_enemies()
    grid:    2D tile grid for the current screen
    seed:    screen_seed (ensures same placement every visit)
    Returns: list[EnemyAgent]
    """
    if not db_rows:
        return []

    rng = random.Random(seed ^ 0xF00DCAFE)

    # Collect all walkable tiles, shuffle deterministically
    walkable = [
        (r, c)
        for r in range(len(grid))
        for c in range(len(grid[0]))
        if is_passable(grid[r][c])
    ]
    rng.shuffle(walkable)

    agents = []
    taken = set()
    for row_dict in db_rows:
        if not walkable:
            break
        # Pick a tile not already taken by another enemy
        pos = None
        for candidate in walkable:
            if candidate not in taken:
                pos = candidate
                break
        if pos is None:
            break
        taken.add(pos)

        btype = row_dict["behavior_type"]
        baxis = row_dict.get("behavior_axis")
        agents.append(EnemyAgent(
            index=row_dict["enemy_index"],
            row=pos[0],
            col=pos[1],
            behavior_type=btype,
            behavior_axis=baxis,
            pace_dir=1,
            activated=False,
            npc_sprite=row_dict["npc_sprite"],
            name=row_dict["name"],
            iq=row_dict.get("iq", 80),
            weight=row_dict.get("weight", 200),
            sweat=row_dict.get("sweat", 4),
            hair=row_dict.get("hair", 0),
            level=row_dict.get("level", 1),
        ))

    return agents


# =============================================================================
# PER-STEP MOVEMENT
# =============================================================================

def _step_type1(agent, grid, party_row, party_col, occupied, rng):
    if rng.random() < 0.65:
        candidate = _step_toward(agent.row, agent.col, party_row, party_col, grid)
        if candidate and candidate not in occupied:
            return candidate
    # random fallback
    options = [(agent.row + dr, agent.col + dc) for dr, dc in _DIRS
               if _walkable(agent.row + dr, agent.col + dc, grid)
               and (agent.row + dr, agent.col + dc) not in occupied]
    return rng.choice(options) if options else (agent.row, agent.col)


def _step_type2(agent, grid, occupied):
    """Pace along behavior_axis. Reverse on impassable/edge/occupied."""
    if agent.behavior_axis == 'H':
        dr, dc = 0, agent.pace_dir
    else:
        dr, dc = agent.pace_dir, 0

    nr, nc = agent.row + dr, agent.col + dc
    if _walkable(nr, nc, grid) and (nr, nc) not in occupied:
        return (nr, nc), agent.pace_dir

    # Try reversing
    nr2, nc2 = agent.row - dr, agent.col - dc
    if _walkable(nr2, nc2, grid) and (nr2, nc2) not in occupied:
        return (nr2, nc2), -agent.pace_dir

    return (agent.row, agent.col), agent.pace_dir


def _step_type3(agent, grid, party_row, party_col, occupied):
    """Frozen until LOS, then chase."""
    if not agent.activated:
        if _cardinal_los(agent.row, agent.col, party_row, party_col, grid, max_dist=8):
            agent.activated = True
    if not agent.activated:
        return (agent.row, agent.col)
    candidate = _step_toward(agent.row, agent.col, party_row, party_col, grid)
    if candidate and candidate not in occupied:
        return candidate
    return (agent.row, agent.col)


def update_enemies(agents, grid, party_row, party_col, rng):
    """Update all enemies one step. Modifies agents in place, returns the list.

    occupied is built from positions at the START of the tick so that all
    enemies move simultaneously rather than serially blocking each other.
    """
    occupied = {(a.row, a.col) for a in agents}

    for agent in agents:
        occupied.discard((agent.row, agent.col))

        if agent.behavior_type == 1:
            nr, nc = _step_type1(agent, grid, party_row, party_col, occupied, rng)
        elif agent.behavior_type == 2:
            (nr, nc), new_dir = _step_type2(agent, grid, occupied)
            agent.pace_dir = new_dir
        else:
            nr, nc = _step_type3(agent, grid, party_row, party_col, occupied)

        agent.row, agent.col = nr, nc
        occupied.add((nr, nc))

    return agents
