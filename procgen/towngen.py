"""Procedural town interior generator for simtank_rpg.

Pipeline:
  1. gen_ground       — grass fill → cobble / gravel / dirt blobs
  2. place_healer     — fixed 3×2 healer hut + pizza sign nearby
  3. place_buildings  — houseA, houseB, stone buildings
  4. place_scatter    — vegetation / scatter / containers in grass
  5. render_town      — crop to content + padding, save PNG (harness only)

Public API:
  generate_town_data(seed) → TownData
"""

import os
import random
from dataclasses import dataclass
from PIL import Image

# ── CONFIG ────────────────────────────────────────────────────────────────────
TILE_PX       = 16
MIN_BUILDINGS = 1    # extra buildings beyond the mandatory healer hut
MAX_BUILDINGS = 9
CANVAS_H      = 14   # overwritten each run based on building count
CANVAS_W      = 16   # overwritten each run based on building count
CANVAS_PAD    = 2

SCATTER_CHANCE   = 0.030
CONTAINER_CHANCE = 0.012

_HERE         = os.path.dirname(os.path.abspath(__file__))
TILESET_PATH  = os.path.join(_HERE, '..', 'assets', 'tiles', 'tiles_town.png')
TILERULES_PATH = os.path.join(_HERE, '..', 'assets', 'tiles', 'tiles_town_rules.txt')
OUTPUT_DIR    = os.path.join(_HERE, 'out')

# ── PALETTE ───────────────────────────────────────────────────────────────────
# 7 placeholder colors from the town tileset rules (in order)
SRC_COLORS = [
    (0x86, 0xBB, 0x2D),  # green
    (0x6F, 0x5C, 0x00),  # brown
    (0x34, 0x28, 0x00),  # dark brown
    (0x78, 0x78, 0x78),  # grey
    (0x35, 0x6D, 0x00),  # dark green
    (0xDF, 0x70, 0x26),  # orange
    (0x5E, 0xCD, 0xE4),  # cyan
    (0x31, 0xBA, 0xA7),  # teal-green
]

NES_PALETTE = [
    ["7c7c7c","0100fc","0000bc","4527bb","930084","a80021","aa1101","881300",
     "503001","007700","006801","005801","004059"],
    ["bcbcbc","0178f8","0058f8","6844fc","d800cd","e6005a","f93801","e45c10",
     "ac7c00","00b800","01a800","00a843","018788"],
    ["f8f8f8","3cbcfd","6988fc","9877f9","f978f9","f85898","fa7759","fca144",
     "f9b701","b7f818","5ad755","58f898","00e8d8"],
    ["fcfcfc","a4e4fd","b8b7f9","d8b8fb","f9b7f7","f8a3c0","f1d0b1","fddfa9",
     "f9d87b","d8f977","b9faba","b8f7d8","01fdfe"],
]

def _hex_rgb(h):
    return (int(h[0:2], 16), int(h[2:4], 16), int(h[4:6], 16))

def pick_palette(rng):
    """Pick 8 non-adjacent NES palette cells. Excludes (3,0)=fcfcfc (white)."""
    eligible = [(r, c) for r in range(4) for c in range(13)
                if not (r == 3 and c == 0)]
    picked = []
    while len(picked) < 8:
        cell = rng.choice(eligible)
        if all(abs(cell[0]-p[0]) + abs(cell[1]-p[1]) > 1 for p in picked):
            picked.append(cell)
    return tuple(_hex_rgb(NES_PALETTE[r][c]) for r, c in picked)

def remap_tileset(raw_tiles, palette):
    """Swap 8 placeholder colours → this run's rolled palette; black/white unchanged."""
    remap = dict(zip(SRC_COLORS, palette))
    result = {}
    for name, img in raw_tiles.items():
        data = list(img.getdata())
        data = [(remap.get((r, g, b), (r, g, b)) + (a,)) for r, g, b, a in data]
        out = img.copy()
        out.putdata(data)
        result[name] = out
    return result

# ── TILE LOADING ──────────────────────────────────────────────────────────────
def _parse_tilerules(path):
    tiles = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or '=' not in line:
                continue
            eq   = line.index('=')
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
            tiles[name] = (col, row)
    return tiles

def load_raw_tiles(tileset_path, tilerules_path):
    sheet  = Image.open(tileset_path).convert('RGBA')
    coords = _parse_tilerules(tilerules_path)
    tiles  = {}
    for name, (col, row) in coords.items():
        x, y = col * TILE_PX, row * TILE_PX
        tiles[name] = sheet.crop((x, y, x + TILE_PX, y + TILE_PX))
    # Derive rotated gravel border tiles from gravelW (left=grass edge)
    if 'gravelW' in tiles:
        gw = tiles['gravelW']
        tiles['gravelE'] = gw.transpose(Image.ROTATE_180)
        tiles['gravelN'] = gw.transpose(Image.ROTATE_270)  # left edge → top edge
        tiles['gravelS'] = gw.transpose(Image.ROTATE_90)   # left edge → bottom edge
    return tiles

# ── GROUND GENERATION ─────────────────────────────────────────────────────────
_GRASS = ['grass1', 'grass2', 'grass3', 'grass4']

def gen_ground(rng):
    """Grass base fill + gravel area blobs. Cobble/dirt placed after buildings."""
    grid = {}
    for r in range(CANVAS_H):
        for c in range(CANVAS_W):
            grid[(r, c)] = rng.choice(_GRASS)

    # Collect ALL gravel positions first, then derive each tile from its actual
    # neighbours. This prevents overlapping blobs from corrupting each other's
    # border tiles.
    gravel = set()
    area_units = max(1, (CANVAS_W * CANVAS_H) // 100)
    for _ in range(rng.randint(1, max(2, area_units // 2))):
        _collect_gravel_rect(gravel, rng)

    for r, c in gravel:
        grid[(r, c)] = _gravel_tile(r, c, gravel, rng)

    return grid

def _collect_gravel_rect(cells, rng):
    max_bw = max(4, CANVAS_W // 3)
    max_bh = max(3, CANVAS_H // 3)
    bw = rng.randint(4, max_bw)
    bh = rng.randint(3, max_bh)
    if CANVAS_H - bh - 2 <= 2 or CANVAS_W - bw - 2 <= 2:
        return
    r0 = rng.randint(2, CANVAS_H - bh - 2)
    c0 = rng.randint(2, CANVAS_W - bw - 2)
    for dr in range(bh):
        for dc in range(bw):
            cells.add((r0 + dr, c0 + dc))

def _gravel_tile(r, c, cells, rng):
    """Derive the correct gravel tile from actual neighbours in the full cell set."""
    n = (r-1, c) in cells
    s = (r+1, c) in cells
    w = (r, c-1) in cells
    e = (r, c+1) in cells
    if n and s and w and e:                return rng.choice(['gravel1', 'gravel2'])
    if not n and not w:                    return 'gravelNW'
    if not n and not e:                    return 'gravelNE'
    if not s and not w:                    return 'gravelSW'
    if not s and not e:                    return 'gravelSE'
    if not n:                              return 'gravelN'
    if not s:                              return 'gravelS'
    if not w:                              return 'gravelW'
    if not e:                              return 'gravelE'
    return rng.choice(['gravel1', 'gravel2'])

# ── COBBLE PATHS — 1-tile wide, MST between building centres ─────────────────
def _mst_edges(centers):
    """Prim's MST on Manhattan distance between center points."""
    n = len(centers)
    if n <= 1:
        return []
    in_tree, not_in = [0], list(range(1, n))
    edges = []
    while not_in:
        best = None
        for i in in_tree:
            ri, ci = centers[i]
            for j in not_in:
                rj, cj = centers[j]
                d = abs(ri - rj) + abs(ci - cj)
                if best is None or d < best[0]:
                    best = (d, i, j)
        _, i, j = best
        edges.append((i, j))
        in_tree.append(j)
        not_in.remove(j)
    return edges

def _draw_cobble_L(ground_grid, overlay, r1, c1, r2, c2, rng):
    """Draw one L-shaped cobble1 path between two points."""
    if rng.random() < 0.5:
        for c in range(min(c1, c2), max(c1, c2) + 1):
            if (r1, c) not in overlay:
                ground_grid[(r1, c)] = 'cobble1'
        for r in range(min(r1, r2), max(r1, r2) + 1):
            if (r, c2) not in overlay:
                ground_grid[(r, c2)] = 'cobble1'
    else:
        for r in range(min(r1, r2), max(r1, r2) + 1):
            if (r, c1) not in overlay:
                ground_grid[(r, c1)] = 'cobble1'
        for c in range(min(c1, c2), max(c1, c2) + 1):
            if (r2, c) not in overlay:
                ground_grid[(r2, c)] = 'cobble1'


def place_cobble_paths(ground_grid, overlay, buildings, rng, crop_box):
    """L-shaped 1-tile cobble1 paths connecting ~50% of MST edges from building south edges.
    One path trails off to the nearest crop_box boundary."""
    if len(buildings) < 2:
        return
    # Endpoints at south edge centre of each building
    anchors = [(r0 + h, c0 + w // 2) for r0, c0, h, w in buildings]
    edges = _mst_edges(anchors)
    # Keep at least 1 edge; randomly skip ~50% of the rest
    rng.shuffle(edges)
    keep_count = max(1, len(edges) - len(edges) // 2)
    kept = edges[:keep_count]
    for i, j in kept:
        _draw_cobble_L(ground_grid, overlay, anchors[i][0], anchors[i][1],
                       anchors[j][0], anchors[j][1], rng)
    # Trail one path to the nearest crop_box edge
    min_r, min_c, max_r, max_c = crop_box
    trail_idx = rng.randrange(len(anchors))
    tr, tc = anchors[trail_idx]
    dists = {
        'N': tr - min_r,
        'S': max_r - tr,
        'W': tc - min_c,
        'E': max_c - tc,
    }
    direction = min(dists, key=dists.get)
    if direction == 'N':
        for r in range(min_r, tr + 1):
            if (r, tc) not in overlay:
                ground_grid[(r, tc)] = 'cobble1'
    elif direction == 'S':
        for r in range(tr, max_r + 1):
            if (r, tc) not in overlay:
                ground_grid[(r, tc)] = 'cobble1'
    elif direction == 'W':
        for c in range(min_c, tc + 1):
            if (tr, c) not in overlay:
                ground_grid[(tr, c)] = 'cobble1'
    else:
        for c in range(tc, max_c + 1):
            if (tr, c) not in overlay:
                ground_grid[(tr, c)] = 'cobble1'

# ── DIRT PATHS / COURTYARDS — placed AFTER buildings ─────────────────────────
def place_courtyards(ground_grid, overlay, occupied, rng, cluster_box):
    """Dirt paths / small courtyards placed inside the town cluster."""
    if cluster_box is None:
        return

    cr0, cc0, cr1, cc1 = cluster_box

    # ── Dirt paths / small courtyards ────────────────────────────────────────
    dirt = set()
    n_dirt = rng.randint(0, 2)
    dirt_centers = []

    for _ in range(n_dirt):
        bw = rng.randint(3, 5)
        bh = rng.randint(3, 4)
        for _ in range(40):
            r0 = rng.randint(max(1, cr0), max(cr0+1, cr1 - bh))
            c0 = rng.randint(max(1, cc0), max(cc0+1, cc1 - bw))
            cells = [(r0+dr, c0+dc) for dr in range(bh) for dc in range(bw)]
            if any(cell in overlay for cell in cells):
                continue
            for cell in cells:
                dirt.add(cell)
            dirt_centers.append((r0 + bh // 2, c0 + bw // 2))
            break

    # Connect dirt areas with horizontal + vertical path tiles
    if len(dirt_centers) >= 2:
        (r1, c1), (r2, c2) = dirt_centers[0], dirt_centers[1]
        for c in range(min(c1, c2), max(c1, c2) + 1):
            if (r1, c) not in dirt:
                dirt.add((r1, c))
                ground_grid[(r1, c)] = 'dirtpathHorizontal'
        for r in range(min(r1, r2), max(r1, r2) + 1):
            if (r, c2) not in dirt:
                dirt.add((r, c2))
                ground_grid[(r, c2)] = 'dirthpathVertical'

    for r, c in dirt:
        if (r, c) not in overlay:
            ground_grid[(r, c)] = _dirt_tile(r, c, dirt)

def _dirt_tile(r, c, cells):
    """Full border tile set for dirt area blobs."""
    n = (r-1, c) in cells
    s = (r+1, c) in cells
    w = (r, c-1) in cells
    e = (r, c+1) in cells
    if n and s and w and e:  return 'dirtmid'
    if not n and not w:      return 'dirtNW'
    if not n and not e:      return 'dirtNE'
    if not s and not w:      return 'dirtSW'
    if not s and not e:      return 'dirtSE'
    if not n:                return 'dirtN'
    if not s:                return 'dirtS'
    if not w:                return 'dirtW'
    if not e:                return 'dirtE'
    return 'dirtmid'

# ── FOOTPRINT HELPERS ─────────────────────────────────────────────────────────
def _fits(r0, c0, h, w, occupied, buf=1):
    for dr in range(-buf, h + buf):
        for dc in range(-buf, w + buf):
            if (r0 + dr, c0 + dc) in occupied:
                return False
    return True

def _claim(r0, c0, h, w, occupied, buf=1):
    for dr in range(-buf, h + buf):
        for dc in range(-buf, w + buf):
            occupied.add((r0 + dr, c0 + dc))


# ── HEALER HUT ────────────────────────────────────────────────────────────────
def place_healer_hut(overlay, occupied, buildings, rng):
    """3 wide × 2 tall. Returns cluster_box seeded at hut position, or None."""
    for _ in range(300):
        r0 = rng.randint(2, max(3, CANVAS_H - 5))
        c0 = rng.randint(2, max(3, CANVAS_W - 6))
        if not _fits(r0, c0, 2, 3, occupied):
            continue
        overlay[(r0,   c0)]   = 'healerHutroof_W'
        overlay[(r0,   c0+1)] = 'healerHutroof_mid'
        overlay[(r0,   c0+2)] = 'healerHutroof_E'
        overlay[(r0+1, c0)]   = 'healerHutwall_W'
        overlay[(r0+1, c0+1)] = 'healerHutwallMid'
        overlay[(r0+1, c0+2)] = 'healerHutwall_E'
        _claim(r0, c0, 2, 3, occupied)
        _place_pizza(overlay, occupied, r0, c0, rng)
        buildings.append((r0, c0, 2, 3))
        return (r0, c0, r0 + 2, c0 + 3)
    return None

def _place_pizza(overlay, occupied, hut_r, hut_c, rng):
    """Pizza sign (3 wide) + 1-2 tile pole below mid, placed south of healer hut."""
    for _ in range(30):
        r0 = hut_r + rng.randint(3, 6)
        c0 = hut_c + rng.randint(-3, 3)
        if (r0 < 2 or r0 + 2 >= CANVAS_H or c0 < 2 or c0 + 3 >= CANVAS_W
                or not _fits(r0, c0, 2, 3, occupied, buf=0)):
            continue
        overlay[(r0, c0)]   = 'pizzaSignW'
        overlay[(r0, c0+1)] = 'pizzaSignmid'
        overlay[(r0, c0+2)] = 'pizzaSignE'
        pole_h = rng.randint(1, 2)
        for ph in range(pole_h):
            overlay[(r0 + 1 + ph, c0 + 1)] = 'pizzaSignPole'
        _claim(r0, c0, 2, 3, occupied, buf=0)
        return

# ── CLUSTER PLACEMENT HELPER ──────────────────────────────────────────────────
def _pick_pos(rng, h, w, cluster_box):
    """Return a (r0, c0) candidate respecting cluster bias.

    92 % of the time tries inside the cluster bounding box + 3-tile margin.
    8 % of the time (or when no cluster yet) uses the full canvas.
    """
    use_cluster = cluster_box is not None and rng.random() < 0.92
    if use_cluster:
        cr0, cc0, cr1, cc1 = cluster_box
        margin = 3
        r_lo = max(2, cr0 - margin)
        r_hi = min(CANVAS_H - h - 2, cr1 + margin)
        c_lo = max(2, cc0 - margin)
        c_hi = min(CANVAS_W - w - 2, cc1 + margin)
        if r_hi > r_lo and c_hi > c_lo:
            return rng.randint(r_lo, r_hi), rng.randint(c_lo, c_hi)
    # full canvas fallback (outlier or no cluster yet)
    r_hi = CANVAS_H - h - 2
    c_hi = CANVAS_W - w - 2
    if r_hi < 2 or c_hi < 2:
        return None, None
    return rng.randint(2, r_hi), rng.randint(2, c_hi)

def _expand_cluster(box, r0, c0, h, w):
    if box is None:
        return (r0, c0, r0 + h, c0 + w)
    return (min(box[0], r0), min(box[1], c0),
            max(box[2], r0 + h), max(box[3], c0 + w))

# ── HOUSE A / B ───────────────────────────────────────────────────────────────
def place_house(overlay, occupied, buildings, rng, cluster_box, variant=None):
    if variant is None:
        variant = rng.choice(['A', 'B'])
    for _ in range(120):
        w = rng.randint(2, 6)
        h = rng.randint(2, 4)
        if h == 2 and w > 2:
            h = 3
        r0, c0 = _pick_pos(rng, h, w, cluster_box)
        if r0 is None:
            continue
        if not _fits(r0, c0, h, w, occupied):
            continue
        _render_house(overlay, r0, c0, w, h, variant, rng)
        _claim(r0, c0, h, w, occupied)
        buildings.append((r0, c0, h, w))
        return _expand_cluster(cluster_box, r0, c0, h, w)
    return cluster_box

def _render_house(overlay, r0, c0, w, h, variant, rng):
    is_tiny = (w == 2 and h == 2)

    trans_opt = 'transition2' if variant == 'B' else 'transition'
    if is_tiny:
        roof_type = rng.choice(['side', trans_opt])
    else:
        roof_type = rng.choice(['roof1', 'roof2', trans_opt])

    row = 0

    # ── Topper row (all sizes except tiny-with-side)
    if not (is_tiny and roof_type == 'side'):
        topper = 'roof1topper' if roof_type == 'roof1' else 'roof2topper'
        for dc in range(w):
            overlay[(r0 + row, c0 + dc)] = topper
        row += 1

    # ── Roof row
    _write_roof_row(overlay, r0 + row, c0, w, roof_type, variant)
    row += 1

    # ── Upper floor rows (none for h <= 3)
    for _ in range(h - 3):
        _write_wall_row(overlay, r0 + row, c0, w, variant, is_ground=False, rng=rng)
        row += 1

    # ── Ground floor
    door_col = rng.randint(0, w - 1)
    _write_wall_row(overlay, r0 + row, c0, w, variant, is_ground=True, rng=rng,
                    door_col=door_col)

def _write_roof_row(overlay, r, c0, w, roof_type, variant):
    if roof_type == 'side':
        if variant == 'A':
            overlay[(r, c0)], overlay[(r, c0+1)] = 'roof2sideW', 'roof2sideE'
        else:
            overlay[(r, c0)], overlay[(r, c0+1)] = 'roof2side2W', 'roof2side2E'
    elif roof_type == 'transition':
        # Pair always adjacent; fill remaining width with roof2
        overlay[(r, c0)]     = 'roof2transitionW'
        overlay[(r, c0 + 1)] = 'roof2transitionE'
        for dc in range(2, w):
            overlay[(r, c0 + dc)] = 'roof2'
    elif roof_type == 'transition2':
        overlay[(r, c0)]     = 'roof2transition2W'
        overlay[(r, c0 + 1)] = 'roof2transition2E'
        for dc in range(2, w):
            overlay[(r, c0 + dc)] = 'roof2'
    elif roof_type == 'roof1':
        for dc in range(w):
            overlay[(r, c0 + dc)] = 'roof1'
    else:  # roof2
        for dc in range(w):
            overlay[(r, c0 + dc)] = 'roof2'

def _write_wall_row(overlay, r, c0, w, variant, is_ground, rng, door_col=None):
    if variant == 'A':
        if is_ground:
            wall = lambda: rng.choice(['wallA3', 'wallA4'])
            win  = lambda: rng.choice(['windowA3', 'windowA4'])
            door = 'door1'
        else:
            wall = lambda: rng.choice(['wallA1', 'wallA2'])
            win  = lambda: rng.choice(['windowA1', 'windowA2'])
            door = None
    else:
        if is_ground:
            wall = lambda: rng.choice(['wallB3', 'wallB4'])
            win  = lambda: rng.choice(['windowB3', 'windowB4'])
            door = 'door2'
        else:
            wall = lambda: rng.choice(['wallB1', 'wallB2'])
            win  = lambda: rng.choice(['windowB1', 'windowB2'])
            door = None

    for dc in range(w):
        c = c0 + dc
        if door_col is not None and dc == door_col and door:
            overlay[(r, c)] = door
        elif rng.random() < 0.28:
            overlay[(r, c)] = win()
        else:
            overlay[(r, c)] = wall()

# ── STONE BUILDINGS ───────────────────────────────────────────────────────────
def place_stone_building(overlay, occupied, buildings, rng, cluster_box):
    for _ in range(120):
        w = rng.randint(3, 8)
        h = rng.randint(3, 5)
        r0, c0 = _pick_pos(rng, h, w, cluster_box)
        if r0 is None:
            continue
        if not _fits(r0, c0, h, w, occupied):
            continue
        _render_stone(overlay, r0, c0, w, h, rng)
        _claim(r0, c0, h, w, occupied)
        buildings.append((r0, c0, h, w))
        return _expand_cluster(cluster_box, r0, c0, h, w)
    return cluster_box

def _render_stone(overlay, r0, c0, w, h, rng):
    use_roof = rng.random() < 0.45
    stepped  = rng.random() < 0.35 and w >= 5 and h >= 4

    if stepped:
        split  = h // 2
        upper_h = h - split
        upper_w = max(w - 2, 2)
        upper_c0 = c0 + (w - upper_w) // 2
        _stone_rect(overlay, r0,         upper_c0, upper_w, upper_h, rng,
                    is_top=True, use_roof=use_roof)
        _stone_rect(overlay, r0 + upper_h, c0,     w,       split,   rng,
                    is_top=False, use_roof=False)
        gnd_r  = r0 + h - 1
        gnd_c0 = c0
    else:
        _stone_rect(overlay, r0, c0, w, h, rng, is_top=True, use_roof=use_roof)
        gnd_r  = r0 + h - 1
        gnd_c0 = c0

    # Doors on ground floor
    n_doors = rng.randint(1, min(3, w))
    for dc in rng.sample(range(w), n_doors):
        overlay[(gnd_r, gnd_c0 + dc)] = 'doorStone'

    # Optional towers rising above the building top
    if rng.random() < 0.35:
        _stone_towers(overlay, r0, c0, w, rng)

def _stone_rect(overlay, r0, c0, w, h, rng, is_top, use_roof):
    for dr in range(h):
        r = r0 + dr
        for dc in range(w):
            c = c0 + dc
            if is_top and dr == 0:
                if use_roof:
                    overlay[(r, c)] = 'roof1'
                else:
                    overlay[(r, c)] = rng.choice(['stoneTopper1', 'stoneTopper2'])
            elif dr == h - 1:
                # Ground floor
                if rng.random() < 0.3:
                    overlay[(r, c)] = rng.choice(['stoneWindow3', 'stoneWindow4'])
                else:
                    overlay[(r, c)] = rng.choice(['stoneBricks3', 'stoneBricks4'])
            else:
                if rng.random() < 0.22:
                    overlay[(r, c)] = rng.choice(['stoneWindow1', 'stoneWindow2'])
                else:
                    overlay[(r, c)] = rng.choice(['stoneBricks1', 'stonebricks2'])

def _stone_towers(overlay, r0, c0, w, rng):
    tower_h = rng.randint(2, 3)
    candidates = [c0, c0 + w - 1]
    if w >= 5:
        candidates.append(c0 + w // 2)
    for tc in rng.sample(candidates, rng.randint(1, min(2, len(candidates)))):
        for th in range(tower_h):
            tr = r0 - tower_h + th
            if tr >= 0:
                overlay[(tr, tc)] = rng.choice(['stoneWindow1', 'stoneWindow2'])

# ── SCATTER ───────────────────────────────────────────────────────────────────
_SCATTER   = ['tire1', 'stump', 'chair1']
_CONTAINER = ['trashcanFull', 'trashcanOpen', 'toiletFull']

def place_scatter(ground_grid, overlay, occupied, rng, bounds):
    """Non-vegetation scatter (props + containers) within crop bounds."""
    min_r, min_c, max_r, max_c = bounds
    grass_set = set(_GRASS)
    for r in range(max(1, min_r), min(CANVAS_H - 1, max_r + 1)):
        for c in range(max(1, min_c), min(CANVAS_W - 1, max_c + 1)):
            if (r, c) in occupied or (r, c) in overlay:
                continue
            if ground_grid.get((r, c)) not in grass_set:
                continue
            roll = rng.random()
            if roll < CONTAINER_CHANCE:
                overlay[(r, c)] = rng.choice(_CONTAINER)
            elif roll < CONTAINER_CHANCE + SCATTER_CHANCE:
                overlay[(r, c)] = rng.choice(_SCATTER)

# ── VEGETATION CLUSTERS ───────────────────────────────────────────────────────
# Single-tile vegetation mixed into clusters
_VEG_SINGLE = ['bush1', 'bush2', 'bushDead1', 'deadplant1']

def place_vegetation_clusters(ground_grid, overlay, occupied, rng, crop_box):
    """Dense vegetation clusters placed in grass within the crop region.

    Multi-tile types (bushTall1, bushTall2, cactus1) stack northward (row - 1).
    occupied already includes a 1-tile south buffer around every building,
    so clusters cannot block doorways.
    """
    if crop_box is None:
        return

    min_r, min_c, max_r, max_c = crop_box
    grass_set = set(_GRASS)
    n_clusters = rng.randint(2, 6)

    for _ in range(n_clusters):
        # Find a valid grass center for this cluster
        for _ in range(30):
            cr = rng.randint(min_r + 1, max(min_r + 1, max_r - 1))
            cc = rng.randint(min_c + 1, max(min_c + 1, max_c - 1))
            if (cr, cc) not in occupied and (cr, cc) not in overlay and \
                    ground_grid.get((cr, cc)) in grass_set:
                break
        else:
            continue

        radius = rng.randint(2, 4)

        for dr in range(-radius, radius + 1):
            for dc in range(-radius, radius + 1):
                r, c = cr + dr, cc + dc
                if not (min_r <= r <= max_r and min_c <= c <= max_c):
                    continue
                if (r, c) in occupied or (r, c) in overlay:
                    continue
                if ground_grid.get((r, c)) not in grass_set:
                    continue

                # Density falls off toward cluster edge
                dist = max(abs(dr), abs(dc))
                prob = 0.75 if dist <= 1 else 0.45 if dist <= 2 else 0.20
                if rng.random() > prob:
                    continue

                _place_veg(r, c, ground_grid, overlay, occupied, grass_set, rng)

def _place_veg(r, c, ground_grid, overlay, occupied, grass_set, rng):
    """Place one vegetation item at (r, c), choosing type by weighted roll."""
    roll = rng.random()

    if roll < 0.40:
        # Single-tile vegetation
        overlay[(r, c)] = rng.choice(_VEG_SINGLE)

    elif roll < 0.60:
        # bushTall1: always exactly 2 tiles (base at r, top at r-1)
        top = (r - 1, c)
        if (top not in occupied and top not in overlay and
                ground_grid.get(top) in grass_set):
            overlay[top] = 'bushTall1Top'
            overlay[(r, c)] = 'bushTall1'
        else:
            overlay[(r, c)] = rng.choice(_VEG_SINGLE)

    elif roll < 0.78:
        # bushTall2: 2–4 tiles, stacks northward
        height = rng.randint(2, 4)
        stack  = [(r - i, c) for i in range(height)]
        if all(cell not in occupied and cell not in overlay and
               ground_grid.get(cell) in grass_set for cell in stack):
            overlay[(r, c)]            = 'bushTall2'
            for i in range(1, height - 1):
                overlay[(r - i, c)]    = 'bushTall2Mid'
            overlay[(r - height + 1, c)] = 'bushTall2Top'
        else:
            overlay[(r, c)] = rng.choice(_VEG_SINGLE)

    else:
        # cactus1: 2–3 tiles; base+mid tiles are both cactus1, top is cactus1top
        height = rng.randint(2, 3)
        stack  = [(r - i, c) for i in range(height)]
        if all(cell not in occupied and cell not in overlay and
               ground_grid.get(cell) in grass_set for cell in stack):
            for i in range(height - 1):
                overlay[(r - i, c)]      = 'cactus1'
            overlay[(r - height + 1, c)] = 'cactus1top'
        else:
            overlay[(r, c)] = rng.choice(_VEG_SINGLE)

# ── RENDER ────────────────────────────────────────────────────────────────────
_MISSING = None

def _get_tile(name, tiles):
    global _MISSING
    img = tiles.get(name)
    if img is None:
        if _MISSING is None:
            _MISSING = Image.new('RGBA', (TILE_PX, TILE_PX), (255, 0, 255, 255))
        return _MISSING
    return img

def render_town(ground_grid, overlay, tiles, crop_box):
    """Render only the region defined by crop_box (min_r, min_c, max_r, max_c)."""
    min_r, min_c, max_r, max_c = crop_box
    rows = max_r - min_r + 1
    cols = max_c - min_c + 1
    out  = Image.new('RGBA', (cols * TILE_PX, rows * TILE_PX), (0, 0, 0, 255))

    for (r, c), name in ground_grid.items():
        if min_r <= r <= max_r and min_c <= c <= max_c:
            img = _get_tile(name, tiles)
            out.paste(img, ((c - min_c) * TILE_PX, (r - min_r) * TILE_PX), img)

    for (r, c), name in sorted(overlay.items()):
        if min_r <= r <= max_r and min_c <= c <= max_c:
            img = _get_tile(name, tiles)
            out.paste(img, ((c - min_c) * TILE_PX, (r - min_r) * TILE_PX), img)

    return out

# ── PUBLIC API ────────────────────────────────────────────────────────────────

@dataclass
class TownData:
    seed:         int
    ground_grid:  dict         # {(r,c): tile_name}
    overlay:      dict         # {(r,c): tile_name}
    crop_box:     tuple        # (r0, c0, r1, c1) — valid tile range after cropping
    palette:      list         # [(r,g,b), ...]
    spawn:        tuple        # (row, col) — party starts here
    entry_tile:   tuple        # (row, col) — exit triggers return to overworld
    healer_spawn: tuple | None = None  # absolute (row, col) of healerHutwallMid tile

    def to_dict(self):
        return {
            'seed': self.seed,
            'ground_grid': {f"{r},{c}": v for (r, c), v in self.ground_grid.items()},
            'overlay':     {f"{r},{c}": v for (r, c), v in self.overlay.items()},
            'crop_box': list(self.crop_box),
            'palette':  [list(p) for p in self.palette],
            'spawn':    list(self.spawn),
            'entry_tile': list(self.entry_tile),
            'healer_spawn': list(self.healer_spawn) if self.healer_spawn else None,
        }


def generate_town_data(seed: int) -> TownData:
    """Generate a town interior deterministically from seed. Cacheable."""
    global CANVAS_H, CANVAS_W

    rng     = random.Random(seed)
    palette = pick_palette(rng)

    n_buildings = rng.randint(MIN_BUILDINGS, MAX_BUILDINGS)
    total       = n_buildings + 1

    per_w  = rng.randint(2, 4)
    per_h  = rng.randint(1, 3)
    CANVAS_W = max(16, 12 + total * per_w)
    CANVAS_H = max(14, 10 + total * per_h)

    ground   = gen_ground(rng)
    overlay  = {}
    occupied = set()
    buildings   = []
    cluster_box = place_healer_hut(overlay, occupied, buildings, rng)
    healer_spawn = next(
        ((r, c) for (r, c), tile in overlay.items() if tile == 'healerHutwallMid'),
        None
    )

    for _ in range(n_buildings):
        kind = rng.choice(['houseA', 'houseB', 'stone', 'houseA', 'houseB'])
        if kind == 'houseA':
            new_box = place_house(overlay, occupied, buildings, rng, cluster_box, variant='A')
        elif kind == 'houseB':
            new_box = place_house(overlay, occupied, buildings, rng, cluster_box, variant='B')
        else:
            new_box = place_stone_building(overlay, occupied, buildings, rng, cluster_box)
        if new_box is not cluster_box:
            cluster_box = new_box

    GROUND_MARGIN = 5
    if overlay:
        ov_rs = [r for r, c in overlay]
        ov_cs = [c for r, c in overlay]
        crop_box = (
            max(0,          min(ov_rs) - GROUND_MARGIN),
            max(0,          min(ov_cs) - GROUND_MARGIN),
            min(CANVAS_H-1, max(ov_rs) + GROUND_MARGIN),
            min(CANVAS_W-1, max(ov_cs) + GROUND_MARGIN),
        )
    else:
        crop_box = (0, 0, CANVAS_H - 1, CANVAS_W - 1)

    place_cobble_paths(ground, overlay, buildings, rng, crop_box)
    place_courtyards(ground, overlay, occupied, rng, cluster_box)
    place_scatter(ground, overlay, occupied, rng, crop_box)
    place_vegetation_clusters(ground, overlay, occupied, rng, crop_box)

    # Entry tile: bottom center of crop_box. Spawn: one row north.
    r0, c0, r1, c1 = crop_box
    entry_col  = (c0 + c1) // 2
    entry_tile = (r1, entry_col)
    spawn      = (r1 - 1, entry_col)

    return TownData(
        seed=seed,
        ground_grid=ground,
        overlay=overlay,
        crop_box=crop_box,
        palette=palette,
        spawn=spawn,
        entry_tile=entry_tile,
        healer_spawn=healer_spawn,
    )


# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    global CANVAS_H, CANVAS_W

    seed = int.from_bytes(os.urandom(8), 'big') & 0x7FFFFFFFFFFFFFFF
    print(f"Town seed: {seed}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith('town_'):
            os.unlink(os.path.join(OUTPUT_DIR, f))

    rng     = random.Random(seed)
    palette = pick_palette(rng)

    pal_hex = '  '.join(f'#{r:02x}{g:02x}{b:02x}' for r, g, b in palette)
    print(f"Palette: {pal_hex}\n")

    # Decide building count first so canvas can be sized to match.
    n_buildings = rng.randint(MIN_BUILDINGS, MAX_BUILDINGS)
    total       = n_buildings + 1   # +1 for healer hut

    # Canvas grows with building count; different per-building increments
    # for w and h keep outputs rectangular.  Kept tight — buildings cluster.
    per_w  = rng.randint(2, 4)
    per_h  = rng.randint(1, 3)
    CANVAS_W = max(16, 12 + total * per_w)
    CANVAS_H = max(14, 10 + total * per_h)

    print(f"Canvas: {CANVAS_W}×{CANVAS_H} tiles  ({CANVAS_W*TILE_PX}×{CANVAS_H*TILE_PX} px)")
    print(f"Buildings: {total} (healer hut + {n_buildings})\n")

    raw_tiles = load_raw_tiles(TILESET_PATH, TILERULES_PATH)
    tiles     = remap_tileset(raw_tiles, palette)

    ground   = gen_ground(rng)
    overlay  = {}
    occupied = set()

    buildings   = []
    cluster_box = place_healer_hut(overlay, occupied, buildings, rng)
    print(f"Healer hut: {'placed' if cluster_box else 'FAILED'}")

    placed = 0
    for _ in range(n_buildings):
        kind = rng.choice(['houseA', 'houseB', 'stone', 'houseA', 'houseB'])
        if kind == 'houseA':
            new_box = place_house(overlay, occupied, buildings, rng, cluster_box, variant='A')
        elif kind == 'houseB':
            new_box = place_house(overlay, occupied, buildings, rng, cluster_box, variant='B')
        else:
            new_box = place_stone_building(overlay, occupied, buildings, rng, cluster_box)
        if new_box is not cluster_box:
            cluster_box = new_box
            placed += 1

    print(f"Extra buildings placed: {placed}/{n_buildings}")

    # Compute crop box from buildings BEFORE cobble paths and scatter.
    GROUND_MARGIN = 5
    if overlay:
        ov_rs = [r for r, c in overlay]
        ov_cs = [c for r, c in overlay]
        crop_box = (
            max(0,          min(ov_rs) - GROUND_MARGIN),
            max(0,          min(ov_cs) - GROUND_MARGIN),
            min(CANVAS_H-1, max(ov_rs) + GROUND_MARGIN),
            min(CANVAS_W-1, max(ov_cs) + GROUND_MARGIN),
        )
    else:
        crop_box = (0, 0, CANVAS_H - 1, CANVAS_W - 1)

    place_cobble_paths(ground, overlay, buildings, rng, crop_box)
    place_courtyards(ground, overlay, occupied, rng, cluster_box)

    place_scatter(ground, overlay, occupied, rng, crop_box)
    place_vegetation_clusters(ground, overlay, occupied, rng, crop_box)

    img   = render_town(ground, overlay, tiles, crop_box)
    fname = f"town_{seed}.png"
    path  = os.path.join(OUTPUT_DIR, fname)
    img.save(path)
    print(f"\n-> {path}  ({img.width}×{img.height} px)")


if __name__ == '__main__':
    main()
