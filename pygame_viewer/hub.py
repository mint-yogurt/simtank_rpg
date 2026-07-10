"""Pygame hub mode — player-controlled hub map with NPC animation.

Entry point: run_hub_pygame()

Controls:
  Arrow keys  — move player (MELVIN)
  Escape / Q  — quit
"""
import random
from pathlib import Path

import pygame

from engine.config import cfg
from engine.enemy_state import update_town_npcs
from engine.player import Player
from engine.scenes.hub import (
    _hub_str_grid, _place_hub_npcs, _spawn_positions, load_hub_grid,
)
from pygame_viewer.sprites import get_npc_frame, get_party_frame, load_sprites
from pygame_viewer.tileset import get_tile, load_tileset

_REPO_ROOT    = Path(__file__).parent.parent
_ASSETS       = _REPO_ROOT / 'assets'
_TOWN_PNG     = _ASSETS / 'tiles' / 'tiles_town.png'
_TOWN_RULES   = _ASSETS / 'tiles' / 'tiles_town_rules.txt'
_SPRITES_PNG  = _ASSETS / 'sprites' / 'party_sprites.png'
_SPRITES_TXT  = _ASSETS / 'sprites' / 'partysprites.txt'

_TILE_PX      = cfg.tile_px    # 16
_VIEW_W       = cfg.view_cols * _TILE_PX   # 256
_VIEW_H       = cfg.view_rows * _TILE_PX   # 224
_NPC_ANIM_MS  = 500    # ms per NPC animation frame flip
_NPC_MOVE_MS  = 800    # ms between NPC AI position updates
_TITLE        = 'simtank — hub (maptest)'

_DIR_KEY = {
    pygame.K_UP:    'N',
    pygame.K_DOWN:  'S',
    pygame.K_LEFT:  'W',
    pygame.K_RIGHT: 'E',
}


def _draw_map(surface: pygame.Surface, tile_grid: list,
              tiles: dict, tile_px: int) -> None:
    """Blit the full tile grid to surface. Hub fits exactly in the viewport."""
    for r, row in enumerate(tile_grid):
        for c, cell in enumerate(row):
            x, y = c * tile_px, r * tile_px
            if isinstance(cell, list):
                # [ground_tile, overlay_tile] — transparent overlay over ground
                gnd = get_tile(tiles, cell[0], tile_px)
                if gnd:
                    surface.blit(gnd, (x, y))
                ov = get_tile(tiles, cell[-1], tile_px)
                if ov:
                    surface.blit(ov, (x, y))
            else:
                surf = get_tile(tiles, cell, tile_px)
                if surf:
                    surface.blit(surf, (x, y))


def run_hub_pygame(scale: int | None = None) -> None:
    """Open a pygame window and run the hub scene with player control."""
    if scale is None:
        scale = cfg.pygame_scale

    pygame.init()
    win_w, win_h = _VIEW_W * scale, _VIEW_H * scale
    window = pygame.display.set_mode((win_w, win_h))
    pygame.display.set_caption(_TITLE)
    screen = pygame.Surface((_VIEW_W, _VIEW_H))  # native-res render target

    # Key repeat: first-press fires immediately, then repeats at player_move_ms
    pygame.key.set_repeat(cfg.player_move_ms, cfg.player_move_ms)

    # ── load assets ───────────────────────────────────────────────────────────
    print('Loading tileset...', flush=True)
    tiles   = load_tileset(_TOWN_PNG, _TOWN_RULES, _TILE_PX)
    sprites = load_sprites(_SPRITES_PNG, _SPRITES_TXT, _TILE_PX)
    print(f'  {len(tiles)} tiles  |  {len(sprites)} sprites', flush=True)

    # ── load hub state ────────────────────────────────────────────────────────
    grid     = load_hub_grid()
    str_grid = _hub_str_grid(grid)
    npcs, _  = _place_hub_npcs(grid)
    npc_rng  = random.Random(0x4875624E5043)

    spawns = _spawn_positions(grid)
    player = Player.default()
    player.row, player.col = spawns[0]   # MELVIN at first spawn slot

    print(f'Hub: {len(grid)}r × {len(grid[0])}c  |  '
          f'player spawn ({player.row},{player.col})  |  '
          f'{len(npcs)} NPCs', flush=True)

    # ── timing ────────────────────────────────────────────────────────────────
    clock          = pygame.time.Clock()
    anim_frame     = 0       # 0 or 1, shared NPC animation tick
    anim_timer     = 0       # ms accumulator for NPC anim flip
    npc_move_timer = 0       # ms accumulator for NPC position update
    player_anim    = 0       # 0 or 1, flips on each successful player step

    print(f'Window {win_w}×{win_h}  (render {_VIEW_W}×{_VIEW_H} × {scale}x)', flush=True)
    print('Controls: Arrow keys = move  |  Esc/Q = quit', flush=True)

    running = True
    while running:
        dt = clock.tick(60)   # returns ms since last frame; caps at 60 fps

        # ── events ────────────────────────────────────────────────────────────
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN:
                if event.key in (pygame.K_ESCAPE, pygame.K_q):
                    running = False
                    continue
                direction = _DIR_KEY.get(event.key)
                if direction:
                    moved = player.try_move(direction, str_grid)
                    if moved:
                        player_anim ^= 1   # toggle walk frame on each step

        # ── NPC animation flip ────────────────────────────────────────────────
        anim_timer += dt
        if anim_timer >= _NPC_ANIM_MS:
            anim_frame ^= 1
            anim_timer -= _NPC_ANIM_MS

        # ── NPC position update ───────────────────────────────────────────────
        npc_move_timer += dt
        if npc_move_timer >= _NPC_MOVE_MS:
            update_town_npcs(npcs, str_grid, npc_rng)
            npc_move_timer -= _NPC_MOVE_MS

        # ── render ────────────────────────────────────────────────────────────
        screen.fill((0, 0, 0))
        _draw_map(screen, grid, tiles, _TILE_PX)

        for npc in npcs:
            npc_surf = get_npc_frame(sprites, npc.npc_sprite, anim_frame)
            if npc_surf:
                screen.blit(npc_surf, (npc.col * _TILE_PX, npc.row * _TILE_PX))

        player_surf = get_party_frame(sprites, 'melvin', player.facing, player_anim)
        if player_surf:
            screen.blit(player_surf, (player.col * _TILE_PX, player.row * _TILE_PX))

        pygame.transform.scale(screen, (win_w, win_h), window)
        pygame.display.flip()

    pygame.quit()
