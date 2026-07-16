"""Save/load — bundles Player + Inventory + GameState + current map name
into one JSON file per numbered slot. Pure logic, no pygame — the file
format is the contract between maptest.py's debug save-slot picker and
engine.renderer.OverworldScene's constructor, which accepts the loaded
pieces directly rather than reading this module itself.

v1 scope: player position/facing/state, the shared inventory, and game
flags/variables. Party member stats (HP/MP/XP/level, data/party/*.json)
aren't included yet — nothing at runtime mutates them until battle is wired
back up (engine/battle.py is currently broken, see CLAUDE.md), so there's
no live state to capture there yet.

SLOT_COUNT is a fixed, small set of numbered slots (matching a typical
retro RPG's save-select screen) rather than freeform save names — good
enough for testing a handful of chapters/scenarios at once via maptest.py.
"""

import json
from pathlib import Path

from engine.game_state import GameState
from engine.inventory import Inventory
from engine.player import Player

_SAVES_DIR = Path(__file__).parent.parent / "saves"
SLOT_COUNT = 3


def _slot_path(slot: int) -> Path:
    return _SAVES_DIR / f"slot{slot}.json"


def slot_exists(slot: int) -> bool:
    return _slot_path(slot).exists()


def save_to_slot(slot: int, player: Player, inventory: Inventory,
                  game_state: GameState, map_name: str) -> None:
    _SAVES_DIR.mkdir(exist_ok=True)
    data = {
        "map_name":   map_name,
        "player":     player.to_dict(),
        "inventory":  inventory.to_dict(),
        "game_state": game_state.to_dict(),
    }
    _slot_path(slot).write_text(json.dumps(data, indent=2))


def load_from_slot(slot: int) -> tuple[str, Player, Inventory, GameState]:
    """(map_name, player, inventory, game_state) restored from `slot`."""
    data = json.loads(_slot_path(slot).read_text())
    return (
        data["map_name"],
        Player.from_dict(data["player"]),
        Inventory.from_dict(data["inventory"]),
        GameState.from_dict(data["game_state"]),
    )


def clear_slot(slot: int) -> None:
    path = _slot_path(slot)
    if path.exists():
        path.unlink()
