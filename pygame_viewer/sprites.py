"""Party and NPC sprite loading for the pygame renderer.

Parses partysprites.txt (col,row = sprite_name) and slices party_sprites.png
into 16x16 named surfaces.

Public API:
  load_sprites(png_path, txt_path, tile_px) → dict[str, Surface]
  get_party_frame(sprites, member_name, facing, anim_frame) → Surface | None
  get_npc_frame(sprites, npc_sprite, anim_frame) → Surface | None
"""
from pathlib import Path

import pygame

# ── Frame name lookup tables ────────────────────────────────────────────────

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
