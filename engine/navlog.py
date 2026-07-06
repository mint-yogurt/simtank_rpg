"""Append-only navigation event log for simtank_rpg.

The unbounded raw journal for overworld navigation events.  The curated
context builder reads from it; nothing ever modifies or trims it.

Notable types fed to the curated builder: SCREEN | GOAL | CHECKPOINT | ENTERED
MOVE events are excluded — too numerous, not notable at the goal level.
"""

from dataclasses import dataclass

_NOTABLE: frozenset[str] = frozenset({'SCREEN', 'GOAL', 'CHECKPOINT', 'ENTERED'})


@dataclass
class NavEntry:
    tick: int
    event_type: str
    desc: str


class NavLog:
    def __init__(self) -> None:
        self._entries: list[NavEntry] = []

    def append(self, tick: int, event_type: str, desc: str) -> None:
        self._entries.append(NavEntry(tick=tick, event_type=event_type, desc=desc))

    def last_notable(self, n: int,
                     notable: frozenset[str] = _NOTABLE) -> list[NavEntry]:
        """Return up to the last n entries whose type is in notable (oldest first)."""
        filtered = [e for e in self._entries if e.event_type in notable]
        return filtered[-n:]

    def __len__(self) -> int:
        return len(self._entries)
