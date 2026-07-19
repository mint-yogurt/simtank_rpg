"""Retro pixel visualizer — standalone pygame demo, not part of the game.

2000s-era music-visualizer / Jeff Minter "Virtual Light Machine" homage: a
handful of effects (plasma, ripple interference, particle swarm, tunnel warp,
starfield warp) rendered onto a 256x240 buffer and scaled up. Each effect is
driven purely by a continuous time value, so nothing ever repeats exactly.
Pick one effect at the prompt and it runs standalone; ESC or close the window
to quit.

Run directly:
    source .venv/bin/activate
    python procgen/visualizer.py
"""

import math
import sys
import time
from pathlib import Path

import numpy as np
import pygame

# ── CONFIG ────────────────────────────────────────────────────────────────────
WIDTH, HEIGHT = 256, 240
SCALE = 3
FPS = 60


def smoothstep(x):
    """Ease 0..1 with zero slope at both ends, instead of linear/robotic motion."""
    x = np.clip(x, 0.0, 1.0)
    return x * x * (3.0 - 2.0 * x)


def hsv_to_rgb(h, s, v):
    """Vectorized HSV->RGB. h/s/v are ndarrays of any matching shape, 0..1."""
    h = np.asarray(h, dtype=np.float32) % 1.0
    s = np.asarray(s, dtype=np.float32)
    v = np.asarray(v, dtype=np.float32)

    i = (h * 6.0).astype(np.int32) % 6
    f = (h * 6.0) - np.floor(h * 6.0)
    p = v * (1.0 - s)
    q = v * (1.0 - s * f)
    t_ = v * (1.0 - s * (1.0 - f))

    r = np.choose(i, [v, q, p, p, t_, v])
    g = np.choose(i, [t_, v, v, q, p, p])
    b = np.choose(i, [p, p, t_, v, v, q])
    return np.stack([r, g, b], axis=-1)


class PlasmaEffect:
    """Classic sum-of-sines plasma with a slowly drifting hue field."""

    def __init__(self):
        ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH]
        self.xs = xs.astype(np.float32)
        self.ys = ys.astype(np.float32)

    def render(self, t):
        xs, ys = self.xs, self.ys
        cx = WIDTH / 2 + 40 * math.sin(t * 0.3)
        cy = HEIGHT / 2 + 40 * math.cos(t * 0.25)
        dist = np.sqrt((xs - cx) ** 2 + (ys - cy) ** 2)

        v = (
            np.sin(xs * 0.05 + t * 1.3)
            + np.sin(ys * 0.07 - t * 0.9)
            + np.sin((xs + ys) * 0.04 + t * 0.6)
            + np.sin(dist * 0.08 - t * 1.8)
        )
        v = (v + 4.0) / 8.0
        hue = (v + t * 0.05) % 1.0
        return hsv_to_rgb(hue, np.ones_like(v), v ** 0.5)


class RipplesEffect:
    """Circular wave interference from a few orbiting point sources."""

    def __init__(self, n_sources=3):
        ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH]
        self.xs = xs.astype(np.float32)
        self.ys = ys.astype(np.float32)
        self.n = n_sources

    def render(self, t):
        v = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
        for k in range(self.n):
            cx = WIDTH / 2 + WIDTH * 0.35 * math.sin(t * 0.21 + k * 2.1)
            cy = HEIGHT / 2 + HEIGHT * 0.35 * math.cos(t * 0.17 + k * 1.7)
            d = np.sqrt((self.xs - cx) ** 2 + (self.ys - cy) ** 2)
            v += np.sin(d * 0.25 - t * 2.2 + k)
        v = (v / self.n + 1.0) / 2.0
        hue = (v * 0.6 + t * 0.04) % 1.0
        return hsv_to_rgb(hue, np.ones_like(v), v ** 0.6)


class SwarmEffect:
    """VLM-style swarm of colored points on morphing Lissajous orbits, with trails."""

    def __init__(self, n=220):
        self.n = n
        self.trail = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
        self.phase = (np.random.rand(n).astype(np.float32)) * math.tau
        self.idx = np.arange(n, dtype=np.float32)

    def render(self, t):
        n, i = self.n, self.idx
        fx = 2.0 + np.sin(t * 0.05 + i * 0.013) * 1.5
        fy = 3.0 + np.cos(t * 0.037 + i * 0.021) * 1.5
        px = WIDTH / 2 + (WIDTH / 2 - 10) * np.sin(fx * t * 0.6 + self.phase)
        py = HEIGHT / 2 + (HEIGHT / 2 - 10) * np.sin(fy * t * 0.6 + self.phase * 1.3)

        hue = (i / n + t * 0.08) % 1.0
        colors = hsv_to_rgb(hue, np.ones(n, dtype=np.float32), np.ones(n, dtype=np.float32)) * 255.0

        self.trail *= 0.90
        xi = np.clip(px.astype(np.int32), 0, WIDTH - 1)
        yi = np.clip(py.astype(np.int32), 0, HEIGHT - 1)
        np.add.at(self.trail, (yi, xi), colors)
        np.clip(self.trail, 0, 255, out=self.trail)
        return self.trail / 255.0


class TunnelEffect:
    """Color tunnel warp — radiating rings rushing past the camera."""

    def __init__(self):
        ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH]
        cx, cy = WIDTH / 2, HEIGHT / 2
        dx = (xs - cx).astype(np.float32)
        dy = (ys - cy).astype(np.float32)
        self.dist = np.sqrt(dx * dx + dy * dy)
        self.angle = (np.arctan2(dy, dx) / math.tau).astype(np.float32)

    def render(self, t):
        depth = 20.0 / (self.dist + 1.0) + t * 0.6
        hue = (self.angle * 3.0 + depth * 0.15 + t * 0.1) % 1.0
        val = np.sin(depth * 3.0) * 0.5 + 0.5
        return hsv_to_rgb(hue, np.full_like(val, 0.9), val)


class StarfieldWarpEffect:
    """Single-pixel starfield on a warp-speed run: stars stream outward from
    the center in straight radial lines, accelerating with distance, leaving
    short connected motion trails behind them (no rotation/spiraling — each
    star's angle is fixed for its whole life). Trails decay fairly quickly,
    so the field stays mostly black between stars."""

    SUBSTEPS = 6  # points drawn per star per frame, so fast-moving trails stay a connected line

    def __init__(self, n=140):
        self.n = n
        self.trail = np.zeros((HEIGHT, WIDTH, 3), dtype=np.float32)
        self.cx, self.cy = WIDTH / 2.0, HEIGHT / 2.0
        self.max_radius = math.hypot(self.cx, self.cy) + 8.0
        self.angle = np.random.rand(n).astype(np.float32) * math.tau
        self.radius = np.random.rand(n).astype(np.float32) * self.max_radius
        self.speed = 0.6 + np.random.rand(n).astype(np.float32) * 0.8
        self.hue_seed = np.random.rand(n).astype(np.float32)
        self.last_t = 0.0

    def render(self, t):
        dt = min(max(t - self.last_t, 0.0), 0.1)
        self.last_t = t

        old_radius = self.radius.copy()

        # warp speed breathes between a cruise and a hyperspace jump
        warp = 30.0 + 220.0 * (0.5 + 0.5 * math.sin(t * 0.15))
        frac = self.radius / self.max_radius
        self.radius += dt * self.speed * warp * (0.25 + frac)

        respawn = self.radius > self.max_radius
        n_respawn = int(respawn.sum())
        if n_respawn:
            self.radius[respawn] = np.random.rand(n_respawn).astype(np.float32) * 6.0
            self.angle[respawn] = np.random.rand(n_respawn).astype(np.float32) * math.tau
            self.hue_seed[respawn] = np.random.rand(n_respawn).astype(np.float32)
            old_radius[respawn] = self.radius[respawn]  # fresh spawn, no streak back in from the edge

        frac = self.radius / self.max_radius
        hue = (self.hue_seed + frac * 0.5 + t * 0.05) % 1.0
        colors = hsv_to_rgb(hue, np.full(self.n, 0.85, dtype=np.float32), np.ones(self.n, dtype=np.float32)) * 255.0
        colors = colors / self.SUBSTEPS

        self.trail *= 0.85
        steps = np.linspace(0.0, 1.0, self.SUBSTEPS, dtype=np.float32)
        radii = old_radius[:, None] + (self.radius - old_radius)[:, None] * steps[None, :]
        angles = np.repeat(self.angle[:, None], self.SUBSTEPS, axis=1)
        px = (self.cx + radii * np.cos(angles)).astype(np.int32).ravel()
        py = (self.cy + radii * np.sin(angles)).astype(np.int32).ravel()

        valid = (px >= 0) & (px < WIDTH) & (py >= 0) & (py < HEIGHT)
        flat_colors = np.repeat(colors, self.SUBSTEPS, axis=0)
        np.add.at(self.trail, (py[valid], px[valid]), flat_colors[valid])
        np.clip(self.trail, 0, 255, out=self.trail)
        return self.trail / 255.0


class StaticEffect:
    """TV static: the 4 hand-drawn noise tiles in staticbg.png (a 64x16 strip)
    are pooled into a palette of source pixels, then every frame the whole
    canvas is re-rolled at a fine grain — small GRAIN x GRAIN cells, each
    stamped with a random color from that palette. Real CRT snow reroots
    every pixel every frame; the earlier 16x16-block version rerolled
    sprite-sized chunks, which reads as a flashing/fake block pattern
    instead of texture. Fine grain + full reroll is what makes it read as
    static rather than mush."""

    GRAIN = 2  # canvas pixels per noise cell; 1 = true per-pixel snow
    SHEET_PATH = Path(__file__).resolve().parent.parent / "assets" / "titlescreen" / "staticbg.png"

    def __init__(self):
        sheet = pygame.image.load(str(self.SHEET_PATH)).convert_alpha()
        arr = pygame.surfarray.array3d(sheet).transpose(1, 0, 2)  # (h, w, 3)
        self.palette = arr.reshape(-1, 3).astype(np.float32) / 255.0  # (n_pixels, 3)

        self.grid_h = HEIGHT // self.GRAIN
        self.grid_w = WIDTH // self.GRAIN

    def render(self, t):
        idx = np.random.randint(0, self.palette.shape[0], size=(self.grid_h, self.grid_w))
        cells = self.palette[idx]  # (grid_h, grid_w, 3)
        return np.repeat(np.repeat(cells, self.GRAIN, axis=0), self.GRAIN, axis=1)


class ChromeFlameEffect:
    """Flames reflecting off a glossy black chrome surface. Not a literal
    fire — a mostly-black reflective field where fire-colored turbulence
    only shows through as streaky highlights, stretched vertically and
    rippled by a slow mirror-warp to read as a reflection on a curved
    polished surface rather than fire painted on the screen. A faint
    drifting cool-white sheen sweeps across independently, standing in for
    the chrome's own specular highlight."""

    def __init__(self):
        ys, xs = np.mgrid[0:HEIGHT, 0:WIDTH]
        self.xs = xs.astype(np.float32)
        self.ys = ys.astype(np.float32)
        self.u = self.xs / WIDTH
        self.v = self.ys / HEIGHT

    def render(self, t):
        xs, ys = self.xs, self.ys

        # low-frequency wobble simulating an imperfect, gently curved mirror
        warp_x = 6.0 * np.sin(ys * 0.05 + t * 0.6) + 3.0 * np.sin(ys * 0.13 - t * 0.9)
        wx = xs + warp_x

        # turbulence flame field; dividing ys by `stretch` before feeding it
        # into the noise elongates features vertically into streaks instead
        # of round blobs, like reflections smeared along brushed metal
        stretch = 3.5
        flame = (
            np.sin(wx * 0.09 + t * 2.0)
            + np.sin(wx * 0.15 - t * 1.3 + ys * 0.02)
            + np.sin(ys / stretch * 0.12 - t * 3.0)
            + np.sin((wx + ys / stretch) * 0.05 + t * 1.7)
        )
        flame = (flame + 4.0) / 8.0  # 0..1

        # brighter toward the bottom, as if the fire itself sits below frame
        # and only its reflection climbs the surface, fading as it rises
        vgrad = np.clip(self.v * 1.3 - 0.05, 0.0, 1.0) ** 0.8
        val = flame * vgrad

        # crush blacks hard: only turbulence peaks survive, keeping most of
        # the surface a true glossy black between the streaks
        val = np.clip(val * 1.6 - 0.55, 0.0, 1.0) ** 1.4

        # fire ramp: black -> deep red -> orange -> yellow -> white-hot
        r = np.clip(val * 3.2, 0.0, 1.0)
        g = np.clip(val * 3.2 - 1.0, 0.0, 1.0)
        b = np.clip(val * 3.2 - 2.2, 0.0, 1.0)

        # drifting cool specular sheen — the chrome's own highlight, independent of the flame color
        sheen_pos = (self.u * 0.6 + self.v * 0.4 + t * 0.08) % 1.0
        sheen = np.exp(-((sheen_pos - 0.5) ** 2) / 0.01) * 0.18
        r += sheen * 0.6
        g += sheen * 0.75
        b += sheen * 1.0

        return np.clip(np.stack([r, g, b], axis=-1), 0.0, 1.0)


class ChromeFlameVariantEffect(ChromeFlameEffect):
    """Mode 7 — the working variant of ChromeFlameEffect (mode 8, "fire"),
    kept as its own independent class so mode 8 stays an untouched reference.

    Differences from the base:
      - Rendered at a chunky low-res grid (PIXEL x PIXEL blocks) and nearest-
        upscaled, for a pixelated look instead of smooth gradients.
      - A persistent `chaos` state random-walks over time (rather than being
        a pure function of t), so the pattern genuinely never repeats — real
        flicker instead of an obviously looping animation.
      - A second, decorrelated turbulence field is thresholded into a hard
        on/off mask multiplied over the flame, so lit patches pop in and out
        irregularly instead of one smooth blob breathing in place.
      - An ember particle system spawns off the brightest bottom-row columns
        of the flame field, rises with a wobble, cools from white-hot to red,
        and fades — actual flakes coming off the fire, not just texture.
      - The specular sheen is now 3 independent short flash-bursts (sin
        envelope, staggered spawn, fast diagonal sweep) instead of one slow
        continuous band.
    """

    PIXEL = 4  # chunk size in canvas pixels; must evenly divide both WIDTH and HEIGHT
    N_EMBERS = 70
    N_SHEENS = 3

    def __init__(self):
        self.gh = HEIGHT // self.PIXEL
        self.gw = WIDTH // self.PIXEL
        gys, gxs = np.mgrid[0:self.gh, 0:self.gw]
        # real canvas-space coordinates of each low-res block's center, so
        # the turbulence frequencies read the same as the full-res version
        self.bx = (gxs * self.PIXEL + self.PIXEL / 2).astype(np.float32)
        self.by = (gys * self.PIXEL + self.PIXEL / 2).astype(np.float32)
        self.gu = self.bx / WIDTH
        self.gv = self.by / HEIGHT

        self.last_t = 0.0
        self.chaos = np.zeros(3, dtype=np.float32)

        self.ember_x = np.random.uniform(0, self.gw, self.N_EMBERS).astype(np.float32)
        self.ember_y = np.random.uniform(0, self.gh, self.N_EMBERS).astype(np.float32)
        self.ember_age = np.random.uniform(0, 1, self.N_EMBERS).astype(np.float32)
        self.ember_life = np.random.uniform(0.5, 1.6, self.N_EMBERS).astype(np.float32)
        self.ember_phase = np.random.uniform(0, 1, self.N_EMBERS).astype(np.float32)
        self.ember_speed = np.random.uniform(8.0, 20.0, self.N_EMBERS).astype(np.float32)
        self.ember_trail = np.zeros((self.gh, self.gw, 3), dtype=np.float32)

        # each burst always travels fully off-screen to off-screen; duration
        # is derived from that distance and speed so the flash never gets
        # cut off mid-sweep -- it only fades via the envelope at the ends,
        # which are already past the visible edges
        start0 = np.random.uniform(-0.25, -0.05, self.N_SHEENS).astype(np.float32)
        end0 = np.random.uniform(1.05, 1.25, self.N_SHEENS).astype(np.float32)
        self.sheen_pos0 = start0
        self.sheen_speed = np.random.uniform(2.0, 3.5, self.N_SHEENS).astype(np.float32)
        self.sheen_dur = (end0 - start0) / self.sheen_speed
        self.sheen_start = np.full(self.N_SHEENS, -10.0, dtype=np.float32)  # forces an immediate spawn

    def render(self, t):
        dt = min(max(t - self.last_t, 0.0), 0.1)
        self.last_t = t

        # slow random walk, not a function of t alone -- keeps the pattern
        # from ever settling into an obviously repeating loop
        self.chaos += np.random.uniform(-1.0, 1.0, size=3).astype(np.float32) * dt * 1.5
        self.chaos = np.clip(self.chaos, -6.0, 6.0)
        c0, c1, c2 = self.chaos

        bx, by = self.bx, self.by

        # mirror warp, chained twice for a less "clean sine" ripple
        warp1 = 5.0 * np.sin(by * 0.05 + t * 0.6 + c0) + 3.0 * np.sin(by * 0.13 - t * 0.9 + c1)
        wx = bx + warp1
        wx = wx + 3.0 * np.sin(wx * 0.07 + t * 1.1 - c2)

        stretch = 3.0
        flame = (
            np.sin(wx * 0.10 + t * 2.2 + c0)
            + np.sin(wx * 0.17 - t * 1.4 + by * 0.03 + c1)
            + np.sin(by / stretch * 0.14 - t * 3.2)
            + np.sin((wx + by / stretch) * 0.06 + t * 1.9 - c2)
        )
        flame = (flame + 4.0) / 8.0  # 0..1

        # a second, decorrelated field used purely as a hard on/off mask, so
        # lit patches pop irregularly instead of one shape breathing smoothly
        mask_field = (
            np.sin(bx * 0.08 - t * 1.7 + c1)
            + np.sin(by * 0.11 + t * 2.3 + c2)
            + np.sin((bx - by) * 0.05 + t * 0.8)
        )
        mask = (mask_field > 0.15).astype(np.float32)
        flame = flame * (0.35 + 0.65 * mask)

        vgrad = np.clip(self.gv * 1.3 - 0.05, 0.0, 1.0) ** 0.8
        val = flame * vgrad
        val = np.clip(val * 1.7 - 0.55, 0.0, 1.0) ** 1.3

        r = np.clip(val * 3.2, 0.0, 1.0)
        g = np.clip(val * 3.2 - 1.0, 0.0, 1.0)
        b = np.clip(val * 3.2 - 2.2, 0.0, 1.0)
        rgb = np.stack([r, g, b], axis=-1)

        # -- embers: flake off the brightest columns near the bottom, rise, cool, fade --
        self.ember_age += dt
        respawn = self.ember_age > self.ember_life
        n_re = int(respawn.sum())
        if n_re:
            bottom_weight = flame[-4:, :].mean(axis=0) + 0.02
            bottom_weight = bottom_weight / bottom_weight.sum()
            cols = np.random.choice(self.gw, size=n_re, p=bottom_weight)
            self.ember_x[respawn] = cols.astype(np.float32) + np.random.uniform(-0.5, 0.5, n_re)
            self.ember_y[respawn] = self.gh - 1.0 - np.random.uniform(0.0, 2.0, n_re)
            self.ember_age[respawn] = 0.0
            self.ember_life[respawn] = np.random.uniform(0.5, 1.6, n_re)
            self.ember_speed[respawn] = np.random.uniform(8.0, 20.0, n_re)
            self.ember_phase[respawn] = np.random.uniform(0.0, 1.0, n_re)

        self.ember_y -= self.ember_speed * dt
        self.ember_x += np.sin(t * 2.2 + self.ember_phase * math.tau) * dt * 3.0

        life_frac = np.clip(1.0 - self.ember_age / self.ember_life, 0.0, 1.0)
        er = np.ones(self.N_EMBERS, dtype=np.float32)
        eg = np.clip(life_frac * 1.6, 0.0, 1.0)
        eb = np.clip(life_frac * 1.6 - 0.6, 0.0, 1.0)
        ecolor = np.stack([er, eg, eb], axis=-1) * life_frac[:, None]

        self.ember_trail *= 0.72  # fast decay -> small twinkly points, not long streaks
        onscreen = (self.ember_y >= 0) & (self.ember_y < self.gh)
        xi = np.clip(self.ember_x.astype(np.int32), 0, self.gw - 1)
        yi = np.clip(self.ember_y.astype(np.int32), 0, self.gh - 1)
        np.add.at(self.ember_trail, (yi[onscreen], xi[onscreen]), ecolor[onscreen])
        np.clip(self.ember_trail, 0.0, 3.0, out=self.ember_trail)

        rgb = rgb + self.ember_trail

        # -- specular sheen: 2-3 short fast flash-bursts instead of one slow continuous band --
        age = t - self.sheen_start
        respawn_sheen = age > self.sheen_dur
        n_res = int(respawn_sheen.sum())
        if n_res:
            self.sheen_start[respawn_sheen] = t + np.random.uniform(4.0, 9.0, n_res)
            start0 = np.random.uniform(-0.25, -0.05, n_res)
            end0 = np.random.uniform(1.05, 1.25, n_res)
            self.sheen_pos0[respawn_sheen] = start0
            self.sheen_speed[respawn_sheen] = np.random.uniform(2.0, 3.5, n_res)
            self.sheen_dur[respawn_sheen] = (end0 - start0) / self.sheen_speed[respawn_sheen]

        age = np.clip(t - self.sheen_start, 0.0, None)
        active = age < self.sheen_dur
        frac = np.clip(age / np.maximum(self.sheen_dur, 1e-6), 0.0, 1.0)
        envelope = np.sin(np.pi * frac) * active

        diag = self.gu * 0.6 + self.gv * 0.4
        sheen_total = np.zeros((self.gh, self.gw), dtype=np.float32)
        for i in range(self.N_SHEENS):
            pos = self.sheen_pos0[i] + self.sheen_speed[i] * age[i]
            sheen_total += np.exp(-((diag - pos) ** 2) / 0.004) * envelope[i]
        sheen_total = np.clip(sheen_total, 0.0, 1.0) * 0.35

        rgb[..., 0] += sheen_total * 0.65
        rgb[..., 1] += sheen_total * 0.8
        rgb[..., 2] += sheen_total * 1.0

        rgb = np.clip(rgb, 0.0, 1.0)
        return np.repeat(np.repeat(rgb, self.PIXEL, axis=0), self.PIXEL, axis=1)


class ChromeFlameSymbolMaskEffect(ChromeFlameVariantEffect):
    """Mode 9, step 1 of 2: mode 7's chrome-flame sim (flame/embers/sheen,
    unchanged), multiplied by the alpha channel of gaidensymbols.png so only
    pixels belonging to a symbol glyph show any color. Symbol art is 256x224
    against a 256x240 canvas, so it's centered vertically (8px letterboxed
    top and bottom) with an exact width match.

    Step 1 only: this run loop draws one effect per frame onto an opaque
    canvas, so "everywhere else" is rendered plain black, not real alpha
    transparency. Compositing this on top of a second running visualizer
    mode is step 2 — that needs the run loop itself to layer two effects,
    which hasn't been built yet.
    """

    SYMBOLS_PATH = Path(__file__).resolve().parent.parent / "assets" / "titlescreen" / "gaidensymbols.png"

    def __init__(self):
        super().__init__()
        sheet = pygame.image.load(str(self.SYMBOLS_PATH)).convert_alpha()
        alpha = pygame.surfarray.array_alpha(sheet).transpose(1, 0)  # (h, w)
        sh, sw = alpha.shape

        mask = np.zeros((HEIGHT, WIDTH), dtype=np.float32)
        y0 = max((HEIGHT - sh) // 2, 0)
        x0 = max((WIDTH - sw) // 2, 0)
        y1, x1 = min(y0 + sh, HEIGHT), min(x0 + sw, WIDTH)
        mask[y0:y1, x0:x1] = alpha[: y1 - y0, : x1 - x0].astype(np.float32) / 255.0
        self.mask = mask[:, :, None]  # (H, W, 1), broadcasts over RGB

    def render(self, t):
        frame = super().render(t)
        return frame * self.mask


class TitleScreenV1Effect:
    """Mode 10, step 2: mode 9 (chrome-flame masked to gaidensymbols.png)
    composited over mode 6 (TV static) as the background layer showing
    through everywhere the symbol mask is transparent.

    ChromeFlameSymbolMaskEffect.render() already returns raw_flame * mask
    (zero outside the glyphs, with fractional alpha at anti-aliased edges),
    so compositing is just fg + bg * (1 - mask) — standard alpha-over.

    Both layers fade in from black on their own independent timers, starting
    at t=0: the static reaches full brightness at STATIC_FADE_DURATION, the
    fire-in-symbols layer at SYMBOLS_FADE_DURATION. The symbol-shaped hole
    cut out of the static is NOT faded — it's always fully cut, regardless
    of how faded the fire showing through it currently is — so the glyph
    shapes read immediately while their fire content brightens in over time.
    """

    STATIC_FADE_DURATION = 4.0
    SYMBOLS_FADE_DURATION = 6.0

    def __init__(self):
        self.bg = StaticEffect()
        self.fg = ChromeFlameSymbolMaskEffect()

    def render(self, t):
        bg = self.bg.render(t) * smoothstep(t / self.STATIC_FADE_DURATION)
        fg = self.fg.render(t) * smoothstep(t / self.SYMBOLS_FADE_DURATION)
        return fg + bg * (1.0 - self.fg.mask)


class TitleIntroEffect:
    """Mode 11: scripted title-screen intro sequence — two image layers,
    timed and composited by hand, on top of mode 10 (titlescreenv1) running
    live underneath for the whole sequence, from t=0 onward.

      t=0..4s        mode 10 alone (static + chrome-flame symbols)
      t=4s           fronthouse.png starts sliding down from off-screen-top
                      into its centered resting spot, its RGB simultaneously
                      ramping from black to full color (its own alpha stays
                      fully opaque throughout — this is a color fade, not an
                      opacity fade, per the "fades from all black to full
                      colour" spec)
      t=4+SLIDE_DURATION      fronthouse finishes sliding/coloring
      +GAIDEN_DELAY (3s)      gaiden.png starts a straight 1s opacity fade-in
                              (alpha-only, no slide), centered in the same spot

    SLIDE_DURATION wasn't specified — defaulted to 2.0s here, eased with a
    smoothstep so the slide isn't robotic-linear. Tune the class constants
    below; timings aren't derived from anything else in the file.
    """

    SLIDE_START = 4.0
    SLIDE_DURATION = 2.0
    GAIDEN_DELAY = 3.0
    GAIDEN_FADE_DURATION = 1.0

    FRONTHOUSE_PATH = Path(__file__).resolve().parent.parent / "assets" / "titlescreen" / "fronthouse.png"
    GAIDEN_PATH = Path(__file__).resolve().parent.parent / "assets" / "titlescreen" / "gaiden.png"

    def __init__(self):
        self.bg = TitleScreenV1Effect()
        self.fh_rgb, self.fh_alpha = self._load_rgba(self.FRONTHOUSE_PATH)
        self.gd_rgb, self.gd_alpha = self._load_rgba(self.GAIDEN_PATH)

        fh_h, fh_w = self.fh_alpha.shape
        self.fh_x0 = (WIDTH - fh_w) // 2
        self.fh_y_end = (HEIGHT - fh_h) // 2
        self.fh_y_start = -fh_h  # fully off-screen above, bottom edge at canvas top

        gd_h, gd_w = self.gd_alpha.shape
        self.gd_x0 = (WIDTH - gd_w) // 2
        self.gd_y0 = (HEIGHT - gd_h) // 2

        self.gaiden_start = self.SLIDE_START + self.SLIDE_DURATION + self.GAIDEN_DELAY

    @staticmethod
    def _load_rgba(path):
        sheet = pygame.image.load(str(path)).convert_alpha()
        rgb = pygame.surfarray.array3d(sheet).transpose(1, 0, 2).astype(np.float32) / 255.0
        alpha = pygame.surfarray.array_alpha(sheet).transpose(1, 0).astype(np.float32) / 255.0
        return rgb, alpha

    @staticmethod
    def _composite(dst, src_rgb, src_alpha, x0, y0):
        """Alpha-blend src onto dst at (x0, y0), clipping to dst's bounds —
        x0/y0 may be negative or push the source partway off any edge."""
        sh, sw = src_alpha.shape
        x0, y0 = int(round(x0)), int(round(y0))
        dst_y0, dst_y1 = max(y0, 0), min(y0 + sh, HEIGHT)
        dst_x0, dst_x1 = max(x0, 0), min(x0 + sw, WIDTH)
        if dst_y0 >= dst_y1 or dst_x0 >= dst_x1:
            return
        src_y0, src_y1 = dst_y0 - y0, dst_y1 - y0
        src_x0, src_x1 = dst_x0 - x0, dst_x1 - x0

        a = src_alpha[src_y0:src_y1, src_x0:src_x1, None]
        rgb = src_rgb[src_y0:src_y1, src_x0:src_x1, :]
        region = dst[dst_y0:dst_y1, dst_x0:dst_x1, :]
        dst[dst_y0:dst_y1, dst_x0:dst_x1, :] = rgb * a + region * (1.0 - a)

    def render(self, t):
        frame = self.bg.render(t)

        if t >= self.SLIDE_START:
            eased = smoothstep((t - self.SLIDE_START) / self.SLIDE_DURATION)
            y = self.fh_y_start + (self.fh_y_end - self.fh_y_start) * eased
            self._composite(frame, self.fh_rgb * eased, self.fh_alpha, self.fh_x0, y)

        if t >= self.gaiden_start:
            prog = np.clip((t - self.gaiden_start) / self.GAIDEN_FADE_DURATION, 0.0, 1.0)
            self._composite(frame, self.gd_rgb, self.gd_alpha * prog, self.gd_x0, self.gd_y0)

        return np.clip(frame, 0.0, 1.0)


class KaleidoscopeEffect:
    """Mode 12: a real kaleidoscope, not a rainbow-noise fill.

    Radial symmetry comes from folding the angle around the center into
    N_WEDGES repeating slices and then mirroring within each slice (the
    classic kaleidoscope-mirror technique) — the same handful of features
    just gets reflected around, rather than the whole canvas being filled
    independently.

    Color is deliberately restrained: a duotone pair of tertiary hues a
    fixed 60 degrees apart (HUE_SPAN) — not a full hue sweep — with the
    pattern blending smoothly between just those two anchors. The pair
    drifts together through the wheel (one full rotation per
    HUE_DRIFT_PERIOD), so the palette itself evolves but never jumps or
    flashes between unrelated hues. Saturation and value are both kept
    mid-range for the same reason — no pure-white flares, no max-saturation
    neon. A small per-block hue jitter, re-rolled every frame, adds sparkle
    without turning it into a rainbow.

    Rendered at a chunky PIXELxPIXEL grid and nearest-upscaled, for a
    pixelated look instead of smooth gradients.
    """

    N_WEDGES = 8
    HUE_SPAN = 60.0 / 360.0     # duotone anchor spacing stays fixed at a tertiary-scale interval
    HUE_DRIFT_PERIOD = 90.0     # seconds for one full rotation of the whole duotone through the wheel
    HUE_JITTER = 0.008          # max per-block random hue offset, re-rolled every frame
    ROTATE_SPEED = 0.09         # overall pattern rotation, radians/sec-ish
    PIXEL = 4                   # chunk size in canvas pixels; must evenly divide both WIDTH and HEIGHT

    def __init__(self):
        self.gh = HEIGHT // self.PIXEL
        self.gw = WIDTH // self.PIXEL
        gys, gxs = np.mgrid[0:self.gh, 0:self.gw]
        bx = (gxs * self.PIXEL + self.PIXEL / 2).astype(np.float32)
        by = (gys * self.PIXEL + self.PIXEL / 2).astype(np.float32)
        cx, cy = WIDTH / 2.0, HEIGHT / 2.0
        dx, dy = bx - cx, by - cy
        self.radius = np.sqrt(dx * dx + dy * dy)
        self.angle = np.arctan2(dy, dx)

    def render(self, t):
        wedge = math.tau / self.N_WEDGES
        a = np.mod(self.angle + t * self.ROTATE_SPEED, wedge)
        a = np.abs(a - wedge / 2.0)  # mirror-fold each wedge -> 2x N_WEDGES-fold symmetry

        r = self.radius
        v = (
            np.sin(a * 9.0 + r * 0.045 - t * 0.35)
            + np.sin(r * 0.08 - a * 6.0 + t * 0.22)
            + np.sin((a * 13.0 + r * 0.03) - t * 0.12)
        )
        v = (v + 3.0) / 6.0  # 0..1 structured pattern, no per-pixel randomness

        hue_offset = (t / self.HUE_DRIFT_PERIOD) % 1.0
        mix = 0.5 + 0.5 * np.cos(v * math.pi + a * 2.0)  # smooth blend between the two duotone anchors
        jitter = np.random.uniform(-self.HUE_JITTER, self.HUE_JITTER, size=v.shape).astype(np.float32)
        hue = (hue_offset + mix * self.HUE_SPAN + jitter) % 1.0

        sat = 0.5 + 0.12 * v
        val = 0.22 + 0.62 * v

        rgb = hsv_to_rgb(hue, sat, val)
        return np.repeat(np.repeat(rgb, self.PIXEL, axis=0), self.PIXEL, axis=1)


EFFECTS = {
    "1": ("plasma", PlasmaEffect),
    "2": ("ripples", RipplesEffect),
    "3": ("swarm", SwarmEffect),
    "4": ("tunnel", TunnelEffect),
    "5": ("starfield", StarfieldWarpEffect),
    "6": ("static", StaticEffect),
    "7": ("chromeflame", ChromeFlameVariantEffect),
    "8": ("fire", ChromeFlameEffect),
    "9": ("chromeflame-symbols", ChromeFlameSymbolMaskEffect),
    "10": ("titlescreenv1", TitleScreenV1Effect),
    "11": ("titleintro", TitleIntroEffect),
    "12": ("kaleidoscope", KaleidoscopeEffect),
}


def prompt_mode():
    print("Select visualization:")
    print("  1) plasma          (sum-of-sines plasma with drifting hue field)")
    print("  2) ripples         (circular wave interference)")
    print("  3) swarm           (Lissajous particle swarm with trails)")
    print("  4) tunnel          (color tunnel warp)")
    print("  5) starfield warp  (straight-line warp-speed starfield)")
    print("  6) static          (TV static from titlescreen noise tiles)")
    print("  7) chromeflame     (flames reflecting off black chrome — working variant)")
    print("  8) fire            (flames reflecting off black chrome — reference)")
    print("  9) chromeflame-symbols (mode 7 masked to gaidensymbols.png glyphs, black elsewhere for now)")
    print("  10) titlescreenv1  (mode 9 composited over mode 6 static)")
    print("  11) titleintro     (scripted fronthouse/gaiden intro sequence)")
    print("  12) kaleidoscope   (mirrored radial symmetry, restrained tertiary duotone)")
    choice = input("> ").strip()
    return EFFECTS.get(choice, EFFECTS["1"])


def run_effect(screen, canvas, clock, effect):
    t0 = time.perf_counter()

    running = True
    while running:
        for event in pygame.event.get():
            if event.type == pygame.QUIT:
                running = False
            elif event.type == pygame.KEYDOWN and event.key == pygame.K_ESCAPE:
                running = False

        t = time.perf_counter() - t0
        rgb = effect.render(t)
        frame = (np.clip(rgb, 0.0, 1.0) * 255).astype(np.uint8)
        pygame.surfarray.blit_array(canvas, frame.transpose(1, 0, 2))
        pygame.transform.scale(canvas, (WIDTH * SCALE, HEIGHT * SCALE), screen)
        pygame.display.flip()
        clock.tick(FPS)


def main():
    pygame.init()
    name, effect_cls = prompt_mode()

    pygame.display.set_caption(f"visualizer - {name}")
    screen = pygame.display.set_mode((WIDTH * SCALE, HEIGHT * SCALE))
    canvas = pygame.Surface((WIDTH, HEIGHT))
    clock = pygame.time.Clock()

    run_effect(screen, canvas, clock, effect_cls())

    pygame.quit()
    sys.exit()


if __name__ == "__main__":
    main()
