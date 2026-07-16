"""Deterministic enemy generation for simtank_rpg.

Given a seed, produces a list of enemies with names, combat stats, and an NPC
sprite key. Stat ranges match the Fighter dataclass in engine/battle.py.
"""

import random

from procgen import names

# NES palette grid (rows 0-3, cols 0-12) — same as worldgen/towngen/cavegen.
_NES_PALETTE = [
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


# NPC sprite base names from web/static/sprites/partysprites.txt.
# Each has _S1 and _S2 animation frames (350ms per frame, looping).
_NPC_SPRITES = ["npc01", "npc02", "npc03", "npc04", "npc05", "npc06", "npc07", "npc08"]

# Overworld-exclusive visual sprites (overworld map only; battle + caves use npc_sprite).
_OVERWORLD_SPRITES = ["enemy_overworld1", "enemy_overworld2", "enemy_overworld3"]

# Placeholder pixel colors to swap per NPC sprite (from partysprites.txt comments).
# Each list entry is an (r, g, b) tuple matching a pixel color in the source PNG.
NPC_PLACEHOLDER_COLORS = {
    "npc01": [(0xff, 0x83, 0xc1), (0x3f, 0x51, 0xcf), (0xc0, 0x35, 0x70)],
    "npc02": [(0xc0, 0x35, 0x70), (0xff, 0x83, 0xc1)],
    "npc03": [(0xff, 0xcf, 0xc8), (0x3f, 0x51, 0xcf)],
    "npc04": [(0x69, 0x69, 0x69), (0x59, 0x56, 0x52)],
    "npc05": [(0x5f, 0x0b, 0x61)],
    "npc06": [(0xc0, 0x35, 0x70), (0xff, 0x83, 0xc1)],
    "npc07": [(0x3f, 0x51, 0xcf)],
    "npc08": [(0xff, 0xcf, 0xc8), (0xc0, 0x35, 0x70)],
}


def _pick_sprite_palette(rng, npc_key):
    """Pick N non-adjacent NES colors for a sprite's N placeholder slots."""
    n = len(NPC_PLACEHOLDER_COLORS.get(npc_key, []))
    if n == 0:
        return []
    picked = []
    while len(picked) < n:
        r, c = rng.randrange(4), rng.randrange(13)
        if all(abs(r - p[0]) + abs(c - p[1]) > 1 for p in picked):
            picked.append((r, c))
    return [list(_hex_rgb(_NES_PALETTE[r][c])) for r, c in picked]


def generate_enemies(seed: int, count: int, level: int = 1,
                     allow_overworld_sprite: bool = False) -> list[dict]:
    """Return a list of `count` enemy dicts, fully determined by `seed`.

    Each dict has keys matching the enemies DB columns:
        name, iq, weight, sweat, hair, level, npc_sprite,
        sprite_palette, overworld_sprite, behavior_type, behavior_axis

    `level` is the base level; each enemy gets ±1 variance.
    Stat ranges match engine/battle.py: IQ 40-200, WEIGHT 0-900, SWEAT 0-10, HAIR 0-100.
    `allow_overworld_sprite`: when True (overworld enemies), enemy also gets an
        overworld_sprite for the map view; battle and cave use npc_sprite.
    """
    rng = random.Random(seed)
    enemies = []
    for _ in range(count):
        e_seed = rng.getrandbits(64)
        e_rng = random.Random(e_seed)

        enemy_level = max(1, level + e_rng.randint(-1, 1))
        iq     = e_rng.randint(40, 160)
        weight = e_rng.randint(80, 600)
        sweat  = e_rng.randint(1, 8)
        hair   = e_rng.randint(0, 80)

        npc_sprite       = e_rng.choice(_NPC_SPRITES)
        sprite_palette   = _pick_sprite_palette(e_rng, npc_sprite)
        overworld_sprite = e_rng.choice(_OVERWORLD_SPRITES) if allow_overworld_sprite else None
        behavior_type    = e_rng.randint(1, 3)
        behavior_axis    = e_rng.choice(['H', 'V']) if behavior_type == 2 else None

        name = names.generate(rng=e_rng).upper()

        enemies.append({
            "name":             name,
            "iq":               iq,
            "weight":           weight,
            "sweat":            sweat,
            "hair":             hair,
            "level":            enemy_level,
            "npc_sprite":       npc_sprite,
            "sprite_palette":   sprite_palette,
            "overworld_sprite": overworld_sprite,
            "behavior_type":    behavior_type,
            "behavior_axis":    behavior_axis,
        })
    return enemies
