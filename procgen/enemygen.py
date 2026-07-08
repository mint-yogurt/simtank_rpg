"""Deterministic enemy generation for simtank_rpg.

Given a seed, produces a list of enemies with names, combat stats, and an NPC
sprite key. Stat ranges match the Fighter dataclass in engine/combat.py.
"""

import random

from procgen import names

# NPC sprite base names from web/static/sprites/partysprites.txt.
# Each has _S1 and _S2 animation frames (350ms per frame, looping).
_NPC_SPRITES = ["npc01", "npc02", "npc03", "npc04"]


def generate_enemies(seed: int, count: int, level: int = 1) -> list[dict]:
    """Return a list of `count` enemy dicts, fully determined by `seed`.

    Each dict has keys matching the enemies DB columns:
        name, iq, weight, sweat, hair, level, npc_sprite

    `level` is the base level; each enemy gets ±1 variance.
    Stat ranges match combat.py: IQ 40-200, WEIGHT 0-900, SWEAT 0-10, HAIR 0-100.
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

        npc_sprite    = e_rng.choice(_NPC_SPRITES)
        behavior_type = e_rng.randint(1, 3)
        behavior_axis = e_rng.choice(['H', 'V']) if behavior_type == 2 else None

        name = names.generate(rng=e_rng).upper()

        enemies.append({
            "name":          name,
            "iq":            iq,
            "weight":        weight,
            "sweat":         sweat,
            "hair":          hair,
            "level":         enemy_level,
            "npc_sprite":    npc_sprite,
            "behavior_type": behavior_type,
            "behavior_axis": behavior_axis,
        })
    return enemies
