"""Combat resolver for simtank_rpg — standalone, tunable, watchable.

All randomness is percentage-based (dice dropped for now; any check can be
re-expressed as sixths later for animated d6). Every knob is a CONSTANT up top.
Run `python combat.py` to watch a fake 4-on-1 fight print out.

Attack chain:
  1. to-hit      (SWEAT + level)          -> miss wastes the turn
  2. crit check  (attacker level)          -> flags +bonus damage
  3. parry check (defender level, rare)    -> reflect half, defender takes none
  4. saving throw(multi-stat contest)      -> glance (tiny dmg) vs full
  5. damage      (WEIGHT + level - resist)

Stats never change; LVL/HP/XP do. Damage scales with level ("dinging").
"""

import random
from dataclasses import dataclass, field

# =============================================================================
# STAT NORMALIZATION  --  reference ranges. raw stat -> 0..1 fraction.
# =============================================================================
IQ_RANGE = (40, 200)
WEIGHT_RANGE = (0, 900)
SWEAT_RANGE = (0, 10)
HAIR_RANGE = (0, 100)


def _frac(value, lo_hi):
    lo, hi = lo_hi
    return max(0.0, min(1.0, (value - lo) / (hi - lo)))


# =============================================================================
# KNOBS
# =============================================================================
# --- to-hit --------------------------------------------------
HIT_BASE = 0.45
HIT_SWEAT = 0.30         # SWEAT fraction's contribution
HIT_LEVEL = 0.004        # per level above 1
HIT_MIN, HIT_MAX = 0.10, 0.95

# --- damage --------------------------------------------------
DMG_BASE = 2.0
DMG_WEIGHT = 4.0         # WEIGHT fraction's contribution
GROWTH = 0.10            # dinging: dmg *= 1 + (level-1)*GROWTH   (lvl60 ~ 6.9x)
RESIST = 2.0             # defender SWEAT fraction soaks this much
CRIT_BONUS = 0.15        # +15% on a crit
GLANCE_CHIP = 1          # flat damage a glancing blow deals

# --- crit & parry (scale with LEVEL only) --------------------
CRIT_START = 0.08
PARRY_START = 0.08
CRITPARRY_CEIL = 0.35    # chance at MAX_LEVEL
MAX_LEVEL = 60
_critparry_step = (CRITPARRY_CEIL - CRIT_START) / (MAX_LEVEL - 1)

# --- saving throw (glance) : multi-stat defender vs attacker --
# defender power = weighted sum of normalized stats. tune who defends well.
SAVE_W_IQ = 0.50         # wits -> dodge (Billy shines here)
SAVE_W_WEIGHT = 0.30     # bulk -> hard to shift
SAVE_W_SWEAT = 0.20      # grit
SAVE_LEVEL = 0.01
SAVE_BASE = 0.25
SAVE_SPREAD = 0.60       # how much the power gap swings the chance
SAVE_MIN, SAVE_MAX = 0.05, 0.75

# --- defend action -------------------------------------------
DEF_BASE = 0.20
DEF_SWEAT = 0.30
DEF_LEVEL = 0.004
DEF_CAP = 0.75

# --- run away (party-wide, escalating) -----------------------
RUN_BASE = 0.15
RUN_STEP = 0.15          # per failed attempt by anyone

# --- hp ------------------------------------------------------
PARTY_HP_BASE = 25
PARTY_HP_PER_LEVEL = 2
ENEMY_HP_BASE = 14
ENEMY_HP_WEIGHT = 12     # + WEIGHT_frac * this
ENEMY_HP_PER_LEVEL = 3

# --- mp ------------------------------------------------------
PARTY_MP_BASE = 20
PARTY_MP_PER_LEVEL = 1

# MP cost per special move name (used by both battle loop and prompt builder)
SPECIAL_MP_COSTS: dict[str, int] = {
    "SING":   8,
    "LAUGH":  7,
    "SNACK":  6,
    "TICKLE": 5,
}


# =============================================================================
# ENTITIES
# =============================================================================
@dataclass
class Fighter:
    name: str
    iq: int
    weight: int
    sweat: int
    hair: int
    level: int = 1
    is_enemy: bool = False
    hp: int = field(default=0)
    max_hp: int = field(default=0)
    mp: int = field(default=0)
    max_mp: int = field(default=0)
    defending: bool = False

    def __post_init__(self):
        if self.max_hp == 0:
            if self.is_enemy:
                self.max_hp = round(
                    ENEMY_HP_BASE
                    + _frac(self.weight, WEIGHT_RANGE) * ENEMY_HP_WEIGHT
                    + (self.level - 1) * ENEMY_HP_PER_LEVEL
                )
            else:
                self.max_hp = PARTY_HP_BASE + (self.level - 1) * PARTY_HP_PER_LEVEL
            self.hp = self.max_hp
        if not self.is_enemy and self.max_mp == 0:
            self.max_mp = PARTY_MP_BASE + (self.level - 1) * PARTY_MP_PER_LEVEL
            self.mp = self.max_mp

    @property
    def alive(self):
        return self.hp > 0


# =============================================================================
# FORMULAS
# =============================================================================
def hit_chance(attacker):
    c = HIT_BASE + _frac(attacker.sweat, SWEAT_RANGE) * HIT_SWEAT \
        + (attacker.level - 1) * HIT_LEVEL
    return max(HIT_MIN, min(HIT_MAX, c))


def crit_chance(level):
    return CRIT_START + (level - 1) * _critparry_step


def parry_chance(level):
    return PARRY_START + (level - 1) * _critparry_step


def save_chance(defender, attacker):
    d_power = (_frac(defender.iq, IQ_RANGE) * SAVE_W_IQ
               + _frac(defender.weight, WEIGHT_RANGE) * SAVE_W_WEIGHT
               + _frac(defender.sweat, SWEAT_RANGE) * SAVE_W_SWEAT
               + defender.level * SAVE_LEVEL)
    a_power = _frac(attacker.weight, WEIGHT_RANGE) + attacker.level * SAVE_LEVEL
    c = SAVE_BASE + (d_power - a_power) * SAVE_SPREAD
    return max(SAVE_MIN, min(SAVE_MAX, c))


def raw_damage(attacker, defender):
    raw = DMG_BASE + _frac(attacker.weight, WEIGHT_RANGE) * DMG_WEIGHT
    scaled = raw * (1 + (attacker.level - 1) * GROWTH)
    resist = _frac(defender.sweat, SWEAT_RANGE) * RESIST
    return max(1, round(scaled - resist))


def defend_reduction(fighter):
    r = DEF_BASE + _frac(fighter.sweat, SWEAT_RANGE) * DEF_SWEAT \
        + (fighter.level - 1) * DEF_LEVEL
    return min(DEF_CAP, r)


# =============================================================================
# RESOLUTION  -- returns a structured result (feeds journal later)
# =============================================================================
def resolve_attack(attacker, defender, rng=None):
    rng = rng or random
    res = {"attacker": attacker.name, "defender": defender.name,
           "outcome": None, "damage": 0, "reflected": 0, "crit": False}

    if rng.random() >= hit_chance(attacker):
        res["outcome"] = "miss"
        return res

    is_crit = rng.random() < crit_chance(attacker.level)
    res["crit"] = is_crit

    # parry (enemies never parry)
    if not defender.is_enemy and rng.random() < parry_chance(defender.level):
        dmg = raw_damage(attacker, defender)
        if is_crit:
            dmg = round(dmg * (1 + CRIT_BONUS))
        reflected = max(1, round(dmg * 0.5))
        attacker.hp -= reflected
        res["outcome"] = "parry"
        res["reflected"] = reflected
        return res

    # saving throw -> glance or full
    glanced = rng.random() < save_chance(defender, attacker)
    if glanced:
        dmg = GLANCE_CHIP
        res["outcome"] = "glance"
    else:
        dmg = raw_damage(attacker, defender)
        res["outcome"] = "hit"

    if is_crit:
        dmg = round(dmg * (1 + CRIT_BONUS))

    if defender.defending:
        dmg = max(1, round(dmg * (1 - defend_reduction(defender))))

    defender.hp -= dmg
    res["damage"] = dmg
    return res


def try_run(run_attempts, rng=None):
    rng = rng or random
    chance = min(0.95, RUN_BASE + run_attempts * RUN_STEP)
    return rng.random() < chance, chance


# =============================================================================
# TEXT BATTLE SIM  (tuning harness)
# =============================================================================
def _fmt(res):
    a, d = res["attacker"], res["defender"]
    c = " CRIT!" if res["crit"] else ""
    o = res["outcome"]
    if o == "miss":
        return f"  {a} swings at {d}... MISS."
    if o == "parry":
        return f"  {a} swings at {d}... PARRIED! {d} reflects {res['reflected']} back.{c}"
    if o == "glance":
        return f"  {a} grazes {d} for {res['damage']}.{c}"
    return f"  {a} hits {d} for {res['damage']}.{c}"


def simulate(party, enemy, rng=None, max_rounds=30):
    rng = rng or random
    print(f"=== {', '.join(p.name for p in party)}  VS  {enemy.name} "
          f"(HP {enemy.hp}) ===\n")
    run_attempts = 0
    for rnd in range(1, max_rounds + 1):
        print(f"-- round {rnd} --")
        # initiative: SWEAT + d6 each, this round only
        order = sorted(party + [enemy],
                       key=lambda f: f.sweat + rng.randint(1, 6), reverse=True)
        for f in order:
            if not f.alive or not enemy.alive:
                continue
            if not any(p.alive for p in party):
                break
            f.defending = False
            if f is enemy:
                target = rng.choice([p for p in party if p.alive])
                print(_fmt(resolve_attack(enemy, target, rng)))
            else:
                # dumb sim policy: everyone attacks the enemy
                print(_fmt(resolve_attack(f, enemy, rng)))
        print(f"   [enemy {enemy.name}: {max(0, enemy.hp)}/{enemy.max_hp} hp | "
              + " ".join(f"{p.name}:{max(0,p.hp)}" for p in party) + "]\n")
        if not enemy.alive:
            print(f"*** {enemy.name} is defeated! ***")
            return
        if not any(p.alive for p in party):
            print("*** the party has fallen. ***")
            return
    print("*** battle timed out ***")


if __name__ == "__main__":
    party = [
        Fighter("BILLY", iq=200, weight=2, sweat=6, hair=0),
        Fighter("MELVIN", iq=140, weight=160, sweat=5, hair=65),
        Fighter("SMELTRUD", iq=80, weight=500, sweat=4, hair=0),
        Fighter("POOTS", iq=121, weight=290, sweat=8, hair=50),
    ]
    enemy = Fighter("GORBOOSHUS", iq=60, weight=180, sweat=4, hair=0,
                    level=2, is_enemy=True)
    simulate(party, enemy)

    print("\n" + "=" * 60 + "\n")

    # same crew at level 20 vs a beefier foe -> watch the numbers grow
    party2 = [
        Fighter("BILLY", iq=200, weight=2, sweat=6, hair=0, level=20),
        Fighter("MELVIN", iq=140, weight=160, sweat=5, hair=65, level=20),
        Fighter("SMELTRUD", iq=80, weight=500, sweat=4, hair=0, level=20),
        Fighter("POOTS", iq=121, weight=290, sweat=8, hair=50, level=20),
    ]
    brute = Fighter("VONBUSSILIMONIO", iq=90, weight=650, sweat=7, hair=20,
                    level=22, is_enemy=True)
    simulate(party2, brute)