"""Hub scene — four party members roam the static hub town map independently.

Each member has their own position and takes turns: viewscan → LLM direction
pick → execute_move.  When a member reaches a map boundary and tries to step
off, a party vote triggers.  If it passes, run_hub() returns "overworld" so
the caller can chain into run_overworld().

# TODO: NPC spawning and interaction
"""

import json
import re
from dataclasses import dataclass
from pathlib import Path

from engine.config import cfg
from engine.party_state import PartyPos, execute_move
from engine.viewscan import scan
from engine.voting import VotingState
from llm.client import ask_with_retry
from llm.prompts import _dir_summary_line
from llm.schema import parse_overworld_action

_HERE = Path(__file__).parent
_HUB_CSV = _HERE.parent.parent / 'web' / 'static' / 'tiles' / 'hub_map_coords_bracketed.csv'
_TOWN_RULES = _HERE.parent.parent / 'web' / 'static' / 'tiles' / 'tiles_town_rules.txt'

PARTY_ORDER = ['MELVIN', 'BILLY', 'POOTS', 'SMELTRUD']
_OPPOSITE = {'N': 'S', 'S': 'N', 'E': 'W', 'W': 'E'}




@dataclass
class HubMember:
    name: str
    lvl: int
    hp: int
    max_hp: int
    personality: str
    row: int = 0
    col: int = 0
    alive: bool = True


def _parse_coord_to_name(rules_path: Path) -> dict:
    """Build {(tileset_col, tileset_row): tilename} from town tilerules.

    The tilerules format is: col,row = tilename, tag1, tag2 ...
    This is a different mapping from what tiles._parse_one builds (name→tags).
    """
    result = {}
    with open(rules_path) as f:
        for line in f:
            line = line.strip()
            if not line or '=' not in line:
                continue
            eq = line.index('=')
            key = line[:eq].strip()
            rest = line[eq + 1:]
            if '#' in rest:
                rest = rest[:rest.index('#')]
            try:
                col_str, row_str = key.split(',')
                tc, tr = int(col_str.strip()), int(row_str.strip())
            except ValueError:
                continue
            parts = [p.strip().rstrip('_').replace(' ', '') for p in rest.split(',')]
            name = parts[0]
            if name:
                result[(tc, tr)] = name
    return result


def load_hub_coords() -> list[list[tuple[int, int]]]:
    """Return raw tile-sheet coords: grid[row][col] = (tileset_col, tileset_row).

    Used by the web renderer to blit tiles; exported so the server can render
    the hub map PNG without re-parsing the CSV independently.
    """
    coords: list[list[tuple[int, int]]] = []
    with open(_HUB_CSV) as f:
        for line in f:
            line = line.strip()
            if not line:
                continue
            row: list[tuple[int, int]] = []
            for m in re.finditer(r'\[(\d+),(\d+)\]', line):
                row.append((int(m.group(1)), int(m.group(2))))
            if row:
                coords.append(row)
    return coords


def load_hub_grid() -> list[list[str]]:
    """Parse hub CSV into grid[row][col] of tile name strings.

    CSV cells are [tileset_col,tileset_row].  Unknown coords fall back to
    'grass1' (passable) and are logged so data gaps are visible.
    """
    coord_to_name = _parse_coord_to_name(_TOWN_RULES)
    coords = load_hub_coords()
    grid: list[list[str]] = []
    unknowns: list[tuple[int, int]] = []
    for coord_row in coords:
        row: list[str] = []
        for (tc, tr) in coord_row:
            name = coord_to_name.get((tc, tr))
            if name:
                row.append(name)
            else:
                row.append('grass1')
                unknowns.append((tc, tr))
        grid.append(row)
    if unknowns:
        unique = sorted(set(unknowns))
        print(f"  [hub] unknown tile coords (falling back to grass1): {unique}", flush=True)
    return grid


def _spawn_positions(grid: list) -> list[tuple[int, int]]:
    """4 rows up from bottom, middle 4 tiles — left-to-right: MELVIN BILLY POOTS SMELTRUD.

    Placing members away from the bottom edge prevents an immediate leave-vote
    trigger on the very first tick if someone picks S.
    """
    bottom_row = len(grid) - 1
    spawn_row = max(bottom_row - 4, 0)
    cols = len(grid[0]) if grid else 16
    mid = cols // 2
    return [
        (spawn_row, mid - 2),
        (spawn_row, mid - 1),
        (spawn_row, mid),
        (spawn_row, mid + 1),
    ]


def _load_party(data_dir: Path) -> list[HubMember]:
    members_by_name: dict[str, HubMember] = {}
    for path in data_dir.glob("*.json"):
        if path.stem == "null":
            continue
        d = json.loads(path.read_text())
        name = d["name"]
        members_by_name[name] = HubMember(
            name=name,
            lvl=d["lvl"],
            hp=d["hp"],
            max_hp=d["max_hp"],
            personality=d.get("personality", ""),
        )
    return [members_by_name[n] for n in PARTY_ORDER if n in members_by_name]


# ── per-member roam prompts ───────────────────────────────────────────────────

def _build_system_prompt(member: HubMember) -> str:
    lines = [
        f"You are {member.name} (LVL {member.lvl}) hanging around the Front House — "
        "the place where your party lives and gathers between adventures.",
    ]
    if member.personality:
        lines.append(f"Personality: {member.personality}")
    lines.append(
        "You're wandering around the Front House. There may be a vote to leave "
        "for the overworld if someone reaches the edge of the property.\n"
        "Pick a direction to walk. Output JSON only — no other text."
    )
    return "\n".join(lines)


def _build_move_prompt(member: HubMember, vs) -> str:
    parts = [f"DIRECTIONS FROM {member.name}'s position:"]
    for label in ("N", "S", "E", "W"):
        ds = getattr(vs, label)
        if ds.kind == 'edge' and not ds.adjacent_passable:
            # member is right at the map boundary — stepping this way triggers a leave vote
            parts.append(f"  {label}: can step? YES — hub boundary (triggers leave vote)")
        else:
            parts.append(_dir_summary_line(label, ds))
    parts.append("")
    parts.append("Choose a direction where 'can step? YES'. Steps: 1–3.")
    parts.append("Choosing a hub-boundary direction triggers a party vote to leave.")
    parts.append('{"action": "PROPOSE", "direction": "N"|"S"|"E"|"W", "steps": 1}')
    return "\n".join(parts)


# ── leave-hub vote ────────────────────────────────────────────────────────────

def _build_leave_vote_system_prompt(member: HubMember) -> str:
    lines = [f"You are {member.name} (LVL {member.lvl}) at the Front House."]
    if member.personality:
        lines.append(f"Personality: {member.personality}")
    lines.append(
        "The party must vote: leave the Front House and head out to the overworld, or stay.\n"
        "Output JSON only — no other text."
    )
    return "\n".join(lines)


def _build_leave_vote_prompt(voter: HubMember, proposer_name: str, direction: str,
                              party: list[HubMember], voting_state: VotingState) -> str:
    parts = [
        f"PROPOSAL: {proposer_name} reached the {direction} edge of the Front House "
        f"and proposes to LEAVE.",
        "Vote YES to head out to the overworld now, or NO to stay at the Front House.",
    ]
    parts.append("")
    parts.append("PARTY")
    for m in party:
        status = "[DEAD]" if not m.alive else f"HP {m.hp}/{m.max_hp}"
        you = " ← YOU" if m.name == voter.name else ""
        parts.append(f"  {m.name}: LVL {m.lvl}  {status}{you}")

    if voting_state.is_open:
        p = voting_state.proposal
        yes_names = [n for n, v in p.votes.items() if v]
        no_names  = [n for n, v in p.votes.items() if not v]
        parts.append("")
        parts.append(f"VOTES SO FAR: YES ({p.yes_count}/{p.threshold} needed): "
                     f"{', '.join(yes_names) or 'none'}  |  "
                     f"NO ({p.no_count}): {', '.join(no_names) or 'none'}")

    parts.append("")
    parts.append('{"action": "VOTE", "vote": "yes"|"no"}')
    return "\n".join(parts)


def _run_leave_vote(proposer: HubMember, direction: str,
                    party: list[HubMember], emit=None) -> bool:
    """Run a party vote to leave the hub. Returns True if vote passes."""
    alive = [m for m in party if m.alive]
    voting_state = VotingState()

    _, immediate = voting_state.open_proposal(proposer.name, direction, 1, len(alive))
    if immediate == 'passed':
        print(f"  Leave hub: auto-passed (solo).", flush=True)
        if emit:
            emit({"type": "hub_vote", "outcome": "passed", "proposer": proposer.name})
        return True

    print(f"\n  LEAVE VOTE — {proposer.name} proposes to exit {direction}", flush=True)

    reprompt = (
        "You MUST output exactly one JSON object, nothing else.\n"
        '  {"action": "VOTE", "vote": "yes"|"no"}'
    )
    fallback = {"action": "VOTE", "vote": "yes"}

    for voter in [m for m in alive if m.name != proposer.name]:
        if not voting_state.is_open:
            break

        sys_prompt = _build_leave_vote_system_prompt(voter)
        vote_prompt = _build_leave_vote_prompt(
            voter, proposer.name, direction, party, voting_state)

        def _parse(raw):
            return parse_overworld_action(raw, {"VOTE"})

        decision = ask_with_retry(vote_prompt, sys_prompt, _parse, reprompt, fallback)
        vote_val = decision.result["vote"]
        tag = " [FALLBACK]" if decision.used_fallback else ""
        print(f"  {voter.name}{tag}: {vote_val}", flush=True)

        outcome = voting_state.cast_vote(voter.name, vote_val == "yes")
        if outcome:
            print(f"  Vote {outcome}.", flush=True)
            if emit:
                emit({"type": "hub_vote", "outcome": outcome,
                      "proposer": proposer.name, "direction": direction})
            return outcome == "passed"

    # All voters polled; check final tally (handles abstentions if any leaked through)
    if voting_state.is_open:
        p = voting_state.proposal
        result_bool = p.yes_count >= p.threshold
        outcome = "passed" if result_bool else "failed"
        print(f"  Vote {outcome} (final tally).", flush=True)
        if emit:
            emit({"type": "hub_vote", "outcome": outcome,
                  "proposer": proposer.name, "direction": direction})
        return result_bool

    return False


# ── main loop ─────────────────────────────────────────────────────────────────

def run_hub(emit=None, render_hub_fn=None) -> str | None:
    """Run the hub scene.

    Returns 'overworld' when the party votes to leave, or None on
    KeyboardInterrupt / unexpected exit.

    Note: party spawns on the bottom row — S-direction leave vote is possible
    on tick 1 if a member chooses to step south.
    """
    data_dir = Path(__file__).parent.parent.parent / 'data' / 'party'
    party = _load_party(data_dir)
    grid = load_hub_grid()

    spawns = _spawn_positions(grid)
    for i, member in enumerate(party):
        member.row, member.col = spawns[i]

    rows, cols = len(grid), len(grid[0]) if grid else 0
    print(f"\nHUB SCENE  grid={rows}×{cols}", flush=True)
    for m in party:
        print(f"  {m.name}: row={m.row} col={m.col}", flush=True)

    if emit:
        payload: dict = {"type": "hub_init", "rows": rows, "cols": cols,
                         "party": [{"name": m.name, "row": m.row, "col": m.col}
                                   for m in party]}
        if render_hub_fn:
            payload.update(render_hub_fn())
        emit(payload)

    reprompt = (
        "You MUST output exactly one JSON object, nothing else.\n"
        '  {"action": "PROPOSE", "direction": "N"|"S"|"E"|"W", "steps": 1}'
    )
    fallback = {"action": "PROPOSE", "direction": "N", "steps": 1}

    def _parse(raw):
        return parse_overworld_action(raw, {"PROPOSE"})

    tick = 0
    try:
        while True:
            tick += 1
            print(f"\n{'─'*60}", flush=True)
            print(f"HUB TICK {tick}", flush=True)

            for member in party:
                if not member.alive:
                    continue

                vs = scan(grid, member.row, member.col)
                sys_prompt = _build_system_prompt(member)
                move_prompt = _build_move_prompt(member, vs)

                decision = ask_with_retry(move_prompt, sys_prompt, _parse, reprompt, fallback)
                direction = decision.result["direction"]
                steps = decision.result["steps"]

                tag = " [FALLBACK]" if decision.used_fallback else ""
                print(f"  {member.name}{tag}: {direction} {steps}  "
                      f"(row={member.row},col={member.col})", flush=True)

                pos = PartyPos(world_seed=0, sx=0, sy=0, row=member.row, col=member.col)
                result = execute_move(
                    pos, direction, steps, grid, db=None, tick=tick, generator=None)
                member.row, member.col = pos.row, pos.col

                if emit:
                    emit({"type": "hub_move", "name": member.name,
                          "row": member.row, "col": member.col, "tick": tick,
                          "direction": direction})

                if result.stop_reason == 'edge':
                    if _run_leave_vote(member, direction, party, emit=emit):
                        print(f"\nLeaving hub → overworld.", flush=True)
                        return "overworld"

                if result.stop_reason == 'edge':
                    # Nudge away from boundary (vote failed or too early)
                    opp = _OPPOSITE[direction]
                    nudge_dir = opp
                    nudge_pos = PartyPos(world_seed=0, sx=0, sy=0,
                                        row=member.row, col=member.col)
                    nudge = execute_move(
                        nudge_pos, opp, 1, grid, db=None, tick=tick, generator=None)
                    if nudge.steps_taken > 0:
                        member.row, member.col = nudge_pos.row, nudge_pos.col
                        print(f"  Nudging — {member.name} steps {opp}.", flush=True)
                    else:
                        # Opposite also blocked (corner); try perpendicular inward
                        for alt in [d for d in ('N', 'S', 'E', 'W')
                                    if d not in (direction, opp)]:
                            alt_pos = PartyPos(world_seed=0, sx=0, sy=0,
                                               row=member.row, col=member.col)
                            alt_r = execute_move(
                                alt_pos, alt, 1, grid, db=None, tick=tick, generator=None)
                            if alt_r.steps_taken > 0:
                                member.row, member.col = alt_pos.row, alt_pos.col
                                nudge_dir = alt
                                print(f"  Nudging — {member.name} steps {alt}.",
                                      flush=True)
                                break
                    if emit:
                        emit({"type": "hub_move", "name": member.name,
                              "row": member.row, "col": member.col, "tick": tick,
                              "direction": nudge_dir})

    except KeyboardInterrupt:
        pass

    print(f"\n{'='*60}", flush=True)
    print("HUB SCENE ENDED (interrupted, no leave vote passed).", flush=True)
    for m in party:
        print(f"  {m.name}: row={m.row} col={m.col}", flush=True)
    print(f"{'='*60}", flush=True)
    return None
