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

NPCs are not wired up yet: hub_fronthouse.json has no object layer, so there's
nothing to place. That comes back once NPC/Trigger/Warp object layers exist.

Controls:
  Arrow keys  — move player (MELVIN) / move start-menu cursor
  Enter       — START: open/close the start menu
  X           — A: confirm start-menu selection (stubbed — no sub-screens yet)
  Z           — B: close the start menu
  Escape / Q  — quit
"""
import json
import random
from dataclasses import dataclass
from pathlib import Path

import pygame

from engine.config import cfg
from engine.input import (
    HeldDirectionInput,
    handle_a_button,
    handle_b_button,
    handle_menu_direction,
    handle_start_button,
)
from engine.menu import StartMenu
from engine.player import Player, PlayerState

_REPO_ROOT   = Path(__file__).parent.parent
_ASSETS      = _REPO_ROOT / 'assets'
_HUB_MAP     = _REPO_ROOT / 'data' / 'maps' / 'hub_fronthouse.json'
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


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


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
    """Load the start menu's PNGs, keyed by the names used in _MENU_ROW_Y plus
    'bg' and 'cursor'. Call after pygame.display.set_mode()."""
    names = ('bg', 'cursor', 'inventory', 'party', 'settings', 'save')
    return {
        name: pygame.image.load(str(menu_dir / f'startmenu_{name}.png')).convert_alpha()
        for name in names
    }


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
    name: str
    grid: list[list[int]]           # GIDs, row-major [row][col]


@dataclass
class TiledMap:
    width:      int                 # tiles
    height:     int                 # tiles
    tilewidth:  int
    tileheight: int
    firstgid:   int
    tileset:    TiledTileset
    layers:     list[TiledLayer]


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
        layers.append(TiledLayer(name=layer["name"], grid=grid))

    return TiledMap(
        width      = width,
        height     = height,
        tilewidth  = raw["tilewidth"],
        tileheight = raw["tileheight"],
        firstgid   = firstgid,
        tileset    = tileset,
        layers     = layers,
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


# ── Scene ────────────────────────────────────────────────────────────────────

class OverworldScene:
    """A single walkable screen: tile grid, player, roaming NPCs.

    Implements the handle_event/update/draw protocol expected by
    engine.renderer.run(). Must be constructed after pygame.display.set_mode()
    so tileset/sprite loading can call .convert_alpha().
    """

    def __init__(self):
        self.tile_px = cfg.tile_px
        self.player_move_ms = cfg.player_move_ms

        print('Loading map...', flush=True)
        self.tmap = load_tiled_map(_HUB_MAP)
        self.tile_surfaces = load_tileset_by_gid(
            _ASSETS / 'tiles' / self.tmap.tileset.image,
            self.tmap.tileset.columns,
            self.tile_px,
        )
        self.passable = tiled_passable_grid(self.tmap)
        self.sprites = load_sprites(_SPRITES_PNG, _SPRITES_TXT, self.tile_px)
        self.menu_assets = load_menu_assets(_MENU_DIR)
        print(f'  {len(self.tile_surfaces)} tiles  |  {len(self.sprites)} sprites', flush=True)

        self.menu = StartMenu()

        # No object layer in hub_fronthouse.json yet, so no NPCs to place.
        self.npcs = []
        self._npc_rng = random.Random(0x4875624E5043)

        self.player = Player.default()
        self.player.row, self.player.col = tiled_spawn_point(self.tmap)

        print(f'Overworld: {self.tmap.height}r × {self.tmap.width}c  |  '
              f'player spawn ({self.player.row},{self.player.col})  |  '
              f'{len(self.npcs)} NPCs', flush=True)

        # No OS key repeat — held-key repeat is driven by engine.input.HeldDirectionInput
        # so that only one cardinal direction is ever stepped per tick, never two at
        # once. Two independent per-key OS repeats firing in the same frame is what
        # produced diagonal-looking moves before.
        self._input = HeldDirectionInput(repeat_ms=self.player_move_ms)

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

        # Same idea for NPCs, keyed by their stable `index`.
        self._npc_tweens = {
            npc.index: {
                'src':     (float(npc.row), float(npc.col)),
                'dst':     (float(npc.row), float(npc.col)),
                'elapsed': _NPC_MOVE_MS,
            }
            for npc in self.npcs
        }

    # ── input ────────────────────────────────────────────────────────────────

    def handle_event(self, event: pygame.event.Event) -> None:
        if event.type == pygame.KEYDOWN:
            direction = _DIR_KEY.get(event.key)
            if direction:
                if self.player.state == PlayerState.IN_MENU:
                    handle_menu_direction(direction, self.menu)
                else:
                    self._input.press(direction)

            button = _BUTTON_KEY.get(event.key)
            if button == 'START':
                handle_start_button(self.player, self.menu)
            elif button == 'B':
                handle_b_button(self.player, self.menu)
            elif button == 'A':
                handle_a_button(self.menu)   # stub — sub-screens not built yet
        elif event.type == pygame.KEYUP:
            direction = _DIR_KEY.get(event.key)
            if direction:
                self._input.release(direction)

    # ── update ───────────────────────────────────────────────────────────────

    def update(self, dt_ms: int) -> None:
        if self.menu.is_open:
            return   # start menu is a full pause — no gameplay ticks while open

        self._anim_timer += dt_ms
        if self._anim_timer >= _NPC_ANIM_MS:
            self._anim_frame ^= 1
            self._anim_timer -= _NPC_ANIM_MS

        self._npc_move_timer += dt_ms
        if self._npc_move_timer >= _NPC_MOVE_MS:
            self._npc_move_timer -= _NPC_MOVE_MS

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
        if self.player.try_move(direction, self.passable):
            # Rebase off wherever the sprite visually is right now (may be
            # mid-glide from the previous step) so repeated steps flow
            # continuously instead of stuttering.
            self._player_tween_src = self._player_vis
            self._player_tween_dst = (float(self.player.row), float(self.player.col))
            self._player_tween_elapsed = 0

    def _npc_visual_pos(self, npc) -> tuple[float, float]:
        tw = self._npc_tweens.get(npc.index)
        if tw is None:
            return float(npc.row), float(npc.col)
        t = min(tw['elapsed'] / _NPC_MOVE_MS, 1.0)
        return _lerp(tw['src'][0], tw['dst'][0], t), _lerp(tw['src'][1], tw['dst'][1], t)

    # ── camera ───────────────────────────────────────────────────────────────

    def _camera_offset_px(self) -> tuple[int, int]:
        """Top-left camera position in pixels: centered on the player's visual
        (tweened) position, clamped so the view never scrolls past a map edge."""
        view_cols, view_rows = cfg.view_cols, cfg.view_rows
        player_row, player_col = self._player_vis

        max_cam_col = max(self.tmap.width - view_cols, 0)
        max_cam_row = max(self.tmap.height - view_rows, 0)

        cam_col = min(max(player_col - view_cols / 2, 0), max_cam_col)
        cam_row = min(max(player_row - view_rows / 2, 0), max_cam_row)

        return round(cam_col * self.tile_px), round(cam_row * self.tile_px)

    # ── draw ─────────────────────────────────────────────────────────────────

    def draw(self, surface: pygame.Surface) -> None:
        surface.fill((0, 0, 0))
        cam_x, cam_y = self._camera_offset_px()

        self._draw_map(surface, cam_x, cam_y)

        for npc in self.npcs:
            npc_surf = get_npc_frame(self.sprites, npc.npc_sprite, self._anim_frame)
            if npc_surf:
                vr, vc = self._npc_visual_pos(npc)
                surface.blit(npc_surf, (round(vc * self.tile_px) - cam_x, round(vr * self.tile_px) - cam_y))

        player_surf = get_party_frame(self.sprites, 'melvin', self.player.facing, self._player_anim)
        if player_surf:
            vr, vc = self._player_vis
            surface.blit(player_surf, (round(vc * self.tile_px) - cam_x, round(vr * self.tile_px) - cam_y))

        if self.menu.is_open:
            self._draw_start_menu(surface)

    def _draw_start_menu(self, surface: pygame.Surface) -> None:
        """Overlay: bg, each option row (highlighted one shifted right), cursor."""
        surface.blit(self.menu_assets['bg'], (0, 0))

        selected_label = self.menu.selected_option().lower()
        for label, y in _MENU_ROW_Y.items():
            x = _MENU_X + (_MENU_SELECT_SHIFT_PX if label == selected_label else 0)
            surface.blit(self.menu_assets[label], (x, y))

        cursor_y = _MENU_ROW_Y[selected_label]
        surface.blit(self.menu_assets['cursor'], (_MENU_X, cursor_y))

    def _draw_map(self, surface: pygame.Surface, cam_x: int, cam_y: int) -> None:
        """Blit every tile layer, bottom to top, offset by the camera."""
        for layer in self.tmap.layers:
            for r, row in enumerate(layer.grid):
                y = r * self.tile_px - cam_y
                for c, gid in enumerate(row):
                    surf = get_tile_by_gid(self.tile_surfaces, self.tmap.firstgid, gid)
                    if surf:
                        surface.blit(surf, (c * self.tile_px - cam_x, y))
