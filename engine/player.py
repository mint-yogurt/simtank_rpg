"""Player character entity.

Separate from OverworldMember / HubMember (AI party models) and BattleMember.
The Player is the human-controlled interface to the game world.

In practice one Player exists at a time; it drives movement, facing, interaction
checks, and holds a reference to the character sheet (stats) it represents.
The state machine gates what input is legal at any moment.
"""

from dataclasses import dataclass, field
from enum import Enum, auto
from pathlib import Path


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


_DIR_DELTA: dict[str, tuple[int, int]] = {
    "N": (-1,  0),
    "S": ( 1,  0),
    "E": ( 0,  1),
    "W": ( 0, -1),
}


@dataclass
class Player:
    """The human-controlled character.

    Position is in tile coordinates (row, col) within the current map.
    `sheet_name` is the character whose stats back this player — allows
    swapping the lead character in future without rewriting the entity.
    """
    name:       str
    row:        int
    col:        int
    facing:     str         = "S"             # "N" | "S" | "E" | "W"
    state:      PlayerState = field(default=PlayerState.IDLE)
    sheet_name: str         = "MELVIN"        # which character sheet drives stats

    # ── movement ──────────────────────────────────────────────────────────────

    def try_move(self, direction: str, passable: list[list[bool]]) -> bool:
        """Attempt to step one tile in `direction`.

        `passable[row][col]` is True if that tile can be walked onto — see
        engine.map_loader.tiled_passable_grid for how this is built from a
        Tiled map's per-tile `walkable` property.

        Updates row/col and facing on success.  Always updates facing so the
        player turns to face a wall they walked into (standard RPG feel).
        Returns True if the step was taken, False if blocked.
        """
        self.facing = direction
        dr, dc = _DIR_DELTA[direction]
        new_row = self.row + dr
        new_col = self.col + dc

        rows = len(passable)
        cols = len(passable[0]) if rows else 0

        if not (0 <= new_row < rows and 0 <= new_col < cols):
            return False   # map edge

        if not passable[new_row][new_col]:
            return False   # wall / water / etc.

        self.row = new_row
        self.col = new_col
        return True

    # ── interaction ───────────────────────────────────────────────────────────

    def facing_tile(self) -> tuple[int, int]:
        """Return the (row, col) of the tile directly in front of the player."""
        dr, dc = _DIR_DELTA[self.facing]
        return self.row + dr, self.col + dc

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
