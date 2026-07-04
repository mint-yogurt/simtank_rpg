"""Dev entry point: run the loop, print to terminal."""

import json
import logging
import random
from dataclasses import dataclass, field
from pathlib import Path

from engine.combat import Fighter, hit_chance, resolve_attack, try_run
from engine.journal import Journal
from llm.client import ask
from llm.prompts import build_battle_context, build_system_prompt
from llm.schema import parse_action

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# Data containers — wrap Fighter with character-sheet fields + battle state
# ---------------------------------------------------------------------------

@dataclass
class BattleMember:
    fighter: Fighter
    personality: str
    special: dict
    xp: int = 0
    status: str | None = None
    buffs: list = field(default_factory=list)  # [{"name", "damage_mult", "turns_remaining"}]

    @property
    def name(self): return self.fighter.name
    @property
    def hp(self): return max(0, self.fighter.hp)
    @property
    def max_hp(self): return self.fighter.max_hp
    @property
    def lvl(self): return self.fighter.level
    @property
    def alive(self): return self.fighter.alive


@dataclass
class BattleEnemy:
    fighter: Fighter
    status: str | None = None
    status_turns: int = 0     # turns active (used by escalating drop-chance statuses)
    status_duration: int | None = None  # fixed turns remaining (used by CRINGE etc.)

    @property
    def name(self): return self.fighter.name
    @property
    def hp(self): return max(0, self.fighter.hp)
    @property
    def max_hp(self): return self.fighter.max_hp
    @property
    def lvl(self): return self.fighter.level
    @property
    def alive(self): return self.fighter.alive


# ---------------------------------------------------------------------------
# Loaders
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
# Announcement formatting
# ---------------------------------------------------------------------------

def _fmt_attack(res: dict) -> str:
    a, d, crit = res["attacker"], res["defender"], " CRIT!" if res["crit"] else ""
    o = res["outcome"]
    if o == "miss":    return f"  {a} swings at {d}... MISS."
    if o == "parry":   return f"  {a} swings at {d}... PARRIED! {d} reflects {res['reflected']} back.{crit}"
    if o == "glance":  return f"  {a} grazes {d} for {res['damage']}.{crit}"
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
    """Normalize special target field: '' and None both mean ally."""
    t = spec.get("target")
    return "ally" if t in (None, "") else t


def _apply_damage_buffs(member: "BattleMember", res: dict, enemy: "BattleEnemy") -> None:
    """Post-hoc: apply active damage buffs to a resolved attack. Mutates enemy HP and res."""
    if res["damage"] <= 0:
        return
    for buff in member.buffs:
        mult = buff.get("damage_mult", 1.0)
        if mult > 1.0:
            extra = max(0, round(res["damage"] * (mult - 1.0)))
            enemy.fighter.hp -= extra
            res["damage"] += extra


def _tick_buffs(member: "BattleMember") -> None:
    """Decrement buff turns at end of a member's action; remove expired ones."""
    member.buffs = [
        dict(b, turns_remaining=b["turns_remaining"] - 1)
        for b in member.buffs
    ]
    member.buffs = [b for b in member.buffs if b["turns_remaining"] > 0]


def special_SING(
    member: "BattleMember", enemy: "BattleEnemy", spec: dict,
    rng: random.Random, history: dict,
) -> None:
    hit = rng.random() < hit_chance(member.fighter)
    print(f"  {member.name} SINGS to {enemy.name}!", flush=True)
    if hit and enemy.alive and not enemy.status:
        enemy.status = spec["status_effect"]
        enemy.status_turns = 0
        enemy.status_duration = None
        print(f"  {enemy.name} is MESMERIZED!", flush=True)
        history[member.name] = f"SANG to {enemy.name} — inflicted MESMERIZED"
    else:
        print(f"  IT HAD NO EFFECT ON {enemy.name}!", flush=True)
        history[member.name] = f"SANG to {enemy.name} — no effect"


def special_LAUGH(
    member: "BattleMember", enemy: "BattleEnemy", spec: dict,
    rng: random.Random, history: dict,
) -> None:
    hit = rng.random() < hit_chance(member.fighter)
    print(f"  {member.name} LAUGHS at {enemy.name}!", flush=True)
    if hit and enemy.alive and not enemy.status:
        enemy.status = "CRINGE"
        enemy.status_turns = 0
        enemy.status_duration = rng.randint(
            spec.get("status_duration_min", 3),
            spec.get("status_duration_max", 5),
        )
        print(f"  {enemy.name} is CRINGING!", flush=True)
        history[member.name] = f"LAUGHED at {enemy.name} — inflicted CRINGE"
    else:
        print(f"  IT HAD NO EFFECT ON {enemy.name}!", flush=True)
        history[member.name] = f"LAUGHED at {enemy.name} — no effect"


def _pick_ally(party: list, target_name: str | None, fallback: "BattleMember") -> "BattleMember":
    return next(
        (m for m in party if m.name == target_name and m.alive),
        next((m for m in party if m.alive), fallback),
    )


# ---------------------------------------------------------------------------
# Battle loop
# ---------------------------------------------------------------------------

MAX_ROUNDS = 100


def run_battle(party: list[BattleMember], enemy: BattleEnemy, rng: random.Random) -> None:
    journal = Journal()
    history: dict[str, str] = {}
    run_attempts = 0
    tick = 0
    alive_set = {m.name for m in party}

    print(f"\n=== {', '.join(m.name for m in party)}  VS  {enemy.name} (LVL {enemy.lvl}) ===\n",
          flush=True)

    while True:
        print(f"--- ROUND {tick + 1} ---", flush=True)

        # --- Party turns ---
        for member in party:
            if not member.alive or not enemy.alive:
                continue

            member.fighter.defending = False

            print(f"  [{member.name} thinking...]", flush=True)
            ctx = build_battle_context(member, party, enemy, history, run_attempts)
            sys_prompt = build_system_prompt(member)
            raw = ask(ctx, sys_prompt)

            party_names = [m.name for m in party]
            dec = parse_action(raw, member.special.get("target"), enemy.name, party_names,
                               special_name=member.special.get("name", ""))
            action, target = dec["action"], dec["target"]

            # If special is unconfigured, treat as ATTACK
            if action == "SPECIAL" and not member.special.get("name"):
                action = "ATTACK"

            if action == "ATTACK":
                res = resolve_attack(member.fighter, enemy.fighter, rng)
                _apply_damage_buffs(member, res, enemy)
                print(_fmt_attack(res), flush=True)
                history[member.name] = f"attacked {enemy.name} — {_fmt_brief(res)}"

            elif action == "DEFEND":
                member.fighter.defending = True
                print(f"  {member.name} takes a defensive stance.", flush=True)
                history[member.name] = "defended"

            elif action == "SPECIAL":
                spec = member.special
                ttype = _spec_target_type(spec)
                effect_type = spec.get("effect_type")

                if spec.get("name") == "SING":
                    special_SING(member, enemy, spec, rng, history)

                elif spec.get("name") == "LAUGH":
                    special_LAUGH(member, enemy, spec, rng, history)

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
                                spec.get("status_duration_max", 5),
                            )
                        else:
                            enemy.status_duration = None
                    status_applied = enemy.status and enemy.status != status_before
                    print(f"  {member.name} uses {spec['name']}! {_fmt_attack(res).strip()}",
                          flush=True)
                    if status_applied:
                        print(f"  {enemy.name} is {enemy.status}!", flush=True)
                    history[member.name] = (
                        f"used {spec['name']} on {enemy.name} — {_fmt_brief(res)}"
                        + (f" — inflicted {enemy.status}" if status_applied else "")
                    )

                elif effect_type == "heal":
                    ally = _pick_ally(party, target, member)
                    pct = rng.uniform(spec.get("heal_pct_min", 0.15), spec.get("heal_pct_max", 0.25))
                    amount = max(1, round(ally.max_hp * pct))
                    ally.fighter.hp = min(ally.fighter.hp + amount, ally.max_hp)
                    print(f"  {member.name} uses {spec['name']} on {ally.name}! "
                          f"Restores {amount} HP.", flush=True)
                    history[member.name] = f"used {spec['name']} on {ally.name} — healed {amount} HP"

                elif effect_type == "buff_damage":
                    ally = _pick_ally(party, target, member)
                    mult = spec.get("buff_mult", 1.15)
                    turns = spec.get("buff_turns", 2)
                    ally.buffs = [b for b in ally.buffs if b["name"] != "TICKLED"]
                    ally.buffs.append({"name": "TICKLED", "damage_mult": mult, "turns_remaining": turns})
                    pct = round((mult - 1.0) * 100)
                    print(f"  {member.name} uses {spec['name']} on {ally.name}! "
                          f"{ally.name} is TICKLED! +{pct}% damage for {turns} turns.", flush=True)
                    history[member.name] = (
                        f"used {spec['name']} on {ally.name} — TICKLED (+{pct}% dmg, {turns} turns)"
                    )

                else:
                    print(f"  {member.name} uses {spec['name']}!", flush=True)
                    history[member.name] = f"used {spec['name']}"

            elif action == "RUN":
                success, chance = try_run(run_attempts, rng)
                run_attempts += 1
                if success:
                    print(f"  {member.name} flees! THE PARTY ESCAPES!", flush=True)
                    journal.log_event({"type": "BATTLE_FLEE", "tick": tick})
                    _print_journal(journal)
                    return
                else:
                    print(f"  {member.name} tries to run... fails. ({chance:.0%} chance)",
                          flush=True)
                    history[member.name] = "attempted to flee — failed"

            _tick_buffs(member)

            # Check enemy death after each member's action
            if not enemy.alive:
                print(f"\n  *** {enemy.name} IS DEFEATED! ***\n", flush=True)
                journal.log_event({
                    "type": "ENEMY_KILLED", "tick": tick,
                    "enemy": enemy.name, "enemy_lvl": enemy.lvl,
                })
                journal.log_event({
                    "type": "BATTLE_WIN", "tick": tick,
                    "enemy": enemy.name, "enemy_lvl": enemy.lvl,
                })
                _print_journal(journal)
                return

        # Abort early if no living targets remain (shouldn't happen mid-party-turn, but guard it)
        living = [m for m in party if m.alive]
        if not living:
            break

        # --- Enemy turn ---
        enemy.fighter.defending = False

        if enemy.status == "MESMERIZED":
            _drop_chances = [0.10, 0.25, 0.50, 0.80]
            drop_chance = _drop_chances[min(enemy.status_turns, len(_drop_chances) - 1)]

            if rng.random() < drop_chance:
                enemy.status = None
                enemy.status_turns = 0
                print(f"  {enemy.name} snaps out of MESMERIZE!", flush=True)
                target_member = rng.choice(living)
                res = resolve_attack(enemy.fighter, target_member.fighter, rng)
                print(_fmt_attack(res), flush=True)
                history[enemy.name] = (
                    f"snapped out of MESMERIZE — attacked {target_member.name} — {_fmt_brief(res)}"
                )
            elif rng.random() < 0.5:
                enemy.status_turns += 1
                print(f"  {enemy.name} is MESMERIZED! Cannot act this turn.", flush=True)
                history[enemy.name] = "MESMERIZED — could not act"
            else:
                enemy.status_turns += 1
                target_member = rng.choice(living)
                res = resolve_attack(enemy.fighter, target_member.fighter, rng)
                print(_fmt_attack(res), flush=True)
                history[enemy.name] = (
                    f"attacked {target_member.name} — {_fmt_brief(res)} (still MESMERIZED)"
                )

        elif enemy.status == "CRINGE":
            if rng.random() < 0.35:
                self_dmg = rng.randint(2, 5)
                enemy.fighter.hp -= self_dmg
                print(f"  {enemy.name} is CRINGE-d! Attacks itself for {self_dmg}!", flush=True)
                history[enemy.name] = f"CRINGE — attacked itself for {self_dmg} damage"
            else:
                target_member = rng.choice(living)
                res = resolve_attack(enemy.fighter, target_member.fighter, rng)
                print(_fmt_attack(res), flush=True)
                history[enemy.name] = (
                    f"attacked {target_member.name} — {_fmt_brief(res)} (CRINGE active)"
                )
            # Count down fixed duration
            enemy.status_duration -= 1
            if enemy.status_duration <= 0:
                enemy.status = None
                enemy.status_duration = None
                print(f"  {enemy.name} is CRINGING no more!", flush=True)

        else:
            target_member = rng.choice(living)
            res = resolve_attack(enemy.fighter, target_member.fighter, rng)
            print(_fmt_attack(res), flush=True)
            history[enemy.name] = f"attacked {target_member.name} — {_fmt_brief(res)}"

        # Check for deaths caused by enemy this turn (including parry reflection on enemy)
        if not enemy.alive:
            print(f"\n  *** {enemy.name} IS DEFEATED! (parry reflection) ***\n", flush=True)
            journal.log_event({
                "type": "ENEMY_KILLED", "tick": tick,
                "enemy": enemy.name, "enemy_lvl": enemy.lvl,
            })
            journal.log_event({
                "type": "BATTLE_WIN", "tick": tick,
                "enemy": enemy.name, "enemy_lvl": enemy.lvl,
            })
            _print_journal(journal)
            return

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
            _print_journal(journal)
            return

    # Party wipe
    journal.log_event({"type": "BATTLE_LOSS", "tick": tick})
    print("\n  *** THE PARTY HAS FALLEN. ***\n", flush=True)
    _print_journal(journal)


def _print_journal(journal: Journal) -> None:
    lines = journal.render_recent(20)
    if lines:
        print("\n" + "=" * 50, flush=True)
        for line in lines:
            print(line, flush=True)


# ---------------------------------------------------------------------------
# Entry point
# ---------------------------------------------------------------------------

def main() -> None:
    seed = random.randint(0, 2**32 - 1)
    rng = random.Random(seed)
    print(f"SEED: {seed}", flush=True)

    party = load_party(Path(__file__).parent / "data" / "party")

    enemy_fighter = Fighter(
        "GORBUSHUS", iq=60, weight=180, sweat=4, hair=0, level=1, is_enemy=True
    )
    enemy = BattleEnemy(enemy_fighter)

    run_battle(party, enemy, rng)


if __name__ == "__main__":
    main()
