"""Load a tileset PNG into a dict of named pygame Surfaces.

parse_tilerules(path)  → {tile_name: (col, row)}
load_tileset(png, rules, tile_px) → {tile_name: Surface}

Rotation suffixes (':90', ':180', ':270') are handled lazily via get_tile().
Degrees are clockwise; pygame.transform.rotate is CCW so we negate.

IMPORTANT: call pygame.display.set_mode() BEFORE load_tileset so .convert_alpha() works.
"""
from pathlib import Path

import pygame

_rot_cache: dict[str, pygame.Surface] = {}


def parse_tilerules(path) -> dict[str, tuple[int, int]]:
    """Return {tile_name: (col, row)} from a tilerules txt file."""
    result: dict[str, tuple[int, int]] = {}
    for line in Path(path).read_text().splitlines():
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


def load_tileset(png_path, rules_path, tile_px: int = 16) -> dict[str, pygame.Surface]:
    """Load the tileset image and slice it into named surfaces.

    Each surface is tile_px × tile_px RGBA. Call after pygame.display.set_mode().
    """
    tile_map = parse_tilerules(rules_path)
    sheet = pygame.image.load(str(png_path)).convert_alpha()
    tiles: dict[str, pygame.Surface] = {}
    for name, (col, row) in tile_map.items():
        rect = pygame.Rect(col * tile_px, row * tile_px, tile_px, tile_px)
        surf = pygame.Surface((tile_px, tile_px), pygame.SRCALPHA)
        surf.blit(sheet, (0, 0), rect)
        tiles[name] = surf
    return tiles


def get_tile(tiles: dict[str, pygame.Surface], name: str,
             tile_px: int = 16) -> pygame.Surface | None:
    """Get a surface by name, handling ':90'/':180'/':270' rotation suffixes.

    Rotated surfaces are cached. Returns None if the base tile is unknown.
    """
    if name in tiles:
        return tiles[name]

    if ':' not in name:
        return None

    if name in _rot_cache:
        return _rot_cache[name]

    base, suffix = name.rsplit(':', 1)
    base_surf = tiles.get(base)
    if base_surf is None:
        return None

    try:
        deg = int(suffix)
    except ValueError:
        return base_surf

    rotated = pygame.transform.rotate(base_surf, -deg)  # CW → negate for pygame CCW
    _rot_cache[name] = rotated
    return rotated
