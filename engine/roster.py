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
in future" framing without inventing any new mechanic.

No leveling formula lives here or anywhere else yet -- xp accumulates as a
tracked stat, lvl stays exactly what data/party/<name>.json says until a
future pass designs the actual curve/thresholds.
"""

import json
from dataclasses import dataclass, field
from pathlib import Path

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

    def to_dict(self) -> dict:
        return {name: {"lvl": m.lvl, "hp": m.hp, "max_hp": m.max_hp,
                        "xp": m.xp, "mp": m.mp, "max_mp": m.max_mp}
                for name, m in self.members.items()}

    @classmethod
    def from_dict(cls, d: dict) -> "Roster":
        """Starts from fresh() and overlays whatever `d` has on top, so a
        save file predating a new party member (or missing the whole
        "roster" key, for saves written before this existed) doesn't
        KeyError -- the missing member just keeps their fresh() defaults."""
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
        return roster
