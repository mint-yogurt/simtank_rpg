"""Tile passability and quality lookup for simtank_rpg.

Parses both tilerules files once on import. No PIL, no game-state deps.

Quality tags found in tilerules:
  impassable  — tile cannot be moved to under any circumstances
  wall        — structural wall tile (also impassable unless also 'enterable')
  enterable   — stepping on this tile triggers a scene entry
  floor       — walkable floor decorator (cave/dungeon)
  scatter     — rare floor decoration
  container   — lootable object occupying a floor cell

is_passable rules:
  - 'impassable' tag → not passable
  - 'wall' without 'enterable' → not passable
  - 'wall' + 'enterable' → passable (e.g. cave 'enter' exit tile)
  - anything else (floor, scatter, container, no tags) → passable
"""

import os

_HERE = os.path.dirname(os.path.abspath(__file__))
_OVERWORLD_RULES = os.path.normpath(
    os.path.join(_HERE, '..', 'web', 'static', 'tiles', 'overworld_1_tilerules.txt'))
_CAVE_RULES = os.path.normpath(
    os.path.join(_HERE, '..', 'web', 'static', 'tiles', 'tiles_cave_rules.txt'))

# tile_name → frozenset of quality strings; populated on first call to _load()
_QUALITIES: dict = {}


def _parse_one(path):
    result = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or '=' not in line:
                continue
            eq = line.index('=')
            rest = line[eq + 1:]
            if '#' in rest:
                rest = rest[:rest.index('#')]
            parts = [p.strip().rstrip('_').replace(' ', '') for p in rest.split(',')]
            name = parts[0]
            if not name:
                continue
            result[name] = frozenset(q for q in parts[1:] if q)
    return result


def _load():
    if _QUALITIES:
        return
    _QUALITIES.update(_parse_one(_OVERWORLD_RULES))
    _QUALITIES.update(_parse_one(_CAVE_RULES))


def _strip_rot(tile):
    return tile.split(':', 1)[0] if ':' in tile else tile


def tile_quality(tile):
    """Return frozenset of quality strings for this tile. Empty frozenset if unknown."""
    _load()
    return _QUALITIES.get(_strip_rot(tile), frozenset())


def is_passable(tile):
    """True if the party can walk onto this tile (see module docstring for rules)."""
    q = tile_quality(tile)
    if 'impassable' in q:
        return False
    if 'wall' in q and 'enterable' not in q:
        return False
    return True


def is_enterable(tile):
    """True if stepping on this tile triggers a scene entry (cave, town, etc.)."""
    return 'enterable' in tile_quality(tile)


if __name__ == '__main__':
    _load()

    cases = [
        # overworld passable
        ('grass1',           True,  False),
        ('grass2',           True,  False),
        ('dirt_mid',         True,  False),
        ('mowdenpass',       True,  False),
        ('pond1',            True,  False),
        ('path_vertical',    True,  False),
        ('path_corner_N+E',  True,  False),
        # overworld impassable
        ('lake_mid',         False, False),
        ('forest_NW',        False, False),
        ('mnt_mid',          False, False),
        ('mountain1',        False, False),
        ('tree1',            False, False),
        # overworld enterable (passable + enterable)
        ('town1',            True,  True),
        ('cave1',            True,  True),
        ('castle1',          True,  True),
        ('hub',              True,  True),
        ('mnt_cave',         True,  True),
        # cave passable
        ('cobble1',          True,  False),
        ('dungfloor1',       True,  False),
        ('puddle1',          True,  False),
        ('skull1',           True,  False),
        # cave: enter tile — wall+enterable → passable
        ('enter',            True,  True),
        # cave wall tiles — impassable
        ('cavewall1',        False, False),
        ('cavewalltop1',     False, False),
        ('cavewallS',        False, False),
        ('brickwallN1',      False, False),
        ('waterfall1',       False, False),
        ('waterN',           False, False),
        # rotation suffix stripped correctly
        ('path_corner_N+E:90', True, False),
        ('path_xroad_W+S+E:180', True, False),
    ]

    failures = 0
    for tile, exp_pass, exp_enter in cases:
        got_pass  = is_passable(tile)
        got_enter = is_enterable(tile)
        ok = got_pass == exp_pass and got_enter == exp_enter
        tag = 'OK' if ok else 'FAIL'
        if not ok:
            failures += 1
        quals = ', '.join(sorted(tile_quality(tile))) or '(none)'
        print(f"  {tag}  {tile:<30} passable={got_pass} enterable={got_enter}  [{quals}]")

    print(f"\n{'All OK' if not failures else f'{failures} FAILURE(S)'} ({len(cases)} cases)")
