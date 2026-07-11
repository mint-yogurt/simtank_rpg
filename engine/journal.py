"""Event journal: structured log + narrative renderer. Milestones only.

Journal — global milestone log + narrative strings.
          Narrative is ALL-CAPS retro-style, e.g. "PARTY DEFEATED LVL 2 GOBLIN."
          Used for high-level display / recap.
"""

# ── global milestone journal ───────────────────────────────────────────────────

_TEMPLATES = {
    "BATTLE_WIN":     lambda e: f"PARTY DEFEATED LVL {e['enemy_lvl']} {e['enemy']}.",
    "BATTLE_LOSS":    lambda e: "THE PARTY HAS FALLEN.",
    "BATTLE_FLEE":    lambda e: "PARTY FLED. NO XP GAINED.",
    "MEMBER_DIED":    lambda e: f"{e['member']} WAS SLAIN.",
    "MEMBER_LEVELUP": lambda e: f"{e['member']} REACHED LVL {e['new_lvl']}.",
    # ENEMY_KILLED goes to structured log only — BATTLE_WIN covers narrative
}


class Journal:
    def __init__(self):
        self._log: list[dict] = []
        self._narrative: list[str] = []

    def log_event(self, event: dict) -> None:
        self._log.append(event)
        renderer = _TEMPLATES.get(event["type"])
        if renderer:
            self._narrative.append(renderer(event))

    def render_recent(self, n: int = 5) -> list[str]:
        return self._narrative[-n:]

    def get_log(self) -> list[dict]:
        return list(self._log)
