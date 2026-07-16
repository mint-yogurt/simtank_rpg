"""Player character entity.

Separate from OverworldMember / HubMember (AI party models) and BattleMember.
The Player is the human-controlled interface to the game world.

In practice one Player exists at a time; it drives movement, facing, interaction
checks, and holds a reference to the character sheet (stats) it represents.
The state machine gates what input is legal at any moment.
"""

import math
from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path

from engine.movement import _DIR_DELTA, step_continuous


class PlayerState(Enum):
    """What the player is doing right now.

    Input handling and update logic branch on this — e.g. arrow keys only
    trigger movement when IDLE, not when IN_DIALOGUE or IN_MENU.
    """
    IDLE        = auto()   # standing still, ready for input
    WALKING     = auto()   # mid-step animation playing
    INTERACTING = auto()   # brief pause after pressing confirm on an object
    IN_DIALOGUE = auto()   # dialogue box open
    IN_MENU     = auto()   # pause/inventory/equipment menu open
    IN_BATTLE   = auto()   # battle scene active


@dataclass
class Player:
    """The human-controlled character.

    Position is continuous (row, col) float tile coordinates within the
    current map — free/Earthbound-style movement, not tile-snapped. The
    value itself is the logical position (mirrors engine.enemy.Enemy), not
    a renderer-side visual tween. Grid-keyed lookups (interactions, warps,
    NPC occupancy) go through `current_tile()` instead of using row/col
    directly. `sheet_name` is the character whose stats back this player —
    allows swapping the lead character in future without rewriting the
    entity.
    """
    name:       str
    row:        float
    col:        float
    facing:     str         = "S"             # "N" | "S" | "E" | "W"
    state:      PlayerState = field(default=PlayerState.IDLE)
    sheet_name: str         = "MELVIN"        # which character sheet drives stats

    # Contact-triggered grid nudge, for squeezing through 1-tile-wide gaps
    # without exact pixel alignment -- see move(). Deliberately NOT a
    # constant/ambient pull (that was tried and looked bad: visible drift
    # even standing still or crossing open floor). _ALIGN_SPEED bounds how
    # fast the nudge can move you per frame once it's triggered, so it reads
    # as a gentle slide into place rather than a snap.
    _ALIGN_SPEED = 6.0   # tiles/sec
    _ALIGN_EPS = 1e-4    # close enough to the grid line to just latch onto it

    # ── movement ──────────────────────────────────────────────────────────────

    def move(self, dt_ms: float, vertical: str | None, horizontal: str | None,
             speed: float, passable: list[list[bool]]) -> None:
        """Advance continuously for one frame given up to one active
        direction per axis (vertical: "N"/"S", horizontal: "E"/"W" — either
        may be None if that axis isn't currently held).

        Always updates facing first — vertical-dominant when both axes are
        active, since there are no diagonal sprites, so NE/NW/SE/SW all
        show the N/S sprite — and updates it even if the move ends up fully
        blocked, so the player still turns to face a wall they walked into
        (standard RPG feel, same as before).

        Diagonal distance is normalized by 1/sqrt(2) when both axes are
        active so moving diagonally isn't faster than moving along one axis
        alone. Each held axis is resolved via engine.movement.step_continuous
        against `passable`, using the same full-tile bounding box Enemy does
        (see engine.movement) — matches the drawn sprite exactly, so there's
        never a mismatch between what collides and what's on screen.

        Contact-triggered grid alignment: a full-tile box has zero slack to
        be off-grid on the cross-axis of a 1-tile-wide gap (doorway,
        corridor) — it can only fit through at one exact coordinate, which
        free (non-grid-snapped) movement essentially never lands on by
        chance. If exactly one axis is held and that axis's step this frame
        comes up short of the full requested distance (`step_continuous`
        hard-stopped early — contact with something), AND aligning the
        *other*, untouched axis to the nearest whole tile would actually let
        further movement proceed (`_gap_ahead` — there's a real opening
        there, not just a flat wall), that axis is nudged toward it at a
        bounded rate.

        The `_gap_ahead` check matters: contact alone happens constantly —
        every ordinary walk into any wall, anywhere, in a room with plenty
        of open floor either side, "comes up short of the full distance."
        Without also confirming a real opening exists at the aligned
        position, the nudge would fire on every wall bump in the game, not
        just at doorways — which is exactly the "sliding/snapping
        everywhere" problem with the earlier ambient version, just gated a
        different way. With the check, walking into a plain wall away from
        any gap causes zero movement on the untouched axis, same as before
        this existed; only a genuine nearby 1-wide opening ever triggers it.
        It re-evaluates fresh every frame, so it works the same regardless
        of which direction you approached from. A diagonal (both axes held)
        gets no assist on either axis, same as not being able to enter a
        1-wide gap diagonally in any tile-based game.
        """
        if vertical is None and horizontal is None:
            return

        self.facing = vertical if vertical is not None else horizontal

        dist = speed * (dt_ms / 1000.0)
        if vertical is not None and horizontal is not None:
            dist /= math.sqrt(2)

        row, col = self.row, self.col
        vertical_blocked = horizontal_blocked = False

        if vertical is not None:
            new_row, new_col = step_continuous(row, col, vertical, dist, passable)
            vertical_blocked = abs(new_row - row) < dist - self._ALIGN_EPS
            row, col = new_row, new_col
        if horizontal is not None:
            new_row, new_col = step_continuous(row, col, horizontal, dist, passable)
            horizontal_blocked = abs(new_col - col) < dist - self._ALIGN_EPS
            row, col = new_row, new_col

        align_dist = self._ALIGN_SPEED * (dt_ms / 1000.0)
        if (vertical is not None and horizontal is None and vertical_blocked
                and self._gap_ahead(row, col, axis="col", direction=vertical, passable=passable)):
            row, col = self._nudge_axis(row, col, axis="col", dist=align_dist, passable=passable)
        if (horizontal is not None and vertical is None and horizontal_blocked
                and self._gap_ahead(row, col, axis="row", direction=horizontal, passable=passable)):
            row, col = self._nudge_axis(row, col, axis="row", dist=align_dist, passable=passable)

        self.row, self.col = row, col

    _GAP_PROBE_DIST = 0.05   # small step used only to test "is there an opening here"

    @classmethod
    def _gap_ahead(cls, row: float, col: float, axis: str, direction: str,
                    passable: list[list[bool]]) -> bool:
        """True if, were `axis` ("row" or "col" — the one NOT in `direction`
        of travel) aligned to its nearest whole tile, continuing to move in
        `direction` from there would actually make progress. Distinguishes a
        genuine 1-wide opening from an ordinary flat wall (see move()'s
        Contact-triggered grid alignment docstring for why this check is
        required, not optional)."""
        target = round(row if axis == "row" else col)
        probe_row = target if axis == "row" else row
        probe_col = col if axis == "row" else target
        new_row, new_col = step_continuous(probe_row, probe_col, direction,
                                            cls._GAP_PROBE_DIST, passable)
        return abs(new_row - probe_row) > cls._ALIGN_EPS or abs(new_col - probe_col) > cls._ALIGN_EPS

    @classmethod
    def _nudge_axis(cls, row: float, col: float, axis: str, dist: float,
                     passable: list[list[bool]]) -> tuple[float, float]:
        """Nudge `row` or `col` (per `axis`) toward the nearest whole tile,
        by at most `dist`, collision-checked via step_continuous — so a
        nearby wall with no actual opening just limits it to zero movement,
        not a nudge into the wall."""
        pos = row if axis == "row" else col
        target = round(pos)
        diff = target - pos
        if abs(diff) < cls._ALIGN_EPS:
            return (target, col) if axis == "row" else (row, target)
        direction = (("S" if diff > 0 else "N") if axis == "row" else ("E" if diff > 0 else "W"))
        return step_continuous(row, col, direction, min(abs(diff), dist), passable)

    # ── interaction ───────────────────────────────────────────────────────────

    def current_tile(self) -> tuple[int, int]:
        """The single (row, col) tile the player counts as standing on, for
        grid-keyed lookups (interactions, warps, NPC occupancy) — nearest
        tile to the continuous position, not floor, so a player mostly
        through a tile already counts as being on it."""
        return math.floor(self.row + 0.5), math.floor(self.col + 0.5)

    def facing_tile(self) -> tuple[int, int]:
        """Return the (row, col) of the tile directly in front of the player."""
        dr, dc = _DIR_DELTA[self.facing]
        row, col = self.current_tile()
        return row + dr, col + dc

    def adjacent_interactable(self, npcs: list, objects: list) -> object | None:
        """Return the first NPC or object on the tile in front of the player.

        Caller checks the return type to decide what kind of interaction to start
        (dialogue, chest open, sign read, etc.).
        Returns None if nothing interactable is there.
        """
        tr, tc = self.facing_tile()
        for npc in npcs:
            if npc.row == tr and npc.col == tc:
                return npc
        for obj in objects:
            if obj.row == tr and obj.col == tc:
                return obj
        return None

    # ── state helpers ─────────────────────────────────────────────────────────

    def set_state(self, state: PlayerState) -> None:
        self.state = state

    def is_idle(self) -> bool:
        return self.state == PlayerState.IDLE

    def can_move(self) -> bool:
        return self.state in (PlayerState.IDLE,)

    def can_interact(self) -> bool:
        return self.state in (PlayerState.IDLE,)

    # ── serialise ─────────────────────────────────────────────────────────────

    def to_dict(self) -> dict:
        return {
            "name":       self.name,
            "row":        self.row,
            "col":        self.col,
            "facing":     self.facing,
            "state":      self.state.name,
            "sheet_name": self.sheet_name,
        }

    @classmethod
    def from_dict(cls, d: dict) -> "Player":
        p = cls(
            name       = d["name"],
            row        = d["row"],
            col        = d["col"],
            facing     = d.get("facing", "S"),
            sheet_name = d.get("sheet_name", "MELVIN"),
        )
        p.state = PlayerState[d.get("state", "IDLE")]
        return p

    @classmethod
    def default(cls) -> "Player":
        """Convenience: create the default player (Melvin, facing south)."""
        return cls(name="MELVIN", row=0, col=0, facing="S")
