"""Two-layer event journal: structured log + narrative renderer. Milestones only.

Journal       — global milestone log + narrative strings (unchanged scaffold).
                Narrative is ALL-CAPS retro-style, e.g. "PARTY DEFEATED LVL 2 GOBLIN."
                Used for high-level display / recap, not LLM context.

MemberJournal — per-member rolling FIFO window for LLM context injection.
                Each entry: (tick, event_type, terse description).
                Wire into execute_move, voting call sites, and screen transitions.
                Out of scope for this tier: hearsay vs. observed distinction,
                long-term compression. Those come later.
"""

from collections import deque
from dataclasses import dataclass


# ── global milestone journal (original scaffold) ──────────────────────────────

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


# ── per-member rolling LLM context window ────────────────────────────────────

WINDOW_SIZE = 12  # last N events per member; ~120 tokens at 40 chars/entry,
                  # well within the 600-800 token call budget


@dataclass
class JournalEntry:
    tick: int
    event_type: str  # 'MOVE'|'PROPOSE'|'VOTE'|'RESOLVED'|'ENTERED'|'SCREEN'
    desc: str        # terse one-liner, target ≤60 chars


class MemberJournal:
    """Per-member rolling window of recent events for LLM context injection.

    FIFO: oldest entries drop automatically when the window is full.
    All members in the party receive the same events today (no hearsay/observed
    distinction yet — that's a later job). Per-member instances are kept separate
    so that distinction is easy to add without refactoring callers.

    Battle events are wired by run_cli.py / the battle loop — not this module.
    """

    def __init__(self, member_name: str, window: int = WINDOW_SIZE):
        self.member_name = member_name
        self._entries: deque[JournalEntry] = deque(maxlen=window)

    def append(self, tick: int, event_type: str, desc: str) -> None:
        """Record one event. Oldest entry drops automatically at window limit."""
        self._entries.append(JournalEntry(tick=tick, event_type=event_type, desc=desc))

    def is_empty(self) -> bool:
        return len(self._entries) == 0

    def __len__(self) -> int:
        return len(self._entries)

    def render(self) -> list[str]:
        """Oldest → newest, formatted for prompt injection."""
        return [f"  t{e.tick}  {e.desc}" for e in self._entries]


def journals_append(journals: dict | None, tick: int, event_type: str, desc: str) -> None:
    """Broadcast one event to every MemberJournal in the dict. None-safe."""
    if journals:
        for mj in journals.values():
            mj.append(tick, event_type, desc)
