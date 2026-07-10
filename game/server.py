"""WebSocket game server for Front House Gaiden.

Each connection gets its own independent game state (single player).
Client sends:  {"type": "key", "key": "ArrowUp"|"ArrowDown"|"ArrowLeft"|"ArrowRight"|...}
Server sends:  hub_init, hub_move, int_enemies events (same schema as SSE layer).

Run via run_game.py.
"""

import json
from pathlib import Path

from flask import Flask, send_from_directory
from flask_sock import Sock

from game.hub_player import HubPlayerState

_REPO_ROOT  = Path(__file__).parent.parent
_STATIC_GAME = Path(__file__).parent / "static"
_STATIC_WEB  = _REPO_ROOT / "web" / "static"
_SCREENS_DIR = _STATIC_WEB / "screens"

app  = Flask(__name__, static_folder=None)
sock = Sock(app)


@app.route("/")
def index():
    return send_from_directory(str(_STATIC_GAME), "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    # Game-specific overrides first, then fall back to shared web/static assets
    # (tilesets, sprites, tilemaps all live there).
    game_path = _STATIC_GAME / filename
    if game_path.exists():
        return send_from_directory(str(_STATIC_GAME), filename)
    return send_from_directory(str(_STATIC_WEB), filename)


@app.route("/screens/<path:filename>")
def screen_files(filename):
    _SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    return send_from_directory(str(_SCREENS_DIR), filename)


@sock.route("/ws")
def game_ws(ws):
    from web.server import get_hub_tile_payload   # reuse existing tileset renderer
    payload = get_hub_tile_payload()

    state = HubPlayerState()
    for event in state.init_events(payload["tileset_url"], payload["tile_grid"]):
        ws.send(json.dumps(event))

    while True:
        msg = ws.receive()
        if msg is None:
            break
        try:
            data = json.loads(msg)
        except (json.JSONDecodeError, TypeError):
            continue

        if data.get("type") == "key":
            for event in state.handle_key(data.get("key", "")):
                ws.send(json.dumps(event))
