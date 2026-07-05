"""Standalone test for engine/worlddb.py.

Tests:
  1. Basic screen discovery: exits and features populated correctly.
  2. mark_visited / first_visit_tick persistence.
  3. set_feature_state / get_feature round-trip.
  4. _compute_exits with synthetic grids (discriminating correctness check).
  5. Replay guarantee: a second fresh DB discovers the same screens and produces
     identical exits and features as the first DB.

Run from the repo root:
    python procgen/worlddb_test.py
"""

import json
import sys
import os

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from engine.worlddb import WorldDB, _compute_exits
from procgen.overworld_test import generate_screen_data

WORLD_SEED = 7654321098765432
TEST_COORDS = [(0, 0), (1, 0), (-1, 0), (0, 1), (2, -1)]


def generator(world_seed, sx, sy):
    return generate_screen_data(world_seed, sx, sy)


def _discover_all(db):
    for sx, sy in TEST_COORDS:
        db.get_or_create_screen(WORLD_SEED, sx, sy, generator)


def test_discovery_and_exits():
    db = WorldDB(':memory:')
    _discover_all(db)

    screens = db.list_known_screens(WORLD_SEED)
    assert len(screens) == len(TEST_COORDS), f"expected {len(TEST_COORDS)} screens, got {len(screens)}"

    for s in screens:
        exits = json.loads(s['exits_json'])
        assert set(exits.keys()) == {'N', 'S', 'E', 'W'}, f"bad exits keys: {exits}"
        assert all(isinstance(v, bool) for v in exits.values()), f"exits not bool: {exits}"
        grid = json.loads(s['grid_json'])
        assert len(grid) == s['rows']
        assert len(grid[0]) == s['cols']
        print(f"  screen ({s['sx']:2},{s['sy']:2})  exits={exits}  "
              f"features={sum(1 for _ in db.list_known_features(WORLD_SEED) if _['sx']==s['sx'] and _['sy']==s['sy'])}")

    db.close()
    print("PASS: discovery and exits")


def test_visited():
    db = WorldDB(':memory:')
    _discover_all(db)

    sx, sy = TEST_COORDS[0]
    s = db.get_or_create_screen(WORLD_SEED, sx, sy, generator)
    assert s['visited'] == 0
    assert s['first_visit_tick'] is None

    db.mark_visited(WORLD_SEED, sx, sy, tick=42)
    s2 = db.get_or_create_screen(WORLD_SEED, sx, sy, generator)
    assert s2['visited'] == 1
    assert s2['first_visit_tick'] == 42, f"expected 42, got {s2['first_visit_tick']}"

    # second mark_visited should not overwrite first_visit_tick
    db.mark_visited(WORLD_SEED, sx, sy, tick=99)
    s3 = db.get_or_create_screen(WORLD_SEED, sx, sy, generator)
    assert s3['first_visit_tick'] == 42, "first_visit_tick changed after second mark_visited"

    db.close()
    print("PASS: mark_visited / first_visit_tick")


def test_feature_state():
    db = WorldDB(':memory:')
    _discover_all(db)

    all_features = db.list_known_features(WORLD_SEED)
    enterable_features = [f for f in all_features if f['enterable']]
    if not enterable_features:
        print("SKIP: no enterable features on test screens (rare but possible)")
        db.close()
        return

    f = enterable_features[0]
    ws, sx, sy, row, col = f['world_seed'], f['sx'], f['sy'], f['local_row'], f['local_col']

    assert f['entered'] is None
    assert f['cleared'] is None
    assert f['npc_flags_json'] is None

    db.set_feature_state(ws, sx, sy, row, col, entered=True)
    f2 = db.get_feature(ws, sx, sy, row, col)
    assert f2['entered'] == 1, f"entered not set: {f2}"
    assert f2['cleared'] is None, "cleared should still be None"

    db.set_feature_state(ws, sx, sy, row, col, cleared=False, npc_flags={'innkeeper': True})
    f3 = db.get_feature(ws, sx, sy, row, col)
    assert f3['cleared'] == 0
    assert json.loads(f3['npc_flags_json']) == {'innkeeper': True}

    print(f"  feature: {f['feature_type']} @ ({sx},{sy}) row={row} col={col}")
    db.close()
    print("PASS: set_feature_state / get_feature")


def test_compute_exits_synthetic():
    """Verify _compute_exits correctly rejects impassable and enterable edge tiles."""
    rows, cols = 3, 4

    # North edge: all impassable (mnt_mid). Rest: passable grass.
    grid = [['mnt_mid'] * cols] + [['grass1'] * cols for _ in range(rows - 1)]
    exits = _compute_exits(grid)
    assert exits['N'] is False, f"N should be blocked by mnt_mid, got {exits['N']}"
    assert exits['S'] is True,  f"S should be open (grass1), got {exits['S']}"
    assert exits['E'] is True,  f"E should be open (grass1), got {exits['E']}"
    assert exits['W'] is True,  f"W should be open (grass1), got {exits['W']}"

    # North edge: one enterable tile + rest impassable → still no exit (enterable ≠ through-route).
    grid2 = [['mnt_mid'] * cols] + [['grass1'] * cols for _ in range(rows - 1)]
    grid2[0][0] = 'cave1'
    exits2 = _compute_exits(grid2)
    assert exits2['N'] is False, f"N: enterable+impassable should not open an exit, got {exits2['N']}"

    print("PASS: _compute_exits synthetic grid")


def test_replay():
    """Two independent DBs discovering the same screens must produce identical data."""
    db1 = WorldDB(':memory:')
    db2 = WorldDB(':memory:')
    _discover_all(db1)
    _discover_all(db2)

    s1 = {(r['sx'], r['sy']): r for r in db1.list_known_screens(WORLD_SEED)}
    s2 = {(r['sx'], r['sy']): r for r in db2.list_known_screens(WORLD_SEED)}

    for coord in TEST_COORDS:
        a, b = s1[coord], s2[coord]
        assert a['screen_seed'] == b['screen_seed'], f"{coord}: screen_seed mismatch"
        assert a['exits_json']  == b['exits_json'],  f"{coord}: exits mismatch"
        assert a['grid_json']   == b['grid_json'],   f"{coord}: grid mismatch"

    f1 = {(r['sx'],r['sy'],r['local_row'],r['local_col']): r
          for r in db1.list_known_features(WORLD_SEED)}
    f2 = {(r['sx'],r['sy'],r['local_row'],r['local_col']): r
          for r in db2.list_known_features(WORLD_SEED)}
    assert set(f1.keys()) == set(f2.keys()), "feature keys differ between DBs"
    for k in f1:
        assert f1[k]['feature_type'] == f2[k]['feature_type'], f"{k}: feature_type mismatch"
        assert f1[k]['enterable']    == f2[k]['enterable'],    f"{k}: enterable mismatch"

    db1.close(); db2.close()
    print(f"PASS: replay guarantee ({len(s1)} screens, {len(f1)} features)")


if __name__ == '__main__':
    print(f"World seed: {WORLD_SEED}\n")
    print("─ discovery and exits ─────────────────")
    test_discovery_and_exits()
    print("\n─ visited tracking ─────────────────────")
    test_visited()
    print("\n─ feature state ────────────────────────")
    test_feature_state()
    print("\n─ exits (synthetic) ────────────────────")
    test_compute_exits_synthetic()
    print("\n─ replay guarantee ─────────────────────")
    test_replay()
    print("\nAll tests passed.")
