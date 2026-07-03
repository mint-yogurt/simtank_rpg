"""
Procedural 8-bit enemy sprite generator.

No image-gen model required. The approach:
  1. Work on the LEFT HALF of the grid only, then mirror -> bilateral symmetry
     (this is the trick that makes noise read as a creature facing you).
  2. A per-archetype probability FIELD biases where "body" pixels can appear,
     so different enemies read as different body plans (blob / humanoid /
     flyer / beast / orb) instead of random splats.
  3. A tiny cellular-automata smoothing pass removes stray/lonely pixels and
     fills pinholes, giving connected masses.
  4. Outline pass: empty cells touching body become the dark outline.
  5. Directional shading (fake top-left light) picks base / highlight / shadow.
  6. Symmetric eyes get stamped into the upper body -> instant "alive".
  7. Palette is chosen by "element type" for readability + variety.

Deterministic: same (seed) -> same sprite. Great for an RPG where an enemy
"species id" should always look the same.
"""

import numpy as np
import random
from PIL import Image

# ----------------------------------------------------------------------------
# Palettes. Each = (outline, shadow, base, highlight) -> 4 colours total.
# The eye reuses `highlight`, and the texture noise only uses shadow/highlight,
# so a finished sprite never shows more than these 4 colours (black included).
# ----------------------------------------------------------------------------
PALETTES = {
    "fire":    ((26, 10, 8),   (150, 40, 20),  (230, 110, 30), (250, 205, 90)),
    "poison":  ((12, 22, 10),  (40, 110, 45),  (95, 190, 80),  (185, 235, 120)),
    "ice":     ((12, 22, 36),  (45, 100, 160), (95, 165, 225), (200, 235, 252)),
    "shadow":  ((8, 6, 14),    (46, 36, 74),   (92, 72, 128),  (165, 140, 205)),
    "earth":   ((22, 14, 8),   (95, 62, 32),   (155, 112, 60), (215, 180, 115)),
    "arcane":  ((14, 8, 26),   (74, 42, 132),  (135, 85, 212), (205, 165, 250)),
    "blood":   ((22, 6, 8),    (112, 22, 30),  (182, 44, 50),  (232, 120, 110)),
    "slime":   ((8, 22, 20),   (30, 122, 110), (62, 192, 172), (165, 242, 222)),
    "gold":    ((30, 22, 6),   (150, 110, 20), (215, 175, 45), (250, 232, 145)),
    "bone":    ((26, 24, 20),  (120, 116, 102),(196, 190, 172),(242, 238, 226)),
}

# state grid values
EMPTY, BODY = 0, 1


# ----------------------------------------------------------------------------
# Archetype probability fields (computed on the LEFT half only).
# x runs 0..hw-1 where hw-1 is the column touching the mirror axis (the center).
# Higher value -> more likely to be body.
# ----------------------------------------------------------------------------
def _field(archetype, hw, h):
    ax, ay = np.meshgrid(np.arange(hw), np.arange(h))  # ax: col, ay: row
    axis_d = (hw - 1 - ax) / max(hw - 1, 1)   # 0 at center axis, ->1 at outer edge
    cy = (h - 1) / 2.0
    vy = (ay - cy) / (h / 2.0)                # -1 top .. +1 bottom (roughly)

    f = np.zeros((h, hw), dtype=float)

    if archetype == "blob":
        # fat central ellipse, slightly bottom-heavy
        r = (axis_d * 1.15) ** 2 + ((vy + 0.15) * 1.0) ** 2
        f = 1.25 - r

    elif archetype == "orb":
        # tight circle, floating -> reads as eye / core / bubble
        r = (axis_d * 1.4) ** 2 + (vy * 1.4) ** 2
        f = 1.3 - r

    elif archetype == "humanoid":
        head = 1.2 - ((axis_d * 2.2) ** 2 + ((vy + 0.72) * 3.2) ** 2)
        torso = 1.15 - ((axis_d * 1.5) ** 2 + ((vy + 0.02) * 1.5) ** 2)
        # legs: two masses low, offset from center so mirroring gives a gap
        leg = 1.1 - (((axis_d - 0.45) * 3.2) ** 2 + ((vy - 0.72) * 2.2) ** 2)
        f = np.maximum.reduce([head, torso, leg])

    elif archetype == "flyer":
        body = 1.2 - ((axis_d * 2.4) ** 2 + (vy * 1.7) ** 2)
        # wings stretch outward around mid height
        wing = 1.05 - (((axis_d - 0.6) * 1.6) ** 2 + (vy * 3.0) ** 2)
        f = np.maximum(body, wing)

    elif archetype == "beast":
        # low horizontal mass + legs -> quadruped-ish
        body = 1.2 - ((axis_d * 1.3) ** 2 + ((vy - 0.15) * 1.9) ** 2)
        legs = 1.05 - (((axis_d - 0.5) * 3.4) ** 2 + ((vy - 0.75) * 2.4) ** 2)
        head = 1.1 - (((axis_d - 0.62) * 3.0) ** 2 + ((vy + 0.55) * 3.0) ** 2)
        f = np.maximum.reduce([body, legs, head])

    else:
        raise ValueError(archetype)

    return np.clip(f, 0.0, 1.0)


def _smooth(grid):
    """One CA pass: kill lonely pixels, fill holes. Operates on left half."""
    h, w = grid.shape
    padded = np.pad(grid, 1, mode="constant")
    neigh = np.zeros_like(grid)
    for dy in (-1, 0, 1):
        for dx in (-1, 0, 1):
            if dx == 0 and dy == 0:
                continue
            neigh += padded[1 + dy:1 + dy + h, 1 + dx:1 + dx + w]
    out = grid.copy()
    out[(grid == BODY) & (neigh <= 1)] = EMPTY   # remove isolated
    out[(grid == EMPTY) & (neigh >= 5)] = BODY    # fill pockets
    return out


def _largest_blob(grid):
    """Keep only the largest 4-connected body component (drop floating debris)."""
    h, w = grid.shape
    lbl = np.zeros((h, w), dtype=int)
    cur = 0
    best_id, best_sz = 0, 0
    for sy in range(h):
        for sx in range(w):
            if grid[sy, sx] != BODY or lbl[sy, sx]:
                continue
            cur += 1
            stack = [(sy, sx)]
            lbl[sy, sx] = cur
            sz = 0
            while stack:
                y, x = stack.pop()
                sz += 1
                for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
                    ny, nx = y + dy, x + dx
                    if 0 <= ny < h and 0 <= nx < w and grid[ny, nx] == BODY and not lbl[ny, nx]:
                        lbl[ny, nx] = cur
                        stack.append((ny, nx))
            if sz > best_sz:
                best_sz, best_id = sz, cur
    return np.where(lbl == best_id, BODY, EMPTY)


def _mirror(half):
    """half is left side (h x hw). Return full (h x 2*hw)."""
    return np.concatenate([half, half[:, ::-1]], axis=1)


def generate_grid(seed, size=16, archetype=None, density=0.55):
    rng = np.random.default_rng(seed)
    if archetype is None:
        archetype = rng.choice(["blob", "orb", "humanoid", "flyer", "beast"])
    hw = size // 2
    field = _field(archetype, hw, size)
    # sample body cells: probability = field * density scaler
    roll = rng.random((size, hw))
    half = np.where(roll < field * (0.55 + density * 0.6), BODY, EMPTY)
    half = _smooth(half)
    half = _smooth(half)
    # guarantee the center column has something so halves connect
    if half[:, hw - 1].sum() == 0:
        half[size // 2, hw - 1] = BODY
    grid = _mirror(half)
    grid = _largest_blob(grid)
    return grid, archetype


# ----------------------------------------------------------------------------
# Rendering: grid -> RGBA pixels with outline, shading, eyes.
# ----------------------------------------------------------------------------
def _neighbors_empty(grid, y, x, dy, dx):
    h, w = grid.shape
    ny, nx = y + dy, x + dx
    if ny < 0 or ny >= h or nx < 0 or nx >= w:
        return True
    return grid[ny, nx] == EMPTY


# role codes for each pixel; colourised at the very end
R_EMPTY, R_OUT, R_BASE, R_HI, R_SHA, R_EYE = 0, 1, 2, 3, 4, 5


def _outline_mask(is_body):
    h, w = is_body.shape
    is_outline = np.zeros_like(is_body)
    for dy, dx in ((-1, 0), (1, 0), (0, -1), (0, 1)):
        shifted = np.zeros_like(is_body)
        ys = slice(max(0, dy), h + min(0, dy))
        xs = slice(max(0, dx), w + min(0, dx))
        yt = slice(max(0, -dy), h + min(0, -dy))
        xt = slice(max(0, -dx), w + min(0, -dx))
        shifted[yt, xt] = is_body[ys, xs]
        is_outline |= shifted
    return is_outline & ~is_body


# 4x4 Bayer matrix for ordered dithering
_BAYER = np.array([[0, 8, 2, 10], [12, 4, 14, 6],
                   [3, 11, 1, 9], [15, 7, 13, 5]]) / 16.0


def _apply_texture(role, grid, seed, style, strength):
    """Mottle the flat BASE fill using the 4-tone palette so blobs aren't solid.
    Only ever recolours BASE cells; the volumetric rim (HI/SHA) is preserved."""
    if style == "none" or strength <= 0:
        return role
    h, w = grid.shape
    base_mask = role == R_BASE
    rng = np.random.default_rng(seed ^ 0x7E27)

    if style == "speckle":
        # coherent noise: white noise + one smoothing pass -> clumps, not static
        noise = rng.random((h, w))
        sm = noise.copy()
        for dy in (-1, 0, 1):
            for dx in (-1, 0, 1):
                sm += np.roll(np.roll(noise, dy, 0), dx, 1)
        sm /= 10.0
        vals = sm[base_mask]
        if vals.size:
            hi = np.quantile(vals, 1.0 - 0.14 * strength)   # sparse sparkles
            lo = np.quantile(vals, 0.30 * strength)          # more speckle shade
            role[base_mask & (sm >= hi)] = R_HI
            role[base_mask & (sm <= lo)] = R_SHA

    elif style == "dither":
        # ordered dither: vertical value gradient rendered as a Bayer pattern
        yy = np.arange(h)[:, None] / max(h - 1, 1)
        thresh = np.tile(_BAYER, (h // 4 + 1, w // 4 + 1))[:h, :w]
        grad = 0.35 + 0.5 * yy * strength      # darker toward the bottom
        role[base_mask & (grad > thresh)] = R_SHA
        role[base_mask & (grad < thresh * 0.4)] = R_HI

    elif style == "spots":
        # a few blobby markings
        n = int(2 + 3 * strength)
        ys, xs = np.where(base_mask)
        if len(ys):
            for _ in range(n):
                i = rng.integers(len(ys))
                cy, cx = ys[i], xs[i]
                rad = rng.integers(1, 3)
                col = R_SHA if rng.random() < 0.7 else R_HI
                for y in range(max(0, cy - rad), min(h, cy + rad + 1)):
                    for x in range(max(0, cx - rad), min(w, cx + rad + 1)):
                        if base_mask[y, x] and (y - cy) ** 2 + (x - cx) ** 2 <= rad * rad:
                            role[y, x] = col
    return role


def _face_row(grid, hw, h):
    """Pick the row for the face: widest body band in the upper portion."""
    best, best_score = None, -1
    for ry in range(1, int(h * 0.6)):
        row = grid[ry]
        if not (row[hw - 1] == BODY or row[hw] == BODY):
            continue
        w2 = 0
        while hw - 1 - w2 >= 0 and row[hw - 1 - w2] == BODY:
            w2 += 1
        score = w2 * 2 + (2 if ry < h * 0.4 else 0) - ry * 0.1
        if score > best_score:
            best_score, best = score, (ry, w2)
    return best


def _stamp_face(role, grid, seed):
    """Symmetric eyes + pupils/brows + a mouth, placed on the found face band."""
    h, w = grid.shape
    hw = w // 2
    fr = _face_row(grid, hw, h)
    if fr is None:
        return role
    ry, w2 = fr
    rng = np.random.default_rng(seed ^ 0x5EED)

    def body(y, x):
        return 0 <= y < h and 0 <= x < w and grid[y, x] == BODY

    # --- eyes ---
    if w2 <= 1:  # narrow -> cyclops (two center cols read as one eye)
        eye_cols = [hw - 1, hw]
        for x in eye_cols:
            if body(ry, x):
                role[ry, x] = R_EYE
        # central pupil below
        if body(ry + 1, hw - 1):
            role[ry + 1, hw - 1] = role[ry + 1, hw] = R_OUT
    else:
        g = int(rng.integers(1, min(w2 - 1, 3) + 1))  # gap from centre
        lx, rx = hw - 1 - g, hw + g
        if body(ry, lx):
            role[ry, lx] = role[ry, rx] = R_EYE
            style = rng.random()
            if style < 0.4 and body(ry + 1, lx):        # pupils look down
                role[ry + 1, lx] = role[ry + 1, rx] = R_OUT
            elif style < 0.75 and body(ry - 1, lx - 1):  # angry brows
                role[ry - 1, lx - 1] = role[ry - 1, rx + 1] = R_OUT

    # --- mouth --- a couple rows under the eyes, centred
    for off in (3, 2, 4):
        my = ry + off
        if body(my, hw - 1) or body(my, hw):
            break
    else:
        return role
    kind = rng.random()
    mw = int(rng.integers(1, min(w2, 3) + 1))
    if kind < 0.45:                                  # thin line mouth
        for i in range(mw):
            for x in (hw - 1 - i, hw + i):
                if body(my, x):
                    role[my, x] = R_OUT
    elif kind < 0.75:                                # open maw (2 rows)
        for yy in (my, my + 1):
            for i in range(mw):
                for x in (hw - 1 - i, hw + i):
                    if body(yy, x):
                        role[yy, x] = R_OUT
    else:                                            # fanged: dark line + teeth
        for i in range(mw):
            for x in (hw - 1 - i, hw + i):
                if body(my, x):
                    role[my, x] = R_OUT
        for x in (hw - 1, hw):
            if body(my + 1, x):
                role[my + 1, x] = R_EYE
    return role


def render(grid, palette_name="fire", seed=0, face=True, texture="speckle",
           tex_strength=1.0, frame=0):
    outline, shadow, base, highlight = PALETTES[palette_name]
    eye = highlight  # reuse the lightest tone -> stays at 4 colours total
    h, w = grid.shape
    is_body = grid == BODY
    role = np.zeros((h, w), dtype=int)
    role[_outline_mask(is_body)] = R_OUT

    # directional shading (light from top-left)
    for y in range(h):
        for x in range(w):
            if not is_body[y, x]:
                continue
            lit = _neighbors_empty(grid, y, x, -1, 0) or _neighbors_empty(grid, y, x, 0, -1)
            dark = _neighbors_empty(grid, y, x, 1, 0) or _neighbors_empty(grid, y, x, 0, 1)
            role[y, x] = R_HI if lit else (R_SHA if dark else R_BASE)

    # frame only perturbs the interior noise -> a subtle idle shimmer,
    # silhouette/face/palette stay identical between frames.
    tex_seed = seed + frame * 104729
    role = _apply_texture(role, grid, tex_seed, texture, tex_strength)
    if face:
        role = _stamp_face(role, grid, seed)

    palette = {R_OUT: outline, R_BASE: base, R_HI: highlight, R_SHA: shadow, R_EYE: eye}
    img = np.zeros((h, w, 4), dtype=np.uint8)
    for r, col in palette.items():
        m = role == r
        img[m] = (*col, 255)
    return img


def make_sprite(seed, size=16, palette_name=None, archetype=None, density=0.55,
                face=True, texture="speckle", tex_strength=1.0, frame=0):
    rng = np.random.default_rng(seed)
    if palette_name is None:
        palette_name = rng.choice(list(PALETTES))
    grid, arch = generate_grid(seed, size=size, archetype=archetype, density=density)
    px = render(grid, palette_name=palette_name, seed=seed, face=face,
                texture=texture, tex_strength=tex_strength, frame=frame)
    return Image.fromarray(px, "RGBA"), arch, palette_name


def make_pair(seed, **kw):
    """Two frames of the same creature for a primitive 2-frame idle."""
    a, arch, pal = make_sprite(seed, frame=0, **kw)
    b, _, _ = make_sprite(seed, frame=1, **kw)
    return a, b, arch, pal


# ----------------------------------------------------------------------------
# Demo: contact sheet
# ----------------------------------------------------------------------------
def contact_sheet(path, rows=8, cols=12, size=16, scale=6, start_seed=0):
    pad = 4
    cellw = size * scale + pad
    sheet = Image.new("RGBA", (cols * cellw + pad, rows * cellw + pad), (30, 30, 38, 255))
    seed = start_seed
    for r in range(rows):
        for c in range(cols):
            spr, _, _ = make_sprite(seed, size=size)
            spr = spr.resize((size * scale, size * scale), Image.NEAREST)
            sheet.paste(spr, (pad + c * cellw, pad + r * cellw), spr)
            seed += 1
    sheet.save(path)
    return path


def pairs_sheet(path, rows=8, cols=6, size=16, scale=8, start_seed=0):
    """Each creature shown as its two idle frames, side by side."""
    intra, pad = scale, scale * 3           # gap within a pair vs between pairs
    cw = size * scale
    pairw = 2 * cw + intra
    sheet = Image.new("RGBA", (pad + cols * (pairw + pad),
                               pad + rows * (cw + pad)), (30, 30, 38, 255))
    seed = start_seed
    for r in range(rows):
        for c in range(cols):
            a, b, _, _ = make_pair(seed, size=size)
            x0 = pad + c * (pairw + pad)
            y0 = pad + r * (cw + pad)
            for i, spr in enumerate((a, b)):
                spr = spr.resize((cw, cw), Image.NEAREST)
                sheet.paste(spr, (x0 + i * (cw + intra), y0), spr)
            seed += 1
    sheet.save(path)
    return path


def save_gif(seed, path, size=16, scale=10, duration=380):
    a, b, _, _ = make_pair(seed, size=size)
    frames = []
    for spr in (a, b):
        bg = Image.new("RGBA", spr.size, (30, 30, 38, 255))
        bg.paste(spr, mask=spr.split()[3])
        frames.append(bg.resize((size * scale, size * scale), Image.NEAREST).convert("P"))
    frames[0].save(path, save_all=True, append_images=frames[1:],
                   duration=duration, loop=0, disposal=2)
    return path


if __name__ == "__main__":
    import os
    out = os.path.join(os.path.dirname(os.path.abspath(__file__)), "out")
    os.makedirs(out, exist_ok=True)
    base = random.randint(0, 10**9)          # new batch every run
    pairs_sheet(os.path.join(out, "pairs16.png"), rows=8, cols=6, size=16, scale=8)
    print(f"wrote sheet to {out}")