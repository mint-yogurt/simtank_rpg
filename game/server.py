"""WebSocket game server — transport only.

Receives key events from the browser, calls the engine, sends events back.
No game logic lives here; everything goes through engine/.

Client → server:  {"type": "key", "key": "ArrowUp"|"ArrowDown"|"ArrowLeft"|"ArrowRight"|...}
Server → client:  hub_init, hub_move, int_enemies  (same schema the SSE layer uses)
"""

import json
import random
from pathlib import Path

from flask import Flask, send_from_directory
from flask_sock import Sock

from engine.config import cfg
from engine.enemy_state import update_town_npcs
from engine.player import Player
from engine.scenes.hub import (
    _hub_str_grid, _place_hub_npcs, _spawn_positions, load_hub_grid,
)

_KEY_TO_DIR = {
    "ArrowUp":    "N",
    "ArrowDown":  "S",
    "ArrowLeft":  "W",
    "ArrowRight": "E",
}

_REPO_ROOT   = Path(__file__).parent.parent
_STATIC_GAME = Path(__file__).parent / "static"
_STATIC_WEB  = _REPO_ROOT / "web" / "static"
_ASSETS_DIR  = _REPO_ROOT / "assets"
_SCREENS_DIR = _STATIC_WEB / "screens"

app  = Flask(__name__, static_folder=None)
sock = Sock(app)


@app.route("/")
def index():
    return send_from_directory(str(_STATIC_GAME), "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    for base in (_STATIC_GAME, _STATIC_WEB):
        if (base / filename).exists():
            return send_from_directory(str(base), filename)
    return send_from_directory(str(_STATIC_WEB), filename)


@app.route("/assets/<path:filename>")
def asset_files(filename):
    return send_from_directory(str(_ASSETS_DIR), filename)


@app.route("/screens/<path:filename>")
def screen_files(filename):
    _SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    return send_from_directory(str(_SCREENS_DIR), filename)


@sock.route("/ws")
def game_ws(ws):
    from web.server import get_hub_tile_payload   # reuses existing tileset renderer

    # ── Load map via engine ───────────────────────────────────────────────────
    grid     = load_hub_grid()
    str_grid = _hub_str_grid(grid)
    rows     = len(grid)
    cols     = len(grid[0]) if grid else 0

    spawns = _spawn_positions(grid)
    player = Player(name="MELVIN", row=spawns[0][0], col=spawns[0][1])

    npcs    = _place_hub_npcs(grid)[0]
    npc_rng = random.Random(0x4875624E5043)
    tick    = 0

    # ── Helpers ───────────────────────────────────────────────────────────────
    def _send(event: dict):
        ws.send(json.dumps(event))

    def _npc_event() -> dict:
        return {
            "type": "int_enemies",
            "enemies": [
                {
                    "index":      n.index + 1000,
                    "row":        n.row,
                    "col":        n.col,
                    "npc_sprite": n.npc_sprite,
                    "name":       "NPC",
                    "anim_ms":    500,
                }
                for n in npcs
            ],
        }

    # ── Initial state ─────────────────────────────────────────────────────────
    payload = get_hub_tile_payload()
    _send({
        "type":        "hub_init",
        "rows":        rows,
        "cols":        cols,
        "tileset_url": payload["tileset_url"],
        "tile_grid":   payload["tile_grid"],
        "party":       [{"name": player.name, "row": player.row, "col": player.col}],
        "config":      {"player_move_ms": cfg.player_move_ms},
    })
    _send(_npc_event())

    # ── Input loop ────────────────────────────────────────────────────────────
    while True:
        msg = ws.receive()
        if msg is None:
            break
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            continue

        if data.get("type") != "key":
            continue

        direction = _KEY_TO_DIR.get(data.get("key", ""))
        if not direction or not player.can_move():
            continue

        if player.try_move(direction, str_grid):
            tick += 1
            npcs = update_town_npcs(npcs, str_grid, npc_rng)
            _send({
                "type":      "hub_move",
                "name":      player.name,
                "row":       player.row,
                "col":       player.col,
                "direction": direction,
                "tick":      tick,
            })
            _send(_npc_event())
