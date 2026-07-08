"""Battle loop for simtank_rpg.

Extracted from run_cli.py so both CLI and web can share the same logic.
The `emit` callback (optional) sends SSE events for the web layer; without it
the battle runs silently to stdout (CLI mode). `run_battle` always returns a
result dict: {"outcome": "win"|"loss"|"flee", "rounds": int}.

Event types emitted:
  battle_start  — initial HP state for all participants
  battle_action — result of a single action (attack, defend, heal, etc.)
  battle_end    — final outcome
"""

import json
import random
import time
from dataclasses import dataclass, field
from pathlib import Path

from engine.combat import Fighter, hit_chance, resolve_attack, try_run
from engine.config import cfg
from engine.journal import Journal
from llm.client import ask
from llm.prompts import build_battle_context, build_system_prompt
from llm.schema import parse_action

SPECIAL_COOLDOWNS: dict[str, int] = {
    "SING":   2,
    "TICKLE": 4,
    "SNACK":  3,
    "LAUGH":  3,
}


# ---------------------------------------------------------------------------
# Data containers
# ---------------------------------------------------------------------------

@dataclass
class BattleMember:
    fighter:          Fighter
    personality:      str
    special:          dict
    xp:               int = 0
    status:           str | None = None
    buffs:            list = field(default_factory=list)  # [{name, damage_mult, turns_remaining}]
    special_cooldown: int = 0

    @property
    def name(self):    return self.fighter.name
    @property
    def hp(self):      return max(0, self.fighter.hp)
    @property
    def max_hp(self):  return self.fighter.max_hp
    @property
    def lvl(self):     return self.fighter.level
    @property
    def alive(self):   return self.fighter.alive


@dataclass
class BattleEnemy:
    fighter:         Fighter
    npc_sprite:      str = ""          # "npc01"–"npc04" for web renderer
    status:          str | None = None
    status_turns:    int = 0
    status_duration: int | None = None

    @property
    def name(self):    return self.fighter.name
    @property
    def hp(self):      return max(0, self.fighter.hp)
    @property
    def max_hp(self):  return self.fighter.max_hp
    @property
    def lvl(self):     return self.fighter.level
    @property
    def alive(self):   return self.fighter.alive


# ---------------------------------------------------------------------------
# Loader
# ---------------------------------------------------------------------------

def load_party(data_dir: Path) -> list[BattleMember]:
    members = []
    for path in sorted(data_dir.glob("*.json")):
        s = json.loads(path.read_text())
        fighter = Fighter(
            name=s["name"], iq=s["iq"], weight=s["weight"],
            sweat=s["sweat"], hair=s["hair"], level=s["lvl"],
            hp=s["hp"], max_hp=s["max_hp"],
        )
        members.append(BattleMember(
            fighter=fighter,
            personality=s.get("personality", ""),
            special=s.get("special", {"name": "", "description": "", "target": None}),
            xp=s.get("xp", 0),
        ))
    return members


# ---------------------------------------------------------------------------
# Formatting
# ---------------------------------------------------------------------------

def _fmt_attack(res: dict) -> str:
    a, d, crit = res["attacker"], res["defender"], " CRIT!" if res["crit"] else ""
    o = res["outcome"]
    if o == "miss":   return f"  {a} swings at {d}... MISS."
    if o == "parry":  return f"  {a} swings at {d}... PARRIED! {d} reflects {res['reflected']} back.{crit}"
    if o == "glance": return f"  {a} grazes {d} for {res['damage']}.{crit}"
    return f"  {a} hits {d} for {res['damage']}.{crit}"


def _fmt_brief(res: dict) -> str:
    o = res["outcome"]
    if o == "miss":   return "missed"
    if o == "parry":  return f"parried — {res['reflected']} reflected"
    if o == "glance": return f"glanced for {res['damage']}"
    return f"{res['damage']} damage"


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _spec_target_type(spec: dict) -> str:
    t = spec.get("target")
    return "ally" if t in (None, "") else t


def _apply_damage_buffs(member: BattleMember, res: dict, enemy: BattleEnemy) -> None:
    if res["damage"] <= 0:
        return
    for buff in member.buffs:
        mult = buff.get("damage_mult", 1.0)
        if mult > 1.0:
            extra = max(0, round(res["damage"] * (mult - 1.0)))
            enemy.fighter.hp -= extra
            res["damage"] += extra


def _tick_buffs(member: BattleMember) -> None:
    member.buffs = [
        dict(b, turns_remaining=b["turns_remaining"] - 1)
        for b in member.buffs
    ]
    member.buffs = [b for b in member.buffs if b["turns_remaining"] > 0]
    if member.special_cooldown > 0:
        member.special_cooldown -= 1


def _pick_ally(party: list, target_name: str | None, fallback: BattleMember) -> BattleMember:
    return next(
        (m for m in party if m.name == target_name and m.alive),
        next((m for m in party if m.alive), fallback),
    )


def _party_hp(party: list[BattleMember]) -> dict:
    return {m.name: m.hp for m in party}


# ---------------------------------------------------------------------------
# SSE emit helpers
# ---------------------------------------------------------------------------

def _emit_action(emit, actor, action, target, res, flavor, enemy, party, sleep_ms):
    if emit is None:
        return
    emit({
        "type":        "battle_action",
        "actor":       actor,
        "action":      action,
        "target":      target,
        "outcome":     res.get("outcome", action.lower()),
        "damage":      res.get("damage", 0),
        "crit":        res.get("crit", False),
        "flavor":      flavor,
        "enemy_hp":    enemy.hp,
        "enemy_max_hp": enemy.max_hp,
        "party_hp":    _party_hp(party),
    })
    time.sleep(sleep_ms / 1000)


def _emit_end(emit, outcome, tick, enemy_name):
    if emit:
        emit({"type": "battle_end", "outcome": outcome,
              "rounds": tick, "enemy_name": enemy_name})


# ---------------------------------------------------------------------------
# Special actions
# ---------------------------------------------------------------------------

def special_SING(member, enemy, spec, rng, history, emit, party, sleep_ms):
    hit = rng.random() < hit_chance(member.fighter)
    print(f"  {member.name} SINGS to {enemy.name}!", flush=True)
    if hit and enemy.alive and not enemy.status:
        enemy.status = spec["status_effect"]
        enemy.status_turns = 0
        enemy.status_duration = None
        flavor = f"{member.name} SINGS to {enemy.name}! {enemy.name} is MESMERIZED!"
        print(f"  {enemy.name} is MESMERIZED!", flush=True)
        history[member.name] = f"SANG to {enemy.name} — inflicted MESMERIZED"
    else:
        flavor = f"{member.name} SINGS to {enemy.name}! No effect."
        print(f"  IT HAD NO EFFECT ON {enemy.name}!", flush=True)
        history[member.name] = f"SANG to {enemy.name} — no effect"
    _emit_action(emit, member.name, "SING", enemy.name,
                 {"outcome": "status" if hit else "miss"}, flavor, enemy, party, sleep_ms)


def special_LAUGH(member, enemy, spec, rng, history, emit, party, sleep_ms):
    hit = rng.random() < hit_chance(member.fighter)
    print(f"  {member.name} LAUGHS at {enemy.name}!", flush=True)
    if hit and enemy.alive and not enemy.status:
        enemy.status = "CRINGE"
        enemy.status_turns = 0
        enemy.status_duration = rng.randint(
            spec.get("status_duration_min", 3), spec.get("status_duration_max", 5))
        flavor = f"{member.name} LAUGHS! {enemy.name} is CRINGING!"
        print(f"  {enemy.name} is CRINGING!", flush=True)
        history[member.name] = f"LAUGHED at {enemy.name} — inflicted CRINGE"
    else:
        flavor = f"{member.name} LAUGHS at {enemy.name}! No effect."
        print(f"  IT HAD NO EFFECT ON {enemy.name}!", flush=True)
        history[member.name] = f"LAUGHED at {enemy.name} — no effect"
    _emit_action(emit, member.name, "LAUGH", enemy.name,
                 {"outcome": "status" if hit else "miss"}, flavor, enemy, party, sleep_ms)


# ---------------------------------------------------------------------------
# Battle loop
# ---------------------------------------------------------------------------

MAX_ROUNDS = cfg.battle_max_rounds


def run_battle(party: list[BattleMember], enemy: BattleEnemy,
               rng: random.Random, emit=None,
               battle_sleep_ms: int = 800) -> dict:
    """Run a complete battle. Returns {"outcome": "win"|"loss"|"flee", "rounds": int}.

    emit:            optional SSE broadcast callback (web mode)
    battle_sleep_ms: delay between actions in web mode
    """
    journal  = Journal()
    history: dict[str, str] = {}
    run_attempts = 0
    tick = 0
    alive_set = {m.name for m in party}

    print(f"\n=== {', '.join(m.name for m in party)}  VS  {enemy.name} (LVL {enemy.lvl}) ===\n",
          flush=True)

    if emit:
        emit({
            "type":  "battle_start",
            "enemy": {"name": enemy.name, "hp": enemy.hp,
                      "max_hp": enemy.max_hp, "npc_sprite": enemy.npc_sprite},
            "party": [{"name": m.name, "hp": m.hp, "max_hp": m.max_hp} for m in party],
        })

    while True:
        print(f"--- ROUND {tick + 1} ---", flush=True)

        # ── Party turns ───────────────────────────────────────────────────────
        for member in party:
            if not member.alive or not enemy.alive:
                continue

            member.fighter.defending = False

            print(f"  [{member.name} thinking...]", flush=True)
            ctx      = build_battle_context(member, party, enemy, history, run_attempts)
            sys_pmt  = build_system_prompt(member)
            raw      = ask(ctx, sys_pmt)
            party_names = [m.name for m in party]
            dec = parse_action(raw, member.special.get("target"), enemy.name, party_names,
                               special_name=member.special.get("name", ""))
            action, target = dec["action"], dec["target"]

            if action == "SPECIAL" and (not member.special.get("name") or member.special_cooldown > 0):
                action = "ATTACK"

            if action == "ATTACK":
                res = resolve_attack(member.fighter, enemy.fighter, rng)
                _apply_damage_buffs(member, res, enemy)
                flavor = _fmt_attack(res).strip()
                print(_fmt_attack(res), flush=True)
                history[member.name] = f"attacked {enemy.name} — {_fmt_brief(res)}"
                _emit_action(emit, member.name, "ATTACK", enemy.name,
                             res, flavor, enemy, party, battle_sleep_ms)

            elif action == "DEFEND":
                member.fighter.defending = True
                flavor = f"{member.name} takes a defensive stance."
                print(f"  {flavor}", flush=True)
                history[member.name] = "defended"
                _emit_action(emit, member.name, "DEFEND", member.name,
                             {"outcome": "defend"}, flavor, enemy, party, battle_sleep_ms)

            elif action == "SPECIAL":
                spec   = member.special
                ttype  = _spec_target_type(spec)
                effect = spec.get("effect_type")

                if spec.get("name") == "SING":
                    special_SING(member, enemy, spec, rng, history, emit, party, battle_sleep_ms)

                elif spec.get("name") == "LAUGH":
                    special_LAUGH(member, enemy, spec, rng, history, emit, party, battle_sleep_ms)

                elif ttype == "enemy":
                    status_effect = spec.get("status_effect")
                    res = resolve_attack(member.fighter, enemy.fighter, rng)
                    _apply_damage_buffs(member, res, enemy)
                    hit = res["outcome"] != "miss"
                    status_before = enemy.status
                    if hit and enemy.alive and status_effect and not enemy.status:
                        enemy.status = status_effect
                        enemy.status_turns = 0
                        if status_effect == "CRINGE":
                            enemy.status_duration = rng.randint(
                                spec.get("status_duration_min", 3),
                                spec.get("status_duration_max", 5))
                        else:
                            enemy.status_duration = None
                    status_applied = enemy.status and enemy.status != status_before
                    flavor = f"{member.name} uses {spec['name']}! {_fmt_attack(res).strip()}"
                    if status_applied:
                        flavor += f" {enemy.name} is {enemy.status}!"
                    print(f"  {flavor}", flush=True)
                    history[member.name] = (
                        f"used {spec['name']} on {enemy.name} — {_fmt_brief(res)}"
                        + (f" — inflicted {enemy.status}" if status_applied else "")
                    )
                    _emit_action(emit, member.name, "SPECIAL", enemy.name,
                                 res, flavor, enemy, party, battle_sleep_ms)

                elif effect == "heal":
                    ally   = _pick_ally(party, target, member)
                    pct    = rng.uniform(spec.get("heal_pct_min", 0.15), spec.get("heal_pct_max", 0.25))
                    amount = max(1, round(ally.max_hp * pct))
                    ally.fighter.hp = min(ally.fighter.hp + amount, ally.max_hp)
                    flavor = f"{member.name} uses {spec['name']} on {ally.name}! Restores {amount} HP."
                    print(f"  {flavor}", flush=True)
                    history[member.name] = f"used {spec['name']} on {ally.name} — healed {amount} HP"
                    _emit_action(emit, member.name, "HEAL", ally.name,
                                 {"outcome": "heal", "damage": amount},
                                 flavor, enemy, party, battle_sleep_ms)

                elif effect == "buff_damage":
                    ally  = _pick_ally(party, target, member)
                    mult  = spec.get("buff_mult", 1.15)
                    turns = spec.get("buff_turns", 2)
                    ally.buffs = [b for b in ally.buffs if b["name"] != "TICKLED"]
                    ally.buffs.append({"name": "TICKLED", "damage_mult": mult, "turns_remaining": turns})
                    pct  = round((mult - 1.0) * 100)
                    flavor = f"{member.name} uses {spec['name']}! {ally.name} is TICKLED! +{pct}% dmg for {turns} turns."
                    print(f"  {flavor}", flush=True)
                    history[member.name] = (
                        f"used {spec['name']} on {ally.name} — TICKLED (+{pct}% dmg, {turns} turns)")
                    _emit_action(emit, member.name, "BUFF", ally.name,
                                 {"outcome": "buff"}, flavor, enemy, party, battle_sleep_ms)

                else:
                    flavor = f"{member.name} uses {spec['name']}!"
                    print(f"  {flavor}", flush=True)
                    history[member.name] = f"used {spec['name']}"
                    _emit_action(emit, member.name, "SPECIAL", enemy.name,
                                 {"outcome": "special"}, flavor, enemy, party, battle_sleep_ms)

                member.special_cooldown = SPECIAL_COOLDOWNS.get(spec.get("name", ""), 0)

            elif action == "RUN":
                success, chance = try_run(run_attempts, rng)
                run_attempts += 1
                if success:
                    flavor = f"{member.name} flees! THE PARTY ESCAPES!"
                    print(f"  {flavor}", flush=True)
                    journal.log_event({"type": "BATTLE_FLEE", "tick": tick})
                    _emit_action(emit, member.name, "RUN", "", {"outcome": "flee"},
                                 flavor, enemy, party, 0)
                    _emit_end(emit, "flee", tick, enemy.name)
                    return {"outcome": "flee", "rounds": tick}
                else:
                    flavor = f"{member.name} tries to run... fails. ({chance:.0%})"
                    print(f"  {flavor}", flush=True)
                    history[member.name] = "attempted to flee — failed"
                    _emit_action(emit, member.name, "RUN", "",
                                 {"outcome": "flee_fail"}, flavor, enemy, party, battle_sleep_ms)

            _tick_buffs(member)

            if not enemy.alive:
                print(f"\n  *** {enemy.name} IS DEFEATED! ***\n", flush=True)
                journal.log_event({"type": "ENEMY_KILLED", "tick": tick,
                                   "enemy": enemy.name, "enemy_lvl": enemy.lvl})
                journal.log_event({"type": "BATTLE_WIN", "tick": tick,
                                   "enemy": enemy.name, "enemy_lvl": enemy.lvl})
                _emit_end(emit, "win", tick, enemy.name)
                return {"outcome": "win", "rounds": tick}

        living = [m for m in party if m.alive]
        if not living:
            break

        # ── Enemy turn ────────────────────────────────────────────────────────
        enemy.fighter.defending = False

        if enemy.status == "MESMERIZED":
            _drop = [0.10, 0.25, 0.50, 0.80]
            drop_chance = _drop[min(enemy.status_turns, len(_drop) - 1)]
            if rng.random() < drop_chance:
                enemy.status = None
                enemy.status_turns = 0
                target_member = rng.choice(living)
                res = resolve_attack(enemy.fighter, target_member.fighter, rng)
                flavor = f"{enemy.name} snaps out of MESMERIZE! " + _fmt_attack(res).strip()
                print(f"  {flavor}", flush=True)
                history[enemy.name] = f"snapped out of MESMERIZE — attacked {target_member.name} — {_fmt_brief(res)}"
                _emit_action(emit, enemy.name, "ATTACK", target_member.name,
                             res, flavor, enemy, party, battle_sleep_ms)
            elif rng.random() < 0.5:
                enemy.status_turns += 1
                flavor = f"{enemy.name} is MESMERIZED! Cannot act."
                print(f"  {flavor}", flush=True)
                history[enemy.name] = "MESMERIZED — could not act"
                _emit_action(emit, enemy.name, "STATUS", "",
                             {"outcome": "mesmerized"}, flavor, enemy, party, battle_sleep_ms)
            else:
                enemy.status_turns += 1
                target_member = rng.choice(living)
                res = resolve_attack(enemy.fighter, target_member.fighter, rng)
                flavor = _fmt_attack(res).strip() + " (MESMERIZED)"
                print(f"  {flavor}", flush=True)
                history[enemy.name] = f"attacked {target_member.name} — {_fmt_brief(res)} (still MESMERIZED)"
                _emit_action(emit, enemy.name, "ATTACK", target_member.name,
                             res, flavor, enemy, party, battle_sleep_ms)

        elif enemy.status == "CRINGE":
            if rng.random() < 0.35:
                self_dmg = rng.randint(2, 5)
                enemy.fighter.hp -= self_dmg
                flavor = f"{enemy.name} is CRINGE-d! Attacks itself for {self_dmg}!"
                print(f"  {flavor}", flush=True)
                history[enemy.name] = f"CRINGE — attacked itself for {self_dmg} damage"
                _emit_action(emit, enemy.name, "SELF_DAMAGE", enemy.name,
                             {"outcome": "self_damage", "damage": self_dmg},
                             flavor, enemy, party, battle_sleep_ms)
            else:
                target_member = rng.choice(living)
                res = resolve_attack(enemy.fighter, target_member.fighter, rng)
                flavor = _fmt_attack(res).strip() + " (CRINGE active)"
                print(f"  {flavor}", flush=True)
                history[enemy.name] = f"attacked {target_member.name} — {_fmt_brief(res)} (CRINGE active)"
                _emit_action(emit, enemy.name, "ATTACK", target_member.name,
                             res, flavor, enemy, party, battle_sleep_ms)
            enemy.status_duration -= 1
            if enemy.status_duration <= 0:
                enemy.status = None
                enemy.status_duration = None
                print(f"  {enemy.name} is CRINGING no more!", flush=True)

        else:
            target_member = rng.choice(living)
            res = resolve_attack(enemy.fighter, target_member.fighter, rng)
            flavor = _fmt_attack(res).strip()
            print(f"  {flavor}", flush=True)
            history[enemy.name] = f"attacked {target_member.name} — {_fmt_brief(res)}"
            _emit_action(emit, enemy.name, "ATTACK", target_member.name,
                         res, flavor, enemy, party, battle_sleep_ms)

        # Check enemy killed by parry reflection
        if not enemy.alive:
            print(f"\n  *** {enemy.name} IS DEFEATED! (parry) ***\n", flush=True)
            journal.log_event({"type": "ENEMY_KILLED", "tick": tick,
                               "enemy": enemy.name, "enemy_lvl": enemy.lvl})
            journal.log_event({"type": "BATTLE_WIN", "tick": tick,
                               "enemy": enemy.name, "enemy_lvl": enemy.lvl})
            _emit_end(emit, "win", tick, enemy.name)
            return {"outcome": "win", "rounds": tick}

        for member in party:
            if member.name in alive_set and not member.alive:
                alive_set.discard(member.name)
                journal.log_event({"type": "MEMBER_DIED", "tick": tick, "member": member.name})
                print(f"  *** {member.name} HAS FALLEN! ***", flush=True)

        if not any(m.alive for m in party):
            break

        tick += 1
        if tick >= MAX_ROUNDS:
            print(f"\n  *** BATTLE TIMED OUT AFTER {MAX_ROUNDS} ROUNDS. ***\n", flush=True)
            _emit_end(emit, "timeout", tick, enemy.name)
            return {"outcome": "timeout", "rounds": tick}

    # Party wipe
    journal.log_event({"type": "BATTLE_LOSS", "tick": tick})
    print("\n  *** THE PARTY HAS FALLEN. ***\n", flush=True)
    _emit_end(emit, "loss", tick, enemy.name)
    return {"outcome": "loss", "rounds": tick}
