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
(sign, healer, NPC, or container) opens one instead — see handle_a_button,
and _open_container for how a container's dialogue also grants its
`contents` item and sets a flag (engine.game_state) the first time it's
opened. An NPC's dialogue can branch on game_state flags the same way —
see handle_a_button and engine.npc.resolve_dialogue/npc_met_flag. A
"healer" (e.g. a saladbar) is meant to fully heal the party for free, every
visit, no flag — the heal itself isn't implemented yet (see
handle_a_button's TODO(party-hp)), though party HP is live state now.
SAVE's YES/NO overlay writes to disk via engine.save on YES — see
handle_a_button.
"""

from engine.dialogue import DialogueBox
from engine.game_state import GameState, persistent_id
from engine.inventory import Inventory
from engine.menu import BUY, InventoryMenu, SaveMenu, SettingsMenu, ShopMenu, StartMenu
from engine.movement import OPPOSITE_DIR
from engine.npc import npc_met_flag, resolve_dialogue
from engine.player import Player, PlayerState
from engine.roster import Roster
from engine.save import save_to_slot


class HeldDirectionInput:
    """Tracks currently-held cardinal directions and resolves which ones (if
    any) are actively driving continuous movement this frame.

    Movement is 8-directional now (Earthbound-style free movement, not
    tile-stepped): up to one direction per axis can be active at once — the
    most recently pressed of N/S (vertical) and the most recently pressed of
    E/W (horizontal), independently ("last pressed wins" per axis, same idea
    as before but no longer forcing a single overall direction). The caller
    (engine.renderer) turns the (vertical, horizontal) pair `tick()` returns
    into an actual velocity via engine.player.Player.move.

    Turn-in-place-without-moving is preserved: a freshly pressed direction
    that differs from the player's current facing, pressed while genuinely
    at rest (caller passes `facing`), is held in `_pending` — excluded from
    the active directions `tick()` returns — until a short shared cooldown
    (`_TURN_COOLDOWN_MS`) expires, so a brief tap turns the player to face a
    new way without walking, and only holding past the cooldown actually
    starts moving. A second direction pressed while the first is still
    pending (e.g. a fast diagonal tap from a dead stop) joins the same
    `_pending` set and releases at the same time. A press that matches the
    current facing already, or one made while the player is already moving
    (caller passes `facing=None`), skips the freeze entirely and is active
    immediately — changing direction (including adding a second axis to go
    diagonal) while already walking has no added delay.
    """

    _TURN_COOLDOWN_MS = 75   # fixed delay before a turn-in-place becomes a step

    _VERTICAL = ("N", "S")
    _HORIZONTAL = ("E", "W")

    def __init__(self):
        self._held: list[str] = []
        self._pending: set[str] = set()   # frozen: turned-to but not yet moving
        self._cooldown_ms: float = 0.0

    def press(self, direction: str, facing: str | None = None) -> bool:
        """Register a held direction press. Returns True if this press
        should just turn the player in place rather than start moving —
        see the class docstring for exactly when that applies."""
        is_new = direction not in self._held
        if not is_new:
            return False
        self._held.append(direction)
        if facing is not None and direction != facing:
            if self._cooldown_ms <= 0.0:
                self._cooldown_ms = self._TURN_COOLDOWN_MS
            self._pending.add(direction)
            return True
        return False

    def release(self, direction: str) -> None:
        if direction in self._held:
            self._held.remove(direction)
        self._pending.discard(direction)

    def tick(self, dt_ms: float) -> tuple[str | None, str | None]:
        """Advance by dt_ms; return (vertical, horizontal) — the active
        direction on each axis this frame, or None for an axis with nothing
        held (or still pending/frozen)."""
        if self._pending:
            self._cooldown_ms -= dt_ms
            if self._cooldown_ms <= 0.0:
                self._pending.clear()
                self._cooldown_ms = 0.0

        active = [d for d in self._held if d not in self._pending]
        vertical = next((d for d in reversed(active) if d in self._VERTICAL), None)
        horizontal = next((d for d in reversed(active) if d in self._HORIZONTAL), None)
        return vertical, horizontal


# ── Start menu routing ───────────────────────────────────────────────────────
#
# Discrete (non-held) button presses. START/B/A only mean something in
# specific player states, so the gating lives here rather than in the menu
# itself or in the renderer. Whichever of the save-confirm / inventory
# overlays is open sits on top of the start menu, so it always gets first
# refusal at B/A/direction input.

def handle_start_button(player: Player, menu: StartMenu, save_menu: SaveMenu,
                         inventory_menu: InventoryMenu, settings_menu: SettingsMenu,
                         shop_menu: ShopMenu) -> None:
    """START opens the menu from IDLE, or closes it if already open.

    Ignored while the save-confirm, inventory, settings, or shop overlay is
    open — those own B/A until dismissed, same as any other confirm dialog /
    sub-screen.
    """
    if save_menu.is_open or inventory_menu.is_open or settings_menu.is_open or shop_menu.is_open:
        return
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)
    elif player.state == PlayerState.IDLE:
        menu.open()
        player.set_state(PlayerState.IN_MENU)


def handle_b_button(player: Player, menu: StartMenu, save_menu: SaveMenu,
                     inventory_menu: InventoryMenu, settings_menu: SettingsMenu,
                     shop_menu: ShopMenu, dialogue: DialogueBox, npcs: list,
                     game_state: GameState, wrap_pages) -> None:
    """B closes whichever menu is on top: the save-confirm, inventory, or
    settings overlay first (only one of the three is ever open at a time —
    all three only ever open from the start menu), otherwise the start menu
    itself.

    The shop overlay (opened directly from the overworld, not from the
    start menu) is the one genuinely two-level close here: B first backs
    out of an in-progress quantity pick without transacting; a second press
    closes the shop screen itself and, if the shopkeeper (resolved from
    `npcs` by `shop_menu.shop_name`) has a `farewell_variants` line that
    resolves against current `game_state` flags, shows it in `dialogue`
    (player moves to IN_DIALOGUE, same close-to-IDLE as any other dialogue)
    instead of returning straight to IDLE."""
    if save_menu.is_open:
        save_menu.close()
        return
    if inventory_menu.is_open:
        inventory_menu.close()
        return
    if settings_menu.is_open:
        settings_menu.close()
        return
    if shop_menu.is_open:
        if shop_menu.picking_amount:
            shop_menu.cancel_amount()
            return
        shop = next((n for n in npcs if n.name == shop_menu.shop_name), None)
        shop_menu.close()
        farewell = resolve_dialogue(shop.farewell_variants, game_state.flag) if shop else []
        if farewell:
            dialogue.open(wrap_pages(farewell))
            player.set_state(PlayerState.IN_DIALOGUE)
        else:
            player.set_state(PlayerState.IDLE)
        return
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)


def _open_container(container, game_state: GameState, inventory: Inventory,
                     item_defs: dict, map_name: str) -> list[str] | None:
    """Return the dialogue pages to show for opening `container`, or None if
    it's already been opened in this save — containers are inert once their
    flag is set, same as not being interactable at all.

    Every container sets its flag the instant it's opened, loot or not —
    single-use, one open, whether it's a real chest or pure flavor text. A
    container with `contents` set additionally grants one of that item
    (added straight to `inventory`, not deferred until the dialogue closes),
    with a synthesized "Received {name}." page appended after its
    hand-authored `dialogue`; a container with `gold` set likewise credits
    that amount via `game_state.add_gold`, with a synthesized "Found $N."
    page. The two are independent — a container can have either, both, or
    neither. That same flag also drives the container's visual "opened"
    state — see OverworldScene._draw_entities in engine/renderer.py, which
    stops drawing the container's `gid` once it's set.
    """
    key = persistent_id(map_name, container.name)
    if game_state.flag(key):
        return None

    pages = list(container.dialogue)
    game_state.set_flag(key)
    if container.contents:
        inventory.add(container.contents)
        item_def = item_defs.get(container.contents)
        item_name = item_def.name if item_def is not None else container.contents
        pages = pages + [f"Received {item_name}."]
    if container.gold:
        game_state.add_gold(container.gold)
        pages = pages + [f"Found ${container.gold}."]
    return pages


def _confirm_shop_transaction(shop_menu: ShopMenu, shop, game_state: GameState,
                               inventory: Inventory, item_defs: dict) -> None:
    """Resolve the amount-picked transaction on `shop_menu`'s current
    mode/cursor row against `shop` (the shopkeeper NPC engine.renderer
    resolved from shop_menu.shop_name — see handle_a_button below). Mutates
    game_state/inventory in place; always leaves shop_menu back in
    list-navigation state (picking_amount cleared) and sets
    shop_menu.message to a one-line result, success or failure.

    BUY spends `price * qty` gold (failing gracefully, insufficient funds
    included, via game_state.spend_gold's bool return) and adds the item to
    inventory. SELL removes `qty` from inventory (clamped to what's
    actually owned, in case shop_menu.amount went stale between frames) and
    credits `value * qty` gold via Inventory.sellable_items/ItemDef.value —
    the same items.yaml `value` field the shop's own contents never touch.
    """
    qty = shop_menu.amount
    if shop_menu.mode == BUY:
        entry = shop.stock[shop_menu.cursor]
        item_id, price = entry["item"], entry["price"]
        item_def = item_defs.get(item_id)
        name = item_def.name if item_def is not None else item_id
        if game_state.spend_gold(price * qty):
            inventory.add(item_id, qty)
            shop_menu.message = f"BOUGHT {name} x{qty}."
        else:
            shop_menu.message = "NOT ENOUGH GOLD."
    else:
        sellable = inventory.sellable_items(item_defs)
        item_id = sellable[shop_menu.cursor]
        item_def = item_defs[item_id]
        qty = min(qty, inventory.counts.get(item_id, 0))
        inventory.remove(item_id, qty)
        game_state.add_gold(item_def.value * qty)
        shop_menu.message = f"SOLD {item_def.name} x{qty}."
    shop_menu.cancel_amount()


def handle_a_button(player: Player, menu: StartMenu, save_menu: SaveMenu,
                     inventory_menu: InventoryMenu, settings_menu: SettingsMenu,
                     shop_menu: ShopMenu,
                     dialogue: DialogueBox,
                     npcs: list, objects: list, wrap_pages,
                     game_state: GameState, inventory: Inventory, roster: Roster,
                     item_defs: dict, map_name: str, active_slot: int) -> str | None:
    """A confirms whichever menu is on top, advances/closes an open dialogue
    box, or — failing all of that — opens one by interacting with whatever
    the player is facing. Returns the confirmed menu option's name, or None.

    On the save-confirm overlay: NO closes it back to the start menu; YES
    writes `player`/`inventory`/`game_state`/`roster` to `active_slot` (see
    engine.save) and also closes back to the start menu. On the start menu:
    SAVE opens the save-confirm overlay, INVENTORY opens the inventory
    screen, SETTINGS opens the settings screen; PARTY is still stubbed — no
    sub-screen exists yet. The inventory and settings screens don't consume
    A at all — inventory is view-only for this alpha pass, and settings
    applies its changes immediately off E/W (see engine.menu.SettingsMenu),
    so A is a no-op on both while they're open.

    Dialogue takes over once open: each press turns the page, and closes the
    box (returning the player to IDLE) on the last one. Signs, healers, and
    NPCs all open a dialogue box this way (facing one and pressing A);
    containers do too, via `_open_container` above, which also handles the
    item grant and flag — `dialogue`/`DialogueBox` itself doesn't know
    containers exist, it's just shown whatever pages `_open_container`
    returns. A healer (e.g. a saladbar) shares the sign branch verbatim for
    now — see the TODO(party-hp) comment right there — since there's no
    party HP yet to actually heal.

    An NPC's pages come from resolving `target.dialogue_variants` (already
    merged from its placement's own npcs_<map>.yaml override or its
    NpcDef's dialogue — see OverworldScene._load_map) against `game_state`'s
    current flags — see engine.npc.resolve_dialogue. Right after resolving
    (so this interaction still sees the pre-visit flag state), an npc_id'd
    NPC has its `engine.npc.npc_met_flag` set — unconditionally, every
    visit, not just the first — so dialogue can branch on "have we ever
    met" via `unless: [npc_met_flag(...)]` on its first-meeting variant.
    Opening an NPC's dialogue also turns it to face the player —
    `engine.movement.OPPOSITE_DIR[player.facing]`, since the player is
    always facing the NPC to reach it (adjacent_interactable only returns
    the tile directly ahead) — visible only on an 8-frame (full-facing)
    sprite; a no-op on a 2-frame one, which ignores `facing` entirely (see
    get_npc_frame). This overrides whatever the NPC was already facing —
    its own authored `facing:`, or wherever a "wander" NPC's last step left
    it — every single time its dialogue opens, not just the first.

    A shop (`target.type == "shop"`) is a person first — built from the
    same npcs_<map>.yaml pipeline as any other NPC (see engine.renderer.NPC)
    — so pressing A on one opens its greeting exactly like the `npc` branch
    above, facing/dialogue/npc_met_flag included. The only difference:
    `shop_menu.pending_shop` is set to the shopkeeper's name at the same
    time, which the `dialogue.is_open` branch below checks the instant that
    greeting closes — if set, it opens `shop_menu` (the buy/sell screen)
    and moves to IN_SHOP instead of back to IDLE. Once the shop screen
    itself is open, pressing A there confirms whichever row/quantity is
    selected — see `_confirm_shop_transaction`. Backing all the way out
    (B, handled in handle_b_button) shows the shopkeeper's farewell line
    the same paired way, if one is authored.

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
            save_to_slot(active_slot, player, inventory, game_state, roster, map_name)
            save_menu.close()
        return option

    if inventory_menu.is_open:
        return None   # view-only for now — no USE/EQUIP action yet

    if settings_menu.is_open:
        return None   # E/W applies changes immediately — no confirm step

    if shop_menu.is_open:
        shop = next((n for n in npcs if n.name == shop_menu.shop_name), None)
        if shop is None:   # defensive -- shouldn't happen, closes cleanly if it does
            shop_menu.close()
            player.set_state(PlayerState.IDLE)
            return None
        if shop_menu.picking_amount:
            _confirm_shop_transaction(shop_menu, shop, game_state, inventory, item_defs)
        else:
            current_list = shop.stock if shop_menu.mode == BUY else inventory.sellable_items(item_defs)
            if current_list:
                shop_menu.start_amount_pick()
        return None

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
            if shop_menu.pending_shop:
                shop_menu.open(shop_menu.pending_shop)
                player.set_state(PlayerState.IN_SHOP)
            else:
                player.set_state(PlayerState.IDLE)
        return None

    if player.can_interact():
        target = player.adjacent_interactable(npcs, objects)
        if target is not None and target.type in ("sign", "healer"):
            # TODO(party-hp): a "healer" (e.g. a map's saladbar -- full heal,
            # Pokemon-Center style, no cost, no flag, works every visit) is
            # otherwise identical to a sign today. Party HP *is* live runtime
            # state now (engine.roster.Roster, threaded through battle) --
            # implementing the actual heal is just unimplemented, not
            # blocked. Once it's built, reset the roster's members to full
            # HP/MP right here, before opening dialogue, and split this back
            # into its own branch -- a sign must never gain a side effect
            # the same way.
            dialogue.open(wrap_pages(target.dialogue))
            player.set_state(PlayerState.IN_DIALOGUE)
        elif target is not None and target.type == "npc":
            target.facing = OPPOSITE_DIR[player.facing]
            pages = resolve_dialogue(target.dialogue_variants, game_state.flag)
            dialogue.open(wrap_pages(pages))
            player.set_state(PlayerState.IN_DIALOGUE)
            if target.npc_id:
                game_state.set_flag(npc_met_flag(target.npc_id))
        elif target is not None and target.type == "container":
            pages = _open_container(target, game_state, inventory, item_defs, map_name)
            if pages is not None:
                dialogue.open(wrap_pages(pages))
                player.set_state(PlayerState.IN_DIALOGUE)
        elif target is not None and target.type == "shop":
            # Talk first, same as any NPC -- the buy/sell screen only opens
            # once this greeting closes (see the dialogue.is_open branch
            # above, which checks shop_menu.pending_shop).
            target.facing = OPPOSITE_DIR[player.facing]
            pages = resolve_dialogue(target.dialogue_variants, game_state.flag)
            shop_menu.pending_shop = target.name
            dialogue.open(wrap_pages(pages))
            player.set_state(PlayerState.IN_DIALOGUE)
            if target.npc_id:
                game_state.set_flag(npc_met_flag(target.npc_id))

    return None


def handle_menu_direction(direction: str, menu: StartMenu, save_menu: SaveMenu,
                           inventory_menu: InventoryMenu, settings_menu: SettingsMenu,
                           shop_menu: ShopMenu, category_item_count: int,
                           shop_list_len: int = 0, shop_max_amount: int = 0) -> None:
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
    elif shop_menu.is_open:
        shop_menu.move_cursor(direction, shop_list_len, shop_max_amount)
    elif menu.is_open:
        menu.move_cursor(direction)
