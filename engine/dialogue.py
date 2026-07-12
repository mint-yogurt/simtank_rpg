"""Dialogue-box state — pure logic, no pygame/graphics.

Mirrors the engine.menu split: this module only tracks whether a dialogue
box is open, which page is showing, and how much of it has "typed in" so
far. engine.input decides *when* to open it (A pressed while facing an
interactable sign/NPC) and when to advance or close it (A pressed again);
engine.renderer decides how it looks on screen and feeds elapsed time into
tick() every frame (holding A/B ticks it faster — see its ms_per_char arg).
"""

from dataclasses import dataclass, field


@dataclass
class DialogueBox:
    """Paged text overlay, opened by facing an interactable and pressing A.

    Each page "types in" one character at a time (tick()) rather than
    appearing all at once. A press only advances to the next page — or
    closes the box, on the last one — once the current page has fully typed
    in; a press while it's still typing is a no-op, so the player always
    gets a beat to read before moving on.
    """
    is_open:     bool      = False
    pages:       list[str] = field(default_factory=list)
    page_index:  int       = 0
    chars_shown: int       = 0
    _reveal_ms:  float     = field(default=0.0, repr=False)

    def open(self, pages: list[str]) -> None:
        self.is_open = True
        self.pages = pages
        self.page_index = 0
        self.chars_shown = 0
        self._reveal_ms = 0.0

    def close(self) -> None:
        self.is_open = False
        self.pages = []
        self.page_index = 0
        self.chars_shown = 0
        self._reveal_ms = 0.0

    def current_page(self) -> str | None:
        if not self.pages:
            return None
        return self.pages[self.page_index]

    def visible_text(self) -> str:
        """The current page's text, truncated to what's typed in so far."""
        page = self.current_page()
        return "" if page is None else page[:self.chars_shown]

    def is_fully_revealed(self) -> bool:
        page = self.current_page()
        return page is None or self.chars_shown >= len(page)

    def tick(self, dt_ms: float, ms_per_char: float) -> None:
        """Reveal more of the current page as time passes.

        `ms_per_char` is how long each newly-revealed character takes —
        pass a smaller value while A/B is held to type in faster. No-op
        once the current page is fully revealed.
        """
        page = self.current_page()
        if page is None or self.chars_shown >= len(page) or ms_per_char <= 0:
            return
        self._reveal_ms += dt_ms
        while self._reveal_ms >= ms_per_char and self.chars_shown < len(page):
            self._reveal_ms -= ms_per_char
            self.chars_shown += 1

    def advance(self) -> bool:
        """Move to the next page, or close if already on the last one.

        No-op (returns False) while the current page is still typing in —
        the caller must wait for a full reveal before an A press does
        anything. Returns True if this call closed the box (caller should
        reset the player's state back to IDLE).
        """
        if not self.is_fully_revealed():
            return False
        if self.page_index + 1 < len(self.pages):
            self.page_index += 1
            self.chars_shown = 0
            self._reveal_ms = 0.0
            return False
        self.close()
        return True
