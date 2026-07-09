"""Flask SSE server for simtank_rpg.

Broadcasts engine events to connected browsers via Server-Sent Events.
Tile payloads (tileset_url + tile_grid) are sent instead of baked screen PNGs.
"""

import hashlib
import json
import os
import queue
import threading
from pathlib import Path

from flask import Flask, Response, send_from_directory

from engine.config import cfg

# ── Paths ─────────────────────────────────────────────────────────────────────

_REPO_ROOT = Path(__file__).parent.parent
_SCREENS_DIR = _REPO_ROOT / "web" / "static" / "screens"

_PARTY_SPRITES      = _REPO_ROOT / "web" / "static" / "sprites" / "party_sprites.png"
_OVERWORLD_TILESET  = _REPO_ROOT / "web" / "static" / "tiles" / "overworld_1.png"
_OVERWORLD_RULES    = _REPO_ROOT / "web" / "static" / "tiles" / "overworld_1_tilerules.txt"
_CAVE_TILESET       = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_cave1.png"
_CAVE_RULES         = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_cave_rules.txt"
_TOWN_TILESET       = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_town.png"
_TOWN_RULES         = _REPO_ROOT / "web" / "static" / "tiles" / "tiles_town_rules.txt"

# ── Tileset PNG cache (palette-remapped copies, keyed by content hash) ────────

_tileset_lock   = threading.Lock()
_npcsprite_lock = threading.Lock()

# Sheet [sheetRow, sheetCol] positions for each NPC sprite's two animation frames.
# Matches partysprites.txt col,row entries — NPC_SPRITE in app.js mirrors this.
_NPC_SPRITE_FRAMES = {
    "npc01": [(4, 0), (4, 1)],
    "npc02": [(4, 2), (4, 3)],
    "npc03": [(5, 0), (5, 1)],
    "npc04": [(5, 2), (5, 3)],
    "npc05": [(4, 4), (4, 5)],
    "npc06": [(5, 4), (5, 5)],
    "npc07": [(6, 0), (6, 1)],
    "npc08": [(6, 2), (6, 3)],
}


def _palette_hash(*colors) -> str:
    flat = b"".join(bytes(c) for c in colors)
    return hashlib.md5(flat).hexdigest()[:12]


def _remap_png(img, color_map: dict):
    """Swap colors in a PIL RGBA image. color_map: {(r,g,b): (r,g,b)}."""
    data = list(img.getdata())
    data = [(color_map.get((r, g, b), (r, g, b)) + (a,)) for r, g, b, a in data]
    out = img.copy()
    out.putdata(data)
    return out


def _render_tileset_png(src_path: Path, tag: str, color_map: dict | None) -> str:
    """Load tileset PNG, optionally remap colors, cache to screens dir, return URL."""
    _SCREENS_DIR.mkdir(parents=True, exist_ok=True)
    if color_map:
        h = _palette_hash(*color_map.values())
        fname = f"tileset_{tag}_{h}.png"
    else:
        fname = f"tileset_{tag}.png"
    out_path = _SCREENS_DIR / fname
    with _tileset_lock:
        if not out_path.exists():
            from PIL import Image
            img = Image.open(str(src_path)).convert("RGBA")
            if color_map:
                img = _remap_png(img, color_map)
            img.save(str(out_path))
    return f"/screens/{fname}"


def _render_npcsprite_png(npc_key: str, palette: list) -> str:
    """Extract the two frames for npc_key, remap placeholder colors, cache, return URL.

    palette is a list of [r,g,b] lists — one per placeholder color for this sprite.
    Returns empty string if the sprite key is unknown or palette is empty.
    """
    from procgen.enemygen import NPC_PLACEHOLDER_COLORS
    placeholders = NPC_PLACEHOLDER_COLORS.get(npc_key)
    if not placeholders or not palette:
        return ""
    frames = _NPC_SPRITE_FRAMES.get(npc_key)
    if not frames:
        return ""

    color_map = {src: tuple(dst) for src, dst in zip(placeholders, palette)}
    h = _palette_hash(*[tuple(c) for c in palette])
    fname = f"npcsprite_{npc_key}_{h}.png"
    out_path = _SCREENS_DIR / fname

    with _npcsprite_lock:
        if not out_path.exists():
            from PIL import Image
            _SCREENS_DIR.mkdir(parents=True, exist_ok=True)
            sheet = Image.open(str(_PARTY_SPRITES)).convert("RGBA")
            TILE = 16
            # Build a 2-frame horizontal strip (32 × 16) from the sheet.
            strip = Image.new("RGBA", (TILE * 2, TILE), (0, 0, 0, 0))
            for i, (row, col) in enumerate(frames):
                frame_img = sheet.crop(
                    (col * TILE, row * TILE, (col + 1) * TILE, (row + 1) * TILE))
                strip.paste(frame_img, (i * TILE, 0))
            strip = _remap_png(strip, color_map)
            strip.save(str(out_path))

    return f"/screens/{fname}"


def get_npc_sprite_url(npc_key: str, palette: list) -> str:
    """Public entry point: return a cached URL for a recolored NPC sprite strip."""
    return _render_npcsprite_png(npc_key, palette)


# ── Per-tileset payload builders ──────────────────────────────────────────────

def get_screen_tile_payload(world_seed: int, sx: int, sy: int) -> dict:
    """Return {tileset_url, tile_grid} for an overworld screen."""
    from procgen.worldgen import generate_screen_data, SRC_GREEN, SRC_BLUE, SRC_BROWN
    data = generate_screen_data(world_seed, sx, sy)
    green, blue, brown = data.palette
    color_map = {SRC_GREEN: green, SRC_BLUE: blue, SRC_BROWN: brown}
    tileset_url = _render_tileset_png(_OVERWORLD_TILESET, "overworld", color_map)
    return {"tileset_url": tileset_url, "tile_grid": data.grid}


def get_hub_tile_payload() -> dict:
    """Return {tileset_url, tile_grid} for the static hub map. No palette swap."""
    from engine.scenes.hub import load_hub_grid
    tile_grid = load_hub_grid()
    tileset_url = _render_tileset_png(_TOWN_TILESET, "hub", None)
    return {"tileset_url": tileset_url, "tile_grid": tile_grid}


def get_interior_tile_payload(world_seed: int, feature_id: int,
                              tag: str, data: dict) -> dict:
    """Return {tileset_url, tile_grid} for a cave or town interior."""
    if tag == "town":
        from procgen.towngen import SRC_COLORS
        palette = [tuple(p) for p in data["palette"]]
        color_map = dict(zip(SRC_COLORS, palette))
        tileset_url = _render_tileset_png(_TOWN_TILESET, "town", color_map)
        # town grid is ground_grid overlaid with overlay
        ground = {tuple(map(int, k.split(","))): v
                  for k, v in data["ground_grid"].items()}
        overlay = {tuple(map(int, k.split(","))): v
                   for k, v in data["overlay"].items()}
        # Determine dimensions from crop_box
        r0, c0, r1, c1 = data["crop_box"]
        rows, cols = r1 - r0, c1 - c0
        tile_grid = []
        for r in range(rows):
            row = []
            for c in range(cols):
                abs_r, abs_c = r + r0, c + c0
                ov  = overlay.get((abs_r, abs_c))
                gnd = ground.get((abs_r, abs_c)) or "grass1"
                # Overlay tiles have transparent backgrounds — send both so
                # JS can draw ground first then the overlay on top.
                row.append([gnd, ov] if ov else gnd)
            tile_grid.append(row)
    else:
        from procgen.cavegen import SRC_GREY, SRC_TEAL, SRC_GOLD
        grey, teal, gold = (tuple(p) for p in data["palette"])
        color_map = {SRC_GREY: grey, SRC_TEAL: teal, SRC_GOLD: gold}
        tileset_url = _render_tileset_png(_CAVE_TILESET, "cave", color_map)
        floor_grid = {tuple(map(int, k.split(","))): v
                      for k, v in data["floor_grid"].items()}
        wall_grid = {tuple(map(int, k.split(","))): v
                     for k, v in data["wall_grid"].items()}
        # Crop to content bounding box + padding, matching overworld_loop origin calc.
        _PAD = 2  # must equal _CAVE_PNG_PAD in overworld_loop.py
        all_cells = set(floor_grid) | set(wall_grid)
        if all_cells:
            min_r = min(r for r, c in all_cells)
            min_c = min(c for r, c in all_cells)
            max_r = max(r for r, c in all_cells)
            max_c = max(c for r, c in all_cells)
            origin_r = min_r - _PAD
            origin_c = min_c - _PAD
            rows = max_r + _PAD - origin_r + 1
            cols = max_c + _PAD - origin_c + 1
        else:
            origin_r = origin_c = 0
            rows, cols = 0, 0
        tile_grid = []
        for vr in range(rows):
            row = []
            for vc in range(cols):
                r, c = vr + origin_r, vc + origin_c
                tile = wall_grid.get((r, c)) or floor_grid.get((r, c)) or "void"
                row.append(tile)
            tile_grid.append(row)
    return {"tileset_url": tileset_url, "tile_grid": tile_grid}


# ── SSE fan-out ───────────────────────────────────────────────────────────────

_subscribers: set = set()
_subs_lock = threading.Lock()

# Snapshot of latest state — sent to clients that connect after loop has started.
_snapshot: dict | None = None
_pre_battle_snapshot: dict | None = None  # saved overworld state, restored after battle
_enemies_snapshot: dict | None = None     # latest enemies event for reconnecting clients
_snapshot_lock = threading.Lock()


def _update_snapshot(event: dict):
    global _snapshot, _pre_battle_snapshot, _enemies_snapshot
    t = event.get("type")
    with _snapshot_lock:
        if t == "init":
            _snapshot = dict(event)
            _enemies_snapshot = None
        elif t == "hub_init":
            _snapshot = dict(event)
            _snapshot["party"] = [dict(m) for m in event.get("party", [])]
            _enemies_snapshot = None
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
                "tileset_url": event["tileset_url"],
                "tile_grid": event["tile_grid"],
            })
            _enemies_snapshot = None  # clear on screen crossing
        elif t == "enemies":
            _enemies_snapshot = dict(event)
        elif t == "move" and _snapshot is not None:
            _snapshot.update({
                "row": event["row"], "col": event["col"],
                "sx": event["sx"], "sy": event["sy"],
            })
        elif t == "interior_init":
            _snapshot = dict(event)
            _enemies_snapshot = None
        elif t == "interior_move" and _snapshot is not None:
            _snapshot.update({"row": event["row"], "col": event["col"]})
        elif t == "battle_start":
            _pre_battle_snapshot = dict(_snapshot) if _snapshot else None
            _snapshot = dict(event)
        elif t == "battle_action" and _snapshot is not None:
            _snapshot["enemy_hp"]  = event.get("enemy_hp", _snapshot.get("enemy_hp"))
            _snapshot["party_hp"]  = event.get("party_hp", _snapshot.get("party_hp"))
        elif t == "battle_end":
            if _pre_battle_snapshot is not None:
                _snapshot = _pre_battle_snapshot
                _pre_battle_snapshot = None


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

    with _snapshot_lock:
        snap  = dict(_snapshot) if _snapshot else None
        esnap = dict(_enemies_snapshot) if _enemies_snapshot else None

    def stream():
        if snap:
            yield f"data: {json.dumps(snap)}\n\n"
        if esnap:
            yield f"data: {json.dumps(esnap)}\n\n"

        with _subs_lock:
            _subscribers.add(q)
        try:
            while True:
                try:
                    data = q.get(timeout=20)
                    yield f"data: {data}\n\n"
                except queue.Empty:
                    yield ": heartbeat\n\n"
        finally:
            with _subs_lock:
                _subscribers.discard(q)

    return Response(stream(), mimetype="text/event-stream",
                    headers={"Cache-Control": "no-cache",
                             "X-Accel-Buffering": "no"})
