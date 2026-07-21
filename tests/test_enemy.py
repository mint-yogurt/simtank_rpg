import tempfile
import unittest
from pathlib import Path

from engine.enemy import load_enemy_defs

_MINIMAL_ENTRY = """
some_enemy:
  name: "SOME ENEMY"
  sprite: some_enemy
  battle_bg: static
  iq: 50
  weight: 100
  sweat: 5
  hair: 5
  level: 1
  move_speed: 1.0
  behavior: wanderer
"""

_FULL_ENTRY = """
some_enemy:
  name: "SOME ENEMY"
  sprite: some_enemy
  battle_bg: static
  iq: 50
  weight: 100
  sweat: 5
  hair: 5
  level: 1
  gold: 5
  xp: 8
  drop_item: greasy_napkin
  drop_chance: 0.25
  move_speed: 1.0
  behavior: wanderer
"""


def _load(yaml_text: str):
    with tempfile.NamedTemporaryFile("w", suffix=".yaml", delete=False) as f:
        f.write(yaml_text)
        path = Path(f.name)
    try:
        return load_enemy_defs(path)
    finally:
        path.unlink()


class TestEnemyDefRewardFields(unittest.TestCase):
    def test_omitted_reward_fields_default_to_none_and_zero(self):
        defs = _load(_MINIMAL_ENTRY)
        edef = defs["some_enemy"]
        self.assertIsNone(edef.xp)
        self.assertIsNone(edef.drop_item)
        self.assertEqual(edef.drop_chance, 0.0)

    def test_full_reward_fields_parse(self):
        defs = _load(_FULL_ENTRY)
        edef = defs["some_enemy"]
        self.assertEqual(edef.xp, 8)
        self.assertEqual(edef.drop_item, "greasy_napkin")
        self.assertEqual(edef.drop_chance, 0.25)


class TestRealEnemiesYaml(unittest.TestCase):
    """The actual data/enemy/enemies.yaml -- confirms every current entry
    still parses cleanly with the new fields added."""

    def test_real_file_loads_with_reward_fields(self):
        defs = load_enemy_defs()
        self.assertGreater(len(defs), 0)
        for edef in defs.values():
            self.assertIsInstance(edef.drop_chance, float)


if __name__ == "__main__":
    unittest.main()
