"""Global game state — flags + variables — pure logic, no pygame.

Tracks story/world state that outlives any one scene: whether a container's
been opened, whether an NPC's been talked to, currency, an entered player
name, etc. Two flat namespaces: `flags` (bool) and `variables` (anything
JSON-serialisable). Same headless split as engine.player/engine.inventory —
a scene holds one instance and reads/writes through it.

persistent_id() is the shared vocabulary between this module and
engine.renderer/engine.input: every object placed on a map's Tiled object
layer (container, sign, npc, warp — anything data/maps/populate_yamls.py
stubs) gets a stable flag key derived from its map + name, with no separate
registry to keep in sync. Adding a chest in Tiled and giving it dialogue is
enough to make its flag usable via this module — nothing needs to be
pre-declared here, and nothing breaks if a flag is checked or set for an id
that's never been touched before (flags default to False). This does mean
an object's `name` must stay unique within its own map, same assumption
warps already make via `destination_warp` (see README).
"""

from dataclasses import dataclass, field
from typing import Any


def persistent_id(map_name: str, object_name: str) -> str:
    return f"{map_name}:{object_name}"


@dataclass
class GameState:
    flags:     dict[str, bool] = field(default_factory=dict)
    variables: dict[str, Any]  = field(default_factory=dict)

    def flag(self, key: str) -> bool:
        return self.flags.get(key, False)

    def set_flag(self, key: str, value: bool = True) -> None:
        self.flags[key] = value

    def get_var(self, key: str, default: Any = None) -> Any:
        return self.variables.get(key, default)

    def set_var(self, key: str, value: Any) -> None:
        self.variables[key] = value

    @property
    def gold(self) -> int:
        return self.variables.get("gold", 0)

    def add_gold(self, n: int) -> None:
        self.variables["gold"] = self.gold + n

    def spend_gold(self, n: int) -> bool:
        if n > self.gold:
            return False
        self.variables["gold"] = self.gold - n
        return True

    @property
    def party_members(self) -> list[str]:
        """Roster names actually in the active party right now -- distinct
        from the full roster (engine.roster.Roster loads every
        data/party/*.json unconditionally, since a character's sheet
        exists whether or not they've joined yet). Defaults to just
        MELVIN -- the only one confirmed recruited/playable so far (see
        engine.battle's module docstring) -- until a real recruit
        mechanic calls set_party_members with more."""
        return self.variables.get("party_members", ["MELVIN"])

    def set_party_members(self, names: list[str]) -> None:
        self.variables["party_members"] = list(names)

    def to_dict(self) -> dict:
        return {"flags": dict(self.flags), "variables": dict(self.variables)}

    @classmethod
    def from_dict(cls, d: dict) -> "GameState":
        return cls(flags=dict(d.get("flags", {})), variables=dict(d.get("variables", {})))
