"""Server-side state for the player-controlled hub scene.

Handles one Melvin, passability checking, and NPC ticking.
No LLM, no voting — pure input → state → event.
"""

import random

from engine.config import cfg
from engine.enemy_state import update_town_npcs
from engine.scenes.hub import (
    load_hub_grid, _hub_str_grid, _spawn_positions, _place_hub_npcs,
)
from engine.tiles import is_passable

_KEY_TO_DIR = {
    "ArrowUp":    "N",
    "ArrowDown":  "S",
    "ArrowLeft":  "W",
    "ArrowRight": "E",
}
_DIR_DELTA = {"N": (-1, 0), "S": (1, 0), "E": (0, 1), "W": (0, -1)}


class HubPlayerState:
    def __init__(self):
        self.grid = load_hub_grid()
        self.str_grid = _hub_str_grid(self.grid)
        self.rows = len(self.grid)
        self.cols = len(self.grid[0]) if self.grid else 0

        spawns = _spawn_positions(self.grid)
        self.row, self.col = spawns[0]   # Melvin's tile
        self.facing = "S"
        self.tick = 0

        self.npcs, _ = _place_hub_npcs(self.grid)
        self._npc_rng = random.Random(0x4875624E5043)

    def init_events(self, tileset_url: str, tile_grid: list) -> list[dict]:
        return [
            {
                "type": "hub_init",
                "rows": self.rows,
                "cols": self.cols,
                "tileset_url": tileset_url,
                "tile_grid": tile_grid,
                "party": [{"name": "MELVIN", "row": self.row, "col": self.col}],
                "config": {
                    "player_move_ms": cfg.player_move_ms,
                },
            },
            self._npc_event(),
        ]

    def handle_key(self, key: str) -> list[dict]:
        direction = _KEY_TO_DIR.get(key)
        if not direction:
            return []

        dr, dc = _DIR_DELTA[direction]
        new_row = self.row + dr
        new_col = self.col + dc

        self.facing = direction   # face the direction even if blocked

        if not (0 <= new_row < self.rows and 0 <= new_col < self.cols):
            return []
        if not is_passable(self.str_grid[new_row][new_col]):
            return []

        self.row = new_row
        self.col = new_col
        self.tick += 1
        self.npcs = update_town_npcs(self.npcs, self.str_grid, self._npc_rng)

        return [
            {
                "type": "hub_move",
                "name": "MELVIN",
                "row": self.row,
                "col": self.col,
                "direction": direction,
                "tick": self.tick,
            },
            self._npc_event(),
        ]

    def _npc_event(self) -> dict:
        return {
            "type": "int_enemies",
            "enemies": [
                {
                    "index": n.index + 1000,
                    "row": n.row,
                    "col": n.col,
                    "npc_sprite": n.npc_sprite,
                    "name": "NPC",
                    "anim_ms": 500,
                }
                for n in self.npcs
            ],
        }
