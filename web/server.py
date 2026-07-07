"""Flask SSE server for simtank_rpg.

Broadcasts engine events to connected browsers via Server-Sent Events.
Screen PNGs are rendered on demand (deterministic, cached to disk).
"""

import json
import os
import queue
import threading
from pathlib import Path

from flask import Flask, Response, send_from_directory

# ── screen PNG rendering ──────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_SCREENS_DIR = _REPO_ROOT / "web" / "static" / "screens"
_TILESET_PATH = str(_REPO_ROOT / "web" / "static" / "tiles" / "overworld_1.png")
_TILERULES_PATH = str(_REPO_ROOT / "web" / "static" / "tiles" / "overworld_1_tilerules.txt")
_TOWN_TILESET_PATH = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_town.png"
_HUB_TILE_PX = 16

_CAVE_TILESET_PATH  = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_cave1.png"
_CAVE_RULES_PATH    = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_cave_rules.txt"
_TOWN_RULES_PATH    = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_town_rules.txt"

_raw_tiles = None
_raw_tiles_lock = threading.Lock()

_cave_raw_tiles = None
_cave_raw_lock  = threading.Lock()

_town_raw_tiles = None
_town_raw_lock  = threading.Lock()


def _get_raw_tiles():
    global _raw_tiles
    if _raw_tiles is None:
        with _raw_tiles_lock:
            if _raw_tiles is None:
                from procgen.worldgen import load_raw_tiles
                _raw_tiles = load_raw_tiles(_TILESET_PATH, _TILERULES_PATH)
    return _raw_tiles


def _get_cave_raw_tiles():
    global _cave_raw_tiles
    if _cave_raw_tiles is None:
        with _cave_raw_lock:
            if _cave_raw_tiles is None:
                from procgen.cavegen import load_raw_tiles as _cave_load
                _cave_raw_tiles = _cave_load(
                    str(_CAVE_TILESET_PATH), str(_CAVE_RULES_PATH))
    return _cave_raw_tiles


def _get_town_raw_tiles():
    global _town_raw_tiles
    if _town_raw_tiles is None:
        with _town_raw_lock:
            if _town_raw_tiles is None:
                from procgen.towngen import load_raw_tiles as _town_load
                _town_raw_tiles = _town_load(
                    str(_TOWN_TILESET_PATH), str(_TOWN_RULES_PATH))
    return _town_raw_tiles


def render_hub_map() -> str:
    """Render hub map PNG from tiles_town.png; return URL path. Cached on disk."""
    _SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    out_path = _SCREENS_DIR / "hub.png"
    if not out_path.exists():
        _render_hub_map_to(out_path)
    return "/screens/hub.png"


def _render_hub_map_to(out_path: Path) -> None:
    from PIL import Image
    from engine.scenes.hub import load_hub_coords

    px = _HUB_TILE_PX
    tileset = Image.open(str(_TOWN_TILESET_PATH)).convert("RGBA")
    coords = load_hub_coords()
    rows_count = len(coords)
    cols_count = max(len(r) for r in coords) if coords else 0

    out = Image.new("RGBA", (cols_count * px, rows_count * px), (0, 0, 0, 255))
    for r, row in enumerate(coords):
        for c, (tc, tr) in enumerate(row):
            tile = tileset.crop((tc * px, tr * px, (tc + 1) * px, (tr + 1) * px))
            out.paste(tile, (c * px, r * px))
    out.save(str(out_path))


def render_screen(world_seed: int, sx: int, sy: int) -> str:
    """Render screen PNG if not cached; return its URL path."""
    _SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"{world_seed}_{sx}_{sy}.png"
    out_path = _SCREENS_DIR / fname
    if not out_path.exists():
        from procgen.worldgen import generate_screen_data, render_screen_data
        data = generate_screen_data(world_seed, sx, sy)
        img = render_screen_data(data, _get_raw_tiles())
        img.save(str(out_path))
    return f"/screens/{fname}"


def render_interior_map(world_seed: int, feature_id: int,
                        tag: str, data: dict) -> str:
    """Render interior PNG (cave or town) if not cached; return URL path.

    tag:  'town' or 'dungeon' (from FEATURE_TYPES)
    data: raw dict from worlddb (serialized CaveData or TownData)
    """
    _SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    fname = f"interior_{world_seed}_{feature_id}.png"
    out_path = _SCREENS_DIR / fname
    if not out_path.exists():
        _render_interior_to(out_path, tag, data)
    return f"/screens/{fname}"


def _render_interior_to(out_path: Path, tag: str, data: dict) -> None:
    if tag == 'town':
        from procgen.towngen import render_town, remap_tileset
        raw = _get_town_raw_tiles()
        palette = [tuple(p) for p in data['palette']]
        tiles = remap_tileset(raw, palette)
        ground = {tuple(map(int, k.split(','))): v
                  for k, v in data['ground_grid'].items()}
        overlay = {tuple(map(int, k.split(','))): v
                   for k, v in data['overlay'].items()}
        crop_box = tuple(data['crop_box'])
        img = render_town(ground, overlay, tiles, crop_box)
    else:
        from procgen.cavegen import render_cave, remap_tileset
        raw = _get_cave_raw_tiles()
        grey, teal, gold = (tuple(p) for p in data['palette'])
        tiles = remap_tileset(raw, grey, teal, gold)
        floor_grid = {tuple(map(int, k.split(','))): v
                      for k, v in data['floor_grid'].items()}
        wall_grid = {tuple(map(int, k.split(','))): v
                     for k, v in data['wall_grid'].items()}
        img = render_cave(floor_grid, wall_grid, tiles)
    img.save(str(out_path))


# ── SSE fan-out ───────────────────────────────────────────────────────────────

_subscribers: set = set()
_subs_lock = threading.Lock()

# Snapshot of latest state — sent to clients that connect after loop has started.
_snapshot: dict | None = None
_snapshot_lock = threading.Lock()


def _update_snapshot(event: dict):
    global _snapshot
    t = event.get("type")
    with _snapshot_lock:
        if t == "init":
            _snapshot = dict(event)
        elif t == "hub_init":
            _snapshot = dict(event)
            # deep-copy party list so hub_move updates don't mutate the source event
            _snapshot["party"] = [dict(m) for m in event.get("party", [])]
        elif t == "hub_move" and _snapshot and _snapshot.get("type") == "hub_init":
            for m in _snapshot["party"]:
                if m["name"] == event["name"]:
                    m["row"] = event["row"]
                    m["col"] = event["col"]
                    break
        elif t == "screen" and _snapshot is not None:
            _snapshot.update({
                "sx": event["sx"], "sy": event["sy"],
                "row": event["row"], "col": event["col"],
                "rows": event["rows"], "cols": event["cols"],
                "screen_url": event["screen_url"],
            })
        elif t == "move" and _snapshot is not None:
            _snapshot.update({
                "row": event["row"], "col": event["col"],
                "sx": event["sx"], "sy": event["sy"],
            })
        elif t == "interior_init":
            _snapshot = dict(event)
        elif t == "interior_move" and _snapshot is not None:
            _snapshot.update({"row": event["row"], "col": event["col"]})


def broadcast(event: dict):
    """Called from the game loop thread to push an event to all SSE subscribers."""
    _update_snapshot(event)
    data = json.dumps(event)
    with _subs_lock:
        dead = set()
        for q in _subscribers:
            try:
                q.put_nowait(data)
            except queue.Full:
                dead.add(q)
        _subscribers.difference_update(dead)


# ── Flask app ─────────────────────────────────────────────────────────────────

app = Flask(__name__, static_folder=None)

_STATIC_DIR = str(_REPO_ROOT / "web" / "static")


@app.route("/")
def index():
    return send_from_directory(_STATIC_DIR, "index.html")


@app.route("/static/<path:filename>")
def static_files(filename):
    return send_from_directory(_STATIC_DIR, filename)


@app.route("/screens/<path:filename>")
def screen_files(filename):
    return send_from_directory(str(_SCREENS_DIR), filename)


@app.route("/events")
def events():
    q: queue.Queue = queue.Queue(maxsize=256)

    # Send snapshot immediately so late-joiners see current state.
    with _snapshot_lock:
        snap = dict(_snapshot) if _snapshot else None

    def stream():
        if snap:
            yield f"data: {json.dumps(snap)}\n\n"

        with _subs_lock:
            _subscribers.add(q)
        try:
            while True:
                try:
                    data = q.get(timeout=20)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"  # keep connection alive
        finally:
            with _subs_lock:
                _subscribers.discard(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
