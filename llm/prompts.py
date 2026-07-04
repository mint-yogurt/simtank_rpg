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
