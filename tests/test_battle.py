import unittest

from engine.battle import BattleState, Fighter, try_run


class _FixedRollRNG:
    """Deterministic stand-in for random.Random -- .random() always returns
    the fixed value given at construction, used to test try_run's threshold
    behavior precisely (roll below the computed chance succeeds, at/above
    it fails)."""

    def __init__(self, value: float):
        self._value = value

    def random(self) -> float:
        return self._value

    def randint(self, lo: int, hi: int) -> int:
        return lo


class _AlwaysHitRNG:
    """Deterministic stand-in for random.Random: every roll succeeds (hit,
    crit, glance -- doesn't matter which, all still deal >=1 damage) and
    resolve_level's range roll always lands on the low end. Used instead of
    a seeded real RNG so the win condition fires on the very first attack,
    without depending on engine.battle's hit-chance tuning."""

    def random(self) -> float:
        return 0.0

    def randint(self, lo: int, hi: int) -> int:
        return lo


def _make_battle(enemy_gold=None, enemy_xp=None, enemy_drop_item=None, enemy_drop_chance=0.0) -> BattleState:
    party = Fighter(name="MELVIN", iq=100, weight=500, sweat=5, hair=10,
                     level=10, hp=100, max_hp=100)
    enemy = Fighter(name="RAT", iq=40, weight=90, sweat=1, hair=1,
                     level=1, is_enemy=True, hp=1, max_hp=1)
    return BattleState(party=party, enemy=enemy, rng=_AlwaysHitRNG(), enemy_gold=enemy_gold,
                        enemy_xp=enemy_xp, enemy_drop_item=enemy_drop_item,
                        enemy_drop_chance=enemy_drop_chance)


class TestGoldReward(unittest.TestCase):
    def test_flat_gold_reward_on_win(self):
        battle = _make_battle(enemy_gold=5)
        battle._step_party_attack()
        self.assertEqual(battle.phase, "win")
        self.assertEqual(battle.gold_reward, 5)
        self.assertTrue(any(e["type"] == "GOLD_AWARDED" and e["gold"] == 5
                             for e in battle.journal.get_log()))

    def test_ranged_gold_reward_resolves_within_range(self):
        battle = _make_battle(enemy_gold=[3, 7])
        battle._step_party_attack()
        self.assertEqual(battle.gold_reward, 3)   # _AlwaysHitRNG.randint always picks lo

    def test_no_gold_field_means_no_reward_and_no_event(self):
        battle = _make_battle(enemy_gold=None)
        battle._step_party_attack()
        self.assertEqual(battle.gold_reward, 0)
        self.assertFalse(any(e["type"] == "GOLD_AWARDED" for e in battle.journal.get_log()))


class TestXpReward(unittest.TestCase):
    def test_flat_xp_reward_on_win(self):
        battle = _make_battle(enemy_xp=8)
        battle._step_party_attack()
        self.assertEqual(battle.xp_reward, 8)
        self.assertTrue(any(e["type"] == "XP_AWARDED" and e["xp"] == 8
                             for e in battle.journal.get_log()))

    def test_ranged_xp_reward_resolves_within_range(self):
        battle = _make_battle(enemy_xp=[4, 9])
        battle._step_party_attack()
        self.assertEqual(battle.xp_reward, 4)   # _AlwaysHitRNG.randint always picks lo

    def test_no_xp_field_means_no_reward_and_no_event(self):
        battle = _make_battle(enemy_xp=None)
        battle._step_party_attack()
        self.assertEqual(battle.xp_reward, 0)
        self.assertFalse(any(e["type"] == "XP_AWARDED" for e in battle.journal.get_log()))


class TestItemReward(unittest.TestCase):
    def test_drop_rolls_when_chance_is_nonzero(self):
        # _AlwaysHitRNG.random() always returns 0.0, so any nonzero chance hits.
        battle = _make_battle(enemy_drop_item="greasy_napkin", enemy_drop_chance=0.5)
        battle._step_party_attack()
        self.assertEqual(battle.item_reward, "greasy_napkin")
        self.assertTrue(any(e["type"] == "ITEM_AWARDED" and e["item"] == "greasy_napkin"
                             for e in battle.journal.get_log()))

    def test_zero_chance_never_drops(self):
        battle = _make_battle(enemy_drop_item="greasy_napkin", enemy_drop_chance=0.0)
        battle._step_party_attack()
        self.assertIsNone(battle.item_reward)
        self.assertFalse(any(e["type"] == "ITEM_AWARDED" for e in battle.journal.get_log()))

    def test_no_drop_item_means_no_reward_regardless_of_chance(self):
        battle = _make_battle(enemy_drop_item=None, enemy_drop_chance=1.0)
        battle._step_party_attack()
        self.assertIsNone(battle.item_reward)


class TestRunChance(unittest.TestCase):
    def test_even_level_base_chance_is_40_percent(self):
        _, chance = try_run(10, 10, run_attempts=0, rng=_FixedRollRNG(0.0))
        self.assertAlmostEqual(chance, 0.40)

    def test_higher_party_level_increases_chance(self):
        _, chance = try_run(20, 10, run_attempts=0, rng=_FixedRollRNG(0.0))
        self.assertAlmostEqual(chance, 0.40 + 10 * 0.03)

    def test_lower_party_level_decreases_chance(self):
        _, chance = try_run(5, 15, run_attempts=0, rng=_FixedRollRNG(0.0))
        self.assertAlmostEqual(chance, 0.40 - 10 * 0.03)

    def test_chance_clamped_to_run_min_at_extreme_disadvantage(self):
        _, chance = try_run(1, 60, run_attempts=0, rng=_FixedRollRNG(0.0))
        self.assertEqual(chance, 0.05)

    def test_chance_clamped_to_run_max_at_extreme_advantage(self):
        _, chance = try_run(60, 1, run_attempts=0, rng=_FixedRollRNG(0.0))
        self.assertEqual(chance, 0.95)

    def test_failed_attempts_escalate_chance(self):
        _, chance = try_run(10, 10, run_attempts=2, rng=_FixedRollRNG(0.0))
        self.assertAlmostEqual(chance, 0.40 + 2 * 0.15)

    def test_escalation_still_caps_at_run_max(self):
        _, chance = try_run(10, 10, run_attempts=10, rng=_FixedRollRNG(0.0))
        self.assertEqual(chance, 0.95)

    def test_roll_below_chance_succeeds(self):
        success, _ = try_run(10, 10, run_attempts=0, rng=_FixedRollRNG(0.39))
        self.assertTrue(success)

    def test_roll_at_or_above_chance_fails(self):
        success, _ = try_run(10, 10, run_attempts=0, rng=_FixedRollRNG(0.40))
        self.assertFalse(success)


class TestAttemptRun(unittest.TestCase):
    def test_successful_run_ends_battle_as_fled_no_rewards(self):
        battle = _make_battle()
        battle.rng = _FixedRollRNG(0.0)   # guaranteed success at any chance > 0
        flavor = battle.attempt_run()
        self.assertEqual(battle.phase, "fled")
        self.assertIn("got away", flavor)
        self.assertIsNone(battle.gold_reward)
        self.assertTrue(any(e["type"] == "BATTLE_FLED" for e in battle.journal.get_log()))

    def test_failed_run_costs_the_turn_and_escalates(self):
        battle = _make_battle()
        battle.rng = _FixedRollRNG(0.99)   # guaranteed failure -- above RUN_MAX
        flavor = battle.attempt_run()
        self.assertEqual(battle.phase, "enemy_turn")
        self.assertEqual(battle.run_attempts, 1)
        self.assertIn("couldn't get away", flavor)
        self.assertTrue(any(e["type"] == "RUN_FAILED" for e in battle.journal.get_log()))

    def test_repeated_failed_attempts_keep_escalating(self):
        battle = _make_battle()
        battle.rng = _FixedRollRNG(0.99)
        battle.attempt_run()
        battle.phase = "party_turn"   # pretend the enemy's turn resolved, back to the player
        battle.attempt_run()
        self.assertEqual(battle.run_attempts, 2)


if __name__ == "__main__":
    unittest.main()
