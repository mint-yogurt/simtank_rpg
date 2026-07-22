import unittest

from engine.roster import PartyMember, Roster


class TestRosterFresh(unittest.TestCase):
    def test_fresh_loads_every_party_json(self):
        roster = Roster.fresh()
        self.assertEqual(set(roster.members.keys()), {"MELVIN", "BILLY", "POOTS", "SMELTRUD"})

    def test_fresh_melvin_matches_data_party_json(self):
        melvin = Roster.fresh().get("MELVIN")
        self.assertEqual(melvin.lvl, 1)
        self.assertEqual(melvin.hp, 26)
        self.assertEqual(melvin.max_hp, 26)
        self.assertEqual(melvin.xp, 0)
        self.assertEqual(melvin.mp, 20)
        self.assertEqual(melvin.max_mp, 20)


class TestRosterRoundTrip(unittest.TestCase):
    def test_to_dict_from_dict_round_trip(self):
        roster = Roster.fresh()
        roster.get("MELVIN").hp = 7
        roster.get("MELVIN").xp = 42
        d = roster.to_dict()
        restored = Roster.from_dict(d)
        self.assertEqual(restored.get("MELVIN").hp, 7)
        self.assertEqual(restored.get("MELVIN").xp, 42)
        # Untouched members still round-trip too.
        self.assertEqual(restored.get("BILLY").hp, 25)

    def test_from_dict_with_missing_member_overlays_onto_fresh_defaults(self):
        # Save file predates a party member (or was written before roster
        # existed at all) -- from_dict must not KeyError, missing members
        # just keep their fresh() defaults.
        restored = Roster.from_dict({"MELVIN": {"hp": 3}})
        self.assertEqual(restored.get("MELVIN").hp, 3)
        self.assertEqual(restored.get("BILLY").hp, 25)   # fresh() default, untouched

    def test_from_dict_empty_means_every_member_is_fresh(self):
        restored = Roster.from_dict({})
        self.assertEqual(restored.get("MELVIN").hp, 26)

    def test_from_dict_partial_fields_only_overrides_given_ones(self):
        restored = Roster.from_dict({"MELVIN": {"xp": 99}})
        member = restored.get("MELVIN")
        self.assertEqual(member.xp, 99)
        self.assertEqual(member.hp, 26)   # not in the dict -- stays fresh() default


class TestPartyMember(unittest.TestCase):
    def test_construct_directly(self):
        member = PartyMember(name="MELVIN", lvl=5, hp=10, max_hp=50, xp=100, mp=5, max_mp=30)
        self.assertEqual(member.lvl, 5)
        self.assertEqual(member.max_hp, 50)


if __name__ == "__main__":
    unittest.main()
