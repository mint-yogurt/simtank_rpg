"""Party position and single-screen scripted movement for simtank_rpg.

PartyPos tracks where the party is (which screen + which tile).
execute_move steps through a direction+count plan one tile at a time,
scanning before each step and stopping early on blockers, screen edges,
or arrival at an enterable feature tile.

When a `generator` is supplied, reaching a screen edge triggers a seamless
crossing: the adjacent screen is generated/cached via enter_screen(), the
party appears at the mirrored entry tile, and the remaining steps continue
on the new screen. The returned MoveResult.final_grid is always the grid
the party ended on (same object as input when no crossing occurred).

No display, no LLM, no RNG — pure engine data layer.

Coordinate convention: row-first throughout.
  grid[row][col]
  scan(grid, row, col)
  set_feature_state(..., row, col, ...)
  get_feature(..., row, col)
"""

import json
from dataclasses import dataclass, field

from engine.journal import journals_append
from engine.tiles import is_enterable
from engine.viewscan import scan


_DIR_DELTAS = {'N': (0, -1), 'S': (0, 1), 'E': (1, 0), 'W': (-1, 0)}

# Screen-coordinate delta when crossing in each direction.
# sx = screen column, sy = screen row (N decreases sy, S increases sy).
# All screens assumed same dimensions (fixed by SCREEN_CONFIGS).
_SCREEN_DIR_DELTA = {'N': (0, -1), 'S': (0, 1), 'E': (1, 0), 'W': (-1, 0)}


@dataclass
class PartyPos:
    world_seed: int
    sx: int     # screen x (column of screens)
    sy: int     # screen y (row of screens)
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
    note: str           # 'moved' | 'stopped_blocker' | 'stopped_edge'
                        # | 'arrived_enterable(<tile>)'
                        # | 'crossed_<dir>_to(<sx>,<sy>)'


@dataclass
class MoveResult:
    steps_taken: int    # tiles actually traversed (crossing counts as 1 step)
    stop_reason: str    # 'completed' | 'blocker' | 'edge' | 'enterable'
    log: list = field(default_factory=list)   # list[StepRecord]
    final_grid: list | None = None            # grid the party ended on;
                                              # same object as input if no crossing,
                                              # new screen's grid if crossed


def enter_screen(pos: PartyPos, db, generator, tick: int = 0) -> dict:
    """Ensure the screen is generated/cached, mark it visited, return screen row dict.

    Call this whenever the party arrives on a new screen (including game start).
    The returned dict contains 'grid_json' (and other columns) for the screen.
    """
    screen = db.get_or_create_screen(pos.world_seed, pos.sx, pos.sy, generator)
    db.mark_visited(pos.world_seed, pos.sx, pos.sy, tick)
    return screen


def execute_move(pos: PartyPos, direction: str, steps: int, grid: list, db,
                 journals: dict | None = None, tick: int = 0,
                 generator=None) -> MoveResult:
    """Execute a scripted move plan, handling screen crossings when generator is given.

    Args:
        pos:       Party position (mutated in place to reflect final position).
        direction: 'N' | 'S' | 'E' | 'W'
        steps:     Maximum number of tiles to advance (crossing counts as 1 step).
        grid:      2D list of tile-name strings for the current screen.
        db:        WorldDB — used to mark features entered and, on crossing, to
                   cache the adjacent screen. Pass None to skip both.
        journals:  Per-member MemberJournal dict; None-safe.
        tick:      Current game tick (attached to journal entries).
        generator: callable(world_seed, sx, sy) → ScreenData.
                   When provided, reaching a screen edge triggers a seamless
                   crossing to the adjacent screen instead of stopping.

    Returns:
        MoveResult with the step log, stop reason, and final_grid.

    Stop conditions (in priority order each step):
      1. Adjacent tile is not passable (blocker or off-screen with no generator) → stop.
      2. Off-screen with generator → cross to adjacent screen; continue stepping.
      3. Plan step count reached → 'completed'.
      4. Party lands on an enterable tile → mark entered in DB, stop.

    Journal events emitted:
      MOVE   — one entry at the end (total steps, stop reason).
      SCREEN — one entry per screen crossing.
      ENTERED — emitted alongside MOVE when landing on an enterable tile.
    """
    dc, dr = _DIR_DELTAS[direction]
    log = []
    steps_taken = 0
    current_grid = grid   # updated in-place on crossing; final value goes to MoveResult

    for i in range(steps):
        before_col, before_row = pos.col, pos.row
        vs = scan(current_grid, pos.row, pos.col)
        dir_scan = getattr(vs, direction)

        if not dir_scan.adjacent_passable:
            if dir_scan.kind == 'edge' and generator is not None:
                # ── Screen crossing ──────────────────────────────────────────
                dsx, dsy = _SCREEN_DIR_DELTA[direction]
                pos.sx += dsx
                pos.sy += dsy

                new_screen = enter_screen(pos, db, generator, tick)
                current_grid = json.loads(new_screen['grid_json'])
                new_rows = len(current_grid)
                new_cols = len(current_grid[0]) if current_grid else 0

                # Entry tile mirrors the exit position.
                # Use before_row/before_col (edge tile, not yet mutated).
                # Assumes all screens share the same dimensions (fixed config).
                if direction == 'N':
                    pos.row, pos.col = new_rows - 1, before_col
                elif direction == 'S':
                    pos.row, pos.col = 0, before_col
                elif direction == 'E':
                    pos.row, pos.col = before_row, 0
                elif direction == 'W':
                    pos.row, pos.col = before_row, new_cols - 1

                steps_taken += 1
                journals_append(journals, tick, 'SCREEN',
                                f"crossed {direction} → screen ({pos.sx},{pos.sy})")
                log.append(StepRecord(
                    step_num=i + 1,
                    before_col=before_col, before_row=before_row,
                    scan=vs,
                    after_col=pos.col, after_row=pos.row,
                    note=f'crossed_{direction}_to({pos.sx},{pos.sy})',
                ))

                # Entry tile might itself be enterable — handle like a normal step.
                entry_tile = (current_grid[pos.row][pos.col] or 'grass1').split(':')[0]
                if is_enterable(entry_tile):
                    if db is not None:
                        db.set_feature_state(
                            pos.world_seed, pos.sx, pos.sy, pos.row, pos.col,
                            entered=True)
                    journals_append(journals, tick, 'MOVE',
                                    f"moved {direction} {steps_taken} → enterable")
                    journals_append(journals, tick, 'ENTERED', f"entered {entry_tile}")
                    return MoveResult(steps_taken=steps_taken, stop_reason='enterable',
                                      log=log, final_grid=current_grid)
                continue  # resume loop on the new screen

            else:
                # ── Hard stop: blocker or edge with no generator ──────────────
                reason = 'edge' if dir_scan.kind == 'edge' else 'blocker'
                log.append(StepRecord(
                    step_num=i + 1,
                    before_col=before_col, before_row=before_row,
                    scan=vs,
                    after_col=pos.col, after_row=pos.row,
                    note=f'stopped_{reason}',
                ))
                move_desc = (f"blocked {direction}" if steps_taken == 0
                             else f"moved {direction} {steps_taken} → {reason}")
                journals_append(journals, tick, 'MOVE', move_desc)
                return MoveResult(steps_taken=steps_taken, stop_reason=reason,
                                  log=log, final_grid=current_grid)

        pos.col += dc
        pos.row += dr
        steps_taken += 1

        tile = (current_grid[pos.row][pos.col] or 'grass1').split(':')[0]
        if is_enterable(tile):
            if db is not None:
                db.set_feature_state(
                    pos.world_seed, pos.sx, pos.sy, pos.row, pos.col, entered=True)
            log.append(StepRecord(
                step_num=i + 1,
                before_col=before_col, before_row=before_row,
                scan=vs,
                after_col=pos.col, after_row=pos.row,
                note=f'arrived_enterable({tile})',
            ))
            journals_append(journals, tick, 'MOVE',
                            f"moved {direction} {steps_taken} → enterable")
            journals_append(journals, tick, 'ENTERED', f"entered {tile}")
            return MoveResult(steps_taken=steps_taken, stop_reason='enterable',
                              log=log, final_grid=current_grid)

        log.append(StepRecord(
            step_num=i + 1,
            before_col=before_col, before_row=before_row,
            scan=vs,
            after_col=pos.col, after_row=pos.row,
            note='moved',
        ))

    journals_append(journals, tick, 'MOVE',
                    f"moved {direction} {steps_taken} → completed")
    return MoveResult(steps_taken=steps_taken, stop_reason='completed',
                      log=log, final_grid=current_grid)
