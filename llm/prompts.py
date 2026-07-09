"""Prompt templates and battle context renderer."""

from engine.combat import SPECIAL_MP_COSTS


HEAL_NEEDED_THRESHOLD = 0.70  # SNACK flagged as low-value if no one is below this fraction of max HP

# Overworld health urgency thresholds (individual member HP fractions)
OVERWORLD_CRITICAL_HP = 0.40
OVERWORLD_LOW_HP      = 0.65


def _health_urgency(party) -> str | None:
    """Return a brief urgency string when party health is concerning, else None.

    Accepts either a list of dicts {name, hp, max_hp, alive} (ctx.party_status)
    or OverworldMember objects — both expose the same field names.
    """
    def _g(m, k):
        return m[k] if isinstance(m, dict) else getattr(m, k)

    dead     = [_g(m, 'name') for m in party if not _g(m, 'alive')]
    critical = [_g(m, 'name') for m in party
                if _g(m, 'alive') and _g(m, 'max_hp') > 0
                and _g(m, 'hp') / _g(m, 'max_hp') < OVERWORLD_CRITICAL_HP]
    crit_set = set(critical)
    low      = [_g(m, 'name') for m in party
                if _g(m, 'alive') and _g(m, 'max_hp') > 0
                and _g(m, 'hp') / _g(m, 'max_hp') < OVERWORLD_LOW_HP
                and _g(m, 'name') not in crit_set]

    parts = []
    if dead:
        parts.append(f"{', '.join(dead)} {'is' if len(dead) == 1 else 'are'} dead")
    if critical:
        parts.append(f"{', '.join(critical)} critically low HP")
    if low:
        parts.append(f"{', '.join(low)} low HP")
    return "; ".join(parts) if parts else None

STATUS_DESCRIPTIONS = {
    "MESMERIZED": "cannot act 50% of the time",
    "CRINGE":     "may attack itself this turn",
    "STUNNED":    "misses attacks more often",
    "CONFUSED":   "may act erratically",
    "ASLEEP":     "cannot act this turn",
    "POISONED":   "loses HP each turn",
}


def _status_tag(status: str | None) -> str:
    if not status:
        return ""
    desc = STATUS_DESCRIPTIONS.get(status, status.lower())
    return f" [{status} — {desc}]"


def build_system_prompt(member) -> str:
    lines = [f"You are {member.name} (LVL {member.lvl}) in an 8-bit RPG battle."]
    if member.personality:
        lines.append(f"Personality: {member.personality}")
    if member.special["name"]:
        lines.append(
            f"Special move: {member.special['name']} — {member.special['description']}"
        )
    lines.append(
        "Pick actions that fit your character and the situation. Output JSON only — no other text."
    )
    return "\n".join(lines)


def build_battle_context(member, party: list, enemy, history: dict, run_attempts: int) -> str:
    parts = []

    parts.append("YOUR STATS")
    parts.append(
        f"Name: {member.name}  LVL: {member.lvl}"
        f"  HP: {member.hp}/{member.max_hp}"
        f"  MP: {member.mp}/{member.max_mp}"
    )

    others = [m for m in party if m.name != member.name]
    if others:
        parts.append("")
        parts.append("PARTY")
        for m in others:
            if not m.alive:
                hp_str = f"{m.hp}/{m.max_hp} [DEAD]"
            elif m.hp == m.max_hp:
                hp_str = f"{m.hp}/{m.max_hp} [FULL]"
            else:
                hp_str = f"{m.hp}/{m.max_hp}"
            buff_tags = ""
            for buff in getattr(m, "buffs", []):
                pct = round((buff.get("damage_mult", 1.0) - 1.0) * 100)
                buff_tags += f" [TICKLED +{pct}% dmg, {buff['turns_remaining']} turns]"
            parts.append(f"{m.name}: {hp_str}{_status_tag(m.status)}{buff_tags}")

    parts.append("")
    parts.append("ENEMY")
    parts.append(
        f"{enemy.name} (LVL {enemy.lvl}): {enemy.hp}/{enemy.max_hp}{_status_tag(enemy.status)}"
    )

    if history:
        parts.append("")
        parts.append("LAST ACTIONS")
        for name, desc in history.items():
            parts.append(f"{name}: {desc}")

    on_cooldown = getattr(member, "special_cooldown", 0) > 0
    spec_name = member.special.get("name", "")
    mp_cost = SPECIAL_MP_COSTS.get(spec_name, 0)
    can_afford_mp = member.mp >= mp_cost
    # Special is usable (shows as a selectable option) only when not on cooldown and MP is sufficient
    special_usable = bool(spec_name) and not on_cooldown and can_afford_mp
    # Special is visible (shown grayed out) when it exists but can't be used right now
    special_visible = bool(spec_name) and not special_usable

    all_names = "|".join(m.name for m in party) + f"|{enemy.name}|null"
    parts.append("")
    parts.append("YOUR OPTIONS")
    parts.append("1. ATTACK — strike the enemy")
    parts.append("2. DEFEND — reduce incoming damage this turn")

    if special_usable:
        spec = member.special
        if spec.get("effect_type") == "heal":
            anyone_hurt = any(
                m.alive and m.hp < m.max_hp * HEAL_NEEDED_THRESHOLD
                for m in party
            )
            if anyone_hurt:
                parts.append(f"3. SPECIAL: {spec['name']} (costs {mp_cost} MP) — {spec['description']}")
            else:
                parts.append(f"3. SPECIAL: {spec['name']} (costs {mp_cost} MP) — (party is healthy, low value this turn)")
        elif spec.get("status_effect") and enemy.status:
            parts.append(
                f"3. SPECIAL: {spec['name']} (costs {mp_cost} MP) — "
                f"({enemy.name} is already {enemy.status} — no additional effect this turn)"
            )
        else:
            parts.append(f"3. SPECIAL: {spec['name']} (costs {mp_cost} MP) — {spec['description']}")
        parts.append("4. RUN — attempt to flee (low chance; increases each attempt)")
        parts.append("")
        parts.append(
            f'Respond with JSON only: {{"action": "ATTACK"|"DEFEND"|"SPECIAL"|"RUN", "target": {all_names}}}'
        )
    elif special_visible:
        if on_cooldown:
            parts.append(f"3. SPECIAL: {spec_name} — [on cooldown, unavailable this turn]")
        else:
            parts.append(
                f"3. SPECIAL: {spec_name} — "
                f"[not enough MP — costs {mp_cost}, you have {member.mp}]"
            )
        parts.append("4. RUN — attempt to flee (low chance; increases each attempt)")
        parts.append("")
        parts.append(
            f'Respond with JSON only: {{"action": "ATTACK"|"DEFEND"|"RUN", "target": {all_names}}}'
        )
    else:
        parts.append("3. RUN — attempt to flee (low chance; increases each attempt)")
        parts.append("")
        parts.append(
            f'Respond with JSON only: {{"action": "ATTACK"|"DEFEND"|"RUN", "target": {all_names}}}'
        )

    return "\n".join(parts)


# ── overworld / voting ────────────────────────────────────────────────────────

def build_overworld_system_prompt(member) -> str:
    lines = [f"You are {member.name} (LVL {member.lvl}) exploring an overworld map with your party."]
    if getattr(member, 'personality', None):
        lines.append(f"Personality: {member.personality}")
    lines.append(
        "The party moves together by voting on a proposed direction.\n"
        "Be decisive. Pick actions that fit your character.\n"
        "Output JSON only — no other text, no explanation."
    )
    return "\n".join(lines)


def _dir_summary_line(label: str, ds, exit_open: bool = False) -> str:
    """One-line direction summary for LLM decision input."""
    if ds.kind == 'edge':
        terrain = ('screen edge (crossing available)' if exit_open
                   else 'screen edge')
        can_step = exit_open or ds.adjacent_passable
    elif ds.kind == 'blocker':
        terrain  = f"blocked by {ds.tile} at dist {ds.distance}"
        can_step = ds.adjacent_passable
    else:  # enterable
        terrain  = f"{ds.tile} (enterable) at dist {ds.distance}"
        can_step = ds.adjacent_passable
    return f"  {label}: can step? {'YES' if can_step else 'NO'} — {terrain}"


def build_overworld_context(member, party: list, vs, voting_state,
                            available_actions: set, member_journal=None,
                            screen_exits: dict | None = None) -> str:
    """Build the overworld/voting prompt for a single member's turn.

    Args:
        member:           current member being prompted (name, lvl, hp, max_hp, alive)
        party:            all members (same fields)
        vs:               ViewScan at current party position
        voting_state:     VotingState
        available_actions: set[str] from engine.voting.available_actions()
        member_journal:   MemberJournal for this member, or None if empty/unavailable
        screen_exits:     {'N': bool, 'S': bool, 'E': bool, 'W': bool} from exits_json;
                          when an edge direction is open the LLM learns crossing is available
    """
    parts = []
    exits = screen_exits or {}

    parts.append("DIRECTIONS FROM @")
    for label in ("N", "S", "E", "W"):
        ds = getattr(vs, label)
        exit_open = exits.get(label, False) if ds.kind == 'edge' else False
        parts.append(_dir_summary_line(label, ds, exit_open=exit_open))

    # Party roster
    parts.append("")
    parts.append("PARTY")
    for m in party:
        if not m.alive:
            hp_str = f"HP {m.hp}/{m.max_hp} [DEAD]"
        elif m.hp == m.max_hp:
            hp_str = f"HP {m.hp}/{m.max_hp}"
        else:
            hp_str = f"HP {m.hp}/{m.max_hp}"
        you = " ← YOU" if m.name == member.name else ""
        parts.append(f"  {m.name}: LVL {m.lvl}  {hp_str}{you}")

    # Short-term memory window — what this member has observed recently
    if member_journal and not member_journal.is_empty():
        parts.append("")
        parts.append("RECENT EVENTS (your memory)")
        parts.extend(member_journal.render())

    # Proposal state
    if voting_state.is_open:
        p = voting_state.proposal
        yes_names = [n for n, v in p.votes.items() if v]
        no_names  = [n for n, v in p.votes.items() if not v]
        still_out = [m.name for m in party
                     if m.alive and not p.has_voted(m.name) and m.name != member.name]
        parts.append("")
        parts.append("OPEN PROPOSAL")
        parts.append(f"  {p.initiator} proposes: {p.action_desc}")
        parts.append(f"  YES ({p.yes_count}/{p.threshold} needed): "
                     f"{', '.join(yes_names) or 'none'}")
        parts.append(f"  NO  ({p.no_count}): {', '.join(no_names) or 'none'}")
        if still_out:
            parts.append(f"  Still voting: {', '.join(still_out)}")

    # Options menu — strictly matches available_actions
    parts.append("")
    if "PROPOSE" in available_actions:
        parts.append("YOUR OPTIONS  (no proposal open — propose or wait)")
        parts.append('  PROPOSE a direction — only propose where "can step? YES"')
        parts.append('    {"action": "PROPOSE", "direction": "N"|"S"|"E"|"W", "steps": 1-8}')
        parts.append('  WAIT — skip your turn')
        parts.append('    {"action": "WAIT"}')
    elif "VOTE" in available_actions:
        parts.append("YOUR OPTIONS  (vote on the open proposal)")
        parts.append('  Vote YES: {"action": "VOTE", "vote": "yes"}')
        parts.append('  Vote NO:  {"action": "VOTE", "vote": "no"}')
        parts.append('  WAIT (abstain this turn): {"action": "WAIT"}')
    else:
        parts.append("YOUR OPTIONS  (already voted — wait for others)")
        parts.append('  {"action": "WAIT"}')

    parts.append("")
    parts.append("Respond with exactly one JSON object and nothing else.")
    return "\n".join(parts)


# ── goal-setting ──────────────────────────────────────────────────────────────

def build_goal_system_prompt(member) -> str:
    lines = [f"You are {member.name} (LVL {member.lvl}) in an exploration party."]
    if getattr(member, 'personality', None):
        lines.append(f"Personality: {member.personality}")
    lines.append(
        "Your party needs a navigation goal. Choose a target screen to travel toward.\n"
        "Output JSON only — no other text, no explanation."
    )
    return "\n".join(lines)


def build_goal_context(member, ctx, previous_goal=None) -> str:
    """Build the goal-setting prompt for a single member.

    Args:
        member:        the proposing member (needs .name for '← YOU' marker)
        ctx:           CuratedContext from build_curated_context()
        previous_goal: Goal that just ended, or None
    """
    parts = []

    parts.append(f"CURRENT POSITION: screen ({ctx.pos_sx},{ctx.pos_sy})")

    parts.append("")
    parts.append("PARTY STATUS")
    for m in ctx.party_status:
        status = "[DEAD]" if not m['alive'] else f"HP {m['hp']}/{m['max_hp']}"
        you = " ← YOU" if m['name'] == member.name else ""
        parts.append(f"  {m['name']}: LVL {m['lvl']}  {status}{you}")

    urgency = _health_urgency(ctx.party_status)
    if urgency:
        parts.append("")
        parts.append(f"HEALTH WARNING: {urgency}")
        towns = [p for p in ctx.pois if p['kind'] == 'town']
        if towns:
            parts.append("  Towns have healers that restore full HP and MP.")

    if ctx.pois:
        parts.append("")
        parts.append("KNOWN POINTS OF INTEREST")
        for p in ctx.pois[:16]:
            visited  = " (visited)" if p['visited'] else ""
            kind_tag = "town (healer)" if p['kind'] == 'town' else "dungeon (enemies)"
            dist_tag = f"  [{p['dist']} screens away]"
            parts.append(f"  screen ({p['sx']},{p['sy']}): {kind_tag}{visited}{dist_tag}")

    if ctx.visited_count:
        parts.append("")
        parts.append(f"VISITED SCREENS ({ctx.visited_count} total)")
        parts.append("  " + "  ".join(f"({x},{y})" for x, y in ctx.visited_sample))
        if ctx.visited_count > 12:
            parts.append(f"  ... and {ctx.visited_count - 12} more")

    if ctx.recent_events:
        parts.append("")
        parts.append("RECENT EVENTS")
        for e in ctx.recent_events:
            parts.append(f"  {e}")

    if previous_goal is not None:
        parts.append("")
        parts.append("PREVIOUS GOAL")
        parts.append(f"  {previous_goal.summary()}")

    parts.append("")
    parts.append("CHOOSE A NAVIGATION GOAL")
    parts.append("  goal_type 'explore': head into unexplored territory")
    parts.append("  goal_type 'travel':  return to a known screen or POI")
    parts.append("  target_sx, target_sy: screen coordinates (may be unvisited)")
    parts.append("")
    parts.append(
        '  {"goal_type": "explore"|"travel", '
        '"target_sx": int, "target_sy": int, "reasoning": "brief"}'
    )
    parts.append("")
    parts.append("Respond with exactly one JSON object and nothing else.")
    return "\n".join(parts)


# ── checkpoint discussion ─────────────────────────────────────────────────────

_CHECKPOINT_REASON_DESC: dict[str, str] = {
    'goal_reached':       'Your party has arrived at the goal destination.',
    'branch_point':       'Multiple paths forward — a decision is needed.',
    'path_blocked':       'No path found through this screen toward the goal.',
    'screen_blocked':     'No path found through this screen toward the goal.',
    'all_exits_blocked':  'All exits from this screen are closed.',
    'battle_wounds':      'The party just won a battle but took serious casualties.',
}


def build_interior_system_prompt(member, kind: str) -> str:
    lines = [f"You are {member.name} (LVL {member.lvl}) leading your party through a {kind}."]
    if getattr(member, 'personality', None):
        lines.append(f"Personality: {member.personality}")
    lines.append(
        "Choose where to go based on the party's needs and what remains unexplored.\n"
        "Output JSON only — no other text, no explanation."
    )
    return "\n".join(lines)


def build_interior_goal_context(member, kind: str, reason: str,
                                 available_pois: list[dict],
                                 visited_names: list[str],
                                 party: list) -> str:
    """Build the interior navigation pick prompt.

    Args:
        member:         the deciding member
        kind:           'cave' or 'town'
        reason:         'entered' | 'arrived at <name>' | 'continuing'
        available_pois: list of {'name': str, 'label': str} dicts not yet visited
        visited_names:  POI names already visited this interior session
        party:          full party list (for HP display)
    """
    parts = []

    parts.append(f"INTERIOR: {kind.upper()}")
    parts.append(f"SITUATION: {reason}")

    parts.append("")
    parts.append("PARTY STATUS")
    for m in party:
        if not m.alive:
            hp_str = f"HP {m.hp}/{m.max_hp} [DEAD]"
        else:
            hp_str = f"HP {m.hp}/{m.max_hp}"
        you = " ← YOU" if m.name == member.name else ""
        parts.append(f"  {m.name}: LVL {m.lvl}  {hp_str}{you}")

    if visited_names:
        parts.append("")
        parts.append("ALREADY VISITED")
        for n in visited_names:
            parts.append(f"  {n}")

    parts.append("")
    parts.append("AVAILABLE DESTINATIONS")
    for i, poi in enumerate(available_pois, 1):
        parts.append(f"  {i}. {poi['name']} — {poi['label']}")

    # Urgency note: surface healer relevance when party is hurt, without prescribing the choice
    healer_available = any(p['name'] == 'healer' for p in available_pois)
    if kind == 'town' and healer_available:
        urgency = _health_urgency(party)
        if urgency:
            parts.append("")
            parts.append(f"NOTE: {urgency}. The healer here restores full HP and MP to all living members.")

    avail_names = "|".join(f'"{p["name"]}"' for p in available_pois)
    parts.append("")
    parts.append(f'Respond with exactly one JSON object: {{"target": {avail_names}, "reasoning": "brief"}}')
    return "\n".join(parts)


def build_checkpoint_system_prompt(member) -> str:
    lines = [f"You are {member.name} (LVL {member.lvl}) making a navigation decision for your party."]
    if getattr(member, 'personality', None):
        lines.append(f"Personality: {member.personality}")
    lines.append(
        "Assess the situation and decide whether to continue, abandon, or modify the current goal.\n"
        "Output JSON only — no other text, no explanation."
    )
    return "\n".join(lines)


def _vs_direction_line(label: str, ds) -> str:
    """One-line ViewScan direction summary for checkpoint context."""
    if ds.kind == 'edge':
        return f"  {label}: screen edge"
    elif ds.kind == 'blocker':
        return f"  {label}: blocked by {ds.tile} — {ds.distance} tiles"
    else:  # enterable
        return f"  {label}: {ds.tile} (enterable) — {ds.distance} tiles"


def build_checkpoint_context(member, ctx, reason: str,
                              vs=None, enemies_nearby: list | None = None) -> str:
    """Build the checkpoint discussion prompt.

    Args:
        member:          the deciding member (needs .name for '← YOU' marker)
        ctx:             CuratedContext from build_curated_context()
        reason:          checkpoint trigger name (e.g. 'goal_reached', 'branch_point')
        vs:              ViewScan at current position, or None
        enemies_nearby:  list of {'name': str, 'distance': int, 'direction': str}, or None
    """
    parts = []

    situation = _CHECKPOINT_REASON_DESC.get(reason, reason)
    parts.append(f"SITUATION: {situation}")
    parts.append(f"CURRENT POSITION: screen ({ctx.pos_sx},{ctx.pos_sy})")

    parts.append("")
    if ctx.active_goal:
        parts.append(f"CURRENT GOAL: {ctx.active_goal.summary()}")
    else:
        parts.append("CURRENT GOAL: none")

    parts.append("")
    parts.append("PARTY STATUS")
    for m in ctx.party_status:
        status = "[DEAD]" if not m['alive'] else f"HP {m['hp']}/{m['max_hp']}"
        you = " ← YOU" if m['name'] == member.name else ""
        parts.append(f"  {m['name']}: LVL {m['lvl']}  {status}{you}")

    urgency = _health_urgency(ctx.party_status)
    if urgency:
        parts.append("")
        parts.append(f"HEALTH WARNING: {urgency}")
        towns = [p for p in ctx.pois if p['kind'] == 'town']
        if towns:
            parts.append("  Towns have healers that restore full HP and MP.")

    if vs is not None:
        parts.append("")
        parts.append("WHAT YOU CAN SEE")
        for label in ("N", "S", "E", "W"):
            parts.append(_vs_direction_line(label, getattr(vs, label)))

    if enemies_nearby:
        parts.append("")
        parts.append("ENEMIES NEARBY")
        for e in enemies_nearby:
            parts.append(f"  {e['name']} — {e['distance']} tiles {e['direction']}")

    if ctx.recent_events:
        parts.append("")
        parts.append("RECENT EVENTS")
        for e in ctx.recent_events:
            parts.append(f"  {e}")

    if ctx.pois:
        parts.append("")
        parts.append("KNOWN POINTS OF INTEREST")
        for p in ctx.pois[:16]:
            visited  = " (visited)" if p['visited'] else ""
            kind_tag = "town (healer)" if p['kind'] == 'town' else "dungeon (enemies)"
            dist_tag = f"  [{p['dist']} screens away]"
            parts.append(f"  screen ({p['sx']},{p['sy']}): {kind_tag}{visited}{dist_tag}")

    parts.append("")
    parts.append("DECIDE")
    parts.append("  continue — keep current goal, proceed as planned")
    parts.append("  abandon  — drop goal; party will set a new one next")
    if reason == 'battle_wounds':
        parts.append("  modify   — set a new goal now (e.g. travel to a nearby town to heal)")
    else:
        parts.append("  modify   — set a new goal now (provide target below)")
    parts.append("")
    parts.append(
        '  {"decision": "continue"|"abandon"|"modify", '
        '"goal_type": "explore"|"travel", '
        '"target_sx": int, "target_sy": int, "reasoning": "brief"}'
    )
    parts.append("  (goal_type/target only required when decision is modify)")
    parts.append("")
    parts.append("Respond with exactly one JSON object and nothing else.")
    return "\n".join(parts)
