"""Start-menu state — pure logic, no pygame/graphics.

Mirrors the player/input split: this module only tracks whether the menu is
open and which option is highlighted. engine.input decides *when* to call
into it (START/B/A presses, direction routing while open); engine.renderer
decides how it looks on screen. Sub-screens for each option (inventory,
party, settings, save) aren't built yet — confirm() is a stub.
"""

from dataclasses import dataclass, field

OPTIONS: tuple[str, ...] = ("INVENTORY", "PARTY", "SETTINGS", "SAVE")


@dataclass
class StartMenu:
    """Cursor state for the paused-gameplay start menu.

    A vertical list: only N/S move the cursor, and it wraps top-to-bottom.
    """
    is_open:  bool = False
    selected: int  = field(default=0)

    def open(self) -> None:
        self.is_open = True
        self.selected = 0   # always opens on INVENTORY

    def close(self) -> None:
        self.is_open = False

    def move_cursor(self, direction: str) -> None:
        """Move the highlight up/down. E/W are no-ops — this is a vertical list."""
        if direction == "N":
            self.selected = (self.selected - 1) % len(OPTIONS)
        elif direction == "S":
            self.selected = (self.selected + 1) % len(OPTIONS)

    def selected_option(self) -> str:
        return OPTIONS[self.selected]

    def confirm(self) -> str:
        """Return the highlighted option. Sub-screens aren't wired up yet."""
        return OPTIONS[self.selected]


SAVE_OPTIONS: tuple[str, ...] = ("YES", "NO")


@dataclass
class SaveMenu:
    """Save-confirmation overlay, opened from SAVE on the start menu and
    drawn on top of it. A horizontal pair: only E/W move the cursor.
    """
    is_open:  bool = False
    selected: int  = field(default=1)   # defaults to NO

    def open(self) -> None:
        self.is_open = True
        self.selected = 1   # always opens on NO

    def close(self) -> None:
        self.is_open = False

    def move_cursor(self, direction: str) -> None:
        """Move the highlight left/right. N/S are no-ops. Only two options,
        so any E/W press just toggles between them."""
        if direction in ("E", "W"):
            self.selected = 1 - self.selected

    def selected_option(self) -> str:
        return SAVE_OPTIONS[self.selected]

    def confirm(self) -> str:
        """Return the highlighted option.

        TODO: the actual save (writing game state to disk) isn't scoped yet.
        Once it exists, YES should perform the save and then close this
        overlay back to the start menu, same as NO does today. For now YES
        does nothing — only NO/B close the overlay.
        """
        return SAVE_OPTIONS[self.selected]
