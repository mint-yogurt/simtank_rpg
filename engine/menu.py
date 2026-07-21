"""Start-menu state — pure logic, no pygame/graphics.

Mirrors the player/input split: this module only tracks whether the menu is
open and which option is highlighted. engine.input decides *when* to call
into it (START/B/A presses, direction routing while open); engine.renderer
decides how it looks on screen. Sub-screens for PARTY/SETTINGS aren't built
yet — confirm() is still a stub for those. SAVE opens SaveMenu; INVENTORY
opens InventoryMenu, both below.
"""

from dataclasses import dataclass, field

from engine.inventory import CATEGORIES

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
        """Return the highlighted option. This class doesn't perform the
        save itself (no file I/O here, same as every other menu in this
        module never touching pygame) — engine.input.handle_a_button calls
        engine.save.save_to_slot() on YES, then closes this overlay back to
        the start menu the same way NO does."""
        return SAVE_OPTIONS[self.selected]


SCALE_OPTIONS: tuple[int, ...] = (1, 2, 3)


@dataclass
class SettingsMenu:
    """Settings-screen overlay, opened from SETTINGS on the start menu and
    drawn on top of it — same tier as SaveMenu/InventoryMenu (only one of
    the three overlays is ever open at once, see engine.input).

    Two rows: SCALE (window scale, 1x/2x/3x) and DISPLAY MODE (windowed /
    fullscreen). N/S moves the highlighted row and wraps (only two rows,
    same idea as StartMenu's vertical wrap); E/W changes the highlighted
    row's value and also wraps. There's no confirm step — a value takes
    effect the instant it changes, since `scale`/`fullscreen` are read
    directly off this object by the app loop (see engine.renderer.run(),
    polled via OverworldScene.display_scale/fullscreen) rather than staged
    behind an A press.
    """
    is_open:    bool = False
    selected:   int  = field(default=0)
    scale:      int  = field(default=3)
    fullscreen: bool = field(default=False)

    def open(self) -> None:
        self.is_open = True
        self.selected = 0   # always opens on the SCALE row

    def close(self) -> None:
        self.is_open = False

    def move_cursor(self, direction: str) -> None:
        """N/S switches row (wraps). E/W changes the highlighted row's
        value: SCALE cycles through SCALE_OPTIONS (wraps either way);
        DISPLAY MODE just flips windowed/fullscreen regardless of which of
        E/W was pressed, same as SaveMenu's YES/NO toggle."""
        if direction == "N":
            self.selected = (self.selected - 1) % 2
        elif direction == "S":
            self.selected = (self.selected + 1) % 2
        elif direction == "E" or direction == "W":
            if self.selected == 0:
                step = 1 if direction == "E" else -1
                idx = SCALE_OPTIONS.index(self.scale)
                self.scale = SCALE_OPTIONS[(idx + step) % len(SCALE_OPTIONS)]
            else:
                self.fullscreen = not self.fullscreen


@dataclass
class InventoryMenu:
    """Inventory-screen overlay, opened from INVENTORY on the start menu and
    drawn on top of it — same tier as SaveMenu (see engine.input for how B
    picks whichever of the two is on top).

    One page per category (engine.inventory.CATEGORIES): E/W switches pages
    and wraps, same as SaveMenu's YES/NO pair. N/S scrolls the current
    page's item list and *clamps* at either end instead of wrapping — a
    deliberate difference from every other cursor in this module, since an
    open-ended scrollable list reads as "stuck" if it wraps but a fixed
    4-option row doesn't.

    This module holds no item data of its own (no Inventory, no ItemDefs) —
    same split as StartMenu holding no game state beyond its own cursor.
    N/S scrolling needs to know how long the current page's list is to know
    where to clamp, so callers pass that in per call rather than this class
    reaching into engine.inventory itself.
    """
    is_open:  bool = False
    category: int  = field(default=0)
    selected: int  = field(default=0)

    def open(self) -> None:
        self.is_open = True
        self.category = 0   # always opens on the first category (ITEMS)
        self.selected = 0

    def close(self) -> None:
        self.is_open = False

    def selected_category(self) -> str:
        return CATEGORIES[self.category]

    def move_cursor(self, direction: str, list_len: int) -> None:
        """Move the highlight. E/W switches category (wraps) and resets the
        scroll position to the top — this class doesn't know the other
        category's list length, so it can't do anything smarter than that.
        N/S scrolls within the current category, clamped to [0, list_len).
        """
        if direction == "E":
            self.category = (self.category + 1) % len(CATEGORIES)
            self.selected = 0
        elif direction == "W":
            self.category = (self.category - 1) % len(CATEGORIES)
            self.selected = 0
        elif direction == "N":
            self.selected = max(0, self.selected - 1)
        elif direction == "S":
            self.selected = min(max(list_len - 1, 0), self.selected + 1)


BUY, SELL = "buy", "sell"


@dataclass
class ShopMenu:
    """Shop-screen overlay for talking to a shopkeeper (a `shop`-type NPC —
    see engine.renderer.NPC/OverworldScene._load_map; a shop is a person
    first, built from the same npcs_<map>.yaml pipeline as any other NPC,
    not a separate static object). Opened by facing the shopkeeper and
    pressing A, same as any NPC — not from the start menu, unlike every
    other menu in this module — see engine.input for the open/close wiring
    and engine.player.PlayerState.IN_SHOP.

    The interaction is talk-then-shop, not straight-to-menu: A on a
    shopkeeper opens a normal dialogue box with their greeting first;
    `pending_shop` (the shopkeeper's name) is set at that moment and
    consumed the instant that dialogue box closes, which is what actually
    calls `open()` and flips to the buy/sell screen — see
    engine.input.handle_a_button. Backing all the way out of the buy/sell
    screen (B, not mid quantity-pick) closes this menu and, if the
    shopkeeper has a farewell line, shows that in a dialogue box too before
    returning to idle — see engine.input.handle_b_button.

    Holds no shop/item/inventory data itself, same split as every other
    class here: `shop_name` is just the shopkeeper NPC's `name`, which the
    caller (engine.input/engine.renderer) resolves back to the actual
    stock/dialogue each time it's needed, the same "identify by name" idiom
    engine.game_state.persistent_id already uses for containers.

    Two modes (BUY/SELL, E/W toggles, same idiom as InventoryMenu's
    category switch), each with its own scrollable list (N/S, clamped, same
    idiom as InventoryMenu's item list). Confirming a row (A) enters a
    nested quantity-picker sub-state (`picking_amount`) rather than
    transacting immediately — E/W adjusts `amount` there (same idiom as
    SettingsMenu's SCALE row), A confirms the transaction, B cancels back
    to the list without transacting.
    """
    is_open:        bool = False
    shop_name:      str | None = None
    pending_shop:   str | None = None   # set on A-press-shopkeeper; consumed when the greeting dialogue closes
    mode:           str = BUY
    cursor:         int = 0
    picking_amount: bool = False
    amount:         int = 1
    message:        str | None = None   # one-line transaction feedback

    def open(self, shop_name: str) -> None:
        self.is_open = True
        self.shop_name = shop_name
        self.pending_shop = None
        self.mode = BUY
        self.cursor = 0
        self.picking_amount = False
        self.amount = 1
        self.message = None

    def close(self) -> None:
        self.is_open = False
        self.shop_name = None
        self.pending_shop = None

    def start_amount_pick(self) -> None:
        self.picking_amount = True
        self.amount = 1

    def cancel_amount(self) -> None:
        self.picking_amount = False
        self.amount = 1

    def move_cursor(self, direction: str, list_len: int, max_amount: int = 1) -> None:
        """Route a direction press: adjusts `amount` while picking a
        transaction quantity, otherwise toggles BUY/SELL (E/W, resetting
        the cursor since the two modes' lists differ) or scrolls the
        current mode's list (N/S, clamped like InventoryMenu's)."""
        self.message = None
        if self.picking_amount:
            if direction == "E":
                self.amount = min(max_amount, self.amount + 1)
            elif direction == "W":
                self.amount = max(1, self.amount - 1)
            return
        if direction in ("E", "W"):
            self.mode = SELL if self.mode == BUY else BUY
            self.cursor = 0
        elif direction == "N":
            self.cursor = max(0, self.cursor - 1)
        elif direction == "S":
            self.cursor = min(max(list_len - 1, 0), self.cursor + 1)
