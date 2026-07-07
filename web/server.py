"""Flask SSE server for the overworld viewer.

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

_raw_tiles = None
_raw_tiles_lock = threading.Lock()


def _get_raw_tiles():
    global _raw_tiles
    if _raw_tiles is None:
        with _raw_tiles_lock:
            if _raw_tiles is None:
                from procgen.worldgen import load_raw_tiles
                _raw_tiles = load_raw_tiles(_TILESET_PATH, _TILERULES_PATH)
    return _raw_tiles


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
            snap_init = dict(snap)
            snap_init["type"] = "init"
            yield f"data: {json.dumps(snap_init)}\n\n"

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
