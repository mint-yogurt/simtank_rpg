"""Procedural overworld screen generator — test harness for simtank_rpg.

Each run uses a new random world seed (printed + embedded in output filenames).
Generation order per screen: base fill → blobs → features → paths → scatter.
"""

import hashlib
import heapq
import os
import random
import struct
from dataclasses import dataclass, field

from PIL import Image

# ── CONFIG ────────────────────────────────────────────────────────────────────
TILE_PX = 16

SCREEN_CONFIGS = {
    "16x14": {"rows": 14, "cols": 16},   # 16 wide × 14 tall
    "16x10": {"rows": 10, "cols": 16},   # 16 wide × 10 tall
}
ACTIVE_CONFIG = "16x14"

FEATURE_CHANCE_1    = 0.15
FEATURE_CHANCE_2    = 0.08
TOWN_CHANCE         = 0.22
TREE_DENSITY        = 0.32
MOUNTAIN1_DENSITY   = 0.06
POND_DENSITY        = 0.04
MROW_LEN_MIN        = 3
MROW_LEN_MAX        = 8
MROWS_MAX           = 3
LAKE_W_MIN          = 3
LAKE_W_MAX          = 6
LAKE_H_MIN          = 3
LAKE_H_MAX          = 4
LAKES_MAX           = 2
FOREST_W_MIN        = 3
FOREST_W_MAX        = 7
FOREST_H_MIN        = 3
FOREST_H_MAX        = 5
FORESTS_MAX         = 2
MNT_W_MIN           = 3
MNT_W_MAX           = 9
MNT_BLOBS_MAX       = 2
MNT_CAVE_CHANCE     = 0.40
PATH_WALK_BIAS      = 0.85   # fraction of steps taken toward target
DIRT_W_MIN          = 3
DIRT_W_MAX          = 7
DIRT_H_MIN          = 3
DIRT_H_MAX          = 5
DIRTS_MAX           = 2
DIRT_CHANCE         = 0.0

BASE_TILES   = ["grass1", "grass2", "grass3"]
FEATURE_POOL = ["tower1", "tower2", "castle1", "skullhouse1", "cave1", "building1"]
TREE_TILES   = ["tree1", "treedead1", "tree2"]

# Metadata tags consumed by the world layer (dungeon connection, town connection, etc.)
FEATURE_TYPES = {
    "tower1":     "dungeon",
    "tower2":     "dungeon",
    "castle1":    "dungeon",
    "skullhouse1":"dungeon",
    "cave1":      "dungeon",
    "mnt_cave":   "dungeon",
    "mowdenpass": "dungeon",
    "town1":      "town",
}

_HERE          = os.path.dirname(os.path.abspath(__file__))
TILESET_PATH   = os.path.join(_HERE, "..", "assets", "tiles", "overworld_1.png")
TILERULES_PATH = os.path.join(_HERE, "..", "assets", "tiles", "overworld_1_tilerules.txt")
OUTPUT_DIR     = os.path.join(_HERE, "out")

# ── PALETTE ───────────────────────────────────────────────────────────────────
SRC_GREEN = (0x69, 0xBD, 0x2F)
SRC_BLUE  = (0x5E, 0xCD, 0xE4)
SRC_BROWN = (0x52, 0x4B, 0x23)

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
    """3 non-orthogonally-adjacent NES palette cells → (green, blue, brown) as RGB tuples."""
    eligible = [(r, c) for r in range(4) for c in range(13)]
    picked = []
    while len(picked) < 3:
        cell = rng.choice(eligible)
        if all(abs(cell[0]-p[0]) + abs(cell[1]-p[1]) > 1 for p in picked):
            picked.append(cell)
    return tuple(_hex_rgb(NES_PALETTE[r][c]) for r, c in picked)

# ── TILESET ───────────────────────────────────────────────────────────────────
def _parse_tilerules(path):
    """Returns {tile_name: (col, row)} from tilerules file."""
    tiles = {}
    with open(path) as f:
        for line in f:
            line = line.strip()
            if not line or '=' not in line:
                continue
            eq = line.index('=')
            coord_part = line[:eq].strip()
            rest = line[eq+1:]
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
    """Returns {tile_name: PIL RGBA Image (16×16)}."""
    sheet = Image.open(tileset_path).convert("RGBA")
    coords = _parse_tilerules(tilerules_path)
    tiles = {}
    for name, (col, row) in coords.items():
        x, y = col * TILE_PX, row * TILE_PX
        tiles[name] = sheet.crop((x, y, x + TILE_PX, y + TILE_PX))
    return tiles

def remap_tileset(raw_tiles, green, blue, brown):
    """Swap placeholder colors → this screen's rolled palette."""
    remap = {SRC_GREEN: green, SRC_BLUE: blue, SRC_BROWN: brown}
    result = {}
    for name, img in raw_tiles.items():
        data = list(img.getdata())
        data = [(remap.get((r,g,b),(r,g,b)) + (a,)) for r,g,b,a in data]
        out = img.copy()
        out.putdata(data)
        result[name] = out
    return result

# ── SCREEN SEED ───────────────────────────────────────────────────────────────
def derive_screen_seed(world_seed, sx, sy):
    """Stable, salted hash of (world_seed, sx, sy). Handles negative coords."""
    packed = struct.pack(">Qqq", world_seed, int(sx), int(sy))
    return int.from_bytes(hashlib.sha256(packed).digest()[:8], 'big')

# ── GRID HELPERS ──────────────────────────────────────────────────────────────
BLOB_TILES = frozenset([
    "mowden_W", "mowden_mid", "mowden_E",
    "lake_NW",    "lake_N",    "lake_NE",
    "lake_W",     "lake_mid",  "lake_E",
    "lake_SW",    "lake_S",    "lake_SE",
    "lake_invCorner_N+E", "lake_invCorner_W+N",
    "lake_invCorner_S+E", "lake_invCorner_W+S",
    "forest_NW",  "forest_N",  "forest_NE",
    "forest_W",   "forest_mid","forest_E",
    "forest_SW",  "forest_S",  "forest_SE",
    "mnt_NW",     "mnt_N",     "mnt_NE",
    "mnt_W",      "mnt_mid",   "mnt_E",
    "mnt_SW",     "mnt_S",     "mnt_SE",
    "mnt_cave",
])

def is_base(tile):
    return tile in BASE_TILES

# ── STEP 1: BASE FILL ─────────────────────────────────────────────────────────
def step_base_fill(grid, rng):
    rows, cols = len(grid), len(grid[0])
    for r in range(rows):
        for c in range(cols):
            grid[r][c] = rng.choice(BASE_TILES)

# ── STEP 2: BLOB PLACEMENT ────────────────────────────────────────────────────
_LAKE_TILE = [
    ["lake_NW", "lake_N",   "lake_NE"],
    ["lake_W",  "lake_mid", "lake_E"],
    ["lake_SW", "lake_S",   "lake_SE"],
]

_FOREST_TILE = [
    ["forest_NW", "forest_N",   "forest_NE"],
    ["forest_W",  "forest_mid", "forest_E"],
    ["forest_SW", "forest_S",   "forest_SE"],
]

_MNT_TILE = [
    ["mnt_NW", "mnt_N",   "mnt_NE"],
    ["mnt_W",  "mnt_mid", "mnt_E"],
    ["mnt_SW", "mnt_S",   "mnt_SE"],
]

_DIRT_TILE = [
    ["dirt_NW", "dirt_N",   "dirt_NE"],
    ["dirt_W",  "dirt_mid", "dirt_E"],
    ["dirt_SW", "dirt_S",   "dirt_SE"],
]

def _lake_tile_at(lake_set, r, c):
    """Resolve a lake cell's tile from its neighbors — handles any rectilinear shape."""
    def L(nr, nc): return (nr, nc) in lake_set
    N = L(r-1,c); S = L(r+1,c); E = L(r,c+1); W = L(r,c-1)
    if N and S and E and W:
        if not L(r-1,c+1): return "lake_invCorner_N+E"
        if not L(r-1,c-1): return "lake_invCorner_W+N"
        if not L(r+1,c+1): return "lake_invCorner_S+E"
        if not L(r+1,c-1): return "lake_invCorner_W+S"
        return "lake_mid"
    if not N and     S and     E and     W: return "lake_N"
    if     N and not S and     E and     W: return "lake_S"
    if     N and     S and not E and     W: return "lake_E"
    if     N and     S and     E and not W: return "lake_W"
    if not N and not W: return "lake_NW"
    if not N and not E: return "lake_NE"
    if not S and not W: return "lake_SW"
    if not S and not E: return "lake_SE"
    return "lake_mid"

def _gen_lake_shape(rng, rows, cols):
    """Return (rel_cells, bounding_w, bounding_h) for a lake with 0-3 corner cuts.

    Budget rule: cut_w[NW]+cut_w[NE] <= base_w-3  (and same for SW+SE),
                 cut_h[NW]+cut_h[SW] <= base_h-3  (and same for NE+SE).
    This guarantees every section of the shape is >=3 wide and >=3 tall.
    """
    n_cuts = rng.choices([0, 1, 2, 3], weights=[2, 4, 3, 1])[0]

    # Base dimensions scale with complexity
    if n_cuts == 0:
        w = rng.randint(LAKE_W_MIN, min(LAKE_W_MAX, cols))
        h = rng.randint(LAKE_H_MIN, min(LAKE_H_MAX, rows))
    elif n_cuts == 1:
        w = rng.randint(6, min(9, cols))
        h = rng.randint(6, min(8, rows))
    else:
        w = rng.randint(9, min(12, cols))
        h = rng.randint(9, min(11, rows))

    # Downgrade if screen is too small for the desired complexity
    if w < 9 and n_cuts >= 2: n_cuts = 1
    if h < 9 and n_cuts >= 2: n_cuts = 1
    if w < 6 and n_cuts >= 1: n_cuts = 0
    if h < 6 and n_cuts >= 1: n_cuts = 0

    cells = {(r, c) for r in range(h) for c in range(w)}
    if n_cuts == 0:
        return cells, w, h

    # Per-corner cut amounts (0 = not cut)
    cw = {'NW': 0, 'NE': 0, 'SW': 0, 'SE': 0}
    ch = {'NW': 0, 'NE': 0, 'SW': 0, 'SE': 0}
    # Width partners share the N or S row budget; height partners share W or E col budget
    w_partner = {'NW': 'NE', 'NE': 'NW', 'SW': 'SE', 'SE': 'SW'}
    h_partner = {'NW': 'SW', 'NE': 'SE', 'SW': 'NW', 'SE': 'NE'}

    def _cut(base, corner, cut_w, cut_h):
        if corner == 'NW': return base - {(r,c) for r in range(cut_h)     for c in range(cut_w)}
        if corner == 'NE': return base - {(r,c) for r in range(cut_h)     for c in range(w-cut_w, w)}
        if corner == 'SW': return base - {(r,c) for r in range(h-cut_h,h) for c in range(cut_w)}
        return                    base - {(r,c) for r in range(h-cut_h,h) for c in range(w-cut_w, w)}

    def _connected(s):
        if not s: return True
        q = [next(iter(s))]; visited = {q[0]}
        while q:
            r, c = q.pop()
            for nb in ((r-1,c),(r+1,c),(r,c-1),(r,c+1)):
                if nb in s and nb not in visited:
                    visited.add(nb); q.append(nb)
        return len(visited) == len(s)

    corners = ['NW', 'NE', 'SW', 'SE']
    rng.shuffle(corners)
    for corner in corners[:n_cuts]:
        max_cw = w - 3 - cw[w_partner[corner]]
        max_ch = h - 3 - ch[h_partner[corner]]
        if max_cw < 3 or max_ch < 3:
            continue
        cut_w = rng.randint(3, max_cw)
        cut_h = rng.randint(3, max_ch)
        candidate = _cut(cells, corner, cut_w, cut_h)
        if not _connected(candidate):
            continue  # skip any cut that splits the lake
        cells = candidate
        cw[corner] = cut_w
        ch[corner] = cut_h

    return cells, w, h

def step_blobs(grid, rng, log):
    rows, cols = len(grid), len(grid[0])
    occupied = set()
    placed = []
    mowdenpass_cells = []

    # Mountain rows
    for _ in range(rng.randint(0, MROWS_MAX)):
        for _ in range(12):
            length = rng.randint(MROW_LEN_MIN, min(MROW_LEN_MAX, cols))
            r  = rng.randint(0, rows - 1)
            c0 = rng.randint(0, cols - length)
            cells = [(r, c0 + i) for i in range(length)]
            if any(cell in occupied for cell in cells):
                continue
            seq = ["mowden_W"] + ["mowden_mid"] * (length - 2) + ["mowden_E"]
            for (cr, cc), t in zip(cells, seq):
                grid[cr][cc] = t
            occupied.update(cells)
            # optional mowdenpass adjacent
            if rng.random() < 0.35:
                adj = [
                    (cr + dr, cc)
                    for cr, cc in cells
                    for dr in (-1, 1)
                    if 0 <= cr + dr < rows
                    and (cr + dr, cc) not in occupied
                    and is_base(grid[cr + dr][cc])
                ]
                if adj:
                    pr, pc = rng.choice(adj)
                    grid[pr][pc] = "mowdenpass"
                    mowdenpass_cells.append((pr, pc))
            placed.append(f"mountain_row(r={r},c={c0},len={length})")
            break

    # Lakes (varied shapes: rect, L, T, U, S, plus via corner cuts)
    for _ in range(rng.randint(0, LAKES_MAX)):
        for _ in range(12):
            rel_cells, lw, lh = _gen_lake_shape(rng, rows, cols)
            if rows < lh or cols < lw:
                continue
            r0 = rng.randint(0, rows - lh)
            c0 = rng.randint(0, cols - lw)
            lake_cells = {(r0+dr, c0+dc) for dr, dc in rel_cells}
            if any(cell in occupied for cell in lake_cells):
                continue
            for (cr, cc) in lake_cells:
                grid[cr][cc] = _lake_tile_at(lake_cells, cr, cc)
            occupied.update(lake_cells)
            placed.append(f"lake(r={r0},c={c0},box={lw}x{lh},cells={len(lake_cells)})")
            break

    # Forest blobs
    for _ in range(rng.randint(0, FORESTS_MAX)):
        for _ in range(12):
            fw = rng.randint(FOREST_W_MIN, min(FOREST_W_MAX, cols))
            fh = rng.randint(FOREST_H_MIN, min(FOREST_H_MAX, rows))
            r = rng.randint(0, rows - fh)
            c = rng.randint(0, cols - fw)
            cells = [(r+dr, c+dc) for dr in range(fh) for dc in range(fw)]
            if any(cell in occupied for cell in cells):
                continue
            for dr in range(fh):
                for dc in range(fw):
                    rp = 0 if dr == 0 else (2 if dr == fh-1 else 1)
                    cp = 0 if dc == 0 else (2 if dc == fw-1 else 1)
                    grid[r+dr][c+dc] = _FOREST_TILE[rp][cp]
            occupied.update(cells)
            placed.append(f"forest(r={r},c={c},h={fh},w={fw})")
            break

    # Mnt blobs (always 3 rows tall, variable width)
    for _ in range(rng.randint(0, MNT_BLOBS_MAX)):
        for _ in range(12):
            mw = rng.randint(MNT_W_MIN, min(MNT_W_MAX, cols))
            if rows < 3:
                break
            r = rng.randint(0, rows - 3)
            c = rng.randint(0, cols - mw)
            cells = [(r+dr, c+dc) for dr in range(3) for dc in range(mw)]
            if any(cell in occupied for cell in cells):
                continue
            for dr in range(3):
                for dc in range(mw):
                    cp = 0 if dc == 0 else (2 if dc == mw-1 else 1)
                    grid[r+dr][c+dc] = _MNT_TILE[dr][cp]
            occupied.update(cells)
            placed.append(f"mnt_blob(r={r},c={c},w={mw})")
            break

    log.append("blobs: " + ("; ".join(placed) or "none"))
    return occupied, mowdenpass_cells

# ── STEP 3: DIRT BLOBS ────────────────────────────────────────────────────────
def step_dirt(grid, rng, blob_cells, log):
    """Rectangular dirt patches on base grass only. Walkable — not added to blob_cells."""
    rows, cols = len(grid), len(grid[0])
    if rng.random() > DIRT_CHANCE:
        return
    n_blobs = rng.randint(1, DIRTS_MAX)
    placed = []
    attempts = 0
    while len(placed) < n_blobs and attempts < 40:
        attempts += 1
        w  = rng.randint(DIRT_W_MIN, min(DIRT_W_MAX, cols))
        h  = rng.randint(DIRT_H_MIN, min(DIRT_H_MAX, rows))
        r0 = rng.randint(0, rows - h)
        c0 = rng.randint(0, cols - w)
        if any(grid[r0+dr][c0+dc] not in BASE_TILES
               for dr in range(h) for dc in range(w)):
            continue
        for dr in range(h):
            for dc in range(w):
                ri = 0 if dr == 0 else (2 if dr == h - 1 else 1)
                ci = 0 if dc == 0 else (2 if dc == w - 1 else 1)
                grid[r0+dr][c0+dc] = _DIRT_TILE[ri][ci]
        placed.append(f"dirt@({r0},{c0}) {w}×{h}")
    if placed:
        log.append("dirt: " + "; ".join(placed))

# ── STEP 4: FEATURE PLACEMENT ─────────────────────────────────────────────────
def step_features(grid, rng, blob_cells, is_hub_screen, log, mowdenpass_cells=None):
    rows, cols = len(grid), len(grid[0])
    feature_cells = {}
    placed_log = []

    # Register mowdenpass tiles as enterable features so paths connect to them.
    if mowdenpass_cells:
        for pr, pc in mowdenpass_cells:
            feature_cells[(pr, pc)] = "mowdenpass"
            placed_log.append(f"mowdenpass@({pr},{pc})")

    if is_hub_screen:
        cr, cc = rows // 2, cols // 2
        if not is_base(grid[cr][cc]):
            # Spiral outward from center to find nearest free base tile
            best, best_dist = None, float('inf')
            for r in range(rows):
                for c in range(cols):
                    if is_base(grid[r][c]):
                        d = abs(r - cr) + abs(c - cc)
                        if d < best_dist:
                            best_dist, best = d, (r, c)
            if best:
                cr, cc = best
        if is_base(grid[cr][cc]):
            grid[cr][cc] = "hub"
            feature_cells[(cr, cc)] = "hub"
            placed_log.append(f"hub@({cr},{cc})")

    free = [(r, c) for r in range(rows) for c in range(cols)
            if is_base(grid[r][c])]

    def place_one():
        if not free:
            return
        feat = rng.choice(FEATURE_POOL)
        r, c = rng.choice(free)
        free.remove((r, c))
        grid[r][c] = feat
        feature_cells[(r, c)] = feat
        placed_log.append(f"{feat}@({r},{c})")

    if rng.random() < FEATURE_CHANCE_1:
        place_one()
        if rng.random() < FEATURE_CHANCE_2:
            place_one()

    # mnt_cave: replace one mnt_S tile — prefer cells whose exit tile is already passable.
    mnt_s_cells = [(r, c) for r in range(rows) for c in range(cols)
                   if grid[r][c] == "mnt_S" and r + 1 < rows]
    if mnt_s_cells and rng.random() < MNT_CAVE_CHANCE:
        preferred = [(r, c) for r, c in mnt_s_cells
                     if grid[r + 1][c] not in BLOB_TILES]
        candidates = preferred if preferred else mnt_s_cells
        cr, cc = rng.choice(candidates)
        grid[cr][cc] = "mnt_cave"
        feature_cells[(cr, cc)] = "mnt_cave"
        # Guarantee the exit tile below is passable — clear any blob that snuck in.
        if grid[cr + 1][cc] in BLOB_TILES:
            grid[cr + 1][cc] = rng.choice(BASE_TILES)
            blob_cells.discard((cr + 1, cc))
        placed_log.append(f"mnt_cave@({cr},{cc})")

    # town1: dedicated placement roll so towns appear at a useful rate.
    if rng.random() < TOWN_CHANCE:
        town_free = [(r, c) for r in range(rows) for c in range(cols)
                     if is_base(grid[r][c]) and (r, c) not in feature_cells]
        if town_free:
            tr, tc = rng.choice(town_free)
            grid[tr][tc] = "town1"
            feature_cells[(tr, tc)] = "town1"
            placed_log.append(f"town1@({tr},{tc})")

    log.append("features: " + ("; ".join(placed_log) or "none"))
    return feature_cells

# ── STEP 4: PATH NETWORK ──────────────────────────────────────────────────────
_N, _S, _E, _W = 1, 2, 4, 8

# bitmask → (tile_name, clockwise_rotation_degrees)
# 3-way tile is rotated from default orientation W+S+E (bitmask 14)
BITMASK_TILE = {
    _N:              ("path_terminus_N",   0),
    _S:              ("path_terminus_S",   0),
    _E:              ("path_terminus_E",   0),
    _W:              ("path_terminus_W",   0),
    _N|_S:           ("path_vertical",     0),
    _E|_W:           ("path_horizontal",   0),
    _N|_E:           ("path_corner_N+E",   0),
    _N|_W:           ("path_corner_W+N",   0),
    _S|_E:           ("path_corner_S+E",   0),
    _S|_W:           ("path_corner_W+S",   0),
    _N|_S|_E|_W:     ("path_xroad_4way",   0),
    _S|_E|_W:        ("path_xroad_W+S+E",   0),   # default
    _N|_S|_W:        ("path_xroad_W+S+E",  90),   # 90° CW
    _N|_E|_W:        ("path_xroad_W+S+E", 180),
    _N|_S|_E:        ("path_xroad_W+S+E", 270),
}

def _biased_walk(start, end, rows, cols, blob_cells, feature_cells, rng, stop_at=None):
    """Jittered A* path from start to end (or to any cell in stop_at).

    Adding up to PATH_WALK_BIAS of random cost per step keeps paths from being
    perfectly straight while guaranteeing a path is found if one exists.
    """
    impassable = blob_cells | set(feature_cells.keys())
    impassable.discard(start)
    impassable.discard(end)

    def h(r, c): return abs(r - end[0]) + abs(c - end[1])

    # (f, tiebreak, pos)
    open_set = [(h(*start), 0, start)]
    parent   = {start: None}
    g_score  = {start: 0.0}
    counter  = 0

    while open_set:
        _, _, pos = heapq.heappop(open_set)
        if pos == end or (stop_at and pos != start and pos in stop_at):
            path = []
            node = pos
            while node is not None:
                path.append(node)
                node = parent[node]
            return list(reversed(path))

        r, c = pos
        dirs = [(-1,0),(1,0),(0,-1),(0,1)]
        rng.shuffle(dirs)
        for dr, dc in dirs:
            nr, nc = r+dr, c+dc
            nb = (nr, nc)
            if not (0 <= nr < rows and 0 <= nc < cols): continue
            if nb in impassable: continue
            # small random jitter creates natural-looking detours without getting stuck
            tg = g_score[pos] + 1.0 + rng.random() * PATH_WALK_BIAS
            if nb not in g_score or tg < g_score[nb]:
                g_score[nb] = tg
                parent[nb]  = pos
                counter += 1
                heapq.heappush(open_set, (tg + h(nr, nc), counter, nb))

    return [start]  # unreachable (all exits truly blocked)

def step_paths(grid, rng, blob_cells, feature_cells, log):
    rows, cols = len(grid), len(grid[0])
    path_set  = set()
    edge_bits = {}
    seed_log  = []

    impassable = blob_cells | set(feature_cells.keys())

    EBIT = {"N": _N, "S": _S, "E": _E, "W": _W}

    def free_on(edge):
        if edge == "N": return [(0,c)      for c in range(cols) if (0,c)      not in impassable]
        if edge == "S": return [(rows-1,c) for c in range(cols) if (rows-1,c) not in impassable]
        if edge == "E": return [(r,cols-1) for r in range(rows) if (r,cols-1) not in impassable]
        return                 [(r,0)      for r in range(rows) if (r,0)      not in impassable]

    def commit(walked, start_edge=None, end_edge=None, start_cell=None, end_cell=None):
        for cell in walked:
            if cell not in feature_cells and cell not in blob_cells:
                path_set.add(cell)
        if start_edge and start_cell in path_set:
            edge_bits[start_cell] = edge_bits.get(start_cell, 0) | EBIT[start_edge]
        if end_edge and end_cell in path_set:
            edge_bits[end_cell] = edge_bits.get(end_cell, 0) | EBIT[end_edge]

    # ── TRUNK: one path spanning two opposite edges ────────────────────────────
    # Randomise which axis is the trunk; the other two edges become branches.
    def _try_trunk(axis_pair):
        a_opts, b_opts = free_on(axis_pair[0]), free_on(axis_pair[1])
        if not a_opts or not b_opts:
            return False
        # Shuffle so repeated failures try different cells
        rng.shuffle(a_opts); rng.shuffle(b_opts)
        for a in a_opts[:3]:
            for b in b_opts[:3]:
                walked = _biased_walk(a, b, rows, cols, blob_cells, feature_cells, rng)
                if len(walked) > 1:
                    commit(walked, axis_pair[0], axis_pair[1], a, b)
                    seed_log.append(f"trunk:{axis_pair[0]}({a})->{axis_pair[1]}({b}) len={len(walked)}")
                    return True
        return False

    if rng.random() < 0.5:
        axes = [("N", "S"), ("E", "W")]
    else:
        axes = [("E", "W"), ("N", "S")]

    trunk_axis = None
    for ax in axes:
        if _try_trunk(ax):
            trunk_axis = ax
            break
    branch_edges = [e for ax in axes for e in ax if trunk_axis is None or e not in trunk_axis]

    # ── BRANCHES: remaining edges join the trunk, stopping on contact ──────────
    for edge in branch_edges:
        opts = free_on(edge)
        if not opts:
            continue
        if not path_set:
            # No trunk — treat as independent edge path toward opposite edge
            opp = {"N":"S","S":"N","E":"W","W":"E"}[edge]
            rng.shuffle(opts)
            for exit_cell in opts:
                opp_opts = free_on(opp)
                if not opp_opts:
                    break
                target = rng.choice(opp_opts)
                walked = _biased_walk(exit_cell, target, rows, cols, blob_cells, feature_cells, rng)
                if len(walked) > 1:
                    commit(walked, edge, opp, exit_cell, target)
                    seed_log.append(f"solo:{edge}({exit_cell})->path len={len(walked)}")
                    break
            continue
        rng.shuffle(opts)
        placed = False
        for exit_cell in opts:
            if exit_cell in path_set:
                edge_bits[exit_cell] = edge_bits.get(exit_cell, 0) | EBIT[edge]
                seed_log.append(f"branch:{edge} already on trunk")
                placed = True
                break
            target = min(path_set, key=lambda p: abs(p[0]-exit_cell[0]) + abs(p[1]-exit_cell[1]))
            walked = _biased_walk(exit_cell, target, rows, cols, blob_cells, feature_cells, rng,
                                  stop_at=path_set)
            if len(walked) > 1:
                commit(walked, edge, None, exit_cell, None)
                seed_log.append(f"branch:{edge}({exit_cell})->path len={len(walked)}")
                placed = True
                break
        if not placed:
            seed_log.append(f"branch:{edge} FAILED")

    # ── FEATURES: connect any unconnected enterable tile to nearest path cell ──
    for fpos in feature_cells:
        fr, fc = fpos
        if any((fr+dr, fc+dc) in path_set for dr, dc in ((-1,0),(1,0),(0,-1),(0,1))):
            continue
        if path_set:
            target = min(path_set, key=lambda p: abs(p[0]-fr) + abs(p[1]-fc))
            walked = _biased_walk(fpos, target, rows, cols, blob_cells, feature_cells, rng,
                                  stop_at=path_set)
            commit(walked)
        else:
            # Fallback: walk to nearest free edge cell
            best, best_bit, best_dist = None, 0, float('inf')
            for ecells, ebit in [
                ([(0,ec) for ec in range(cols)],      _N),
                ([(rows-1,ec) for ec in range(cols)], _S),
                ([(er,cols-1) for er in range(rows)], _E),
                ([(er,0) for er in range(rows)],      _W),
            ]:
                free = [p for p in ecells if p not in blob_cells and p not in feature_cells]
                if not free: continue
                nearest = min(free, key=lambda p: abs(p[0]-fr)+abs(p[1]-fc))
                d = abs(nearest[0]-fr)+abs(nearest[1]-fc)
                if d < best_dist:
                    best, best_bit, best_dist = nearest, ebit, d
            if best:
                walked = _biased_walk(fpos, best, rows, cols, blob_cells, feature_cells, rng)
                commit(walked, None, None, None, None)
                if best in path_set:
                    edge_bits[best] = edge_bits.get(best, 0) | best_bit
        seed_log.append(f"conn:{fpos}")

    # ── RESOLVE TILES ─────────────────────────────────────────────────────────
    for r, c in path_set:
        mask = 0
        if r > 0      and (r-1, c) in path_set: mask |= _N
        if r < rows-1 and (r+1, c) in path_set: mask |= _S
        if c < cols-1 and (r, c+1) in path_set: mask |= _E
        if c > 0      and (r, c-1) in path_set: mask |= _W
        mask |= edge_bits.get((r, c), 0)
        if mask == 0:
            mask = _N
        tile_name, rot = BITMASK_TILE.get(mask, ("path_vertical", 0))
        grid[r][c] = f"{tile_name}:{rot}" if rot else tile_name

    log.append("paths: " + ("; ".join(seed_log) or "none"))
    return path_set

# ── STEP 5: DECORATION SCATTER ────────────────────────────────────────────────
def step_scatter(grid, rng, log):
    rows, cols = len(grid), len(grid[0])

    noise = [[rng.random() for _ in range(cols)] for _ in range(rows)]
    # 3 smoothing passes to build spatial correlation → tree blobs, not scatter
    for _ in range(3):
        tmp = [[0.0] * cols for _ in range(rows)]
        for r in range(rows):
            for c in range(cols):
                vals = [noise[r][c]]
                for dr, dc in ((-1,0),(1,0),(0,-1),(0,1)):
                    nr, nc = r+dr, c+dc
                    if 0 <= nr < rows and 0 <= nc < cols:
                        vals.append(noise[nr][nc])
                tmp[r][c] = sum(vals) / len(vals)
        noise = tmp
    # normalize to [0,1] so density constants work as fractions of base cells
    flat = [noise[r][c] for r in range(rows) for c in range(cols)]
    mn, mx = min(flat), max(flat)
    if mx > mn:
        for r in range(rows):
            for c in range(cols):
                noise[r][c] = (noise[r][c] - mn) / (mx - mn)

    trees = ponds = mountains = 0
    for r in range(rows):
        for c in range(cols):
            if not is_base(grid[r][c]):
                continue
            v = noise[r][c]
            if v < TREE_DENSITY:
                grid[r][c] = rng.choice(TREE_TILES)
                trees += 1
            elif v < TREE_DENSITY + MOUNTAIN1_DENSITY:
                grid[r][c] = "mountain1"
                mountains += 1
            elif rng.random() < POND_DENSITY:
                grid[r][c] = "pond1"
                ponds += 1

    log.append(f"scatter: {trees} trees, {mountains} mountain1, {ponds} ponds")

# ── RENDERING ─────────────────────────────────────────────────────────────────
_MISSING = None  # lazy-init magenta fallback tile

def _get_tile(name, tiles):
    global _MISSING
    rot = 0
    if ':' in name:
        name, rot_s = name.split(':', 1)
        rot = int(rot_s)
    img = tiles.get(name)
    if img is None:
        if _MISSING is None:
            _MISSING = Image.new("RGBA", (TILE_PX, TILE_PX), (255, 0, 255, 255))
        img = _MISSING
    if rot:
        # rot is CW degrees; PIL transpose constants are CCW
        xp = {90: Image.Transpose.ROTATE_270,
              180: Image.Transpose.ROTATE_180,
              270: Image.Transpose.ROTATE_90}
        img = img.transpose(xp[rot])
    return img

def render_screen(grid, tiles):
    rows, cols = len(grid), len(grid[0])
    out = Image.new("RGBA", (cols * TILE_PX, rows * TILE_PX))
    for r in range(rows):
        for c in range(cols):
            cell = grid[r][c] or "grass1"
            img = _get_tile(cell, tiles)
            out.paste(img, (c * TILE_PX, r * TILE_PX), img)
    return out

# ── SCREEN GENERATION ─────────────────────────────────────────────────────────
@dataclass
class ScreenData:
    world_seed:    int
    sx:            int
    sy:            int
    screen_seed:   int
    rows:          int
    cols:          int
    grid:          list         # 2D list of tile-name strings
    feature_cells: dict         # (row, col) → tile_name string
    blob_cells:    set          # set of (row, col)
    palette:       tuple        # (green, blue, brown) as RGB tuples
    feature_types: dict         # (row, col) → type tag ("dungeon", "town", ...)
    log:           list = field(default_factory=list)


def generate_screen_data(world_seed, sx, sy, config_key=ACTIVE_CONFIG):
    """Generate structured screen data without rendering. No PIL, no raw_tiles needed."""
    cfg = SCREEN_CONFIGS[config_key]
    rows, cols = cfg["rows"], cfg["cols"]

    screen_seed = derive_screen_seed(world_seed, sx, sy)
    rng = random.Random(screen_seed)

    green, blue, brown = pick_palette(rng)

    pal_hex = (f"#{green[0]:02x}{green[1]:02x}{green[2]:02x} "
               f"#{blue[0]:02x}{blue[1]:02x}{blue[2]:02x} "
               f"#{brown[0]:02x}{brown[1]:02x}{brown[2]:02x}")
    log = [f"screen ({sx},{sy})  seed={screen_seed}  palette={pal_hex}"]

    grid = [[None] * cols for _ in range(rows)]
    step_base_fill(grid, rng)
    blob_cells, mowdenpass_cells = step_blobs(grid, rng, log)
    step_dirt(grid, rng, blob_cells, log)
    feature_cells = step_features(grid, rng, blob_cells, sx == 0 and sy == 0, log,
                                  mowdenpass_cells=mowdenpass_cells)
    _             = step_paths(grid, rng, blob_cells, feature_cells, log)
    step_scatter(grid, rng, log)

    feature_types = {pos: FEATURE_TYPES[name]
                     for pos, name in feature_cells.items()
                     if name in FEATURE_TYPES}

    return ScreenData(
        world_seed=world_seed,
        sx=sx, sy=sy,
        screen_seed=screen_seed,
        rows=rows, cols=cols,
        grid=grid,
        feature_cells=feature_cells,
        blob_cells=blob_cells,
        palette=(green, blue, brown),
        feature_types=feature_types,
        log=log,
    )


def render_screen_data(data, raw_tiles):
    """Render a ScreenData to a PIL image."""
    green, blue, brown = data.palette
    tiles = remap_tileset(raw_tiles, green, blue, brown)
    return render_screen(data.grid, tiles)


def generate_screen(world_seed, sx, sy, raw_tiles, config_key=ACTIVE_CONFIG):
    """Backward-compatible wrapper: returns (PIL image, log)."""
    data = generate_screen_data(world_seed, sx, sy, config_key)
    return render_screen_data(data, raw_tiles), data.log

# ── MAIN ──────────────────────────────────────────────────────────────────────
def main():
    world_seed = int.from_bytes(os.urandom(8), 'big') & 0x7FFFFFFFFFFFFFFF
    print(f"World seed: {world_seed}\n")

    os.makedirs(OUTPUT_DIR, exist_ok=True)
    raw_tiles = load_raw_tiles(TILESET_PATH, TILERULES_PATH)

    test_coords = [(0, 0), (1, 0), (-1, 0), (0, 1), (2, -1), (-2, 3)]

    for sx, sy in test_coords:
        img, log_lines = generate_screen(world_seed, sx, sy, raw_tiles)
        fname    = f"screen_{world_seed}_{sx}_{sy}.png"
        out_path = os.path.join(OUTPUT_DIR, fname)
        img.save(out_path)
        for line in log_lines:
            print(line)
        print(f"  -> {out_path}\n")

if __name__ == "__main__":
    main()
