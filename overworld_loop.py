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
from engine.viewscan import scan as _viewscan
from engine.worlddb import WorldDB, compute_feature_id
from engine.party_state import PartyPos, enter_screen, execute_move
from engine.pathfinding import bfs_to_exit, bfs_to_tile, path_to_segments, screen_direction_toward
from engine.scenes import Interior
from engine.tiles import is_enterable, is_passable
from llm.client import ask_with_retry
from llm.prompts import (build_checkpoint_context, build_checkpoint_system_prompt,
                          build_goal_context, build_goal_system_prompt,
                          build_interior_system_prompt, build_interior_goal_context,
                          OVERWORLD_CRITICAL_HP)
from llm.schema import (parse_checkpoint_decision, parse_goal_decision,
                         parse_interior_destination)
from procgen.cavegen import generate_cave_data
from procgen.towngen import generate_town_data
from procgen.worldgen import FEATURE_TYPES, generate_screen_data
from engine.battle import BattleEnemy, load_party as load_battle_party, run_battle
from engine.combat import Fighter, PARTY_HP_BASE, PARTY_HP_PER_LEVEL, PARTY_MP_BASE, PARTY_MP_PER_LEVEL
from engine.enemy_state import EnemyAgent, place_enemies, update_enemies, update_town_npcs


XP_PER_ENEMY_LVL = 30
XP_THRESHOLD_BASE = 80


def xp_threshold(lvl: int) -> int:
    return XP_THRESHOLD_BASE * lvl


@dataclass
class OverworldMember:
    name: str
    lvl: int
    hp: int
    max_hp: int
    personality: str
    xp: int = 0
    mp: int = 0
    max_mp: int = 0
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
            xp=d.get("xp", 0),
            mp=d.get("mp", 0),
            max_mp=d.get("max_mp", 0),
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


def _overlay_party_state(bparty: list, party: list) -> None:
    """Overlay live OverworldMember state onto BattleMember objects before a battle.

    JSON files supply static stats (iq/weight/sweat/hair/special).
    OverworldMember supplies dynamic state (hp/max_hp/mp/max_mp/lvl/xp).
    This ensures battles run with current in-memory state, including any
    healer-restored HP/MP, rather than stale JSON-file values.
    """
    ow = {m.name: m for m in party}
    for bm in bparty:
        o = ow.get(bm.name)
        if not o:
            continue
        bm.fighter.hp      = o.hp
        bm.fighter.max_hp  = o.max_hp
        bm.fighter.mp      = o.mp
        bm.fighter.max_mp  = o.max_mp
        bm.fighter.level   = o.lvl
        bm.xp              = o.xp


def _apply_battle_result(
    bparty: list, party: list, result: dict, enemy_lvl: int
) -> None:
    """Write battle outcome back to OverworldMember list. Caller must call _save()."""
    bm_by_name = {m.name: m for m in bparty}
    leveled_up = []
    for m in party:
        bm = bm_by_name.get(m.name)
        if not bm:
            continue
        m.hp    = bm.hp
        m.mp    = bm.mp
        m.alive = bm.alive
        if result["outcome"] == "win" and bm.alive:
            m.xp += enemy_lvl * XP_PER_ENEMY_LVL
            while m.xp >= xp_threshold(m.lvl):
                m.xp -= xp_threshold(m.lvl)
                m.lvl += 1
                m.max_hp = PARTY_HP_BASE + (m.lvl - 1) * PARTY_HP_PER_LEVEL
                m.max_mp = PARTY_MP_BASE + (m.lvl - 1) * PARTY_MP_PER_LEVEL
                m.hp = min(m.hp, m.max_hp)
                leveled_up.append(m.name)
    if leveled_up:
        print(f"  *** LEVEL UP: {', '.join(leveled_up)} ***", flush=True)


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
                    tick: int, emit=None, leader_idx: int = 0,
                    vs=None, enemies_nearby: list | None = None) -> CheckpointOutcome:
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
    user_prompt = build_checkpoint_context(leader, ctx, reason,
                                           vs=vs, enemies_nearby=enemies_nearby)
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


def _build_interior_pois(interior: Interior, grid_dict: dict) -> list[dict]:
    """Build an ordered list of Points of Interest for interior LLM navigation.

    Each POI: {'name': str, 'label': str, 'row': int, 'col': int}

    Always includes an 'exit' POI.  Towns add a 'healer' POI (adjacent
    walkable tile, not the impassable wall).  Caves get 2-3 explore spots
    at staggered flood depths.
    """
    spawn_tuple = tuple(interior.spawn)
    exit_tuple = tuple(interior.entry_tile) if interior.entry_tile else None
    pois: list[dict] = []

    if not interior.monster_spawn:
        # Town: healer POI — find walkable tile adjacent to healerHutwallMid.
        healer_wall = interior.healer_spawn
        if healer_wall:
            for dr, dc in [(-1, 0), (1, 0), (0, -1), (0, 1)]:
                nr, nc = healer_wall[0] + dr, healer_wall[1] + dc
                tile = grid_dict.get((nr, nc))
                if tile is not None:
                    base = tile.split(':', 1)[0] if ':' in tile else tile
                    if is_passable(base):
                        pois.append({'name': 'healer', 'row': nr, 'col': nc,
                                     'label': 'visit the healer (restores HP and MP)'})
                        break

        # Town: 1 explore spot at the far end.
        far1 = _interior_find_far_tile(grid_dict, spawn_tuple, exclude=exit_tuple,
                                        max_dist=cfg.interior_max_explore_dist)
        if far1 != spawn_tuple and far1 != exit_tuple:
            pois.append({'name': 'explore_A', 'row': far1[0], 'col': far1[1],
                         'label': 'explore the town'})
    else:
        # Cave: 2-3 explore spots at staggered depths.
        half_dist = cfg.interior_max_explore_dist // 2
        far1 = _interior_find_far_tile(grid_dict, spawn_tuple, exclude=exit_tuple,
                                        max_dist=cfg.interior_max_explore_dist)
        if far1 != spawn_tuple and far1 != exit_tuple:
            pois.append({'name': 'explore_A', 'row': far1[0], 'col': far1[1],
                         'label': 'explore the far end of the cave'})

        far2 = _interior_find_far_tile(grid_dict, spawn_tuple, exclude=far1,
                                        max_dist=half_dist)
        if far2 != spawn_tuple and far2 not in (far1, exit_tuple):
            pois.append({'name': 'explore_B', 'row': far2[0], 'col': far2[1],
                         'label': 'explore another section'})

    # Always: exit.
    if exit_tuple:
        pois.append({'name': 'exit', 'row': exit_tuple[0], 'col': exit_tuple[1],
                     'label': 'leave and return to the overworld'})

    return pois


def _do_healer_interaction(party: list, emit=None) -> None:
    """Restore full HP and MP to all living party members."""
    healed = []
    for m in party:
        if m.alive and (m.hp < m.max_hp or m.mp < m.max_mp):
            m.hp = m.max_hp
            m.mp = m.max_mp
            healed.append(m.name)
    if healed:
        print(f"  HEALER: restored HP and MP to {', '.join(healed)}", flush=True)
    else:
        print("  HEALER: party already at full HP and MP.", flush=True)


def _nearby_enemies(enemy_agents: list, party_row: int, party_col: int,
                    radius: int = 8) -> list[dict]:
    """Return enemies within Manhattan distance, sorted nearest-first."""
    dirs = {(True, True): 'northeast', (True, False): 'southeast',
            (False, True): 'northwest', (False, False): 'southwest'}
    result = []
    for e in enemy_agents:
        dr = e.row - party_row
        dc = e.col - party_col
        d = abs(dr) + abs(dc)
        if d == 0 or d > radius:
            continue
        if abs(dr) > abs(dc):
            direction = 'north' if dr < 0 else 'south'
        elif abs(dc) > abs(dr):
            direction = 'west' if dc < 0 else 'east'
        else:
            direction = dirs[(dr < 0, dc > 0)]
        result.append({'name': e.name, 'distance': d, 'direction': direction})
    return sorted(result, key=lambda x: x['distance'])


def _run_interior_loop(interior: Interior, pos: PartyPos,
                       emit=None, render_interior_fn=None,
                       party: list | None = None,
                       cave_enemy_pool: list | None = None,
                       town_npc_pool: list | None = None,
                       enemy_rng=None,
                       data_dir: Path | None = None,
                       render_npc_sprite_fn=None,
                       leader_idx: int = 0,
                       db=None) -> int:
    """Navigate the party through an interior with LLM-driven destination picks.

    The leader LLM chooses from a menu of POIs (healer, explore spots, exit).
    BFS executes each chosen path. On arrival the LLM picks again until it
    chooses 'exit'. Returns the updated leader_idx after all LLM calls.
    """
    _CAVE_PNG_PAD = 2  # matches CANVAS_PAD in cavegen.py

    grid_dict = interior.combined_grid()
    spawn = interior.spawn
    exit_tile = interior.entry_tile

    if not spawn:
        return leader_idx

    # Compute PNG-relative origin so emitted row/col match the rendered image.
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
        cb = interior.data.get('crop_box', [0, 0, 0, 0])
        origin_r, origin_c = cb[0], cb[1]
        img_rows = cb[2] - cb[0]
        img_cols = cb[3] - cb[1]

    # Place cave enemies (per room, drawn from shared screen pool).
    int_enemies: list = []
    grid2d = None
    if interior.monster_spawn and grid_dict and cave_enemy_pool:
        rooms = interior.data.get('rooms', [])
        int_enemies, grid2d = _place_interior_enemies(
            cave_enemy_pool, rooms, grid_dict, interior.feature_id, _min_r, _min_c)
        print(f"  Cave enemies placed: {len(int_enemies)}", flush=True)

    # Place town NPCs (friendly; no battles).
    town_npcs: list = []
    town_grid2d = None
    t_r0 = t_c0 = 0
    if not interior.monster_spawn and grid_dict and town_npc_pool:
        cb = interior.data.get('crop_box', [0, 0, 0, 0])
        t_r0, t_c0, t_r1, t_c1 = cb
        t_rows, t_cols = t_r1 - t_r0, t_c1 - t_c0
        town_grid2d = [[None] * t_cols for _ in range(t_rows)]
        for (r, c), tile in grid_dict.items():
            ri, ci = r - t_r0, c - t_c0
            if 0 <= ri < t_rows and 0 <= ci < t_cols:
                town_grid2d[ri][ci] = tile

        healer_db = next((r for r in town_npc_pool if r['enemy_index'] == 0), None)
        healer_abs = interior.healer_spawn
        if healer_db and healer_abs:
            raw_pal = healer_db.get('sprite_palette_json')
            town_npcs.append(EnemyAgent(
                index=0,
                row=healer_abs[0] - t_r0, col=healer_abs[1] - t_c0,
                behavior_type=3, behavior_axis=None, pace_dir=1, activated=False,
                npc_sprite=healer_db['npc_sprite'], name=healer_db['name'],
                iq=healer_db.get('iq', 80), weight=healer_db.get('weight', 200),
                sweat=healer_db.get('sweat', 4), hair=healer_db.get('hair', 0),
                level=healer_db.get('level', 1),
                sprite_palette=json.loads(raw_pal) if raw_pal else None,
            ))

        other_rows = [r for r in town_npc_pool if r['enemy_index'] != 0]
        if other_rows:
            placed = place_enemies(other_rows, town_grid2d,
                                   interior.feature_id ^ 0xABCD1234)
            for i, a in enumerate(placed):
                a.index = i + 1
            town_npcs.extend(placed)

        print(f"  Town NPCs placed: {len(town_npcs)}", flush=True)

    def _emit_int_enemies():
        if emit:
            enemy_list = []
            for e in int_enemies:
                d = {"index": e.index,
                     "row": e.row + _CAVE_PNG_PAD,
                     "col": e.col + _CAVE_PNG_PAD,
                     "npc_sprite": e.npc_sprite,
                     "name": e.name}
                if render_npc_sprite_fn and e.sprite_palette:
                    url = render_npc_sprite_fn(e.npc_sprite, e.sprite_palette)
                    if url:
                        d["sprite_url"] = url
                enemy_list.append(d)
            for e in town_npcs:
                d = {"index": e.index + 1000,
                     "row": e.row,
                     "col": e.col,
                     "npc_sprite": e.npc_sprite,
                     "name": e.name,
                     "anim_ms": 500}
                if render_npc_sprite_fn and e.sprite_palette:
                    url = render_npc_sprite_fn(e.npc_sprite, e.sprite_palette)
                    if url:
                        d["sprite_url"] = url
                enemy_list.append(d)
            emit({"type": "enemies", "enemies": enemy_list})

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

    spawn_tuple = tuple(spawn)
    exit_tuple = tuple(exit_tile) if exit_tile else None
    pois = _build_interior_pois(interior, grid_dict)
    print(f"  POIs: {[p['name'] for p in pois]}", flush=True)

    # Per-step walk helper — captures all non-local state via closure.
    def _do_walk(path: list) -> None:
        nonlocal int_enemies, town_npcs
        for step_r, step_c in path:
            if emit:
                emit({'type': 'interior_move',
                      'row': step_r - origin_r, 'col': step_c - origin_c})

            if town_npcs and town_grid2d is not None and enemy_rng is not None:
                town_npcs = update_town_npcs(town_npcs, town_grid2d, enemy_rng)
                _emit_int_enemies()

            pending_cave_battle = None
            if int_enemies and grid2d is not None and enemy_rng is not None:
                party_g_r = step_r - _min_r
                party_g_c = step_c - _min_c
                collision = next(
                    (e for e in int_enemies if e.row == party_g_r and e.col == party_g_c),
                    None)
                int_enemies = update_enemies(
                    int_enemies, grid2d, party_g_r, party_g_c, enemy_rng)
                if collision is None:
                    collision = next(
                        (e for e in int_enemies
                         if e.row == party_g_r and e.col == party_g_c), None)
                if collision is not None:
                    pending_cave_battle = collision
                    int_enemies = [e for e in int_enemies if e.index != collision.index]
                _emit_int_enemies()

            if emit:
                time.sleep(cfg.move_ms / 1000)

            if pending_cave_battle is not None and data_dir is not None:
                agent = pending_cave_battle
                bparty = load_battle_party(data_dir)
                _overlay_party_state(bparty, party)
                cave_sprite_url = ""
                if render_npc_sprite_fn and agent.sprite_palette:
                    cave_sprite_url = (
                        render_npc_sprite_fn(agent.npc_sprite, agent.sprite_palette) or "")
                benemy = BattleEnemy(
                    fighter=Fighter(
                        name=agent.name, iq=agent.iq, weight=agent.weight,
                        sweat=agent.sweat, hair=agent.hair, level=agent.level,
                        is_enemy=True,
                    ),
                    npc_sprite=agent.npc_sprite,
                    sprite_url=cave_sprite_url,
                )
                cave_result = run_battle(bparty, benemy, enemy_rng, emit=emit,
                                         battle_sleep_ms=cfg.move_ms * 2)
                _apply_battle_result(bparty, party, cave_result, agent.level)
                if db is not None:
                    db.party_level = max(1, round(sum(m.lvl for m in party) / len(party)))
                _emit_int_enemies()

    # ── LLM-driven destination loop ───────────────────────────────────────────
    current_pos = spawn_tuple
    visited_names: list[str] = []
    reason = 'entered'

    while True:
        available = [p for p in pois if p['name'] not in visited_names]
        if not available:
            break

        alive = [m for m in party if m.alive] if party else []
        if alive:
            leader = alive[leader_idx % len(alive)]
            sys_p = build_interior_system_prompt(leader, kind)
            user_p = build_interior_goal_context(
                leader, kind, reason, available, visited_names, party)
            avail_names = [p['name'] for p in available]
            fallback_name = avail_names[-1]  # prefer exit (usually last) as fallback
            fallback_dict = {'target': fallback_name, 'reasoning': 'fallback'}
            reprompt = (
                f'Output exactly: {{"target": "{fallback_name}", "reasoning": "..."}}'
            )
            result = ask_with_retry(
                user_p, sys_p,
                lambda raw, an=avail_names: parse_interior_destination(raw, an),
                reprompt, fallback_dict)
            target_name = result.result['target']
            reasoning = result.result.get('reasoning', '')
            tag = ' [FALLBACK]' if result.used_fallback else ''
            print(f"  INTERIOR{tag} [{leader.name}]: → {target_name} — {reasoning}",
                  flush=True)
            leader_idx = (leader_idx + 1) % max(len(alive), 1)
        else:
            # No alive party — head straight to exit.
            exit_names = [p['name'] for p in available if p['name'] == 'exit']
            target_name = exit_names[0] if exit_names else available[0]['name']

        target_poi = next(p for p in pois if p['name'] == target_name)
        path = _interior_bfs(grid_dict, current_pos,
                             (target_poi['row'], target_poi['col']))

        if not path:
            print(f"  No BFS path to {target_name} — skipping.", flush=True)
            visited_names.append(target_name)
            reason = f'no path to {target_name}'
            continue

        _do_walk(path)
        current_pos = (target_poi['row'], target_poi['col'])
        visited_names.append(target_name)

        if target_name == 'exit':
            break

        if target_name == 'healer' and not interior.monster_spawn and party:
            _do_healer_interaction(party, emit=emit)

        reason = f'arrived at {target_name}'

    # ── Exit ──────────────────────────────────────────────────────────────────
    print(f"  Interior done — returning to overworld at {(pos.row, pos.col)}.", flush=True)
    if emit:
        time.sleep(cfg.interior_exit_prepare_ms / 1000)
        emit({'type': 'interior_exit', 'feature_id': interior.feature_id})
        time.sleep(cfg.interior_exit_complete_ms / 1000)
        emit({"type": "move", "row": pos.row, "col": pos.col,
              "sx": pos.sx, "sy": pos.sy, "member": None})

    return leader_idx


def _enter_interior(pos: PartyPos, db, emit=None,
                    render_interior_fn=None, party: list | None = None,
                    screen_seed: int | None = None,
                    enemy_rng=None, data_dir: Path | None = None,
                    render_npc_sprite_fn=None,
                    leader_idx: int = 0) -> int:
    """Generate (or load from cache) the interior at pos and navigate through it.

    Returns the updated leader_idx after interior LLM calls.
    """
    feature = db.get_feature(pos.world_seed, pos.sx, pos.sy, pos.row, pos.col)
    if feature is None:
        return leader_idx

    feature_id = compute_feature_id(
        pos.world_seed, pos.sx, pos.sy, pos.row, pos.col)

    ftype = feature.get('feature_type', '')
    tag   = FEATURE_TYPES.get(ftype)
    if tag is None:
        print(f"  [interior] unknown feature type {ftype!r} — skipping.", flush=True)
        return leader_idx
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

    town_npc_pool = None
    if not monster_spawn:
        town_npc_pool = db.get_or_create_town_npcs(feature_id)

    return _run_interior_loop(interior, pos, emit=emit,
                               render_interior_fn=render_interior_fn, party=party,
                               cave_enemy_pool=cave_enemy_pool,
                               town_npc_pool=town_npc_pool,
                               enemy_rng=enemy_rng,
                               data_dir=data_dir,
                               render_npc_sprite_fn=render_npc_sprite_fn,
                               leader_idx=leader_idx,
                               db=db)


# ── main loop ─────────────────────────────────────────────────────────────────

def run_overworld(world_seed: int, db_path: str = "world.db",
                  emit=None, render_screen_fn=None,
                  render_interior_fn=None,
                  render_npc_sprite_fn=None,
                  session: dict | None = None) -> None:
    """Main overworld loop. Runs until KeyboardInterrupt.

    world_seed:           logged at launch; determines all procedural generation.
    db_path:              SQLite path for the world DB. Use ':memory:' for ephemeral runs.
    emit:                 optional callback(dict) — called with typed event dicts for the
                          web SSE layer. None in CLI mode.
    render_screen_fn:     optional callback(world_seed, sx, sy) → {tileset_url, tile_grid}
                          dict — builds the tile payload for the web layer. None in CLI mode.
    render_npc_sprite_fn: optional callback(npc_key, palette) → sprite_url string.
                          Generates a cached recolored NPC sprite strip. None in CLI mode.
    session:              optional saved session dict from WorldDB.load_session(); when
                          provided the party resumes at the saved position instead of (0,0).
    """
    print(f"WORLD SEED: {world_seed}", flush=True)

    data_dir = Path(__file__).parent / "data" / "party"
    party = load_overworld_party(data_dir)
    print(f"Party: {[m.name for m in party]}", flush=True)

    journals = {m.name: MemberJournal(m.name) for m in party}
    navlog = NavLog()
    db = WorldDB(db_path)

    # ── Restore session or start fresh ────────────────────────────────────────
    if session is not None:
        pos = PartyPos(
            world_seed=world_seed,
            sx=session["sx"], sy=session["sy"],
            row=session["row"], col=session["col"],
        )
        tick       = session.get("tick", 0)
        leader_idx = session.get("leader_idx", 0)

        # Restore party HP, MP, XP, and level
        hp_by_name = {m["name"]: m for m in session.get("party", [])}
        for m in party:
            saved = hp_by_name.get(m.name)
            if saved:
                m.hp    = saved["hp"]
                m.alive = saved["alive"]
                m.xp    = saved.get("xp", m.xp)
                m.mp    = saved.get("mp", m.mp)
                if saved.get("lvl", m.lvl) != m.lvl:
                    m.lvl    = saved["lvl"]
                    m.max_hp = PARTY_HP_BASE + (m.lvl - 1) * PARTY_HP_PER_LEVEL
                    m.max_mp = PARTY_MP_BASE + (m.lvl - 1) * PARTY_MP_PER_LEVEL

        # Restore active goal
        goal_data = session.get("goal")
        active_goal = Goal.from_dict(goal_data) if goal_data and goal_data.get("status") == "active" else None
        previous_goal: Goal | None = None

        # Restore navlog
        for entry in session.get("navlog", []):
            navlog.append(entry["tick"], entry["event_type"], entry["desc"])

        # Restore per-member journals
        for name, entries in session.get("journals", {}).items():
            mj = journals.get(name)
            if mj:
                for entry in entries:
                    mj.append(entry["tick"], entry["event_type"], entry["desc"])

        screen = enter_screen(pos, db, generate_screen_data, tick=tick)
        grid = json.loads(screen["grid_json"])
        rows, cols = len(grid), len(grid[0])
        print(f"RESUMED at screen ({pos.sx},{pos.sy})  row={pos.row} col={pos.col}  tick={tick}",
              flush=True)
        if active_goal:
            print(f"  Goal restored: {active_goal.summary()}", flush=True)
    else:
        pos = PartyPos(world_seed=world_seed, sx=0, sy=0, col=0, row=0)
        tick        = 0
        leader_idx  = 0
        active_goal: Goal | None = None
        previous_goal: Goal | None = None

        screen = enter_screen(pos, db, generate_screen_data, tick=0)
        grid = json.loads(screen["grid_json"])
        rows, cols = len(grid), len(grid[0])
        exits = json.loads(screen["exits_json"])
        print(f"Screen (0,0): {rows}×{cols}  exits={exits}", flush=True)

        start = _find_connected_start(grid, rows, cols)
        assert start, "no exit-connected walkable tile on screen (0,0)"
        pos.row, pos.col = start
        print(f"Start: row={pos.row} col={pos.col}", flush=True)

    # Set party_level after session restore so resumed parties use their actual level
    db.party_level = max(1, round(sum(m.lvl for m in party) / len(party)))

    enemy_rng    = random.Random(world_seed ^ 0xDEADBEEF)
    enemy_agents: list = place_enemies(
        db.list_enemies('screen', screen['screen_seed']), grid, screen['screen_seed'])
    pending_battle = None  # EnemyAgent that collided; resolved after execute_proposal returns

    def _emit_enemies():
        if emit:
            enemy_list = []
            for e in enemy_agents:
                d = {"index": e.index, "row": e.row, "col": e.col,
                     "npc_sprite": e.npc_sprite, "name": e.name}
                if e.overworld_sprite:
                    d["overworld_sprite"] = e.overworld_sprite
                if render_npc_sprite_fn and e.sprite_palette:
                    url = render_npc_sprite_fn(e.npc_sprite, e.sprite_palette)
                    if url:
                        d["sprite_url"] = url
                enemy_list.append(d)
            emit({"type": "enemies", "enemies": enemy_list})

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

    def _respawn_at_hub():
        nonlocal active_goal, previous_goal, grid, rows, cols, enemy_agents
        print("\n  *** PARTY WIPE — respawning at hub. ***\n", flush=True)
        for m in party:
            m.alive = True
            m.hp = max(1, m.max_hp // 2)
            m.mp = max(0, m.max_mp // 2)
        pos.sx, pos.sy = 0, 0
        hub_screen = enter_screen(pos, db, generate_screen_data, tick=tick)
        grid = json.loads(hub_screen["grid_json"])
        rows, cols = len(grid), len(grid[0])
        start = _find_connected_start(grid, rows, cols)
        if start:
            pos.row, pos.col = start
        active_goal = None
        previous_goal = None
        enemy_agents = place_enemies(
            db.list_enemies('screen', hub_screen['screen_seed']), grid, hub_screen['screen_seed'])
        if emit:
            alive_members = [m for m in party if m.alive]
            leader_name = alive_members[0].name if alive_members else None
            tile_payload = render_screen_fn(world_seed, 0, 0) if render_screen_fn else {}
            emit({"type": "init", "sx": 0, "sy": 0, "row": pos.row, "col": pos.col,
                  "rows": rows, "cols": cols, "member": leader_name, **tile_payload})
            _emit_enemies()
        _save()

    def _trigger_battle(agent):
        nonlocal enemy_agents, active_goal, previous_goal, leader_idx, branch_points_seen
        bparty = load_battle_party(data_dir)
        _overlay_party_state(bparty, party)
        sprite_url = ""
        if render_npc_sprite_fn and agent.sprite_palette:
            sprite_url = render_npc_sprite_fn(agent.npc_sprite, agent.sprite_palette) or ""
        benemy = BattleEnemy(
            fighter=Fighter(
                name=agent.name, iq=agent.iq, weight=agent.weight,
                sweat=agent.sweat, hair=agent.hair, level=agent.level,
                is_enemy=True,
            ),
            npc_sprite=agent.npc_sprite,
            sprite_url=sprite_url,
        )
        battle_result = run_battle(bparty, benemy, enemy_rng, emit=emit,
                                   battle_sleep_ms=cfg.move_ms * 2)
        _apply_battle_result(bparty, party, battle_result, agent.level)
        db.party_level = max(1, round(sum(m.lvl for m in party) / len(party)))
        if battle_result["outcome"] == "loss":
            _respawn_at_hub()
            return
        # Remove the enemy on win or flee — it's gone for this visit
        enemy_agents = [e for e in enemy_agents if e.index != agent.index]
        _emit_enemies()
        # Post-battle health checkpoint: fire when anyone is dead or critically low HP
        alive = [m for m in party if m.alive]
        health_critical = (
            any(not m.alive for m in party) or
            any(m.max_hp > 0 and m.hp / m.max_hp < OVERWORLD_CRITICAL_HP for m in alive)
        )
        if health_critical and alive:
            ctx = build_curated_context(active_goal, navlog, db, world_seed,
                                        party, pos.sx, pos.sy)
            vs  = _viewscan(grid, pos.row, pos.col)
            en  = _nearby_enemies(enemy_agents, pos.row, pos.col)
            outcome = _run_checkpoint('battle_wounds', active_goal, ctx, party,
                                      pos, navlog, journals, tick, emit=emit,
                                      leader_idx=leader_idx, vs=vs,
                                      enemies_nearby=en or None)
            leader_idx = (leader_idx + 1) % max(len(alive), 1)
            if outcome.decision == 'abandon':
                if active_goal:
                    active_goal.abandon('battle_wounds')
                previous_goal = active_goal
                active_goal = None
            elif outcome.decision == 'modify' and outcome.new_goal:
                if active_goal:
                    active_goal.abandon('battle_wounds_modify')
                previous_goal = active_goal
                active_goal = outcome.new_goal
                branch_points_seen.clear()
            # 'continue' → keep active_goal as-is
        _save()

    if emit:
        tile_payload = render_screen_fn(world_seed, 0, 0) if render_screen_fn else {}
        alive0 = [m for m in party if m.alive]
        init_leader = alive0[0].name if alive0 else None
        emit({"type": "init", "sx": 0, "sy": 0, "row": pos.row, "col": pos.col,
              "rows": rows, "cols": cols, "member": init_leader, **tile_payload})
        _emit_enemies()

    movements = 0
    branch_points_seen: set[tuple] = set()

    def _save():
        db.save_session(
            scene='overworld',
            world_seed=world_seed,
            sx=pos.sx, sy=pos.sy, row=pos.row, col=pos.col,
            tick=tick, leader_idx=leader_idx,
            party=party,
            goal=active_goal,
            navlog=navlog,
            journals=journals,
        )

    try:
        while True:
            tick += 1
            _alive = [m for m in party if m.alive]
            if not _alive:
                _respawn_at_hub()
                continue
            cur_leader_name = _alive[leader_idx % len(_alive)].name

            # ── TIER 1: ensure active goal ────────────────────────────────────
            if active_goal is None or not active_goal.is_active():
                active_goal, leader_idx = _run_goal_setting(
                    party, pos, db, navlog, journals, tick,
                    previous_goal=previous_goal, emit=emit,
                    leader_idx=leader_idx)
                previous_goal = None
                branch_points_seen.clear()
                _save()

            # ── Already at goal screen? ───────────────────────────────────────
            if active_goal.at_target(pos.sx, pos.sy):
                active_goal.complete()
                ctx = build_curated_context(active_goal, navlog, db, world_seed,
                                            party, pos.sx, pos.sy)
                vs = _viewscan(grid, pos.row, pos.col)
                en = _nearby_enemies(enemy_agents, pos.row, pos.col)
                outcome = _run_checkpoint('goal_reached', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx, vs=vs,
                                          enemies_nearby=en or None)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                # goal_reached is always terminal; 'continue'→Tier-1, 'modify'→new goal
                if outcome.decision == 'modify' and outcome.new_goal:
                    previous_goal = active_goal
                    active_goal = outcome.new_goal
                    branch_points_seen.clear()
                else:
                    previous_goal = active_goal
                    active_goal = None
                _save()
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
                vs = _viewscan(grid, pos.row, pos.col)
                en = _nearby_enemies(enemy_agents, pos.row, pos.col)
                outcome = _run_checkpoint('all_exits_blocked', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx, vs=vs,
                                          enemies_nearby=en or None)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                active_goal.abandon('all_exits_blocked')
                if outcome.decision == 'modify' and outcome.new_goal:
                    previous_goal = active_goal
                    active_goal = outcome.new_goal
                    branch_points_seen.clear()
                else:
                    previous_goal = active_goal
                    active_goal = None
                _save()
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
                vs = _viewscan(grid, pos.row, pos.col)
                en = _nearby_enemies(enemy_agents, pos.row, pos.col)
                outcome = _run_checkpoint('branch_point', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx, vs=vs,
                                          enemies_nearby=en or None)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                if outcome.decision == 'abandon':
                    active_goal.abandon('branch_point')
                    previous_goal = active_goal
                    active_goal = None
                    _save()
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
                    _save()
                    continue
                # 'continue' — fall through, proceed with next_dir as chosen
                _save()

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
                            _save()
                        if pending_battle is not None:
                            break
                        if feat_stop in ('blocker', 'enterable'):
                            break
                    if pending_battle is not None:
                        _trigger_battle(pending_battle)
                        pending_battle = None
                        _save()
                        continue
                    if feat_stop == 'enterable':
                        enemy_agents.clear()
                        leader_idx = _enter_interior(
                            pos, db, emit=emit,
                            render_interior_fn=render_interior_fn,
                            party=party,
                            screen_seed=screen_row['screen_seed'],
                            enemy_rng=enemy_rng, data_dir=data_dir,
                            render_npc_sprite_fn=render_npc_sprite_fn,
                            leader_idx=leader_idx)
                        if not any(m.alive for m in party):
                            _respawn_at_hub()
                            continue
                        _reload_enemies(grid)
                        _save()
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
                vs = _viewscan(grid, pos.row, pos.col)
                en = _nearby_enemies(enemy_agents, pos.row, pos.col)
                outcome = _run_checkpoint('path_blocked', active_goal, ctx, party,
                                          pos, navlog, journals, tick, emit=emit,
                                          leader_idx=leader_idx, vs=vs,
                                          enemies_nearby=en or None)
                leader_idx = (leader_idx + 1) % max(len([m for m in party if m.alive]), 1)
                active_goal.abandon('screen_blocked')
                if outcome.decision == 'modify' and outcome.new_goal:
                    previous_goal = active_goal
                    active_goal = outcome.new_goal
                    branch_points_seen.clear()
                else:
                    previous_goal = active_goal
                    active_goal = None
                _save()
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
                    _save()
                if pending_battle is not None:
                    break
                if stop in ('blocker', 'enterable'):
                    break
            if pending_battle is not None:
                _trigger_battle(pending_battle)
                pending_battle = None
                _save()
                continue

            if stop == 'enterable':
                movements += 1
                enemy_agents.clear()
                leader_idx = _enter_interior(
                    pos, db, emit=emit,
                    render_interior_fn=render_interior_fn, party=party,
                    screen_seed=screen_row['screen_seed'],
                    enemy_rng=enemy_rng, data_dir=data_dir,
                    render_npc_sprite_fn=render_npc_sprite_fn,
                    leader_idx=leader_idx)
                if not any(m.alive for m in party):
                    _respawn_at_hub()
                    continue
                _reload_enemies(grid)
                active_goal.abandon('enterable')
                previous_goal = active_goal
                active_goal = None
                _save()
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
                _save()

            if pending_battle is not None:
                _trigger_battle(pending_battle)
                pending_battle = None
                _save()
                continue

            if stop == 'enterable':
                movements += 1
                enemy_agents.clear()
                leader_idx = _enter_interior(
                    pos, db, emit=emit,
                    render_interior_fn=render_interior_fn, party=party,
                    render_npc_sprite_fn=render_npc_sprite_fn,
                    leader_idx=leader_idx)
                if not any(m.alive for m in party):
                    _respawn_at_hub()
                    continue
                _reload_enemies(grid)
                active_goal.abandon('enterable')
                previous_goal = active_goal
                active_goal = None
                _save()
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
