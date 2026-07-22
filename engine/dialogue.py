"""Dialogue-box state — pure logic, no pygame/graphics.

Mirrors the engine.menu split: this module only tracks whether a dialogue
box is open, which page is showing, and how much of it has "typed in" so
far. engine.input decides *when* to open it (A pressed while facing an
interactable sign/NPC) and when to advance or close it (A pressed again);
engine.renderer decides how it looks on screen and feeds elapsed time into
tick() every frame (holding A/B ticks it faster — see its ms_per_char arg).

Ordinary sign/NPC/container dialogue never sets `choices` (open()'s second
arg defaults to none), so is_showing_choices() is always False for it and
advance() behaves exactly as it always has. Choices are a cutscene
`dialogue` step feature only (see engine.cutscene.CutsceneChoice,
engine.renderer.OverworldScene._cutscene_step_dialogue/
_confirm_dialogue_choice) — a response list shown once the last page's
fully revealed, N/S-cursor + A-confirm same as every other list-driven menu
in this game (engine.menu.StartMenu and friends).
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

    `choices`, if opened with any, turns the *last* page into a response
    prompt instead of a plain "press A to close" page once it's fully
    revealed (see is_showing_choices) -- move_choice_cursor/confirm_choice
    replace advance() for that one page; advance() itself becomes a no-op
    while choices are showing, rather than closing the box, so a caller
    that doesn't know about choices at all (there isn't one currently, but
    nothing prevents ordinary dialogue from gaining this later) can't
    accidentally skip past an unresolved choice.
    """
    is_open:     bool      = False
    pages:       list[str] = field(default_factory=list)
    page_index:  int       = 0
    chars_shown: int       = 0
    choices:       list[str] = field(default_factory=list)
    choice_cursor: int       = 0
    _reveal_ms:  float     = field(default=0.0, repr=False)

    def open(self, pages: list[str], choices: list[str] | None = None) -> None:
        self.is_open = True
        self.pages = pages
        self.page_index = 0
        self.chars_shown = 0
        self._reveal_ms = 0.0
        self.choices = list(choices) if choices else []
        self.choice_cursor = 0

    def close(self) -> None:
        self.is_open = False
        self.pages = []
        self.page_index = 0
        self.chars_shown = 0
        self._reveal_ms = 0.0
        self.choices = []
        self.choice_cursor = 0

    def is_showing_choices(self) -> bool:
        """True the instant a response list should be interactable instead
        of "press A to close" -- the box is open, was given choices, is on
        its last page, and that page has fully typed in. False for any
        dialogue opened with no choices at all (the ordinary case)."""
        return (self.is_open and bool(self.choices)
                and self.page_index == len(self.pages) - 1
                and self.is_fully_revealed())

    def move_choice_cursor(self, delta: int) -> None:
        """Move the response cursor by `delta` (+1/-1), wrapping — same
        convention as engine.menu.StartMenu's cursor. A no-op if choices
        aren't actually showing (nothing to move)."""
        if self.choices:
            self.choice_cursor = (self.choice_cursor + delta) % len(self.choices)

    def confirm_choice(self) -> int | None:
        """The chosen response's index, or None if choices aren't showing
        yet (still typing, not opened with any, or not on that page) --
        the caller resolves what that choice actually *does* (this class
        has no notion of consequences) and must call close() itself
        afterward, same as advance() closing on an ordinary last page."""
        if not self.is_showing_choices():
            return None
        return self.choice_cursor

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

        Also a no-op while is_showing_choices() is true -- that page isn't
        "press A to close," it's a response prompt; use confirm_choice()
        instead. Without this, an ordinary A-press-to-advance caller could
        accidentally close a cutscene's choice prompt without ever
        resolving which response was picked.
        """
        if not self.is_fully_revealed():
            return False
        if self.is_showing_choices():
            return False
        if self.page_index + 1 < len(self.pages):
            self.page_index += 1
            self.chars_shown = 0
            self._reveal_ms = 0.0
            return False
        self.close()
        return True
