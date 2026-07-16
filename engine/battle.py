"""Combat resolver + battle state machine for simtank_rpg — pure logic, no
pygame. Mirrors the player/input split: this module knows stats, hit/crit/
parry/damage math, and how one round of a fight advances; engine.renderer's
BattleScene (see maptest.py's "battles" mode) owns pacing, drawing, and
feeding dt into it.

Formerly two files (this one + engine/combat.py) — combat.py's resolver
(Fighter, hit/crit/parry/damage math) is merged in here, since the split
between "battle loop" and "battle math" wasn't pulling its weight once the
loop itself got this small. combat.py no longer exists.

Attack chain:
  1. to-hit      (SWEAT + level)          -> miss wastes the turn
  2. crit check  (attacker level)          -> flags +bonus damage
  3. parry check (defender level, rare)    -> reflect half, defender takes none
  4. saving throw(multi-stat contest)      -> glance (tiny dmg) vs full
  5. damage      (WEIGHT + level - resist)

Stats never change; LVL/HP/XP do. Damage scales with level ("dinging").

Scope note (first graphical wire-up, see README): this is a single Fighter
(MELVIN) vs. a single enemy, auto-attack only — no player-driven action
menu, no DEFEND/RUN logic, no party specials (SING/LAUGH/SNACK/TICKLE) or
items wired in. Those are real, future battle actions -- but their exact
scope/spec (what ITEM does mid-battle, whether/how SPECIAL comes back) isn't
decided yet. Don't guess at an implementation for either when the time
comes -- ask first.
"""

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from engine.journal import Journal

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


def load_fighter(path: Path) -> Fighter:
    """One party member's Fighter from data/party/<name>.json. Only pulls
    the fields Fighter needs -- personality/special/items/xp live in the
    same file but aren't read here (see module docstring: SPECIAL/ITEM
    aren't wired into battle yet)."""
    s = json.loads(Path(path).read_text())
    return Fighter(
        name=s["name"], iq=s["iq"], weight=s["weight"], sweat=s["sweat"], hair=s["hair"],
        level=s["lvl"], hp=s["hp"], max_hp=s["max_hp"],
        mp=s.get("mp", 0), max_mp=s.get("max_mp", 0),
    )


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
# RESOLUTION  -- returns a structured result
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
# FORMATTING
# =============================================================================
def _fmt_attack(res: dict) -> str:
    a, d, crit = res["attacker"], res["defender"], " CRIT!" if res["crit"] else ""
    o = res["outcome"]
    if o == "miss":   return f"{a} swings at {d}... MISS."
    if o == "parry":  return f"{a} swings at {d}... PARRIED! {d} reflects {res['reflected']} back.{crit}"
    if o == "glance": return f"{a} grazes {d} for {res['damage']}.{crit}"
    return f"{a} hits {d} for {res['damage']}.{crit}"


# =============================================================================
# BATTLE STATE MACHINE
# =============================================================================
@dataclass
class BattleState:
    """One Fighter vs. one Fighter, auto-attack only (see module docstring).

    A single `step()` resolves whichever side's turn it currently is and
    returns the flavor text for it -- the caller (BattleScene) decides
    *when* to call step(), so pacing/animation/typewriter timing stays a
    rendering concern, not something this class blocks on.
    """
    party: Fighter
    enemy: Fighter
    rng:   random.Random
    phase: str = "party_turn"   # "party_turn" | "enemy_turn" | "win" | "loss"
    round: int = 1
    journal: Journal = field(default_factory=Journal)

    def step(self) -> str:
        if self.phase == "party_turn":
            return self._step_party_attack()
        if self.phase == "enemy_turn":
            return self._step_enemy_attack()
        return ""   # battle already over -- nothing left to advance

    def _step_party_attack(self) -> str:
        res = resolve_attack(self.party, self.enemy, self.rng)
        flavor = _fmt_attack(res)
        if not self.enemy.alive:
            self.phase = "win"
            flavor += f" {self.enemy.name} is defeated!"
            self.journal.log_event({"type": "ENEMY_KILLED", "tick": self.round,
                                     "enemy": self.enemy.name, "enemy_lvl": self.enemy.level})
            self.journal.log_event({"type": "BATTLE_WIN", "tick": self.round,
                                     "enemy": self.enemy.name, "enemy_lvl": self.enemy.level})
        else:
            self.phase = "enemy_turn"
        return flavor

    def _step_enemy_attack(self) -> str:
        res = resolve_attack(self.enemy, self.party, self.rng)
        flavor = _fmt_attack(res)
        if not self.party.alive:
            self.phase = "loss"
            flavor += f" {self.party.name} has fallen!"
            self.journal.log_event({"type": "BATTLE_LOSS", "tick": self.round})
        else:
            self.phase = "party_turn"
            self.round += 1
        return flavor
