"""Framework-agnostic player input resolution.

Renderers own raw key polling (pygame key codes → direction strings/button
names) and visual tweening; this module owns the actual decision logic —
which direction, if any, should be stepped this frame, and what a button
press (START/A/B) or a direction press means given the player's current
state. No pygame dependency, so it's independently testable.

The start menu (engine.menu.StartMenu) never reads input itself — it only
exposes state mutators (open/close/move_cursor/confirm). This module is the
one place that decides when to call them.
"""

from engine.menu import StartMenu
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

    def __init__(self, repeat_ms: int):
        self._repeat_ms = repeat_ms
        self._held: list[str] = []
        self._cooldown_ms: float = 0.0   # 0 == ready to fire on next tick

    def press(self, direction: str) -> None:
        if direction not in self._held:
            self._held.append(direction)

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
# itself or in the renderer.

def handle_start_button(player: Player, menu: StartMenu) -> None:
    """START opens the menu from IDLE, or closes it if already open."""
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)
    elif player.state == PlayerState.IDLE:
        menu.open()
        player.set_state(PlayerState.IN_MENU)


def handle_b_button(player: Player, menu: StartMenu) -> None:
    """B closes the menu if it's open."""
    if menu.is_open:
        menu.close()
        player.set_state(PlayerState.IDLE)


def handle_a_button(menu: StartMenu) -> str | None:
    """A confirms the highlighted option. Returns its name, or None if the
    menu isn't open. Sub-screens aren't implemented yet, so the caller has
    nothing to do with the result for now."""
    if menu.is_open:
        return menu.confirm()
    return None


def handle_menu_direction(direction: str, menu: StartMenu) -> None:
    """Route a direction press to the menu cursor while the menu is open."""
    if menu.is_open:
        menu.move_cursor(direction)
