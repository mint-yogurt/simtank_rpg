"""Procedural overworld screen generator — test harness for simtank_rpg.

Each run uses a new random world seed (printed + embedded in output filenames).
Generation order per screen: base fill → blobs → features → paths → scatter.
"""

import hashlib
import os
import random
import struct

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
PATH_SEEDS_MIN      = 1
PATH_SEEDS_MAX      = 3
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
PATH_WALK_BIAS      = 0.70   # fraction of steps taken toward target

BASE_TILES   = ["grass1", "grass2"]
FEATURE_POOL = ["house1", "tower1", "tower2", "town1", "castle1",
                "skullhouse1", "cave1", "building1"]
TREE_TILES   = ["tree1", "treedead1", "tree2"]

_HERE          = os.path.dirname(os.path.abspath(__file__))
TILESET_PATH   = os.path.join(_HERE, "..", "web", "static", "tiles", "overworld_1.png")
TILERULES_PATH = os.path.join(_HERE, "..", "web", "static", "tiles", "overworld_1_tilerules.txt")
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
    "lake_NW", "lake_N", "lake_NE",
    "lake_W",  "lake_mid", "lake_E",
    "lake_SW", "lake_S",  "lake_SE",
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

def step_blobs(grid, rng, log):
    rows, cols = len(grid), len(grid[0])
    occupied = set()
    placed = []

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
            placed.append(f"mountain_row(r={r},c={c0},len={length})")
            break

    # Lakes
    for _ in range(rng.randint(0, LAKES_MAX)):
        for _ in range(12):
            lw = rng.randint(LAKE_W_MIN, LAKE_W_MAX)
            lh = rng.randint(LAKE_H_MIN, LAKE_H_MAX)
            if rows < lh or cols < lw:
                break
            r = rng.randint(0, rows - lh)
            c = rng.randint(0, cols - lw)
            cells = [(r+dr, c+dc) for dr in range(lh) for dc in range(lw)]
            if any(cell in occupied for cell in cells):
                continue
            for dr in range(lh):
                for dc in range(lw):
                    rp = 0 if dr == 0 else (2 if dr == lh-1 else 1)
                    cp = 0 if dc == 0 else (2 if dc == lw-1 else 1)
                    grid[r+dr][c+dc] = _LAKE_TILE[rp][cp]
            occupied.update(cells)
            placed.append(f"lake(r={r},c={c},h={lh},w={lw})")
            break

    log.append("blobs: " + ("; ".join(placed) or "none"))
    return occupied

# ── STEP 3: FEATURE PLACEMENT ─────────────────────────────────────────────────
def step_features(grid, rng, blob_cells, is_hub_screen, log):
    rows, cols = len(grid), len(grid[0])
    feature_cells = {}
    placed_log = []

    if is_hub_screen:
        cr, cc = rows // 2, cols // 2
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

def _biased_walk(start, end, rows, cols, blob_cells, feature_cells, rng):
    impassable = blob_cells | set(feature_cells.keys())
    impassable.discard(start)
    impassable.discard(end)

    path = []
    pos = start
    visited = {pos}
    max_steps = rows * cols * 4

    for _ in range(max_steps):
        path.append(pos)
        if pos == end:
            break
        r, c = pos
        er, ec = end

        toward = set()
        if er < r: toward.add((-1, 0))
        if er > r: toward.add(( 1, 0))
        if ec < c: toward.add(( 0,-1))
        if ec > c: toward.add(( 0, 1))

        all_dirs = [(-1,0),(1,0),(0,-1),(0,1)]
        rng.shuffle(all_dirs)

        candidates = []
        for d in all_dirs:
            nr, nc = r + d[0], c + d[1]
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if (nr, nc) in impassable:
                continue
            w = PATH_WALK_BIAS if d in toward else (1.0 - PATH_WALK_BIAS) / 3
            candidates.append(((nr, nc), w))

        if not candidates:
            break

        # prefer unvisited
        unvisited = [(cell, w) for cell, w in candidates if cell not in visited]
        pool = unvisited if unvisited else candidates

        total = sum(w for _, w in pool)
        pick = rng.random() * total
        acc = 0.0
        chosen = pool[-1][0]
        for cell, w in pool:
            acc += w
            if pick <= acc:
                chosen = cell
                break

        visited.add(chosen)
        pos = chosen

    return path

def step_paths(grid, rng, blob_cells, feature_cells, log):
    rows, cols = len(grid), len(grid[0])
    n_seeds = rng.randint(PATH_SEEDS_MIN, PATH_SEEDS_MAX)

    path_set  = set()   # cells that are path
    edge_bits = {}      # (r,c) -> accumulated outward direction bits for edge exits

    def free_edge_cell(edge):
        if edge == "N":
            opts = [(0, c) for c in range(cols)
                    if (0,c) not in blob_cells and (0,c) not in feature_cells]
            return (rng.choice(opts), _N) if opts else (None, 0)
        if edge == "S":
            opts = [(rows-1, c) for c in range(cols)
                    if (rows-1,c) not in blob_cells and (rows-1,c) not in feature_cells]
            return (rng.choice(opts), _S) if opts else (None, 0)
        if edge == "E":
            opts = [(r, cols-1) for r in range(rows)
                    if (r,cols-1) not in blob_cells and (r,cols-1) not in feature_cells]
            return (rng.choice(opts), _E) if opts else (None, 0)
        # W
        opts = [(r, 0) for r in range(rows)
                if (r,0) not in blob_cells and (r,0) not in feature_cells]
        return (rng.choice(opts), _W) if opts else (None, 0)

    def random_interior():
        opts = [(r, c) for r in range(1, rows-1) for c in range(1, cols-1)
                if (r,c) not in blob_cells and (r,c) not in feature_cells]
        return rng.choice(opts) if opts else None

    seed_log = []
    for _ in range(n_seeds):
        # Build endpoint pool: (kind, cell, edge_bit)
        pool = []
        for edge in ("N","S","E","W"):
            cell, bit = free_edge_cell(edge)
            if cell:
                pool.append(("edge", cell, bit))
        for fpos in feature_cells:
            pool.append(("feature", fpos, 0))
        ip = random_interior()
        if ip:
            pool.append(("interior", ip, 0))

        if len(pool) < 2:
            continue

        a = rng.choice(pool)
        pool_b = [e for e in pool if e[1] != a[1]]
        if not pool_b:
            continue
        b = rng.choice(pool_b)

        walked = _biased_walk(a[1], b[1], rows, cols, blob_cells, feature_cells, rng)

        for cell in walked:
            if cell not in feature_cells and cell not in blob_cells:
                path_set.add(cell)

        for kind, cell, bit in (a, b):
            if kind == "edge" and cell in path_set:
                edge_bits[cell] = edge_bits.get(cell, 0) | bit

        seed_log.append(f"{a[0]}({a[1]})->{b[0]}({b[1]}) len={len(walked)}")

    # Resolve tiles from bitmask
    for r, c in path_set:
        mask = 0
        if r > 0      and (r-1, c) in path_set: mask |= _N
        if r < rows-1 and (r+1, c) in path_set: mask |= _S
        if c < cols-1 and (r, c+1) in path_set: mask |= _E
        if c > 0      and (r, c-1) in path_set: mask |= _W
        mask |= edge_bits.get((r, c), 0)
        if mask == 0:
            mask = _N  # isolated fallback; shouldn't occur
        tile_name, rot = BITMASK_TILE.get(mask, ("path_vertical", 0))
        grid[r][c] = f"{tile_name}:{rot}" if rot else tile_name

    log.append(f"paths: {n_seeds} seed(s); " + ("; ".join(seed_log) or "none"))
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
def generate_screen(world_seed, sx, sy, raw_tiles, config_key=ACTIVE_CONFIG):
    cfg = SCREEN_CONFIGS[config_key]
    rows, cols = cfg["rows"], cfg["cols"]

    seed = derive_screen_seed(world_seed, sx, sy)
    rng  = random.Random(seed)

    green, blue, brown = pick_palette(rng)
    tiles = remap_tileset(raw_tiles, green, blue, brown)

    pal_hex = (f"#{green[0]:02x}{green[1]:02x}{green[2]:02x} "
               f"#{blue[0]:02x}{blue[1]:02x}{blue[2]:02x} "
               f"#{brown[0]:02x}{brown[1]:02x}{brown[2]:02x}")
    log = [f"screen ({sx},{sy})  seed={seed}  palette={pal_hex}"]

    grid = [[None] * cols for _ in range(rows)]
    step_base_fill(grid, rng)
    blob_cells    = step_blobs(grid, rng, log)
    feature_cells = step_features(grid, rng, blob_cells, sx == 0 and sy == 0, log)
    _             = step_paths(grid, rng, blob_cells, feature_cells, log)
    step_scatter(grid, rng, log)

    return render_screen(grid, tiles), log

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
