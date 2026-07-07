"""Test harness for engine/viewscan.py.

Runs synthetic grid checks (fast, no procgen) then live overworld tests with
ASCII grid output.

Run from the repo root:
    python procgen/viewscan_test.py
"""

import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.tiles import is_enterable, is_passable
from engine.viewscan import _DIRS, scan
from llm.ascii_map import render_map_overlay
from procgen.worldgen import generate_screen_data

WORLD_SEED = 77777


# ─── assertion helper ─────────────────────────────────────────────────────────

def _assert_ray(vs, label, expected_kind, expected_dist, expected_adj,
                grid, rows, cols, expected_tile=None):
    """Assert one DirectionScan and structurally verify the terminating cell."""
    ds = getattr(vs, label)
    dc, dr = _DIRS[label]

    assert ds.kind == expected_kind, \
        f"{label}: expected kind={expected_kind!r}, got {ds.kind!r}"
    assert ds.distance == expected_dist, \
        f"{label}: expected dist={expected_dist}, got {ds.distance}"
    assert ds.adjacent_passable == expected_adj, \
        f"{label}: expected adj_passable={expected_adj}, got {ds.adjacent_passable}"
    if expected_tile is not None:
        assert ds.tile == expected_tile, \
            f"{label}: expected tile={expected_tile!r}, got {ds.tile!r}"

    if ds.kind == 'edge':
        assert ds.tile == '', f"{label}: edge should have tile='', got {ds.tile!r}"
        tc, tr = vs.party_col + dc * ds.distance, vs.party_row + dr * ds.distance
        assert not (0 <= tr < rows and 0 <= tc < cols), \
            f"{label}: edge dist={ds.distance} → ({tc},{tr}) is in-bounds (should be off)"
    else:
        tc, tr = vs.party_col + dc * ds.distance, vs.party_row + dr * ds.distance
        assert 0 <= tr < rows and 0 <= tc < cols, \
            f"{label}: {ds.kind} dist={ds.distance} → ({tc},{tr}) is off-grid"
        actual = (grid[tr][tc] or 'grass1').split(':')[0]
        assert actual == ds.tile, \
            f"{label}: grid[{tr}][{tc}]={actual!r} != reported tile {ds.tile!r}"

    # Every intermediate cell must be passable and not enterable
    for d in range(1, ds.distance):
        ic, ir = vs.party_col + dc * d, vs.party_row + dr * d
        itile = (grid[ir][ic] or 'grass1').split(':')[0]
        assert is_passable(itile), \
            f"{label}: intermediate d={d} tile={itile!r} is impassable"
        assert not is_enterable(itile), \
            f"{label}: intermediate d={d} tile={itile!r} is enterable (should've stopped ray)"


# ─── synthetic tests ──────────────────────────────────────────────────────────

def test_synthetic():
    """Fabricated grids — verifies each kind fires with correct distance."""

    # Grid A: 4 rows × 5 cols
    # Party at col=1, row=2
    #   N → edge (row 1 passable, row 0 passable, row -1 off-grid) → dist=3
    #   S → blocker lake_mid at row 3                               → dist=1
    #   E → enterable cave1 at col 2                                → dist=1
    #   W → edge (col 0 passable, col -1 off-grid)                  → dist=2
    gridA = [
        ['grass1', 'grass1', 'grass1',  'grass1',  'grass1'],
        ['grass1', 'grass1', 'cave1',   'grass1',  'grass1'],
        ['grass1', 'grass1', 'cave1',   'grass1',  'grass1'],  # party at (col=1,row=2)
        ['grass1', 'lake_mid','lake_mid','lake_mid','grass1'],
    ]
    vs = scan(gridA, party_col=1, party_row=2)
    _assert_ray(vs, 'N', 'edge',      3, True,  gridA, 4, 5)
    _assert_ray(vs, 'S', 'blocker',   1, False, gridA, 4, 5, expected_tile='lake_mid')
    _assert_ray(vs, 'E', 'enterable', 1, True,  gridA, 4, 5, expected_tile='cave1')
    _assert_ray(vs, 'W', 'edge',      2, True,  gridA, 4, 5)

    # Grid B: enterable at distance > 1 (tests intermediate passable check)
    # Party at col=1, row=1; hub at col=4, row=1 → E enterable dist=3
    gridB = [
        ['grass1'] * 6,
        ['grass1', 'grass1', 'grass1', 'grass1', 'hub',    'grass1'],
        ['grass1'] * 6,
    ]
    vs2 = scan(gridB, party_col=1, party_row=1)
    _assert_ray(vs2, 'E', 'enterable', 3, True, gridB, 3, 6, expected_tile='hub')

    # Grid C: party at NW corner — N and W are edge at dist=1
    gridC = [
        ['grass1', 'grass1', 'grass1'],
        ['grass1', 'grass1', 'grass1'],
    ]
    vs3 = scan(gridC, party_col=0, party_row=0)
    _assert_ray(vs3, 'N', 'edge', 1, False, gridC, 2, 3)
    _assert_ray(vs3, 'W', 'edge', 1, False, gridC, 2, 3)

    print("PASS: synthetic grid tests")


# ─── real screen helpers ──────────────────────────────────────────────────────

def _passable_adj_to(grid, rows, cols, pred):
    """First passable non-enterable tile that has a direct cardinal neighbor matching pred."""
    for r in range(rows):
        for c in range(cols):
            base = (grid[r][c] or 'grass1').split(':')[0]
            if not is_passable(base) or is_enterable(base):
                continue
            for dc, dr in ((1, 0), (-1, 0), (0, 1), (0, -1)):
                nr, nc = r + dr, c + dc
                if 0 <= nr < rows and 0 <= nc < cols:
                    ntile = (grid[nr][nc] or 'grass1').split(':')[0]
                    if pred(ntile):
                        return r, c
    return None


def _passable_at_edge(grid, rows, cols):
    """First passable non-enterable tile on any screen edge row/col."""
    for r in range(rows):
        for c in range(cols):
            if r not in (0, rows - 1) and c not in (0, cols - 1):
                continue
            base = (grid[r][c] or 'grass1').split(':')[0]
            if is_passable(base) and not is_enterable(base):
                return r, c
    return None


# ─── real screen test ─────────────────────────────────────────────────────────

def test_real_screen(sx=0, sy=0):
    data = generate_screen_data(WORLD_SEED, sx, sy)
    grid, rows, cols = data.grid, data.rows, data.cols

    print(f"\n=== Screen ({sx},{sy})  screen_seed={data.screen_seed} ===")
    for line in data.log:
        print(' ', line)

    has_features = any(
        is_enterable((grid[r][c] or 'grass1').split(':')[0])
        for r in range(rows) for c in range(cols)
    )

    pos_enterable = _passable_adj_to(grid, rows, cols, is_enterable) if has_features else None
    pos_blocker   = _passable_adj_to(grid, rows, cols, lambda t: not is_passable(t))
    pos_edge      = _passable_at_edge(grid, rows, cols)

    assert pos_blocker is not None, "no passable tile adjacent to an impassable tile"
    assert pos_edge    is not None, "no passable tile on screen edge"

    positions = {}
    if pos_enterable is not None:
        positions['near_enterable'] = pos_enterable
    if pos_blocker:
        positions['near_blocker'] = pos_blocker
    if pos_edge:
        positions['at_edge'] = pos_edge

    all_kinds = set()
    for tag, (pr, pc) in positions.items():
        tile_name = (grid[pr][pc] or 'grass1').split(':')[0]
        vs = scan(grid, party_col=pc, party_row=pr, feature_cells=data.feature_cells)

        print(f"\n  [{tag}]  party col={pc} row={pr}  tile={tile_name!r}")
        print(render_map_overlay(grid, rows, cols, vs))
        print()

        for label in ('N', 'S', 'E', 'W'):
            ds = getattr(vs, label)
            adj_str = 'step' if ds.adjacent_passable else 'wall'
            print(f"    {label}: {ds.kind:10}  tile={ds.tile or '(edge)':20}  "
                  f"dist={ds.distance:2}  adj={adj_str}")
            all_kinds.add(ds.kind)

            # Structural reconstruction check
            dc, dr = _DIRS[label]
            tc, tr = pc + dc * ds.distance, pr + dr * ds.distance
            if ds.kind == 'edge':
                assert not (0 <= tr < rows and 0 <= tc < cols), \
                    f"[{tag}] {label}: edge should be off-grid at ({tc},{tr})"
            else:
                assert 0 <= tr < rows and 0 <= tc < cols, \
                    f"[{tag}] {label}: {ds.kind} terminator ({tc},{tr}) is off-grid"
                actual = (grid[tr][tc] or 'grass1').split(':')[0]
                assert actual == ds.tile, \
                    f"[{tag}] {label}: grid[{tr}][{tc}]={actual!r} != reported tile {ds.tile!r}"

    expected_kinds = {'blocker', 'edge'}
    if has_features:
        expected_kinds.add('enterable')
    missing = expected_kinds - all_kinds
    assert not missing, f"screen ({sx},{sy}): missing scan kinds {missing}"
    print(f"\n  coverage: {sorted(all_kinds)}")
    print(f"PASS: real screen ({sx},{sy})")


if __name__ == '__main__':
    print("─ synthetic grid tests ─────────────────")
    test_synthetic()
    print("\n─ real screen (0,0) ─────────────────────")
    test_real_screen(0, 0)
    print("\n─ real screen (1,0) ─────────────────────")
    test_real_screen(1, 0)
    print("\nAll viewscan tests passed.")
