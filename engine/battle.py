"""Combat resolver + battle state machine for simtank_rpg — pure logic, no
pygame. Mirrors the player/input split: this module knows stats, hit/crit/
damage math, and how one round of a fight advances; engine.renderer's
BattleScene (see maptest.py's "battles" mode) owns pacing, drawing, and
feeding dt into it.

Formerly two files (this one + engine/combat.py) — combat.py's resolver
(Fighter, hit/crit/damage math) is merged in here, since the split between
"battle loop" and "battle math" wasn't pulling its weight once the loop
itself got this small. combat.py no longer exists.

Stats (IQ/WEIGHT/SWEAT/HAIR, see README's "Stats" paragraph) are fixed per
character/enemy and never change -- LEVEL is what's meant to dominate a
fight's outcome, with stats acting as smaller fixed modifiers layered on
top of a level-driven baseline, not the thing that decides who wins. This
covers the PHYSICAL track only (WEIGHT = power/toughness, SWEAT =
accuracy/evasion) -- IQ (magic/special power) and HAIR (magic/special
resist) are reserved for the SPECIAL/magic track and untouched here; don't
guess at those formulas ahead of SPECIAL actually being scoped, same as
DEFEND below.

Attack chain (redesigned from an earlier 3-gate version -- to-hit, then a
separate glance/save roll, then parry -- which stacked enough independent
"nothing happened" chances that fights dragged and felt miss-heavy even
against a basic enemy):
  1. to-hit  (attacker SWEAT+LEVEL vs. defender SWEAT+LEVEL, contested) -> miss wastes the turn
  2. crit    (attacker level only)                                      -> +bonus damage
  3. damage  (LEVEL-driven base +/- WEIGHT modifiers, floored)

Parry is intentionally not in this chain -- it's slated to become a
timing/button-press mechanic (a real skill check) rather than a passive
RNG roll, so there's nothing to tune here until that's built as its own
system in engine.renderer.BattleScene.
# TODO: PARRY QUICK TIME EVENT + ANIMATION -- see engine.renderer.BattleScene,
# this is where the old passive parry chance used to hook in, right before
# the enemy's attack resolves.

Scope note (first graphical wire-up, see README): this is a single Fighter
(MELVIN) vs. a single enemy. The party's turn is now player-driven -- see
BattleMenu below -- and ATTACK/ITEM/RUN all actually do something when
confirmed (see BattleState.step/step_item/attempt_run); DEFEND is still a
real, choosable row with no effect wired in yet, and SPECIAL isn't in the
row at all (each party member gets their own special, unlocked by a story
flag or player level -- see README roadmap's party-role table -- not by
just existing in the party). Don't guess at DEFEND's effect when the time
comes -- ask first.
"""

import json
import random
from dataclasses import dataclass, field
from pathlib import Path

from engine.enemy import resolve_level
from engine.journal import Journal
from engine.roster import PartyMember

# =============================================================================
# STAT NORMALIZATION  --  reference ranges. raw stat -> 0..1 fraction.
# =============================================================================
# IQ_RANGE/HAIR_RANGE are reserved for the magic/special track (SPECIAL isn't
# built yet -- see module docstring) -- not read by anything below.
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
# --- to-hit: attacker SWEAT+LEVEL vs. defender SWEAT+LEVEL, contested ------
HIT_BASE = 0.75
LEVEL_HIT_SCALE = 0.03   # per level of (attacker.level - defender.level)
SWEAT_ACC = 0.25         # attacker SWEAT fraction's contribution (accuracy)
SWEAT_EVA = 0.25         # defender SWEAT fraction's contribution (evasion)
HIT_MIN, HIT_MAX = 0.15, 0.95

# --- damage: a LEVEL-driven base, WEIGHT as a smaller fixed modifier ------
LEVEL_BASE = 6.0         # dmg *= 1 + (level-1)*GROWTH -- the dominant term
GROWTH = 0.10            # dinging: (lvl60 ~ 6.9x LEVEL_BASE)
WEIGHT_POWER = 1.5       # attacker WEIGHT fraction's contribution
WEIGHT_TOUGHNESS = 1.0   # defender WEIGHT fraction's contribution (damage reduction)
DMG_FLOOR = 1
CRIT_BONUS = 0.15        # +15% on a crit
# TODO: CRIT ANIMATION WHEN CRITS HAPPEN -- see engine.renderer.BattleScene,
# resolve_attack's res["crit"] is where this should be read from.

# --- crit (scales with LEVEL only) ----------------------------------------
CRIT_START = 0.08
CRIT_CEIL = 0.35         # chance at MAX_LEVEL
MAX_LEVEL = 60
_crit_step = (CRIT_CEIL - CRIT_START) / (MAX_LEVEL - 1)

# --- defend action -------------------------------------------
DEF_BASE = 0.20
DEF_SWEAT = 0.30
DEF_LEVEL = 0.004
DEF_CAP = 0.75

# --- run away (level-scaled base, escalating per failed attempt) --
RUN_BASE = 0.40          # chance at equal level
RUN_LEVEL_SCALE = 0.03   # +/- per level of difference (party level - enemy level)
RUN_STEP = 0.15          # added per failed attempt this battle, by anyone
RUN_MIN, RUN_MAX = 0.05, 0.95

# --- hp ------------------------------------------------------
PARTY_HP_BASE = 25
PARTY_HP_PER_LEVEL = 2
PARTY_HP_WEIGHT = 8      # + WEIGHT_frac * this -- mirrors ENEMY_HP_WEIGHT, so
                          #   a heavy party member (e.g. POOTS) is visibly
                          #   tankier, not just harder-hitting
ENEMY_HP_BASE = 14
ENEMY_HP_WEIGHT = 12     # + WEIGHT_frac * this
ENEMY_HP_PER_LEVEL = 3

# --- mp ------------------------------------------------------
PARTY_MP_BASE = 20
PARTY_MP_PER_LEVEL = 1

# --- xp / leveling ---------------------------------------------
# level 1->2: 20, 2->3: ~57, 3->4: ~104, 4->5: ~160 ... tune freely, no other
# code depends on the exact curve.
XP_TO_NEXT_BASE = 20
XP_TO_NEXT_EXPONENT = 1.5


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
    attack_bonus: int = 0    # flat, from an equipped weapon's effect.attack -- see fighter_from_roster
    defense_bonus: int = 0   # flat, from an equipped armour's effect.defense -- see fighter_from_roster

    def __post_init__(self):
        if self.max_hp == 0:
            if self.is_enemy:
                self.max_hp = round(
                    ENEMY_HP_BASE
                    + _frac(self.weight, WEIGHT_RANGE) * ENEMY_HP_WEIGHT
                    + (self.level - 1) * ENEMY_HP_PER_LEVEL
                )
            else:
                self.max_hp = round(
                    PARTY_HP_BASE
                    + _frac(self.weight, WEIGHT_RANGE) * PARTY_HP_WEIGHT
                    + (self.level - 1) * PARTY_HP_PER_LEVEL
                )
            self.hp = self.max_hp
        if not self.is_enemy and self.max_mp == 0:
            self.max_mp = PARTY_MP_BASE + (self.level - 1) * PARTY_MP_PER_LEVEL
            self.mp = self.max_mp

    @property
    def alive(self):
        return self.hp > 0


def load_fighter(path: Path) -> Fighter:
    """One party member's Fighter from data/party/<name>.json, read fresh
    off disk -- hp/mp/level are whatever the file says, not live state. Only
    pulls the fields Fighter needs -- personality/special/items/xp live in
    the same file but aren't read here (see module docstring: SPECIAL/ITEM
    aren't wired into battle yet). Still used by maptest.py's isolated
    "battles" debug mode, which has no save/roster to read live state from
    -- see fighter_from_roster for the real-game path."""
    s = json.loads(Path(path).read_text())
    return Fighter(
        name=s["name"], iq=s["iq"], weight=s["weight"], sweat=s["sweat"], hair=s["hair"],
        level=s["lvl"], hp=s["hp"], max_hp=s["max_hp"],
        mp=s.get("mp", 0), max_mp=s.get("max_mp", 0),
    )


def fighter_from_roster(path: Path, member: PartyMember, item_defs: dict | None = None) -> Fighter:
    """One party member's Fighter for a real (non-debug) battle: static
    combat stats (iq/weight/sweat/hair) from data/party/<name>.json, live
    hp/max_hp/mp/max_mp/level from `member` (engine.roster.Roster) instead
    of the file -- a battle that leaves this member hurt carries that HP
    back into the overworld and across saves, rather than resetting fresh
    from disk every time the way load_fighter's debug path does.

    `item_defs` (engine.inventory.load_item_defs()'s id -> ItemDef map), if
    given, resolves member.equipped_weapon/equipped_armour into flat
    attack_bonus/defense_bonus off each ItemDef's effect dict -- 0 for
    either if unequipped, or if item_defs itself is omitted (maptest.py's
    debug "battles" mode has no roster/equipment concept at all)."""
    s = json.loads(Path(path).read_text())
    attack_bonus = defense_bonus = 0
    if item_defs is not None:
        weapon_def = item_defs.get(member.equipped_weapon) if member.equipped_weapon else None
        armour_def = item_defs.get(member.equipped_armour) if member.equipped_armour else None
        if weapon_def is not None and weapon_def.effect:
            attack_bonus = weapon_def.effect.get("attack", 0)
        if armour_def is not None and armour_def.effect:
            defense_bonus = armour_def.effect.get("defense", 0)
    return Fighter(
        name=s["name"], iq=s["iq"], weight=s["weight"], sweat=s["sweat"], hair=s["hair"],
        level=member.lvl, hp=member.hp, max_hp=member.max_hp,
        mp=member.mp, max_mp=member.max_mp,
        attack_bonus=attack_bonus, defense_bonus=defense_bonus,
    )


def xp_to_next_level(level: int) -> int:
    return round(XP_TO_NEXT_BASE * level ** XP_TO_NEXT_EXPONENT)


def apply_level_ups(member: PartyMember) -> int:
    """Mutates member in place; returns how many levels were gained (0 if
    none). xp is per-level progress, not a cumulative total (every
    data/party/*.json starts at xp: 0) -- each level crossed subtracts that
    level's threshold and increments max_hp/max_mp by the same
    PARTY_HP_PER_LEVEL/PARTY_MP_PER_LEVEL step a fresh Fighter uses, so the
    authored data/party/*.json max_hp (itself already including that
    member's fixed PARTY_HP_WEIGHT bonus -- see Fighter.__post_init__)
    stays the baseline rather than being recomputed wholesale. Current
    hp/mp are left exactly as they are -- a level-up doesn't also fully
    heal you."""
    levels_gained = 0
    while member.xp >= xp_to_next_level(member.lvl):
        member.xp -= xp_to_next_level(member.lvl)
        member.lvl += 1
        member.max_hp += PARTY_HP_PER_LEVEL
        member.max_mp += PARTY_MP_PER_LEVEL
        levels_gained += 1
    return levels_gained


# =============================================================================
# FORMULAS
# =============================================================================
def hit_chance(attacker, defender):
    """Contested to-hit: attacker SWEAT+LEVEL vs. defender SWEAT+LEVEL, one
    roll -- no separate glance/save gate on top (see module docstring for
    why the old 3-gate chain got collapsed to this)."""
    c = (HIT_BASE
         + (attacker.level - defender.level) * LEVEL_HIT_SCALE
         + _frac(attacker.sweat, SWEAT_RANGE) * SWEAT_ACC
         - _frac(defender.sweat, SWEAT_RANGE) * SWEAT_EVA)
    return max(HIT_MIN, min(HIT_MAX, c))


def crit_chance(level):
    return CRIT_START + (level - 1) * _crit_step


def raw_damage(attacker, defender):
    """LEVEL is the dominant term; WEIGHT is a smaller fixed modifier on
    both sides (attacker's power, defender's toughness) -- floored so a
    near-zero-WEIGHT attacker (e.g. BILLY) still deals real, level-scaled
    damage rather than collapsing toward nothing."""
    base = LEVEL_BASE * (1 + (attacker.level - 1) * GROWTH)
    dmg = (base
           + _frac(attacker.weight, WEIGHT_RANGE) * WEIGHT_POWER
           - _frac(defender.weight, WEIGHT_RANGE) * WEIGHT_TOUGHNESS)
    return max(DMG_FLOOR, round(dmg) + attacker.attack_bonus)


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
           "outcome": None, "damage": 0, "crit": False}

    if rng.random() >= hit_chance(attacker, defender):
        res["outcome"] = "miss"
        return res

    is_crit = rng.random() < crit_chance(attacker.level)
    res["crit"] = is_crit
    # TODO: CRIT ANIMATION WHEN CRITS HAPPEN -- hook off res["crit"] in
    # engine.renderer.BattleScene wherever this result's flavor text gets shown.

    dmg = raw_damage(attacker, defender)
    if is_crit:
        dmg = round(dmg * (1 + CRIT_BONUS))

    if defender.defending:
        dmg = max(1, round(dmg * (1 - defend_reduction(defender))))

    dmg = max(1, dmg - defender.defense_bonus)

    defender.hp -= dmg
    res["outcome"] = "hit"
    res["damage"] = dmg
    return res


def resolve_item_use(fighter, effect: dict) -> dict:
    """Apply a consumable's hp/mp effect to `fighter` (clamped to max_hp/
    max_mp) -- the in-battle ITEM row's target is always the single Fighter
    present, unlike the overworld's USE flow, which lets the player pick
    which party member (engine.input._confirm_item_action). Returns the
    amounts actually applied (may be less than `effect` says, if already
    near full), for _fmt_item_use."""
    hp_before, mp_before = fighter.hp, fighter.mp
    if "hp" in effect:
        fighter.hp = min(fighter.max_hp, fighter.hp + effect["hp"])
    if "mp" in effect:
        fighter.mp = min(fighter.max_mp, fighter.mp + effect["mp"])
    return {"hp_applied": fighter.hp - hp_before, "mp_applied": fighter.mp - mp_before}


def try_run(party_level, enemy_level, run_attempts, rng=None):
    """Escape chance for RUN -- see BattleState.attempt_run. `party_level`
    is meant to be the average party member's level; battle is still
    1v1-MELVIN-only (see module docstring), so for now it's just his level
    -- this becomes a real average once full-party battles exist, no other
    change needed here. Base chance is RUN_BASE at equal level, shifting by
    RUN_LEVEL_SCALE per level of advantage/disadvantage (clamped to
    RUN_MIN/RUN_MAX -- no guaranteed-escape threshold at any level gap,
    everything in this battle system stays probabilistic). `run_attempts`
    (prior failed attempts *this battle*) adds RUN_STEP on top, so a run
    always eventually succeeds no matter how bad the matchup, same idea as
    hit_chance/crit_chance elsewhere in this module never hard-gating on a stat."""
    rng = rng or random
    base = max(RUN_MIN, min(RUN_MAX, RUN_BASE + (party_level - enemy_level) * RUN_LEVEL_SCALE))
    chance = min(RUN_MAX, base + run_attempts * RUN_STEP)
    return rng.random() < chance, chance


# =============================================================================
# FORMATTING
# =============================================================================
def _fmt_attack(res: dict) -> str:
    a, d, crit = res["attacker"], res["defender"], " CRIT!" if res["crit"] else ""
    if res["outcome"] == "miss":
        return f"{a} swings at {d}... MISS."
    return f"{a} hits {d} for {res['damage']}.{crit}"


def _fmt_item_use(fighter_name: str, item_name: str, res: dict) -> str:
    parts = []
    if res["hp_applied"]:
        parts.append(f"+{res['hp_applied']} HP")
    if res["mp_applied"]:
        parts.append(f"+{res['mp_applied']} MP")
    gained = "  ".join(parts) if parts else "no effect"
    return f"{fighter_name} uses {item_name}. {gained}."


# =============================================================================
# BATTLE STATE MACHINE
# =============================================================================
@dataclass
class BattleState:
    """One Fighter vs. one Fighter (see module docstring). ATTACK, ITEM, and
    RUN are wired to actually do something -- see BattleMenu below; DEFEND
    is still a no-op.

    A single `step()` resolves whichever side's turn it currently is and
    returns the flavor text for it -- the caller (BattleScene) decides
    *when* to call step(), so pacing/animation/typewriter timing stays a
    rendering concern, not something this class blocks on. RUN is a
    separate entry point, `attempt_run()`, since it's a player choice on
    the party's turn rather than something step() should ever resolve on
    its own the way it does the enemy's turn.
    """
    party: Fighter
    enemy: Fighter
    rng:   random.Random
    phase: str = "party_turn"   # "party_turn" | "enemy_turn" | "win" | "loss" | "fled"
    round: int = 1
    run_attempts: int = 0   # prior *failed* RUN attempts this battle -- see try_run
    journal: Journal = field(default_factory=Journal)
    enemy_gold:  int | list[int] | None = None   # raw EnemyDef.gold, threaded in by the caller
    enemy_xp:    int | list[int] | None = None   # raw EnemyDef.xp, threaded in by the caller
    enemy_drop_item:   str | None = None          # raw EnemyDef.drop_item, threaded in by the caller
    enemy_drop_chance: float = 0.0                 # raw EnemyDef.drop_chance, threaded in by the caller
    enemy_defeat_text: str | None = None           # raw EnemyDef.defeat_text, threaded in by the caller --
                                                     #   appended to the fatal attack's flavor line, see
                                                     #   _step_party_attack. None only in tests that don't set it.
    gold_reward: int | None = None                # resolved once the win fires; None until then
    xp_reward:   int | None = None                # resolved once the win fires; None until then
    item_reward: str | None = None                 # resolved item id, or None if no drop rolled/set

    def step(self) -> str:
        if self.phase == "party_turn":
            return self._step_party_attack()
        if self.phase == "enemy_turn":
            return self._step_enemy_attack()
        return ""   # battle already over -- nothing left to advance

    def attempt_run(self) -> str:
        """Resolve a RUN choice on the party's turn -- see try_run for the
        chance formula. Success ends the battle with phase "fled" (no
        rewards, no penalty -- the caller just returns to the overworld,
        the enemy that was fought stays on the map since it wasn't
        defeated). Failure burns the turn -- the enemy gets to attack --
        and bumps run_attempts so the next try is more likely to work."""
        success, chance = try_run(self.party.level, self.enemy.level, self.run_attempts, self.rng)
        if success:
            self.phase = "fled"
            self.journal.log_event({"type": "BATTLE_FLED", "tick": self.round, "chance": chance})
            return f"{self.party.name} got away safely!"
        self.run_attempts += 1
        self.phase = "enemy_turn"
        self.journal.log_event({"type": "RUN_FAILED", "tick": self.round, "chance": chance})
        return f"{self.party.name} couldn't get away!"

    def _step_party_attack(self) -> str:
        res = resolve_attack(self.party, self.enemy, self.rng)
        flavor = _fmt_attack(res)
        if not self.enemy.alive:
            self.phase = "win"
            flavor += f" {self.enemy_defeat_text}" if self.enemy_defeat_text else f" {self.enemy.name} is defeated!"
            self.journal.log_event({"type": "ENEMY_KILLED", "tick": self.round,
                                     "enemy": self.enemy.name, "enemy_lvl": self.enemy.level})
            self.journal.log_event({"type": "BATTLE_WIN", "tick": self.round,
                                     "enemy": self.enemy.name, "enemy_lvl": self.enemy.level})
            self.gold_reward = resolve_level(self.enemy_gold, self.rng) if self.enemy_gold is not None else 0
            if self.gold_reward:
                flavor += f" +${self.gold_reward}."
                self.journal.log_event({"type": "GOLD_AWARDED", "tick": self.round,
                                         "enemy": self.enemy.name, "gold": self.gold_reward})
            self.xp_reward = resolve_level(self.enemy_xp, self.rng) if self.enemy_xp is not None else 0
            if self.xp_reward:
                flavor += f" +{self.xp_reward}XP."
                self.journal.log_event({"type": "XP_AWARDED", "tick": self.round,
                                         "enemy": self.enemy.name, "xp": self.xp_reward})
            # Item name isn't shown here -- BattleState/BattleScene have no
            # ItemDef access (that's OverworldScene's job) -- see caller for
            # the "Found X!" message shown after returning to the overworld.
            if self.enemy_drop_item and self.rng.random() < self.enemy_drop_chance:
                self.item_reward = self.enemy_drop_item
                self.journal.log_event({"type": "ITEM_AWARDED", "tick": self.round,
                                         "enemy": self.enemy.name, "item": self.item_reward})
        else:
            self.phase = "enemy_turn"
        return flavor

    def step_item(self, item_name: str, effect: dict) -> str:
        """Resolve using a consumable on the party's turn -- called instead
        of step() when the player confirms an item off the ITEM row (see
        BattleMenu.picking_item / BattleScene._confirm_choice). Unlike an
        attack, this can never end the battle, so it always burns the turn
        and hands off to the enemy, same as a DEFEND would if that were
        wired up."""
        res = resolve_item_use(self.party, effect)
        self.phase = "enemy_turn"
        self.journal.log_event({"type": "ITEM_USED", "tick": self.round,
                                 "item": item_name, **res})
        return _fmt_item_use(self.party.name, item_name, res)

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


# =============================================================================
# ACTION MENU  -- cursor state for the battle screen's ATTACK/ITEM/DEFEND/RUN
# row (see module docstring). SPECIAL isn't in this row -- each party member
# gets their own special ability, unlocked by a story flag or player level,
# not just by existing in the party -- re-add it once that gating is built.
# =============================================================================
BATTLE_MENU_OPTIONS: tuple[str, ...] = ("ATTACK", "ITEM", "DEFEND", "RUN")


@dataclass
class BattleMenu:
    """Cursor state for the party turn's action row -- mirrors
    engine.menu.StartMenu: a vertical list, N/S move + wrap. ATTACK, ITEM,
    and RUN all do something when confirmed right now (see
    BattleScene._confirm_choice) -- DEFEND is still a real choosable row,
    just a no-op until its effect is designed. This class only tracks
    the cursor itself; whether the row is even shown is BattleScene's call
    (only during the party's turn, while it's actually waiting on a
    choice).

    Also carries the ITEM row's own sub-state (`picking_item`), mirroring
    engine.menu.ShopMenu.picking_amount's nested-sub-state shape: confirming
    ITEM doesn't act immediately, it opens a scrollable list of usable
    consumables. This class holds no item data itself (same split as
    InventoryMenu not knowing about engine.inventory) -- BattleScene passes
    the list length in per call."""
    selected: int = 0
    picking_item: bool = False
    item_cursor: int = 0

    def move_cursor(self, direction: str) -> None:
        if direction == "N":
            self.selected = (self.selected - 1) % len(BATTLE_MENU_OPTIONS)
        elif direction == "S":
            self.selected = (self.selected + 1) % len(BATTLE_MENU_OPTIONS)

    def selected_option(self) -> str:
        return BATTLE_MENU_OPTIONS[self.selected]

    def start_item_pick(self) -> None:
        self.picking_item = True
        self.item_cursor = 0

    def cancel_item_pick(self) -> None:
        self.picking_item = False
        self.item_cursor = 0

    def move_item_cursor(self, direction: str, list_len: int) -> None:
        """N/S scrolls the usable-item list, clamped (not wrapped) -- same
        idiom as InventoryMenu.move_cursor's list scroll."""
        if direction == "N":
            self.item_cursor = max(0, self.item_cursor - 1)
        elif direction == "S":
            self.item_cursor = min(max(list_len - 1, 0), self.item_cursor + 1)
