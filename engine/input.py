"""Framework-agnostic player input resolution.

Renderers own raw key polling (pygame key codes → direction strings) and
visual tweening; this module owns the actual decision logic — which
direction, if any, should be stepped this frame. No pygame dependency, so
it's independently testable.
"""


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
