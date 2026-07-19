"""THE graphical renderer — the single script that owns everything on-screen.

This is one file on purpose: the generic pygame app loop (window/clock/event
dispatch), Tiled JSON map + tileset loading, sprite-sheet slicing, the camera,
and the OverworldScene handle_event/update/draw loop all live here together.
Nothing about "reading the map," "drawing the map," or "running the loop" is
split into a separate module — that split was tried once and rejected.

Player movement *rules* stay in engine/player.py (headless: takes a plain
passable: list[list[bool]] grid, no knowledge of Tiled/GIDs). Held-key
direction resolution stays in engine/input.py (pure decision logic). Enemy
definitions, runtime state, and movement/collision stay in engine/enemy.py
(same split — headless, no Tiled/pygame knowledge). All three are imported
here, not duplicated.

Entry point: engine.renderer.run(OverworldScene, ...) — see maptest.py.

NPC objects (type == "npc" in a map's object layer) render and move per their
authored `behavior` — "static" idles in place, "wander" roams walkable tiles
— and are interactable exactly like signs: face one and press A to open its
`dialogue` (see MapObject/NPC), read via engine.dialogue. No npc-type
objects are placed on hub_fronthouse.json yet, though — its object layer
only has container/sign objects so far.

Enemy objects (type == "enemy", hardcoded placement, or "spawner",
rolled once per map load) reference data/enemy/enemies.yaml (see
engine.enemy) and become live Enemy instances with continuous, sub-tile
movement — not tile-discrete like NPC (Player moves this way too now, see
engine/player.py and engine/movement.py). They never block or are blocked
by the player; not battle, just placement/sprites/movement — see
engine.enemy.update_enemy and OverworldScene._update_enemies.

Warp objects (type == "warp") are invisible, one-way, one-tile trigger zones
— unlike signs/NPCs there's no A-press, stepping onto one is enough. Each
warp names its destination as a (destination_map, destination_warp) pair —
the other map's folder/stem name, plus the *name* of the warp object on that
map to land on — so warp names only need to be unique within one map's
object layer, never globally (see data/maps/populate_yamls.py). Triggering
one pauses gameplay and fades to black (cfg.warp_fade_out_ms), swaps the
loaded map underneath the fully-black frame, then fades back in
(cfg.warp_fade_in_ms) on the new map — see OverworldScene._update_player_movement,
_begin_warp, _tick_transition, _swap_map, _load_map.

Controls:
  Arrow keys  — move player (MELVIN) / move start-menu cursor / move
                save-confirm cursor / on the inventory screen: E-W switches
                category (wraps), N-S scrolls the list (clamps) / on the
                settings screen: N-S switches row (wraps), E-W changes the
                highlighted row's value (wraps)
  Enter       — START: open/close the start menu (no-op while save-confirm,
                inventory, or settings screen is open)
  X           — A: confirm start-menu selection (SAVE opens the save-confirm
                overlay, INVENTORY opens the inventory screen, SETTINGS
                opens the settings screen; PARTY still stubbed) / confirm
                YES-NO on the save-confirm overlay (YES is stubbed — see
                engine.menu.SaveMenu.confirm) / no-op on the inventory and
                settings screens (inventory is view-only; settings applies
                changes immediately off E/W) / advance or close an open
                dialogue box / open one by facing a sign or NPC
  Z           — B: close whichever menu is on top (save-confirm, inventory,
                or settings, then the start menu)
  Escape / Q  — quit
"""
import json
import math
import random
from dataclasses import dataclass, field
from pathlib import Path

import numpy as np
import pygame
import yaml

from engine.battle import BattleState, Fighter, load_fighter
from engine.config import cfg
from engine.dialogue import DialogueBox
from engine.enemy import (
    Enemy,
    check_overlap,
    load_enemy_defs,
    resolve_level,
    resolve_spawn,
    update_enemy,
)
from engine.input import (
    HeldDirectionInput,
    handle_a_button,
    handle_b_button,
    handle_menu_direction,
    handle_start_button,
)
from engine.game_state import GameState, persistent_id
from engine.inventory import CATEGORY_LABELS, Inventory, ItemDef, load_item_defs
from engine.menu import InventoryMenu, SaveMenu, SettingsMenu, StartMenu
from engine.npc import DialogueVariant, load_npc_defs, load_npc_sprite_specs, parse_dialogue
from engine.player import Player, PlayerState
from procgen.visualizer import EFFECTS

_REPO_ROOT   = Path(__file__).parent.parent
_ASSETS      = _REPO_ROOT / 'assets'
_MAPS_DIR    = _REPO_ROOT / 'data' / 'maps'
_HUB_MAP     = _MAPS_DIR / 'hub_fronthouse' / 'hub_fronthouse.json'
_SPRITES_PNG = _ASSETS / 'sprites' / 'party_sprites.png'
_SPRITES_TXT = _ASSETS / 'sprites' / 'partysprites.txt'
_ENEMY_SPRITES_DIR = _ASSETS / 'sprites' / 'enemies'
_NPC_SPRITES_DIR = _ASSETS / 'sprites' / 'npcs'
_BATTLE_ART_DIR = _ASSETS / 'tiles' / 'enemies'
_PARTY_DIR   = _REPO_ROOT / 'data' / 'party'
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

# Inventory-screen layout. No art assets exist yet for this alpha pass — the
# whole screen is drawn from dialogue_font + plain shapes, not menu_assets
# images like the start/save overlays above. Native render target is
# view_cols*tile_px x view_rows*tile_px (256x224 at the default config).
_INV_MARGIN       = 8
_INV_HEADER_Y     = _INV_MARGIN
_INV_LIST_TOP_Y   = 24
_INV_LIST_LEFT_X  = _INV_MARGIN
_INV_DIVIDER_X    = 124   # column of "|" glyphs separating list from detail pane
_INV_DETAIL_LEFT_X = _INV_DIVIDER_X + 12
_INV_ICON_SIZE    = 32    # placeholder box — no icon art yet
_INV_TEXT_COLOR   = (255, 255, 255)
_INV_DIM_COLOR    = (110, 110, 110)   # unselected list rows / divider / empty-category text

# Settings-screen layout. Same "no art yet, plain dialogue_font on black"
# approach as the inventory screen above — two rows (SCALE, DISPLAY MODE),
# each drawn as a "< value >" pair same as the inventory header's category
# picker (_draw_inventory_menu's `header`).
_SET_MARGIN     = 8
_SET_TITLE_Y    = _SET_MARGIN
_SET_ROWS_TOP_Y = 24
_SET_LABEL_X    = _SET_MARGIN
_SET_VALUE_X    = 120
_SET_ROW_LABELS = ("SCALE", "DISPLAY MODE")
_SET_TEXT_COLOR = (255, 255, 255)
_SET_DIM_COLOR  = (110, 110, 110)   # unselected row label

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


def _fit_text(font: pygame.font.Font, text: str, max_w: int) -> str:
    """Truncate `text` with a trailing "..." if it's wider than max_w px —
    used for the inventory list column, where item names are free-authored
    text with no length limit and the column is a fixed pixel width."""
    if font.size(text)[0] <= max_w:
        return text
    ellipsis = "..."
    while text and font.size(text + ellipsis)[0] > max_w:
        text = text[:-1]
    return (text + ellipsis) if text else ellipsis


# ── Generic app loop ─────────────────────────────────────────────────────────
#
# Owns the window, the clock, and event dispatch. Knows nothing about maps,
# tiles, or NPCs — any scene that implements handle_event/update/draw can run
# here. Title screens, the overworld, and battle all drive through this same
# loop; nothing about it is specific to any one scene.

def _set_display_mode(view_w: int, view_h: int, scale: int, fullscreen: bool) -> tuple[pygame.Surface, int, int]:
    """(Re)open the OS window at `scale`x, windowed or fullscreen.

    Fullscreen adds pygame.SCALED on top of pygame.FULLSCREEN: SDL then
    letterbox-scales the requested (view*scale) resolution up to whatever
    the actual display supports instead of trying to *set* the display to
    that exact (likely unsupported) mode, which is what plain
    pygame.FULLSCREEN would attempt. Windowed mode skips SCALED so the
    window is exactly view*scale px, matching the 1x/2x/3x choice exactly.
    """
    win_w, win_h = view_w * scale, view_h * scale
    flags = (pygame.FULLSCREEN | pygame.SCALED) if fullscreen else 0
    window = pygame.display.set_mode((win_w, win_h), flags)
    return window, win_w, win_h


def run(scene_factory, view_size: tuple[int, int], scale: int, title: str) -> None:
    """Open the window, build the scene, then run it until closed or Escape/Q.

    `scene_factory` is called with no arguments after pygame.display.set_mode()
    so scene construction (tileset/sprite loading, etc.) can safely call
    .convert_alpha().

    The constructed scene must implement:
        handle_event(event: pygame.event.Event) -> None
        update(dt_ms: int) -> None
        draw(surface: pygame.Surface) -> None

    A scene may also expose `display_scale: int` / `fullscreen: bool`
    attributes (OverworldScene proxies these off its SettingsMenu) — after
    every update() this loop compares them against the window's current
    scale/fullscreen and, on a change (made via the in-game settings
    screen), reopens the window at the new mode. Scenes that don't expose
    them just never trigger a change, so this stays optional/generic rather
    than specific to any one scene.
    """
    pygame.init()
    view_w, view_h = view_size
    current_scale = scale
    current_fullscreen = False
    window, win_w, win_h = _set_display_mode(view_w, view_h, current_scale, current_fullscreen)
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

        new_scale = getattr(scene, 'display_scale', current_scale)
        new_fullscreen = getattr(scene, 'fullscreen', current_fullscreen)
        if new_scale != current_scale or new_fullscreen != current_fullscreen:
            current_scale, current_fullscreen = new_scale, new_fullscreen
            window, win_w, win_h = _set_display_mode(view_w, view_h, current_scale, current_fullscreen)

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


# ── Enemy sprite sheet loading ───────────────────────────────────────────────
#
# Unlike the party sheet (one shared indexed sheet + partysprites.txt), each
# enemy gets its own small strip file under assets/sprites/enemies/ -- a
# single horizontal row of tile_px-tall frames (frame count = width / tile_px;
# 2 today, a south-facing walk cycle). Keyed by filename stem, matching the
# `sprite` field in data/enemy/enemies.yaml.

def load_enemy_sprites(sprites_dir: Path, tile_px: int = 16) -> dict[str, list[pygame.Surface]]:
    """Slice every PNG in `sprites_dir` into a list of tile_px-wide animation
    frames, keyed by filename stem. Frame height is the sheet's own height
    (always a multiple of tile_px), not hardcoded to tile_px -- a sheet
    taller than one tile is a normal tile_px x tile_px frame (bottom rows)
    with extra rows stacked above it (e.g. a hat/head overhang on an NPC).
    See _draw_entities, which anchors the bottom tile_px rows to the tile
    and lets any extra height extend upward. Call after
    pygame.display.set_mode()."""
    frames: dict[str, list[pygame.Surface]] = {}
    for path in sorted(sprites_dir.glob('*.png')):
        sheet = pygame.image.load(str(path)).convert_alpha()
        width, height = sheet.get_size()
        count = width // tile_px
        frames[path.stem] = [
            sheet.subsurface(pygame.Rect(i * tile_px, 0, tile_px, height)).copy()
            for i in range(count)
        ]
    return frames


def get_enemy_frame(enemy_sprites: dict[str, list[pygame.Surface]],
                    sprite: str, anim_frame: int) -> pygame.Surface | None:
    """Surface for an enemy's current animation frame."""
    frames = enemy_sprites.get(sprite)
    if not frames:
        return None
    return frames[anim_frame % len(frames)]


# ── NPC sprite sheet loading ─────────────────────────────────────────────────
#
# Same shape as the enemy loader above -- one strip PNG per sprite under
# assets/sprites/npcs/, keyed by filename stem (data/npcs/npc.yaml's
# NpcSpriteSpec.file). NOT the old shared party_sprites.png +
# partysprites.txt mechanism, which is deprecated for NPCs. A strip's frame
# count (width / tile_px) decides how get_npc_frame animates it: 2 frames is
# a facing-less south-only idle loop, 8 is a full walk cycle in the party
# sheet's own S1,S2,N1,N2,W1,W2,E1,E2 order, and does respond to facing.
#
# A strip's height, independently of frame count, may be any multiple of
# tile_px -- e.g. 128x32 is still the 8-frame walk cycle above, just with
# each frame 32px tall instead of 16. The bottom tile_px rows are the
# ordinary body, treated exactly like a 16-tall sprite; anything above that
# is drawn as a plain overhang (a hat, a tall hood, ...) with no separate
# animation or logic of its own. load_enemy_sprites slices the full height;
# _draw_entities anchors the bottom tile_px rows to the NPC's tile and lets
# the rest extend upward.
#
# Recoloring (see engine.npc's module docstring / NpcDef.colors /
# NpcSpriteSpec.colors): a sprite loads once here, unrecolored; per-NPC
# recolored copies are computed once at OverworldScene.__init__ time (see
# self.npc_frames_by_id), not per frame drawn -- recolor_surface below is
# only ever called there, never from draw().

load_npc_sprites = load_enemy_sprites   # identical mechanism, different directory


def _hex_to_rgb(hex_str: str) -> tuple[int, int, int]:
    return (int(hex_str[0:2], 16), int(hex_str[2:4], 16), int(hex_str[4:6], 16))


def recolor_surface(surf: pygame.Surface, from_colors: list[str], to_colors: list[str]) -> pygame.Surface:
    """Copy of `surf` with every pixel matching one of `from_colors` (hex,
    no '#') swapped to the position-matched color in `to_colors`; every
    other pixel -- including fully transparent ones -- passes through
    unchanged. Colors beyond the shorter of the two lists are left alone
    (a hand-authoring mismatch shouldn't crash map load, same fail-soft
    spirit as engine.enemy.resolve_spawn dropping half-filled candidates)."""
    out = surf.copy()
    rgb = pygame.surfarray.pixels3d(out)   # mutable view into `out`, released below
    for src_hex, dst_hex in zip(from_colors, to_colors):
        if src_hex.lower() == dst_hex.lower():
            continue
        src = _hex_to_rgb(src_hex)
        dst = _hex_to_rgb(dst_hex)
        mask = (rgb[:, :, 0] == src[0]) & (rgb[:, :, 1] == src[1]) & (rgb[:, :, 2] == src[2])
        rgb[mask] = dst
    del rgb   # unlock `out` before handing it back
    return out


def get_npc_frame(frames: list[pygame.Surface] | None, facing: str, anim_frame: int) -> pygame.Surface | None:
    """Surface for an NPC's current animation frame, given its already-
    resolved frame list -- see OverworldScene._draw_entities for how that
    list is picked (the recolored cache for an npc_id'd NPC, or the sprite's
    raw frames for an inline one with no npc_id). A 2-frame list ignores
    `facing` (south-only idle loop); an 8-frame one picks the S1,S2 / N1,N2
    / W1,W2 / E1,E2 pair matching `facing`, defaulting to S for an
    unrecognized value."""
    if not frames:
        return None
    if len(frames) <= 2:
        return frames[anim_frame % len(frames)]
    pair_index = {'S': 0, 'N': 1, 'W': 2, 'E': 3}.get(facing, 0)
    return frames[pair_index * 2 + anim_frame % 2]


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
    """One entry in a map's real, authored layer order (see load_tiled_map).

    `kind` is `"tile"` (a paintable GID grid, `grid` set) or `"objects"` (a
    marker for where the map's object layer — containers/signs/NPCs/enemies/
    warps/player — falls among the tile layers; `grid` is None). There's
    normally exactly one `"objects"` entry per map, matching Tiled's single
    Object Layer convention every map here already follows."""
    name:  str
    kind:  str                          # "tile" or "objects"
    grid:  list[list[int]] | None = None   # GIDs, row-major [row][col]; only set for kind == "tile"


@dataclass
class MapObject:
    """A container/sign/npc/warp placed on the map's Tiled object layer,
    merged with its hand-authored content from obj_<map>.yaml /
    npcs_<map>.yaml (see data/maps/populate_yamls.py). `dialogue` is a list
    of pages; `type == "sign"` opens it directly, `type == "container"`
    opens it via engine.input's container handling (dialogue plus an item
    grant — see `contents` below), and `type == "npc"` entries hand theirs
    off to NPC (see below, built from these in OverworldScene._load_map)
    which is interactable the same way. `sprite`/`behavior`/`npc_id` are
    only ever set for `type == "npc"` entries — `npc_id`, if set, is a key
    into engine.npc.load_npc_defs()'s result (data/npcs/npc.yaml); this
    object's own `sprite`/`behavior`/`dialogue` are then optional overrides
    on top of that shared definition — set, they win for this one
    placement, left blank (None / empty list) they fall back to the
    NpcDef's — resolved in OverworldScene._load_map, not here (this class
    just carries whatever was in the YAML, unmerged).
    `destination_map`/`destination_warp`/`facing`/`distance` are only ever
    set for `type == "warp"` entries — see
    OverworldScene._update_player_movement/_swap_map. `facing`/`distance` do double
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
    this field exists on them too but nothing reads it.

    `contents` is only ever set for `type == "container"` entries: a single
    item id from data/items/items.yaml, or None for a container that's pure
    flavor (dialogue only, no loot). See engine.input's container handling
    for how opening one grants the item; either way (loot or not), opening a
    container sets a flag keyed by engine.game_state.persistent_id(map_name,
    this object's name) — every container is single-use, one open. That same
    flag is what OverworldScene._draw_entities checks to stop drawing the
    container's `gid` once opened: paint whichever tile should show through
    once it's empty (a used-up trash can, an opened chest, ...) on the tile
    layer beneath the container object, and it's revealed for free the
    instant the flag's set — no separate "opened" sprite or state field.

    `enemy_id`/`level` are only ever set for `type == "enemy"` entries: a
    hardcoded placement, `enemy_id` a key into data/enemy/enemies.yaml,
    `level` an optional override of that entry's own level/level-range for
    this one placement. `enemies`/`spawn_chance`/`level` are only ever set
    for `type == "spawner"` entries: rolled once per map load (see
    OverworldScene._load_map) -- `spawn_chance` (0-1) is the overall gate,
    `enemies` a list of `{enemy_id, chance}` dicts weight-picking which one
    spawns once the gate passes (see engine.enemy.resolve_spawn), `level`
    again an optional override. Both types are placed and anchored exactly
    like `npc` objects (rectangle tool, no `gid`, top-left anchor) -- see
    engine.enemy for the runtime `Enemy` these become."""
    id:       int
    name:     str
    type:     str
    row:      int
    col:      int
    gid:      int
    row_exact: float = 0.0
    col_exact: float = 0.0
    rotation: float = 0.0   # Tiled object rotation, degrees clockwise about the object's x/y anchor
    dialogue: list[str] = field(default_factory=list)
    contents: str | None = None
    sprite:   str | None = None
    behavior: str | None = None
    npc_id:   str | None = None
    destination_map:  str | None = None
    destination_warp: str | None = None
    facing:   str | None = None
    distance: int | None = None
    enemy_id:     str | None = None
    level:        int | list[int] | None = None
    enemies:      list = field(default_factory=list)
    spawn_chance: float | None = None


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
                rotation  = obj.get("rotation", 0.0),
                dialogue  = data.get("dialogue", []),
                contents  = data.get("contents"),
                sprite    = data.get("sprite"),
                behavior  = data.get("behavior"),
                npc_id    = data.get("npc_id"),
                destination_map  = data.get("destination_map"),
                destination_warp = data.get("destination_warp"),
                facing    = data.get("facing"),
                distance  = data.get("distance"),
                enemy_id     = data.get("enemy_id"),
                level        = data.get("level"),
                enemies      = data.get("enemies", []),
                spawn_chance = data.get("spawn_chance"),
            ))
    return objects


# Tiled stores per-tile mirroring in the top 3 bits of a layer cell's GID
# (see https://doc.mapeditor.org/en/stable/reference/global-tile-ids/) --
# used by e.g. the "mirror" brush to reuse one drawn tile both ways instead
# of authoring two. Any code resolving a raw layer GID to an actual tile
# must strip these before using it as a tileset-local id, or a mirrored
# cell's GID (well over a billion) reads as "no such tile" -- see
# get_tile_by_gid/tiled_passable_grid.
_FLIP_H = 0x80000000
_FLIP_V = 0x40000000
_FLIP_D = 0x20000000
_FLIP_MASK = _FLIP_H | _FLIP_V | _FLIP_D


@dataclass
class TiledMap:
    width:      int                 # tiles
    height:     int                 # tiles
    tilewidth:  int
    tileheight: int
    tilesets:   list[tuple[int, TiledTileset]]   # (firstgid, tileset), ascending by firstgid
    layers:     list[TiledLayer]
    objects:    list[MapObject]


def _tileset_for_gid(tilesets: list[tuple[int, TiledTileset]], clean_gid: int) -> tuple[int, TiledTileset] | None:
    """Which of a map's (possibly several) tilesets a flip-stripped GID
    belongs to: the one with the highest firstgid <= clean_gid -- same rule
    Tiled itself uses to split one map's GID range across tilesets."""
    match = None
    for firstgid, tileset in tilesets:
        if firstgid > clean_gid:
            break
        match = (firstgid, tileset)
    return match


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
    """Load a Tiled JSON map and its sibling tileset JSON(s).

    A map may paint from more than one tileset (e.g. town.json draws its
    ground from tiles_town.json and its buildings from tiles_houses.json)
    -- every entry in raw["tilesets"] gets loaded, not just the first, or
    any GID from the ones after it resolves to nothing and draws as a
    missing/black tile.
    """
    raw = json.loads(path.read_text())

    tilesets = []
    for ts_entry in raw["tilesets"]:
        ts_stem = Path(ts_entry["source"]).stem
        tileset = _load_tiled_tileset(_ASSETS / "tiles" / f"{ts_stem}.json")
        tilesets.append((ts_entry["firstgid"], tileset))
    tilesets.sort(key=lambda pair: pair[0])

    width, height = raw["width"], raw["height"]
    # Real, authored order: walk raw["layers"] once and keep every tile layer
    # AND the object layer, in the exact sequence Tiled lists them. Where the
    # object layer (containers/signs/NPCs/enemies/warps/player) falls among
    # the tile layers determines what draws above/below it — a tile layer
    # before the object layer draws under the player/objects, one after draws
    # over them (roofs, canopies, ...). No hardcoded "first layer is special"
    # rule; see OverworldScene.draw()/_draw_tile_layer()/_draw_entities().
    layers = []
    for layer in raw["layers"]:
        if layer["type"] == "tilelayer":
            flat = layer["data"]
            grid = [flat[r * width:(r + 1) * width] for r in range(height)]
            layers.append(TiledLayer(name=layer["name"], kind="tile", grid=grid))
        elif layer["type"] == "objectgroup":
            layers.append(TiledLayer(name=layer["name"], kind="objects"))

    objects = load_map_objects(raw, raw["tilewidth"], path.parent, path.stem)

    return TiledMap(
        width      = width,
        height     = height,
        tilewidth  = raw["tilewidth"],
        tileheight = raw["tileheight"],
        tilesets   = tilesets,
        layers     = layers,
        objects    = objects,
    )


def tiled_passable_grid(tmap: TiledMap) -> list[list[bool]]:
    """Build a [row][col] -> bool grid from every layer's `walkable` property.

    A cell's walkability is decided by the topmost non-empty tile stacked
    there, not by ANDing every layer together -- a walkable tile painted on
    a higher layer (a rug, a road decal, ...) makes the cell walkable even
    if a lower layer underneath it is marked not walkable. `tmap.layers` is
    in Tiled's authored bottom-to-top draw order (see load_tiled_map), so
    walking it in order and overwriting the grid on every non-empty tile
    naturally leaves the topmost tile's value as the final one. GID 0
    (empty cell) never overwrites -- it shows through to whatever's below,
    same as during drawing.

    NOTE: "topmost tile wins" is a judgment call, not something confirmed
    against every authored map -- if a map ever wants a decorative
    walkable-tagged overlay (e.g. a shadow or overlay decal) to NOT make an
    otherwise-blocked tile walkable, this rule will need reverting or
    reworking (e.g. an explicit per-layer opt-in) rather than assuming
    topmost-always-wins is universally correct.
    """
    grid = [[True] * tmap.width for _ in range(tmap.height)]
    for layer in tmap.layers:
        if layer.kind != "tile":
            continue
        for r, row in enumerate(layer.grid):
            for c, gid in enumerate(row):
                if gid == 0:
                    continue
                clean_gid = gid & ~_FLIP_MASK   # mirroring doesn't change walkability
                resolved = _tileset_for_gid(tmap.tilesets, clean_gid)
                if resolved is None:
                    continue
                firstgid, tileset = resolved
                local_id = clean_gid - firstgid
                grid[r][c] = tileset.walkable.get(local_id, False)
    return grid


def tiled_spawn_point(tmap: TiledMap) -> tuple[int, int]:
    """Player spawn tile: a few rows up from the bottom edge, centered.

    No object layer with an authored spawn point exists yet.
    """
    bottom_row = tmap.height - 1
    spawn_row = max(bottom_row - 4, 0)
    return (spawn_row, tmap.width // 2)


def _slice_tileset_image(image_path: Path, columns: int, tile_px: int) -> list[pygame.Surface]:
    """Slice a tileset image into a flat list of surfaces, index == local tile id.

    Matches Tiled's own local-id numbering (row * columns + col).
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


def load_tile_surfaces(tmap: TiledMap, tile_px: int) -> dict[int, pygame.Surface]:
    """Slice every tileset a map draws from and combine them into one
    gid -> surface lookup, keyed by each tile's absolute (map-wide) gid
    rather than a tileset-local id -- so draw code doesn't need to know
    which of a map's (possibly several) tilesets a given gid came from."""
    surfaces: dict[int, pygame.Surface] = {}
    for firstgid, tileset in tmap.tilesets:
        local_surfaces = _slice_tileset_image(
            _ASSETS / 'tiles' / tileset.image, tileset.columns, tile_px)
        for local_id, surf in enumerate(local_surfaces):
            surfaces[firstgid + local_id] = surf
    return surfaces


def get_tile_by_gid(surfaces: dict[int, pygame.Surface], gid: int) -> pygame.Surface | None:
    """Resolve a raw Tiled layer GID to a surface. Returns None for GID 0
    (empty cell) or a gid with no matching tile in any of the map's
    tilesets.

    A gid's top 3 bits may carry Tiled's per-cell mirror flags (see
    _FLIP_MASK) rather than being part of the tile id -- those are stripped
    to look the base tile up, then the mirror is applied and the result
    cached back under the raw (flagged) gid so repeats are free. Flagged
    gids run well over a billion, so they can't collide with a real
    (unflagged) tile id in the same dict.
    """
    if gid == 0:
        return None
    cached = surfaces.get(gid)
    if cached is not None:
        return cached
    flip_h = bool(gid & _FLIP_H)
    flip_v = bool(gid & _FLIP_V)
    flip_d = bool(gid & _FLIP_D)
    if not (flip_h or flip_v or flip_d):
        return None   # unflagged gid, already missed the dict above -- no such tile
    base = surfaces.get(gid & ~_FLIP_MASK)
    if base is None:
        return None
    surf = base
    if flip_d:
        surf = pygame.transform.rotate(pygame.transform.flip(surf, True, False), 90)
    if flip_h:
        surf = pygame.transform.flip(surf, True, False)
    if flip_v:
        surf = pygame.transform.flip(surf, False, True)
    surfaces[gid] = surf
    return surf


def _rotate_about_point(surf: pygame.Surface, degrees_cw: float, pivot_local: tuple[float, float],
                         pivot_world: tuple[float, float]) -> tuple[pygame.Surface, tuple[float, float]]:
    """Rotate `surf` clockwise by `degrees_cw` (Tiled's object-rotation
    convention) about `pivot_local` -- a point in the surface's own
    coordinate space, e.g. (0, tile_px) for its bottom-left corner, which
    is where Tiled tile objects anchor.

    Returns the rotated surface and the top-left position to blit it at so
    that the pivot lands at `pivot_world`. pygame.transform.rotate always
    rotates about the surface's center and grows the surface to fit, so the
    offset from that new center back to the pivot has to be tracked by hand.
    """
    w, h = surf.get_size()
    center_x, center_y = w / 2, h / 2
    dx, dy = pivot_local[0] - center_x, pivot_local[1] - center_y
    # pygame's rotate() takes a counterclockwise angle -- negate to get Tiled's clockwise one.
    rad = math.radians(-degrees_cw)
    cos_a, sin_a = math.cos(rad), math.sin(rad)
    new_dx = dx * cos_a + dy * sin_a
    new_dy = -dx * sin_a + dy * cos_a
    rotated = pygame.transform.rotate(surf, -degrees_cw)
    rw, rh = rotated.get_size()
    pivot_in_rotated = (rw / 2 + new_dx, rh / 2 + new_dy)
    topleft = (pivot_world[0] - pivot_in_rotated[0], pivot_world[1] - pivot_in_rotated[1])
    return rotated, topleft


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
# authored content. NPC sprites load from their own strip PNGs under
# assets/sprites/npcs/ (see the "NPC sprite sheet loading" section above,
# and data/npcs/npc.yaml) — not the party sheet. A 2-frame strip has no
# facing (always the same idle pair); an 8-frame one does, same S1,S2,N1,N2,
# W1,W2,E1,E2 order the party sheet uses — see NPC.facing / get_npc_frame.

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
    OverworldScene._load_map). `sprite`/`behavior`/`facing`/`dialogue` are
    already fully resolved by the time an NPC exists — that MapObject's own
    fields win if set, else whatever its `npc_id` points at in
    engine.npc.load_npc_defs()'s result (data/npcs/npc.yaml), else the
    static "no def, nothing set" fallback (`behavior="static"`,
    `facing="S"`, empty dialogue) — see _load_map. `type` is always "npc" —
    it exists only so Player.adjacent_interactable()/handle_a_button can
    branch on it the same way they do for MapObject, without needing to
    know NPC is a distinct class. `dialogue` works exactly like a sign's:
    face the NPC and press A to open it — see handle_a_button.

    `npc_id` (distinct from `npc_sprite`) is only ever set when this NPC
    resolved against a real def — draw-time (OverworldScene._draw_entities)
    uses it to pick that def's precomputed *recolored* frame set
    (self.npc_frames_by_id) instead of the sprite's raw one; an inline NPC
    with no def (npc_id stays None) always draws its sprite unrecolored.

    `dialogue_variants` (engine.npc.DialogueVariant list) is the
    placement's own npcs_<map>.yaml `dialogue:` if it set one (parsed via
    engine.npc.parse_dialogue — a flat page list or a conditional variant
    list, same rule as the def), else the resolved def's own `dialogue`,
    else empty — same override-or-fall-back-to-def rule as sprite/behavior,
    see _load_map. engine.input.handle_a_button resolves this against live
    engine.game_state flags at interact time, not baked into a flat list
    here, since flags can change between one visit and the next — see
    engine.npc.resolve_dialogue.
    """
    index:      int                 # stable identity (Tiled object id) for tweens
    name:       str
    row:        int
    col:        int
    npc_sprite: str | None          # e.g. "npc01" -- key into engine.npc.load_npc_sprite_specs()'s result
    behavior:   str = "static"      # "static" | "wander"
    facing:     str = "S"           # "N" | "S" | "E" | "W" -- only visible on an 8-frame sprite
    type:       str = "npc"
    npc_id:     str | None = None   # key into OverworldScene.npc_defs/npc_frames_by_id, if resolved
    dialogue_variants: list[DialogueVariant] = field(default_factory=list)


# ── Scene ────────────────────────────────────────────────────────────────────

class OverworldScene:
    """A single walkable screen: tile grid, player, roaming NPCs.

    Implements the handle_event/update/draw protocol expected by
    engine.renderer.run(). Must be constructed after pygame.display.set_mode()
    so tileset/sprite loading can call .convert_alpha().
    """

    def __init__(self, map_path: Path = _HUB_MAP, player: Player | None = None,
                 inventory: Inventory | None = None, game_state: GameState | None = None,
                 active_slot: int = 1):
        """`player`/`inventory`/`game_state`, if given, are what a save slot
        restores (see engine.save / maptest.py's save-slot picker) — omit
        all three for a fresh boot with no save (maptest.py's map-picker
        flow: default Player, empty Inventory, empty GameState). `spawn` is
        only overridden from the loaded player's own row/col when a save
        was actually passed in; a fresh boot keeps the existing debug
        convenience of landing on a random warp (see _load_map) rather than
        Player.default()'s meaningless (0, 0) placeholder. `active_slot` is
        only used by SAVE on the start menu, to know which slot to write.
        """
        self.tile_px = cfg.tile_px
        self.player_move_ms = cfg.player_move_ms   # ms to cross one tile at normal speed
        self.player_speed = 1000.0 / self.player_move_ms   # tiles/sec, for continuous movement

        self.sprites = load_sprites(_SPRITES_PNG, _SPRITES_TXT, self.tile_px)
        self.enemy_sprites = load_enemy_sprites(_ENEMY_SPRITES_DIR, self.tile_px)
        self.menu_assets = load_menu_assets(_MENU_DIR)
        self.dialogue_font = pygame.font.Font(str(_DIALOGUE_FONT_PATH), _DIALOGUE_FONT_PT)
        print(f'  {len(self.sprites)} sprites, {len(self.enemy_sprites)} enemy sprites', flush=True)

        # Static enemy definitions (data/enemy/enemies.yaml) -- loaded once,
        # same as item_defs below; per-map placement/spawning happens in
        # _load_map.
        self.enemy_defs = load_enemy_defs()

        # A def whose `sprite` has no matching file in enemy_sprites still
        # loads and spawns fine -- get_enemy_frame just silently returns
        # None and draw() skips the blit, so a missing/misnamed sprite file
        # otherwise shows up as "enemies exist but nothing renders," with no
        # error at all. Warn once at boot instead of leaving that silent.
        missing_sprites = sorted({d.sprite for d in self.enemy_defs.values()
                                  if d.sprite not in self.enemy_sprites})
        if missing_sprites:
            print(f'WARNING: enemies.yaml references sprite(s) with no matching file in '
                  f'assets/sprites/enemies/: {missing_sprites} -- those enemies will spawn '
                  f'but render as nothing until a strip PNG with that filename stem is added',
                  flush=True)

        # Static NPC definitions + sprite specs (data/npcs/npc.yaml) -- loaded
        # once, same spirit as enemy_defs above. Per-map placement (resolving
        # each npc-type object's own fields against these) happens in
        # _load_map.
        self.npc_defs = load_npc_defs()
        self.npc_sprite_specs = load_npc_sprite_specs()
        self.npc_sprites = load_npc_sprites(_NPC_SPRITES_DIR, self.tile_px)   # raw, unrecolored, by filename stem

        missing_npc_sprites = sorted({spec.file for spec in self.npc_sprite_specs.values()
                                      if spec.file not in self.npc_sprites})
        if missing_npc_sprites:
            print(f'WARNING: npc.yaml references sprite file(s) with no matching PNG in '
                  f'assets/sprites/npcs/: {missing_npc_sprites} -- those NPCs will place but '
                  f'render as nothing until a strip PNG with that filename stem is added',
                  flush=True)

        # One recolored frame set per unique NPC def (not per placement, not
        # per frame drawn) -- see recolor_surface / engine.npc.NpcDef.colors.
        # A def with no `colors` override still gets an entry here (recolored
        # "to" its own sprite's placeholder colors, a no-op swap) so draw-time
        # lookup never has to branch between "recolored" and "not."
        self.npc_frames_by_id: dict[str, list[pygame.Surface]] = {}
        for npc_id, npc_def in self.npc_defs.items():
            spec = self.npc_sprite_specs.get(npc_def.sprite)
            base_frames = self.npc_sprites.get(spec.file) if spec else None
            if spec is None or not base_frames:
                continue
            to_colors = npc_def.colors if npc_def.colors is not None else spec.colors
            self.npc_frames_by_id[npc_id] = [recolor_surface(f, spec.colors, to_colors) for f in base_frames]

        self.menu = StartMenu()
        self.save_menu = SaveMenu()   # confirm-save overlay, opened from SAVE on self.menu
        self.inventory_menu = InventoryMenu()   # opened from INVENTORY on self.menu
        self.settings_menu = SettingsMenu(scale=cfg.pygame_scale)   # opened from SETTINGS on self.menu
        self.dialogue = DialogueBox()   # opened by facing a sign and pressing A
        self.active_slot = active_slot

        # Item defs are static hand-authored data (data/items/items.yaml),
        # loaded once here same as sprites/menu assets. self.inventory and
        # self.game_state are what SAVE (start menu) writes to disk and what
        # a loaded save restores — empty/fresh unless maptest.py loaded a
        # save slot and passed them in.
        self.item_defs = load_item_defs()
        self.inventory = inventory if inventory is not None else Inventory()
        self.game_state = game_state if game_state is not None else GameState()

        self._npc_rng = random.Random(0x4875624E5043)
        self._enemy_rng = random.Random(0x456E656D79)   # "Enemy" in hex
        self._loaded_save = player is not None
        self.player = player if player is not None else Player.default()

        # No OS key repeat — held directions are tracked by
        # engine.input.HeldDirectionInput, which resolves up to one active
        # direction per axis (vertical/horizontal) each frame so diagonal
        # movement is deliberate (both axes held) rather than an artifact of
        # independent per-key OS repeats landing in the same frame.
        self._input = HeldDirectionInput()

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

        spawn = (self.player.row, self.player.col) if self._loaded_save else None
        facing = self.player.facing if self._loaded_save else None
        self._load_map(map_path, spawn=spawn, facing=facing)

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
        override the map's default spawn point — used when arriving via a
        warp instead of booting into the map fresh. With no override (a
        fresh boot, e.g. maptest.py's map picker), a map that has any warp
        objects spawns the player on a random one (_warp_landing) instead
        of the arbitrary tiled_spawn_point() fallback, so debugging a map
        starts you somewhere the map's own content considers an entrance —
        tiled_spawn_point() only ever fires for maps with no warps at all.
        """
        print(f'Loading map {map_path.stem}...', flush=True)
        self.map_path = map_path
        self.tmap = tmap if tmap is not None else load_tiled_map(map_path)
        self.tile_surfaces = load_tile_surfaces(self.tmap, self.tile_px)
        self.passable = tiled_passable_grid(self.tmap)
        print(f'  {len(self.tile_surfaces)} tiles', flush=True)

        self.objects = [o for o in self.tmap.objects
                        if o.type not in ("npc", "warp", "enemy", "spawner")]   # containers/signs
        self.warps = [o for o in self.tmap.objects if o.type == "warp"]
        npc_map_objects = [o for o in self.tmap.objects if o.type == "npc"]
        self.npcs = []
        for o in npc_map_objects:
            # npc_id, if set, points at a shared definition in
            # data/npcs/npc.yaml (engine.npc.load_npc_defs) -- this object's
            # own sprite/behavior/dialogue win when set, otherwise fall back
            # to the def's, otherwise the same "nothing set" defaults as
            # before npc_id existed (no sprite, static, no dialogue).
            npc_def = self.npc_defs.get(o.npc_id) if o.npc_id else None
            if o.npc_id and npc_def is None:
                print(f'npc object {o.name!r} references unknown npc_id {o.npc_id!r} -- '
                      f'falling back to its own inline sprite/behavior/dialogue fields', flush=True)
            # NPC.row/col is the tile the NPC's sprite mostly covers, not
            # the tile its top-left corner floors into (o.row/o.col) — a
            # sub-tile nudge (see row_exact/col_exact) can leave the
            # floored tile holding only a sliver of the sprite while the
            # tile next to it holds nearly all of it. Rounding instead of
            # flooring picks whichever tile has the majority, which is what
            # passability (_passable_with_npcs) and interaction
            # (Player.adjacent_interactable) both key off of, so both line
            # up with where the NPC visually stands.
            # Dialogue: the placement's own o.dialogue (npcs_<map>.yaml),
            # parsed the same way npc.yaml's own dialogue is (flat page
            # list or conditional variant list -- see engine.npc.
            # parse_dialogue), wins if it authored anything at all --
            # exactly like sprite/behavior above. Otherwise fall back to
            # the resolved def's own (already-parsed) variant list.
            own_dialogue = parse_dialogue(o.dialogue)
            dialogue_variants = own_dialogue or (npc_def.dialogue if npc_def else [])
            self.npcs.append(NPC(
                index=o.id, name=o.name, row=round(o.row_exact), col=round(o.col_exact),
                npc_sprite=o.sprite or (npc_def.sprite if npc_def else None),
                behavior=o.behavior or (npc_def.behavior if npc_def else None) or "static",
                facing=npc_def.facing if npc_def else "S",
                npc_id=o.npc_id if npc_def else None,
                dialogue_variants=dialogue_variants,
            ))

        # Enemies: `enemy` objects place directly; `spawner` objects roll
        # once per map load (this call — covers both a fresh boot and every
        # warp arrival/re-arrival, so leaving and re-entering a screen
        # re-rolls for free). Both start at their exact Tiled placement,
        # same convention as NPCs (see row_exact/col_exact above) — enemy
        # position is continuous from here on (see engine.enemy), not
        # renderer-tweened.
        self.enemies: list[Enemy] = []
        for o in self.tmap.objects:
            if o.type == "enemy":
                enemy_id, level_field = o.enemy_id, o.level
            elif o.type == "spawner":
                enemy_id = resolve_spawn(o.spawn_chance or 0.0, o.enemies, self._enemy_rng)
                level_field = o.level
                if enemy_id is None:
                    continue
            else:
                continue
            edef = self.enemy_defs.get(enemy_id)
            if edef is None:
                print(f'{o.type} object {o.name!r} references unknown enemy_id '
                      f'{enemy_id!r} -- skipping', flush=True)
                continue
            self.enemies.append(Enemy(
                index=o.id, enemy_id=enemy_id, row=o.row_exact, col=o.col_exact,
                level=resolve_level(level_field if level_field is not None else edef.level, self._enemy_rng),
                behavior=edef.behavior, behavior_axis=edef.behavior_axis,
            ))

        if spawn is not None:
            self.player.row, self.player.col = spawn
            if facing is not None:
                self.player.facing = facing
        elif self.warps:
            # No explicit spawn (i.e. booting straight onto this map, not
            # arriving via a warp — see maptest.py) but the map has warp
            # objects: land on a random one, same landing math as a real
            # warp trip, rather than the arbitrary tiled_spawn_point()
            # fallback below. Deterministic per scene (self._npc_rng is
            # freshly seeded every construction), not actually different
            # run to run — see engine/renderer.py's RNG usage elsewhere.
            warp = self._npc_rng.choice(self.warps)
            (self.player.row, self.player.col), self.player.facing = self._warp_landing(warp)
        else:
            self.player.row, self.player.col = tiled_spawn_point(self.tmap)

        self._anim_frame = 0       # 0 or 1, shared NPC animation tick
        self._anim_timer = 0       # ms accumulator for NPC anim flip
        self._npc_move_timer = 0   # ms accumulator for NPC position update
        self._player_anim = 0      # 0 or 1, walk-cycle frame
        self._player_walk_timer = 0   # ms accumulator for the walk-cycle flip

        # Player.row/col is itself the continuous position now (see
        # engine.player.Player.move / engine.movement) — no renderer-side
        # tween to seed. _player_vis just mirrors it each frame (updated in
        # update()) so camera/draw code has one name to read regardless of
        # whether it's the player or an NPC's tweened position.
        self._player_vis = (self.player.row, self.player.col)
        self._player_last_tile = self.player.current_tile()   # for edge-triggered warp checks
        self._player_moving = False   # any axis active last tick? — gates turn-in-place

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
              f'{len(self.npcs)} NPCs  |  {len(self.warps)} warps  |  {len(self.enemies)} enemies', flush=True)

    # ── display settings ────────────────────────────────────────────────────
    #
    # Proxies onto self.settings_menu (the actual owner of these values —
    # see engine.menu.SettingsMenu) so engine.renderer.run()'s generic app
    # loop can poll a plain scene attribute rather than reaching into a
    # scene-specific menu object.

    @property
    def display_scale(self) -> int:
        return self.settings_menu.scale

    @property
    def fullscreen(self) -> bool:
        return self.settings_menu.fullscreen

    # ── input ────────────────────────────────────────────────────────────────

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYDOWN:
            # DEV-ONLY: hot-reload the current map's JSON/tileset off disk
            # without restarting the process, so map edits can be checked
            # live. Not a shipping feature -- cut this before release.
            if event.key == pygame.K_r and self._transition_phase is None:
                self._load_map(self.map_path,
                                spawn=(self.player.row, self.player.col),
                                facing=self.player.facing)
                return

            direction = _DIR_KEY.get(event.key)
            if direction:
                if self.player.state == PlayerState.IN_MENU:
                    category_item_count = len(self.inventory.items_in(
                        self.inventory_menu.selected_category(), self.item_defs))
                    handle_menu_direction(direction, self.menu, self.save_menu,
                                           self.inventory_menu, self.settings_menu,
                                           category_item_count)
                else:
                    # Turn-in-place (a tap that doesn't move, just faces the
                    # new way) only applies from a dead stop — see
                    # engine.input.HeldDirectionInput. Already moving, a
                    # direction change (including adding a second axis to go
                    # diagonal) takes effect immediately, no added delay.
                    facing = self.player.facing if not self._player_moving else None
                    if self._input.press(direction, facing):
                        self.player.facing = direction

            button = _BUTTON_KEY.get(event.key)
            if button:
                self._held_buttons.add(button)
            if button == 'START':
                handle_start_button(self.player, self.menu, self.save_menu,
                                     self.inventory_menu, self.settings_menu)
            elif button == 'B':
                handle_b_button(self.player, self.menu, self.save_menu,
                                 self.inventory_menu, self.settings_menu)
            elif button == 'A':
                # Menu confirm (SAVE writes self.active_slot to disk and
                # closes back to the start menu; INVENTORY/SETTINGS open
                # their overlays; PARTY still stubbed) / dialogue
                # advance-close (no-op while still typing in) / open a
                # dialogue by facing an interactable sign or NPC, or a
                # container (grants its `contents` item + sets its flag the
                # first time, inert after that — see engine.input).
                handle_a_button(self.player, self.menu, self.save_menu, self.inventory_menu,
                                 self.settings_menu,
                                 self.dialogue, self.npcs, self.objects,
                                 wrap_pages=self._wrap_dialogue_pages,
                                 game_state=self.game_state, inventory=self.inventory,
                                 item_defs=self.item_defs, map_name=self.map_path.stem,
                                 active_slot=self.active_slot)
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

        self._update_enemies(dt_ms)

        vertical, horizontal = self._input.tick(dt_ms)
        self._player_moving = vertical is not None or horizontal is not None
        self._update_player_movement(dt_ms, vertical, horizontal)

        if self._player_moving:
            self._player_walk_timer += dt_ms
            if self._player_walk_timer >= self.player_move_ms / 2:
                self._player_walk_timer -= self.player_move_ms / 2
                self._player_anim ^= 1
        else:
            self._player_walk_timer = 0
            self._player_anim = 0

        for tw in self._npc_tweens.values():
            if tw['elapsed'] < _NPC_MOVE_MS:
                tw['elapsed'] = min(tw['elapsed'] + dt_ms, _NPC_MOVE_MS)

    def _update_player_movement(self, dt_ms: int, vertical: str | None, horizontal: str | None) -> None:
        self.player.move(dt_ms, vertical, horizontal, self.player_speed, self._passable_with_npcs())
        self._player_vis = (self.player.row, self.player.col)

        # Warps auto-trigger the instant the player's current tile becomes
        # theirs — no A press. Edge-triggered off the rounded tile changing
        # (rather than "a step landed," now that movement is continuous) so
        # this fires exactly once per entry, from any approach angle.
        # Spawning onto a tile (see _load_map's spawn param) seeds
        # _player_last_tile directly, so landing on the paired warp on the
        # other side can't immediately bounce back here.
        current_tile = self.player.current_tile()
        if current_tile != self._player_last_tile:
            self._player_last_tile = current_tile
            warp = self._warp_at(*current_tile)
            if warp is not None:
                self._begin_warp(warp)

    def _warp_at(self, row: int, col: int) -> 'MapObject | None':
        for warp in self.warps:
            if warp.row == row and warp.col == col:
                return warp
        return None

    @staticmethod
    def _warp_landing(warp: 'MapObject') -> tuple[tuple[int, int], str]:
        """Where the player lands (and faces) when spawning at `warp`: its
        own row/col, offset `distance` tiles in its own `facing` direction
        (see the warp fields in data/maps/populate_yamls.py). Shared by
        _swap_map (warping in from another map) and _load_map's own
        no-explicit-spawn fallback (booting straight onto a map that has
        warps, e.g. via maptest.py)."""
        facing = warp.facing or 'S'
        dr, dc = _DIR_DELTA[facing]
        distance = warp.distance or 0
        return (warp.row + dr * distance, warp.col + dc * distance), facing

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
        spawn, facing = self._warp_landing(target)
        self._load_map(dest_path, tmap=dest_tmap, spawn=spawn, facing=facing)

    def _npc_frames(self, npc: 'NPC') -> list[pygame.Surface] | None:
        """This NPC's resolved frame list -- the recolored cache for an
        npc_id'd NPC (self.npc_frames_by_id, set once at __init__), or its
        sprite's raw, unrecolored frames for an inline placement with no
        def. Shared by _draw_entities (which frame to draw) and
        _passable_with_npcs (how many tiles tall it is, for collision) so
        both always agree on the same frame set."""
        frames = self.npc_frames_by_id.get(npc.npc_id) if npc.npc_id else None
        if frames is None:
            spec = self.npc_sprite_specs.get(npc.npc_sprite)
            frames = self.npc_sprites.get(spec.file) if spec else None
        return frames

    def _passable_with_npcs(self) -> list[list[bool]]:
        """The static walkable grid with every NPC's current tile marked
        impassable, so the player can't walk through them -- including any
        extra tile(s) a taller-than-one-tile sprite's overhang is drawn
        over (see load_enemy_sprites/_draw_entities): a 32px-tall NPC also
        blocks the tile directly north of its own row/col, where its
        hat/head is drawn, not just its body tile. Rebuilt fresh on each
        attempted step since NPCs move — engine.player stays headless and
        ignorant of NPCs entirely; it just sees a plain grid."""
        grid = [row[:] for row in self.passable]
        for npc in self.npcs:
            frames = self._npc_frames(npc)
            tile_span = (frames[0].get_height() // self.tile_px) if frames else 1
            for i in range(tile_span):
                r = npc.row - i
                if 0 <= r < len(grid):
                    grid[r][npc.col] = False
        return grid

    def _tile_occupied(self, row: int, col: int, exclude_npc: 'NPC | None' = None) -> bool:
        """True if the player or another NPC currently stands on (row, col).
        Used to keep wander NPCs from stepping onto the player or stacking
        on each other."""
        if self.player.current_tile() == (row, col):
            return True
        for other in self.npcs:
            if other is not exclude_npc and other.row == row and other.col == col:
                return True
        return False

    def _update_npc_wander(self) -> None:
        """One decision tick for every "wander" NPC: a coin flip decides
        whether it steps this tick, then a random cardinal direction is
        tried against the walkable grid and current occupancy — same rules
        as the player, just without input driving it. `facing` only turns
        on an actual step (blocked attempts don't turn the NPC in place the
        way the player's tap-to-turn does) -- visible only on an 8-frame
        sprite, a no-op field otherwise."""
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
            npc.facing = direction
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

    def _update_enemies(self, dt_ms: int) -> None:
        """One frame of enemy movement. Unlike NPCs, there's no tween step
        here — engine.enemy.update_enemy mutates Enemy.row/col continuously
        in place, so the position it leaves behind is already what draw()
        reads. Enemies never block or are blocked by the player (see
        README/CLAUDE.md) — check_overlap is a no-op today, just marking
        where a future battle-trigger-on-touch + post-battle blink-immunity
        window will hook in."""
        for enemy in self.enemies:
            edef = self.enemy_defs.get(enemy.enemy_id)
            if edef is None:
                continue
            update_enemy(enemy, edef, self.passable, self.player.row, self.player.col,
                         dt_ms, self._enemy_rng)
            if check_overlap(enemy, self.player.row, self.player.col):
                pass   # no-op placeholder — see docstring above

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
        """Composite the map in Tiled's real, authored layer order: each
        `TiledLayer` is either a tile layer (blit its GID grid) or the
        `"objects"` marker (blit containers/signs/NPCs/enemies/player as one
        bundle, via _draw_entities) — see load_tiled_map. A tile layer drawn
        before that marker ends up under the player/objects, one drawn after
        ends up over them (roofs, canopies, ...), purely as a consequence of
        where the map author put it in Tiled — nothing here hardcodes which
        layer index is "the ground."
        """
        surface.fill((0, 0, 0))
        cam_x, cam_y = self._camera_offset_px()

        drew_entities = False
        for layer in self.tmap.layers:
            if layer.kind == "tile":
                self._draw_tile_layer(surface, layer, cam_x, cam_y)
            else:
                self._draw_entities(surface, cam_x, cam_y)
                drew_entities = True
        if not drew_entities:
            # Defensive only: every map here has exactly one Object Layer,
            # but a map authored without one shouldn't hide the player.
            self._draw_entities(surface, cam_x, cam_y)

        if self.menu.is_open:
            self._draw_start_menu(surface)

        if self.save_menu.is_open:
            self._draw_save_menu(surface)

        if self.inventory_menu.is_open:
            self._draw_inventory_menu(surface)

        if self.settings_menu.is_open:
            self._draw_settings_menu(surface)

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

    def _draw_inventory_menu(self, surface: pygame.Surface) -> None:
        """Alpha placeholder inventory screen: solid black, dialogue_font +
        ASCII only — no icon/background art yet. Left half: category header
        (E/W cycles, wraps) over a scrollable item list (N/S, clamped).
        Right half: the highlighted item's icon placeholder, name,
        description, and effect — blank if the category has nothing in it.
        """
        surface.fill((0, 0, 0))
        font = self.dialogue_font
        line_h = font.get_linesize()

        category = self.inventory_menu.selected_category()
        header = f"< {CATEGORY_LABELS[category]} >"
        surface.blit(font.render(header, False, _INV_TEXT_COLOR), (_INV_LIST_LEFT_X, _INV_HEADER_Y))

        view_h = cfg.view_rows * self.tile_px
        for row in range((view_h - _INV_LIST_TOP_Y) // line_h):
            surface.blit(font.render("|", False, _INV_DIM_COLOR),
                         (_INV_DIVIDER_X, _INV_LIST_TOP_Y + row * line_h))

        item_ids = self.inventory.items_in(category, self.item_defs)
        if not item_ids:
            surface.blit(font.render("(nothing here)", False, _INV_DIM_COLOR),
                         (_INV_LIST_LEFT_X, _INV_LIST_TOP_Y))
            return

        list_w = _INV_DIVIDER_X - _INV_LIST_LEFT_X - 4   # keep clear of the divider column
        selected = self.inventory_menu.selected
        for i, item_id in enumerate(item_ids):
            item_def = self.item_defs[item_id]
            qty = self.inventory.counts.get(item_id, 0)
            suffix = f" x{qty}" if item_def.stackable and qty > 1 else ""
            cursor = "> " if i == selected else "  "
            color = _INV_TEXT_COLOR if i == selected else _INV_DIM_COLOR
            line = _fit_text(font, f"{cursor}{item_def.name}{suffix}", list_w)
            surface.blit(font.render(line, False, color),
                         (_INV_LIST_LEFT_X, _INV_LIST_TOP_Y + i * line_h))

        self._draw_inventory_detail(surface, self.item_defs[item_ids[selected]])

    def _draw_inventory_detail(self, surface: pygame.Surface, item_def: 'ItemDef') -> None:
        """Right-half detail pane for whichever item is currently
        highlighted on the inventory screen."""
        font = self.dialogue_font
        line_h = font.get_linesize()
        x, y = _INV_DETAIL_LEFT_X, _INV_LIST_TOP_Y

        icon_rect = pygame.Rect(x, y, _INV_ICON_SIZE, _INV_ICON_SIZE)
        pygame.draw.rect(surface, _INV_TEXT_COLOR, icon_rect, width=1)   # icon placeholder — no art yet
        y += _INV_ICON_SIZE + line_h

        surface.blit(font.render(item_def.name, False, _INV_TEXT_COLOR), (x, y))
        y += line_h * 2

        detail_w = cfg.view_cols * self.tile_px - x - _INV_MARGIN
        wrapped = _wrap_text_to_screens(font, item_def.description, detail_w, rect_h=10_000)[0]
        for line in wrapped.split("\n"):
            surface.blit(font.render(line, False, _INV_TEXT_COLOR), (x, y))
            y += line_h
        y += line_h

        effect_line = self._format_item_effect(item_def.effect)
        if effect_line:
            surface.blit(font.render(effect_line, False, _INV_TEXT_COLOR), (x, y))

    @staticmethod
    def _format_item_effect(effect: dict | None) -> str:
        """'{hp: 15, mp: 5}' -> 'HP +15  MP +20' for the detail pane. Empty
        string (nothing drawn) for non-consumables, which have no effect."""
        if not effect:
            return ""
        return "  ".join(f"{key.upper()} +{value}" for key, value in effect.items())

    def _draw_settings_menu(self, surface: pygame.Surface) -> None:
        """Alpha placeholder settings screen: solid black, dialogue_font
        only — same "no art yet" approach as the inventory screen. Two
        rows, each a "< value >" pair like the inventory header's category
        picker; the highlighted row's label is drawn in the bright color,
        the other dim. Values change immediately off E/W (see
        engine.menu.SettingsMenu.move_cursor) — there's no separate confirm
        step, so nothing here needs to know about that, only display the
        current value.
        """
        surface.fill((0, 0, 0))
        font = self.dialogue_font
        line_h = font.get_linesize()

        surface.blit(font.render("SETTINGS", False, _SET_TEXT_COLOR), (_SET_LABEL_X, _SET_TITLE_Y))

        scale = self.settings_menu.scale
        mode = "FULLSCREEN" if self.settings_menu.fullscreen else "WINDOWED"
        values = (f"{scale}x", mode)

        for i, label in enumerate(_SET_ROW_LABELS):
            y = _SET_ROWS_TOP_Y + i * line_h
            selected = i == self.settings_menu.selected
            color = _SET_TEXT_COLOR if selected else _SET_DIM_COLOR
            cursor = "> " if selected else "  "
            surface.blit(font.render(f"{cursor}{label}", False, color), (_SET_LABEL_X, y))
            surface.blit(font.render(f"< {values[i]} >", False, _SET_TEXT_COLOR), (_SET_VALUE_X, y))

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

    def _draw_tile_layer(self, surface: pygame.Surface, layer: 'TiledLayer', cam_x: int, cam_y: int) -> None:
        """Blit one tile layer's GID grid, offset by the camera. See draw()
        for how this and _draw_entities are interleaved in Tiled's real
        layer order."""
        for r, row in enumerate(layer.grid):
            y = r * self.tile_px - cam_y
            for c, gid in enumerate(row):
                surf = get_tile_by_gid(self.tile_surfaces, gid)
                if surf:
                    surface.blit(surf, (c * self.tile_px - cam_x, y))

    def _draw_entities(self, surface: pygame.Surface, cam_x: int, cam_y: int) -> None:
        """Blit containers/signs, NPCs, enemies, and the player as one bundle
        — drawn together wherever the map's object layer falls among the
        tile layers (see draw()).

        A container-type object stops drawing its `gid` once its persistent
        flag is set (engine.game_state.persistent_id) — see
        engine.input._open_container, which sets that flag the first time a
        container's opened. That's the mechanism behind a container's
        "opened" visual: whatever's painted on a tile layer beneath it (an
        empty trash can, an open chest, ...) shows through once the flag's
        set — no second sprite/state field, just the container disappearing.
        Signs/warps have no such state and always draw (warps aren't drawn
        at all — see OverworldScene.warps).
        """
        map_name = self.map_path.stem
        for obj in self.objects:
            if obj.type == "container" and self.game_state.flag(persistent_id(map_name, obj.name)):
                continue
            obj_surf = get_tile_by_gid(self.tile_surfaces, obj.gid)
            if obj_surf:
                topleft = (obj.col * self.tile_px - cam_x, obj.row * self.tile_px - cam_y)
                if obj.rotation:
                    # Tiled tile objects anchor (and rotate) about their bottom-left
                    # corner, not their center -- see MapObject.rotation.
                    pivot_world = (topleft[0], topleft[1] + self.tile_px)
                    obj_surf, topleft = _rotate_about_point(
                        obj_surf, obj.rotation, (0, self.tile_px), pivot_world)
                surface.blit(obj_surf, topleft)

        for npc in self.npcs:
            frames = self._npc_frames(npc)
            npc_surf = get_npc_frame(frames, npc.facing, self._anim_frame)
            if npc_surf:
                vr, vc = self._npc_visual_pos(npc)
                # A frame taller than one tile (see load_enemy_sprites) anchors its
                # bottom tile_px rows to the tile; extra height extends upward.
                y_overhang = npc_surf.get_height() - self.tile_px
                surface.blit(npc_surf, (round(vc * self.tile_px) - cam_x,
                                        round(vr * self.tile_px) - cam_y - y_overhang))

        for enemy in self.enemies:
            edef = self.enemy_defs.get(enemy.enemy_id)
            enemy_surf = get_enemy_frame(self.enemy_sprites, edef.sprite, self._anim_frame) if edef else None
            if enemy_surf:
                y_overhang = enemy_surf.get_height() - self.tile_px
                surface.blit(enemy_surf, (round(enemy.col * self.tile_px) - cam_x,
                                          round(enemy.row * self.tile_px) - cam_y - y_overhang))

        player_surf = get_party_frame(self.sprites, 'melvin', self.player.facing, self._player_anim)
        if player_surf:
            vr, vc = self._player_vis
            surface.blit(player_surf, (round(vc * self.tile_px) - cam_x, round(vr * self.tile_px) - cam_y))


# ── Battle scene ──────────────────────────────────────────────────────────
#
# Debug/graphical-test battle screen — see maptest.py's "battles" mode.
# First pass, scoped deliberately narrow (see engine.battle's module
# docstring): MELVIN vs. one enemy, auto-attack only, no player-driven
# action menu wired up yet. The 4-option list at the bottom is drawn so the
# layout's blocked out, but it's cosmetic — nothing routes input to it, and
# ATTACK is the only option actually happening. When ITEM/SPECIAL become
# real, their exact scope needs to be nailed down first — don't wire either
# up based on a guess.

_BATTLE_TOP_H     = 40      # top textbox height, px — battle text
_BATTLE_BOTTOM_H  = 56      # bottom textbox height, px — party status + stub menu
_BATTLE_MARGIN    = 8
_BATTLE_TEXT_COLOR = (255, 255, 255)
_BATTLE_DIM_COLOR  = (110, 110, 110)   # stub menu options other than ATTACK
_BATTLE_MENU_X    = 160     # bottom-right pane start — same split as the start menu's _MENU_X
_BATTLE_MENU_OPTIONS = ("ATTACK", "ITEM", "SPECIAL", "RUN")
_BATTLE_HOLD_MS   = 700     # pause after a line's fully typed before the next action fires

_EFFECTS_BY_NAME = {name: cls for name, cls in EFFECTS.values()}   # procgen.visualizer effects, by name


def load_battle_art(art_dir: Path, filename: str) -> pygame.Surface | None:
    """One enemy's full-size battle portrait (assets/tiles/enemies/<filename>)
    — unrelated to the 16px overworld strip loaded by load_enemy_sprites;
    this is a single static image, not an animation strip. Returns None if
    the file doesn't exist so a missing/blank battle_art fails soft, same
    spirit as OverworldScene's missing-overworld-sprite warning. Call after
    pygame.display.set_mode()."""
    path = art_dir / filename
    if not path.exists():
        return None
    return pygame.image.load(str(path)).convert_alpha()


class BattleScene:
    """Debug battle screen — MELVIN vs. one enemy, auto-attack only.

    Implements the handle_event/update/draw protocol expected by
    engine.renderer.run(), same as OverworldScene. Not reachable from normal
    play yet — booted directly by maptest.py's "battles" mode, which prompts
    for an enemy id from data/enemy/enemies.yaml.
    """

    def __init__(self, enemy_id: str, rng: random.Random | None = None):
        self.dialogue_font = pygame.font.Font(str(_DIALOGUE_FONT_PATH), _DIALOGUE_FONT_PT)
        self.rng = rng or random.Random()

        edef = load_enemy_defs()[enemy_id]
        level = resolve_level(edef.level, self.rng)
        enemy = Fighter(name=edef.name, iq=edef.iq, weight=edef.weight,
                        sweat=edef.sweat, hair=edef.hair, level=level, is_enemy=True)
        party = load_fighter(_PARTY_DIR / 'melvin.json')
        self.battle = BattleState(party=party, enemy=enemy, rng=self.rng)

        self.battle_art = load_battle_art(_BATTLE_ART_DIR, edef.battle_art) if edef.battle_art else None
        if self.battle_art is None:
            print(f'WARNING: no battle_art for enemy {enemy_id!r} -- it will render as nothing', flush=True)

        # battle_bg pins a specific procgen.visualizer effect by name;
        # null/omitted (the common case today) picks one at random per battle.
        effect_cls = _EFFECTS_BY_NAME.get(edef.battle_bg) if edef.battle_bg else None
        if effect_cls is None:
            effect_name, effect_cls = self.rng.choice(list(EFFECTS.values()))
        else:
            effect_name = edef.battle_bg
        self._effect = effect_cls()
        self._effect_canvas = pygame.Surface((256, 240))
        self._effect_t = 0.0

        self.text_box = DialogueBox()
        self.text_box.open([f"A wild {enemy.name} (LVL {enemy.level}) appeared!"])
        self._hold_elapsed = 0

        print(f'Battle: MELVIN vs {enemy.name} (LVL {enemy.level})  |  background: {effect_name}', flush=True)

    def handle_event(self, event: pygame.event.Event) -> None:
        pass   # auto-attack only -- nothing to route yet, see class docstring

    def update(self, dt_ms: int) -> None:
        self._effect_t += dt_ms / 1000
        self.text_box.tick(dt_ms, cfg.dialogue_char_ms)

        if not self.text_box.is_fully_revealed():
            return

        self._hold_elapsed += dt_ms
        if self._hold_elapsed < _BATTLE_HOLD_MS:
            return
        if self.battle.phase in ("win", "loss"):
            return   # final message stays on screen -- no return-to-map flow yet
        self._hold_elapsed = 0
        self.text_box.open([self.battle.step()])

    def draw(self, surface: pygame.Surface) -> None:
        self._draw_background(surface)
        self._draw_enemy(surface)
        self._draw_top_box(surface)
        self._draw_bottom_box(surface)

    def _draw_background(self, surface: pygame.Surface) -> None:
        rgb = self._effect.render(self._effect_t)
        frame = (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
        pygame.surfarray.blit_array(self._effect_canvas, frame.transpose(1, 0, 2))
        # Effect canvas is 256x240 (NES-overscan-style, like dialogue_box.png)
        # against this scene's 256x224 view -- center-crop 8px off top/bottom
        # rather than resizing the effect's own math.
        surface.blit(self._effect_canvas, (0, -8))

    def _draw_enemy(self, surface: pygame.Surface) -> None:
        if self.battle_art is None:
            return
        view_w = surface.get_width()
        mid_top, mid_bottom = _BATTLE_TOP_H, surface.get_height() - _BATTLE_BOTTOM_H
        art_w, art_h = self.battle_art.get_size()
        x = (view_w - art_w) // 2
        y = mid_top + (mid_bottom - mid_top - art_h) // 2
        surface.blit(self.battle_art, (x, y))

    def _draw_top_box(self, surface: pygame.Surface) -> None:
        pygame.draw.rect(surface, (0, 0, 0), pygame.Rect(0, 0, surface.get_width(), _BATTLE_TOP_H))
        line_h = self.dialogue_font.get_linesize()
        for i, line in enumerate(self.text_box.visible_text().split("\n")):
            line_surf = self.dialogue_font.render(line, False, _BATTLE_TEXT_COLOR)
            surface.blit(line_surf, (_BATTLE_MARGIN, _BATTLE_MARGIN + i * line_h))

    def _draw_bottom_box(self, surface: pygame.Surface) -> None:
        view_w, view_h = surface.get_size()
        top = view_h - _BATTLE_BOTTOM_H
        pygame.draw.rect(surface, (0, 0, 0), pygame.Rect(0, top, view_w, _BATTLE_BOTTOM_H))

        font = self.dialogue_font
        line_h = font.get_linesize()
        party = self.battle.party
        status = f"{party.name}   HP {max(0, party.hp)}/{party.max_hp}   MP {max(0, party.mp)}/{party.max_mp}"
        surface.blit(font.render(status, False, _BATTLE_TEXT_COLOR), (_BATTLE_MARGIN, top + _BATTLE_MARGIN))

        # Stub action menu -- drawn, not interactive (see class docstring).
        for i, option in enumerate(_BATTLE_MENU_OPTIONS):
            color = _BATTLE_TEXT_COLOR if option == "ATTACK" else _BATTLE_DIM_COLOR
            surface.blit(font.render(option, False, color), (_BATTLE_MENU_X, top + _BATTLE_MARGIN + i * line_h))
