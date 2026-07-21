import unittest

from engine.game_state import GameState


class TestGold(unittest.TestCase):
    def test_default_zero(self):
        self.assertEqual(GameState().gold, 0)

    def test_add_gold(self):
        gs = GameState()
        gs.add_gold(10)
        gs.add_gold(5)
        self.assertEqual(gs.gold, 15)

    def test_spend_gold_sufficient(self):
        gs = GameState()
        gs.add_gold(10)
        self.assertTrue(gs.spend_gold(5))
        self.assertEqual(gs.gold, 5)

    def test_spend_gold_insufficient_leaves_balance_unchanged(self):
        gs = GameState()
        gs.add_gold(5)
        self.assertFalse(gs.spend_gold(10))
        self.assertEqual(gs.gold, 5)

    def test_round_trips_through_save_dict(self):
        gs = GameState()
        gs.add_gold(42)
        restored = GameState.from_dict(gs.to_dict())
        self.assertEqual(restored.gold, 42)


if __name__ == "__main__":
    unittest.main()
