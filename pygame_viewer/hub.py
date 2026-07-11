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
from engine.enemy_state import place_hub_npcs, update_town_npcs
from engine.input import HeldDirectionInput
from engine.map_loader import hub_spawn_point, hub_str_grid, load_hub_grid
from engine.player import Player
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
_NPC_MOVE_MS  = 800    # ms between NPC AI position updates; also NPC tween duration
_PLAYER_MOVE_MS = cfg.player_move_ms   # tween duration for a player step; matches key-repeat rate
_TITLE        = 'simtank — hub (maptest)'

_DIR_KEY = {
    pygame.K_UP:    'N',
    pygame.K_DOWN:  'S',
    pygame.K_LEFT:  'W',
    pygame.K_RIGHT: 'E',
}


def _lerp(a: float, b: float, t: float) -> float:
    return a + (b - a) * t


def _npc_visual_pos(npc, tweens: dict) -> tuple[float, float]:
    """Interpolated (row, col) for an NPC given its in-flight tween, if any."""
    tw = tweens.get(npc.index)
    if tw is None:
        return float(npc.row), float(npc.col)
    t = min(tw['elapsed'] / _NPC_MOVE_MS, 1.0)
    return _lerp(tw['src'][0], tw['dst'][0], t), _lerp(tw['src'][1], tw['dst'][1], t)


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

    # No OS key repeat — held-key repeat is driven by engine.input.HeldDirectionInput
    # so that only one cardinal direction is ever stepped per tick, never two at
    # once. Two independent per-key OS repeats firing in the same frame is what
    # produced diagonal-looking moves before.

    # ── load assets ───────────────────────────────────────────────────────────
    print('Loading tileset...', flush=True)
    tiles   = load_tileset(_TOWN_PNG, _TOWN_RULES, _TILE_PX)
    sprites = load_sprites(_SPRITES_PNG, _SPRITES_TXT, _TILE_PX)
    print(f'  {len(tiles)} tiles  |  {len(sprites)} sprites', flush=True)

    # ── load hub state ────────────────────────────────────────────────────────
    grid     = load_hub_grid()
    str_grid = hub_str_grid(grid)
    npcs, _  = place_hub_npcs(grid)
    npc_rng  = random.Random(0x4875624E5043)

    player = Player.default()
    player.row, player.col = hub_spawn_point(grid)

    print(f'Hub: {len(grid)}r × {len(grid[0])}c  |  '
          f'player spawn ({player.row},{player.col})  |  '
          f'{len(npcs)} NPCs', flush=True)

    # ── timing ────────────────────────────────────────────────────────────────
    clock          = pygame.time.Clock()
    anim_frame     = 0       # 0 or 1, shared NPC animation tick
    anim_timer     = 0       # ms accumulator for NPC anim flip
    npc_move_timer = 0       # ms accumulator for NPC position update
    player_anim    = 0       # 0 or 1, walk-cycle frame, driven by tween progress

    # Visual (sub-tile) position tracking — engine stays tile-discrete; only the
    # renderer interpolates, so movement flows smoothly between tiles instead of
    # snapping. player_tween_elapsed >= _PLAYER_MOVE_MS means "at rest".
    player_tween_src     = (float(player.row), float(player.col))
    player_tween_dst     = player_tween_src
    player_tween_elapsed = _PLAYER_MOVE_MS
    player_vis_row, player_vis_col = player_tween_src

    # Held-key resolution ("last pressed wins", never a diagonal) lives in
    # engine/input.py — this is a pure decision, not a rendering concern.
    input_state = HeldDirectionInput(repeat_ms=_PLAYER_MOVE_MS)

    def _step_player(direction: str) -> None:
        nonlocal player_tween_src, player_tween_dst, player_tween_elapsed
        if player.try_move(direction, str_grid):
            # Rebase off wherever the sprite visually is right now (may be
            # mid-glide from the previous step) so repeated steps flow
            # continuously instead of stuttering.
            player_tween_src = (player_vis_row, player_vis_col)
            player_tween_dst = (float(player.row), float(player.col))
            player_tween_elapsed = 0

    # Same idea for NPCs, keyed by their stable `index`.
    npc_tweens = {
        npc.index: {
            'src':     (float(npc.row), float(npc.col)),
            'dst':     (float(npc.row), float(npc.col)),
            'elapsed': _NPC_MOVE_MS,
        }
        for npc in npcs
    }

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
                    input_state.press(direction)
            elif event.type == pygame.KEYUP:
                direction = _DIR_KEY.get(event.key)
                if direction:
                    input_state.release(direction)

        # ── NPC animation flip ────────────────────────────────────────────────
        anim_timer += dt
        if anim_timer >= _NPC_ANIM_MS:
            anim_frame ^= 1
            anim_timer -= _NPC_ANIM_MS

        # ── NPC position update ───────────────────────────────────────────────
        npc_move_timer += dt
        if npc_move_timer >= _NPC_MOVE_MS:
            before = {npc.index: (npc.row, npc.col) for npc in npcs}
            update_town_npcs(npcs, str_grid, npc_rng)
            for npc in npcs:
                old = before[npc.index]
                if old != (npc.row, npc.col):
                    npc_tweens[npc.index] = {
                        'src':     (float(old[0]), float(old[1])),
                        'dst':     (float(npc.row), float(npc.col)),
                        'elapsed': 0,
                    }
                else:
                    npc_tweens[npc.index]['elapsed'] = _NPC_MOVE_MS   # stayed put
            npc_move_timer -= _NPC_MOVE_MS

        # ── held-key auto-repeat ─────────────────────────────────────────────
        direction = input_state.tick(dt)
        if direction:
            _step_player(direction)

        # ── advance in-flight tweens ─────────────────────────────────────────
        player_tween_elapsed = min(player_tween_elapsed + dt, _PLAYER_MOVE_MS)
        t = player_tween_elapsed / _PLAYER_MOVE_MS
        player_vis_row = _lerp(player_tween_src[0], player_tween_dst[0], t)
        player_vis_col = _lerp(player_tween_src[1], player_tween_dst[1], t)
        player_anim = 0 if t < 0.5 else 1   # walk-cycle contact frame mid-step

        for tw in npc_tweens.values():
            if tw['elapsed'] < _NPC_MOVE_MS:
                tw['elapsed'] = min(tw['elapsed'] + dt, _NPC_MOVE_MS)

        # ── render ────────────────────────────────────────────────────────────
        screen.fill((0, 0, 0))
        _draw_map(screen, grid, tiles, _TILE_PX)

        for npc in npcs:
            npc_surf = get_npc_frame(sprites, npc.npc_sprite, anim_frame)
            if npc_surf:
                vr, vc = _npc_visual_pos(npc, npc_tweens)
                screen.blit(npc_surf, (round(vc * _TILE_PX), round(vr * _TILE_PX)))

        player_surf = get_party_frame(sprites, 'melvin', player.facing, player_anim)
        if player_surf:
            screen.blit(player_surf,
                         (round(player_vis_col * _TILE_PX), round(player_vis_row * _TILE_PX)))

        pygame.transform.scale(screen, (win_w, win_h), window)
        pygame.display.flip()

    pygame.quit()
