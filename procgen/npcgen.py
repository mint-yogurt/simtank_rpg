"""Deterministic town NPC generation for simtank_rpg.

Generates a pool of friendly NPCs for a town interior: one fixed healer plus
a random mix of wanderers (type 1), pacers (type 2), and statics (type 3).
Reuses the sprite/palette infrastructure from enemygen.
"""

import random

from procgen.enemygen import _NPC_SPRITES, _pick_sprite_palette
from procgen.names import generate as gen_name


def generate_town_npcs(seed: int, count: int) -> list[dict]:
    """Return `count` NPC dicts fully determined by `seed`.

    Index 0 is always the healer (behavior_type=3, name='HEALER', fixed position).
    Indices 1..count-1 are randomly typed 1/2/3 with random palettes.

    Dict keys match the enemies DB columns:
        name, iq, weight, sweat, hair, level,
        npc_sprite, sprite_palette, overworld_sprite,
        behavior_type, behavior_axis
    """
    rng = random.Random(seed)
    npcs = []

    # Index 0: healer — always static (type 3), placed at healerHutwallMid
    h_rng = random.Random(rng.getrandbits(64))
    sprite = h_rng.choice(_NPC_SPRITES)
    npcs.append({
        "name":             "HEALER",
        "iq":               h_rng.randint(40, 160),
        "weight":           h_rng.randint(80, 600),
        "sweat":            h_rng.randint(1, 8),
        "hair":             h_rng.randint(0, 80),
        "level":            1,
        "npc_sprite":       sprite,
        "sprite_palette":   _pick_sprite_palette(h_rng, sprite),
        "overworld_sprite": None,
        "behavior_type":    3,
        "behavior_axis":    None,
    })

    # Indices 1+: random types 1/2/3, placed on walkable tiles
    for _ in range(count - 1):
        e_rng = random.Random(rng.getrandbits(64))
        sprite = e_rng.choice(_NPC_SPRITES)
        btype = e_rng.randint(1, 3)
        baxis = e_rng.choice(['H', 'V']) if btype == 2 else None
        npcs.append({
            "name":             gen_name(rng=e_rng).upper(),
            "iq":               e_rng.randint(40, 160),
            "weight":           e_rng.randint(80, 600),
            "sweat":            e_rng.randint(1, 8),
            "hair":             e_rng.randint(0, 80),
            "level":            1,
            "npc_sprite":       sprite,
            "sprite_palette":   _pick_sprite_palette(e_rng, sprite),
            "overworld_sprite": None,
            "behavior_type":    btype,
            "behavior_axis":    baxis,
        })

    return npcs
