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

import numpy as np
import pygame

# ── CONFIG ────────────────────────────────────────────────────────────────────
WIDTH, HEIGHT = 256, 240
SCALE = 3
FPS = 60


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


EFFECTS = {
    "1": ("plasma", PlasmaEffect),
    "2": ("ripples", RipplesEffect),
    "3": ("swarm", SwarmEffect),
    "4": ("tunnel", TunnelEffect),
    "5": ("starfield", StarfieldWarpEffect),
}


def prompt_mode():
    print("Select visualization:")
    print("  1) plasma          (sum-of-sines plasma with drifting hue field)")
    print("  2) ripples         (circular wave interference)")
    print("  3) swarm           (Lissajous particle swarm with trails)")
    print("  4) tunnel          (color tunnel warp)")
    print("  5) starfield warp  (straight-line warp-speed starfield)")
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
