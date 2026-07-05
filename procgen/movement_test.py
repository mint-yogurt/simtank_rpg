"""Movement test for engine/party_state.py.

Tests single-screen scripted movement: scan-as-you-walk, blocker stop,
edge stop, enterable arrival, visited + entered DB state.

Run from the repo root:
    python procgen/movement_test.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.worlddb import WorldDB
from engine.party_state import PartyPos, enter_screen, execute_move
from procgen.overworld_test import ScreenData

WORLD_SEED = 77777


# ─── synthetic screen factory ────────────────────────────────────────────────

def _make_generator(grid, feature_cells=None):
    """Wrap a synthetic grid in a callable compatible with WorldDB.get_or_create_screen."""
    fc = feature_cells or {}
    def _gen(world_seed, sx, sy):
        return ScreenData(
            world_seed=world_seed, sx=sx, sy=sy,
            screen_seed=0,
            rows=len(grid), cols=len(grid[0]),
            grid=grid,
            feature_cells=fc,
            blob_cells=set(),
            palette=((0, 128, 0), (0, 0, 128), (128, 64, 0)),
            log=[],
        )
    return _gen


# ─── print helpers ───────────────────────────────────────────────────────────

_KIND_CHAR = {'edge': 'G', 'blocker': 'B', 'enterable': 'X'}  # G=going-off-Grid

def _scan_summary(vs, direction):
    """One-line scan summary showing all four rays.

    Kind chars: G=edge/off-grid  B=blocker  X=enterable
    adj: ok=passable step available  no=blocked/edge
    """
    parts = []
    for d in ('N', 'S', 'E', 'W'):
        ds = getattr(vs, d)
        adj = 'ok' if ds.adjacent_passable else 'no'
        marker = '<' if d == direction else ' '
        kc = _KIND_CHAR.get(ds.kind, '?')
        parts.append(f"{marker}{d}:{kc}{ds.distance}({adj})")
    return '  '.join(parts)


def _print_step(rec, direction):
    scan_str = _scan_summary(rec.scan, direction)
    print(f"    step {rec.step_num}: ({rec.before_col},{rec.before_row}) → "
          f"({rec.after_col},{rec.after_row})  {scan_str}  [{rec.note}]")


def _print_result(result, direction):
    for rec in result.log:
        _print_step(rec, direction)
    print(f"  → stop_reason={result.stop_reason!r}  steps_taken={result.steps_taken}")


# ─── Test 1: clear path ──────────────────────────────────────────────────────
#
# 6×3 all-grass grid, party at (col=1, row=5), plan N×4.
# Expected: all 4 steps complete, party ends at (col=1, row=1).

def test_clear_path():
    print("\n─── Test 1: clear path (N×4) ───────────────────────────────────")
    grid = [['grass1', 'grass1', 'grass1']] * 6  # 6 rows × 3 cols
    gen = _make_generator(grid)
    db = WorldDB(':memory:')

    pos = PartyPos(world_seed=WORLD_SEED, sx=0, sy=0, col=1, row=5)
    screen = enter_screen(pos, db, gen, tick=1)
    loaded_grid = json.loads(screen['grid_json'])

    print(f"  screen {pos.sx},{pos.sy}  start=(col={pos.col},row={pos.row})  "
          f"visited={screen['visited']}")

    result = execute_move(pos, 'N', 4, loaded_grid, db)
    _print_result(result, 'N')

    # Visited is set by enter_screen
    s = db.get_or_create_screen(WORLD_SEED, 0, 0, gen)
    assert s['visited'] == 1, f"expected visited=1, got {s['visited']}"

    assert result.stop_reason == 'completed', \
        f"expected 'completed', got {result.stop_reason!r}"
    assert result.steps_taken == 4, \
        f"expected 4 steps, got {result.steps_taken}"
    assert pos.col == 1 and pos.row == 1, \
        f"expected (col=1,row=1), got (col={pos.col},row={pos.row})"
    assert len([r for r in result.log if r.note == 'moved']) == 4

    db.close()
    print("PASS: clear path")


# ─── Test 2: blocker partway ─────────────────────────────────────────────────
#
# 5×3 grid, lake_mid (impassable) at row=2 col=1, party at (col=1, row=4), plan N×4.
# Ray: step 1 → row=3 (grass, passable) → moves; step 2 scan at row=3 sees
# lake_mid at row=2 → adjacent_passable=False → stop.
# Expected: steps_taken=1, party at (col=1,row=3), stop_reason='blocker'.

def test_blocker():
    print("\n─── Test 2: blocker partway (N×4) ──────────────────────────────")
    grid = [
        ['grass1', 'grass1', 'grass1'],  # row 0
        ['grass1', 'grass1', 'grass1'],  # row 1
        ['grass1', 'lake_mid', 'grass1'],# row 2  ← blocker at col=1
        ['grass1', 'grass1', 'grass1'],  # row 3
        ['grass1', 'grass1', 'grass1'],  # row 4  ← start
    ]
    gen = _make_generator(grid)
    db = WorldDB(':memory:')

    pos = PartyPos(world_seed=WORLD_SEED, sx=1, sy=0, col=1, row=4)
    screen = enter_screen(pos, db, gen, tick=2)
    loaded_grid = json.loads(screen['grid_json'])

    print(f"  screen {pos.sx},{pos.sy}  start=(col={pos.col},row={pos.row})"
          f"  blocker=lake_mid@(col=1,row=2)")

    result = execute_move(pos, 'N', 4, loaded_grid, db)
    _print_result(result, 'N')

    assert result.stop_reason == 'blocker', \
        f"expected 'blocker', got {result.stop_reason!r}"
    assert result.steps_taken == 1, \
        f"expected 1 step, got {result.steps_taken}"
    assert pos.col == 1 and pos.row == 3, \
        f"expected (col=1,row=3), got (col={pos.col},row={pos.row})"

    # log has 2 entries: one 'moved' (step 1) and one 'stopped_blocker' (step 2)
    notes = [r.note for r in result.log]
    assert 'moved' in notes, f"expected a 'moved' entry, got {notes}"
    assert 'stopped_blocker' in notes, f"expected 'stopped_blocker', got {notes}"

    db.close()
    print("PASS: blocker stop")


# ─── Test 3: arrive at enterable feature ─────────────────────────────────────
#
# 4×3 grid, cave1 (enterable) at (row=1, col=1), party at (col=1, row=3), plan N×3.
# cave1 is passable, so adjacent_passable=True when seen from row=2.
# Party moves row=3→row=2 (step 1), then row=2→row=1=cave1 (step 2, enterable) → stop.
# Expected: steps_taken=2, party at (col=1,row=1), stop_reason='enterable',
#           DB feature entered=1.

def test_enterable_feature():
    print("\n─── Test 3: arrive at enterable feature (N×3) ───────────────────")
    grid = [
        ['grass1', 'grass1', 'grass1'],  # row 0
        ['grass1', 'cave1',  'grass1'],  # row 1  ← enterable feature at col=1
        ['grass1', 'grass1', 'grass1'],  # row 2
        ['grass1', 'grass1', 'grass1'],  # row 3  ← start
    ]
    feature_cells = {(1, 1): 'cave1'}   # (row, col) → feature type
    gen = _make_generator(grid, feature_cells)
    db = WorldDB(':memory:')

    pos = PartyPos(world_seed=WORLD_SEED, sx=2, sy=0, col=1, row=3)
    screen = enter_screen(pos, db, gen, tick=3)
    loaded_grid = json.loads(screen['grid_json'])

    # Confirm feature was pre-inserted by get_or_create_screen
    feat_before = db.get_feature(WORLD_SEED, 2, 0, row=1, col=1)
    assert feat_before is not None, "feature not inserted during screen creation"
    assert feat_before['entered'] is None, \
        f"expected entered=None before move, got {feat_before['entered']}"
    print(f"  screen {pos.sx},{pos.sy}  start=(col={pos.col},row={pos.row})"
          f"  cave1@(col=1,row=1)  entered_before={feat_before['entered']}")

    result = execute_move(pos, 'N', 3, loaded_grid, db)
    _print_result(result, 'N')

    assert result.stop_reason == 'enterable', \
        f"expected 'enterable', got {result.stop_reason!r}"
    assert result.steps_taken == 2, \
        f"expected 2 steps, got {result.steps_taken}"
    assert pos.col == 1 and pos.row == 1, \
        f"expected (col=1,row=1), got (col={pos.col},row={pos.row})"

    feat_after = db.get_feature(WORLD_SEED, 2, 0, row=1, col=1)
    assert feat_after['entered'] == 1, \
        f"expected entered=1, got {feat_after['entered']}"
    print(f"  feature entered_after={feat_after['entered']}")

    db.close()
    print("PASS: enterable feature arrival + entered marked")


# ─── Test 4: screen edge stop ────────────────────────────────────────────────
#
# 3×3 grid, party at (col=1, row=1), plan N×5.
# Row=0 is passable grass. Moving north: step 1 → row=0 (moved). Step 2: scan
# at row=0 shows N adjacent is off-grid (edge, adjacent_passable=False) → stop.
# Expected: steps_taken=1, party at (col=1,row=0), stop_reason='edge'.

def test_edge_stop():
    print("\n─── Test 4: screen edge stop (N×5) ──────────────────────────────")
    grid = [
        ['grass1', 'grass1', 'grass1'],  # row 0  ← edge
        ['grass1', 'grass1', 'grass1'],  # row 1  ← start
        ['grass1', 'grass1', 'grass1'],  # row 2
    ]
    gen = _make_generator(grid)
    db = WorldDB(':memory:')

    pos = PartyPos(world_seed=WORLD_SEED, sx=3, sy=0, col=1, row=1)
    screen = enter_screen(pos, db, gen, tick=4)
    loaded_grid = json.loads(screen['grid_json'])

    print(f"  screen {pos.sx},{pos.sy}  start=(col={pos.col},row={pos.row})")

    result = execute_move(pos, 'N', 5, loaded_grid, db)
    _print_result(result, 'N')

    assert result.stop_reason == 'edge', \
        f"expected 'edge', got {result.stop_reason!r}"
    assert result.steps_taken == 1, \
        f"expected 1 step (lands on row=0 then edge stops next), got {result.steps_taken}"
    assert pos.col == 1 and pos.row == 0, \
        f"expected (col=1,row=0), got (col={pos.col},row={pos.row})"

    db.close()
    print("PASS: screen edge stop")


# ─── interface notes ─────────────────────────────────────────────────────────

def print_interface_notes():
    print("""
─── Interface notes for next job (screen transitions) ──────────────────────

  1. No read-only WorldDB.get_screen(ws, sx, sy).
     get_or_create_screen requires a generator even for cached reads.
     Noted in worlddb.py header; fix before any job that needs ad-hoc grid
     reads without a generator.

  2. scan() arg order fixed (was col-first, now row-first to match grid/DB).
     All call sites updated; consistent across the stack.

  3. set_feature_state silently no-ops on a missing feature row.
     Noted in worlddb.py header; safe today, could hide mistyped coords later.
""")


# ─── main ────────────────────────────────────────────────────────────────────

if __name__ == '__main__':
    print(f"World seed: {WORLD_SEED}\n")
    test_clear_path()
    test_blocker()
    test_enterable_feature()
    test_edge_stop()
    print_interface_notes()
    print("All movement tests passed.")
