"""CLI harness for the voting state machine + multi-screen movement (Jobs 7, 8, 9).

Runs a real overworld screen with real LLM calls. Party members vote on
proposed moves; a passed proposal triggers movement via execute_move(), which
handles screen crossings seamlessly when a generator is supplied.

Job 9 additions:
  - Direct crossing assertion test (no LLM) runs first and validates the
    execute_move crossing path in isolation.
  - Party starts near the north edge of screen (0,0) so the LLM rounds
    naturally produce proposals that span the screen boundary.
  - Generator (generate_screen_data) is threaded through to execute_move so
    crossings happen transparently; the caller updates its grid reference from
    MoveResult.final_grid.
  - screen_exits are fetched from WorldDB and passed to build_overworld_context
    so the LLM sees 'crossing available' in the direction summary.
  - Journal SCREEN events record each crossing per member.

Run from the repo root:
    python procgen/voting_test.py
"""

import json
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from dataclasses import dataclass
from pathlib import Path

from engine.journal import MemberJournal, journals_append
from engine.party_state import PartyPos, enter_screen, execute_move
from engine.tiles import is_passable, is_enterable
from engine.viewscan import scan
from engine.voting import VotingState, available_actions
from engine.worlddb import WorldDB
from llm.ascii_map import render_map_overlay
from llm.client import ask, ask_with_retry
from llm.prompts import build_overworld_system_prompt, build_overworld_context
from llm.schema import parse_overworld_action
from procgen.worldgen import generate_screen_data

WORLD_SEED = 77777
MAX_ROUNDS = 8   # enough to fill and roll the 12-entry journal window


# ── party member ─────────────────────────────────────────────────────────────

@dataclass
class OverworldMember:
    name: str
    lvl: int
    hp: int
    max_hp: int
    personality: str
    alive: bool = True

    @classmethod
    def from_json(cls, path: Path) -> 'OverworldMember':
        with open(path) as f:
            d = json.load(f)
        return cls(
            name=d['name'],
            lvl=d['lvl'],
            hp=d['hp'],
            max_hp=d['max_hp'],
            personality=d.get('personality', ''),
        )


def load_party(data_dir: Path) -> list:
    members = []
    for p in sorted(data_dir.glob('*.json')):
        if p.stem == 'null':
            continue
        members.append(OverworldMember.from_json(p))
    return members


# ── vote math assertions ──────────────────────────────────────────────────────

def _test_vote_math():
    """Inline assertions — run before any LLM call so logic bugs surface fast."""

    # 4 alive: threshold=3; Y,Y,Y → locked at 3rd yes
    vs4 = VotingState()
    p, out = vs4.open_proposal('A', 'N', 2, alive_count=4)
    assert out is None and p is not None, "4-alive should not auto-resolve"
    assert p.yes_count == 1 and not p.is_locked()
    vs4.cast_vote('B', True)
    assert p.yes_count == 2 and not p.is_locked()
    out = vs4.cast_vote('C', True)
    assert out == 'passed', f"4-alive Y,Y,Y: expected 'passed', got {out!r}"
    assert not vs4.is_open

    # 4 alive: Y,N,N → locked when 2nd no lands (can't reach threshold=3)
    vs4b = VotingState()
    p, _ = vs4b.open_proposal('A', 'S', 1, alive_count=4)
    vs4b.cast_vote('B', False)
    assert not p.is_locked(), "one no on 4-alive threshold=3 should not lock yet"
    out = vs4b.cast_vote('C', False)
    assert out == 'failed', f"4-alive Y,N,N: expected 'failed', got {out!r}"

    # 3 alive: threshold=2; Y,Y → passed on 2nd yes
    vs3 = VotingState()
    _, _ = vs3.open_proposal('A', 'E', 3, alive_count=3)
    out = vs3.cast_vote('B', True)
    assert out == 'passed', f"3-alive Y,Y: expected 'passed', got {out!r}"

    # 2 alive: threshold=2 (unanimous); Y,N → failed
    vs2 = VotingState()
    _, _ = vs2.open_proposal('A', 'W', 1, alive_count=2)
    out = vs2.cast_vote('B', False)
    assert out == 'failed', f"2-alive Y,N: expected 'failed', got {out!r}"

    # 1 alive: auto-pass, proposal clears immediately
    vs1 = VotingState()
    p, out = vs1.open_proposal('A', 'N', 1, alive_count=1)
    assert out == 'passed' and p is None, \
        f"1-alive: expected auto-pass, got p={p} out={out!r}"

    # Initiator cannot flip their vote
    vs_dup = VotingState()
    p, _ = vs_dup.open_proposal('A', 'N', 1, alive_count=3)
    yes_before = p.yes_count
    vs_dup.cast_vote('A', False)   # attempt to flip — should no-op
    assert p.yes_count == yes_before, "initiator should not be able to change vote"

    print("PASS: vote math assertions")


# ── provider probe ────────────────────────────────────────────────────────────

def _probe_provider() -> bool:
    """One real call to verify the provider is reachable."""
    import secrets as s
    print(f"\nProvider: {s.PROVIDER}")
    raw = ask("Respond with exactly: OK", "Reply with just the word OK.")
    if raw is None:
        print("WARNING: LLM provider unreachable — ALL decisions will use fallback defaults")
        return False
    print(f"Provider probe: {raw.strip()[:80]!r}")
    return True


# ── position helpers ──────────────────────────────────────────────────────────

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
                base = (grid[r][c] or 'grass1').split(':')[0]
                if is_passable(base) and not is_enterable(base):
                    return r, c
    return None


def _find_north_crossing_start(grid, rows, cols, max_start_row=5):
    """Find a passable non-enterable tile with a clear northward path to the edge.

    Uses viewscan: qualifies when scan.N.kind == 'edge' and
    scan.N.adjacent_passable == True, meaning no blocker between this tile and
    the top of the screen. Prefers tiles close to the north edge (small row index).
    """
    for r in range(1, min(max_start_row + 1, rows)):
        for c in range(cols):
            base = (grid[r][c] or 'grass1').split(':')[0]
            if not is_passable(base) or is_enterable(base):
                continue
            vs = scan(grid, r, c)
            if vs.N.kind == 'edge' and vs.N.adjacent_passable:
                return r, c
    return None


# ── direct crossing assertion test (no LLM) ──────────────────────────────────

def _test_screen_crossing_direct(grid, rows, cols, db):
    """Assert execute_move seamlessly crosses a screen edge when given a generator.

    Uses a fresh PartyPos and fresh journals; does not mutate the caller's state.
    """
    print("\n─── DIRECT CROSSING TEST (no LLM) ─────────────────────────────────")

    start = _find_north_crossing_start(grid, rows, cols, max_start_row=5)
    if start is None:
        print("SKIP: no suitable north-crossing start on screen (0,0)")
        return

    start_r, start_c = start
    vs_start = scan(grid, start_r, start_c)
    # distance = steps until the N ray exits the grid (includes the crossing step)
    edge_dist = vs_start.N.distance
    steps = edge_dist + 3  # cross the edge + 3 more on new screen

    print(f"Start: row={start_r} col={start_c}  edge_dist={edge_dist}  proposing N {steps}")

    test_pos = PartyPos(world_seed=WORLD_SEED, sx=0, sy=0,
                        col=start_c, row=start_r)
    # Register screen (0,0) with the test db instance (it may already be cached)
    enter_screen(test_pos, db, generate_screen_data, tick=0)

    test_journals = {'_cross_test': MemberJournal('_cross_test')}

    result = execute_move(test_pos, 'N', steps, grid, db,
                          journals=test_journals, tick=0,
                          generator=generate_screen_data)

    # ── assertions ────────────────────────────────────────────────────────────
    assert test_pos.sy == -1, \
        f"expected sy=-1 after N crossing from (0,0), got {test_pos.sy}"
    assert test_pos.sx == 0, \
        f"expected sx=0 after N crossing, got {test_pos.sx}"
    assert result.final_grid is not None, \
        "execute_move must set final_grid on crossing"
    assert result.final_grid is not grid, \
        "final_grid must differ from input grid after screen crossing"
    assert result.steps_taken >= edge_dist, \
        f"steps_taken={result.steps_taken} must be >= edge_dist={edge_dist}"

    mj = test_journals['_cross_test']
    screen_entries = [e for e in mj._entries if e.event_type == 'SCREEN']
    assert screen_entries, "journal must have a SCREEN entry after crossing"
    assert 'crossed N' in screen_entries[0].desc, \
        f"unexpected SCREEN entry: {screen_entries[0].desc!r}"

    print(f"  steps_taken={result.steps_taken}  stop={result.stop_reason!r}")
    print(f"  final pos: screen ({test_pos.sx},{test_pos.sy}) "
          f"row={test_pos.row} col={test_pos.col}")
    print(f"  SCREEN journal: {screen_entries[0].desc!r}")
    print("PASS: direct screen crossing test")

    # Validate viewscan on the new screen is coherent (context refresh)
    new_rows = len(result.final_grid)
    new_cols = len(result.final_grid[0])
    vs_new = scan(result.final_grid, test_pos.row, test_pos.col)
    # Party entered from the south edge of the new screen, so S should be 'edge'
    assert vs_new.S.kind == 'edge', \
        f"after N crossing, party should be near south edge; S.kind={vs_new.S.kind!r}"
    print(f"  Viewscan on new screen: S.kind={vs_new.S.kind!r}  (correct — at south edge)")
    print(f"  new screen: {new_rows}×{new_cols}  "
          f"N={vs_new.N.kind} S={vs_new.S.kind} E={vs_new.E.kind} W={vs_new.W.kind}")


# ── movement execution ────────────────────────────────────────────────────────

def _execute_proposal(pos, direction, steps, grid, db, journals, tick,
                      generator) -> tuple[str, list]:
    """Execute movement for a passed proposal; return (stop_reason, final_grid).

    Prints before/after ASCII maps and the step log. Uses result.final_grid so
    the caller always gets the correct grid even after a screen crossing.
    """
    rows, cols = len(grid), len(grid[0])

    vs_before = scan(grid, pos.row, pos.col)
    print(f"\n  MAP before move  screen=({pos.sx},{pos.sy}) "
          f"(row={pos.row},col={pos.col}):")
    print(render_map_overlay(grid, rows, cols, vs_before))

    result = execute_move(pos, direction, steps, grid, db,
                          journals=journals, tick=tick, generator=generator)

    final_grid = result.final_grid if result.final_grid is not None else grid
    final_rows = len(final_grid)
    final_cols = len(final_grid[0])

    vs_after = scan(final_grid, pos.row, pos.col)
    print(f"\n  MAP after move  screen=({pos.sx},{pos.sy}) "
          f"(row={pos.row},col={pos.col}):")
    print(render_map_overlay(final_grid, final_rows, final_cols, vs_after))

    crossed = result.final_grid is not None and result.final_grid is not grid
    cross_note = f"  *** CROSSED SCREEN → now at ({pos.sx},{pos.sy})" if crossed else ""
    print(f"\n  Move: {result.steps_taken} steps, stop={result.stop_reason!r}"
          + (f"  {cross_note}" if cross_note else ""))
    for rec in result.log:
        print(f"    step {rec.step_num}: ({rec.before_col},{rec.before_row}) → "
              f"({rec.after_col},{rec.after_row})  [{rec.note}]")

    return result.stop_reason, final_grid


# ── voting round ──────────────────────────────────────────────────────────────

def _run_voting_round(party, pos, grid, db, voting_state, round_num,
                      decision_tally, journals, tick,
                      generator=None) -> tuple[str | None, list]:
    """One full round of turns (one per alive member).

    Returns (stop_reason, final_grid) if a proposal passed and was executed,
    else (None, grid). final_grid may differ from the input grid when a screen
    crossing occurred during movement. Callers must update their grid reference.
    """
    alive       = [m for m in party if m.alive]
    alive_count = len(alive)

    print(f"\n{'─'*60}")
    print(f"ROUND {round_num}  t={tick}  alive={alive_count}  "
          f"screen=({pos.sx},{pos.sy})  pos=(row={pos.row},col={pos.col})")
    print(f"{'─'*60}")

    # Fetch exits from DB so the LLM can see which screen edges are crossable.
    screen_row = db.get_or_create_screen(pos.world_seed, pos.sx, pos.sy, generator)
    screen_exits = json.loads(screen_row['exits_json']) if screen_row else {}

    for member in alive:
        vs   = scan(grid, pos.row, pos.col)
        acts = available_actions(member.name, voting_state)
        mj   = journals[member.name]

        sys_prompt  = build_overworld_system_prompt(member)
        user_prompt = build_overworld_context(
            member, party, vs, voting_state, acts, member_journal=mj,
            screen_exits=screen_exits)

        # Fallback: safe default that is valid for the current available_actions
        fallback = ({"action": "VOTE", "vote": "no"}
                    if "VOTE" in acts else {"action": "WAIT"})

        vote_opts = (
            '  {"action": "PROPOSE", "direction": "N"|"S"|"E"|"W", "steps": 1-8}\n'
            '  {"action": "WAIT"}'
        ) if "PROPOSE" in acts else (
            '  {"action": "VOTE", "vote": "yes"|"no"}\n'
            '  {"action": "WAIT"}'
        ) if "VOTE" in acts else '  {"action": "WAIT"}'

        reprompt = (
            "You MUST output exactly one JSON object, nothing else.\n"
            "Valid formats this turn:\n" + vote_opts
        )

        validator = lambda raw, _acts=acts: parse_overworld_action(raw, _acts)
        decision  = ask_with_retry(user_prompt, sys_prompt, validator, reprompt, fallback)

        decision_tally['total']    += 1
        decision_tally['fallback'] += decision.used_fallback
        decision_tally['retried']  += (decision.raw_retry is not None
                                       and not decision.used_fallback)

        raw_shown    = decision.raw_retry if decision.raw_retry else decision.raw_first
        fallback_tag = (' [FALLBACK]' if decision.used_fallback else
                        ' [retry ok]' if decision.raw_retry else '')
        print(f"\n{member.name} | acts={sorted(acts)}  journal={len(mj)} entries{fallback_tag}")
        if raw_shown:
            print(f"  raw: {raw_shown.strip()[:120]}")
        print(f"  parsed: {decision.result}")

        action = decision.result['action']

        if action == 'PROPOSE':
            direction = decision.result['direction']
            steps     = decision.result['steps']
            print(f"  → {member.name} PROPOSES: move {direction} {steps} steps")
            proposal, outcome = voting_state.open_proposal(
                member.name, direction, steps, alive_count)
            # Log proposal — all members hear it
            journals_append(journals, tick, 'PROPOSE',
                            f"{member.name}: propose {direction} {steps}")
            if outcome == 'passed':
                # Single-survivor auto-pass
                print("  → AUTO-PASS (single survivor)")
                journals_append(journals, tick, 'RESOLVED',
                                f"move {direction} {steps} → PASSED (auto)")
                stop, new_grid = _execute_proposal(
                    pos, direction, steps, grid, db, journals, tick, generator)
                return stop, new_grid

        elif action == 'VOTE' and voting_state.is_open:
            # Capture direction/steps and yes_count BEFORE cast_vote may clear the proposal
            saved_dir   = voting_state.proposal.direction
            saved_steps = voting_state.proposal.steps
            vote_yes    = decision.result['vote'] == 'yes'
            outcome     = voting_state.cast_vote(member.name, vote_yes)
            vote_word   = 'YES' if vote_yes else 'NO'
            print(f"  → {member.name} votes {vote_word}")
            journals_append(journals, tick, 'VOTE', f"{member.name}: {vote_word}")
            if outcome == 'passed':
                journals_append(journals, tick, 'RESOLVED',
                                f"move {saved_dir} {saved_steps} → PASSED")
                print(f"  → Proposal PASSED! Executing: move {saved_dir} {saved_steps}")
                stop, new_grid = _execute_proposal(
                    pos, saved_dir, saved_steps, grid, db, journals, tick, generator)
                return stop, new_grid
            elif outcome == 'failed':
                journals_append(journals, tick, 'RESOLVED',
                                f"move {saved_dir} {saved_steps} → FAILED")
                print("  → Proposal FAILED")

        else:
            print(f"  → {member.name} WAITs")

    # End of round — abandon any still-open proposal
    if voting_state.is_open:
        p = voting_state.proposal
        print(f"\n  Round {round_num} ended: proposal open "
              f"({p.yes_count}Y/{p.no_count}N/{p.threshold} needed) — abandoning")
        voting_state.abandon()

    # Journal size summary after each round
    sizes = {m.name: len(journals[m.name]) for m in alive}
    print(f"\n  Journal sizes: {sizes}")

    return None, grid


# ── main ──────────────────────────────────────────────────────────────────────

def main():
    print(f"World seed: {WORLD_SEED}")
    print(f"Max rounds: {MAX_ROUNDS}\n")

    _test_vote_math()

    # Load party from JSON files
    data_dir = Path(__file__).parent.parent / 'data' / 'party'
    party = load_party(data_dir)
    print(f"Party: {[m.name for m in party]}")

    # Per-member journals (all members share the same broadcast events for now)
    journals = {m.name: MemberJournal(m.name) for m in party}

    # Generate screen (0,0) and seed WorldDB
    db   = WorldDB(':memory:')
    pos  = PartyPos(world_seed=WORLD_SEED, sx=0, sy=0, col=0, row=0)

    screen = enter_screen(pos, db, generate_screen_data, tick=0)
    grid   = json.loads(screen['grid_json'])
    rows, cols = len(grid), len(grid[0])
    exits  = json.loads(screen['exits_json'])

    print(f"Screen (0,0): {rows}×{cols}  exits={exits}")

    # Prefer a start near the north edge so LLM proposals can span the boundary.
    north_start = _find_north_crossing_start(grid, rows, cols)
    if north_start:
        pos.row, pos.col = north_start
        print(f"Start: row={pos.row} col={pos.col}  (near north edge with clear path)")
    else:
        center_start = _find_walkable_center(grid, rows, cols)
        assert center_start, "no walkable tile found on screen (0,0)"
        pos.row, pos.col = center_start
        print(f"Start: row={pos.row} col={pos.col}  (center fallback — no clear north path)")

    vs0 = scan(grid, pos.row, pos.col)
    print(f"\nStarting map:")
    print(render_map_overlay(grid, rows, cols, vs0))
    for label in ('N', 'S', 'E', 'W'):
        ds = getattr(vs0, label)
        exit_open = exits.get(label, False) if ds.kind == 'edge' else False
        step_note = 'ok' if (ds.adjacent_passable or exit_open) else 'NO'
        cross_note = ' [crossing available]' if exit_open else ''
        print(f"  {label}: {ds.kind:10}  tile={ds.tile or '(edge)':20}  "
              f"dist={ds.distance}  step={step_note}{cross_note}")

    # Run direct crossing assertion test before any LLM calls
    _test_screen_crossing_direct(grid, rows, cols, db)

    provider_ok = _probe_provider()

    voting_state   = VotingState()
    decision_tally = {'total': 0, 'fallback': 0, 'retried': 0}
    movement_count = 0
    screens_crossed_total = 0
    last_round     = 0
    tick           = 0

    for round_num in range(1, MAX_ROUNDS + 1):
        last_round = round_num
        tick += 1
        stop, grid = _run_voting_round(
            party, pos, grid, db, voting_state, round_num, decision_tally,
            journals, tick, generator=generate_screen_data)
        if stop:
            movement_count += 1
            print(f"\n  Movement complete (round {round_num}, stop={stop!r}, "
                  f"screen=({pos.sx},{pos.sy}))")
            # Don't break on enterable — party stays on feature tile and continues voting

    # Full journal dump for one member as validation of accumulation and rollover
    sample_member = party[0]
    mj = journals[sample_member.name]
    print(f"\n{'='*60}")
    print(f"JOURNAL DUMP — {sample_member.name}  ({len(mj)}/{mj._entries.maxlen} entries)")
    for line in mj.render():
        print(line)

    # Count screen crossings from journal
    screen_events = [e for mj_ in journals.values()
                     for e in mj_._entries if e.event_type == 'SCREEN']
    screens_crossed_total = len(screen_events) // max(len(party), 1)

    # Summary
    known = db.list_known_screens(WORLD_SEED)
    print(f"\n{'='*60}")
    print("SUMMARY")
    print(f"  Rounds run:          {last_round}/{MAX_ROUNDS}")
    print(f"  Movements executed:  {movement_count}")
    print(f"  Screens crossed:     {screens_crossed_total}")
    print(f"  Screens discovered:  {len(known)}  {[(s['sx'],s['sy']) for s in known]}")
    print(f"  Final pos:           screen ({pos.sx},{pos.sy})  "
          f"row={pos.row} col={pos.col}")
    total    = decision_tally['total']
    fallback = decision_tally['fallback']
    retried  = decision_tally['retried']
    real     = total - fallback
    print(f"  LLM decisions:       {total} total | {real} valid LLM | "
          f"{fallback} fallback | {retried} needed retry")
    if not provider_ok:
        print("  NOTE: provider unreachable — all decisions are fallbacks from None response")
    elif fallback == total and total > 0:
        print("  WARNING: every decision fell back — check raw output above for JSON issues")
    print(f"{'='*60}")


if __name__ == '__main__':
    main()
