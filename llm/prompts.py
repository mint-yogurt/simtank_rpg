"""Prompt templates and battle context renderer."""


HEAL_NEEDED_THRESHOLD = 0.70  # SNACK flagged as low-value if no one is below this fraction of max HP

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
    parts.append(f"Name: {member.name}  LVL: {member.lvl}  HP: {member.hp}/{member.max_hp}")

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

    parts.append("")
    parts.append("YOUR OPTIONS")
    parts.append("1. ATTACK — strike the enemy")
    parts.append("2. DEFEND — reduce incoming damage this turn")
    if member.special["name"]:
        spec = member.special
        if spec.get("effect_type") == "heal":
            anyone_hurt = any(
                m.alive and m.hp < m.max_hp * HEAL_NEEDED_THRESHOLD
                for m in party
            )
            if anyone_hurt:
                parts.append(f"3. SPECIAL: {spec['name']} — {spec['description']}")
            else:
                parts.append(f"3. SPECIAL: {spec['name']} — (party is healthy, low value this turn)")
        elif spec.get("status_effect") and enemy.status:
            parts.append(
                f"3. SPECIAL: {spec['name']} — "
                f"({enemy.name} is already {enemy.status} — no additional effect this turn)"
            )
        else:
            parts.append(f"3. SPECIAL: {member.special['name']} — {member.special['description']}")
    else:
        parts.append("3. SPECIAL — use your special move")
    parts.append("4. RUN — attempt to flee (low chance; increases each attempt)")

    all_names = "|".join(m.name for m in party) + f"|{enemy.name}|null"
    parts.append("")
    parts.append(
        f'Respond with JSON only: {{"action": "ATTACK"|"DEFEND"|"SPECIAL"|"RUN", "target": {all_names}}}'
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


def _dir_summary_line(label: str, ds) -> str:
    """One-line direction summary for LLM decision input."""
    if ds.kind == 'edge':
        terrain = 'screen edge'
    elif ds.kind == 'blocker':
        terrain = f"blocked by {ds.tile} at dist {ds.distance}"
    else:  # enterable
        terrain = f"{ds.tile} (enterable) at dist {ds.distance}"
    can_step = "YES" if ds.adjacent_passable else "NO"
    return f"  {label}: can step? {can_step} — {terrain}"


def build_overworld_context(member, party: list, vs, voting_state,
                            available_actions: set, member_journal=None) -> str:
    """Build the overworld/voting prompt for a single member's turn.

    Args:
        member:           current member being prompted (name, lvl, hp, max_hp, alive)
        party:            all members (same fields)
        vs:               ViewScan at current party position
        voting_state:     VotingState
        available_actions: set[str] from engine.voting.available_actions()
        member_journal:   MemberJournal for this member, or None if empty/unavailable
    """
    parts = []

    parts.append("DIRECTIONS FROM @")
    for label in ("N", "S", "E", "W"):
        parts.append(_dir_summary_line(label, getattr(vs, label)))

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
