"""Framework-agnostic player input resolution.

Renderers own raw key polling (pygame key codes → direction strings/button
names) and visual tweening; this module owns the actual decision logic —
which direction, if any, should be stepped this frame, and what a button
press (START/A/B) or a direction press means given the player's current
state. No pygame dependency, so it's independently testable.

The start menu (engine.menu.StartMenu) and the two overlays it can open —
engine.menu.SaveMenu (from SAVE) and engine.menu.InventoryMenu (from
INVENTORY) — never read input themselves — they only expose state mutators
(open/close/move_cursor/confirm). This module is the one place that decides
when to call them, including which one is "on top" (whichever of SaveMenu /
InventoryMenu is open, since only one ever is at a time — both only ever
open from the start menu) and should receive input first.

engine.dialogue.DialogueBox follows the same rule: it never reads input
itself, only open()/close()/advance(). A pressed while a dialogue box is
open advances/closes it; A pressed while idle and facing an interactable
(sign, NPC, or container) opens one instead — see handle_a_button, and
_open_container for how a container's dialogue also grants its `contents`
item and sets a flag (engine.game_state) the first time it's opened. SAVE's
YES/NO overlay writes to disk via engine.save on YES — see handle_a_button.
"""

from engine.dialogue import DialogueBox
from engine.game_state import GameState, persistent_id
from engine.inventory import Inventory
from engine.menu import InventoryMenu, SaveMenu, SettingsMenu, StartMenu
from engine.player import Player, PlayerState
from engine.save import save_to_slot


class HeldDirectionInput:
    """Tracks currently-held cardinal directions and resolves which one (if
    any) should be stepped this frame.

    "Last pressed wins": only the most recently pressed direction is ever
    returned, so two directions held together never combine into a diagonal.

    Steps are rate-limited by a cooldown that runs continuously — it is not
    reset by press()/release(), only by an actual step firing. This matters:
    resetting the cooldown on every fresh key-down (the previous behavior)
    let rapid re-presses of a key ("mashing") bypass the repeat interval
    entirely, firing far more engine steps than `repeat_ms` should allow —
    the character would step several tiles almost instantly, and since the
    renderer's per-step tween is exactly `repeat_ms` long, the visual glide
    would restart mid-flight on every one of those steps, dragging a
    leftover fractional offset from the interrupted tween into the next one
    and producing a visibly diagonal slide even though the engine itself
    only ever moves one cardinal direction at a time. Gating strictly on
    elapsed time keeps steps (and therefore tweens) back-to-back with no
    overlap, which fixes both symptoms at once.
    """

    _TURN_COOLDOWN_MS = 75   # fixed delay before a turn-in-place becomes a step

    def __init__(self, repeat_ms: int):
        self._repeat_ms = repeat_ms
        self._held: list[str] = []
        self._cooldown_ms: float = 0.0   # 0 == ready to fire on next tick

    def press(self, direction: str, facing: str | None = None) -> bool:
        """Register a held direction press. Returns True if this press
        should just turn the player in place rather than queue a step.

        That happens when it's a fresh press (the key wasn't already down)
        of a direction other than `facing` — the standard RPG "flip
        around": tapping a new direction turns you without moving, and only
        continuing to hold it past `_TURN_COOLDOWN_MS` actually steps. That
        cooldown is a fixed, short delay rather than a full repeat interval
        — long enough that a brief tap never sneaks in a step, but short
        enough that holding through a turn doesn't feel like it stalls
        movement. A press matching `facing` already (or a caller that
        doesn't pass `facing`, e.g. menu navigation) behaves as before:
        queued immediately, eligible to step on the very next tick.
        """
        is_new = direction not in self._held
        if is_new:
            self._held.append(direction)
        if is_new and facing is not None and direction != facing:
            self._cooldown_ms = min(self._repeat_ms, self._TURN_COOLDOWN_MS)
            return True
        return False

    def release(self, direction: str) -> None:
        if direction in self._held:
            self._held.remove(direction)

    def tick(self, dt_ms: float) -> str | None:
        """Advance by dt_ms; return a direction to step this frame, or None."""
        self._cooldown_ms = max(0.0, self._cooldown_ms - dt_ms)
        if self._held and self._cooldown_ms == 0.0:
            self._cooldown_ms = self._repeat_ms
            return self._held[-1]
        return None


# ── Start menu routing ───────────────────────────────────────────────────────
#
# Discrete (non-held) button presses. START/B/A only mean something in
# specific player states, so the gating lives here rather than in the menu
# itself or in the renderer. Whichever of the save-confirm / inventory
# overlays is open sits on top of the start menu, so it always gets first
# refusal at B/A/direction input.

def handle_start_button(player: Player, menu: StartMenu, save_menu: SaveMenu,
                         inventory_menu: InventoryMenu, settings_menu: SettingsMenu) -> None:
    """START opens the menu from IDLE, or closes it if already open.

    Ignored while the save-confirm, inventory, or settings overlay is open
    — those own B/A until dismissed, same as any other confirm dialog /
    sub-screen.
    """
    if save_menu.is_open or inventory_menu.is_open or settings_menu.is_open:
        return
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)
    elif player.state == PlayerState.IDLE:
        menu.open()
        player.set_state(PlayerState.IN_MENU)


def handle_b_button(player: Player, menu: StartMenu, save_menu: SaveMenu,
                     inventory_menu: InventoryMenu, settings_menu: SettingsMenu) -> None:
    """B closes whichever menu is on top: the save-confirm, inventory, or
    settings overlay first (only one of the three is ever open at a time —
    all three only ever open from the start menu), otherwise the start menu
    itself."""
    if save_menu.is_open:
        save_menu.close()
        return
    if inventory_menu.is_open:
        inventory_menu.close()
        return
    if settings_menu.is_open:
        settings_menu.close()
        return
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)


def _open_container(container, game_state: GameState, inventory: Inventory,
                     item_defs: dict, map_name: str) -> list[str] | None:
    """Return the dialogue pages to show for opening `container`, or None if
    it's already been opened in this save — containers are inert once their
    flag is set, same as not being interactable at all.

    A container with `contents` set grants one of that item (added straight
    to `inventory`, not deferred until the dialogue closes) and sets its
    flag the instant it's opened, with a synthesized "Received {name}." page
    appended after its hand-authored `dialogue`. A container with no
    `contents` (pure flavor, no loot) never sets a flag and behaves exactly
    like a sign — its dialogue replays every time.
    """
    key = persistent_id(map_name, container.name)
    if game_state.flag(key):
        return None

    pages = list(container.dialogue)
    if container.contents:
        inventory.add(container.contents)
        game_state.set_flag(key)
        item_def = item_defs.get(container.contents)
        item_name = item_def.name if item_def is not None else container.contents
        pages = pages + [f"Received {item_name}."]
    return pages


def handle_a_button(player: Player, menu: StartMenu, save_menu: SaveMenu,
                     inventory_menu: InventoryMenu, settings_menu: SettingsMenu,
                     dialogue: DialogueBox,
                     npcs: list, objects: list, wrap_pages,
                     game_state: GameState, inventory: Inventory,
                     item_defs: dict, map_name: str, active_slot: int) -> str | None:
    """A confirms whichever menu is on top, advances/closes an open dialogue
    box, or — failing all of that — opens one by interacting with whatever
    the player is facing. Returns the confirmed menu option's name, or None.

    On the save-confirm overlay: NO closes it back to the start menu; YES
    writes `player`/`inventory`/`game_state` to `active_slot` (see
    engine.save) and also closes back to the start menu. On the start menu:
    SAVE opens the save-confirm overlay, INVENTORY opens the inventory
    screen, SETTINGS opens the settings screen; PARTY is still stubbed — no
    sub-screen exists yet. The inventory and settings screens don't consume
    A at all — inventory is view-only for this alpha pass, and settings
    applies its changes immediately off E/W (see engine.menu.SettingsMenu),
    so A is a no-op on both while they're open.

    Dialogue takes over once open: each press turns the page, and closes the
    box (returning the player to IDLE) on the last one. Signs and NPCs both
    open a dialogue box this way (facing one and pressing A); containers do
    too, via `_open_container` above, which also handles the item grant and
    flag — `dialogue`/`DialogueBox` itself doesn't know containers exist,
    it's just shown whatever pages `_open_container` returns.

    `wrap_pages(raw_pages: list[str]) -> list[str]` turns each hand-authored
    YAML page into one or more screens sized to fit the dialogue box, since a
    page's text won't always fit in one screen. It's pygame.font-aware
    (measures pixel width/line height), so it's supplied by engine.renderer
    rather than done in this module, which stays pygame-free.
    """
    if save_menu.is_open:
        option = save_menu.confirm()
        if option == "NO":
            save_menu.close()
        elif option == "YES":
            save_to_slot(active_slot, player, inventory, game_state, map_name)
            save_menu.close()
        return option

    if inventory_menu.is_open:
        return None   # view-only for now — no USE/EQUIP action yet

    if settings_menu.is_open:
        return None   # E/W applies changes immediately — no confirm step

    if menu.is_open:
        option = menu.confirm()
        if option == "SAVE":
            save_menu.open()
        elif option == "INVENTORY":
            inventory_menu.open()
        elif option == "SETTINGS":
            settings_menu.open()
        return option

    if dialogue.is_open:
        if dialogue.advance():
            player.set_state(PlayerState.IDLE)
        return None

    if player.can_interact():
        target = player.adjacent_interactable(npcs, objects)
        if target is not None and target.type in ("sign", "npc"):
            dialogue.open(wrap_pages(target.dialogue))
            player.set_state(PlayerState.IN_DIALOGUE)
        elif target is not None and target.type == "container":
            pages = _open_container(target, game_state, inventory, item_defs, map_name)
            if pages is not None:
                dialogue.open(wrap_pages(pages))
                player.set_state(PlayerState.IN_DIALOGUE)

    return None


def handle_menu_direction(direction: str, menu: StartMenu, save_menu: SaveMenu,
                           inventory_menu: InventoryMenu, settings_menu: SettingsMenu,
                           category_item_count: int) -> None:
    """Route a direction press to whichever menu is on top.

    `category_item_count` is how many items are owned in whichever category
    `inventory_menu` currently has selected — needed to clamp N/S scrolling.
    This module has no item data of its own, so the caller (engine.renderer,
    which owns the scene's Inventory + item defs) computes it beforehand.
    Only read when inventory_menu is actually the one on top; harmless to
    pass 0 otherwise.
    """
    if save_menu.is_open:
        save_menu.move_cursor(direction)
    elif inventory_menu.is_open:
        inventory_menu.move_cursor(direction, category_item_count)
    elif settings_menu.is_open:
        settings_menu.move_cursor(direction)
    elif menu.is_open:
        menu.move_cursor(direction)
