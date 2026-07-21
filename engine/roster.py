"""Live party-member stats (HP/MP/XP/level) — pure logic, no pygame.

Mirrors engine.inventory's split: data/party/<name>.json holds each
character's full sheet (personality/special/iq/weight/sweat/hair/etc.),
read fresh from disk wherever that flavor data is needed (see
engine.battle.load_fighter/fighter_from_roster) -- this module only tracks
the runtime-mutable subset that a battle can actually change: lvl, hp,
max_hp, xp, mp, max_mp. That's the one piece of a character sheet that
needs to round-trip through a save file.

Only MELVIN fights right now (engine.battle's module docstring -- battle
stays 1v1, single-Fighter scope), but every data/party/*.json already
exists fully authored, so fresh() loads all of them -- costs nothing extra,
and matches engine.player.Player.sheet_name's own "swap the lead character
in future" framing without inventing any new mechanic. Loading everyone
into this Roster is not the same as being in the active party, though --
see current_party() below -- so screens that list "the party" (equip
targets, the PARTY status screen) never iterate self.members directly.

Leveling (engine.battle.xp_to_next_level/apply_level_ups) increments
lvl/max_hp/max_mp in place on a PartyMember once xp crosses a threshold.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

from engine.game_state import GameState

_PARTY_DIR = Path(__file__).parent.parent / "data" / "party"


@dataclass
class PartyMember:
    name:    str
    lvl:     int
    hp:      int
    max_hp:  int
    xp:      int
    mp:      int
    max_mp:  int
    equipped_weapon: str | None = None
    equipped_armour: str | None = None


def _load_member(path: Path) -> PartyMember:
    s = json.loads(path.read_text())
    return PartyMember(name=s["name"], lvl=s["lvl"], hp=s["hp"], max_hp=s["max_hp"],
                        xp=s["xp"], mp=s["mp"], max_mp=s["max_mp"])


@dataclass
class Roster:
    """Every party member's live stats, keyed by name (e.g. "MELVIN")."""
    members: dict[str, PartyMember] = field(default_factory=dict)

    @classmethod
    def fresh(cls, party_dir: Path = _PARTY_DIR) -> "Roster":
        """A brand-new game's starting roster -- every data/party/*.json's
        own lvl/hp/max_hp/xp/mp/max_mp, unmodified."""
        members = {}
        for path in sorted(party_dir.glob("*.json")):
            member = _load_member(path)
            members[member.name] = member
        return cls(members=members)

    def get(self, name: str) -> PartyMember:
        return self.members[name]

    def current_party(self, game_state: GameState) -> list[PartyMember]:
        """Every member currently in the active party -- filtered against
        engine.game_state.GameState.party_members, since every
        data/party/*.json loads into this roster regardless of whether
        that character has actually joined yet (see fresh()). This is the
        one place equip-target/heal-target pickers, the inventory's
        equipped-item greying check, and the PARTY screen's member list
        all go through, so a future recruit mechanic (adding a name to
        game_state.party_members) is reflected everywhere without
        touching any of those call sites. MELVIN is always present in the
        default, so this list is never empty today -- callers (e.g.
        PartyMenu's modulo wrap) rely on that."""
        return [self.members[name] for name in game_state.party_members if name in self.members]

    def to_dict(self) -> dict:
        return {name: {"lvl": m.lvl, "hp": m.hp, "max_hp": m.max_hp,
                        "xp": m.xp, "mp": m.mp, "max_mp": m.max_mp,
                        "equipped_weapon": m.equipped_weapon,
                        "equipped_armour": m.equipped_armour}
                for name, m in self.members.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "Roster":
        """Starts from fresh() and overlays whatever `d` has on top, so a
        save file predating a new party member (or missing the whole
        "roster" key, for saves written before this existed) doesn't
        KeyError -- the missing member just keeps their fresh() defaults.
        Same reasoning covers equipped_weapon/equipped_armour, which didn't
        exist in earlier saves -- .get(..., None) leaves those members
        unequipped rather than erroring."""
        roster = cls.fresh()
        for name, fields in d.items():
            if name not in roster.members:
                continue
            member = roster.members[name]
            member.lvl    = fields.get("lvl", member.lvl)
            member.hp     = fields.get("hp", member.hp)
            member.max_hp = fields.get("max_hp", member.max_hp)
            member.xp     = fields.get("xp", member.xp)
            member.mp     = fields.get("mp", member.mp)
            member.max_mp = fields.get("max_mp", member.max_mp)
            member.equipped_weapon = fields.get("equipped_weapon", member.equipped_weapon)
            member.equipped_armour = fields.get("equipped_armour", member.equipped_armour)
        return roster
