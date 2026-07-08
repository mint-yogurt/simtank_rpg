"""Production overworld loop: goal-driven navigation (JOB 11a/11b).

Three-tier decision system:
  TIER 1 — Goal: persistent Goal object; LLM sets a new goal only when none
            is active.  Single-member goal-setting in 11a/11b; multi-member
            deliberation stubbed for a future job.
  TIER 2 — Execution: BFS within-screen pathfinding + scripted movement.
            No LLM calls on individual movement steps.
  TIER 3 — Checkpoints: surfaced at four trigger points (goal reached, branch
            point, path blocked, battle resolved [stub]).  Discussion produces
            continue / abandon / modify.

Called by run_cli.py and run_web.py.

Optional emit/render_screen_fn callbacks wire in the web SSE server:
  emit(dict)                   — called with typed event dicts
  render_screen_fn(ws, sx, sy) — renders screen PNG, returns URL string
Both default to None (CLI mode: no web output).
"""

import json
import random
import re
import time
from dataclasses import dataclass
from pathlib import Path

from engine.config import cfg
from engine.context import CuratedContext, build_curated_context
from engine.goal import Goal
from engine.journal import MemberJournal, journals_append
from engine.navlog import NavLog
from engine.worlddb import WorldDB, compute_feature_id
from engine.party_state import PartyPos, enter_screen, execute_move
from engine.pathfinding import bfs_to_exit, bfs_to_tile, path_to_segments, screen_direction_toward
from engine.scenes import Interior
from engine.tiles import is_enterable, is_passable
from llm.client import ask_with_retry
from llm.prompts import (build_checkpoint_context, build_checkpoint_system_prompt,
                          build_goal_context, build_goal_system_prompt)
from llm.schema import parse_checkpoint_decision, parse_goal_decision
from procgen.cavegen import generate_cave_data
from procgen.towngen import generate_town_data
from procgen.worldgen import FEATURE_TYPES, generate_screen_data
from engine.battle import BattleEnemy, load_party as load_battle_party, run_battle
from engine.combat import Fighter
from engine.enemy_state import EnemyAgent, place_enemies, update_enemies


@dataclass
class OverworldMember:
    name: str
    lvl: int
    hp: int
    max_hp: int
    personality: str
    alive: bool = True

    @classmethod
    def from_json(cls, path: Path) -> "OverworldMember":
        with open(path) as f:
            d = json.load(f)
        return cls(
            name=d["name"],
            lvl=d["lvl"],
            hp=d["hp"],
            max_hp=d["max_hp"],
            personality=d.get("personality", ""),
        )


_PARTY_ORDER = ["melvin", "billy", "smeltrud", "poots"]

def load_overworld_party(data_dir: Path) -> list:
    members = []
    for p in sorted(data_dir.glob("*.json"),
                    key=lambda p: _PARTY_ORDER.index(p.stem)
                                  if p.stem in _PARTY_ORDER else 999):
        if p.stem == "null":
            continue
        members.append(OverworldMember.from_json(p))
    return members


def _find_connected_start(grid, rows, cols):
    """Return a walkable tile connected to at least one exit edge, closest to center.

    Seeds BFS from all walkable exit-edge tiles inward so the party always
    starts in a region that has a reachable screen crossing.  Falls back to
    the nearest walkable tile to center if nothing is exit-connected (should
    not happen given procgen guarantees, but safe).
    """
    def walkable(r, c):
        base = (grid[r][c] or "grass1").split(":")[0]
        return is_passable(base) and not is_enterable(base)

    # Seed set: every walkable tile on any exit edge.
    seeds = set()
    for c in range(cols):
        if walkable(0, c):
            seeds.add((0, c))
        if walkable(rows - 1, c):
            seeds.add((rows - 1, c))
    for r in range(rows):
        if walkable(r, 0):
            seeds.add((r, 0))
        if walkable(r, cols - 1):
            seeds.add((r, cols - 1))

    # BFS inward from all seeds → exit-connected component.
    from collections import deque as _deque
    visited: set[tuple[int, int]] = set(seeds)
    q = _deque(seeds)
    while q:
        r, c = q.popleft()
        for dr, dc in ((-1, 0), (1, 0), (0, -1), (0, 1)):
            nr, nc = r + dr, c + dc
            if 0 <= nr < rows and 0 <= nc < cols and (nr, nc) not in visited and walkable(nr, nc):
                visited.add((nr, nc))
                q.append((nr, nc))

    cr, cc = rows // 2, cols // 2
    if visited:
        return min(visited, key=lambda rc: abs(rc[0] - cr) + abs(rc[1] - cc))

    # Fallback: any walkable tile closest to center.
    for radius in range(max(rows, cols)):
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                if abs(dr) != radius and abs(dc) != radius:
                    continue
                r, c = cr + dr, cc + dc
                if 0 <= r < rows and 0 <= c < cols and walkable(r, c):
                    return r, c
    return None


def _goal_directed_open_exits(sx: int, sy: int,
                               target_sx: int, target_sy: int,
                               exits: dict) -> list[str]:
    """Return all exit directions that are both open and reduce distance to goal."""
    dx = target_sx - sx
    dy = target_sy - sy
    candidates: list[str] = []
    if dx > 0:
        candidates.append('E')
    elif dx < 0:
        candidates.append('W')
    if dy > 0:
        candidates.append('S')
    elif dy < 0:
        candidates.append('N')
    return [d for d in candidates if exits.get(d, False)]


def _execute_proposal(pos, direction, steps, grid, db, journals, navlog, tick,
                      generator, emit=None, render_screen_fn=None,
                      member_name: str | None = None, on_step=None):
    """Execute scripted movement. Returns (stop_reason, final_grid, crossed).

    on_step: optional callable(party_row, party_col, grid) called on each
             non-crossing step, used to update enemy positions each tile move.
    """
    result = execute_move(pos, direction, steps, grid, db,
                          journals=journals, tick=tick, generator=generator)
    final_grid = result.final_grid if result.final_grid is not None else grid
    crossed = result.final_grid is not None and result.final_grid is not grid
    print(f"  Move: {direction} {result.steps_taken} steps, stop={result.stop_reason!r}"
          + (f"\n  *** CROSSED SCREEN → now at ({pos.sx},{pos.sy})" if crossed else ""),
          flush=True)

    for step in result.log:
        if step.note.startswith("crossed_"):
            m = re.match(r'crossed_[NSEW]_to\((-?\d+),(-?\d+)\)', step.note)
            if m:
                sx2, sy2 = int(m.group(1)), int(m.group(2))
                navlog.append(tick, 'SCREEN', f"crossed {direction} → screen ({sx2},{sy2})")
                if emit:
                    rows2 = len(final_grid)
                    cols2 = len(final_grid[0]) if rows2 else 0
                    tile_payload = render_screen_fn(pos.world_seed, sx2, sy2) if render_screen_fn else {}
                    emit({"type": "screen", "sx": sx2, "sy": sy2,
                          "row": step.after_row, "col": step.after_col,
                          "rows": rows2, "cols": cols2, **tile_payload})
                    time.sleep(cfg.screen_cross_ms / 1000)
        elif step.note.startswith("arrived_enterable"):
            m = re.match(r'arrived_enterable\((\w+)\)', step.note)
            if m:
                navlog.append(tick, 'ENTERED', f"arrived at {m.group(1)}")
            if emit:
                emit({"type": "move", "row": step.after_row, "col": step.after_col,
                      "sx": pos.sx, "sy": pos.sy, "member": member_name})
            if on_step and not crossed:
                on_step(step.after_row, step.after_col, final_grid)
            if emit:
                time.sleep(cfg.move_ms / 1000)
        else:
            if emit:
                emit({"type": "move", "row": step.after_row, "col": step.after_col,
                      "sx": pos.sx, "sy": pos.sy, "member": member_name})
            if on_step and not crossed:
                on_step(step.after_row, step.after_col, final_grid)
            if emit:
                time.sleep(cfg.move_ms / 1000)

    return result.stop_reason, final_grid, crossed


# ── TIER 1: goal-setting ──────────────────────────────────────────────────────

def _run_goal_setting(party: list, pos: PartyPos, db, navlog: NavLog,
                      journals: dict, tick: int,
                      previous_goal: Goal | None = None, emit=None,
                      leader_idx: int = 0) -> tuple:
    """Goal-setting LLM call with rotating leader.

    Returns (goal, next_leader_idx).  The leader rotates through alive members
    so every party member gets a turn proposing goals.  Falls back to 'explore
    north' if LLM fails or party is wiped.
    """
    alive = [m for m in party if m.alive]
    fallback_goal = Goal(
        goal_type='explore',
        target_sx=pos.sx,
        target_sy=pos.sy - 3,
        reasoning='default: heading north',
    )
    if not alive:
        return fallback_goal, leader_idx

    leader = alive[leader_idx % len(alive)]
    next_leader_idx = (leader_idx + 1) % len(alive)
    ctx = build_curated_context(None, navlog, db, pos.world_seed,
                                party, pos.sx, pos.sy)

    sys_prompt = build_goal_system_prompt(leader)
    user_prompt = build_goal_context(leader, ctx, previous_goal=previous_goal)

    fallback_dict = {
        "goal_type": "explore",
        "target_sx": pos.sx,
        "target_sy": pos.sy - 3,
        "reasoning": "default: heading north",
    }
    reprompt = (
        "You MUST output exactly one JSON object, nothing else.\n"
        '  {"goal_type": "explore"|"travel", '
        '"target_sx": int, "target_sy": int, "reasoning": "..."}'
    )

    decision = ask_with_retry(user_prompt, sys_prompt, parse_goal_decision,
                              reprompt, fallback_dict)
    d = decision.result
    goal = Goal(goal_type=d["goal_type"], target_sx=d["target_sx"],
                target_sy=d["target_sy"], reasoning=d["reasoning"])

    tag = " [FALLBACK]" if decision.used_fallback else ""
    print(f"\nGOAL SET{tag} [{leader.name}]: {goal.summary()}", flush=True)
    event_desc = f"{goal.goal_type} → ({goal.target_sx},{goal.target_sy}): {goal.reasoning[:60]}"
    navlog.append(tick, 'GOAL', event_desc)
    journals_append(journals, tick, 'GOAL', event_desc)
    if emit:
        emit({"type": "goal", "text": f"[{leader.name}] {goal.summary()}"})
    return goal, next_leader_idx


# ── TIER 3: checkpoint discussion ─────────────────────────────────────────────

@dataclass
class CheckpointOutcome:
    decision: str           # 'continue' | 'abandon' | 'modify'
    new_goal: Goal | None   # populated when decision == 'modify'
    reasoning: str


def _run_checkpoint(reason: str, active_goal: Goal | None, ctx: CuratedContext,
                    party: list, pos: PartyPos, navlog: NavLog, journals: dict,
                    tick: int, emit=None, leader_idx: int = 0) -> CheckpointOutcome:
    """Run single-leader checkpoint discussion with rotating leader.

    Returns a CheckpointOutcome whose decision drives what happens to active_goal:
      continue — caller keeps active_goal active
      abandon  — caller marks active_goal abandoned
      modify   — caller replaces active_goal with outcome.new_goal
    """
    alive = [m for m in party if m.alive]
    hp_summary = ", ".join(f"{m.name} {m.hp}/{m.max_hp}" for m in alive)
    goal_text = active_goal.summary() if active_goal else "none"

    print(f"\n{'─'*60}", flush=True)
    print(f"CHECKPOINT [{reason}]  t={tick}  screen=({pos.sx},{pos.sy})", flush=True)
    print(f"  Goal: {goal_text}", flush=True)
    print(f"  Party HP: {hp_summary}", flush=True)

    # Fallback if party wiped or LLM fails
    fallback_decision = 'abandon'
    fallback_dict = {'decision': fallback_decision, 'reasoning': 'fallback'}

    if not alive:
        print(f"  Party wiped — abandoning.", flush=True)
        print(f"{'─'*60}", flush=True)
        _log_checkpoint(navlog, journals, tick, reason, fallback_decision, None)
        if emit:
            emit({"type": "checkpoint", "reason": reason, "goal": goal_text,
                  "decision": fallback_decision, "reasoning": "party wiped"})
        return CheckpointOutcome(decision=fallback_decision, new_goal=None,
                                 reasoning="party wiped")

    leader = alive[leader_idx % len(alive)]
    sys_prompt = build_checkpoint_system_prompt(leader)
    user_prompt = build_checkpoint_context(leader, ctx, reason)
    reprompt = (
        "You MUST output exactly one JSON object, nothing else.\n"
        '  {"decision": "continue"|"abandon"|"modify", '
        '"goal_type": "explore"|"travel", "target_sx": int, "target_sy": int, '
        '"reasoning": "..."}\n'
        "  (goal_type/target only required when decision is modify)"
    )

    result = ask_with_retry(user_prompt, sys_prompt, parse_checkpoint_decision,
                            reprompt, fallback_dict)
    d = result.result
    decision = d['decision']
    reasoning = d.get('reasoning', '')

    tag = " [FALLBACK]" if result.used_fallback else ""
    print(f"  Decision{tag} [{leader.name}]: {decision} — {reasoning}", flush=True)
    print(f"{'─'*60}", flush=True)

    new_goal: Goal | None = None
    if decision == 'modify':
        new_goal = Goal(goal_type=d['goal_type'], target_sx=d['target_sx'],
                        target_sy=d['target_sy'], reasoning=d['reasoning'])
        print(f"  → New goal: {new_goal.summary()}", flush=True)

    _log_checkpoint(navlog, journals, tick, reason, decision, new_goal)
    if emit:
        emit({"type": "checkpoint", "reason": reason, "goal": goal_text,
              "decision": decision, "reasoning": reasoning})

    return CheckpointOutcome(decision=decision, new_goal=new_goal, reasoning=reasoning)


def _log_checkpoint(navlog: NavLog, journals: dict, tick: int,
                    reason: str, decision: str, new_goal: Goal | None) -> None:
    desc = f"{reason}: {decision}"
    if new_goal:
        desc += f" → ({new_goal.target_sx},{new_goal.target_sy})"
    navlog.append(tick, 'CHECKPOINT', desc)
    journals_append(journals, tick, 'CHECKPOINT', desc)


def _interior_passable(grid_dict: dict):
    """Return a passability checker for the interior grid dict."""
    def check(r, c):
        tile = grid_dict.get((r, c))
        if tile is None:
            return False
        base = tile.split(':', 1)[0] if ':' in tile else tile
        return is_passable(base)
    return check


def _interior_bfs(grid_dict: dict, start: tuple, goal: tuple) -> list:
    """BFS from start to goal within an interior grid dict.

    Returns an ordered list of (row, col) waypoints including goal, or [] if
    no path exists.  Only cells present in grid_dict and passable are traversable.
    """
    from collections import deque
    if not grid_dict or goal is None:
        return []

    passable = _interior_passable(grid_dict)
    if not passable(*goal):
        return []

    visited = {start}
    q = deque([(start, [])])
    while q:
        (r, c), path = q.popleft()
        if (r, c) == goal:
            return path + [(r, c)]
        for dr, dc in [(-1, 0), (1, 0), (0, 1), (0, -1)]:
            nb = (r + dr, c + dc)
            if nb not in visited and passable(*nb):
                visited.add(nb)
                q.append((nb, path + [(r, c)]))
    return []


def _interior_find_far_tile(grid_dict: dict, start: tuple,
                             exclude: tuple | None = None,
                             max_dist: int = 50) -> tuple:
    """BFS-flood from start; return the farthest reachable tile within max_dist.

    Caps exploration distance so interior traversal stays under ~20 seconds.
    """
    from collections import deque
    passable = _interior_passable(grid_dict)
    visited = {start}
    q = deque([(start, 0)])
    farthest = (start, 0)
    while q:
        pos, dist = q.popleft()
        if dist >= max_dist:
            continue
        if pos != exclude and dist >= farthest[1]:
            farthest = (pos, dist)
        for dr, dc in [(-1, 0), (1, 0), (0, 1), (0, -1)]:
            nb = (pos[0] + dr, pos[1] + dc)
            if nb not in visited and passable(*nb):
                visited.add(nb)
                q.append((nb, dist + 1))
    return farthest[0]


MAX_ENEMIES_PER_ROOM = 3


def _place_interior_enemies(pool, rooms, grid_dict, feature_id, min_r, min_c):
    """Place 0-MAX_ENEMIES_PER_ROOM enemies per cave room from pool on walkable tiles.

    Returns (agents, grid2d) where agents are in grid2d-space (0-based offset from
    min_r/min_c) and grid2d is the compact 2D tile list for update_enemies().
    Each room gets its own deterministic RNG seeded from feature_id ^ room_idx.
    """
    max_r = max(r for r, c in grid_dict)
    max_c = max(c for r, c in grid_dict)
    grid2d = [[None] * (max_c - min_c + 1) for _ in range(max_r - min_r + 1)]
    for (r, c), tile in grid_dict.items():
        grid2d[r - min_r][c - min_c] = tile

    if not pool or not rooms:
        return [], grid2d

    agents = []
    taken = set()
    global_idx = 0
    fid = abs(int(feature_id))

    for room_idx, room in enumerate(rooms):
        rng = random.Random((fid ^ (room_idx * 0x9E3779B9 + 1)) & 0xFFFFFFFFFFFFFFFF)
        count = rng.randint(0, MAX_ENEMIES_PER_ROOM)
        if count == 0:
            continue

        walkable = []
        for dr in range(room['h']):
            for dc in range(room['w']):
                g_r = room['r0'] + dr - min_r
                g_c = room['c0'] + dc - min_c
                if (g_r, g_c) not in taken and is_passable(
                        grid_dict.get((room['r0'] + dr, room['c0'] + dc))):
                    walkable.append((g_r, g_c))
        if not walkable:
            continue

        rng.shuffle(walkable)
        for pos_g in walkable[:count]:
            taken.add(pos_g)
            db_row = pool[rng.randrange(len(pool))]
            agents.append(EnemyAgent(
                index=global_idx,
                row=pos_g[0],
                col=pos_g[1],
                behavior_type=db_row['behavior_type'],
                behavior_axis=db_row.get('behavior_axis'),
                pace_dir=1,
                activated=False,
                npc_sprite=db_row['npc_sprite'],
                name=db_row['name'],
                iq=db_row.get('iq', 80),
                weight=db_row.get('weight', 200),
                sweat=db_row.get('sweat', 4),
                hair=db_row.get('hair', 0),
                level=db_row.get('level', 1),
            ))
            global_idx += 1

    return agents, grid2d


def _run_interior_loop(interior: Interior, pos: PartyPos,
                       emit=None, render_interior_fn=None,
                       party: list | None = None,
                       cave_enemy_pool: list | None = None,
                       enemy_rng=None,
                       data_dir: Path | None = None) -> None:
    """Navigate the party through an interior: spawn → wander → exit.

    Renders the interior PNG on first visit (cached), shows it on the web
    canvas via interior_init, walks the party along a BFS path to the exit
    tile emitting interior_move events, then emits interior_exit so the
    client switches back to the overworld view.

    No LLM calls — purely deterministic.  Combat and NPC interaction are
    roadmap items.
    """
    _CAVE_PNG_PAD = 2  # matches CANVAS_PAD in cavegen.py

    grid_dict = interior.combined_grid()
    spawn = interior.spawn
    exit_tile = interior.entry_tile

    if not spawn:
        return

    # Compute PNG-relative origin so emitted row/col match the rendered image.
    # Cave PNGs are cropped to content bounding box plus CANVAS_PAD=2 border rows/cols.
    # Town grids start at (0,0) so origin is (0,0) and no translation needed.
    _min_r = _min_c = 0
    if interior.monster_spawn and grid_dict:
        _min_r = min(r for r, c in grid_dict)
        _min_c = min(c for r, c in grid_dict)
        _max_r = max(r for r, c in grid_dict)
        _max_c = max(c for r, c in grid_dict)
        origin_r = _min_r - _CAVE_PNG_PAD
        origin_c = _min_c - _CAVE_PNG_PAD
        img_rows = _max_r + _CAVE_PNG_PAD - origin_r + 1
        img_cols = _max_c + _CAVE_PNG_PAD - origin_c + 1
    else:
        origin_r = origin_c = 0
        if grid_dict:
            img_rows = max(r for r, c in grid_dict) + 1
            img_cols = max(c for r, c in grid_dict) + 1
        else:
            img_rows, img_cols = 1, 1

    # Place cave enemies (per room, drawn from shared screen pool).
    int_enemies: list = []
    grid2d = None
    if interior.monster_spawn and grid_dict and cave_enemy_pool:
        rooms = interior.data.get('rooms', [])
        int_enemies, grid2d = _place_interior_enemies(
            cave_enemy_pool, rooms, grid_dict, interior.feature_id, _min_r, _min_c)
        print(f"  Cave enemies placed: {len(int_enemies)}", flush=True)

    def _emit_int_enemies():
        if emit:
            emit({"type": "enemies", "enemies": [
                {"index": e.index,
                 "row": e.row + _CAVE_PNG_PAD,
                 "col": e.col + _CAVE_PNG_PAD,
                 "npc_sprite": e.npc_sprite,
                 "name": e.name}
                for e in int_enemies
            ]})

    if emit:
        tile_payload = {}
        if render_interior_fn:
            tile_payload = render_interior_fn(
                pos.world_seed, interior.feature_id,
                'town' if not interior.monster_spawn else 'dungeon',
                interior.data)
        party_names = [m.name for m in party if m.alive] if party else []
        emit({'type': 'interior_init',
              'rows': img_rows, 'cols': img_cols,
              'row': spawn[0] - origin_r, 'col': spawn[1] - origin_c,
              'monster_spawn': interior.monster_spawn,
              'party': party_names,
              **tile_payload})
        time.sleep(cfg.interior_entry_ms / 1000)
        _emit_int_enemies()

    kind = "town" if not interior.monster_spawn else "cave"
    print(f"\n  INTERIOR [{kind}]  spawn={spawn}  exit={exit_tile}", flush=True)

    # Navigate: explore farthest reachable tile first, then return to exit.
    # This ensures the party actually sees the interior rather than immediately leaving.
    exit_tuple = tuple(exit_tile) if exit_tile else None
    spawn_tuple = tuple(spawn)
    far = _interior_find_far_tile(grid_dict, spawn_tuple, exclude=exit_tuple,
                                   max_dist=cfg.interior_max_explore_dist)
    print(f"  Exploring to {far} before exit.", flush=True)

    # Phase 1: spawn → farthest exploration point
    path_in = _interior_bfs(grid_dict, spawn_tuple, far)
    # Phase 2: far point → exit
    path_out = _interior_bfs(grid_dict, far, exit_tuple) if exit_tuple else []

    cur = spawn_tuple
    for step_r, step_c in (path_in + path_out):
        cur = (step_r, step_c)
        if emit:
            emit({'type': 'interior_move', 'row': cur[0] - origin_r, 'col': cur[1] - origin_c})

        # Enemy step: move enemies, check collision with party.
        pending_cave_battle = None
        if int_enemies and grid2d is not None and enemy_rng is not None:
            party_g_r = step_r - _min_r
            party_g_c = step_c - _min_c
            collision = next(
                (e for e in int_enemies if e.row == party_g_r and e.col == party_g_c), None)
            int_enemies = update_enemies(
                int_enemies, grid2d, party_g_r, party_g_c, enemy_rng)
            if collision is None:
                collision = next(
                    (e for e in int_enemies if e.row == party_g_r and e.col == party_g_c), None)
            if collision is not None:
                pending_cave_battle = collision
                int_enemies = [e for e in int_enemies if e.index != collision.index]
            _emit_int_enemies()

        if emit:
            time.sleep(cfg.move_ms / 1000)

        if pending_cave_battle is not None and data_dir is not None:
            agent = pending_cave_battle
            bparty = load_battle_party(data_dir)
            benemy = BattleEnemy(
                fighter=Fighter(
                    name=agent.name, iq=agent.iq, weight=agent.weight,
                    sweat=agent.sweat, hair=agent.hair, level=agent.level,
                    is_enemy=True,
                ),
                npc_sprite=agent.npc_sprite,
            )
            run_battle(bparty, benemy, enemy_rng, emit=emit,
                       battle_sleep_ms=cfg.move_ms * 2)
            _emit_int_enemies()

    print(f"  Interior done — returning to overworld at {(pos.row, pos.col)}.", flush=True)
    if emit:
        time.sleep(cfg.interior_exit_prepare_ms / 1000)
        emit({'type': 'interior_exit', 'feature_id': interior.feature_id})
        time.sleep(cfg.interior_exit_complete_ms / 1000)
        # Re-anchor the overworld sprite at the entrance tile
        emit({"type": "move", "row": pos.row, "col": pos.col,
              "sx": pos.sx, "sy": pos.sy, "member": None})


def _enter_interior(pos: PartyPos, db, emit=None,
                    render_interior_fn=None, party: list | None = None,
                    screen_seed: int | None = None,
                    enemy_rng=None, data_dir: Path | None = None) -> None:
    """Generate (or load from cache) the interior at pos and navigate through it.

    Looks up the feature, derives feature_id, dispatches to the right generator,
    runs _run_interior_loop (which shows the interior on-screen and walks to the
    exit tile), then returns.  The party's overworld position is unchanged.
    """
    feature = db.get_feature(pos.world_seed, pos.sx, pos.sy, pos.row, pos.col)
    if feature is None:
        return

    feature_id = compute_feature_id(
        pos.world_seed, pos.sx, pos.sy, pos.row, pos.col)

    ftype = feature.get('feature_type', '')
    tag   = FEATURE_TYPES.get(ftype)
    if tag is None:
        print(f"  [interior] unknown feature type {ftype!r} — skipping.", flush=True)
        return
    if tag == 'town':
        generator     = generate_town_data
        monster_spawn = False
    else:
        generator     = generate_cave_data
        monster_spawn = True

    data = db.get_or_create_interior(pos.world_seed, feature_id, generator)
    interior = Interior(feature_id=feature_id, data=data,
                        monster_spawn=monster_spawn)

    cave_enemy_pool = None
    if monster_spawn and screen_seed is not None:
        cave_enemy_pool = db.list_enemies('cave_screen', screen_seed)

    _run_interior_loop(interior, pos, emit=emit,
                       render_interior_fn=render_interior_fn, party=party,
                       cave_enemy_pool=cave_enemy_pool,
                       enemy_rng=enemy_rng,
                       data_dir=data_dir)


# ── main loop ─────────────────────────────────────────────────────────────────

def run_overworld(world_seed: int, db_path: str = "world.db",
                  emit=None, render_screen_fn=None,
                  render_interior_fn=None) -> None:
    """Main overworld loop. Runs until KeyboardInterrupt.

    world_seed:        logged at launch; determines all procedural generation.
    db_path:           SQLite path for the world DB. Use ':memory:' for ephemeral runs.
    emit:              optional callback(dict) — called with typed event dicts for the
                       web SSE layer. None in CLI mode.
    render_screen_fn:  optional callback(world_seed, sx, sy) → {tileset_url, tile_grid}
                       dict — builds the tile payload for the web layer. None in CLI mode.
    """
    print(f"WORLD SEED: {world_seed}", flush=True)

    data_dir = Path(__file__).parent / "data" / "party"
    party = load_overworld_party(data_dir)
    print(f"Party: {[m.name for m in party]}", flush=True)

    journals = {m.name: MemberJournal(m.name) for m in party}
    navlog = NavLog()
    db = WorldDB(db_path)
    pos = PartyPos(world_seed=world_seed, sx=0, sy=0, col=0, row=0)

    screen = enter_screen(pos, db, generate_screen_data, tick=0)
    grid = json.loads(screen["grid_json"])
    rows, cols = len(grid), len(grid[0])
    exits = json.loads(screen["exits_json"])
    print(f"Screen (0,0): {rows}×{cols}  exits={exits}", flush=True)

    start = _find_connected_start(grid, rows, cols)
    assert start, "no exit-connected walkable tile on screen (0,0)"
    pos.row, pos.col = start
    print(f"Start: row={pos.row} col={pos.col}", flush=True)

    enemy_rng    = random.Random(world_seed ^ 0xDEADBEEF)
    enemy_agents: list = place_enemies(
        db.list_enemies('screen', screen['screen_seed']), grid, screen['screen_seed'])
    pending_battle = None  # EnemyAgent that collided; resolved after execute_proposal returns

    def _emit_enemies():
        if emit:
            emit({"type": "enemies", "enemies": [
                {"index": e.index, "row": e.row, "col": e.col,
                 "npc_sprite": e.npc_sprite, "name": e.name}
                for e in enemy_agents
            ]})

    def _reload_enemies(current_grid):
        nonlocal enemy_agents
        cur = db.get_or_create_screen(world_seed, pos.sx, pos.sy, generate_screen_data)
        raw = db.list_enemies('screen', cur['screen_seed'])
        enemy_agents = place_enemies(raw, current_grid, cur['screen_seed'])
        _emit_enemies()

    def _enemy_step(party_row, party_col, current_grid):
        nonlocal enemy_agents, pending_battle
        # Check if the party stepped onto an enemy tile (party-initiated collision)
        collision = next(
            (e for e in enemy_agents if e.row == party_row and e.col == party_col), None)
        enemy_agents = update_enemies(
            enemy_agents, current_grid, party_row, party_col, enemy_rng)
        # Check if an enemy stepped onto the party tile (enemy-initiated collision)
        if collision is None:
            collision = next(
                (e for e in enemy_agents if e.row == party_row and e.col == party_col), None)
        if collision is not None:
            pending_battle = collision
            return  # skip enemies broadcast — battle takes over
        _emit_enemies()

    def _trigger_battle(agent):
        nonlocal enemy_agents
        bparty = load_battle_party(data_dir)
        benemy = BattleEnemy(
            fighter=Fighter(
                name=agent.name, iq=agent.iq, weight=agent.weight,
                sweat=agent.sweat, hair=agent.hair, level=agent.level,
                is_enemy=True,
            ),
            npc_sprite=agent.npc_sprite,
        )
        run_battle(bparty, benemy, enemy_rng, emit=emit,
                   battle_sleep_ms=cfg.move_ms * 2)
        # Remove the enemy win or flee — it's gone for this visit
        enemy_agents = [e for e in enemy_agents if e.index != agent.index]
        _emit_enemies()

    if emit:
        tile_payload = render_screen_fn(world_seed, 0, 0) if render_screen_fn else {}
        alive0 = [m for m in party if m.alive]
        init_leader = alive0[0].name if alive0 else None
        emit({"type": "init", "sx": 0, "sy": 0, "row": pos.row, "col": pos.col,
              "rows": rows, "cols": cols, "member": init_leader, **tile_payload})
        _emit_enemies()

    active_goal: Goal | None = None
    previous_goal: Goal | None = None
    tick = 0
    movements = 0
    leader_idx = 0  # rotates through alive members for goal-setting & checkpoints
    # Track screens where a branch-point checkpoint has already fired for the
    # current goal, to avoid re-triggering on the same screen.
    branch_points_seen: set[tuple] = set()

    try:
        while True:
            tick += 1
            _alive = [m for m in party if m.alive]
            cur_leader_name = _alive[leader_idx % len(_alive)].name if _alive else None

            # ── TIER 1: ensure active goal ────────────────────────────────────
            if active_goal is None or not active_goal.is_active():
                active_goal, leader_idx = _run_goal_setting(
                    party, pos, db, navlog, journals, tick,
                    previous_goal=previous_goal, emit=emit,
                    leader_idx=leader_idx)
                previous_goal = None
                branch_points_seen.clear()

            # ── Already at goal screen? ───────────────────────────────────────
            if active_goal.at_target(pos.sx, pos.sy):
                active_goal.complete()
                ctx = build_curated_context(active_goal, navlog, db, world_seed,
                                            party, pos.sx, pos.sy)
                outcome = _run_checkpoint('goal_reached', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                # goal_reached is always terminal; 'continue'→Tier-1, 'modify'→new goal
                if outcome.decision == 'modify' and outcome.new_goal:
                    previous_goal = active_goal
                    active_goal = outcome.new_goal
                    branch_points_seen.clear()
                else:
                    previous_goal = active_goal
                    active_goal = None
                continue

            # ── TIER 2: screen-level direction ────────────────────────────────
            screen_row = db.get_or_create_screen(
                pos.world_seed, pos.sx, pos.sy, generate_screen_data)
            screen_exits = json.loads(screen_row['exits_json'])

            next_dir = screen_direction_toward(
                pos.sx, pos.sy,
                active_goal.target_sx, active_goal.target_sy,
                screen_exits)

            if next_dir is None:
                ctx = build_curated_context(active_goal, navlog, db, world_seed,
                                            party, pos.sx, pos.sy)
                outcome = _run_checkpoint('all_exits_blocked', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                active_goal.abandon('all_exits_blocked')
                if outcome.decision == 'modify' and outcome.new_goal:
                    previous_goal = active_goal
                    active_goal = outcome.new_goal
                    branch_points_seen.clear()
                else:
                    previous_goal = active_goal
                    active_goal = None
                continue

            # ── TIER 3: branch-point checkpoint ──────────────────────────────
            # Only fire when both axes are at equal distance (genuine tie);
            # diagonal goals with unequal axes have a clear primary direction.
            branch_key = (pos.sx, pos.sy, active_goal.target_sx, active_goal.target_sy)
            dx = active_goal.target_sx - pos.sx
            dy = active_goal.target_sy - pos.sy
            open_goal_dirs = _goal_directed_open_exits(
                pos.sx, pos.sy,
                active_goal.target_sx, active_goal.target_sy,
                screen_exits)
            if (len(open_goal_dirs) >= 2 and abs(dx) == abs(dy)
                    and branch_key not in branch_points_seen):
                branch_points_seen.add(branch_key)
                ctx = build_curated_context(active_goal, navlog, db, world_seed,
                                            party, pos.sx, pos.sy)
                outcome = _run_checkpoint('branch_point', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                if outcome.decision == 'abandon':
                    active_goal.abandon('branch_point')
                    previous_goal = active_goal
                    active_goal = None
                    continue
                elif outcome.decision == 'modify' and outcome.new_goal:
                    active_goal.abandon('modified')
                    previous_goal = active_goal
                    active_goal = outcome.new_goal
                    # Mark current screen consumed for the new goal so we don't
                    # immediately re-trigger branch_point on the same tile.
                    branch_points_seen.add(
                        (pos.sx, pos.sy,
                         outcome.new_goal.target_sx, outcome.new_goal.target_sy))
                    continue
                # 'continue' — fall through, proceed with next_dir as chosen

            print(f"\n{'─'*60}", flush=True)
            print(f"TICK {tick}  screen=({pos.sx},{pos.sy})  "
                  f"pos=(row={pos.row},col={pos.col})  "
                  f"goal=({active_goal.target_sx},{active_goal.target_sy})  "
                  f"heading={next_dir}", flush=True)
            print(f"{'─'*60}", flush=True)

            # ── Feature detour: visit unvisited enterables before crossing ────
            # bfs_to_exit avoids enterable tiles by design, so we must
            # explicitly route to any unvisited caves/towns on this screen
            # before executing the crossing path.
            screen_feats = db.list_screen_features(pos.world_seed, pos.sx, pos.sy)
            unvisited_feats = [
                f for f in screen_feats
                if f.get('enterable') and not f.get('entered')
                and (f['local_row'], f['local_col']) != (pos.row, pos.col)
                and f.get('feature_type') in FEATURE_TYPES  # skip hub and unknowns
            ]
            if unvisited_feats:
                nearest = min(unvisited_feats,
                              key=lambda f: (abs(f['local_row'] - pos.row)
                                             + abs(f['local_col'] - pos.col)))
                feat_path = bfs_to_tile(grid, pos.row, pos.col,
                                        nearest['local_row'], nearest['local_col'])
                if feat_path and len(feat_path) > 1:
                    print(f"  Detour → {nearest['feature_type']} at "
                          f"({nearest['local_row']},{nearest['local_col']})", flush=True)
                    segments = path_to_segments(feat_path)
                    feat_stop = 'completed'
                    for direction, steps in segments:
                        feat_stop, grid, _crossed = _execute_proposal(
                            pos, direction, steps, grid, db, journals, navlog, tick,
                            generate_screen_data, emit=emit,
                            render_screen_fn=render_screen_fn,
                            member_name=cur_leader_name, on_step=_enemy_step)
                        if _crossed:
                            _reload_enemies(grid)
                        if pending_battle is not None:
                            break
                        if feat_stop in ('blocker', 'enterable'):
                            break
                    if pending_battle is not None:
                        _trigger_battle(pending_battle)
                        pending_battle = None
                        continue
                    if feat_stop == 'enterable':
                        enemy_agents.clear()
                        _enter_interior(pos, db, emit=emit,
                                        render_interior_fn=render_interior_fn,
                                        party=party,
                                        screen_seed=screen_row['screen_seed'],
                                        enemy_rng=enemy_rng, data_dir=data_dir)
                        _reload_enemies(grid)
                        continue  # resume goal after exiting interior

            # ── Within-screen BFS to exit edge ────────────────────────────────
            path = bfs_to_exit(grid, pos.row, pos.col, next_dir)

            if path is None:
                # Landing tile from a screen crossing might be in a disconnected
                # pocket — try relocating to the exit-connected component once.
                rows_g, cols_g = len(grid), len(grid[0])
                reconnect = _find_connected_start(grid, rows_g, cols_g)
                if reconnect and reconnect != (pos.row, pos.col):
                    print(f"  Reconnect: ({pos.row},{pos.col}) → {reconnect}", flush=True)
                    pos.row, pos.col = reconnect
                    path = bfs_to_exit(grid, pos.row, pos.col, next_dir)

            if path is None:
                ctx = build_curated_context(active_goal, navlog, db, world_seed,
                                            party, pos.sx, pos.sy)
                outcome = _run_checkpoint('path_blocked', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                active_goal.abandon('screen_blocked')
                if outcome.decision == 'modify' and outcome.new_goal:
                    previous_goal = active_goal
                    active_goal = outcome.new_goal
                    branch_points_seen.clear()
                else:
                    previous_goal = active_goal
                    active_goal = None
                continue

            # ── Execute within-screen path segments ───────────────────────────
            segments = path_to_segments(path)
            stop = 'completed'
            for direction, steps in segments:
                stop, grid, _crossed = _execute_proposal(
                    pos, direction, steps, grid, db, journals, navlog, tick,
                    generate_screen_data, emit=emit, render_screen_fn=render_screen_fn,
                    member_name=cur_leader_name, on_step=_enemy_step)
                if _crossed:
                    _reload_enemies(grid)
                if pending_battle is not None:
                    break
                if stop in ('blocker', 'enterable'):
                    break
            if pending_battle is not None:
                _trigger_battle(pending_battle)
                pending_battle = None
                continue

            if stop == 'enterable':
                movements += 1
                enemy_agents.clear()
                _enter_interior(pos, db, emit=emit,
                                render_interior_fn=render_interior_fn, party=party,
                                screen_seed=screen_row['screen_seed'],
                                enemy_rng=enemy_rng, data_dir=data_dir)
                _reload_enemies(grid)
                active_goal.abandon('enterable')
                previous_goal = active_goal
                active_goal = None
                continue

            if stop == 'blocker':
                active_goal.abandon('movement_blocked')
                previous_goal = active_goal
                active_goal = None
                continue

            # ── Trigger the screen crossing (one step off the exit edge) ──────
            stop, grid, crossed = _execute_proposal(
                pos, next_dir, 1, grid, db, journals, navlog, tick,
                generate_screen_data, emit=emit, render_screen_fn=render_screen_fn,
                member_name=cur_leader_name, on_step=_enemy_step)

            if crossed:
                _reload_enemies(grid)

            if pending_battle is not None:
                _trigger_battle(pending_battle)
                pending_battle = None
                continue

            if stop == 'enterable':
                movements += 1
                enemy_agents.clear()
                _enter_interior(pos, db, emit=emit,
                                render_interior_fn=render_interior_fn, party=party)
                _reload_enemies(grid)
                active_goal.abandon('enterable')
                previous_goal = active_goal
                active_goal = None
            elif stop == 'blocker':
                active_goal.abandon('crossing_blocked')
                previous_goal = active_goal
                active_goal = None
            else:
                # 'completed' after the crossing step — normal screen advance
                movements += 1

    except KeyboardInterrupt:
        pass

    known = db.list_known_screens(world_seed)
    print(f"\n{'='*60}", flush=True)
    print("SESSION ENDED", flush=True)
    print(f"  Ticks: {tick}  Screen crossings: {movements}", flush=True)
    print(f"  Screens discovered: {len(known)}", flush=True)
    print(f"  Final pos: screen ({pos.sx},{pos.sy})  row={pos.row} col={pos.col}",
          flush=True)
    if active_goal:
        print(f"  Active goal: {active_goal.summary()}", flush=True)

    if party:
        sample = party[0]
        mj = journals[sample.name]
        print(f"\nJOURNAL — {sample.name} (last {len(mj)} entries)", flush=True)
        for line in mj.render():
            print(f"  {line}", flush=True)

    print(f"{'='*60}", flush=True)
