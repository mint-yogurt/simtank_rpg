"""Procedural cave/dungeon interior generator — test harness for simtank_rpg.

Pipeline per run:
  1. place_rooms      — random rectangles, overlap-checked with wall footprint
  2. build_floor      — fill floor/style grids from rooms (row-major)
  3. lay_hallways     — MST L/Z paths; cells already in floor keep their style
  4. derive_walls     — post-placement, sorted for determinism; junctions auto-open
  5. place_entry      — enter tile on northernmost north wall
  6. place_waterfalls — waterfall frames on cave north walls, terminate as puddle
  7. place_water      — 2×2 or 3×3+ pools in cave rooms, size scales with room
  8. place_features   — scatter + puddles + containers
  9. render_cave      — crop to content + padding, save PNG
"""

import os
import random
from dataclasses import dataclass

from PIL import Image

# ── CONFIG ────────────────────────────────────────────────────────────────────
TILE_PX        = 16

MIN_ROOMS      = 2
MAX_ROOMS      = 8       # tunable knob
ROOM_W_MIN     = 3
ROOM_W_MAX     = 16
ROOM_H_MIN     = 3
ROOM_H_MAX     = 12
CANVAS_ORIGIN  = 15      # rooms placed inside [ORIGIN, ORIGIN+SPREAD)
CANVAS_SPREAD  = 60
CANVAS_PAD     = 2       # void border around content bounding box

WATER_CHANCE      = 0.70  # cave room gets a water pool
POOL_W_MAX        = 8     # max pool width  (enforces 2×2 or 3×3+ rule)
POOL_H_MAX        = 6     # max pool height
WATERFALL_CHANCE  = 0.50  # cave room gets a waterfall on its north wall
PUDDLE_CHANCE     = 0.04  # per cave-floor cell: animated puddle
SCATTER_CHANCE    = 0.04  # per cave-floor cell: skull or sock
CONTAINER_CHANCE  = 0.04  # per dungeon-floor cell: chest or trashcan (max 2/room)

_HERE          = os.path.dirname(os.path.abspath(__file__))
TILESET_PATH   = os.path.join(_HERE, "..", "web", "static", "tiles", "tiles_cave1.png")
TILERULES_PATH = os.path.join(_HERE, "..", "web", "static", "tiles", "tiles_cave_rules.txt")
OUTPUT_DIR     = os.path.join(_HERE, "out")

# ── PALETTE ───────────────────────────────────────────────────────────────────
SRC_GREY = (0x78, 0x78, 0x78)
SRC_TEAL = (0x00, 0x40, 0x58)
SRC_GOLD = (0xAC, 0x7C, 0x00)

# Rows 0-3, cols 0-12 only (cols 13-15 excluded per spec)
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
    """3 non-orthogonally-adjacent NES palette cells → (grey, teal, gold) as RGB tuples."""
    eligible = [(r, c) for r in range(4) for c in range(13)]
    picked = []
    while len(picked) < 3:
        cell = rng.choice(eligible)
        if all(abs(cell[0]-p[0]) + abs(cell[1]-p[1]) > 1 for p in picked):
            picked.append(cell)
    return tuple(_hex_rgb(NES_PALETTE[r][c]) for r, c in picked)

def remap_tileset(raw_tiles, grey, teal, gold):
    """Swap placeholder colors → this run's rolled palette. Black and white are preserved."""
    remap = {SRC_GREY: grey, SRC_TEAL: teal, SRC_GOLD: gold}
    result = {}
    for name, img in raw_tiles.items():
        data = list(img.getdata())
        data = [(remap.get((r, g, b), (r, g, b)) + (a,)) for r, g, b, a in data]
        out = img.copy()
        out.putdata(data)
        result[name] = out
    return result

# ── TILE TABLES ───────────────────────────────────────────────────────────────
# WALL_DEFS[style][dir] = (wall_tiles, topper_tiles_or_None)
# topper is placed one additional step in the same direction as the wall.
WALL_DEFS = {
    'cave': {
        'N': (['cavewall1',   'cavewall2'],    ['cavewalltop1', 'cavewalltop2']),
        'S': (['cavewallS'],                    None),
        'W': (None,                             None),  # cave: void on sides
        'E': (None,                             None),
    },
    'brickwall': {
        'N': (['brickwallN1', 'brickwallN2'],  None),   # 1-tile-tall, no topper
        'S': (['cavewallS'],                    None),
        'W': (['brickwallW1', 'brickwallW2'],  None),
        'E': (['brickwallE1', 'brickwallE2'],  None),
    },
    'dungwall': {
        'N': (['dungwallN1',  'dungwallN2'],   ['dungwalltopN1', 'dungwalltopN2']),
        'S': (['cavewallS'],                    None),
        'W': (['brickwallW1', 'brickwallW2'],  None),   # shared with brickwall
        'E': (['brickwallE1', 'brickwallE2'],  None),
    },
}

_SWE = [('S', 1, 0), ('W', 0, -1), ('E', 0, 1)]

FLOOR_TILES = {
    'cobble':    ['cobble1', 'cobble2'],
    'dungeon':   ['dungfloor1', 'dungfloor2', 'dungfloor3',
                  'minidungfloor1', 'minidungfloor2', 'minidungfloor3'],
}

HALLWAY_FLOOR = {          # wall_style → floor_style for new hallway cells
    'cave':      'cobble',
    'brickwall': 'dungeon',
    'dungwall':  'dungeon',
}

SCATTER_TILES  = ['skull1', 'sock']
CONTAINER_FULL = ['trashcanFull', 'chestFull']

# ── DATA ──────────────────────────────────────────────────────────────────────
@dataclass
class Room:
    r0: int
    c0: int
    h: int
    w: int
    kind:        str   # 'cave' | 'dungeon'
    floor_style: str   # 'cobble' | 'dungfloor' | 'minidungfloor'
    wall_style:  str   # 'cave' | 'brickwall' | 'dungwall'

# ── TILESET ───────────────────────────────────────────────────────────────────
def _parse_tilerules(path):
    tiles = {}
    with open(path) as f:
        for line in f:
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
            tiles[name] = (col, row)
    return tiles

def load_raw_tiles(tileset_path, tilerules_path):
    sheet  = Image.open(tileset_path).convert("RGBA")
    coords = _parse_tilerules(tilerules_path)
    tiles  = {}
    for name, (col, row) in coords.items():
        x, y = col * TILE_PX, row * TILE_PX
        tiles[name] = sheet.crop((x, y, x + TILE_PX, y + TILE_PX))
    return tiles

# ── ROOM PLACEMENT ────────────────────────────────────────────────────────────
def _room_footprint(r0, c0, h, w):
    """All cells a room + its maximum wall footprint can occupy.

    Uses worst-case margins: 2 rows above (wall + topper), 1 below, 1 each side.
    """
    cells = set()
    for dr in range(h):
        for dc in range(w):
            r, c = r0 + dr, c0 + dc
            cells.add((r,     c))
            cells.add((r - 1, c))
            cells.add((r - 2, c))
            cells.add((r + 1, c))
            cells.add((r,     c - 1))
            cells.add((r,     c + 1))
    return cells

def place_rooms(rng, n_rooms):
    rooms    = []
    occupied = set()
    for _ in range(n_rooms):
        for _ in range(60):
            w  = max(rng.randint(ROOM_W_MIN, ROOM_W_MAX), rng.randint(ROOM_W_MIN, ROOM_W_MAX))
            h  = max(rng.randint(ROOM_H_MIN, ROOM_H_MAX), rng.randint(ROOM_H_MIN, ROOM_H_MAX))
            r0 = rng.randint(CANVAS_ORIGIN, CANVAS_ORIGIN + CANVAS_SPREAD - h)
            c0 = rng.randint(CANVAS_ORIGIN, CANVAS_ORIGIN + CANVAS_SPREAD - w)
            fp = _room_footprint(r0, c0, h, w)
            if fp & occupied:
                continue
            kind = rng.choice(['cave', 'dungeon'])
            if kind == 'cave':
                wall_style  = 'cave'
                floor_style = 'cobble'
            else:
                wall_style  = rng.choice(['brickwall', 'dungwall'])
                floor_style = 'dungeon'
            rooms.append(Room(r0, c0, h, w, kind, floor_style, wall_style))
            occupied |= fp
            break
    return rooms

# ── FLOOR BUILD ───────────────────────────────────────────────────────────────
def build_floor(rooms, rng):
    """Row-major fill of floor and style grids from placed rooms."""
    floor_grid = {}
    style_grid = {}
    for room in rooms:
        ft = FLOOR_TILES[room.floor_style]
        for dr in range(room.h):
            for dc in range(room.w):
                r, c = room.r0 + dr, room.c0 + dc
                floor_grid[(r, c)] = rng.choice(ft)
                style_grid[(r, c)] = room.wall_style
    return floor_grid, style_grid

# ── MST ───────────────────────────────────────────────────────────────────────
def mst_edges(rooms):
    """Prim's MST on room centers (Manhattan distance). Deterministic tie-break."""
    n = len(rooms)
    if n <= 1:
        return []

    def center(room):
        return (room.r0 + room.h // 2, room.c0 + room.w // 2)

    def dist(i, j):
        ri, ci = center(rooms[i])
        rj, cj = center(rooms[j])
        return abs(ri - rj) + abs(ci - cj)

    in_tree = [0]
    not_in  = list(range(1, n))
    edges   = []
    while not_in:
        best = None
        for i in in_tree:
            for j in not_in:
                d = dist(i, j)
                if best is None or d < best[0] or (d == best[0] and (i, j) < best[1:]):
                    best = (d, i, j)
        _, i, j = best
        edges.append((i, j))
        in_tree.append(j)
        not_in.remove(j)
    return edges

# ── HALLWAYS ──────────────────────────────────────────────────────────────────
def _hallway_cells(rooms, i, j, rng):
    """L-shaped or Z-shaped path between centers of rooms i and j."""
    rA = rooms[i].r0 + rooms[i].h // 2
    cA = rooms[i].c0 + rooms[i].w // 2
    rB = rooms[j].r0 + rooms[j].h // 2
    cB = rooms[j].c0 + rooms[j].w // 2

    cells = set()
    shape = rng.randint(0, 2)   # 0=horiz-first  1=vert-first  2=zigzag

    if shape == 0:
        for c in range(min(cA, cB), max(cA, cB) + 1): cells.add((rA, c))
        for r in range(min(rA, rB), max(rA, rB) + 1): cells.add((r,  cB))
    elif shape == 1:
        for r in range(min(rA, rB), max(rA, rB) + 1): cells.add((r,  cA))
        for c in range(min(cA, cB), max(cA, cB) + 1): cells.add((rB, c))
    else:
        lo_c, hi_c = min(cA, cB), max(cA, cB)
        mid_c = rng.randint(lo_c, hi_c) if lo_c < hi_c else lo_c
        for c in range(min(cA, mid_c), max(cA, mid_c) + 1): cells.add((rA, c))
        for r in range(min(rA, rB),    max(rA, rB)    + 1): cells.add((r,  mid_c))
        for c in range(min(mid_c, cB), max(mid_c, cB) + 1): cells.add((rB, c))

    return sorted(cells)

def lay_hallways(rooms, edges, floor_grid, style_grid, rng):
    """Add hallway floor cells for each MST edge. Returns the set of new hallway cells."""
    hallway_cells = set()
    for i, j in edges:
        h_style = rng.choice(['cave', 'brickwall', 'dungwall'])
        ft      = FLOOR_TILES[HALLWAY_FLOOR[h_style]]
        for r, c in _hallway_cells(rooms, i, j, rng):
            if (r, c) not in floor_grid:
                floor_grid[(r, c)] = rng.choice(ft)
                style_grid[(r, c)] = h_style
                hallway_cells.add((r, c))
    return hallway_cells

# ── WALL DERIVATION ───────────────────────────────────────────────────────────
def derive_walls(floor_grid, style_grid, hallway_cells, rng):
    """Two-pass wall derivation. North walls win over S/W/E at any contested cell.

    Pass 1 claims all N wall + topper positions first (row-major, deterministic).
    Pass 2 fills S/W/E, skipping cells already claimed — so a hallway's side wall
    never overwrites a room's north wall, and a room's side wall never blocks a
    hallway's north wall from reaching its last cell.

    Hallway cells never receive south walls regardless of style.
    """
    wall_grid = {}

    # Pass 1 — north walls + toppers
    for r, c in sorted(floor_grid):
        wall_tiles, top_tiles = WALL_DEFS[style_grid[(r, c)]]['N']
        if not wall_tiles:
            continue
        wr, wc = r - 1, c
        if (wr, wc) in floor_grid or (wr, wc) in wall_grid:
            continue
        wall_grid[(wr, wc)] = rng.choice(wall_tiles)
        if top_tiles:
            tr, tc = wr - 1, wc
            if (tr, tc) not in floor_grid and (tr, tc) not in wall_grid:
                wall_grid[(tr, tc)] = rng.choice(top_tiles)

    # Pass 2 — south, west, east (yields to pass 1; hallways never get south walls)
    for r, c in sorted(floor_grid):
        wd = WALL_DEFS[style_grid[(r, c)]]
        for direction, dr, dc in _SWE:
            if direction == 'S' and (r, c) in hallway_cells:
                continue
            wall_tiles, _ = wd[direction]
            if not wall_tiles:
                continue
            wr, wc = r + dr, c + dc
            if (wr, wc) in floor_grid or (wr, wc) in wall_grid:
                continue
            wall_grid[(wr, wc)] = rng.choice(wall_tiles)

    return wall_grid

# ── ENTRY PLACEMENT ───────────────────────────────────────────────────────────
def place_entry(floor_grid, wall_grid, rng):
    """Replace north-wall cell of the northernmost floor row with the enter tile."""
    if not floor_grid:
        return None
    min_r    = min(r for r, c in floor_grid)
    top_cols = sorted(c for r, c in floor_grid if r == min_r)
    c        = rng.choice(top_cols)
    wall_grid[(min_r - 1, c)] = 'enter'
    return (min_r, c)   # spawn point (1 tile south of enter)

# ── WATER POOLS & WATERFALLS ──────────────────────────────────────────────────
_COBBLE       = set(FLOOR_TILES['cobble'])
_CAVEWALL     = {'cavewall1', 'cavewall2'}
_CAVEWALLTOP  = {'cavewalltop1', 'cavewalltop2'}

def _water_tile_at(dr, dc, ph, pw):
    """Return the correct water tile for position (dr, dc) in a ph×pw pool.

    Valid sizes: 2×2 (corners only, all walkable) or 3×3+ (edges+mid, mostly impassable).
    """
    top, bot   = dr == 0,      dr == ph - 1
    left, right = dc == 0,     dc == pw - 1
    if ph == 2 and pw == 2:
        if top  and left:  return 'waterNW'
        if top  and right: return 'waterNE'
        if bot  and left:  return 'waterSW'
        return 'waterSE'
    if top  and left:  return 'waterNW'
    if top  and right: return 'waterNE'
    if bot  and left:  return 'waterSW'
    if bot  and right: return 'waterSE'
    if top:            return 'waterN'
    if bot:            return 'waterS'
    if left:           return 'waterW'
    if right:          return 'waterE'
    return 'waterMid'

def place_waterfalls(rooms, floor_grid, wall_grid, rng):
    """Place a waterfall on the north wall of eligible cave rooms.

    Replaces cavewalltop + cavewall pair with alternating waterfall frames and
    sets the floor cell directly below to a puddle tile.
    Enter tile column is automatically excluded (its wall tile is 'enter', not cavewall).
    """
    for room in rooms:
        if room.kind != 'cave':
            continue
        if rng.random() > WATERFALL_CHANCE:
            continue
        top_r = room.r0
        valid = [
            room.c0 + dc
            for dc in range(room.w)
            if (wall_grid.get((top_r - 1, room.c0 + dc)) in _CAVEWALL
                and wall_grid.get((top_r - 2, room.c0 + dc)) in _CAVEWALLTOP
                and floor_grid.get((top_r, room.c0 + dc)) in _COBBLE)
        ]
        if not valid:
            continue
        c = rng.choice(valid)
        wall_grid[(top_r - 2, c)] = 'waterfall1'
        wall_grid[(top_r - 1, c)] = 'waterfall2'
        floor_grid[(top_r,     c)] = rng.choice(['puddle1', 'puddle2'])

def place_water(rooms, floor_grid, rng):
    """Place water pools in cave rooms. 2×2 (corners only) or 3×3+ (edge+mid tiles).

    Pool size is biased toward larger in bigger rooms.
    """
    for room in rooms:
        if room.kind != 'cave':
            continue
        if rng.random() > WATER_CHANCE:
            continue

        max_pw = min(room.w - 2, POOL_W_MAX)
        max_ph = min(room.h - 2, POOL_H_MAX)
        if max_pw < 2 or max_ph < 2:
            continue

        # Bias toward larger pool sizes
        pw = max(rng.randint(2, max_pw), rng.randint(2, max_pw)) if max_pw > 2 else 2
        ph = max(rng.randint(2, max_ph), rng.randint(2, max_ph)) if max_ph > 2 else 2

        # Enforce: either 2×2 OR both dims ≥ 3
        if pw > 2 or ph > 2:
            pw, ph = max(pw, 3), max(ph, 3)
        if pw > max_pw or ph > max_ph:
            pw, ph = 2, 2   # fall back to 2×2 if enforced size doesn't fit

        if room.w < pw or room.h < ph:
            continue

        for _ in range(15):
            dr = rng.randint(0, room.h - ph)
            dc = rng.randint(0, room.w - pw)
            r0, c0 = room.r0 + dr, room.c0 + dc
            cells = [(r0 + i, c0 + j) for i in range(ph) for j in range(pw)]
            if all(floor_grid.get(cell) in _COBBLE for cell in cells):
                for i in range(ph):
                    for j in range(pw):
                        floor_grid[(r0 + i, c0 + j)] = _water_tile_at(i, j, ph, pw)
                break

# ── FEATURE SCATTER ───────────────────────────────────────────────────────────
def place_features(rooms, floor_grid, rng):
    """Scatter (skulls/socks) in cave rooms; containers in dungeon rooms."""
    for room in rooms:
        floor_cells = sorted(
            (room.r0 + dr, room.c0 + dc)
            for dr in range(room.h)
            for dc in range(room.w)
            if (room.r0 + dr, room.c0 + dc) in floor_grid
        )
        if room.kind == 'cave':
            for r, c in floor_cells:
                if floor_grid[(r, c)] not in _COBBLE:
                    continue
                roll = rng.random()
                if roll < SCATTER_CHANCE:
                    floor_grid[(r, c)] = rng.choice(SCATTER_TILES)
                elif roll < SCATTER_CHANCE + PUDDLE_CHANCE:
                    floor_grid[(r, c)] = rng.choice(['puddle1', 'puddle2'])
        elif room.kind == 'dungeon':
            placed = 0
            for r, c in floor_cells:
                if placed >= 2:
                    break
                if rng.random() < CONTAINER_CHANCE:
                    floor_grid[(r, c)] = rng.choice(CONTAINER_FULL)
                    placed += 1

# ── RENDER ────────────────────────────────────────────────────────────────────
_MISSING = None

def _get_tile(name, tiles):
    global _MISSING
    img = tiles.get(name)
    if img is None:
        if _MISSING is None:
            _MISSING = Image.new("RGBA", (TILE_PX, TILE_PX), (255, 0, 255, 255))
        img = _MISSING
    return img

def render_cave(floor_grid, wall_grid, tiles):
    all_cells = set(floor_grid) | set(wall_grid)
    if not all_cells:
        return Image.new("RGBA", (TILE_PX, TILE_PX), (0, 0, 0, 255))

    min_r = min(r for r, c in all_cells) - CANVAS_PAD
    max_r = max(r for r, c in all_cells) + CANVAS_PAD
    min_c = min(c for r, c in all_cells) - CANVAS_PAD
    max_c = max(c for r, c in all_cells) + CANVAS_PAD

    rows = max_r - min_r + 1
    cols = max_c - min_c + 1
    out  = Image.new("RGBA", (cols * TILE_PX, rows * TILE_PX), (0, 0, 0, 255))

    for (r, c), name in sorted(floor_grid.items()):
        img = _get_tile(name, tiles)
        out.paste(img, ((c - min_c) * TILE_PX, (r - min_r) * TILE_PX), img)

    for (r, c), name in sorted(wall_grid.items()):
        img = _get_tile(name, tiles)
        out.paste(img, ((c - min_c) * TILE_PX, (r - min_r) * TILE_PX), img)

    return out

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    seed = int.from_bytes(os.urandom(8), 'big') & 0x7FFFFFFFFFFFFFFF
    rng  = random.Random(seed)
    print(f"Cave seed: {seed}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    for f in os.listdir(OUTPUT_DIR):
        if f.startswith('cave_'):
            os.unlink(os.path.join(OUTPUT_DIR, f))

    raw_tiles        = load_raw_tiles(TILESET_PATH, TILERULES_PATH)
    grey, teal, gold = pick_palette(rng)
    tiles            = remap_tileset(raw_tiles, grey, teal, gold)
    pal_hex = (f"grey=#{grey[0]:02x}{grey[1]:02x}{grey[2]:02x} "
               f"teal=#{teal[0]:02x}{teal[1]:02x}{teal[2]:02x} "
               f"gold=#{gold[0]:02x}{gold[1]:02x}{gold[2]:02x}")
    print(f"Palette: {pal_hex}\n")

    n_rooms = rng.randint(MIN_ROOMS, MAX_ROOMS)
    rooms   = place_rooms(rng, n_rooms)
    print(f"Requested {n_rooms} rooms, placed {len(rooms)}:")
    for i, room in enumerate(rooms):
        print(f"  {i}: {room.kind}/{room.floor_style}/{room.wall_style}"
              f"  pos=({room.r0},{room.c0})  size={room.w}w×{room.h}h")

    floor_grid, style_grid = build_floor(rooms, rng)

    edges         = mst_edges(rooms)
    hallway_cells = lay_hallways(rooms, edges, floor_grid, style_grid, rng)

    wall_grid = derive_walls(floor_grid, style_grid, hallway_cells, rng)
    spawn     = place_entry(floor_grid, wall_grid, rng)

    place_waterfalls(rooms, floor_grid, wall_grid, rng)
    place_water(rooms, floor_grid, rng)
    place_features(rooms, floor_grid, rng)

    img   = render_cave(floor_grid, wall_grid, tiles)
    fname = f"cave_{seed}.png"
    img.save(os.path.join(OUTPUT_DIR, fname))

    print(f"\nFloor cells: {len(floor_grid)}  Wall cells: {len(wall_grid)}")
    print(f"Spawn point: {spawn}")
    print(f"-> {os.path.join(OUTPUT_DIR, fname)}")

if __name__ == "__main__":
    main()
