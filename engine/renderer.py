"""THE graphical renderer — the single script that owns everything on-screen.

This is one file on purpose: the generic pygame app loop (window/clock/event
dispatch), Tiled JSON map + tileset loading, sprite-sheet slicing, the camera,
and the OverworldScene handle_event/update/draw loop all live here together.
Nothing about "reading the map," "drawing the map," or "running the loop" is
split into a separate module — that split was tried once and rejected.

Player movement *rules* stay in engine/player.py (headless: takes a plain
passable: list[list[bool]] grid, no knowledge of Tiled/GIDs). Held-key
direction resolution stays in engine/input.py (pure decision logic). Both are
imported here, not duplicated.

Entry point: engine.renderer.run(OverworldScene, ...) — see maptest.py.

NPC objects (type == "npc" in a map's object layer) render and move per their
authored `behavior` — "static" idles in place, "wander" roams walkable tiles
— and are interactable exactly like signs: face one and press A to open its
`dialogue` (see MapObject/NPC), read via engine.dialogue. No npc-type
objects are placed on hub_fronthouse.json yet, though — its object layer
only has container/sign objects so far.

Warp objects (type == "warp") are invisible, one-way, one-tile trigger zones
— unlike signs/NPCs there's no A-press, stepping onto one is enough. Each
warp names its destination as a (destination_map, destination_warp) pair —
the other map's folder/stem name, plus the *name* of the warp object on that
map to land on — so warp names only need to be unique within one map's
object layer, never globally (see data/maps/populate_yamls.py). Triggering
one pauses gameplay and fades to black (cfg.warp_fade_out_ms), swaps the
loaded map underneath the fully-black frame, then fades back in
(cfg.warp_fade_in_ms) on the new map — see OverworldScene._step_player,
_begin_warp, _tick_transition, _swap_map, _load_map.

Controls:
  Arrow keys  — move player (MELVIN) / move start-menu or save-confirm cursor
  Enter       — START: open/close the start menu (no-op while save-confirm is open)
  X           — A: confirm start-menu selection (SAVE opens the save-confirm
                overlay; other options stubbed) / confirm YES-NO on that overlay
                (YES is stubbed — see engine.menu.SaveMenu.confirm) / advance or
                close an open dialogue box / open one by facing a sign or NPC
  Z           — B: close whichever menu is on top (save-confirm, then start menu)
  Escape / Q  — quit
"""
import json
import random
from dataclasses import dataclass, field
from pathlib import Path

import pygame
import yaml

from engine.config import cfg
from engine.dialogue import DialogueBox
from engine.input import (
    HeldDirectionInput,
    handle_a_button,
    handle_b_button,
    handle_menu_direction,
    handle_start_button,
)
from engine.menu import SaveMenu, StartMenu
from engine.player import Player, PlayerState

_REPO_ROOT   = Path(__file__).parent.parent
_ASSETS      = _REPO_ROOT / 'assets'
_MAPS_DIR    = _REPO_ROOT / 'data' / 'maps'
_HUB_MAP     = _MAPS_DIR / 'hub_fronthouse' / 'hub_fronthouse.json'
_SPRITES_PNG = _ASSETS / 'sprites' / 'party_sprites.png'
_SPRITES_TXT = _ASSETS / 'sprites' / 'partysprites.txt'
_MENU_DIR    = _ASSETS / 'menus'

_NPC_ANIM_MS = 500   # ms per NPC animation frame flip
_NPC_MOVE_MS = 800   # ms between NPC AI position updates; also NPC tween duration

_DIR_KEY = {
    pygame.K_UP:    'N',
    pygame.K_DOWN:  'S',
    pygame.K_LEFT:  'W',
    pygame.K_RIGHT: 'E',
}

_BUTTON_KEY = {
    pygame.K_RETURN: 'START',
    pygame.K_z:      'B',
    pygame.K_x:      'A',
}

# Start-menu layout: top-left pixel of each option row, and the cursor's
# resting spot (same coordinate as the row it's highlighting). The
# highlighted row's own image shifts right by this many pixels to make room.
_MENU_X = 160
_MENU_ROW_Y = {
    'inventory': 21,
    'party':     37,
    'settings':  53,
    'save':      69,
}
_MENU_SELECT_SHIFT_PX = 7

# Save-confirm overlay layout: fixed cursor spot per option, drawn on top of
# the start menu. Only the cursor moves — the bg image is one static overlay.
_SAVE_MENU_CURSOR_Y = 117
_SAVE_MENU_CURSOR_X = {
    'YES': 87,
    'NO':  139,
}

# Dialogue box layout. dialogue_box.png is 256x240 — 16px taller than the
# 256x224 game screen (view_cols/view_rows * tile_px) — so it docks to one
# edge of the screen with the other 16px cropped off, same idea as NES
# overscan. Free text space within the image's own local coordinates is
# given as top-left/bottom-right pixel coordinates (10,178)-(245,233),
# inset by a margin so wrapped text doesn't sit flush against those edges.
_DIALOGUE_FONT_PATH = _ASSETS / 'fonts' / 'ModernDOS8x8.ttf'
_DIALOGUE_FONT_PT = 16   # renders at 8px — this font's point size isn't 1:1 with pixels
_DIALOGUE_MARGIN_PX = 4
_DIALOGUE_TEXT_LEFT = 10 + _DIALOGUE_MARGIN_PX
_DIALOGUE_TEXT_TOP = 178 + _DIALOGUE_MARGIN_PX
_DIALOGUE_TEXT_RIGHT = 245 - _DIALOGUE_MARGIN_PX
_DIALOGUE_TEXT_BOTTOM = 233 - _DIALOGUE_MARGIN_PX
_DIALOGUE_TEXT_W = _DIALOGUE_TEXT_RIGHT - _DIALOGUE_TEXT_LEFT
_DIALOGUE_TEXT_H = _DIALOGUE_TEXT_BOTTOM - _DIALOGUE_TEXT_TOP
_DIALOGUE_TEXT_COLOR = (255, 255, 255)


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _wrap_text_to_screens(font: pygame.font.Font, text: str, rect_w: int, rect_h: int) -> list[str]:
    """Word-wrap `text` to `rect_w`-px lines, then group those lines into
    `rect_h`-px screens — one screen per A press, newline-joined for draw.

    A page that fits in one screen returns a single-element list; a longer
    page returns multiple, so the caller (engine.dialogue.DialogueBox, via
    the pages list it's opened with) pages through them one A press at a time.
    """
    words = text.split()
    lines: list[str] = []
    current = ""
    for word in words:
        candidate = f"{current} {word}".strip()
        if not current or font.size(candidate)[0] <= rect_w:
            current = candidate
        else:
            lines.append(current)
            current = word
    if current:
        lines.append(current)

    line_h = font.get_linesize()
    lines_per_screen = max(1, rect_h // line_h)
    screens = [
        "\n".join(lines[i:i + lines_per_screen])
        for i in range(0, len(lines), lines_per_screen)
    ]
    return screens or [""]


# ── Generic app loop ─────────────────────────────────────────────────────────
#
# Owns the window, the clock, and event dispatch. Knows nothing about maps,
# tiles, or NPCs — any scene that implements handle_event/update/draw can run
# here. Title screens, the overworld, and battle all drive through this same
# loop; nothing about it is specific to any one scene.

def run(scene_factory, view_size: tuple[int, int], scale: int, title: str) -> None:
    """Open the window, build the scene, then run it until closed or Escape/Q.

    `scene_factory` is called with no arguments after pygame.display.set_mode()
    so scene construction (tileset/sprite loading, etc.) can safely call
    .convert_alpha().

    The constructed scene must implement:
        handle_event(event: pygame.event.Event) -> None
        update(dt_ms: int) -> None
        draw(surface: pygame.Surface) -> None
    """
    pygame.init()
    view_w, view_h = view_size
    win_w, win_h = view_w * scale, view_h * scale
    window = pygame.display.set_mode((win_w, win_h))
    pygame.display.set_caption(title)
    screen = pygame.Surface((view_w, view_h))  # native-res render target

    scene = scene_factory()

    clock = pygame.time.Clock()
    running = True
    while running:
        dt = clock.tick(60)  # ms since last frame; caps at 60 fps

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False
            else:
                scene.handle_event(event)

        scene.update(dt)

        scene.draw(screen)
        pygame.transform.scale(screen, (win_w, win_h), window)
        pygame.display.flip()

    pygame.quit()


# ── Sprite sheet loading ─────────────────────────────────────────────────────
#
# Parses partysprites.txt (col,row = sprite_name) and slices party_sprites.png
# into tile_px x tile_px named surfaces.

_FACING_SUFFIX = {'S': ('S1', 'S2'), 'N': ('N1', 'N2'),
                  'W': ('W1', 'W2'), 'E': ('E1', 'E2')}

_PARTY_NAMES = ('melvin', 'billy', 'smeltrud', 'poots')

# For NPCs that only have S1 frames (txt lists S1 twice), both frames point to S1.
# npc08 uses its W/E frames as the two animation frames (per partysprites.txt comment).
_NPC_FRAME_NAMES: dict[str, tuple[str, str]] = {
    'npc01': ('npc01_S1', 'npc01_S2'),
    'npc02': ('npc02_S1', 'npc02_S2'),
    'npc03': ('npc03_S1', 'npc03_S2'),
    'npc04': ('npc04_S1', 'npc04_S2'),
    'npc05': ('npc05_S1', 'npc05_S2'),
    'npc06': ('npc06_S1', 'npc06_S1'),
    'npc07': ('npc07_S1', 'npc07_S1'),
    'npc08': ('npc08_W',  'npc08_E'),
}


def _parse_spritemap(txt_path) -> dict[str, tuple[int, int]]:
    """Return {sprite_name: (col, row)} from partysprites.txt."""
    result: dict[str, tuple[int, int]] = {}
    for line in Path(txt_path).read_text().splitlines():
        line = line.strip()
        if not line or '=' not in line:
            continue
        eq = line.index('=')
        coord_part = line[:eq].strip()
        rest = line[eq + 1:]
        if '#' in rest:
            rest = rest[:rest.index('#')]
        name = rest.split(',')[0].strip().rstrip('_').replace(' ', '')
        if not name:
            continue
        try:
            col, row = [int(v) for v in coord_part.split(',')]
        except ValueError:
            continue
        result[name] = (col, row)
    return result


def load_sprites(png_path, txt_path, tile_px: int = 16) -> dict[str, pygame.Surface]:
    """Slice the party sprite sheet into named surfaces.

    Call after pygame.display.set_mode().
    """
    sprite_map = _parse_spritemap(txt_path)
    sheet = pygame.image.load(str(png_path)).convert_alpha()
    sprites: dict[str, pygame.Surface] = {}
    for name, (col, row) in sprite_map.items():
        rect = pygame.Rect(col * tile_px, row * tile_px, tile_px, tile_px)
        surf = pygame.Surface((tile_px, tile_px), pygame.SRCALPHA)
        surf.blit(sheet, (0, 0), rect)
        sprites[name] = surf
    return sprites


def get_party_frame(sprites: dict[str, pygame.Surface],
                    member_name: str, facing: str,
                    anim_frame: int) -> pygame.Surface | None:
    """Surface for a party member's current animation state.

    member_name: 'melvin' | 'billy' | 'smeltrud' | 'poots' (case-insensitive)
    facing: 'N' | 'S' | 'E' | 'W'
    anim_frame: 0 or 1
    """
    name_lc = member_name.lower()
    suffixes = _FACING_SUFFIX.get(facing, ('S1', 'S2'))
    sprite_name = name_lc + suffixes[anim_frame % 2]
    surf = sprites.get(sprite_name)
    if surf is not None:
        return surf
    # Fallback: facing-S frame 0
    return sprites.get(name_lc + 'S1')


def get_npc_frame(sprites: dict[str, pygame.Surface],
                  npc_sprite: str, anim_frame: int) -> pygame.Surface | None:
    """Surface for an NPC's current animation frame."""
    pair = _NPC_FRAME_NAMES.get(npc_sprite)
    if not pair:
        return None
    return sprites.get(pair[anim_frame % 2])


# ── Start menu assets ────────────────────────────────────────────────────────

def load_menu_assets(menu_dir: Path) -> dict[str, pygame.Surface]:
    """Load the start menu's and save-confirm overlay's PNGs, keyed by name.
    Call after pygame.display.set_mode()."""
    filenames = {
        'bg':               'startmenu_bg.png',
        'cursor':           'startmenu_cursor.png',
        'inventory':        'startmenu_inventory.png',
        'party':            'startmenu_party.png',
        'settings':         'startmenu_settings.png',
        'save':             'startmenu_save.png',
        'save_confirm_bg':     'save_menu_bg.png',
        'save_confirm_cursor': 'save_menu_cursor.png',
        'dialogue_box':        'dialogue_box.png',
    }
    assets = {
        key: pygame.image.load(str(menu_dir / filename)).convert_alpha()
        for key, filename in filenames.items()
    }
    # Pre-flipped copy for docking the dialogue box to the top of the screen
    # instead of the bottom — see OverworldScene._draw_dialogue_box.
    assets['dialogue_box_flipped'] = pygame.transform.flip(assets['dialogue_box'], False, True)
    return assets


# ── Tiled JSON map + tileset loading ────────────────────────────────────────
#
# Maps are authored in Tiled and exported as JSON to data/maps/*.json. Per-tile
# metadata (currently just `walkable`) is authored on the tileset and exported
# separately as a JSON tileset next to the tileset image in assets/tiles/ — the
# map's own `tilesets[].source` still points at the .tsx Tiled manages
# internally, which isn't where the JSON export lands, so the two files are
# matched by basename instead of following `source` literally.

@dataclass
class TiledTileset:
    image:      str                 # image filename, relative to assets/tiles/
    columns:    int
    tilecount:  int
    tilewidth:  int
    tileheight: int
    walkable:   dict[int, bool]     # local tile id -> walkable


@dataclass
class TiledLayer:
    name:  str
    grid:  list[list[int]]          # GIDs, row-major [row][col]
    above: bool                     # True for every tile layer but the first — see load_tiled_map


@dataclass
class MapObject:
    """A container/sign/npc/warp placed on the map's Tiled object layer,
    merged with its hand-authored content from obj_<map>.yaml /
    npcs_<map>.yaml (see data/maps/populate_yamls.py). `dialogue` is a list
    of pages; `type == "sign"` opens it directly, and `type == "npc"`
    entries hand theirs off to NPC (see below, built from these in
    OverworldScene.__init__) which is interactable the same way. Containers
    are out of scope until the object/event system exists. `sprite`/
    `behavior` are only ever set for `type == "npc"` entries;
    `destination_map`/`destination_warp`/`facing`/`distance` are only ever
    set for `type == "warp"` entries — see
    OverworldScene._step_player/_swap_map. `facing`/`distance` do double
    duty: they're this warp's own landing spot when something else's
    destination_warp points here (`distance` tiles from this warp's row/col,
    offset in the `facing` direction; distance 0 or unset lands exactly on
    it) *and* the direction the player faces once they land.

    `row`/`col` are the tile this object's top-left pixel floors into —
    used for gameplay (facing/interaction checks, and for NPCs, wander
    stepping/collision). `row_exact`/`col_exact` are the same position
    un-floored, i.e. Tiled's authored pixel placement in tile units; only
    NPC's *initial* render position (see OverworldScene.__init__'s
    _npc_tweens seed) ever reads these, so a static or freshly-placed NPC
    draws exactly where it was put in Tiled instead of snapping to the
    grid. Containers/signs always draw tile-snapped (row/col * tile_px) —
    this field exists on them too but nothing reads it."""
    id:       int
    name:     str
    type:     str
    row:      int
    col:      int
    gid:      int
    row_exact: float = 0.0
    col_exact: float = 0.0
    dialogue: list[str] = field(default_factory=list)
    sprite:   str | None = None
    behavior: str | None = None
    destination_map:  str | None = None
    destination_warp: str | None = None
    facing:   str | None = None
    distance: int | None = None


def load_map_objects(raw: dict, tile_px: int, map_dir: Path, map_name: str) -> list[MapObject]:
    """Parse every objectgroup layer and merge in each object's YAML content.

    Tile objects (the ones with a `gid` — containers/signs, stamped with
    Tiled's tile-stamp tool) anchor at their bottom-left corner in Tiled's
    coordinate system, not top-left like plain rectangles, so their height
    has to be subtracted before converting to a tile row. Objects placed
    with the rectangle tool instead (no `gid` — e.g. NPCs, which don't need
    a tileset graphic) anchor top-left like any rectangle and must NOT get
    that correction, or they land one tile north of where Tiled shows them.
    """
    content: dict[int, dict] = {}
    for filename in (f"obj_{map_name}.yaml", f"npcs_{map_name}.yaml"):
        path = map_dir / filename
        if path.exists():
            content.update(yaml.safe_load(path.read_text()) or {})

    objects: list[MapObject] = []
    for layer in raw["layers"]:
        if layer["type"] != "objectgroup":
            continue
        for obj in layer["objects"]:
            y = (obj["y"] - obj["height"]) if "gid" in obj else obj["y"]
            row_exact = y / tile_px
            col_exact = obj["x"] / tile_px
            data = content.get(obj["id"], {})
            objects.append(MapObject(
                id        = obj["id"],
                name      = obj["name"],
                type      = obj["type"],
                row       = int(row_exact),
                col       = int(col_exact),
                gid       = obj.get("gid", 0),
                row_exact = row_exact,
                col_exact = col_exact,
                dialogue  = data.get("dialogue", []),
                sprite    = data.get("sprite"),
                behavior  = data.get("behavior"),
                destination_map  = data.get("destination_map"),
                destination_warp = data.get("destination_warp"),
                facing    = data.get("facing"),
                distance  = data.get("distance"),
            ))
    return objects


@dataclass
class TiledMap:
    width:      int                 # tiles
    height:     int                 # tiles
    tilewidth:  int
    tileheight: int
    firstgid:   int
    tileset:    TiledTileset
    layers:     list[TiledLayer]
    objects:    list[MapObject]


def _load_tiled_tileset(path: Path) -> TiledTileset:
    raw = json.loads(path.read_text())
    walkable: dict[int, bool] = {}
    for tile in raw.get("tiles", []):
        for prop in tile.get("properties", []):
            if prop["name"] == "walkable":
                walkable[tile["id"]] = bool(prop["value"])
    return TiledTileset(
        image      = raw["image"],
        columns    = raw["columns"],
        tilecount  = raw["tilecount"],
        tilewidth  = raw["tilewidth"],
        tileheight = raw["tileheight"],
        walkable   = walkable,
    )


def load_tiled_map(path: Path) -> TiledMap:
    """Load a Tiled JSON map and its sibling tileset JSON."""
    raw = json.loads(path.read_text())

    ts_entry = raw["tilesets"][0]
    firstgid = ts_entry["firstgid"]
    ts_stem = Path(ts_entry["source"]).stem
    tileset = _load_tiled_tileset(_ASSETS / "tiles" / f"{ts_stem}.json")

    width, height = raw["width"], raw["height"]
    layers = []
    for layer in raw["layers"]:
        if layer["type"] != "tilelayer":
            continue
        flat = layer["data"]
        grid = [flat[r * width:(r + 1) * width] for r in range(height)]
        # The first tile layer is the ground and draws below the player/NPCs/
        # objects; every tile layer after it draws above them, stacked in the
        # same order Tiled lists them in, so any number of overlay layers
        # (roofs, canopies, ...) composite correctly.
        layers.append(TiledLayer(name=layer["name"], grid=grid, above=len(layers) > 0))

    objects = load_map_objects(raw, raw["tilewidth"], path.parent, path.stem)

    return TiledMap(
        width      = width,
        height     = height,
        tilewidth  = raw["tilewidth"],
        tileheight = raw["tileheight"],
        firstgid   = firstgid,
        tileset    = tileset,
        layers     = layers,
        objects    = objects,
    )


def tiled_passable_grid(tmap: TiledMap) -> list[list[bool]]:
    """Build a [row][col] -> bool grid from every layer's `walkable` property.

    A cell is blocked if ANY non-empty tile stacked there (across layers) is
    marked not walkable. GID 0 (empty cell) never blocks by itself.
    """
    grid = [[True] * tmap.width for _ in range(tmap.height)]
    for layer in tmap.layers:
        for r, row in enumerate(layer.grid):
            for c, gid in enumerate(row):
                if gid == 0:
                    continue
                local_id = gid - tmap.firstgid
                if not tmap.tileset.walkable.get(local_id, False):
                    grid[r][c] = False
    return grid


def tiled_spawn_point(tmap: TiledMap) -> tuple[int, int]:
    """Player spawn tile: a few rows up from the bottom edge, centered.

    No object layer with an authored spawn point exists yet.
    """
    bottom_row = tmap.height - 1
    spawn_row = max(bottom_row - 4, 0)
    return (spawn_row, tmap.width // 2)


def load_tileset_by_gid(image_path: Path, columns: int, tile_px: int) -> list[pygame.Surface]:
    """Slice a tileset image into a flat list of surfaces, index == local tile id.

    Matches Tiled's own local-id numbering (row * columns + col), so a GID
    from a map layer resolves via `gid - firstgid` directly into this list.
    """
    sheet = pygame.image.load(str(image_path)).convert_alpha()
    _, sheet_h = sheet.get_size()
    rows = sheet_h // tile_px
    surfaces = []
    for row in range(rows):
        for col in range(columns):
            rect = pygame.Rect(col * tile_px, row * tile_px, tile_px, tile_px)
            surf = pygame.Surface((tile_px, tile_px), pygame.SRCALPHA)
            surf.blit(sheet, (0, 0), rect)
            surfaces.append(surf)
    return surfaces


def get_tile_by_gid(surfaces: list[pygame.Surface], firstgid: int, gid: int) -> pygame.Surface | None:
    """Resolve a Tiled GID to a surface. Returns None for GID 0 (empty cell)."""
    if gid == 0:
        return None
    local_id = gid - firstgid
    if 0 <= local_id < len(surfaces):
        return surfaces[local_id]
    return None


# ── NPC behaviors ────────────────────────────────────────────────────────────
#
# Two behaviors, authored per-NPC via the `behavior` field in npcs_<map>.yaml
# (see data/maps/populate_yamls.py):
#   "static" — never moves, just plays its 2-frame idle animation in place.
#   "wander" — every _NPC_MOVE_MS tick, a coin flip decides whether it takes
#              one step in a random cardinal direction, same as the player:
#              onto a walkable tile that isn't already occupied.
# Any other/missing value (an un-authored NPC) defaults to "static" — the
# safe choice, since an unconfigured NPC standing still is harmless and an
# unconfigured one wandering unexpectedly would look like a bug.
#
# Unlike MapObject (which is immutable map content), NPC.row/col mutate at
# runtime as wander NPCs move around — this is live gameplay state, not
# authored content. NPC sprites reuse the party sheet's NPC frames (see
# get_npc_frame/_NPC_FRAME_NAMES above) exactly like the player does with its
# own frames; there's no per-direction facing since almost all NPC frame
# pairs are facing-less idle/animation pairs, not a real walk cycle.

_NPC_WANDER_MOVE_CHANCE = 0.5   # per _NPC_MOVE_MS tick, chance a "wander" NPC steps
_NPC_WANDER_DIRECTIONS = ('N', 'S', 'E', 'W')

# Cardinal direction -> (row, col) delta. Shared by NPC wander stepping and
# warp landing-spot offset (see _swap_map) — same mapping engine.player uses
# internally (engine.player._DIR_DELTA), just not imported since that one's
# private to its module.
_DIR_DELTA = {'N': (-1, 0), 'S': (1, 0), 'E': (0, 1), 'W': (0, -1)}


@dataclass
class NPC:
    """A roaming/static character built from a map's `type == "npc"` objects
    merged with npcs_<map>.yaml (see MapObject, which this is built from in
    OverworldScene.__init__). `type` is always "npc" — it exists only so
    Player.adjacent_interactable()/handle_a_button can branch on it the same
    way they do for MapObject, without needing to know NPC is a distinct
    class. `dialogue` works exactly like a sign's: face the NPC and press A
    to open it — see handle_a_button."""
    index:      int                 # stable identity (Tiled object id) for tweens
    name:       str
    row:        int
    col:        int
    npc_sprite: str | None          # e.g. "npc01" -- key into _NPC_FRAME_NAMES
    behavior:   str = "static"      # "static" | "wander"
    type:       str = "npc"
    dialogue:   list[str] = field(default_factory=list)


# ── Scene ────────────────────────────────────────────────────────────────────

class OverworldScene:
    """A single walkable screen: tile grid, player, roaming NPCs.

    Implements the handle_event/update/draw protocol expected by
    engine.renderer.run(). Must be constructed after pygame.display.set_mode()
    so tileset/sprite loading can call .convert_alpha().
    """

    def __init__(self, map_path: Path = _HUB_MAP):
        self.tile_px = cfg.tile_px
        self.player_move_ms = cfg.player_move_ms

        self.sprites = load_sprites(_SPRITES_PNG, _SPRITES_TXT, self.tile_px)
        self.menu_assets = load_menu_assets(_MENU_DIR)
        self.dialogue_font = pygame.font.Font(str(_DIALOGUE_FONT_PATH), _DIALOGUE_FONT_PT)
        print(f'  {len(self.sprites)} sprites', flush=True)

        self.menu = StartMenu()
        self.save_menu = SaveMenu()   # confirm-save overlay, opened from SAVE on self.menu
        self.dialogue = DialogueBox()   # opened by facing a sign and pressing A

        self._npc_rng = random.Random(0x4875624E5043)
        self.player = Player.default()

        # No OS key repeat — held-key repeat is driven by engine.input.HeldDirectionInput
        # so that only one cardinal direction is ever stepped per tick, never two at
        # once. Two independent per-key OS repeats firing in the same frame is what
        # produced diagonal-looking moves before.
        self._input = HeldDirectionInput(repeat_ms=self.player_move_ms)

        # A/B held state, tracked independently of HeldDirectionInput — used
        # only to type the dialogue box in faster, not for repeat-stepping.
        self._held_buttons: set[str] = set()

        # Warp transition state — see _begin_warp/_tick_transition/_swap_map.
        # None means no warp is in progress; "out" while fading to black,
        # "in" while fading back in on the destination map (the swap itself
        # happens the instant fade-out completes, fully hidden behind solid
        # black). The overlay surface is allocated once and reused every
        # frame — its size (the native render target) never changes.
        self._transition_phase: str | None = None
        self._transition_elapsed = 0
        self._pending_warp: 'MapObject | None' = None
        self._fade_overlay = pygame.Surface((cfg.view_cols * self.tile_px, cfg.view_rows * self.tile_px))
        self._fade_overlay.fill((0, 0, 0))

        self._load_map(map_path)

    def _load_map(self, map_path: Path, tmap: 'TiledMap | None' = None,
                   spawn: tuple[int, int] | None = None, facing: str | None = None) -> None:
        """Load (or, mid-game via a warp, reload) everything specific to one
        map: the Tiled map + tileset, passability, objects/warps/NPCs, and
        the player's spawn tile. Pygame resources that don't vary by map
        (sprites, menu/dialogue assets) are loaded once in __init__ instead
        and untouched here.

        `tmap`, if given, is a TiledMap the caller already parsed — _swap_map
        has to load the destination map anyway to find the target warp
        object, so this avoids parsing the same JSON twice. `spawn`/`facing`
        override the map's default spawn point (tiled_spawn_point) — used
        when arriving via a warp instead of booting into the map fresh.
        """
        print(f'Loading map {map_path.stem}...', flush=True)
        self.map_path = map_path
        self.tmap = tmap if tmap is not None else load_tiled_map(map_path)
        self.tile_surfaces = load_tileset_by_gid(
            _ASSETS / 'tiles' / self.tmap.tileset.image,
            self.tmap.tileset.columns,
            self.tile_px,
        )
        self.passable = tiled_passable_grid(self.tmap)
        print(f'  {len(self.tile_surfaces)} tiles', flush=True)

        self.objects = [o for o in self.tmap.objects if o.type not in ("npc", "warp")]   # containers/signs
        self.warps = [o for o in self.tmap.objects if o.type == "warp"]
        npc_map_objects = [o for o in self.tmap.objects if o.type == "npc"]
        self.npcs = [
            # NPC.row/col is the tile the NPC's sprite mostly covers, not
            # the tile its top-left corner floors into (o.row/o.col) — a
            # sub-tile nudge (see row_exact/col_exact) can leave the
            # floored tile holding only a sliver of the sprite while the
            # tile next to it holds nearly all of it. Rounding instead of
            # flooring picks whichever tile has the majority, which is what
            # passability (_passable_with_npcs) and interaction
            # (Player.adjacent_interactable) both key off of, so both line
            # up with where the NPC visually stands.
            NPC(index=o.id, name=o.name, row=round(o.row_exact), col=round(o.col_exact),
                npc_sprite=o.sprite, behavior=o.behavior or "static",
                dialogue=o.dialogue)
            for o in npc_map_objects
        ]

        self.player.row, self.player.col = spawn if spawn is not None else tiled_spawn_point(self.tmap)
        if facing is not None:
            self.player.facing = facing

        self._anim_frame = 0       # 0 or 1, shared NPC animation tick
        self._anim_timer = 0       # ms accumulator for NPC anim flip
        self._npc_move_timer = 0   # ms accumulator for NPC position update
        self._player_anim = 0      # 0 or 1, walk-cycle frame, driven by tween progress

        # Visual (sub-tile) position tracking — engine stays tile-discrete; only the
        # renderer interpolates, so movement flows smoothly between tiles instead of
        # snapping. _player_tween_elapsed >= player_move_ms means "at rest".
        start = (float(self.player.row), float(self.player.col))
        self._player_tween_src = start
        self._player_tween_dst = start
        self._player_tween_elapsed = self.player_move_ms
        self._player_vis = start

        # Same idea for NPCs, keyed by their stable `index` — except the
        # *initial* rest position is seeded from the object's exact
        # (un-floored) placement rather than its floored row/col, so an NPC
        # renders exactly where it was placed in Tiled instead of snapping
        # to the tile grid. NPC.row/col (tile-discrete ints) still drive
        # every gameplay concern — wander stepping, player/NPC collision —
        # unaffected by this; it's purely where the sprite starts drawing.
        # A static NPC never moves, so it stays at this exact spot forever.
        # A wander NPC's first step tweens it from here onto the tile grid
        # (dst is always a plain int tile from then on), same as any step.
        self._npc_tweens = {
            npc.index: {
                'src':     (o.row_exact, o.col_exact),
                'dst':     (o.row_exact, o.col_exact),
                'elapsed': _NPC_MOVE_MS,
            }
            for npc, o in zip(self.npcs, npc_map_objects)
        }

        print(f'Overworld: {self.tmap.height}r × {self.tmap.width}c  |  '
              f'player spawn ({self.player.row},{self.player.col})  |  '
              f'{len(self.npcs)} NPCs  |  {len(self.warps)} warps', flush=True)

    # ── input ────────────────────────────────────────────────────────────────

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYDOWN:
            direction = _DIR_KEY.get(event.key)
            if direction:
                if self.player.state == PlayerState.IN_MENU:
                    handle_menu_direction(direction, self.menu, self.save_menu)
                else:
                    # Turn-in-place (a tap that doesn't step, just faces the
                    # new way) only applies from a dead stop. Mid-glide, a
                    # direction change — including a full reversal — is just
                    # queued like any other held-direction press: it takes
                    # over on the *next* step, which lands exactly when the
                    # current tween finishes (repeat_ms == player_move_ms),
                    # so there's no added delay and no fractional leftover
                    # on the old axis to blend into the new one. Applying
                    # the turn-in-place/freeze treatment mid-glide was the
                    # bug: freezing at a fractional position on one axis and
                    # then stepping on the *other* axis interpolated both at
                    # once, i.e. visible diagonal movement — and the extra
                    # forced pause before that frozen step could fire read
                    # as the character slowing down every time it turned.
                    at_rest = self._player_tween_elapsed >= self.player_move_ms
                    facing = self.player.facing if at_rest else None
                    if self._input.press(direction, facing):
                        self.player.facing = direction

            button = _BUTTON_KEY.get(event.key)
            if button:
                self._held_buttons.add(button)
            if button == 'START':
                handle_start_button(self.player, self.menu, self.save_menu)
            elif button == 'B':
                handle_b_button(self.player, self.menu, self.save_menu)
            elif button == 'A':
                # Menu confirm (stub — sub-screens not built yet) / dialogue
                # advance-close (no-op while still typing in) / open a
                # dialogue by facing an interactable sign.
                handle_a_button(self.player, self.menu, self.save_menu,
                                 self.dialogue, self.npcs, self.objects,
                                 wrap_pages=self._wrap_dialogue_pages)
        elif event.type == pygame.KEYUP:
            direction = _DIR_KEY.get(event.key)
            if direction:
                self._input.release(direction)
            button = _BUTTON_KEY.get(event.key)
            if button:
                self._held_buttons.discard(button)

    # ── update ───────────────────────────────────────────────────────────────

    def update(self, dt_ms: int) -> None:
        if self._transition_phase is not None:
            # A warp is fading out/holding/fading in — full gameplay pause,
            # same idea as dialogue/menu below, just driven by elapsed time
            # instead of input.
            self._tick_transition(dt_ms)
            return

        if self.dialogue.is_open:
            # Holding A or B types the current page in faster; still a full
            # gameplay pause otherwise — no movement/anim ticks while open.
            fast = bool(self._held_buttons & {'A', 'B'})
            ms_per_char = cfg.dialogue_char_fast_ms if fast else cfg.dialogue_char_ms
            self.dialogue.tick(dt_ms, ms_per_char)
            return

        if self.menu.is_open:
            return   # start menu is a full pause — no gameplay ticks while open

        self._anim_timer += dt_ms
        if self._anim_timer >= _NPC_ANIM_MS:
            self._anim_frame ^= 1
            self._anim_timer -= _NPC_ANIM_MS

        self._npc_move_timer += dt_ms
        if self._npc_move_timer >= _NPC_MOVE_MS:
            self._npc_move_timer -= _NPC_MOVE_MS
            self._update_npc_wander()

        direction = self._input.tick(dt_ms)
        if direction:
            self._step_player(direction)

        self._player_tween_elapsed = min(self._player_tween_elapsed + dt_ms, self.player_move_ms)
        t = self._player_tween_elapsed / self.player_move_ms
        self._player_vis = (
            _lerp(self._player_tween_src[0], self._player_tween_dst[0], t),
            _lerp(self._player_tween_src[1], self._player_tween_dst[1], t),
        )
        self._player_anim = 0 if t < 0.5 else 1   # walk-cycle contact frame mid-step

        for tw in self._npc_tweens.values():
            if tw['elapsed'] < _NPC_MOVE_MS:
                tw['elapsed'] = min(tw['elapsed'] + dt_ms, _NPC_MOVE_MS)

    def _step_player(self, direction: str) -> None:
        if self.player.try_move(direction, self._passable_with_npcs()):
            # Rebase off wherever the sprite visually is right now (may be
            # mid-glide from the previous step) so repeated steps flow
            # continuously instead of stuttering.
            self._player_tween_src = self._player_vis
            self._player_tween_dst = (float(self.player.row), float(self.player.col))
            self._player_tween_elapsed = 0

            # Warps auto-trigger the instant a step lands on their tile —
            # no A press. Spawning onto a tile (see _load_map's spawn
            # param) never goes through try_move, so landing on the paired
            # warp on the other side can't immediately bounce back here.
            warp = self._warp_at(self.player.row, self.player.col)
            if warp is not None:
                self._begin_warp(warp)

    def _warp_at(self, row: int, col: int) -> 'MapObject | None':
        for warp in self.warps:
            if warp.row == row and warp.col == col:
                return warp
        return None

    def _begin_warp(self, warp: 'MapObject') -> None:
        """Start fading to black; the actual map swap happens once the
        fade-out completes (see _tick_transition/_swap_map) so it's fully
        hidden behind solid black instead of popping mid-screen."""
        if not warp.destination_map or not warp.destination_warp:
            print(f'Warp {warp.name!r} (id {warp.id}) has no destination configured '
                  f'-- ignoring', flush=True)
            return
        self._transition_phase = 'out'
        self._transition_elapsed = 0
        self._pending_warp = warp

    def _tick_transition(self, dt_ms: int) -> None:
        self._transition_elapsed += dt_ms
        if self._transition_phase == 'out':
            if self._transition_elapsed >= cfg.warp_fade_out_ms:
                self._swap_map(self._pending_warp)
                self._pending_warp = None
                self._transition_phase = 'in'
                self._transition_elapsed = 0
        elif self._transition_phase == 'in':
            if self._transition_elapsed >= cfg.warp_fade_in_ms:
                self._transition_phase = None

    def _swap_map(self, warp: 'MapObject') -> None:
        """Load warp.destination_map and land the player on whichever of
        its warp objects is named warp.destination_warp. The name lookup is
        scoped to that one destination map's object layer only — see the
        warp docs in data/maps/populate_yamls.py for why that's enough."""
        dest_path = _MAPS_DIR / warp.destination_map / f'{warp.destination_map}.json'
        dest_tmap = load_tiled_map(dest_path)
        target = next(
            (o for o in dest_tmap.objects if o.type == "warp" and o.name == warp.destination_warp),
            None,
        )
        if target is None:
            raise ValueError(
                f'Warp {warp.name!r} points to destination_warp={warp.destination_warp!r} '
                f'in map {warp.destination_map!r}, but no warp with that name exists there'
            )
        facing = target.facing or 'S'
        dr, dc = _DIR_DELTA[facing]
        distance = target.distance or 0
        spawn = (target.row + dr * distance, target.col + dc * distance)
        self._load_map(dest_path, tmap=dest_tmap, spawn=spawn, facing=facing)

    def _passable_with_npcs(self) -> list[list[bool]]:
        """The static walkable grid with every NPC's current tile marked
        impassable, so the player can't walk through them. Rebuilt fresh on
        each attempted step since NPCs move — engine.player stays headless
        and ignorant of NPCs entirely; it just sees a plain grid."""
        grid = [row[:] for row in self.passable]
        for npc in self.npcs:
            grid[npc.row][npc.col] = False
        return grid

    def _tile_occupied(self, row: int, col: int, exclude_npc: 'NPC | None' = None) -> bool:
        """True if the player or another NPC currently stands on (row, col).
        Used to keep wander NPCs from stepping onto the player or stacking
        on each other."""
        if self.player.row == row and self.player.col == col:
            return True
        for other in self.npcs:
            if other is not exclude_npc and other.row == row and other.col == col:
                return True
        return False

    def _update_npc_wander(self) -> None:
        """One decision tick for every "wander" NPC: a coin flip decides
        whether it steps this tick, then a random cardinal direction is
        tried against the walkable grid and current occupancy — same rules
        as the player, just without input driving it."""
        for npc in self.npcs:
            if npc.behavior != 'wander':
                continue
            if self._npc_rng.random() >= _NPC_WANDER_MOVE_CHANCE:
                continue
            direction = self._npc_rng.choice(_NPC_WANDER_DIRECTIONS)
            dr, dc = _DIR_DELTA[direction]
            new_row, new_col = npc.row + dr, npc.col + dc
            if not (0 <= new_row < self.tmap.height and 0 <= new_col < self.tmap.width):
                continue
            if not self.passable[new_row][new_col]:
                continue
            if self._tile_occupied(new_row, new_col, exclude_npc=npc):
                continue

            vis = self._npc_visual_pos(npc)
            npc.row, npc.col = new_row, new_col
            tw = self._npc_tweens[npc.index]
            tw['src'] = vis
            tw['dst'] = (float(new_row), float(new_col))
            tw['elapsed'] = 0

    def _npc_visual_pos(self, npc) -> tuple[float, float]:
        tw = self._npc_tweens.get(npc.index)
        if tw is None:
            return float(npc.row), float(npc.col)
        t = min(tw['elapsed'] / _NPC_MOVE_MS, 1.0)
        return _lerp(tw['src'][0], tw['dst'][0], t), _lerp(tw['src'][1], tw['dst'][1], t)

    def _wrap_dialogue_pages(self, raw_pages: list[str]) -> list[str]:
        """Expand each hand-authored YAML page into one or more on-screen
        screens sized to the dialogue box's text area. Passed into
        engine.input.handle_a_button so it stays pygame-free."""
        screens: list[str] = []
        for page in raw_pages:
            screens.extend(_wrap_text_to_screens(
                self.dialogue_font, page, _DIALOGUE_TEXT_W, _DIALOGUE_TEXT_H))
        return screens

    # ── camera ───────────────────────────────────────────────────────────────

    def _camera_offset_px(self) -> tuple[int, int]:
        """Top-left camera position in pixels: centered on the player's visual
        (tweened) position, clamped so the view never scrolls past a map edge.
        When the map is smaller than the viewport along an axis, there's no
        room to scroll at all, so that axis centers the map instead (a
        negative offset that letterboxes the excess viewport)."""
        view_cols, view_rows = cfg.view_cols, cfg.view_rows
        player_row, player_col = self._player_vis

        cam_col = self._camera_axis(player_col, self.tmap.width, view_cols)
        cam_row = self._camera_axis(player_row, self.tmap.height, view_rows)

        return round(cam_col * self.tile_px), round(cam_row * self.tile_px)

    @staticmethod
    def _camera_axis(player_pos: float, map_len: int, view_len: int) -> float:
        if map_len <= view_len:
            return (map_len - view_len) / 2
        return min(max(player_pos - view_len / 2, 0), map_len - view_len)

    # ── draw ─────────────────────────────────────────────────────────────────

    def draw(self, surface: pygame.Surface) -> None:
        surface.fill((0, 0, 0))
        cam_x, cam_y = self._camera_offset_px()

        self._draw_map(surface, cam_x, cam_y, above=False)

        for obj in self.objects:
            obj_surf = get_tile_by_gid(self.tile_surfaces, self.tmap.firstgid, obj.gid)
            if obj_surf:
                surface.blit(obj_surf, (obj.col * self.tile_px - cam_x, obj.row * self.tile_px - cam_y))

        for npc in self.npcs:
            npc_surf = get_npc_frame(self.sprites, npc.npc_sprite, self._anim_frame)
            if npc_surf:
                vr, vc = self._npc_visual_pos(npc)
                surface.blit(npc_surf, (round(vc * self.tile_px) - cam_x, round(vr * self.tile_px) - cam_y))

        player_surf = get_party_frame(self.sprites, 'melvin', self.player.facing, self._player_anim)
        if player_surf:
            vr, vc = self._player_vis
            surface.blit(player_surf, (round(vc * self.tile_px) - cam_x, round(vr * self.tile_px) - cam_y))

        self._draw_map(surface, cam_x, cam_y, above=True)

        if self.menu.is_open:
            self._draw_start_menu(surface)

        if self.save_menu.is_open:
            self._draw_save_menu(surface)

        if self.dialogue.is_open:
            self._draw_dialogue_box(surface)

        if self._transition_phase is not None:
            self._draw_transition(surface)

    def _draw_transition(self, surface: pygame.Surface) -> None:
        """Solid black overlay on top of everything else, alpha ramping
        0->255 during fade-out and 255->0 during fade-in (see
        _tick_transition). The scene underneath is already the destination
        map by the time fade-in starts — the swap happens the instant
        fade-out completes — so this is the only piece that needs to know
        which phase is active."""
        if self._transition_phase == 'out':
            t = min(self._transition_elapsed / cfg.warp_fade_out_ms, 1.0)
            alpha = round(255 * t)
        else:
            t = min(self._transition_elapsed / cfg.warp_fade_in_ms, 1.0)
            alpha = round(255 * (1 - t))
        self._fade_overlay.set_alpha(alpha)
        surface.blit(self._fade_overlay, (0, 0))

    def _draw_start_menu(self, surface: pygame.Surface) -> None:
        """Overlay: bg, each option row (highlighted one shifted right), cursor."""
        surface.blit(self.menu_assets['bg'], (0, 0))

        selected_label = self.menu.selected_option().lower()
        for label, y in _MENU_ROW_Y.items():
            x = _MENU_X + (_MENU_SELECT_SHIFT_PX if label == selected_label else 0)
            surface.blit(self.menu_assets[label], (x, y))

        cursor_y = _MENU_ROW_Y[selected_label]
        surface.blit(self.menu_assets['cursor'], (_MENU_X, cursor_y))

    def _draw_save_menu(self, surface: pygame.Surface) -> None:
        """Save-confirm overlay on top of the start menu: static bg, cursor
        at a fixed spot per option (YES/NO) — nothing else moves."""
        surface.blit(self.menu_assets['save_confirm_bg'], (0, 0))
        cursor_x = _SAVE_MENU_CURSOR_X[self.save_menu.selected_option()]
        surface.blit(self.menu_assets['save_confirm_cursor'], (cursor_x, _SAVE_MENU_CURSOR_Y))

    def _player_screen_row(self) -> float:
        """Player sprite's current top-edge y position on screen, in pixels."""
        _, cam_y = self._camera_offset_px()
        return self._player_vis[0] * self.tile_px - cam_y

    def _draw_dialogue_box(self, surface: pygame.Surface) -> None:
        """Bg (docked to the screen's bottom edge, 16px of the taller source
        image cropped off the top — see _DIALOGUE_TEXT_* comment), then the
        current screen's typed-in text.

        Docks to the top instead — image vertically flipped, text rect
        mirrored to match — when the player is in the lower half of the
        screen. That happens near a map edge, where the camera clamps
        instead of re-centering and the player can end up low enough that a
        bottom-docked box would sit right on top of them.
        """
        view_h = surface.get_height()
        mirrored = self._player_screen_row() > view_h / 2

        box = self.menu_assets['dialogue_box_flipped' if mirrored else 'dialogue_box']
        box_h = box.get_height()
        blit_y = 0 if mirrored else view_h - box_h
        surface.blit(box, (0, blit_y))

        text_top_local = (box_h - _DIALOGUE_TEXT_BOTTOM) if mirrored else _DIALOGUE_TEXT_TOP
        text_y = blit_y + text_top_local

        screen_text = self.dialogue.visible_text()
        line_h = self.dialogue_font.get_linesize()
        for i, line in enumerate(screen_text.split("\n")):
            line_surf = self.dialogue_font.render(line, False, _DIALOGUE_TEXT_COLOR)
            surface.blit(line_surf, (_DIALOGUE_TEXT_LEFT, text_y + i * line_h))

    def _draw_map(self, surface: pygame.Surface, cam_x: int, cam_y: int, above: bool) -> None:
        """Blit tile layers, bottom to top, offset by the camera.

        The first tile layer (the ground) draws below the player/NPCs/objects;
        every tile layer after it draws above them, in the same order Tiled
        lists them, so sprites can pass behind tall map features (roofs, tree
        canopies, ...) no matter how many overlay layers there are. See
        TiledLayer.above / load_tiled_map.
        """
        for layer in self.tmap.layers:
            if layer.above != above:
                continue
            for r, row in enumerate(layer.grid):
                y = r * self.tile_px - cam_y
                for c, gid in enumerate(row):
                    surf = get_tile_by_gid(self.tile_surfaces, self.tmap.firstgid, gid)
                    if surf:
                        surface.blit(surf, (c * self.tile_px - cam_x, y))
