"""Framework-agnostic player input resolution.

Renderers own raw key polling (pygame key codes → direction strings/button
names) and visual tweening; this module owns the actual decision logic —
which direction, if any, should be stepped this frame, and what a button
press (START/A/B) or a direction press means given the player's current
state. No pygame dependency, so it's independently testable.

The start menu (engine.menu.StartMenu) and the save-confirm overlay it can
open (engine.menu.SaveMenu) never read input themselves — they only expose
state mutators (open/close/move_cursor/confirm). This module is the one
place that decides when to call them, including which of the two is "on
top" (the save-confirm overlay, when open) and should receive input first.

engine.dialogue.DialogueBox follows the same rule: it never reads input
itself, only open()/close()/advance(). A pressed while a dialogue box is
open advances/closes it; A pressed while idle and facing an interactable
opens one instead — see handle_a_button.
"""

from engine.dialogue import DialogueBox
from engine.menu import SaveMenu, StartMenu
from engine.player import Player, PlayerState


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
# itself or in the renderer. The save-confirm overlay sits on top of the
# start menu, so it always gets first refusal at B/A/direction input.

def handle_start_button(player: Player, menu: StartMenu, save_menu: SaveMenu) -> None:
    """START opens the menu from IDLE, or closes it if already open.

    Ignored while the save-confirm overlay is open — that overlay owns B/A
    until it's dismissed, same as any other confirm dialog.
    """
    if save_menu.is_open:
        return
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)
    elif player.state == PlayerState.IDLE:
        menu.open()
        player.set_state(PlayerState.IN_MENU)


def handle_b_button(player: Player, menu: StartMenu, save_menu: SaveMenu) -> None:
    """B closes whichever menu is on top: the save-confirm overlay first
    (same effect as selecting NO), otherwise the start menu."""
    if save_menu.is_open:
        save_menu.close()
        return
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)


def handle_a_button(player: Player, menu: StartMenu, save_menu: SaveMenu,
                     dialogue: DialogueBox, npcs: list, objects: list,
                     wrap_pages) -> str | None:
    """A confirms whichever menu is on top, advances/closes an open dialogue
    box, or — failing all of that — opens one by interacting with whatever
    the player is facing. Returns the confirmed menu option's name, or None.

    On the save-confirm overlay: NO closes it back to the start menu; YES is
    stubbed (see engine.menu.SaveMenu.confirm) and currently does nothing.
    On the start menu: SAVE opens the save-confirm overlay; the other
    options are stubbed — no sub-screens exist yet.

    Dialogue takes over once open: each press turns the page, and closes the
    box (returning the player to IDLE) on the last one. Signs and NPCs both
    open a dialogue box this way (facing one and pressing A); containers are
    out of scope until the object/event system exists.

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
        return option

    if menu.is_open:
        option = menu.confirm()
        if option == "SAVE":
            save_menu.open()
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

    return None


def handle_menu_direction(direction: str, menu: StartMenu, save_menu: SaveMenu) -> None:
    """Route a direction press to whichever menu is on top."""
    if save_menu.is_open:
        save_menu.move_cursor(direction)
    elif menu.is_open:
        menu.move_cursor(direction)
