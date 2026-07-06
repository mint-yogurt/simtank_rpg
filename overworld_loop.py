"""Production overworld loop: propose → vote → move → journal, repeating.

Extracted from procgen/voting_test.py; all test scaffolding removed.
Runs until KeyboardInterrupt. Called by run_cli.py and run_web.py.

Optional emit/render_screen_fn callbacks wire in the web SSE server:
  emit(dict)                   — called with typed event dicts
  render_screen_fn(ws, sx, sy) — renders screen PNG, returns URL string
Both default to None (CLI mode: no web output).
"""

import json
import re
import time
from dataclasses import dataclass
from pathlib import Path

from engine.journal import MemberJournal, journals_append
from engine.party_state import PartyPos, enter_screen, execute_move
from engine.tiles import is_enterable, is_passable
from engine.viewscan import scan
from engine.voting import VotingState, available_actions
from engine.worlddb import WorldDB
from llm.client import ask_with_retry
from llm.prompts import build_overworld_context, build_overworld_system_prompt
from llm.schema import parse_overworld_action
from procgen.overworld_test import generate_screen_data


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


def load_overworld_party(data_dir: Path) -> list:
    members = []
    for p in sorted(data_dir.glob("*.json")):
        if p.stem == "null":
            continue
        members.append(OverworldMember.from_json(p))
    return members


def _find_walkable_center(grid, rows, cols):
    """Non-enterable passable tile closest to screen center."""
    cr, cc = rows // 2, cols // 2
    for radius in range(max(rows, cols)):
        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                if abs(dr) != radius and abs(dc) != radius:
                    continue
                r, c = cr + dr, cc + dc
                if not (0 <= r < rows and 0 <= c < cols):
                    continue
                base = (grid[r][c] or "grass1").split(":")[0]
                if is_passable(base) and not is_enterable(base):
                    return r, c
    return None


def _execute_proposal(pos, direction, steps, grid, db, journals, tick, generator,
                      emit=None, render_screen_fn=None):
    """Execute movement for a passed proposal. Returns (stop_reason, final_grid)."""
    result = execute_move(pos, direction, steps, grid, db,
                          journals=journals, tick=tick, generator=generator)
    final_grid = result.final_grid if result.final_grid is not None else grid
    crossed = result.final_grid is not None and result.final_grid is not grid
    print(f"  Move: {direction} {result.steps_taken} steps, stop={result.stop_reason!r}"
          + (f"\n  *** CROSSED SCREEN → now at ({pos.sx},{pos.sy})" if crossed else ""),
          flush=True)

    if emit:
        final_rows = len(final_grid)
        final_cols = len(final_grid[0]) if final_rows else 0
        for step in result.log:
            if step.note.startswith("crossed_"):
                m = re.match(r'crossed_[NSEW]_to\((-?\d+),(-?\d+)\)', step.note)
                if m:
                    sx2, sy2 = int(m.group(1)), int(m.group(2))
                    url = render_screen_fn(pos.world_seed, sx2, sy2) if render_screen_fn else ""
                    emit({"type": "screen", "sx": sx2, "sy": sy2,
                          "row": step.after_row, "col": step.after_col,
                          "rows": final_rows, "cols": final_cols, "screen_url": url})
            else:
                emit({"type": "move", "row": step.after_row, "col": step.after_col,
                      "sx": pos.sx, "sy": pos.sy})
            time.sleep(0.35)

    return result.stop_reason, final_grid


def _run_voting_round(party, pos, grid, db, voting_state, round_num,
                      journals, tick, generator=None, emit=None, render_screen_fn=None,
                      proposer_idx=0):
    """One full round of member turns. Returns (stop_reason, final_grid) or (None, grid)."""
    alive = [m for m in party if m.alive]
    alive_count = len(alive)
    # Rotate so a different member leads (and therefore proposes) each round.
    if alive_count:
        offset = proposer_idx % alive_count
        alive = alive[offset:] + alive[:offset]

    print(f"\n{'─'*60}", flush=True)
    print(f"ROUND {round_num}  t={tick}  screen=({pos.sx},{pos.sy})  "
          f"pos=(row={pos.row},col={pos.col})", flush=True)
    print(f"{'─'*60}", flush=True)

    screen_row = db.get_or_create_screen(pos.world_seed, pos.sx, pos.sy, generator)
    screen_exits = json.loads(screen_row["exits_json"]) if screen_row else {}

    for member in alive:
        vs = scan(grid, pos.row, pos.col)
        acts = available_actions(member.name, voting_state)
        mj = journals[member.name]

        sys_prompt = build_overworld_system_prompt(member)
        user_prompt = build_overworld_context(
            member, party, vs, voting_state, acts, member_journal=mj,
            screen_exits=screen_exits)

        fallback = ({"action": "VOTE", "vote": "no"}
                    if "VOTE" in acts else {"action": "WAIT"})

        if "PROPOSE" in acts:
            vote_opts = (
                '  {"action": "PROPOSE", "direction": "N"|"S"|"E"|"W", "steps": 1-8}\n'
                '  {"action": "WAIT"}'
            )
        elif "VOTE" in acts:
            vote_opts = (
                '  {"action": "VOTE", "vote": "yes"|"no"}\n'
                '  {"action": "WAIT"}'
            )
        else:
            vote_opts = '  {"action": "WAIT"}'

        reprompt = (
            "You MUST output exactly one JSON object, nothing else.\n"
            "Valid formats this turn:\n" + vote_opts
        )

        validator = lambda raw, _acts=acts: parse_overworld_action(raw, _acts)
        decision = ask_with_retry(user_prompt, sys_prompt, validator, reprompt, fallback)

        fallback_tag = (" [FALLBACK]" if decision.used_fallback else
                        " [retry ok]" if decision.raw_retry else "")
        print(f"\n{member.name} | acts={sorted(acts)}{fallback_tag}", flush=True)
        print(f"  → {decision.result}", flush=True)

        action = decision.result["action"]

        if action == "PROPOSE":
            direction = decision.result["direction"]
            steps = decision.result["steps"]
            print(f"  {member.name} PROPOSES: move {direction} {steps} steps", flush=True)
            proposal, outcome = voting_state.open_proposal(
                member.name, direction, steps, alive_count)
            journals_append(journals, tick, "PROPOSE",
                            f"{member.name}: propose {direction} {steps}")
            if emit:
                emit({"type": "propose",
                      "text": f"{member.name} proposes: move {direction} {steps} steps"})
            if outcome == "passed":
                print("  AUTO-PASS (single survivor)", flush=True)
                journals_append(journals, tick, "RESOLVED",
                                f"move {direction} {steps} → PASSED (auto)")
                if emit:
                    emit({"type": "resolve", "text": f"AUTO-PASS → {direction} {steps}"})
                stop, new_grid = _execute_proposal(
                    pos, direction, steps, grid, db, journals, tick, generator,
                    emit=emit, render_screen_fn=render_screen_fn)
                return stop, new_grid

        elif action == "VOTE" and voting_state.is_open:
            saved_dir = voting_state.proposal.direction
            saved_steps = voting_state.proposal.steps
            vote_yes = decision.result["vote"] == "yes"
            outcome = voting_state.cast_vote(member.name, vote_yes)
            vote_word = "YES" if vote_yes else "NO"
            print(f"  {member.name} votes {vote_word}", flush=True)
            journals_append(journals, tick, "VOTE", f"{member.name}: {vote_word}")
            if emit:
                emit({"type": "vote", "text": f"{member.name}: {vote_word}"})
            if outcome == "passed":
                journals_append(journals, tick, "RESOLVED",
                                f"move {saved_dir} {saved_steps} → PASSED")
                print(f"  Proposal PASSED! Moving {saved_dir} {saved_steps}", flush=True)
                if emit:
                    emit({"type": "resolve",
                          "text": f"Proposal PASSED → {saved_dir} {saved_steps}"})
                stop, new_grid = _execute_proposal(
                    pos, saved_dir, saved_steps, grid, db, journals, tick, generator,
                    emit=emit, render_screen_fn=render_screen_fn)
                return stop, new_grid
            elif outcome == "failed":
                journals_append(journals, tick, "RESOLVED",
                                f"move {saved_dir} {saved_steps} → FAILED")
                print("  Proposal FAILED", flush=True)
                if emit:
                    emit({"type": "resolve",
                          "text": f"Proposal FAILED → {saved_dir} {saved_steps}"})

        else:
            print(f"  {member.name} WAITs", flush=True)

    if voting_state.is_open:
        p = voting_state.proposal
        print(f"\n  Round ended with open proposal "
              f"({p.yes_count}Y/{p.no_count}N, {p.threshold} needed) — abandoning",
              flush=True)
        voting_state.abandon()

    return None, grid


def run_overworld(world_seed: int, db_path: str = "world.db",
                  emit=None, render_screen_fn=None) -> None:
    """Main overworld loop. Runs until KeyboardInterrupt.

    world_seed:        logged at launch; determines all procedural generation.
    db_path:           SQLite path for the world DB. Use ':memory:' for ephemeral runs.
    emit:              optional callback(dict) — called with typed event dicts for the
                       web SSE layer. None in CLI mode.
    render_screen_fn:  optional callback(world_seed, sx, sy) → URL string — renders
                       screen PNG and returns its URL. None in CLI mode.
    """
    print(f"WORLD SEED: {world_seed}", flush=True)

    data_dir = Path(__file__).parent / "data" / "party"
    party = load_overworld_party(data_dir)
    print(f"Party: {[m.name for m in party]}", flush=True)

    journals = {m.name: MemberJournal(m.name) for m in party}
    db = WorldDB(db_path)
    pos = PartyPos(world_seed=world_seed, sx=0, sy=0, col=0, row=0)

    screen = enter_screen(pos, db, generate_screen_data, tick=0)
    grid = json.loads(screen["grid_json"])
    rows, cols = len(grid), len(grid[0])
    exits = json.loads(screen["exits_json"])
    print(f"Screen (0,0): {rows}×{cols}  exits={exits}", flush=True)

    start = _find_walkable_center(grid, rows, cols)
    assert start, "no walkable tile on screen (0,0)"
    pos.row, pos.col = start
    print(f"Start: row={pos.row} col={pos.col}", flush=True)

    if emit:
        screen_url = render_screen_fn(world_seed, 0, 0) if render_screen_fn else ""
        emit({"type": "init", "sx": 0, "sy": 0, "row": pos.row, "col": pos.col,
              "rows": rows, "cols": cols, "screen_url": screen_url})

    voting_state = VotingState()
    round_num = 0
    tick = 0
    movements = 0
    proposer_idx = 0  # advances each round so a different member leads

    try:
        while True:
            round_num += 1
            tick += 1
            stop, grid = _run_voting_round(
                party, pos, grid, db, voting_state, round_num,
                journals, tick, generator=generate_screen_data,
                emit=emit, render_screen_fn=render_screen_fn,
                proposer_idx=proposer_idx)
            proposer_idx += 1
            if stop:
                movements += 1
                if stop == "enterable":
                    print("\n  *** ARRIVED AT ENTERABLE FEATURE — battle/scene entry stub ***",
                          flush=True)
                    # TODO: trigger battle or scene transition here
    except KeyboardInterrupt:
        pass

    known = db.list_known_screens(world_seed)
    print(f"\n{'='*60}", flush=True)
    print("SESSION ENDED", flush=True)
    print(f"  Rounds: {round_num}  Movements: {movements}", flush=True)
    print(f"  Screens discovered: {len(known)}", flush=True)
    print(f"  Final pos: screen ({pos.sx},{pos.sy})  row={pos.row} col={pos.col}",
          flush=True)

    if party:
        sample = party[0]
        mj = journals[sample.name]
        print(f"\nJOURNAL — {sample.name} (last {len(mj)} entries)", flush=True)
        for line in mj.render():
            print(f"  {line}", flush=True)

    print(f"{'='*60}", flush=True)
