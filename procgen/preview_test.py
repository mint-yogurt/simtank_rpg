"""Visual preview harness: generate one overworld screen and composite a party
sprite onto it, producing a PNG in procgen/out/.

Run from the repo root:
    python procgen/preview_test.py [--seed N] [--sx X] [--sy Y]
                                   [--tile-col C] [--tile-row R]
                                   [--sprite SPRITE_NAME]

Defaults: seed=77777, screen (0,0), MELVIN standing on the nearest
non-enterable walkable tile to screen center.
"""

import argparse
import os
import sys

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))

from PIL import Image

from procgen.overworld_test import (
    TILE_PX,
    TILERULES_PATH,
    TILESET_PATH,
    _parse_tilerules,
    generate_screen_data,
    load_raw_tiles,
    render_screen_data,
)
from engine.tiles import is_enterable, is_passable

_HERE = os.path.dirname(os.path.abspath(__file__))

SPRITE_SHEET_PATH = os.path.normpath(
    os.path.join(_HERE, '..', 'web', 'static', 'sprites', 'party_sprites.png'))
SPRITE_RULES_PATH = os.path.normpath(
    os.path.join(_HERE, '..', 'web', 'static', 'sprites', 'partysprites.txt'))
OUTPUT_DIR = os.path.join(_HERE, 'out')


def load_party_sprites():
    """Return {sprite_name: RGBA Image} from party_sprites.png + partysprites.txt.

    Reuses _parse_tilerules (same col,row = name format as tilerules).
    Skips entries whose pixel coordinates fall outside the actual sheet bounds
    (partysprites.txt contains path-tile rows 6-7 from the overworld tilerules
    that are out of bounds for this 96px-tall sheet — harmless, just ignored).
    """
    sheet = Image.open(SPRITE_SHEET_PATH).convert('RGBA')
    sheet_w, sheet_h = sheet.size
    coords = _parse_tilerules(SPRITE_RULES_PATH)
    sprites = {}
    for name, (col, row) in coords.items():
        x, y = col * TILE_PX, row * TILE_PX
        if x + TILE_PX > sheet_w or y + TILE_PX > sheet_h:
            continue
        sprites[name] = sheet.crop((x, y, x + TILE_PX, y + TILE_PX))
    return sprites


def find_walkable_tile(grid, rows, cols):
    """Return (row, col) of the nearest non-enterable passable tile to center.

    Avoids hub/town/cave entrance tiles so MELVIN stands on plain terrain,
    not on top of a building. Falls back to any passable tile if needed.
    """
    cr, cc = rows // 2, cols // 2
    best = best_fb = None
    best_dist = best_fb_dist = float('inf')
    for r in range(rows):
        for c in range(cols):
            tile = (grid[r][c] or 'grass1').split(':')[0]
            if not is_passable(tile):
                continue
            d = abs(r - cr) + abs(c - cc)
            if not is_enterable(tile) and d < best_dist:
                best_dist, best = d, (r, c)
            if d < best_fb_dist:
                best_fb_dist, best_fb = d, (r, c)
    return best if best is not None else best_fb


def main():
    parser = argparse.ArgumentParser(
        description='Generate an overworld screen preview with a party sprite.')
    parser.add_argument('--seed', type=int, default=77777, help='World seed (default 77777)')
    parser.add_argument('--sx',   type=int, default=0,     help='Screen X coord (default 0)')
    parser.add_argument('--sy',   type=int, default=0,     help='Screen Y coord (default 0)')
    parser.add_argument('--tile-col', type=int, default=None,
                        help='Tile column for sprite placement (auto if omitted)')
    parser.add_argument('--tile-row', type=int, default=None,
                        help='Tile row for sprite placement (auto if omitted)')
    parser.add_argument('--sprite', type=str, default='melvinS1',
                        help='Sprite name from partysprites.txt (default melvinS1)')
    args = parser.parse_args()

    os.makedirs(OUTPUT_DIR, exist_ok=True)

    # ── Generate + render screen ───────────────────────────────────────────────
    print(f'Generating screen ({args.sx},{args.sy})  world_seed={args.seed} ...')
    data = generate_screen_data(args.seed, args.sx, args.sy)
    raw_tiles = load_raw_tiles(TILESET_PATH, TILERULES_PATH)
    screen_img = render_screen_data(data, raw_tiles)

    # ── Locate sprite tile ─────────────────────────────────────────────────────
    if args.tile_col is not None and args.tile_row is not None:
        tc, tr = args.tile_col, args.tile_row
        print(f'Using supplied tile col={tc} row={tr}')
    else:
        pos = find_walkable_tile(data.grid, data.rows, data.cols)
        if pos is None:
            print('Warning: no passable tile found; defaulting to screen center')
            tr, tc = data.rows // 2, data.cols // 2
        else:
            tr, tc = pos
        tile_name = (data.grid[tr][tc] or 'grass1').split(':')[0]
        print(f'Auto-selected walkable tile: col={tc} row={tr}  tile={tile_name!r}')

    # ── Load sprites ───────────────────────────────────────────────────────────
    sprites = load_party_sprites()
    sprite = sprites.get(args.sprite)
    if sprite is None:
        print(f'Error: sprite {args.sprite!r} not found.')
        print(f'Available sprites: {sorted(sprites.keys())}')
        sys.exit(1)

    # Sanity check: sprite cell is non-empty (non-fully-transparent)
    assert sprite.getbbox() is not None, (
        f'Sprite {args.sprite!r} loaded as fully transparent — check sheet coords')

    # ── Composite sprite over tile ─────────────────────────────────────────────
    px, py = tc * TILE_PX, tr * TILE_PX
    out = screen_img.copy()
    out.alpha_composite(sprite, dest=(px, py))

    # Verify composite changed pixels in that region
    region_box = (px, py, px + TILE_PX, py + TILE_PX)
    before = screen_img.crop(region_box).tobytes()
    after  = out.crop(region_box).tobytes()
    assert before != after, 'Composite had no effect — sprite may be fully transparent'

    # ── Save outputs ───────────────────────────────────────────────────────────
    fname = f'preview_{args.seed}_{args.sx}_{args.sy}_{tc}_{tr}.png'
    out_path = os.path.join(OUTPUT_DIR, fname)
    out.save(out_path)
    print(f'PNG saved: {out_path}')

    # Minimal HTML viewer (3× nearest-neighbour scale, no JS, no server)
    html_fname = fname.replace('.png', '.html')
    html_path  = os.path.join(OUTPUT_DIR, html_fname)
    img_w = data.cols * TILE_PX * 3
    img_h = data.rows * TILE_PX * 3
    with open(html_path, 'w') as f:
        f.write(f'''<!DOCTYPE html>
<html><head><meta charset="utf-8">
<title>{fname}</title>
<style>
  body {{ margin: 0; background: #111; display: flex;
         justify-content: center; align-items: center; min-height: 100vh; }}
  img  {{ image-rendering: pixelated; image-rendering: crisp-edges; }}
</style>
</head><body>
<img src="{fname}" width="{img_w}" height="{img_h}" alt="{fname}">
</body></html>
''')
    print(f'HTML viewer: {html_path}')

    # ── Log ────────────────────────────────────────────────────────────────────
    tile_at_pos = (data.grid[tr][tc] or 'grass1').split(':')[0]
    print(f'\n{args.sprite} standing on: {tile_at_pos!r}  pixel=({px},{py})')
    print(f'Sprite sheet: {SPRITE_SHEET_PATH}')
    sprite_sheet = Image.open(SPRITE_SHEET_PATH)
    sw, sh = sprite_sheet.size
    print(f'Sheet size: {sw}x{sh} = {sw//TILE_PX} cols x {sh//TILE_PX} rows at {TILE_PX}px/cell')
    print(f'Loaded {len(sprites)} sprites (out-of-bounds rows 6-7 in partysprites.txt skipped)')
    print()
    for line in data.log:
        print(line)


if __name__ == '__main__':
    main()
