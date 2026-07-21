import json
import shutil
import tempfile
import unittest
from pathlib import Path
from unittest import mock

from engine.game_state import GameState
from engine.inventory import Inventory
from engine.player import Player
from engine.roster import Roster


class TestSaveRoundTrip(unittest.TestCase):
    """Patches engine.save._SAVES_DIR to a scratch temp dir so this never
    touches the real saves/ folder."""

    def setUp(self):
        self._tmpdir = tempfile.mkdtemp()
        self._patcher = mock.patch("engine.save._SAVES_DIR", Path(self._tmpdir))
        self._patcher.start()
        import engine.save
        self.save = engine.save

    def tearDown(self):
        self._patcher.stop()
        shutil.rmtree(self._tmpdir, ignore_errors=True)

    def test_round_trip_includes_roster(self):
        player = Player.default()
        inventory = Inventory()
        game_state = GameState()
        roster = Roster.fresh()
        roster.get("MELVIN").hp = 3
        roster.get("MELVIN").xp = 12

        self.save.save_to_slot(1, player, inventory, game_state, roster, "town")
        map_name, p2, inv2, gs2, r2 = self.save.load_from_slot(1)

        self.assertEqual(map_name, "town")
        self.assertEqual(p2.name, player.name)
        self.assertEqual(r2.get("MELVIN").hp, 3)
        self.assertEqual(r2.get("MELVIN").xp, 12)
        self.assertEqual(r2.get("BILLY").hp, 25)   # untouched member round-trips too

    def test_old_format_save_without_roster_key_still_loads(self):
        # Simulates a save file written before "roster" existed.
        data = {
            "map_name": "town",
            "player": Player.default().to_dict(),
            "inventory": Inventory().to_dict(),
            "game_state": GameState().to_dict(),
        }
        path = Path(self._tmpdir) / "slot2.json"
        path.write_text(json.dumps(data))

        map_name, player, inventory, game_state, roster = self.save.load_from_slot(2)
        self.assertEqual(map_name, "town")
        self.assertEqual(roster.get("MELVIN").hp, 25)   # falls back to fresh() defaults


if __name__ == "__main__":
    unittest.main()
