"""Party position and single-screen scripted movement for simtank_rpg.

PartyPos tracks where the party is (which screen + which tile).
execute_move steps through a direction+count plan one tile at a time,
scanning before each step and stopping early on blockers, screen edges,
or arrival at an enterable feature tile.

No display, no LLM, no RNG — pure engine data layer.

Coordinate convention: row-first throughout.
  grid[row][col]
  scan(grid, row, col)
  set_feature_state(..., row, col, ...)
  get_feature(..., row, col)
"""

import json
from dataclasses import dataclass, field

from engine.tiles import is_enterable
from engine.viewscan import scan


_DIR_DELTAS = {'N': (0, -1), 'S': (0, 1), 'E': (1, 0), 'W': (-1, 0)}


@dataclass
class PartyPos:
    world_seed: int
    sx: int     # screen x (grid of screens)
    sy: int     # screen y (grid of screens)
    col: int    # tile column within current screen
    row: int    # tile row within current screen


@dataclass
class StepRecord:
    step_num: int       # 1-indexed iteration counter
    before_col: int     # party col before this iteration
    before_row: int     # party row before this iteration
    scan: object        # ViewScan at (before_col, before_row)
    after_col: int      # party col after (equals before if stopped)
    after_row: int      # party row after (equals before if stopped)
    note: str           # 'moved' | 'stopped_blocker' | 'stopped_edge' | 'arrived_enterable(<tile>)'


@dataclass
class MoveResult:
    steps_taken: int    # tiles actually traversed
    stop_reason: str    # 'completed' | 'blocker' | 'edge' | 'enterable'
    log: list = field(default_factory=list)  # list[StepRecord]


def enter_screen(pos: PartyPos, db, generator, tick: int = 0) -> dict:
    """Ensure the screen is generated/cached, mark it visited, return screen row dict.

    Call this whenever the party arrives on a new screen (including game start).
    The returned dict contains 'grid_json' (and other columns) for the screen.
    """
    screen = db.get_or_create_screen(pos.world_seed, pos.sx, pos.sy, generator)
    db.mark_visited(pos.world_seed, pos.sx, pos.sy, tick)
    return screen


def execute_move(pos: PartyPos, direction: str, steps: int, grid: list, db) -> MoveResult:
    """Execute a scripted single-screen move plan, scanning before each step.

    Args:
        pos:       Party position (mutated in place to reflect final position).
        direction: 'N' | 'S' | 'E' | 'W'
        steps:     Maximum number of tiles to advance.
        grid:      2D list of tile-name strings, loaded from screen['grid_json'].
                   Caller is responsible for loading this (e.g. via enter_screen).
        db:        WorldDB — used only to mark features as entered when the party
                   lands on an enterable tile. Pass None to skip feature marking.

    Returns:
        MoveResult with the step log and stop reason.

    Stop conditions (in priority order each step):
      1. Adjacent tile in direction is not passable (blocker or screen edge) → stop.
      2. Plan step count reached → 'completed'.
      3. Party lands on an enterable tile → mark entered in DB, stop.
    """
    dc, dr = _DIR_DELTAS[direction]
    log = []
    steps_taken = 0

    for i in range(steps):
        before_col, before_row = pos.col, pos.row
        vs = scan(grid, pos.row, pos.col)
        dir_scan = getattr(vs, direction)

        if not dir_scan.adjacent_passable:
            reason = 'edge' if dir_scan.kind == 'edge' else 'blocker'
            note = f'stopped_{reason}'
            log.append(StepRecord(
                step_num=i + 1,
                before_col=before_col, before_row=before_row,
                scan=vs,
                after_col=pos.col, after_row=pos.row,
                note=note,
            ))
            return MoveResult(steps_taken=steps_taken, stop_reason=reason, log=log)

        pos.col += dc
        pos.row += dr
        steps_taken += 1

        tile = (grid[pos.row][pos.col] or 'grass1').split(':')[0]
        if is_enterable(tile):
            if db is not None:
                db.set_feature_state(
                    pos.world_seed, pos.sx, pos.sy, pos.row, pos.col, entered=True
                )
            log.append(StepRecord(
                step_num=i + 1,
                before_col=before_col, before_row=before_row,
                scan=vs,
                after_col=pos.col, after_row=pos.row,
                note=f'arrived_enterable({tile})',
            ))
            return MoveResult(steps_taken=steps_taken, stop_reason='enterable', log=log)

        log.append(StepRecord(
            step_num=i + 1,
            before_col=before_col, before_row=before_row,
            scan=vs,
            after_col=pos.col, after_row=pos.row,
            note='moved',
        ))

    return MoveResult(steps_taken=steps_taken, stop_reason='completed', log=log)
