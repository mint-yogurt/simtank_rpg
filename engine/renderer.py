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
                item-action popup: N-S moves its (usually one-row) option,
                or scrolls the target-member picker (clamps) / on the
                settings screen: N-S switches row (wraps), E-W changes the
                highlighted row's value (wraps) / on the party screen: N-S
                scrolls the member list (wraps)
  Enter       — START: open/close the start menu (no-op while any overlay
                is open)
  X           — A: confirm start-menu selection (SAVE opens the save-confirm
                overlay, INVENTORY/SETTINGS/PARTY open their screens) /
                confirm YES-NO on the save-confirm overlay (YES is stubbed
                — see engine.menu.SaveMenu.confirm) / on the inventory
                screen, opens the item-action popup for the selected row
                (USE/EQUIP/UNEQUIP, see engine.input._item_action_options)
                / no-op on the settings and party screens (settings applies
                changes immediately off E/W; party is pure display) /
                advance or close an open dialogue box / open one by facing
                a sign or NPC
  Z           — B: close whichever menu is on top (item-action popup first
                if it's open, then save-confirm/inventory/settings/party,
                then the start menu)
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

from engine.battle import (
    BATTLE_MENU_OPTIONS, BattleMenu, BattleState, Fighter, apply_level_ups, fighter_from_roster,
    load_fighter, xp_to_next_level,
)
from engine.config import cfg
from engine.cutscene import CutsceneDef, CutscenePlayer, load_cutscene_defs, trigger_matches
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
    cutscene_id_from_result,
    handle_a_button,
    handle_b_button,
    handle_menu_direction,
    handle_start_button,
    held_button_speed_multiplier,
)
from engine.game_state import GameState, persistent_id
from engine.inventory import CATEGORY_LABELS, Inventory, ItemDef, load_item_defs
from engine.menu import (
    BUY, InventoryMenu, ItemActionMenu, PartyMenu, SaveMenu, SettingsMenu, ShopMenu, StartMenu,
)
from engine.movement import step_continuous
from engine.npc import DialogueVariant, load_npc_defs, load_npc_sprite_specs, parse_dialogue, validated_colors
from engine.player import Player, PlayerState
from engine.roster import PartyMember, Roster
from engine.save import load_from_slot, slot_exists
from procgen.visualizer import EFFECTS

_REPO_ROOT   = Path(__file__).parent.parent
_ASSETS      = _REPO_ROOT / 'assets'
_MAPS_DIR    = _REPO_ROOT / 'data' / 'maps'
_HUB_MAP     = _MAPS_DIR / 'hub_fronthouse' / 'hub_fronthouse.json'
_SPRITES_PNG = _ASSETS / 'sprites' / 'party_sprites.png'
_SPRITES_TXT = _ASSETS / 'sprites' / 'partysprites.txt'
_ENEMY_SPRITES_DIR = _ASSETS / 'sprites' / 'enemies'
# An NPC sprite strip normally lives in assets/sprites/npcs/, but a handful
# (e.g. melvin_sleep, a one-off pose for a cutscene-only NPC placement)
# live in assets/sprites/party/ instead, alongside the rest of that
# character's other party art -- see load_npc_sprites, which merges both
# directories keyed by filename stem rather than requiring every NPC
# strip's PNG to physically live under npcs/ even when it's really party
# art. Order matters only on a filename collision -- npcs/ wins.
_NPC_SPRITES_DIRS = [_ASSETS / 'sprites' / 'party', _ASSETS / 'sprites' / 'npcs']
_BATTLE_ART_DIR = _ASSETS / 'tiles' / 'enemies'
_PARTY_DIR   = _REPO_ROOT / 'data' / 'party'
_MENU_DIR    = _ASSETS / 'menus'
_FX_TRANSITIONS_PNG = _ASSETS / 'fx' / 'transitions.png'

_NPC_ANIM_MS = 500   # ms per NPC animation frame flip
_NPC_MOVE_MS = 800   # ms between NPC AI position updates; also NPC tween duration

_BATTLE_IMMUNITY_MS = 2000   # after a battle ends, how long before touching
                              # an enemy can trigger another one -- the player
                              # is returned to the exact tile they were
                              # grabbed on, likely still overlapping it
_BATTLE_IMMUNITY_BLINK_MS = 100   # how often the player sprite toggles
                                   # visible/hidden during the immunity window

_BATTLE_TRANSITION_ANIM_MS  = 2000   # 8 frames over this long -- see
                                      # OverworldScene._tick_battle_transition
_BATTLE_TRANSITION_HOLD_MS  = 500    # solid black after the animation, before
                                      # BattleScene actually appears
_BATTLE_TRANSITION_ANIM_COUNT = 3    # rows of assets/fx/transitions.png that
                                      # are actually finished -- bump by hand
                                      # as more get added; never lets a random
                                      # pick exceed however many rows the file
                                      # actually has, even if this constant
                                      # drifts ahead of it (see start_battle)

_GAME_OVER_TEXT_COLOR = (255, 255, 255)

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

# Controller support (see joytest.py, run against an 8BitDo FC30 over
# Bluetooth) — the D-pad reports as a fully-deflected analog axis rather
# than a hat (axis 0: -1/W, +1/E; axis 1: -1/N, +1/S), and A/B/START are
# plain digital buttons. Both translate to the exact same pygame key codes
# _DIR_KEY/_BUTTON_KEY already know, via _joy_axis_key_events/_JOY_BUTTON_KEY
# below in run() — so a controller is just another source of the same
# KEYDOWN/KEYUP events real keyboard input produces, with no duplicate
# handling anywhere else in this file.
_JOY_AXIS_KEY = {
    (0, -1): pygame.K_LEFT,
    (0, 1):  pygame.K_RIGHT,
    (1, -1): pygame.K_UP,
    (1, 1):  pygame.K_DOWN,
}
_JOY_AXIS_DEADZONE = 0.5

_JOY_BUTTON_KEY = {
    0: pygame.K_z,       # B
    1: pygame.K_x,       # A
    7: pygame.K_RETURN,  # START
    # 6 (SELECT) deliberately unmapped — reserved for future debug controls
    # (live-reload, load save, etc.), not real gameplay input. Wire it up
    # when that's actually built rather than guessing its scope now.
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

# Shop-screen layout. Same "no art yet, plain dialogue_font on black"
# approach as inventory/settings above -- the greeting/farewell are their
# own dialogue-box beats (see engine.menu.ShopMenu.pending_shop), so this
# screen opens straight on a BUY/SELL mode header (same "< X >" idiom as
# the inventory header), then the current mode's scrollable list.
_SHOP_MARGIN      = 8
_SHOP_HEADER_Y    = _SHOP_MARGIN
_SHOP_LIST_TOP_Y  = 40
_SHOP_LIST_LEFT_X = _SHOP_MARGIN
_SHOP_TEXT_COLOR  = (255, 255, 255)
_SHOP_DIM_COLOR   = (110, 110, 110)

# Item-action popup layout: a small box drawn on top of the inventory
# screen (see _draw_item_action_menu), bottom-right corner so it never
# covers the list/detail panes above it. Same "no art yet" idiom as
# everything else -- solid rect + dialogue_font.
_ITEMACT_W          = 96
_ITEMACT_H          = 64
_ITEMACT_MARGIN     = 6
_ITEMACT_TEXT_COLOR = (255, 255, 255)
_ITEMACT_DIM_COLOR  = (110, 110, 110)

# Party-screen layout. Same split as the inventory screen: left pane lists
# party members (N/S, wraps -- a fixed small roster, not an open-ended
# list, so wrap reads fine here unlike the inventory's clamp), right pane
# is the selected member's stat detail.
_PARTY_MARGIN       = 8
_PARTY_HEADER_Y     = _PARTY_MARGIN
_PARTY_LIST_TOP_Y   = 24
_PARTY_LIST_LEFT_X  = _PARTY_MARGIN
_PARTY_DIVIDER_X    = 80   # column of "|" glyphs separating list from detail pane
_PARTY_DETAIL_LEFT_X = _PARTY_DIVIDER_X + 12
_PARTY_TEXT_COLOR   = (255, 255, 255)
_PARTY_DIM_COLOR    = (110, 110, 110)

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
_DIALOGUE_CHOICE_DIM_COLOR = (110, 110, 110)   # unselected response row -- see _draw_dialogue_box


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


def _joy_axis_key_events(event, axis_held: dict[int, int | None]) -> list[pygame.event.Event]:
    """Translate one JOYAXISMOTION event into the matching synthetic
    KEYDOWN/KEYUP event(s) via _JOY_AXIS_KEY, mutating `axis_held` (per-axis
    currently-pressed key, keyed by axis index) so a release — or a direct
    snap to the opposite direction on the same axis — emits the correct
    KEYUP first. Needed because a physical axis has no separate "up"
    event of its own the way a button does; the direction is only implied
    by its value crossing back through _JOY_AXIS_DEADZONE toward 0."""
    axis = event.axis
    prev_key = axis_held.get(axis)
    events = []
    if abs(event.value) > _JOY_AXIS_DEADZONE:
        sign = 1 if event.value > 0 else -1
        new_key = _JOY_AXIS_KEY.get((axis, sign))
        if new_key is not None and new_key != prev_key:
            if prev_key is not None:
                events.append(pygame.event.Event(pygame.KEYUP, key=prev_key))
            events.append(pygame.event.Event(pygame.KEYDOWN, key=new_key))
            axis_held[axis] = new_key
    elif prev_key is not None:
        events.append(pygame.event.Event(pygame.KEYUP, key=prev_key))
        axis_held[axis] = None
    return events


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
    # USER IS UNHAPPY WITH RENDERER HANDLING THIS BUT REAL TALK ITS BASICALLY
    # THE MAIN ENGINE SCRIPT IT JUST HAS A GRAPHICAL SOUNDING NAME.
    pygame.joystick.init()
    # Reference kept alive for the life of run() -- an unstored Joystick()
    # object gets garbage-collected almost immediately, which closes the
    # underlying device with it: the subsystem stays "initialized" but no
    # JOYAXISMOTION/JOYBUTTONDOWN events ever actually arrive.
    joysticks = [pygame.joystick.Joystick(i) for i in range(pygame.joystick.get_count())]
    for js in joysticks:
        js.init()

    view_w, view_h = view_size
    current_scale = scale
    current_fullscreen = cfg.start_fullscreen
    window, win_w, win_h = _set_display_mode(view_w, view_h, current_scale, current_fullscreen)
    pygame.display.set_caption(title)
    screen = pygame.Surface((view_w, view_h))  # native-res render target

    scene = scene_factory()

    clock = pygame.time.Clock()
    joy_axis_held: dict[int, int | None] = {}   # axis index -> currently-synthesized key
    running = True
    while running:
        dt = clock.tick(60)  # ms since last frame; caps at 60 fps

        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key in (pygame.K_ESCAPE, pygame.K_q):
                running = False
            elif event.type == pygame.JOYAXISMOTION:
                for synthetic in _joy_axis_key_events(event, joy_axis_held):
                    scene.handle_event(synthetic)
            elif event.type == pygame.JOYBUTTONDOWN:
                key = _JOY_BUTTON_KEY.get(event.button)
                if key is not None:
                    scene.handle_event(pygame.event.Event(pygame.KEYDOWN, key=key))
            elif event.type == pygame.JOYBUTTONUP:
                key = _JOY_BUTTON_KEY.get(event.button)
                if key is not None:
                    scene.handle_event(pygame.event.Event(pygame.KEYUP, key=key))
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
# single horizontal row of frames (2 today, a south-facing walk cycle).
# Keyed by filename stem, matching the `sprite` field in data/enemy/enemies.yaml.
#
# Frame width isn't hardcoded to tile_px -- only 2-frame and 8-frame strips
# are authored (see engine.npc's module docstring), so frame count is
# inferred by trying 8 first, then 2: whichever evenly divides the sheet
# width into a tile_px-multiple frame width wins. 8 is tried first because
# it's the narrower-frame reading -- a 128px-wide sheet must stay the
# existing 8-frame x 16px-wide walk cycle (wizard/poots/gnome/roach2 all
# depend on this), not get reinterpreted as 2 frames x 64px wide. A sheet
# that matches neither (e.g. some future one-off) falls back to the old
# fixed-tile_px-width slicing so it degrades exactly like before this
# inference existed.

def load_enemy_sprites(sprites_dir: Path, tile_px: int = 16) -> dict[str, list[pygame.Surface]]:
    """Slice every PNG in `sprites_dir` into a list of animation frames,
    keyed by filename stem. Frame width is inferred (see comment above) --
    16px for the ordinary one-tile-wide sprites, a multiple of tile_px for a
    multi-tile-wide one (e.g. 64x32 -- 2 frames, each 32x32). Frame height is
    the sheet's own height (always a multiple of tile_px), not hardcoded to
    tile_px -- a sheet taller than one tile is a normal tile_px-tall frame
    (bottom rows) with extra rows stacked above it (e.g. a hat/head overhang
    on an NPC). See _draw_entities, which anchors the bottom-left tile_px x
    tile_px cell to the object's tile and lets any extra height extend
    upward and any extra width extend rightward. Call after
    pygame.display.set_mode()."""
    frames: dict[str, list[pygame.Surface]] = {}
    for path in sorted(sprites_dir.glob('*.png')):
        sheet = pygame.image.load(str(path)).convert_alpha()
        width, height = sheet.get_size()
        frame_w = tile_px
        count = width // tile_px
        for candidate_count in (8, 2):
            candidate_w = width / candidate_count
            if candidate_w == int(candidate_w) and int(candidate_w) % tile_px == 0:
                frame_w, count = int(candidate_w), candidate_count
                break
        frames[path.stem] = [
            sheet.subsurface(pygame.Rect(i * frame_w, 0, frame_w, height)).copy()
            for i in range(count)
        ]
    return frames


def load_transition_frames(path: Path, tile_px: int = 16) -> list[list[pygame.Surface]]:
    """Slice assets/fx/transitions.png into rows of 8 tile_px-wide frames --
    one row per animated screen transition (see OverworldScene.
    start_battle/_draw_battle_transition_overlay). `.convert_alpha()` is
    load-bearing here, same as load_enemy_sprites -- these frames dissolve
    from mostly-transparent toward opaque black across their 8 frames, and
    a `.convert()` (dropping alpha) would make every frame opaque instead,
    silently turning the whole transition into a hard cut to black on
    frame 1. Slices every row present in the file; how many are actually
    finished/usable is a separate, hand-maintained count -- see
    _BATTLE_TRANSITION_ANIM_COUNT."""
    sheet = pygame.image.load(str(path)).convert_alpha()
    width, height = sheet.get_size()
    cols, rows = width // tile_px, height // tile_px
    return [
        [sheet.subsurface(pygame.Rect(col * tile_px, row * tile_px, tile_px, tile_px)).copy()
         for col in range(cols)]
        for row in range(rows)
    ]


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
# count (inferred by load_enemy_sprites -- 8 tried before 2) decides how
# get_npc_frame animates it: 2 frames is a facing-less south-only idle loop,
# 8 is a full walk cycle in the party sheet's own S1,S2,N1,N2,W1,W2,E1,E2
# order, and does respond to facing.
#
# A strip's height, independently of frame count, may be any multiple of
# tile_px -- e.g. 128x32 is still the 8-frame walk cycle above, just with
# each frame 32px tall instead of 16. Likewise a strip's *width per frame*
# may be any multiple of tile_px -- e.g. manager is 64x32 (2 frames, each
# 32x32 -- a 2-tile-wide, 2-tile-tall idle loop). Either way, the bottom-left
# tile_px x tile_px cell of each frame is the ordinary body, treated exactly
# like a 16x16 sprite; extra rows above it are a plain overhang (a hat, a
# tall hood, ...) and extra columns to its right are the rest of a
# wider-than-one-tile character -- neither has any separate animation or
# logic of its own. A map's npc/shop object's row/col anchors the sprite's
# bottom-*left* tile -- i.e. place the object where you want that lower-left
# 16x16 cell, and the rest of the sprite extends up and to the right from
# there. load_enemy_sprites slices the full sheet; _draw_entities anchors
# the bottom-left tile_px x tile_px cell to the object's tile and lets the
# rest extend upward/rightward; _passable_with_npcs and
# Player.adjacent_interactable both block/reach the NPC's whole footprint
# (NPC.width_span x NPC.height_span), not just that one anchor tile.
#
# Recoloring (see engine.npc's module docstring / NpcDef.colors /
# NpcSpriteSpec.colors): a sprite loads once here, unrecolored; per-NPC
# recolored copies are computed once at OverworldScene.__init__ time (see
# self.npc_frames_by_id), not per frame drawn -- recolor_surface below is
# only ever called there, never from draw().

def load_npc_sprites(dirs: list[Path], tile_px: int = 16) -> dict[str, list[pygame.Surface]]:
    """Same slicing rules as load_enemy_sprites (identical mechanism, just
    merged across more than one directory -- see _NPC_SPRITES_DIRS above
    for why an NPC sprite strip isn't always under assets/sprites/npcs/).
    A later directory's file wins on a filename-stem collision."""
    frames: dict[str, list[pygame.Surface]] = {}
    for d in dirs:
        frames.update(load_enemy_sprites(d, tile_px))
    return frames


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
    """A container/sign/healer/npc/warp placed on the map's Tiled object
    layer, merged with its hand-authored content from obj_<map>.yaml /
    npcs_<map>.yaml (see data/maps/populate_yamls.py). `dialogue` is a list
    of pages; `type == "sign"` opens it directly, `type == "healer"` (e.g. a
    saladbar) fully heals the active party (engine.roster.Roster) to max
    HP/MP for free, every visit, no flag, then opens the same way with a
    synthesized "restored" page appended (see engine.input.handle_a_button),
    `type == "container"` opens it via engine.input's container handling
    (dialogue plus an item grant — see `contents` below), and `type ==
    "npc"` entries hand theirs off to NPC (see below, built from these in
    OverworldScene._load_map) which is interactable the same way.
    `sprite`/`behavior`/`npc_id`/`colors` are
    only ever set for `type == "npc"` entries — `npc_id`, if set, is a key
    into engine.npc.load_npc_defs()'s result (data/npcs/npc.yaml); this
    object's own `sprite`/`behavior`/`dialogue`/`colors` are then optional
    overrides on top of that shared definition — set, they win for this one
    placement, left blank (None / empty list) they fall back to the
    NpcDef's — resolved in OverworldScene._load_map, not here (this class
    just carries whatever was in the YAML, unmerged). `colors` is
    position-matched against the resolved sprite's own placeholder colors,
    same rule as engine.npc.NpcDef.colors, but wins outright over the
    NpcDef's `colors` for just this one placement instead of every
    placement sharing that npc_id — see engine.npc's module docstring and
    OverworldScene._resolve_npc_frames.
    `destination_map`/`destination_warp`/`facing`/`distance` are only ever
    set for `type == "warp"` entries — see
    OverworldScene._update_player_movement/_swap_map. `facing`/`distance` do double
    duty: they're this warp's own landing spot when something else's
    destination_warp points here (`distance` tiles from this warp's row/col,
    offset in the `facing` direction; distance 0 or unset lands exactly on
    it) *and* the direction the player faces once they land.

    `cutscene_id` is only ever set for `type == "trigger"` entries: an
    invisible, one-tile trigger zone modeled directly on `warp` (edge-
    triggered the same way -- see OverworldScene._trigger_at/
    _try_start_tile_trigger/_update_player_movement) except it starts a
    cutscene instead of a map transition. The id is a key into
    OverworldScene.cutscene_defs; that cutscene's own `trigger.event` must
    be "tile" (checked at fire time, not here) -- this object only says
    *which* cutscene to check, not that it unconditionally plays, since the
    cutscene's own `when`/`unless` still has to pass.

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
    item id from data/items/items.yaml, or None for no item loot. `gold` is
    also only ever set for `type == "container"` entries: a flat gold
    amount credited via engine.game_state.GameState.add_gold, or None for
    no gold. The two are independent -- a container can grant an item, gold,
    both, or neither (pure flavor, dialogue only). See engine.input's
    container handling for how opening one grants either/both; either way
    (loot or not), opening a container sets a flag keyed by
    engine.game_state.persistent_id(map_name, this object's name) — every
    container is single-use, one open. That same flag is what
    OverworldScene._draw_entities checks to stop drawing the container's
    `gid` once opened: paint whichever tile should show through once it's
    empty (a used-up trash can, an opened chest, ...) on the tile layer
    beneath the container object, and it's revealed for free the instant
    the flag's set — no separate "opened" sprite or state field.

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
    engine.enemy for the runtime `Enemy` these become.

    `stock`/`farewell` are only ever set for `type == "shop"` entries -- a
    shop is a derivative of `npc` (same sprite/behavior/npc_id/dialogue
    fields, same row in npcs_<map>.yaml, same self.npcs list and
    engine.npc.NPC runtime object -- see OverworldScene._load_map), not a
    static object, so it's excluded from self.objects the same way `npc` is.
    `stock` is a list of `{item, price}` dicts, `item` a key into
    data/items/items.yaml and `price` the buy cost in gold -- independent of
    that item's own `value` (the sell price a shop pays out, see
    engine.inventory.ItemDef). `dialogue` is the shopkeeper's one-line
    greeting, shown via a normal dialogue box before the buy/sell screen
    opens (see engine.input.handle_a_button); `farewell` is the one-line
    goodbye shown the same way after the player backs out of the buy/sell
    screen. Unlike a container, a shop has no one-shot flag -- both
    greeting and buy/sell screen are available every visit."""
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
    gold:     int | None = None   # type == "container" only: flat gold grant, independent of contents
    sprite:   str | None = None
    behavior: str | None = None
    npc_id:   str | None = None
    colors:   list[str] | None = None   # type == "npc"/"shop" only: per-placement recolor override, see docstring above
    destination_map:  str | None = None
    destination_warp: str | None = None
    facing:   str | None = None
    distance: int | None = None
    cutscene_id:  str | None = None   # type == "trigger" only: which cutscene to check on step-on
    enemy_id:     str | None = None
    level:        int | list[int] | None = None
    enemies:      list = field(default_factory=list)
    spawn_chance: float | None = None
    stock:        list[dict] = field(default_factory=list)   # type == "shop" only: [{item, price}, ...]
    farewell:     list[str] = field(default_factory=list)    # type == "shop" only: goodbye line, shown on close


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
            # Empty-list vs unset both mean "no override" -- collapse `[]`
            # to None here so downstream resolution (OverworldScene.
            # _resolve_npc_frames) only ever has to check one falsy case,
            # same normalization npc.yaml's own sprite/NpcDef colors get.
            raw_colors = data.get("colors") or None
            colors = (validated_colors(raw_colors, f"{map_name}/npcs_{map_name}.yaml id {obj['id']}.colors")
                      if raw_colors else None)
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
                gold      = data.get("gold"),
                sprite    = data.get("sprite"),
                behavior  = data.get("behavior"),
                npc_id    = data.get("npc_id"),
                colors    = colors,
                destination_map  = data.get("destination_map"),
                destination_warp = data.get("destination_warp"),
                facing    = data.get("facing"),
                distance  = data.get("distance"),
                cutscene_id  = data.get("cutscene_id"),
                enemy_id     = data.get("enemy_id"),
                level        = data.get("level"),
                enemies      = data.get("enemies", []),
                spawn_chance = data.get("spawn_chance"),
                stock        = data.get("stock", []),
                farewell     = data.get("farewell", []),
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
    """A roaming/static character built from a map's `type == "npc"` OR
    `type == "shop"` objects merged with npcs_<map>.yaml (see MapObject,
    which this is built from in OverworldScene._load_map) — a shop is a
    person (a shopkeeper) first, so it's built from exactly the same
    pipeline as any other NPC, not a separate static-object type.
    `sprite`/`behavior`/`facing`/`dialogue` are already fully resolved by
    the time an NPC exists — that MapObject's own fields win if set, else
    whatever its `npc_id` points at in engine.npc.load_npc_defs()'s result
    (data/npcs/npc.yaml), else the static "no def, nothing set" fallback
    (`behavior="static"`, `facing="S"`, empty dialogue) — see _load_map.
    `type` is "npc" or "shop", carried straight from the source MapObject —
    Player.adjacent_interactable()/handle_a_button branch on it the same
    way they do for MapObject, without needing to know NPC is a distinct
    class. `dialogue` works exactly like a sign's: face the NPC and press A
    to open it — see handle_a_button. For a shop, that same greeting closes
    into the buy/sell screen instead of back to idle — see
    engine.menu.ShopMenu.pending_shop / handle_a_button.

    `npc_id` (distinct from `npc_sprite`) is only ever set when this NPC
    resolved against a real def — draw-time (OverworldScene._draw_entities)
    uses it to pick that def's precomputed *recolored* frame set
    (self.npc_frames_by_id) instead of the sprite's raw one; an inline NPC
    with no def (npc_id stays None) always draws its sprite unrecolored,
    unless `colors` (below) is set.

    `colors`, if set, is this placement's own npcs_<map>.yaml `colors:`
    override (MapObject.colors), already resolved by _load_map -- when
    present it wins outright over npc_id's def-level recolor (see
    engine.npc's module docstring), position-matched against the resolved
    sprite's own placeholder colors. OverworldScene._npc_frames/
    _resolve_npc_frames key a separate cache off (npc_sprite, colors) for
    this case, same "recolor once, not per frame drawn" rule as the
    npc_id-level cache.

    `width_span`/`height_span` are this NPC's resolved sprite's frame size in
    tiles (1x1 for an ordinary 16x16 sprite) — resolved once at construction
    time in _load_map, from the same frame set _npc_frames would resolve for
    it, so they can't drift out of sync with what actually gets drawn.
    `row`/`col` anchor the sprite's bottom-*left* tile, so its footprint is
    columns `[col, col + width_span)` and rows `(row - height_span, row]`;
    _passable_with_npcs and Player.adjacent_interactable both use this full
    footprint, not just the anchor tile, so a wider-than-one-tile NPC (e.g.
    manager, 2x2) blocks and can be talked to from any tile it visually
    covers.

    `dialogue_variants` (engine.npc.DialogueVariant list) is the
    placement's own npcs_<map>.yaml `dialogue:` if it set one (parsed via
    engine.npc.parse_dialogue — a flat page list or a conditional variant
    list, same rule as the def), else the resolved def's own `dialogue`,
    else empty — same override-or-fall-back-to-def rule as sprite/behavior,
    see _load_map. engine.input.handle_a_button resolves this against live
    engine.game_state flags at interact time, not baked into a flat list
    here, since flags can change between one visit and the next — see
    engine.npc.resolve_dialogue.

    `stock`/`farewell_variants` are only ever set for `type == "shop"`
    entries: `stock` a list of `{item, price}` dicts (see
    engine.menu.ShopMenu / engine.input._confirm_shop_transaction),
    `farewell_variants` the goodbye line shown (same resolve_dialogue rule
    as `dialogue_variants`) after the player backs all the way out of the
    buy/sell screen — placement-only, no npc.yaml-level fallback, since a
    farewell is authored per-shopkeeper, not shared across placements.
    """
    index:      int                 # stable identity (Tiled object id) for tweens
    name:       str
    row:        int
    col:        int
    npc_sprite: str | None          # e.g. "npc01" -- key into engine.npc.load_npc_sprite_specs()'s result
    behavior:   str = "static"      # "static" | "wander"
    facing:     str = "S"           # "N" | "S" | "E" | "W" -- only visible on an 8-frame sprite
    type:       str = "npc"         # "npc" | "shop"
    npc_id:     str | None = None   # key into OverworldScene.npc_defs/npc_frames_by_id, if resolved
    colors:     list[str] | None = None   # this placement's own recolor override, if set -- see docstring above
    width_span:  int = 1            # tiles wide, resolved sprite's frame width // tile_px -- see _load_map
    height_span: int = 1            # tiles tall, resolved sprite's frame height // tile_px -- see _load_map
    dialogue_variants: list[DialogueVariant] = field(default_factory=list)
    stock:             list[dict] = field(default_factory=list)             # type == "shop" only
    farewell_variants: list[DialogueVariant] = field(default_factory=list)   # type == "shop" only


# ── Scene ────────────────────────────────────────────────────────────────────

class OverworldScene:
    """A single walkable screen: tile grid, player, roaming NPCs.

    Implements the handle_event/update/draw protocol expected by
    engine.renderer.run(). Must be constructed after pygame.display.set_mode()
    so tileset/sprite loading can call .convert_alpha().
    """

    def __init__(self, map_path: Path = _HUB_MAP, player: Player | None = None,
                 inventory: Inventory | None = None, game_state: GameState | None = None,
                 roster: Roster | None = None, active_slot: int = 1,
                 play_cutscene: str | None = None):
        """`player`/`inventory`/`game_state`/`roster`, if given, are what a
        save slot restores (see engine.save / maptest.py's save-slot picker)
        — omit all four for a fresh boot with no save (maptest.py's
        map-picker flow: default Player, empty Inventory, empty GameState,
        fresh Roster). `spawn` is
        only overridden from the loaded player's own row/col when a save
        was actually passed in; a fresh boot keeps the existing debug
        convenience of landing on a random warp (see _load_map) rather than
        Player.default()'s meaningless (0, 0) placeholder. `active_slot` is
        only used by SAVE on the start menu, to know which slot to write.
        `play_cutscene`, if given, starts that cutscene id (see
        engine.cutscene) immediately once the map's loaded -- maptest.py's
        debug `cutscene` mode uses this to play one in isolation, no save
        involved, the same spirit as its `battles` mode.
        """
        self.tile_px = cfg.tile_px
        self.player_move_ms = cfg.player_move_ms   # ms to cross one tile at normal speed
        self.player_speed = 1000.0 / self.player_move_ms   # tiles/sec, for continuous movement

        self.sprites = load_sprites(_SPRITES_PNG, _SPRITES_TXT, self.tile_px)
        self.enemy_sprites = load_enemy_sprites(_ENEMY_SPRITES_DIR, self.tile_px)
        self.menu_assets = load_menu_assets(_MENU_DIR)
        self.transition_frames = load_transition_frames(_FX_TRANSITIONS_PNG, self.tile_px)
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
        self.cutscene_defs = load_cutscene_defs()   # data/cutscenes/*.yaml -- see engine.cutscene
        self.npc_sprite_specs = load_npc_sprite_specs()
        self.npc_sprites = load_npc_sprites(_NPC_SPRITES_DIRS, self.tile_px)   # raw, unrecolored, by filename stem

        missing_npc_sprites = sorted({spec.file for spec in self.npc_sprite_specs.values()
                                      if spec.file not in self.npc_sprites})
        if missing_npc_sprites:
            print(f'WARNING: npc.yaml references sprite file(s) with no matching PNG in '
                  f'assets/sprites/npcs/ or assets/sprites/party/: {missing_npc_sprites} -- those '
                  f'NPCs will place but render as nothing until a strip PNG with that filename '
                  f'stem is added to one of those two directories',
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

        # Per-placement recolor overrides (NPC.colors / MapObject.colors,
        # npcs_<map>.yaml's own `colors:`) are rarer than the def-level case
        # above and not known until _load_map resolves each placement, so
        # this cache fills in lazily there instead of eagerly here -- keyed
        # by (sprite id, target colors) rather than npc_id, since the same
        # override can be shared across several placements (or npc_ids) of
        # the same sprite. See _resolve_npc_frames.
        self.npc_color_override_frames: dict[tuple, list[pygame.Surface]] = {}

        self.menu = StartMenu()
        self.save_menu = SaveMenu()   # confirm-save overlay, opened from SAVE on self.menu
        self.inventory_menu = InventoryMenu()   # opened from INVENTORY on self.menu
        self.settings_menu = SettingsMenu(scale=cfg.pygame_scale, fullscreen=cfg.start_fullscreen)   # opened from SETTINGS on self.menu
        self.party_menu = PartyMenu()   # opened from PARTY on self.menu
        self.item_action_menu = ItemActionMenu()   # opened from a row on self.inventory_menu
        self.shop_menu = ShopMenu()   # opened directly from the overworld by facing a `shop` object
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
        self.roster = roster if roster is not None else Roster.fresh()

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

        # A/B held state, tracked independently of HeldDirectionInput —
        # read for two things: typing an open dialogue box in faster (see
        # update()'s dialogue branch), and hold-B-to-run via
        # engine.input.held_button_speed_multiplier (see
        # _update_player_movement) — not for repeat-stepping.
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

        # Battle state -- see start_battle/_end_battle/_update_enemies.
        # battle_scene is None whenever no battle is active; while it isn't,
        # handle_event/update/draw all delegate to it wholesale (see those
        # methods) rather than swapping the top-level scene (engine.renderer.
        # run() has no such mechanism, and this mirrors the warp transition
        # above -- one self-contained state machine per concern, all living
        # inside this one OverworldScene instance).
        self.battle_scene: 'BattleScene | None' = None
        self._battle_source_enemy: 'Enemy | None' = None
        self._battle_immunity_ms = 0   # counts down after a battle ends -- see _update_enemies
        self._immunity_blink_elapsed = 0
        self._immunity_blink_visible = True

        # Battle-entry transition -- see start_battle/_tick_battle_transition/
        # _draw_battle_transition_overlay. None means no transition is in
        # progress; battle_scene stays None the whole time this is active --
        # it's only actually constructed once the transition completes.
        # _pending_battle stashes start_battle's args until then.
        self._battle_transition_phase: str | None = None   # None | "anim" | "hold"
        self._battle_transition_elapsed = 0
        self._battle_transition_frame_idx = 0
        self._battle_transition_frames: list[pygame.Surface] | None = None
        self._pending_battle: dict | None = None

        # Game-over state -- see _start_game_over/_reload_last_save. A full
        # pause, same tier as the battle/transition/dialogue/menu checks in
        # update()/handle_event()/draw().
        self._game_over = False

        # Cutscene state -- see start_cutscene/_tick_cutscene/_end_cutscene.
        # cutscene_player is None whenever no cutscene is active; while it
        # isn't, update()/handle_event() delegate to it wholesale, same
        # full-pause pattern as battle_scene/dialogue/menu above -- the
        # world doesn't wander/spawn enemies/accept movement input under a
        # running cutscene. _cutscene_spawned_ids tracks which of self.npcs
        # this cutscene itself added (see _cutscene_step_spawn_actor) so
        # _end_cutscene knows to remove exactly those and no others --
        # cutscene-spawned actors are always temporary, per the build plan.
        self.cutscene_player: CutscenePlayer | None = None
        self._cutscene_armed_index: int | None = None   # which step index setup has already run for
        self._cutscene_wait_remaining = 0.0
        self._cutscene_camera_pan: dict | None = None
        self._cutscene_spawned_ids: set[int] = set()
        self._cutscene_next_actor_index = -1   # negative, so it can never collide with a real Tiled object id
        self._camera_override: tuple[float, float] | None = None   # see _camera_offset_px / _cutscene_step_pan_camera

        # Cutscene fade overlay -- see _cutscene_step_fade/_draw_cutscene_fade.
        # Distinct from _transition_phase/_fade_overlay above (the fixed-
        # color, fixed-duration warp fade): this one is fully author-
        # controlled -- any color, any duration -- and, per the fade step's
        # own semantics, does NOT auto-clear once its tween finishes: a
        # fade-out holds at full opacity until a later fade-in step ramps it
        # back down, or the whole cutscene ends (see _end_cutscene). alpha
        # is 0..255; 0 means "draw nothing," so draw() can skip the blit
        # entirely whenever no fade is in effect.
        self._cutscene_fade_overlay = pygame.Surface((cfg.view_cols * self.tile_px, cfg.view_rows * self.tile_px))
        self._cutscene_fade_alpha = 0.0
        self._cutscene_fade_see_through = False
        self._cutscene_fade_anim: dict | None = None   # in-progress tween, see _cutscene_step_fade

        spawn = (self.player.row, self.player.col) if self._loaded_save else None
        facing = self.player.facing if self._loaded_save else None
        self._load_map(map_path, spawn=spawn, facing=facing)
        if play_cutscene is not None:
            self.start_cutscene(play_cutscene)

    def _load_map(self, map_path: Path, tmap: 'TiledMap | None' = None,
                   spawn: tuple[int, int] | None = None, facing: str | None = None,
                   check_triggers: bool = True) -> None:
        """Load (or, mid-game via a warp, reload) everything specific to one
        map: the Tiled map + tileset, passability, objects/warps/NPCs, and
        the player's spawn tile. Pygame resources that don't vary by map
        (sprites, menu/dialogue assets) are loaded once in __init__ instead
        and untouched here.

        `check_triggers` gates a "map_load"-triggered cutscene check (see
        _check_cutscene_triggers) at the very end, once passability/objects/
        NPCs/warps/spawn are all in place -- on for every real arrival
        (fresh boot, warp, game-over revert), off only for the dev-only R-key
        hot-reload (same map, same position, not a real "arrival").

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
                        if o.type not in ("npc", "shop", "warp", "enemy", "spawner", "trigger")]   # containers/signs
        self.warps = [o for o in self.tmap.objects if o.type == "warp"]
        # Invisible, one-tile cutscene trigger zones -- not drawn/A-press
        # interactable (excluded from self.objects above), same as warps;
        # see _trigger_at/_try_start_tile_trigger/_update_player_movement.
        self.triggers = [o for o in self.tmap.objects if o.type == "trigger"]
        # A shop is a person first -- built from the exact same objects as
        # any other NPC (npcs_<map>.yaml, sprite/behavior/npc_id, drawn by
        # _draw_entities the same way), not a separate static object type.
        npc_map_objects = [o for o in self.tmap.objects if o.type in ("npc", "shop")]
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
            resolved_npc_id = o.npc_id if npc_def else None
            resolved_sprite = o.sprite or (npc_def.sprite if npc_def else None)
            # width_span/height_span: this placement's footprint in tiles,
            # resolved from the same frame set _npc_frames would resolve for
            # it once the NPC exists -- computed here (instead of lazily,
            # e.g. in _passable_with_npcs) so it's set once at load and both
            # collision and interaction read the same stored numbers rather
            # than re-deriving them from pixel dimensions in two places.
            resolved_frames = self._resolve_npc_frames(resolved_npc_id, resolved_sprite, o.colors)
            width_span = (resolved_frames[0].get_width() // self.tile_px) if resolved_frames else 1
            height_span = (resolved_frames[0].get_height() // self.tile_px) if resolved_frames else 1
            self.npcs.append(NPC(
                index=o.id, name=o.name, row=round(o.row_exact), col=round(o.col_exact),
                npc_sprite=resolved_sprite,
                behavior=o.behavior or (npc_def.behavior if npc_def else None) or "static",
                facing=npc_def.facing if npc_def else "S",
                type=o.type,
                npc_id=resolved_npc_id,
                colors=o.colors,
                width_span=width_span,
                height_span=height_span,
                dialogue_variants=dialogue_variants,
                stock=o.stock,
                farewell_variants=parse_dialogue(o.farewell),
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

        if check_triggers:
            self._check_cutscene_triggers('map_load')

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

    def _resolve_shop(self) -> 'NPC | None':
        return next((n for n in self.npcs if n.name == self.shop_menu.shop_name), None)

    def _shop_amount_context(self, shop: 'NPC') -> tuple[int, int]:
        """(list_len, max_amount) for whichever mode self.shop_menu is
        currently in -- shared by the direction-key routing in
        handle_event and the "x{amount} TOTAL" line in _draw_shop_menu."""
        if self.shop_menu.mode == BUY:
            if not shop.stock:
                return 0, 0
            price = shop.stock[self.shop_menu.cursor]["price"]
            return len(shop.stock), (self.game_state.gold // price if price > 0 else 0)
        sellable = self.inventory.sellable_items(self.item_defs)
        if not sellable:
            return 0, 0
        owned = self.inventory.counts.get(sellable[self.shop_menu.cursor], 0)
        return len(sellable), owned

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYUP:
            # Always resolve a physical key release, regardless of which
            # gate below would otherwise swallow the event -- otherwise a
            # direction key released while the battle-transition/battle/
            # game-over gates are active never reaches self._input.release,
            # and the player keeps "walking" in that direction once control
            # returns (stuck facing/moving whatever was held the instant an
            # enemy touched them), since HeldDirectionInput still thinks the
            # key is down.
            direction = _DIR_KEY.get(event.key)
            if direction:
                self._input.release(direction)
            button = _BUTTON_KEY.get(event.key)
            if button:
                self._held_buttons.discard(button)
            return

        if self._battle_transition_phase is not None:
            return   # entering battle -- swallow input entirely, see start_battle

        if self.battle_scene is not None:
            self.battle_scene.handle_event(event)
            return

        if self._game_over:
            if event.type == pygame.KEYDOWN and _BUTTON_KEY.get(event.key) == 'A':
                self._reload_last_save()
            return

        if self.cutscene_player is not None:
            self._handle_cutscene_event(event)
            return

        if event.type == pygame.KEYDOWN:
            # DEV-ONLY: hot-reload the current map's JSON/tileset off disk
            # without restarting the process, so map edits can be checked
            # live. Not a shipping feature -- cut this before release.
            if event.key == pygame.K_r and self._transition_phase is None:
                self._load_map(self.map_path,
                                spawn=(self.player.row, self.player.col),
                                facing=self.player.facing,
                                check_triggers=False)
                return

            direction = _DIR_KEY.get(event.key)
            if direction:
                if self.player.state in (PlayerState.IN_MENU, PlayerState.IN_SHOP):
                    category_item_count = len(self.inventory.items_in(
                        self.inventory_menu.selected_category(), self.item_defs))
                    shop = self._resolve_shop() if self.shop_menu.is_open else None
                    shop_list_len, shop_max_amount = self._shop_amount_context(shop) if shop else (0, 0)
                    handle_menu_direction(direction, self.menu, self.save_menu,
                                           self.inventory_menu, self.settings_menu,
                                           self.shop_menu, self.item_action_menu, self.party_menu,
                                           category_item_count, len(self.roster.current_party(self.game_state)),
                                           shop_list_len, shop_max_amount)
                elif self.player.state == PlayerState.IN_DIALOGUE:
                    # Locked out entirely -- a direction press must not turn
                    # the player to face a new way while a dialogue box is
                    # up. Ordinary sign/NPC/container/shop dialogue never
                    # sets choices (see engine.dialogue's module docstring),
                    # so there's no N/S-cursor case to route here either --
                    # that only exists for a cutscene's dialogue step, which
                    # is handled entirely by _handle_cutscene_event instead
                    # (self.cutscene_player is not None already returns
                    # above, before this branch is ever reached).
                    pass
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
                                     self.inventory_menu, self.settings_menu, self.shop_menu,
                                     self.item_action_menu, self.party_menu)
            elif button == 'B':
                handle_b_button(self.player, self.menu, self.save_menu,
                                 self.inventory_menu, self.settings_menu, self.shop_menu,
                                 self.item_action_menu, self.party_menu,
                                 self.dialogue, self.npcs, self.game_state,
                                 wrap_pages=self._wrap_dialogue_pages)
            elif button == 'A':
                # Menu confirm (SAVE writes self.active_slot to disk and
                # closes back to the start menu; INVENTORY/SETTINGS/PARTY
                # open their overlays) / an inventory row opens the
                # item-action popup (USE/EQUIP/UNEQUIP, see
                # engine.input._item_action_options) / dialogue
                # advance-close (no-op while still typing in) / open a
                # dialogue by facing an interactable sign, NPC, or shopkeeper
                # (a shop is a person -- greeting first, buy/sell screen
                # opens once that closes, see engine.menu.ShopMenu.
                # pending_shop), or a container (grants its `contents` item
                # + sets its flag the first time, inert after that -- see
                # engine.input) / confirms a buy/sell row while the shop
                # screen itself is open -- see engine.input.handle_a_button.
                # A talk-triggered cutscene (event: npc_talk) preempts the
                # NPC/shop's ordinary dialogue -- see engine.input's
                # cutscene_defs param/cutscene_id_from_result.
                result = handle_a_button(self.player, self.menu, self.save_menu, self.inventory_menu,
                                          self.settings_menu, self.shop_menu,
                                          self.item_action_menu, self.party_menu,
                                          self.dialogue, self.npcs, self.objects,
                                          wrap_pages=self._wrap_dialogue_pages,
                                          game_state=self.game_state, inventory=self.inventory,
                                          roster=self.roster,
                                          item_defs=self.item_defs, map_name=self.map_path.stem,
                                          active_slot=self.active_slot,
                                          cutscene_defs=self.cutscene_defs)
                cutscene_id = cutscene_id_from_result(result)
                if cutscene_id is not None:
                    self.start_cutscene(cutscene_id)

    # ── update ───────────────────────────────────────────────────────────────

    def update(self, dt_ms: int) -> None:
        if self._battle_transition_phase is not None:
            # Entering battle -- highest-priority gate, same reasoning as
            # battle_scene below (battle_scene is still None throughout
            # this, see start_battle/_tick_battle_transition).
            self._tick_battle_transition(dt_ms)
            return

        if self.battle_scene is not None:
            # A battle owns input/update/draw entirely while active -- see
            # start_battle/_end_battle. Highest-priority gate: nothing else
            # in this method (warps, dialogue, menus, enemy/player movement)
            # should tick underneath a battle.
            self.battle_scene.update(dt_ms)
            if self.battle_scene.finished:
                self._end_battle()
            return

        if self._game_over:
            return   # full pause -- A-press-only, see handle_event/_reload_last_save

        if self.cutscene_player is not None:
            # A cutscene owns update/input entirely while active -- same
            # full-pause tier as battle_scene/dialogue/menu above, so no
            # wander/enemy/player-input ticking happens underneath it. See
            # _tick_cutscene/_end_cutscene. The purely cosmetic idle/walk
            # animation-frame toggle keeps running regardless though, same
            # reasoning as the ordinary dialogue.is_open branch below -- an
            # NPC standing around mid-cutscene (spawn_actor's sleeping
            # Melvin, Billy stood at the foot of the bed, ...) shouldn't
            # look frozen solid just because nothing else about it is
            # moving. _tick_npc_movement (wander/tween advancement) stays
            # untouched here -- a cutscene actor's actual position is
            # driven by its own step (move_actor/teleport_actor), not
            # ordinary wander.
            self._tick_npc_animation(dt_ms)
            self._tick_cutscene(dt_ms)
            return

        if self._transition_phase is not None:
            # A warp is fading out/holding/fading in — full gameplay pause,
            # same idea as dialogue/menu below, just driven by elapsed time
            # instead of input.
            self._tick_transition(dt_ms)
            return

        if self.dialogue.is_open:
            # Holding A or B types the current page in faster. Player
            # input/movement, enemy AI, and warp/trigger checks all stay
            # fully paused, and so does NPC wander (_tick_npc_movement is
            # deliberately NOT called here) -- an NPC mid-conversation was
            # just turned to face the player (see engine.input.
            # handle_a_button's target.facing assignment) and must hold
            # that position/facing until the box closes, not wander off
            # mid-sentence, same for every other NPC on screen while the
            # player's attention is locked on the box. Only the ordinary
            # idle/walk animation-frame toggle (_tick_npc_animation) keeps
            # running, so nobody looks visibly frozen solid.
            fast = bool(self._held_buttons & {'A', 'B'})
            ms_per_char = cfg.dialogue_char_fast_ms if fast else cfg.dialogue_char_ms
            self.dialogue.tick(dt_ms, ms_per_char)
            self._tick_npc_animation(dt_ms)
            return

        if self.menu.is_open or self.shop_menu.is_open:
            # start menu / shop screen are both a full pause -- no gameplay
            # ticks while open. Unlike save/inventory/settings (which
            # overlay atop an already-open self.menu), a shop opens
            # directly from the overworld with self.menu.is_open staying
            # False the whole time, so it needs its own check here.
            return

        # `flag`-event cutscene triggers (see engine.cutscene's module
        # docstring / _check_cutscene_triggers) are the only trigger surface
        # polled every ordinary-gameplay frame instead of off some physical
        # action (arriving somewhere, stepping on a tile, pressing A) -- this
        # is what lets one cutscene chain straight into another purely off a
        # flag it set itself, with no map transition/tile-step/NPC-talk in
        # between (e.g. a multi-part intro that plays before the player ever
        # gets control). Checked here, past every full-pause gate above, so
        # it can't preempt a battle/dialogue/menu/warp-fade already in
        # progress -- only ordinary free-roam.
        self._check_cutscene_triggers('flag')
        if self.cutscene_player is not None:
            return   # a flag trigger just started a cutscene this frame

        # Only ticks once the player actually has control again (past every
        # full-pause gate above) -- an item-drop win opens a "Found X!"
        # dialogue in the same beat immunity starts, and ticking it down
        # while that's still open would burn the window before the player
        # can even move, defeating the point of it.
        if self._battle_immunity_ms > 0:
            self._battle_immunity_ms -= dt_ms
            self._immunity_blink_elapsed += dt_ms
            if self._immunity_blink_elapsed >= _BATTLE_IMMUNITY_BLINK_MS:
                self._immunity_blink_elapsed -= _BATTLE_IMMUNITY_BLINK_MS
                self._immunity_blink_visible = not self._immunity_blink_visible
        else:
            self._immunity_blink_visible = True   # self-heal if immunity expires mid-blink-off

        self._tick_npc_animation(dt_ms)
        self._tick_npc_movement(dt_ms)

        if self._update_enemies(dt_ms):
            return   # a battle just started this frame -- see _update_enemies

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

    def _tick_npc_animation(self, dt_ms: int) -> None:
        """The global idle/walk animation-frame toggle every NPC sprite
        draws off (see get_npc_frame) -- split out of the main body of
        update() so a dialogue box (see the self.dialogue.is_open branch
        above) can keep this ticking on its own, without also resuming
        wander movement (see _tick_npc_movement, deliberately NOT called
        from that branch). A 2-frame sprite's pair and an 8-frame sprite's
        per-facing pair both animate continuously regardless of whether
        the NPC is actually walking anywhere -- that's what makes it safe
        to keep running on a stationary, mid-conversation NPC without it
        looking like it's about to take a step."""
        self._anim_timer += dt_ms
        if self._anim_timer >= _NPC_ANIM_MS:
            self._anim_frame ^= 1
            self._anim_timer -= _NPC_ANIM_MS

    def _tick_npc_movement(self, dt_ms: int) -> None:
        """Wander-move decisions plus in-flight move-tween advancement --
        the part of NPC upkeep that actually changes an NPC's tile/facing,
        as opposed to _tick_npc_animation's purely cosmetic frame toggle.
        Not called while a dialogue box is open (see update()): the NPC
        just turned to face the player (engine.input.handle_a_button)
        must hold that facing and tile until the box closes, and every
        other NPC on screen should likewise stop mid-wander rather than
        visibly walking around while the player's attention is locked on
        the box -- same full-pause tier as a battle/menu/warp-fade already
        gets, just carved out from the animation toggle instead of lumped
        in with it."""
        self._npc_move_timer += dt_ms
        if self._npc_move_timer >= _NPC_MOVE_MS:
            self._npc_move_timer -= _NPC_MOVE_MS
            self._update_npc_wander()

        for tw in self._npc_tweens.values():
            if tw['elapsed'] < _NPC_MOVE_MS:
                tw['elapsed'] = min(tw['elapsed'] + dt_ms, _NPC_MOVE_MS)

    def _update_player_movement(self, dt_ms: int, vertical: str | None, horizontal: str | None) -> None:
        speed = self.player_speed * held_button_speed_multiplier(self.inventory, self._held_buttons)
        self.player.move(dt_ms, vertical, horizontal, speed, self._passable_with_npcs())
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
            trigger_obj = self._trigger_at(*current_tile)
            if trigger_obj is not None:
                self._try_start_tile_trigger(trigger_obj)

    def _warp_at(self, row: int, col: int) -> 'MapObject | None':
        for warp in self.warps:
            if warp.row == row and warp.col == col:
                return warp
        return None

    def _trigger_at(self, row: int, col: int) -> 'MapObject | None':
        for trigger in self.triggers:
            if trigger.row == row and trigger.col == col:
                return trigger
        return None

    def _try_start_tile_trigger(self, trigger_obj: 'MapObject') -> None:
        """A `trigger`-type object's own `cutscene_id` names which cutscene
        to check the instant the player's tile becomes this object's tile
        (see _trigger_at, called from _update_player_movement -- same
        edge-triggered mechanism as a warp). Only actually starts it if
        that cutscene exists, its own trigger is `event: "tile"`, and its
        `when`/`unless` passes against live flags -- the object just says
        *which* cutscene to check, not that it unconditionally fires every
        time the tile's stepped on. Unknown cutscene_id or a mismatched
        trigger event logs a warning and does nothing, same fail-soft
        convention as an unknown npc_id/enemy_id elsewhere in this class."""
        cutscene = self.cutscene_defs.get(trigger_obj.cutscene_id)
        if cutscene is None:
            print(f'trigger {trigger_obj.name!r} (id {trigger_obj.id}) references unknown '
                  f'cutscene_id {trigger_obj.cutscene_id!r} -- ignoring', flush=True)
            return
        if cutscene.trigger is None or cutscene.trigger.event != 'tile':
            print(f"trigger {trigger_obj.name!r}: cutscene {cutscene.id!r} has no "
                  f"trigger event == 'tile' -- ignoring", flush=True)
            return
        if self.cutscene_player is None and trigger_matches(cutscene.trigger, self.game_state.flag):
            self.start_cutscene(cutscene.id)

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

    def _resolve_npc_frames(self, npc_id: str | None, npc_sprite: str | None,
                             colors: list[str] | None = None) -> list[pygame.Surface] | None:
        """The frame list a given npc_id/npc_sprite/colors triple resolves
        to. `colors`, if given, is a placement's own npcs_<map>.yaml
        override (NPC.colors/MapObject.colors) -- it wins outright over
        npc_id's def-level recolor, position-matched against the sprite's
        own placeholder colors (engine.npc.NpcSpriteSpec.colors), same rule
        NpcDef.colors uses. Recolored once per unique (sprite, colors) pair
        and cached in self.npc_color_override_frames, not per frame drawn,
        same spirit as the npc_id-level cache below.

        With no override, this is the recolored cache for an npc_id'd NPC
        (self.npc_frames_by_id, set once at __init__), or its sprite's raw,
        unrecolored frames for an inline placement with no def. Takes plain
        ids rather than an NPC instance so _load_map can resolve a frame set
        (for width_span/height_span) before that NPC object even exists
        yet."""
        if colors is None:
            frames = self.npc_frames_by_id.get(npc_id) if npc_id else None
            if frames is not None:
                return frames
        spec = self.npc_sprite_specs.get(npc_sprite)
        base_frames = self.npc_sprites.get(spec.file) if spec else None
        if colors is None or spec is None or not base_frames:
            return base_frames
        key = (npc_sprite, tuple(colors))
        cached = self.npc_color_override_frames.get(key)
        if cached is None:
            cached = [recolor_surface(f, spec.colors, colors) for f in base_frames]
            self.npc_color_override_frames[key] = cached
        return cached

    def _npc_frames(self, npc: 'NPC') -> list[pygame.Surface] | None:
        """This NPC's resolved frame list -- see _resolve_npc_frames. Shared
        by _draw_entities (which frame to draw) and _load_map (which sets
        width_span/height_span at construction time) so both always agree
        on the same frame set."""
        return self._resolve_npc_frames(npc.npc_id, npc.npc_sprite, npc.colors)

    def _passable_with_npcs(self) -> list[list[bool]]:
        """The static walkable grid with every NPC's current footprint
        marked impassable, so the player can't walk through them --
        including any extra tile(s) a bigger-than-one-tile sprite's overhang
        is drawn over (see load_enemy_sprites/_draw_entities): e.g. a 32px-
        tall NPC also blocks the tile directly north of its own row/col,
        where its hat/head is drawn, and a 32px-wide one also blocks the
        tile to its east, not just its anchor tile. Rebuilt fresh on each
        attempted step since NPCs move — engine.player stays headless and
        ignorant of NPCs entirely; it just sees a plain grid."""
        grid = [row[:] for row in self.passable]
        for npc in self.npcs:
            for i in range(npc.height_span):
                r = npc.row - i
                if not (0 <= r < len(grid)):
                    continue
                for j in range(npc.width_span):
                    c = npc.col + j
                    if 0 <= c < len(grid[r]):
                        grid[r][c] = False
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
        tried via _npc_step -- same rules as the player, just without input
        driving it."""
        for npc in self.npcs:
            if npc.behavior != 'wander':
                continue
            if self._npc_rng.random() >= _NPC_WANDER_MOVE_CHANCE:
                continue
            self._npc_step(npc, self._npc_rng.choice(_NPC_WANDER_DIRECTIONS))

    def _npc_step(self, npc: 'NPC', direction: str) -> bool:
        """Attempt one tile-step for `npc` in `direction`, against the
        walkable grid and current occupancy -- same rules as the player.
        On success, mutates npc.row/col, turns it to face `direction`
        (visible only on an 8-frame sprite, a no-op field otherwise), and
        seeds its visual tween (see _npc_visual_pos) so it animates
        smoothly to the new tile over _NPC_MOVE_MS. Returns whether the
        step actually happened -- a blocked attempt does nothing at all,
        same as before this was split out of _update_npc_wander (a wander
        NPC doesn't turn in place on a blocked attempt the way the player's
        tap-to-turn does).

        Shared by _update_npc_wander (a random direction, chosen for it)
        and the cutscene executor's move_actor step (_cutscene_move_npc, a
        direction chosen toward a scripted target) -- the two callers
        differ only in *which* direction they try, not in how a step is
        actually taken."""
        dr, dc = _DIR_DELTA[direction]
        new_row, new_col = npc.row + dr, npc.col + dc
        if not (0 <= new_row < self.tmap.height and 0 <= new_col < self.tmap.width):
            return False
        if not self.passable[new_row][new_col]:
            return False
        if self._tile_occupied(new_row, new_col, exclude_npc=npc):
            return False

        vis = self._npc_visual_pos(npc)
        npc.row, npc.col = new_row, new_col
        npc.facing = direction
        tw = self._npc_tweens[npc.index]
        tw['src'] = vis
        tw['dst'] = (float(new_row), float(new_col))
        tw['elapsed'] = 0
        return True

    def _npc_visual_pos(self, npc) -> tuple[float, float]:
        tw = self._npc_tweens.get(npc.index)
        if tw is None:
            return float(npc.row), float(npc.col)
        t = min(tw['elapsed'] / _NPC_MOVE_MS, 1.0)
        return _lerp(tw['src'][0], tw['dst'][0], t), _lerp(tw['src'][1], tw['dst'][1], t)

    def _update_enemies(self, dt_ms: int) -> bool:
        """One frame of enemy movement. Unlike NPCs, there's no tween step
        here — engine.enemy.update_enemy mutates Enemy.row/col continuously
        in place, so the position it leaves behind is already what draw()
        reads. Enemies never block or are blocked by the player (see
        README/CLAUDE.md) — touching one starts a battle instead, gated by
        `_battle_immunity_ms` (see __init__/update) so the player isn't
        instantly regrabbed on the same tile a battle just ended on.

        Returns True the instant a battle starts, so update() can bail out
        of the rest of that frame's processing (player movement, warp
        checks) rather than keep advancing gameplay underneath the battle
        that just began."""
        for enemy in self.enemies:
            edef = self.enemy_defs.get(enemy.enemy_id)
            if edef is None:
                continue
            update_enemy(enemy, edef, self.passable, self.player.row, self.player.col,
                         dt_ms, self._enemy_rng)
            if self._battle_immunity_ms <= 0 and check_overlap(enemy, self.player.row, self.player.col):
                self.start_battle(enemy.enemy_id, level=enemy.level, source_enemy=enemy)
                return True
        return False

    def start_battle(self, enemy_id: str, level: int | None = None,
                      source_enemy: 'Enemy | None' = None) -> None:
        """Launch a battle against `enemy_id`. `level`, if given, is used
        as-is instead of re-rolling EnemyDef.level's range -- the touch-
        trigger path (_update_enemies) passes the already-resolved
        Enemy.level, so what the player *saw* on the overworld is what they
        *fight*, not a fresh independent roll. `source_enemy`, if given, is
        removed from self.enemies on a win (see _end_battle) -- it respawns
        for free the next time _load_map runs (leaving and re-entering the
        map), same mechanism `spawner` objects already use every reload; no
        flag, nothing persisted. Omit `source_enemy` for a scripted
        encounter with no overworld Enemy instance behind it (see
        trigger_scripted_encounter).

        Doesn't build BattleScene directly -- kicks off the battle-entry
        transition instead (see _tick_battle_transition), which builds it
        the instant the transition completes. `battle_scene` stays None for
        the whole 2.5s of the transition; `player.state` flips to IN_BATTLE
        immediately though, so movement/interaction are already gated the
        same as during the battle itself."""
        self._pending_battle = {"enemy_id": enemy_id, "level": level, "source_enemy": source_enemy}
        # min() so a random pick can never exceed however many rows the file
        # actually has, even if _BATTLE_TRANSITION_ANIM_COUNT drifts ahead of
        # it (e.g. the constant bumped before the sheet's actually updated).
        usable_rows = min(_BATTLE_TRANSITION_ANIM_COUNT, len(self.transition_frames))
        self._battle_transition_frames = self.transition_frames[self._enemy_rng.randrange(usable_rows)]
        self._battle_transition_phase = "anim"
        self._battle_transition_elapsed = 0
        self._battle_transition_frame_idx = 0
        self.player.set_state(PlayerState.IN_BATTLE)

    def _tick_battle_transition(self, dt_ms: int) -> None:
        """Advance the battle-entry transition (see start_battle). "anim"
        cycles self._battle_transition_frames over _BATTLE_TRANSITION_ANIM_MS
        (see _draw_battle_transition_overlay for how a frame gets drawn);
        once that completes, "hold" is a plain solid-black pause for
        _BATTLE_TRANSITION_HOLD_MS -- the moment that ends, BattleScene
        actually gets constructed from _pending_battle, exactly the call
        start_battle used to make directly, fully hidden behind that black
        hold the same way _swap_map's map-load is hidden behind the warp
        fade's black."""
        self._battle_transition_elapsed += dt_ms
        if self._battle_transition_phase == "anim":
            frame_ms = _BATTLE_TRANSITION_ANIM_MS / len(self._battle_transition_frames)
            self._battle_transition_frame_idx = min(
                int(self._battle_transition_elapsed / frame_ms),
                len(self._battle_transition_frames) - 1,
            )
            if self._battle_transition_elapsed >= _BATTLE_TRANSITION_ANIM_MS:
                self._battle_transition_phase = "hold"
                self._battle_transition_elapsed = 0
        elif self._battle_transition_phase == "hold":
            if self._battle_transition_elapsed >= _BATTLE_TRANSITION_HOLD_MS:
                pending = self._pending_battle
                self.battle_scene = BattleScene(pending["enemy_id"], rng=self._enemy_rng,
                                                 roster=self.roster, level_override=pending["level"],
                                                 inventory=self.inventory, item_defs=self.item_defs)
                self._battle_source_enemy = pending["source_enemy"]
                self._pending_battle = None
                self._battle_transition_phase = None
                self._battle_transition_frames = None

    def trigger_scripted_encounter(self, enemy_id: str, level: int | None = None) -> None:
        """Stub hook for a future Event System action (e.g. `then: [battle:
        enemy_id]`) -- the general if/then/else executor isn't built yet
        (see README's Event System section), so nothing can author this
        from YAML today. Not called from anywhere yet; exists so a later
        scripted "boss appears after dialogue" flow has one obvious place to
        wire into, without duplicating start_battle's setup inline wherever
        that turns out to be triggered from."""
        self.start_battle(enemy_id, level=level, source_enemy=None)

    def _end_battle(self) -> None:
        """Tear down self.battle_scene once it reports `finished` (see
        BattleScene's docstring) and apply the outcome. A win credits
        rewards and drops the defeated enemy from this session's list; a
        successful RUN ("fled") credits nothing and leaves the enemy on the
        map, since it was never defeated -- either way MELVIN's post-battle
        hp/mp (a failed run still costs a free enemy attack) get written
        back to the roster and the post-battle immunity window starts. A
        loss goes to the game-over screen instead -- see _start_game_over --
        since reverting to the last save replaces self.roster wholesale,
        there's nothing to write back."""
        battle = self.battle_scene.battle
        if battle.phase == "win":
            melvin = self.roster.get("MELVIN")
            melvin.hp, melvin.mp = battle.party.hp, battle.party.mp
            if self._battle_source_enemy in self.enemies:
                self.enemies.remove(self._battle_source_enemy)
                # Only removed from this session's in-memory list -- respawns
                # for free next _load_map, same as spawner objects already do.
                # FUTURE (see README roadmap): an Earthbound-style "respawn
                # only once the defeated tile scrolls off-camera" refinement
                # is explicitly deferred, not implemented here.
            self._return_to_overworld_after_battle()
            # Last, so an item-drop dialogue box (see _credit_battle_rewards)
            # is the state that actually sticks, not overwritten back to IDLE.
            self._credit_battle_rewards(battle)
        elif battle.phase == "fled":
            melvin = self.roster.get("MELVIN")
            melvin.hp, melvin.mp = battle.party.hp, battle.party.mp
            # source enemy is NOT removed -- a successful RUN isn't a defeat,
            # it just stays on the map exactly where it was.
            self._return_to_overworld_after_battle()
        else:
            self._start_game_over()

    def _return_to_overworld_after_battle(self) -> None:
        """Shared teardown for both a win and a successful RUN -- clears the
        battle scene, resets to IDLE, and starts the post-battle immunity
        window (see _update_enemies/_draw_entities for how that's used)."""
        self.battle_scene = None
        self._battle_source_enemy = None
        self._battle_immunity_ms = _BATTLE_IMMUNITY_MS
        self._immunity_blink_elapsed = 0
        self._immunity_blink_visible = True   # always starts visible, never mid-blink
        self.player.set_state(PlayerState.IDLE)

    def _credit_battle_rewards(self, battle: BattleState) -> None:
        """Apply a battle win's resolved rewards (already rolled inside
        BattleState on its own seeded rng, per CLAUDE.md's RNG rule) to real
        game state -- gold/XP/item, mirroring engine.input._open_container's
        grant shape. XP crossing a threshold (see engine.battle.
        xp_to_next_level/apply_level_ups) also grants a level, bumping
        max_hp/max_mp -- current hp/mp aren't topped up to match. A level-up
        and/or an item drop each add a synthesized page to the same
        dialogue box, shown once control returns to the overworld."""
        pages = []
        if battle.gold_reward:
            self.game_state.add_gold(battle.gold_reward)
        if battle.xp_reward:
            melvin = self.roster.get("MELVIN")
            melvin.xp += battle.xp_reward
            levels_gained = apply_level_ups(melvin)
            if levels_gained:
                pages.append(f"MELVIN reached LEVEL {melvin.lvl}!")
        if battle.item_reward:
            self.inventory.add(battle.item_reward)
            item_def = self.item_defs.get(battle.item_reward)
            name = item_def.name if item_def else battle.item_reward
            pages.append(f"Found {name}!")
        if pages:
            self.dialogue.open(self._wrap_dialogue_pages(pages))
            self.player.set_state(PlayerState.IN_DIALOGUE)

    def _start_game_over(self) -> None:
        """A battle loss: drop the battle scene, show GAME OVER, and wait
        for an A press (see handle_event) before reverting to the last
        save -- see _reload_last_save. No penalty beyond that reversion
        (discards everything since the last manual save, same as any
        roguelike-style death)."""
        self.battle_scene = None
        self._battle_source_enemy = None
        self._game_over = True

    def _reload_last_save(self) -> None:
        """Revert to `self.active_slot`'s last save, exactly the way a warp
        lands the player on another map (_swap_map) -- goes through
        _load_map with the *saved* map's own path, since the player may
        have warped to a different map since they last saved, not just
        reset position on whatever's currently loaded.

        If there's no save to revert to at all (e.g. a fresh maptest.py
        map-picker boot that was never saved), the closest reasonable
        approximation of "start over" is a fresh Player/Inventory/
        GameState/Roster on the map already loaded -- there's nothing to
        revert to."""
        if not slot_exists(self.active_slot):
            self.player = Player.default()
            self.inventory = Inventory()
            self.game_state = GameState()
            self.roster = Roster.fresh()
            self._load_map(self.map_path)
        else:
            map_name, player, inventory, game_state, roster = load_from_slot(self.active_slot)
            self.player, self.inventory, self.game_state, self.roster = \
                player, inventory, game_state, roster
            map_path = _MAPS_DIR / map_name / f'{map_name}.json'
            self._load_map(map_path, spawn=(player.row, player.col), facing=player.facing)
        self._game_over = False
        self.player.set_state(PlayerState.IDLE)

    def _draw_game_over(self, surface: pygame.Surface) -> None:
        """Full-screen black, dialogue_font only -- same "no art yet"
        approach as every other alpha-pass screen in this class."""
        surface.fill((0, 0, 0))
        title = self.dialogue_font.render("GAME OVER", False, _GAME_OVER_TEXT_COLOR)
        surface.blit(title, ((surface.get_width() - title.get_width()) // 2,
                              (surface.get_height() - title.get_height()) // 2))
        prompt = self.dialogue_font.render("Press A to continue", False, _GAME_OVER_TEXT_COLOR)
        surface.blit(prompt, ((surface.get_width() - prompt.get_width()) // 2,
                               surface.get_height() // 2 + title.get_height() + 8))

    def _wrap_dialogue_pages(self, raw_pages: list[str]) -> list[str]:
        """Expand each hand-authored YAML page into one or more on-screen
        screens sized to the dialogue box's text area. Passed into
        engine.input.handle_a_button so it stays pygame-free."""
        screens: list[str] = []
        for page in raw_pages:
            screens.extend(_wrap_text_to_screens(
                self.dialogue_font, page, _DIALOGUE_TEXT_W, _DIALOGUE_TEXT_H))
        return screens

    # ── cutscene ─────────────────────────────────────────────────────────────
    #
    # engine.cutscene holds the headless step list + step pointer
    # (CutsceneDef/CutscenePlayer); everything below actually executes a
    # step against real pygame/Tiled state -- moving/tweening an NPC or the
    # player, opening the dialogue box, mutating a tile layer's GID grid,
    # panning the camera. See engine.cutscene's module docstring for the
    # step-kind/YAML shape, and the build plan (cutscene system) for why
    # this lives here rather than in engine.cutscene itself: same
    # headless-logic/pygame-execution split as engine.battle.BattleState
    # vs. BattleScene.

    def _check_cutscene_triggers(self, event: str) -> None:
        """Check every loaded cutscene (self.cutscene_defs, insertion order
        -- see engine.cutscene.load_cutscene_defs, sorted by filename) whose
        own `trigger.event` matches `event` and whose `map` is the one
        that's actually loaded right now, and start the first one whose
        `when`/`unless` passes against live flags (engine.cutscene.
        trigger_matches) -- first match wins, same "checked top-to-bottom"
        rule engine.npc.resolve_dialogue already uses for dialogue variants.
        A no-op if a cutscene is somehow already running, or if none match.

        This is also the mechanism behind chaining a story beat across two
        maps (see the build plan's "no mid-cutscene map switching" decision):
        cutscene A, on map 1, ends by setting a flag; the player leaves
        through an ordinary warp; map 2's own "map_load" check (called from
        _load_map, at the tail end of _swap_map) sees that flag and starts
        cutscene B -- no map-switching logic ever needed inside the executor
        itself.

        `event == 'flag'` is the odd one out: update() polls it every
        ordinary-gameplay frame rather than off a physical action (arriving
        somewhere / a tile-step / an A-press), which is what makes it the
        right surface for chaining a cutscene purely off another cutscene's
        own set_flag step -- no map transition, tile, or NPC needed in
        between (the case that actually motivated this: an intro made of
        several back-to-back cutscenes, before the player has ever had
        control at all). Being polled every frame with no physical gate
        also means it's the one surface that would refire forever the
        instant it ends if nothing stopped it -- so unlike map_load/tile/
        npc_talk (author's own job to add an `unless: [cutscene_seen:<id>]`
        guard, per this module's docstring), a `flag`-event cutscene is
        *implicitly* skipped here the moment its own `cutscene_seen:<id>`
        flag is set, regardless of what the author's own `when`/`unless`
        say -- and that flag gets set automatically, right here, the
        instant it fires. Not "auto-populates the author's `unless` list"
        (an empty `unless: []` wouldn't check it) -- an actual second,
        unconditional gate this surface alone enforces, so no author
        discipline is required at all for this one to be one-shot; a
        manual `unless: [cutscene_seen:<id>]` on top is harmless, just
        redundant."""
        if self.cutscene_player is not None:
            return
        map_name = self.map_path.stem
        for cutscene in self.cutscene_defs.values():
            trigger = cutscene.trigger
            if trigger is None or trigger.event != event or cutscene.map != map_name:
                continue
            if event == 'flag' and self.game_state.flag(f'cutscene_seen:{cutscene.id}'):
                continue
            if trigger_matches(trigger, self.game_state.flag):
                if event == 'flag':
                    self.game_state.set_flag(f'cutscene_seen:{cutscene.id}', True)
                self.start_cutscene(cutscene.id)
                return

    def start_cutscene(self, cutscene_id: str) -> None:
        """Begin playing `cutscene_id` (a key into self.cutscene_defs) from
        its first step. Reachable two ways: a matching trigger firing (see
        _check_cutscene_triggers -- map-load today; tile/npc-talk below) or
        directly, e.g. maptest.py's debug `cutscene` mode. A no-op (with a
        console warning) if the id doesn't resolve, rather than raising --
        an author typo shouldn't crash the game."""
        cutscene = self.cutscene_defs.get(cutscene_id)
        if cutscene is None:
            print(f'start_cutscene: unknown cutscene id {cutscene_id!r} -- ignoring', flush=True)
            return
        self.cutscene_player = CutscenePlayer(cutscene=cutscene)
        self._cutscene_armed_index = None
        self._cutscene_wait_remaining = 0.0
        self._cutscene_camera_pan = None

    def _end_cutscene(self) -> None:
        """Clean up once CutscenePlayer.finished: despawn every actor this
        cutscene itself spawned (per the build plan, cutscene-spawned
        actors never persist), drop the camera override (so _camera_offset_px
        goes back to following the player), and clear cutscene_player so
        update()/handle_event()'s gates fall through to ordinary play again.

        Also re-seeds _player_last_tile at wherever the cutscene actually
        left the player -- _cutscene_move_player moves player.row/col
        directly without touching it (unlike ordinary input-driven movement,
        which updates it every frame in _update_player_movement), so without
        this, the very next normal frame would see current_tile() differ
        from a stale pre-cutscene _player_last_tile and treat that as a
        fresh step onto whatever warp/trigger tile the cutscene happened to
        park the player on -- an unintended, unauthored map transition or
        cutscene the instant this one ends. Same reasoning _load_map already
        applies right after positioning the player (fresh boot, warp
        arrival, game-over revert) -- this is that same rule for a cutscene
        ending. Chaining across maps (see the build plan) still works: it's
        the player's own next *ordinary* step onto a warp, after regaining
        control, that fires -- not a step the cutscene silently walked them
        through as a side effect of where it stopped them."""
        if self._cutscene_spawned_ids:
            self.npcs = [npc for npc in self.npcs if npc.index not in self._cutscene_spawned_ids]
            for index in self._cutscene_spawned_ids:
                self._npc_tweens.pop(index, None)
            self._cutscene_spawned_ids = set()
        self._camera_override = None
        self._cutscene_camera_pan = None
        self._cutscene_fade_alpha = 0.0   # a fade holds until faded back in or the cutscene ends -- this is "ends"
        self._cutscene_fade_anim = None
        self.cutscene_player = None
        self._cutscene_armed_index = None
        self._player_last_tile = self.player.current_tile()

    def _handle_cutscene_event(self, event: pygame.event.Event) -> None:
        """Input while a cutscene is active: A or B advances/closes whatever
        dialogue page is currently open (same as ordinary play -- see
        engine.input.handle_a_button/handle_b_button's own dialogue.is_open
        branches), tracked via _held_buttons same as normal for the
        fast-reveal-on-hold effect (see _cutscene_step_dialogue). Movement
        keys are otherwise ignored entirely -- a cutscene owns the player's
        position via move_actor steps, same "full pause" reasoning as a
        battle or menu owning input -- except while a dialogue step's
        response list is showing (self.dialogue.is_showing_choices()),
        where N/S move the response cursor and A confirms one instead of
        advancing/closing, same N/S-cursor + A-confirm idiom as every other
        list menu in this game (B has no role there -- confirming a choice
        is a distinct action from merely proceeding, so it stays A-only)."""
        if event.type != pygame.KEYDOWN:
            return
        button = _BUTTON_KEY.get(event.key)
        if button:
            self._held_buttons.add(button)

        if self.dialogue.is_showing_choices():
            direction = _DIR_KEY.get(event.key)
            if direction == 'N':
                self.dialogue.move_choice_cursor(-1)
            elif direction == 'S':
                self.dialogue.move_choice_cursor(1)
            elif button == 'A':
                self._confirm_dialogue_choice()
            return

        if button in ('A', 'B') and self.dialogue.is_open:
            self.dialogue.advance()

    def _confirm_dialogue_choice(self) -> None:
        """Resolve whichever response the player just confirmed on the
        current dialogue step's choice list: splice that CutsceneChoice's
        own `then:` steps into the running cutscene's step list, right
        after the dialogue step that presented them, then close the
        dialogue box. _tick_cutscene's normal advance (triggered next by
        _cutscene_step_dialogue seeing the box closed, see below) lands
        exactly on the first spliced-in step -- so a choice's consequences
        get the same multi-frame handling as any other step, not a
        separate one-shot execution path. Splices into `cp.steps` -- the
        CutscenePlayer's own private copy (see CutscenePlayer.__post_init__)
        -- never `cp.cutscene.steps`, which is the shared list living on
        the cached CutsceneDef (self.cutscene_defs is built once and reused
        for the process lifetime); splicing into the shared list would
        permanently graft these steps onto every future play of this same
        cutscene, including a replay after picking a different choice."""
        cp = self.cutscene_player
        index = self.dialogue.confirm_choice()
        self.dialogue.close()
        if index is None or cp is None:
            return
        step = cp.current_step()
        choices = (step.args.get('choices') or []) if step is not None else []
        if 0 <= index < len(choices):
            cp.steps[cp.index + 1:cp.index + 1] = choices[index].then

    def _tick_cutscene(self, dt_ms: int) -> None:
        """One frame of cutscene playback: run the current step, and if it
        reports itself finished, advance to the next one and keep going
        within the same frame -- so a run of instantaneous steps (set_flag,
        give_item, face, ...) all resolve on one frame, same as they would
        if hand-written as separate lines of Python, rather than each
        silently costing a real frame of nothing happening. Only the first
        step run this frame gets `dt_ms`; any instantaneous steps that fall
        through afterward get 0, so a wait/move/dialogue that immediately
        follows doesn't lose the frame's elapsed time twice."""
        cp = self.cutscene_player
        while True:
            step = cp.current_step()
            if step is None:
                self._end_cutscene()
                return
            is_new = self._cutscene_armed_index != cp.index
            if is_new:
                self._cutscene_armed_index = cp.index
            finished = self._run_cutscene_step(step, dt_ms, is_new)
            if not finished:
                return
            cp.advance()
            dt_ms = 0

    def _run_cutscene_step(self, step: 'CutsceneStep', dt_ms: int, is_new: bool) -> bool:
        handler = getattr(self, f'_cutscene_step_{step.kind}', None)
        if handler is None:
            print(f'cutscene: unknown step kind {step.kind!r} -- skipping', flush=True)
            return True
        return handler(step.args, dt_ms, is_new)

    def _cutscene_actor(self, name: str):
        """Resolve a step's `actor` name to the live object it refers to --
        the literal string "player" for self.player, otherwise a name match
        against self.npcs (covers both a map's real NPCs and anything a
        prior spawn_actor step in this same cutscene added, since both live
        in that one list). Returns None (with a console warning) for an
        unknown name rather than raising -- an author typo shouldn't crash
        the game, same fail-soft convention as an unknown npc_id elsewhere
        in this class."""
        if name == 'player':
            return self.player
        for npc in self.npcs:
            if npc.name == name:
                return npc
        print(f'cutscene: unknown actor {name!r} -- step skipped', flush=True)
        return None

    # ── cutscene: step handlers ──────────────────────────────────────────────
    # Each takes (args, dt_ms, is_new) and returns whether the step is now
    # finished, dispatched by name via _run_cutscene_step -- new step kinds
    # (a future editor's action palette) just need a new _cutscene_step_*
    # method here, no dispatcher changes.

    def _cutscene_step_wait(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        if is_new:
            self._cutscene_wait_remaining = args['ms']
        self._cutscene_wait_remaining -= dt_ms
        return self._cutscene_wait_remaining <= 0

    def _cutscene_step_face(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        actor = self._cutscene_actor(args['actor'])
        if actor is not None:
            actor.facing = args['dir']
        return True

    def _cutscene_step_dialogue(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        """Behaves exactly like an ordinary dialogue box (see update()'s own
        dialogue.is_open branch, which this duplicates rather than shares
        since that branch is wired to full-pause `return`, not a
        step-finished bool): reveals at the normal pace, and only counts as
        finished once the box is closed -- via an ordinary A press (see
        _handle_cutscene_event) for a plain dialogue step, same as any other
        dialogue in the game, or via _confirm_dialogue_choice closing it once
        a response is picked, for one authored with `choices:` (see
        engine.cutscene.CutsceneChoice) -- either way, this method just
        watches self.dialogue.is_open, so it doesn't need to know which.

        `args['pages']` is run through _wrap_dialogue_pages just like
        ordinary sign/NPC dialogue -- authoring this step with raw
        unwrapped strings used to overflow the box past the screen edge
        for anything longer than one line; every other dialogue.open() call
        site already wrapped, this one just hadn't.

        An optional `position: "top"|"bottom"` pins which edge the box
        docks to for this step specifically, read directly by
        _draw_dialogue_box off the currently-active step rather than
        stored here -- see that method. Anything else (unset, "auto")
        leaves the normal player-position-based auto-dock behavior alone."""
        if is_new:
            choices = args.get('choices') or []
            self.dialogue.open(self._wrap_dialogue_pages(list(args['pages'])),
                                choices=[c.label for c in choices] or None)
            return False
        if not self.dialogue.is_open:
            return True
        fast = bool(self._held_buttons & {'A', 'B'})
        ms_per_char = cfg.dialogue_char_fast_ms if fast else cfg.dialogue_char_ms
        self.dialogue.tick(dt_ms, ms_per_char)
        return False

    def _cutscene_step_set_flag(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        self.game_state.set_flag(args['flag'], args.get('value', True))
        return True

    def _cutscene_step_clear_flag(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        self.game_state.set_flag(args['flag'], False)
        return True

    def _cutscene_step_give_item(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        self.inventory.add(args['item'], args.get('qty', 1))
        return True

    def _cutscene_step_start_cutscene(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        """Replace the running cutscene with a different one outright --
        e.g. a dialogue choice's `then:` immediately kicking off a
        flashback (see engine.cutscene's module docstring), or an ordinary
        top-level step chaining straight into another scene. Only valid
        targeting the *same* map as the one currently running -- the
        executor never switches maps itself (see the build plan's "no
        mid-cutscene map switching" decision) -- a cross-map id is a no-op,
        logged, same fail-soft convention as an unknown npc_id/enemy_id
        elsewhere in this class.

        Always returns False (never "finished"): _tick_cutscene captured
        the *old* CutscenePlayer in a local variable before calling this,
        so it must return immediately rather than keep looping against a
        player object that's no longer self.cutscene_player -- the new
        cutscene's own first step starts fresh next frame, when
        _tick_cutscene re-reads self.cutscene_player from scratch."""
        cutscene_id = args['id']
        cutscene = self.cutscene_defs.get(cutscene_id)
        if cutscene is None:
            print(f'cutscene: start_cutscene references unknown cutscene id {cutscene_id!r} -- '
                  f'skipping', flush=True)
            return False
        if cutscene.map != self.map_path.stem:
            print(f"cutscene: start_cutscene({cutscene_id!r}) targets map {cutscene.map!r}, but "
                  f"the executor never switches maps mid-cutscene -- skipping", flush=True)
            return False
        self.cutscene_player = CutscenePlayer(cutscene=cutscene)
        self._cutscene_armed_index = None
        return False

    def _cutscene_step_spawn_actor(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        """Add a temporary NPC-shaped actor for this cutscene's lifetime
        only (see _end_cutscene) -- always resolved against an existing
        data/npcs/npc.yaml `npc_id`, the same shared-def mechanism any real
        placement uses, rather than a bespoke ad hoc sprite path; this way
        a spawned actor draws through the exact same _draw_entities/
        _npc_frames code as any other NPC, no new draw path needed."""
        npc_id = args['npc_id']
        npc_def = self.npc_defs.get(npc_id)
        if npc_def is None:
            print(f'cutscene: spawn_actor references unknown npc_id {npc_id!r} -- skipping', flush=True)
            return True
        frames = self._resolve_npc_frames(npc_id, npc_def.sprite)
        width_span = (frames[0].get_width() // self.tile_px) if frames else 1
        height_span = (frames[0].get_height() // self.tile_px) if frames else 1
        index = self._cutscene_next_actor_index
        self._cutscene_next_actor_index -= 1
        npc = NPC(
            index=index, name=args['id'], row=args['row'], col=args['col'],
            npc_sprite=npc_def.sprite, behavior='static',
            facing=args.get('facing', npc_def.facing), npc_id=npc_id,
            width_span=width_span, height_span=height_span,
        )
        self.npcs.append(npc)
        self._npc_tweens[index] = {
            'src': (float(npc.row), float(npc.col)),
            'dst': (float(npc.row), float(npc.col)),
            'elapsed': _NPC_MOVE_MS,
        }
        self._cutscene_spawned_ids.add(index)
        return True

    def _cutscene_step_despawn_actor(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        actor_id = args['id']
        for npc in self.npcs:
            if npc.name == actor_id and npc.index in self._cutscene_spawned_ids:
                self.npcs.remove(npc)
                self._npc_tweens.pop(npc.index, None)
                self._cutscene_spawned_ids.discard(npc.index)
                return True
        print(f'cutscene: despawn_actor {actor_id!r} not found (already despawned, or never '
              f'spawned by this cutscene) -- ignoring', flush=True)
        return True

    def _cutscene_step_set_tile(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        """One-shot GID swap on a tile layer's already-loaded grid (e.g. a
        door's tile flipping from closed to open) -- a direct mutation of
        TiledLayer.grid, read fresh every frame by _draw_tile_layer, so
        there's nothing to invalidate/recompute afterward. Deliberately not
        the animated/cycling-tile system sketched in README's roadmap --
        that's a separate, unbuilt, unrelated feature; this is a single
        discrete swap, done once, same as picking up a container's loot."""
        layer = next((l for l in self.tmap.layers if l.name == args['layer'] and l.kind == 'tile'), None)
        if layer is None:
            print(f"cutscene: set_tile references unknown tile layer {args['layer']!r} -- skipping",
                  flush=True)
            return True
        layer.grid[args['row']][args['col']] = args['gid']
        return True

    def _cutscene_step_pan_camera(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        """Move the camera to an explicit (row, col) instead of its normal
        player-locked behavior (see _camera_offset_px), lerping smoothly
        over `duration_ms` (default instant). `{to_player: true}` clears
        the override immediately instead -- the camera resumes tracking
        the player from wherever it currently is, no animation needed since
        _camera_offset_px already recomputes from _player_vis every frame
        once _camera_override is None."""
        if args.get('to_player'):
            self._camera_override = None
            self._cutscene_camera_pan = None
            return True

        target = (float(args['to'][0]), float(args['to'][1]))
        duration_ms = max(args.get('duration_ms', 0), 1)
        if is_new:
            start = self._camera_override if self._camera_override is not None else self._player_vis
            self._cutscene_camera_pan = {'src': start, 'dst': target, 'elapsed': 0, 'duration': duration_ms}
        pan = self._cutscene_camera_pan
        pan['elapsed'] = min(pan['elapsed'] + dt_ms, pan['duration'])
        t = pan['elapsed'] / pan['duration']
        self._camera_override = (_lerp(pan['src'][0], pan['dst'][0], t), _lerp(pan['src'][1], pan['dst'][1], t))
        return pan['elapsed'] >= pan['duration']

    _CUTSCENE_MOVE_EPS = 1e-3

    def _cutscene_step_move_actor(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        actor = self._cutscene_actor(args['actor'])
        if actor is None:
            return True
        target = (args['to'][0], args['to'][1])
        if actor is self.player:
            return self._cutscene_move_player(target, dt_ms)
        return self._cutscene_move_npc(actor, target, dt_ms)

    def _cutscene_move_player(self, target: tuple[int, int], dt_ms: int) -> bool:
        """Continuous move toward `target`, reusing engine.movement.
        step_continuous directly (the same primitive engine.player.Player.
        move wraps for held-key input) rather than Player.move itself,
        since that's built around one-direction-per-axis held input, not a
        scripted destination. Also drives the walk-cycle anim timer/frame
        by hand (_player_walk_timer/_player_anim) since the normal update()
        body that does this is skipped entirely while a cutscene is active
        (see update()'s cutscene gate) -- _draw_entities reads those same
        fields regardless of who's driving them."""
        t_row, t_col = target
        row, col = self.player.row, self.player.col
        if abs(row - t_row) < self._CUTSCENE_MOVE_EPS and abs(col - t_col) < self._CUTSCENE_MOVE_EPS:
            self.player.row, self.player.col = float(t_row), float(t_col)
            self._player_vis = (self.player.row, self.player.col)
            self._player_walk_timer = 0
            self._player_anim = 0
            return True

        if round(col) == t_col:
            direction, remaining = ('S' if t_row > row else 'N'), abs(t_row - row)
        elif round(row) == t_row:
            direction, remaining = ('E' if t_col > col else 'W'), abs(t_col - col)
        else:
            print(f'cutscene: move_actor target {target} is not a straight cardinal line '
                  f'from the player\'s position {(row, col)} -- stopping short', flush=True)
            return True

        dist = min(self.player_speed * (dt_ms / 1000.0), remaining)
        new_row, new_col = step_continuous(row, col, direction, dist, self._passable_with_npcs())
        self.player.facing = direction

        # A wall (or another NPC) between here and target hard-stops
        # step_continuous at the obstacle every frame -- with no guard,
        # `remaining` would never shrink and this step would never finish,
        # freezing the game (cutscene input is fully locked, see
        # _handle_cutscene_event). Same "stop short, don't hang" contract
        # _cutscene_move_npc already has via _npc_step's bool return; this
        # is the continuous-movement equivalent of that check. Gated on
        # `dist` itself being non-negligible -- dt_ms is legitimately 0 on
        # a step that starts the same frame an earlier step (e.g. a wait)
        # just finished (see _tick_cutscene), which also produces zero
        # movement but isn't blocked at all, just not ticked yet.
        if dist > self._CUTSCENE_MOVE_EPS and abs(new_row - row) < self._CUTSCENE_MOVE_EPS \
                and abs(new_col - col) < self._CUTSCENE_MOVE_EPS:
            print(f'cutscene: move_actor target {target} is blocked from the player\'s '
                  f'position {(row, col)} -- stopping short', flush=True)
            self._player_walk_timer = 0
            self._player_anim = 0
            return True

        self.player.row, self.player.col = new_row, new_col
        self._player_vis = (new_row, new_col)

        self._player_walk_timer += dt_ms
        if self._player_walk_timer >= self.player_move_ms / 2:
            self._player_walk_timer -= self.player_move_ms / 2
            self._player_anim ^= 1
        return False

    def _cutscene_move_npc(self, npc: 'NPC', target: tuple[int, int], dt_ms: int) -> bool:
        """Step `npc` toward `target` one tile at a time via _npc_step,
        waiting out each tile's tween (_NPC_MOVE_MS) before taking the
        next -- same per-tile cadence as wander, just aimed at a scripted
        destination instead of a random direction. Advances the tween's
        elapsed time here by hand (rather than relying on update()'s usual
        per-frame tween loop, which doesn't run while a cutscene is active,
        same reasoning as _cutscene_move_player's walk-anim timer above)."""
        tw = self._npc_tweens[npc.index]
        if tw['elapsed'] < _NPC_MOVE_MS:
            tw['elapsed'] = min(tw['elapsed'] + dt_ms, _NPC_MOVE_MS)
            return False
        if (npc.row, npc.col) == target:
            return True

        t_row, t_col = target
        if npc.col == t_col:
            direction = 'S' if t_row > npc.row else 'N'
        elif npc.row == t_row:
            direction = 'E' if t_col > npc.col else 'W'
        else:
            print(f'cutscene: move_actor target {target} is not a straight cardinal line '
                  f"from {npc.name}'s position {(npc.row, npc.col)} -- stopping short", flush=True)
            return True

        if not self._npc_step(npc, direction):
            print(f"cutscene: {npc.name} blocked moving {direction} toward {target} -- "
                  f'stopping short', flush=True)
            return True
        return False

    def _cutscene_step_teleport_actor(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        """Instantly set an actor's position -- no walk cycle, no cardinal-
        line requirement, always finishes the same frame it starts. This
        is the tool for repositioning an actor while the camera's looking
        somewhere else (see pan_camera) so the reposition itself is never
        seen: move_actor always walks (proportional to distance, cardinal
        only), which is either a visibly wrong diagonal-looking L-shaped
        detour or just pointless time spent off-screen for no visual
        payoff. Same not-a-real-movement spirit as spawn_actor seeding a
        finished tween outright rather than animating in from somewhere."""
        actor = self._cutscene_actor(args['actor'])
        if actor is None:
            return True
        row, col = args['to'][0], args['to'][1]
        if actor is self.player:
            self.player.row, self.player.col = float(row), float(col)
            self._player_vis = (self.player.row, self.player.col)
            self._player_walk_timer = 0
            self._player_anim = 0
        else:
            actor.row, actor.col = int(row), int(col)
            self._npc_tweens[actor.index] = {
                'src': (float(row), float(col)),
                'dst': (float(row), float(col)),
                'elapsed': _NPC_MOVE_MS,
            }
        return True

    def _cutscene_step_fade(self, args: dict, dt_ms: int, is_new: bool) -> bool:
        """Ramp a full-screen color overlay in (`direction: "in"`, toward
        clear) or out (`direction: "out"`, toward opaque), lerping over
        `duration_ms` -- same src/dst/elapsed/duration tween shape as
        _cutscene_step_pan_camera. `color` (`#rrggbb` or `rrggbb`, default
        black) is latched onto the shared overlay surface the instant this
        step starts, so a later fade step can pick a different color
        without the previous one bleeding through mid-tween.

        Unlike pan_camera's `to_player`, there's no equivalent auto-reset
        here: the overlay just holds at wherever the tween lands -- a
        fade-out stays solid until a later fade-in step or the cutscene
        ending (see _end_cutscene) clears it, because the entire point of
        this step is hiding something for as long as the author needs (e.g.
        a teleport_actor happening off-camera), not for a fixed beat the
        engine decides on its own.

        `see_through` (default off) redraws every entity on top of the
        overlay once it's blitted (see draw()/_draw_cutscene_fade) -- same
        "overlay first, entities redrawn on top" trick
        _draw_battle_transition_overlay already uses -- so a scripted beat
        can keep character sprites visible against the solid color instead
        of hiding everything. Dialogue boxes draw over the overlay
        regardless of this flag either way (see draw()'s own ordering)."""
        direction = args.get('direction', 'out')
        target_alpha = 255.0 if direction == 'out' else 0.0
        duration_ms = max(args.get('duration_ms', 0), 1)
        if is_new:
            color = (args.get('color') or '#000000').lstrip('#')
            self._cutscene_fade_overlay.fill(_hex_to_rgb(color))
            self._cutscene_fade_see_through = bool(args.get('see_through', False))
            self._cutscene_fade_anim = {
                'src': self._cutscene_fade_alpha, 'dst': target_alpha,
                'elapsed': 0, 'duration': duration_ms,
            }
        anim = self._cutscene_fade_anim
        anim['elapsed'] = min(anim['elapsed'] + dt_ms, anim['duration'])
        t = anim['elapsed'] / anim['duration']
        self._cutscene_fade_alpha = _lerp(anim['src'], anim['dst'], t)
        return anim['elapsed'] >= anim['duration']

    # ── camera ───────────────────────────────────────────────────────────────

    def _camera_offset_px(self) -> tuple[int, int]:
        """Top-left camera position in pixels: centered on the player's visual
        (tweened) position, clamped so the view never scrolls past a map edge.
        When the map is smaller than the viewport along an axis, there's no
        room to scroll at all, so that axis centers the map instead (a
        negative offset that letterboxes the excess viewport).

        `_camera_override`, if set, centers on that (row, col) instead --
        a cutscene's `pan_camera` step (_cutscene_step_pan_camera) is the
        only thing that ever sets it, and only while self.cutscene_player
        is active; it's always cleared by the time a cutscene ends
        (_end_cutscene), so ordinary play always centers on the player."""
        view_cols, view_rows = cfg.view_cols, cfg.view_rows
        player_row, player_col = (self._camera_override if self._camera_override is not None
                                   else self._player_vis)

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
        if self._battle_transition_phase == "hold":
            surface.fill((0, 0, 0))   # "the battle 'loads'" -- see start_battle
            return

        if self.battle_scene is not None:
            self.battle_scene.draw(surface)
            return

        if self._game_over:
            self._draw_game_over(surface)
            return

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

        if self._cutscene_fade_alpha > 0:
            self._draw_cutscene_fade(surface, cam_x, cam_y)

        if self._battle_transition_phase == "anim":
            self._draw_battle_transition_overlay(surface, cam_x, cam_y)

        if self.menu.is_open:
            self._draw_start_menu(surface)

        if self.save_menu.is_open:
            self._draw_save_menu(surface)

        if self.inventory_menu.is_open:
            self._draw_inventory_menu(surface)

        if self.item_action_menu.is_open:
            self._draw_item_action_menu(surface)

        if self.settings_menu.is_open:
            self._draw_settings_menu(surface)

        if self.party_menu.is_open:
            self._draw_party_menu(surface)

        if self.shop_menu.is_open:
            self._draw_shop_menu(surface)

        if self.dialogue.is_open:
            self._draw_dialogue_box(surface)

        if self._transition_phase is not None:
            self._draw_transition(surface)

    def _draw_cutscene_fade(self, surface: pygame.Surface, cam_x: int, cam_y: int) -> None:
        """Full-screen color overlay driven by a cutscene's own `fade` step
        (_cutscene_step_fade) -- unrelated to _draw_transition's warp fade,
        which is fixed-color/fixed-duration and drawn separately, later,
        at the very end of draw(). `see_through` redraws entities on top of
        the overlay afterward, same "overlay then entities again" trick
        _draw_battle_transition_overlay uses just below, so sprites stay
        visible against the solid color instead of being hidden by it."""
        self._cutscene_fade_overlay.set_alpha(round(self._cutscene_fade_alpha))
        surface.blit(self._cutscene_fade_overlay, (0, 0))
        if self._cutscene_fade_see_through:
            self._draw_entities(surface, cam_x, cam_y)

    def _draw_battle_transition_overlay(self, surface: pygame.Surface, cam_x: int, cam_y: int) -> None:
        """Battle-entry transition, "anim" phase only (see start_battle):
        the map+entities are already drawn (normal draw() pipeline, just
        above); this tiles the chosen animation's current frame across the
        whole screen on top of that, screen-locked (ignores camera scroll --
        this is a full-screen effect, not a world-space one), then redraws
        entities once more on top of *that* so the player/NPCs/enemies stay
        visible while the environment dissolves underneath them, per "overlay
        on ALL tiles, leaving only the currently on screen sprites visible."
        The frames themselves carry real per-pixel alpha (see
        load_transition_frames) -- early frames are mostly transparent,
        later ones opaque black, so this blit is a gradual reveal->black,
        not an instant swap."""
        frame = self._battle_transition_frames[self._battle_transition_frame_idx]
        view_w, view_h = surface.get_size()
        for y in range(0, view_h, self.tile_px):
            for x in range(0, view_w, self.tile_px):
                surface.blit(frame, (x, y))
        self._draw_entities(surface, cam_x, cam_y)

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

        self._draw_gold(surface)

    def _draw_gold(self, surface: pygame.Surface) -> None:
        """Right-aligned gold readout, bottom-right of the native surface —
        shared by the start menu and inventory screen (see callers)."""
        font = self.dialogue_font
        text = f"${self.game_state.gold}"
        w = font.size(text)[0]
        x = cfg.view_cols * self.tile_px - w - _INV_MARGIN
        y = cfg.view_rows * self.tile_px - font.get_linesize() - _INV_MARGIN
        surface.blit(font.render(text, False, _INV_TEXT_COLOR), (x, y))

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
        self._draw_gold(surface)

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
        current_party = self.roster.current_party(self.game_state)
        for i, item_id in enumerate(item_ids):
            item_def = self.item_defs[item_id]
            qty = self.inventory.counts.get(item_id, 0)
            suffix = f" x{qty}" if item_def.stackable and qty > 1 else ""
            cursor = "> " if i == selected else "  "
            # A weapon/armour piece currently worn by anyone always renders
            # dim, even while the cursor is on it -- still fully selectable
            # (its detail/description still show, and A still opens the
            # UNEQUIP popup), just visually marked as "already in use". This
            # is derived here each frame, never stored on the item itself --
            # see engine.roster.Roster.current_party.
            equipped = item_def.category in ("weapon", "armour") and any(
                m.equipped_weapon == item_id or m.equipped_armour == item_id
                for m in current_party
            )
            color = _INV_DIM_COLOR if equipped else (_INV_TEXT_COLOR if i == selected else _INV_DIM_COLOR)
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

    def _draw_item_action_menu(self, surface: pygame.Surface) -> None:
        """Small popup on top of the inventory screen for whichever row's A
        press opened it (see engine.input.handle_a_button/
        _item_action_options). Bottom-right corner box, dialogue_font + the
        same "> "/dim-color list idiom as everywhere else. Shows either
        this item's own (usually length-1) options row, or -- once USE/
        EQUIP is confirmed and picking_target kicks in (see
        engine.menu.ItemActionMenu) -- a scrollable list of current party
        members to apply it to."""
        font = self.dialogue_font
        line_h = font.get_linesize()
        view_w, view_h = cfg.view_cols * self.tile_px, cfg.view_rows * self.tile_px
        x, y = view_w - _ITEMACT_W, view_h - _ITEMACT_H
        pygame.draw.rect(surface, (0, 0, 0), pygame.Rect(x, y, _ITEMACT_W, _ITEMACT_H))
        pygame.draw.rect(surface, _ITEMACT_TEXT_COLOR, pygame.Rect(x, y, _ITEMACT_W, _ITEMACT_H), width=1)

        text_x, text_y = x + _ITEMACT_MARGIN, y + _ITEMACT_MARGIN
        menu = self.item_action_menu
        if menu.picking_target:
            for i, member in enumerate(self.roster.current_party(self.game_state)):
                cursor = "> " if i == menu.target_cursor else "  "
                color = _ITEMACT_TEXT_COLOR if i == menu.target_cursor else _ITEMACT_DIM_COLOR
                surface.blit(font.render(f"{cursor}{member.name}", False, color),
                             (text_x, text_y + i * line_h))
            return

        for i, option in enumerate(menu.options):
            cursor = "> " if i == menu.selected else "  "
            color = _ITEMACT_TEXT_COLOR if i == menu.selected else _ITEMACT_DIM_COLOR
            surface.blit(font.render(f"{cursor}{option}", False, color),
                         (text_x, text_y + i * line_h))

    def _draw_party_menu(self, surface: pygame.Surface) -> None:
        """PARTY status screen: solid black, dialogue_font -- left pane
        lists current party members (N/S, wraps), right pane shows the
        selected member's level/HP/MP/XP-to-next-level/equipment. Pure
        display -- no A-driven action, see engine.menu.PartyMenu."""
        surface.fill((0, 0, 0))
        font = self.dialogue_font
        line_h = font.get_linesize()

        surface.blit(font.render("< PARTY >", False, _PARTY_TEXT_COLOR),
                     (_PARTY_LIST_LEFT_X, _PARTY_HEADER_Y))
        self._draw_gold(surface)

        view_h = cfg.view_rows * self.tile_px
        for row in range((view_h - _PARTY_LIST_TOP_Y) // line_h):
            surface.blit(font.render("|", False, _PARTY_DIM_COLOR),
                         (_PARTY_DIVIDER_X, _PARTY_LIST_TOP_Y + row * line_h))

        members = self.roster.current_party(self.game_state)
        selected = self.party_menu.selected
        for i, member in enumerate(members):
            cursor = "> " if i == selected else "  "
            color = _PARTY_TEXT_COLOR if i == selected else _PARTY_DIM_COLOR
            surface.blit(font.render(f"{cursor}{member.name}", False, color),
                         (_PARTY_LIST_LEFT_X, _PARTY_LIST_TOP_Y + i * line_h))

        self._draw_party_detail(surface, members[selected])

    def _draw_party_detail(self, surface: pygame.Surface, member: 'PartyMember') -> None:
        """Right-half detail pane for whichever member is currently
        highlighted on the party screen."""
        font = self.dialogue_font
        line_h = font.get_linesize()
        x, y = _PARTY_DETAIL_LEFT_X, _PARTY_LIST_TOP_Y

        # TODO: show ATTACK/DEFENSE alongside the stats below. Neither is
        # stored on PartyMember today -- they're derived at battle start
        # (engine.battle.fighter_from_roster -> Fighter, from the member's
        # iq/weight/sweat/hair via raw_damage/defend_reduction's formulas,
        # plus the flat equipped_weapon/equipped_armour bonus) and thrown
        # away once the battle ends. Showing them here means either
        # building a Fighter off-battle just to read the numbers back off
        # it, or pulling the relevant formula math out of engine.battle so
        # both call sites can share it -- decide which before touching this.
        lines = [
            f"LVL {member.lvl}",
            f"HP  {member.hp}/{member.max_hp}",
            f"MP  {member.mp}/{member.max_mp}",
            f"XP  {member.xp}/{xp_to_next_level(member.lvl)}",
            "",
            f"WEAPON: {self._equip_item_name(member.equipped_weapon)}",
            f"ARMOUR: {self._equip_item_name(member.equipped_armour)}",
        ]
        for line in lines:
            surface.blit(font.render(line, False, _PARTY_TEXT_COLOR), (x, y))
            y += line_h

    def _equip_item_name(self, item_id: str | None) -> str:
        if item_id is None:
            return "NONE"
        item_def = self.item_defs.get(item_id)
        return item_def.name if item_def is not None else item_id

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

    def _draw_shop_menu(self, surface: pygame.Surface) -> None:
        """Alpha placeholder shop screen: solid black, dialogue_font only —
        same "no art yet" approach as inventory/settings. The shopkeeper's
        greeting/farewell are separate dialogue-box beats (see
        engine.input.handle_a_button/handle_b_button, engine.menu.
        ShopMenu.pending_shop) — this screen itself opens straight on a
        BUY/SELL mode header (E/W toggles, same "< X >" idiom as the
        inventory screen's category header), the current mode's scrollable
        list (N/S, clamped, cursor-highlighted like the inventory list), an
        "x{amount} TOTAL" line while a quantity's being picked, the current
        gold total, and a one-line result message after a transaction (see
        engine.input._confirm_shop_transaction).
        """
        surface.fill((0, 0, 0))
        font = self.dialogue_font
        line_h = font.get_linesize()

        shop = self._resolve_shop()
        if shop is None:
            return   # defensive -- handle_a_button closes the menu itself if this happens

        mode_label = "BUY" if self.shop_menu.mode == BUY else "SELL"
        surface.blit(font.render(f"< {mode_label} >", False, _SHOP_TEXT_COLOR),
                     (_SHOP_LIST_LEFT_X, _SHOP_HEADER_Y))

        if self.shop_menu.mode == BUY:
            rows = [(self.item_defs[e["item"]].name if e["item"] in self.item_defs else e["item"], e["price"])
                    for e in shop.stock]
        else:
            sellable = self.inventory.sellable_items(self.item_defs)
            rows = [(self.item_defs[item_id].name, self.item_defs[item_id].value) for item_id in sellable]

        if not rows:
            surface.blit(font.render("(nothing here)", False, _SHOP_DIM_COLOR),
                         (_SHOP_LIST_LEFT_X, _SHOP_LIST_TOP_Y))
        else:
            cursor = self.shop_menu.cursor
            for i, (name, price) in enumerate(rows):
                prefix = "> " if i == cursor else "  "
                color = _SHOP_TEXT_COLOR if i == cursor else _SHOP_DIM_COLOR
                line = f"{prefix}{name}  ${price}"
                surface.blit(font.render(line, False, color),
                             (_SHOP_LIST_LEFT_X, _SHOP_LIST_TOP_Y + i * line_h))

        if self.shop_menu.picking_amount and rows:
            _, price = rows[self.shop_menu.cursor]
            amount = self.shop_menu.amount
            y = cfg.view_rows * self.tile_px - line_h * 3 - _SHOP_MARGIN
            surface.blit(font.render(f"x{amount}  TOTAL ${price * amount}", False, _SHOP_TEXT_COLOR),
                         (_SHOP_LIST_LEFT_X, y))

        if self.shop_menu.message:
            y = cfg.view_rows * self.tile_px - line_h - _SHOP_MARGIN
            surface.blit(font.render(self.shop_menu.message, False, _SHOP_TEXT_COLOR),
                         (_SHOP_LIST_LEFT_X, y))

        self._draw_gold(surface)

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

        A running cutscene's own `dialogue` step can override this
        entirely via `position: "top"|"bottom"`, read directly off the
        currently-active step rather than cached anywhere -- it naturally
        stops applying the instant that step ends (whether it advances to
        another step or the cutscene finishes), no separate reset needed.
        Anything else (unset, "auto", ordinary non-cutscene dialogue) falls
        through to the player-position rule above.
        """
        view_h = surface.get_height()
        position_override = None
        if self.cutscene_player is not None:
            step = self.cutscene_player.current_step()
            if step is not None and step.kind == 'dialogue':
                position_override = step.args.get('position')
        if position_override == 'top':
            mirrored = True
        elif position_override == 'bottom':
            mirrored = False
        else:
            mirrored = self._player_screen_row() > view_h / 2

        box = self.menu_assets['dialogue_box_flipped' if mirrored else 'dialogue_box']
        box_h = box.get_height()
        blit_y = 0 if mirrored else view_h - box_h
        surface.blit(box, (0, blit_y))

        text_top_local = (box_h - _DIALOGUE_TEXT_BOTTOM) if mirrored else _DIALOGUE_TEXT_TOP
        text_y = blit_y + text_top_local

        screen_text = self.dialogue.visible_text()
        line_h = self.dialogue_font.get_linesize()
        text_lines = screen_text.split("\n")
        for i, line in enumerate(text_lines):
            line_surf = self.dialogue_font.render(line, False, _DIALOGUE_TEXT_COLOR)
            surface.blit(line_surf, (_DIALOGUE_TEXT_LEFT, text_y + i * line_h))

        # A cutscene dialogue step's response list (see engine.cutscene.
        # CutsceneChoice) draws directly below the page's own text, once
        # is_showing_choices() -- same "> " cursor-prefix idiom as every
        # other list menu in this game (e.g. _draw_shop_screen's stock
        # list), not a separate overlay.
        if self.dialogue.is_showing_choices():
            choice_top = text_y + len(text_lines) * line_h
            for i, choice in enumerate(self.dialogue.choices):
                selected = i == self.dialogue.choice_cursor
                prefix = "> " if selected else "  "
                color = _DIALOGUE_TEXT_COLOR if selected else _DIALOGUE_CHOICE_DIM_COLOR
                line_surf = self.dialogue_font.render(f"{prefix}{choice}", False, color)
                surface.blit(line_surf, (_DIALOGUE_TEXT_LEFT, choice_top + i * line_h))

    def _draw_tile_layer(self, surface: pygame.Surface, layer: 'TiledLayer', cam_x: int, cam_y: int) -> None:
        """Blit one tile layer's GID grid, offset by the camera. See draw()
        for how this and _draw_entities are interleaved in Tiled's real
        layer order.

        Only walks the rows/cols actually inside the camera's view (plus a
        2-tile margin, since cam_x/cam_y are sub-tile pixel values, not
        tile-aligned -- without it, a partially-scrolled-in tile at the
        view's trailing edge would get skipped, popping in a frame late).
        A map far bigger than the viewport (e.g. town.json at 166x85) is
        mostly off-screen at any given moment; walking every cell of every
        layer every frame regardless of camera position was the actual
        per-frame cost on slow hardware, not the blit itself -- pygame
        clips an off-screen dest for free, but the wasted Python-level
        loop iterations to get there weren't free.
        """
        tile_px = self.tile_px
        grid = layer.grid
        height = len(grid)
        width = len(grid[0]) if height else 0
        view_w, view_h = surface.get_size()

        row_start = max(0, cam_y // tile_px)
        row_end = min(height, row_start + view_h // tile_px + 2)
        col_start = max(0, cam_x // tile_px)
        col_end = min(width, col_start + view_w // tile_px + 2)

        for r in range(row_start, row_end):
            y = r * tile_px - cam_y
            row = grid[r]
            for c in range(col_start, col_end):
                surf = get_tile_by_gid(self.tile_surfaces, row[c])
                if surf:
                    surface.blit(surf, (c * tile_px - cam_x, y))

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
        # During the post-battle immunity window, blink the player sprite
        # on/off (_immunity_blink_visible, toggled in update()) so the
        # window actually reads as "briefly safe" instead of being invisible.
        if player_surf and (self._battle_immunity_ms <= 0 or self._immunity_blink_visible):
            vr, vc = self._player_vis
            surface.blit(player_surf, (round(vc * self.tile_px) - cam_x, round(vr * self.tile_px) - cam_y))


# ── Battle scene ──────────────────────────────────────────────────────────
#
# Graphical battle screen — wired into real play via OverworldScene.
# start_battle (touching an enemy on the overworld), and also reachable
# standalone via maptest.py's isolated "battles" debug mode. Scoped
# deliberately narrow (see engine.battle's module docstring): MELVIN vs.
# one enemy, no full party. The 4-option ATTACK/ITEM/DEFEND/RUN row is
# player-driven (N/S cursor + A confirm); ATTACK, ITEM, and RUN all do
# something when confirmed, DEFEND is still a selectable row with no effect
# wired in yet. When it becomes real, its exact scope needs to be nailed
# down first — don't wire it up based on a guess.

_BATTLE_TOP_H     = 40      # top textbox height, px — battle text
_BATTLE_BOTTOM_H  = 56      # bottom textbox height, px — party status + action menu
_BATTLE_MARGIN    = 8
_BATTLE_TEXT_COLOR = (255, 255, 255)
_BATTLE_DIM_COLOR  = (110, 110, 110)   # DEFEND (only remaining no-op row) / unselected item-pick rows
_BATTLE_MENU_X    = 160     # bottom-right pane start — same split as the start menu's _MENU_X
_BATTLE_HOLD_MS   = 700     # pause after a line's fully typed before the next action fires
_BATTLE_PRE_MENU_HOLD_MS = 3000   # pause after the enemy's turn message (or the
                                   # intro line) fully types before the party's
                                   # action menu replaces it -- long enough to
                                   # actually read what just happened; A/B
                                   # skips straight to the menu, see handle_event
_BATTLE_TEXT_W    = cfg.view_cols * cfg.tile_px - 2 * _BATTLE_MARGIN   # top-box word-wrap width, px

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
    """Graphical battle screen — MELVIN vs. one enemy.

    Implements the handle_event/update/draw protocol expected by
    engine.renderer.run(), same as OverworldScene. Reachable from real play
    via OverworldScene.start_battle (touching an enemy on the overworld),
    and also standalone via maptest.py's isolated "battles" debug mode,
    which prompts for an enemy id from data/enemy/enemies.yaml.

    The party's turn is player-driven: the ATTACK/ITEM/DEFEND/RUN row (see
    engine.battle.BattleMenu) is only shown while it's actually waiting on a
    choice (`self._awaiting_choice`) — N/S moves the cursor, A confirms.
    ATTACK, ITEM, and RUN all do something when confirmed; DEFEND is still
    a real row with no effect wired in yet (see engine.battle's module
    docstring). Confirming ITEM doesn't act immediately -- it opens a
    scrollable list of usable consumables (`self.menu.picking_item`, see
    `usable_battle_items`); confirming one of those applies it to the party
    Fighter and removes it from `inventory` (see `_confirm_item_choice`).
    The enemy's turn stays auto-resolved on the same hold-then-step timer as
    before — there's no choice to make there.

    `roster`/`level_override`, if given, are how a real (overworld-
    triggered) battle threads in live party HP/MP and the already-resolved
    overworld Enemy.level, via engine.renderer.OverworldScene.start_battle
    — both default to None so maptest.py's isolated "battles" debug mode
    (no save, no overworld Enemy) keeps working exactly as before: fresh
    stats off data/party/melvin.json, a fresh level roll off EnemyDef.level.
    `inventory`/`item_defs`, likewise, are how a real battle's ITEM row
    (and equipped weapon/armour bonuses, via `fighter_from_roster`) see the
    shared bag -- both default to None, in which case the ITEM row stays
    effectively inert (see `usable_battle_items`) and equip bonuses are 0,
    the same debug-mode behavior as before this existed.
    Once `self.phase` (via `self.battle`) reaches "win"/"loss" and the final
    message is fully typed, `awaiting_dismiss` goes True; one more A press
    sets `finished` — the caller (OverworldScene) polls that to know when to
    tear this scene down and credit/apply the outcome. Nothing polls it in
    maptest.py's debug mode, so an extra A press there is a harmless no-op.
    """

    def __init__(self, enemy_id: str, rng: random.Random | None = None,
                 roster: 'Roster | None' = None, level_override: int | None = None,
                 inventory: 'Inventory | None' = None, item_defs: dict | None = None):
        self.dialogue_font = pygame.font.Font(str(_DIALOGUE_FONT_PATH), _DIALOGUE_FONT_PT)
        self.rng = rng or random.Random()
        self.inventory = inventory
        self.item_defs = item_defs

        edef = load_enemy_defs()[enemy_id]
        level = level_override if level_override is not None else resolve_level(edef.level, self.rng)
        enemy = Fighter(name=edef.name, iq=edef.iq, weight=edef.weight,
                        sweat=edef.sweat, hair=edef.hair, level=level, is_enemy=True)
        if roster is not None:
            party = fighter_from_roster(_PARTY_DIR / 'melvin.json', roster.get('MELVIN'), item_defs)
        else:
            party = load_fighter(_PARTY_DIR / 'melvin.json')
        self.battle = BattleState(party=party, enemy=enemy, rng=self.rng, enemy_gold=edef.gold,
                                   enemy_xp=edef.xp, enemy_drop_item=edef.drop_item,
                                   enemy_drop_chance=edef.drop_chance, enemy_defeat_text=edef.defeat_text)

        self.battle_art = load_battle_art(_BATTLE_ART_DIR, edef.battle_art) if edef.battle_art else None
        if self.battle_art is None:
            print(f'WARNING: no battle_art for enemy {enemy_id!r} -- it will render as nothing', flush=True)

        # battle_bg is required per-enemy -- see EnemyDef.battle_bg / the
        # enemies.yaml template. No random fallback: a missing/invalid name
        # raises a KeyError, same as any other required enemies.yaml field.
        effect_name = edef.battle_bg
        effect_cls = _EFFECTS_BY_NAME[effect_name]
        self._effect = effect_cls()
        self._effect_canvas = pygame.Surface((256, 240))
        self._effect_t = 0.0

        self.text_box = DialogueBox()
        self.text_box.open([self._wrap_battle_text(edef.intro_text)])
        self._hold_elapsed = 0
        self.menu = BattleMenu()
        self._awaiting_choice = False   # True once it's the party's turn and the message is fully typed
        self.awaiting_dismiss = False   # True once phase is win/loss and the final message is fully typed
        self.finished = False           # True once the player dismisses a win/loss screen -- see docstring

        print(f'Battle: MELVIN vs {enemy.name} (LVL {enemy.level})  |  background: {effect_name}', flush=True)

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type != pygame.KEYDOWN:
            return
        if self.awaiting_dismiss:
            if _BUTTON_KEY.get(event.key) == 'A':
                self.finished = True
            return
        if self._awaiting_pre_menu_hold():
            # Reading the enemy's (or the intro's) message -- A or B skips
            # straight to the menu instead of waiting out
            # _BATTLE_PRE_MENU_HOLD_MS. Only live once that message is
            # fully typed; doesn't fast-forward the typewriter itself.
            if _BUTTON_KEY.get(event.key) in ('A', 'B'):
                self._show_battle_menu()
            return
        if not self._awaiting_choice:
            return
        if self.menu.picking_item:
            direction = _DIR_KEY.get(event.key)
            if direction:
                self.menu.move_item_cursor(direction, len(self.usable_battle_items()))
                return
            button = _BUTTON_KEY.get(event.key)
            if button == 'A':
                self._confirm_item_choice()
            elif button == 'B':
                self.menu.cancel_item_pick()
            return
        direction = _DIR_KEY.get(event.key)
        if direction:
            self.menu.move_cursor(direction)
            return
        if _BUTTON_KEY.get(event.key) == 'A':
            self._confirm_choice()

    def usable_battle_items(self) -> list[str]:
        """Item ids the ITEM row can offer right now -- consumables with an
        hp/mp effect that the party actually owns. Empty (ITEM stays
        effectively inert, same as DEFEND) if `self.inventory`/
        `self.item_defs` weren't given -- maptest.py's debug "battles" mode
        has neither."""
        if self.inventory is None or self.item_defs is None:
            return []
        return [item_id for item_id, item_def in self.item_defs.items()
                if item_def.category == "consumables" and item_def.effect
                and ("hp" in item_def.effect or "mp" in item_def.effect)
                and self.inventory.has(item_id)]

    def _awaiting_pre_menu_hold(self) -> bool:
        """True while the party's action menu is about to appear but hasn't
        yet -- the message that just finished typing (the enemy's move, or
        the intro line) is still held on screen for _BATTLE_PRE_MENU_HOLD_MS
        first, see update(). Used so handle_event can let A/B skip it."""
        return (self.battle.phase == "party_turn" and not self._awaiting_choice
                and self.text_box.is_fully_revealed())

    def _show_battle_menu(self) -> None:
        self.text_box.close()   # clear the last message -- see _draw_top_box
        self._awaiting_choice = True
        self._hold_elapsed = 0

    def _confirm_choice(self) -> None:
        option = self.menu.selected_option()
        if option == "ATTACK":
            action = self.battle.step
        elif option == "RUN":
            action = self.battle.attempt_run
        elif option == "ITEM":
            if self.usable_battle_items():
                self.menu.start_item_pick()
            return   # nothing usable -- ITEM stays inert, same as DEFEND
        else:
            return   # DEFEND is a real row, just a no-op for now -- see class docstring
        self._awaiting_choice = False
        self._hold_elapsed = 0
        self.text_box.open([self._wrap_battle_text(action())])

    def _confirm_item_choice(self) -> None:
        """Resolve whichever consumable is highlighted on the ITEM row's
        item-pick list -- applies it to the party Fighter (see
        BattleState.step_item), removes it from `inventory`, and ends the
        turn, same as confirming ATTACK."""
        item_id = self.usable_battle_items()[self.menu.item_cursor]
        item_def = self.item_defs[item_id]
        self.menu.cancel_item_pick()
        self._awaiting_choice = False
        self._hold_elapsed = 0
        flavor = self.battle.step_item(item_def.name, item_def.effect)
        self.inventory.remove(item_id)
        self.text_box.open([self._wrap_battle_text(flavor)])

    def update(self, dt_ms: int) -> None:
        self._effect_t += dt_ms / 1000
        self.text_box.tick(dt_ms, cfg.dialogue_char_ms)

        if not self.text_box.is_fully_revealed():
            return

        if self.battle.phase == "party_turn":
            if self._awaiting_choice:
                return   # already showing the menu, nothing more to do
            self._hold_elapsed += dt_ms
            if self._hold_elapsed < _BATTLE_PRE_MENU_HOLD_MS:
                return   # giving the player time to read -- see handle_event for the A/B skip
            self._show_battle_menu()
            return

        self._hold_elapsed += dt_ms
        if self._hold_elapsed < _BATTLE_HOLD_MS:
            return
        if self.battle.phase in ("win", "loss", "fled"):
            self.awaiting_dismiss = True   # final message held on screen -- A dismisses, see handle_event
            return
        self._hold_elapsed = 0
        self.text_box.open([self._wrap_battle_text(self.battle.step())])

    def _wrap_battle_text(self, text: str) -> str:
        """Word-wrap one line of battle flavor text to the top box's width,
        so a long line (e.g. a crit + a gold-reward line combined) breaks
        onto multiple lines instead of running off the right edge. Passed
        a tall rect_h so this always comes back as a single screen -- see
        _wrap_text_to_screens -- update() opens one page per battle.step()
        call and doesn't page through multi-screen text."""
        return _wrap_text_to_screens(self.dialogue_font, text, _BATTLE_TEXT_W, 9999)[0]

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

        if not self._awaiting_choice:
            return   # only shown while it's actually the player's turn to pick -- see class docstring

        # Own black panel behind the options, same idea as the start menu's
        # overlay -- covers the status line's right edge so long HP/MP text
        # can't bleed into the option column (exact position is a later tweak).
        pygame.draw.rect(surface, (0, 0, 0), pygame.Rect(_BATTLE_MENU_X, top, view_w - _BATTLE_MENU_X, _BATTLE_BOTTOM_H))

        if self.menu.picking_item:
            self._draw_item_pick_list(surface, top)
            return

        for i, option in enumerate(BATTLE_MENU_OPTIONS):
            prefix = "> " if i == self.menu.selected else "  "
            color = _BATTLE_TEXT_COLOR if option != "DEFEND" else _BATTLE_DIM_COLOR
            surface.blit(font.render(f"{prefix}{option}", False, color),
                         (_BATTLE_MENU_X, top + _BATTLE_MARGIN + i * line_h))

    def _draw_item_pick_list(self, surface: pygame.Surface, top: int) -> None:
        """Usable-consumable list shown in place of the ATTACK/ITEM/DEFEND/
        RUN row while self.menu.picking_item -- same "> "/dim-color idiom,
        name + owned qty per row."""
        font = self.dialogue_font
        line_h = font.get_linesize()
        item_ids = self.usable_battle_items()
        for i, item_id in enumerate(item_ids):
            item_def = self.item_defs[item_id]
            qty = self.inventory.counts.get(item_id, 0)
            prefix = "> " if i == self.menu.item_cursor else "  "
            color = _BATTLE_TEXT_COLOR if i == self.menu.item_cursor else _BATTLE_DIM_COLOR
            surface.blit(font.render(f"{prefix}{item_def.name} x{qty}", False, color),
                         (_BATTLE_MENU_X, top + _BATTLE_MARGIN + i * line_h))
