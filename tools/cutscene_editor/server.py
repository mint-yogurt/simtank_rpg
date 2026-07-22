"""Local server backing the visual cutscene editor -- the Phase 4 deliverable
of the cutscene build plan (see engine/cutscene.py's module docstring for
Phases 1-3). Run with:

    source .venv/bin/activate
    python tools/cutscene_editor/server.py

then open http://localhost:8420/ in a browser. This is a self-contained
local tool, its own folder, same relationship to engine/ that Tiled itself
already has to this project -- it doesn't touch engine/renderer.py's
one-file rule (see CLAUDE.md) since it never lives inside the engine.

Why a local HTTP server at all, not a plain static HTML file: a page opened
via file:// can't fetch other local files (no XHR/fetch of file:// URLs in
any browser) and has no way to write data/cutscenes/<id>.yaml back to disk.
Serving over http://localhost sidesteps both -- one Python process, stdlib
only (no new dependency), reusing this repo's own engine code directly
rather than re-deriving map/tileset logic in JS.

Map rendering: rather than re-implement Tiled's GID decode / multi-tileset /
flip-bit logic in JavaScript (real drift risk -- see the flip/firstgid
handling in engine.renderer.load_tiled_map/_tileset_for_gid/get_tile_by_gid),
this constructs a real engine.renderer.OverworldScene for the requested map
(headless, SDL_VIDEODRIVER=dummy -- same trick this project's own headless
integration tests already use) and calls its actual, private
_draw_tile_layer/_draw_entities methods onto a full-map-sized offscreen
Surface with camera at (0, 0) -- not draw() itself, since draw() computes a
viewport-clamped camera position (_camera_offset_px) sized for the real
in-game window, not a "show me the whole map" export. The PNG that comes out
is pixel-for-pixel what the game itself would draw, because it's the same
code drawing it. NPCs/objects overlay as separate JSON (position, name,
type, per-type fields) so the browser can make them clickable/selectable;
the background PNG itself already includes them baked in (draw() interleaves
tile layers and the object layer in Tiled's own authored order), so the
overlay is just hit-testing metadata, not a second visual layer.

Cutscene load/save: loading reuses engine.cutscene.load_cutscene directly
(so the editor can never show something the real parser would reject/read
differently) and converts its dataclasses to plain JSON. Saving converts the
editor's JSON back to the exact single-key-mapping YAML shape
engine/cutscene.py's docstring documents, then re-loads what was just
written via load_cutscene as a save-time correctness check (catches the
class of bug Phase 1 hit with YAML's bare on/off/yes/no keywords -- "the
file parsed" isn't proof it parsed into what was meant).
"""
import io
import json
import os
import sys
from pathlib import Path
from urllib.parse import parse_qs, urlparse

os.environ.setdefault("SDL_VIDEODRIVER", "dummy")
os.environ.setdefault("SDL_AUDIODRIVER", "dummy")

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

import pygame  # noqa: E402
import yaml  # noqa: E402

pygame.init()
pygame.display.set_mode((1, 1))

from engine.cutscene import (  # noqa: E402
    CutsceneDef,
    CutsceneStep,
    _CUTSCENES_DIR,
    _parse_steps,
    _parse_trigger,
    load_cutscene,
    load_cutscene_defs,
)
from engine.inventory import load_item_defs  # noqa: E402
from engine.npc import load_npc_defs  # noqa: E402
from engine.renderer import OverworldScene  # noqa: E402

_MAPS_DIR = _REPO / "data" / "maps"
_STATIC_DIR = Path(__file__).parent / "static"
_PORT = 8420

_scene_cache: dict[str, OverworldScene] = {}


def _available_maps() -> list[str]:
    return sorted(
        entry.name for entry in _MAPS_DIR.iterdir()
        if entry.is_dir() and (entry / f"{entry.name}.json").exists()
    )


def _get_scene(map_name: str) -> OverworldScene:
    if map_name not in _scene_cache:
        map_path = _MAPS_DIR / map_name / f"{map_name}.json"
        if not map_path.exists():
            raise FileNotFoundError(map_name)
        _scene_cache[map_name] = OverworldScene(map_path=map_path)
    return _scene_cache[map_name]


def _render_map_background(scene: OverworldScene) -> bytes:
    """See module docstring's "Map rendering" section -- draws the real,
    private per-layer methods onto a full-map-sized surface at camera
    (0, 0), not scene.draw() (which clamps to the in-game viewport)."""
    tile_px = scene.tile_px
    w = scene.tmap.width * tile_px
    h = scene.tmap.height * tile_px
    surface = pygame.Surface((w, h))
    surface.fill((0, 0, 0))
    for layer in scene.tmap.layers:
        if layer.kind == "tile":
            scene._draw_tile_layer(surface, layer, 0, 0)
        else:
            scene._draw_entities(surface, 0, 0)
    buf = io.BytesIO()
    pygame.image.save(surface, buf, "map.png")
    return buf.getvalue()


def _map_metadata(scene: OverworldScene, map_name: str) -> dict:
    return {
        "name": map_name,
        "tile_px": scene.tile_px,
        "width": scene.tmap.width,
        "height": scene.tmap.height,
        "background_url": f"/api/maps/{map_name}/background.png",
        "actors": (
            [{"name": "player", "row": scene.player.row, "col": scene.player.col,
              "width_span": 1, "height_span": 1, "facing": scene.player.facing, "kind": "player"}]
            + [{"name": n.name, "row": n.row, "col": n.col,
                "width_span": n.width_span, "height_span": n.height_span,
                "facing": n.facing, "kind": n.type} for n in scene.npcs]
        ),
        "objects": (
            [{"id": o.id, "name": o.name, "type": o.type, "row": o.row, "col": o.col,
              "contents": o.contents, "gold": o.gold, "dialogue": o.dialogue}
             for o in scene.objects]
            + [{"id": o.id, "name": o.name, "type": "warp", "row": o.row, "col": o.col,
                "destination_map": o.destination_map, "destination_warp": o.destination_warp}
               for o in scene.warps]
            + [{"id": o.id, "name": o.name, "type": "trigger", "row": o.row, "col": o.col,
                "cutscene_id": o.cutscene_id}
               for o in scene.triggers]
        ),
        "tile_layers": [layer.name for layer in scene.tmap.layers if layer.kind == "tile"],
    }


def _collect_flags(obj, acc: set[str]) -> None:
    if isinstance(obj, dict):
        for key, value in obj.items():
            if key in ("when", "unless") and isinstance(value, list):
                acc.update(v for v in value if isinstance(v, str))
            elif key == "flag" and isinstance(value, str):
                acc.add(value)
            else:
                _collect_flags(value, acc)
    elif isinstance(obj, list):
        for item in obj:
            _collect_flags(item, acc)


def _known_flags() -> list[str]:
    acc: set[str] = set()
    for path in _CUTSCENES_DIR.glob("*.yaml") if _CUTSCENES_DIR.exists() else []:
        _collect_flags(yaml.safe_load(path.read_text()) or {}, acc)
    for path in _MAPS_DIR.glob("*/npcs_*.yaml"):
        _collect_flags(yaml.safe_load(path.read_text()) or {}, acc)
    for path in _MAPS_DIR.glob("*/obj_*.yaml"):
        _collect_flags(yaml.safe_load(path.read_text()) or {}, acc)
    return sorted(acc)


def _step_to_json(step: CutsceneStep) -> dict:
    args = dict(step.args)
    if step.kind == "dialogue" and args.get("choices"):
        args["choices"] = [
            {"label": choice.label, "then": [_step_to_json(s) for s in choice.then]}
            for choice in args["choices"]
        ]
    return {"kind": step.kind, "args": args}


def _cutscene_to_json(cutscene: CutsceneDef) -> dict:
    trigger = cutscene.trigger
    return {
        "id": cutscene.id,
        "map": cutscene.map,
        "trigger": None if trigger is None else {
            "event": trigger.event, "when": trigger.when,
            "unless": trigger.unless, "actor": trigger.actor,
        },
        "steps": [_step_to_json(s) for s in cutscene.steps],
    }


def _step_to_yaml(step: dict) -> dict:
    kind = step["kind"]
    args = dict(step.get("args") or {})
    if kind == "dialogue" and args.get("choices"):
        args["choices"] = [
            {"label": choice["label"], "then": [_step_to_yaml(s) for s in choice.get("then", [])]}
            for choice in args["choices"]
        ]
    return {kind: args}


def _cutscene_to_yaml_text(data: dict) -> str:
    out: dict = {"id": data["id"], "map": data["map"]}
    trigger = data.get("trigger")
    if trigger:
        trigger_out = {"event": trigger["event"]}
        if trigger.get("when"):
            trigger_out["when"] = trigger["when"]
        if trigger.get("unless"):
            trigger_out["unless"] = trigger["unless"]
        if trigger.get("actor"):
            trigger_out["actor"] = trigger["actor"]
        out["trigger"] = trigger_out
    out["steps"] = [_step_to_yaml(s) for s in data.get("steps", [])]
    return yaml.safe_dump(out, sort_keys=False, allow_unicode=True)


class _ApiError(Exception):
    def __init__(self, status: int, message: str):
        super().__init__(message)
        self.status = status


def _handle_get(path: str, query: dict) -> tuple[bytes, str]:
    if path == "/api/maps":
        return json.dumps(_available_maps()).encode(), "application/json"

    if path.startswith("/api/maps/") and path.endswith("/background.png"):
        map_name = path[len("/api/maps/"):-len("/background.png")]
        try:
            scene = _get_scene(map_name)
        except FileNotFoundError:
            raise _ApiError(404, f"no such map: {map_name}")
        return _render_map_background(scene), "image/png"

    if path.startswith("/api/maps/"):
        map_name = path[len("/api/maps/"):]
        try:
            scene = _get_scene(map_name)
        except FileNotFoundError:
            raise _ApiError(404, f"no such map: {map_name}")
        return json.dumps(_map_metadata(scene, map_name)).encode(), "application/json"

    if path == "/api/flags":
        return json.dumps(_known_flags()).encode(), "application/json"

    if path == "/api/items":
        defs = load_item_defs()
        items = [{"id": d.id, "name": d.name} for d in defs.values()]
        return json.dumps(items).encode(), "application/json"

    if path == "/api/npc_ids":
        defs = load_npc_defs()
        return json.dumps(sorted(defs.keys())).encode(), "application/json"

    if path == "/api/cutscenes":
        defs = load_cutscene_defs()
        listing = [{"id": c.id, "map": c.map} for c in sorted(defs.values(), key=lambda c: c.id)]
        return json.dumps(listing).encode(), "application/json"

    if path.startswith("/api/cutscenes/"):
        cutscene_id = path[len("/api/cutscenes/"):]
        file_path = _CUTSCENES_DIR / f"{cutscene_id}.yaml"
        if not file_path.exists():
            raise _ApiError(404, f"no such cutscene: {cutscene_id}")
        cutscene = load_cutscene(file_path)
        return json.dumps(_cutscene_to_json(cutscene)).encode(), "application/json"

    raise _ApiError(404, f"no such route: {path}")


def _handle_post(path: str, body: bytes) -> tuple[bytes, str]:
    if path.startswith("/api/cutscenes/"):
        cutscene_id = path[len("/api/cutscenes/"):]
        try:
            data = json.loads(body)
        except json.JSONDecodeError as exc:
            raise _ApiError(400, f"bad JSON body: {exc}")
        if data.get("id") != cutscene_id:
            raise _ApiError(400, f"body id {data.get('id')!r} does not match URL id {cutscene_id!r}")

        yaml_text = _cutscene_to_yaml_text(data)
        _CUTSCENES_DIR.mkdir(parents=True, exist_ok=True)
        file_path = _CUTSCENES_DIR / f"{cutscene_id}.yaml"

        # Save-time round-trip check (see module docstring) -- write to a
        # *temp* path first, re-parse it with the real loader (load_cutscene)
        # and deep-compare the result against a canonical parse of what the
        # browser actually sent (built via the same _parse_trigger/
        # _parse_steps the file loader itself uses, just skipping the YAML
        # dump/write step) -- only once that matches does the temp file
        # replace the real one. A plain "did it parse" check wouldn't catch
        # e.g. a nested `then:` list quietly losing a step, or an arg value
        # getting YAML-coerced into another type (the bare on/off/yes/no
        # class of bug Phase 1 hit); doing it against a temp path means a
        # failed check never leaves a corrupted file where the good one was.
        tmp_path = file_path.with_suffix(".yaml.tmp")
        tmp_path.write_text(yaml_text)
        try:
            roundtrip = _cutscene_to_json(load_cutscene(tmp_path))
        except Exception as exc:
            tmp_path.unlink(missing_ok=True)
            raise _ApiError(500, f"not saved -- the written file failed to re-parse: {exc}")

        expected = _cutscene_to_json(CutsceneDef(
            id=data["id"], map=data["map"],
            trigger=_parse_trigger(data.get("trigger")),
            steps=_parse_steps([_step_to_yaml(s) for s in data.get("steps", [])]),
        ))
        if roundtrip != expected:
            tmp_path.unlink(missing_ok=True)
            raise _ApiError(500, "not saved -- the file would have re-parsed differently than what "
                                  "was authored; please report this")

        tmp_path.replace(file_path)
        return json.dumps({"ok": True}).encode(), "application/json"

    raise _ApiError(404, f"no such route: {path}")


from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer  # noqa: E402


class Handler(BaseHTTPRequestHandler):
    def _send(self, status: int, body: bytes, content_type: str) -> None:
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _send_json_error(self, status: int, message: str) -> None:
        self._send(status, json.dumps({"ok": False, "error": message}).encode(), "application/json")

    def do_GET(self) -> None:  # noqa: N802
        parsed = urlparse(self.path)
        path, query = parsed.path, parse_qs(parsed.query)

        if path == "/":
            path = "/index.html"

        if path.startswith("/api/"):
            try:
                body, content_type = _handle_get(path, query)
                self._send(200, body, content_type)
            except _ApiError as exc:
                self._send_json_error(exc.status, str(exc))
            except Exception as exc:
                self._send_json_error(500, f"{type(exc).__name__}: {exc}")
            return

        rel = path[len("/static/"):] if path.startswith("/static/") else path.lstrip("/")
        file_path = (_STATIC_DIR / rel).resolve()
        if _STATIC_DIR not in file_path.parents and file_path != _STATIC_DIR:
            self._send(403, b"forbidden", "text/plain")
            return
        if not file_path.is_file():
            self._send(404, b"not found", "text/plain")
            return
        content_type = {
            ".html": "text/html", ".js": "application/javascript", ".css": "text/css",
        }.get(file_path.suffix, "application/octet-stream")
        self._send(200, file_path.read_bytes(), content_type)

    def do_POST(self) -> None:  # noqa: N802
        length = int(self.headers.get("Content-Length", 0))
        body = self.rfile.read(length)
        try:
            resp_body, content_type = _handle_post(self.path, body)
            self._send(200, resp_body, content_type)
        except _ApiError as exc:
            self._send_json_error(exc.status, str(exc))
        except Exception as exc:
            self._send_json_error(500, f"{type(exc).__name__}: {exc}")

    def log_message(self, fmt: str, *args) -> None:
        print(f"[editor] {self.address_string()} - {fmt % args}", flush=True)


def main() -> None:
    server = ThreadingHTTPServer(("127.0.0.1", _PORT), Handler)
    print(f"Cutscene editor running at http://localhost:{_PORT}/  (Ctrl+C to stop)", flush=True)
    try:
        server.serve_forever()
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    main()
