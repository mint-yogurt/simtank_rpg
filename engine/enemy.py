"""Enemy definitions, runtime state, and movement -- pure logic, no pygame.

Mirrors the player/input split: engine.renderer owns object-layer parsing,
sprite loading, spawn rolling, and drawing; this module only knows what
enemies exist (EnemyDef, loaded from data/enemy/enemies.yaml) and how they
move (Enemy, the live per-instance state, ticked every frame).

Enemy position is NOT tile-discrete like NPC -- Enemy.row/col are floats, in
tile units, and that float pair IS the logical position, not a renderer-only
visual tween the way OverworldScene keeps one for NPCs. Movement is
continuous: every frame, an enemy advances along its current cardinal
direction by move_speed (tiles/sec) * dt, hard-stopping at the first
non-walkable/out-of-bounds tile its 1-tile-square bounding box would enter.
Player moves the same continuous way now (see engine.player, engine.movement)
-- NPC is the one that stays tile-discrete, unchanged.

Behavior types (authored per-enemy in enemies.yaml):
  wanderer -- 65% chance to head toward the party, 35% random, re-rolled on
              a fixed decision interval; moves continuously between rolls.
  pacer    -- walks back and forth along behavior_axis ('H' or 'V'),
              reversing when the fixed decision-interval check finds the
              next tile blocked.
  sentinel -- frozen until the party enters 8-tile cardinal line of sight,
              then continuously chases.
"""

import math
import random
from dataclasses import dataclass
from pathlib import Path

import yaml

from engine.movement import _DIR_DELTA, step_continuous

_ENEMY_DEFS_PATH = Path(__file__).parent.parent / "data" / "enemy" / "enemies.yaml"
_CARDINALS = ("N", "S", "E", "W")

DECISION_MS = 800   # how often a behavior re-evaluates its direction -- same
                     # cadence as OverworldScene's NPC wander tick, for feel
                     # consistency between the two systems.

_LOS_MAX_DIST = 8   # sentinel aggro range, tiles -- matches the old ported logic


# ── Definitions (static, hand-authored) ──────────────────────────────────────

@dataclass(frozen=True)
class EnemyDef:
    """One entry of data/enemy/enemies.yaml. Static -- never mutated at runtime."""
    id:             str
    name:           str
    sprite:         str                  # stem in assets/sprites/enemies/<sprite>.png
    battle_art:     str | None           # filename in assets/tiles/enemies/ -- the
                                          #   full-size battle portrait (see
                                          #   engine.renderer.BattleScene), distinct
                                          #   from the 16px overworld `sprite` above
    battle_bg:      str | None           # a procgen.visualizer effect name
                                          #   ("plasma"/"ripples"/"swarm"/"tunnel"/
                                          #   "starfield") to use as this enemy's
                                          #   battle background; null/omitted picks
                                          #   one at random per battle
    iq:             int
    weight:         int
    sweat:          int
    hair:           int
    level:          int | list[int]      # flat level, or a [min, max] range
    move_speed:     float                # tiles/sec
    behavior:       str                  # "wanderer" | "pacer" | "sentinel"
    behavior_axis:  str | None = None    # "H" | "V" -- pacer only


def load_enemy_defs(path: Path = _ENEMY_DEFS_PATH) -> dict[str, EnemyDef]:
    """Parse enemies.yaml into id -> EnemyDef."""
    raw = yaml.safe_load(path.read_text()) or {}
    defs: dict[str, EnemyDef] = {}
    for eid, entry in raw.items():
        defs[eid] = EnemyDef(
            id            = eid,
            name          = entry["name"],
            sprite        = entry["sprite"],
            battle_art    = entry.get("battle_art"),
            battle_bg     = entry.get("battle_bg"),
            iq            = entry["iq"],
            weight        = entry["weight"],
            sweat         = entry["sweat"],
            hair          = entry["hair"],
            level         = entry["level"],
            move_speed    = entry["move_speed"],
            behavior      = entry["behavior"],
            behavior_axis = entry.get("behavior_axis"),
        )
    return defs


def resolve_level(level_field: int | list[int], rng: random.Random) -> int:
    """A def's (or a placement's override) level field is either a flat int
    or a [min, max] range -- roll within the range, or return the int as-is.
    Used for both hardcoded `enemy` placements and `spawner` rolls."""
    if isinstance(level_field, (list, tuple)):
        lo, hi = level_field
        return rng.randint(lo, hi)
    return int(level_field)


def resolve_spawn(spawn_chance: float, candidates: list[dict], rng: random.Random) -> str | None:
    """One `spawner` roll: first the overall gate (spawn_chance, 0-1),
    then -- only if that passes -- a weighted pick among `candidates`
    ([{'enemy_id':..., 'chance':...}, ...], each `chance` a relative weight,
    not an independent probability) so a successful roll always produces
    exactly one enemy, never zero or more than one. Returns an enemy_id or
    None.

    Candidates missing enemy_id or chance (a half-filled copy-pasted stub
    block -- see data/maps/populate_yamls.py's spawner stub) are dropped
    before weighing, same fail-soft spirit as an unconfigured warp: a
    hand-authoring mistake shouldn't crash map load."""
    candidates = [c for c in candidates if c.get("enemy_id") is not None and c.get("chance") is not None]
    if not candidates or rng.random() >= spawn_chance:
        return None
    total = sum(c["chance"] for c in candidates)
    if total <= 0:
        return None
    roll = rng.uniform(0, total)
    upto = 0.0
    for c in candidates:
        upto += c["chance"]
        if roll <= upto:
            return c["enemy_id"]
    return candidates[-1]["enemy_id"]


# ── Runtime state (mutable, live gameplay) ───────────────────────────────────

@dataclass
class Enemy:
    """One live enemy on the current map. Unlike NPC, row/col are floats --
    the logical position IS the visual position, ticked continuously every
    frame (see module docstring); there's no separate renderer tween dict
    for enemies the way OverworldScene keeps one for NPCs.

    `direction` is the cardinal currently being walked, or None when idle
    (e.g. an un-triggered sentinel, or every direction blocked). Behavior
    state (`pace_dir`, `activated`, `decision_timer`) is mutable live
    gameplay state, same spirit as NPC.row/col mutating for wander.
    """
    index:          int          # stable identity (Tiled object id)
    enemy_id:       str          # key into EnemyDef
    row:            float
    col:            float
    level:          int
    behavior:       str
    behavior_axis:  str | None = None
    direction:      str | None = None
    pace_dir:       int = 1              # +1 / -1 along behavior_axis, pacer only
    activated:      bool = False         # sentinel only: True once LOS has triggered
    decision_timer: int = 0              # ms accumulator toward the next direction re-roll


def _tile_walkable(passable: list[list[bool]], row: int, col: int) -> bool:
    rows = len(passable)
    cols = len(passable[0]) if rows else 0
    return 0 <= row < rows and 0 <= col < cols and passable[row][col]


def _toward_directions(row: float, col: float, party_row: int, party_col: int) -> list[str]:
    """Cardinal directions ordered by how much they reduce Manhattan
    distance to the party (greater-gap axis first), with any remaining
    cardinals appended so a caller always has a full fallback order to try
    if the preferred ones are blocked."""
    dr, dc = party_row - row, party_col - col
    ordered = []
    if abs(dr) >= abs(dc):
        if dr != 0:
            ordered.append('S' if dr > 0 else 'N')
        if dc != 0:
            ordered.append('E' if dc > 0 else 'W')
    else:
        if dc != 0:
            ordered.append('E' if dc > 0 else 'W')
        if dr != 0:
            ordered.append('S' if dr > 0 else 'N')
    for d in _CARDINALS:
        if d not in ordered:
            ordered.append(d)
    return ordered


def _first_walkable(enemy: Enemy, passable: list[list[bool]], directions: list[str]) -> str | None:
    er, ec = math.floor(enemy.row), math.floor(enemy.col)
    for d in directions:
        dr, dc = _DIR_DELTA[d]
        if _tile_walkable(passable, er + dr, ec + dc):
            return d
    return None


def _cardinal_los(er: float, ec: float, pr: int, pc: int, passable: list[list[bool]],
                   max_dist: int = _LOS_MAX_DIST) -> bool:
    """True if the enemy has an unobstructed cardinal sightline to the
    party -- same row or column, within max_dist tiles, nothing blocking
    between them."""
    er, ec = math.floor(er), math.floor(ec)
    if er == pr:
        dist = abs(ec - pc)
        if dist == 0 or dist > max_dist:
            return False
        step = 1 if pc > ec else -1
        return all(_tile_walkable(passable, er, c) for c in range(ec + step, pc, step))
    if ec == pc:
        dist = abs(er - pr)
        if dist == 0 or dist > max_dist:
            return False
        step = 1 if pr > er else -1
        return all(_tile_walkable(passable, r, ec) for r in range(er + step, pr, step))
    return False


def _decide_wanderer(enemy: Enemy, passable: list[list[bool]],
                      party_row: int, party_col: int, rng: random.Random) -> str | None:
    if rng.random() < 0.65:
        directions = _toward_directions(enemy.row, enemy.col, party_row, party_col)
    else:
        directions = list(_CARDINALS)
        rng.shuffle(directions)
    return _first_walkable(enemy, passable, directions)


def _decide_pacer(enemy: Enemy, passable: list[list[bool]]) -> str | None:
    axis_dirs = ('W', 'E') if enemy.behavior_axis == 'H' else ('N', 'S')
    forward = axis_dirs[1] if enemy.pace_dir == 1 else axis_dirs[0]
    if _first_walkable(enemy, passable, [forward]):
        return forward
    enemy.pace_dir *= -1
    reversed_dir = axis_dirs[1] if enemy.pace_dir == 1 else axis_dirs[0]
    return _first_walkable(enemy, passable, [reversed_dir])


def _decide_sentinel(enemy: Enemy, passable: list[list[bool]],
                      party_row: int, party_col: int) -> str | None:
    if not enemy.activated:
        if _cardinal_los(enemy.row, enemy.col, party_row, party_col, passable):
            enemy.activated = True
        else:
            return None
    return _first_walkable(enemy, passable, _toward_directions(enemy.row, enemy.col, party_row, party_col))


_DECIDERS = {
    "wanderer": lambda e, p, pr, pc, rng: _decide_wanderer(e, p, pr, pc, rng),
    "pacer":    lambda e, p, pr, pc, rng: _decide_pacer(e, p),
    "sentinel": lambda e, p, pr, pc, rng: _decide_sentinel(e, p, pr, pc),
}


def update_enemy(enemy: Enemy, enemy_def: EnemyDef, passable: list[list[bool]],
                  party_row: int, party_col: int, dt_ms: int, rng: random.Random) -> None:
    """Advance one enemy by one frame: re-evaluate its direction on the
    fixed decision interval (DECISION_MS), then move continuously along
    whatever direction it's currently holding. Mutates `enemy` in place."""
    enemy.decision_timer += dt_ms
    if enemy.decision_timer >= DECISION_MS:
        enemy.decision_timer -= DECISION_MS
        decide = _DECIDERS.get(enemy.behavior)
        enemy.direction = decide(enemy, passable, party_row, party_col, rng) if decide else None

    if enemy.direction is None:
        return

    dist = enemy_def.move_speed * (dt_ms / 1000)
    enemy.row, enemy.col = step_continuous(enemy.row, enemy.col, enemy.direction, dist, passable)


def check_overlap(enemy: Enemy, player_row: int, player_col: int) -> bool:
    """True if the player's tile intersects this enemy's current bounding
    box. No-op hook -- enemies never block the player (see README/CLAUDE.md)
    -- this only flags where a future battle-trigger-on-touch + post-battle
    blink-immunity window will read from once battle exists. Two unit
    squares overlap iff both axis gaps are under 1 tile."""
    return abs(player_row - enemy.row) < 1.0 and abs(player_col - enemy.col) < 1.0
