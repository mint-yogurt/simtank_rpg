import tempfile
import unittest
from pathlib import Path

from engine.renderer import load_map_objects


def _raw_map(objects: list[dict]) -> dict:
    return {"layers": [{"type": "objectgroup", "objects": objects}]}


class TestLoadMapObjectsTrigger(unittest.TestCase):
    """A `trigger`-type object's own `cutscene_id` comes from
    obj_<map>.yaml, same YAML-merge path every other object type already
    goes through (see engine.renderer.load_map_objects) -- not exercised
    anywhere else, since the Phase 2 cutscene work only ever hand-built
    MapObject instances directly in ad hoc verification scripts."""

    def test_trigger_object_gets_cutscene_id_from_yaml(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_dir = Path(tmp)
            (map_dir / "obj_testmap.yaml").write_text(
                "5:\n  name: door_trigger\n  type: trigger\n  cutscene_id: intro_scene\n"
            )
            raw = _raw_map([
                {"id": 5, "name": "door_trigger", "type": "trigger", "x": 32, "y": 48, "width": 16, "height": 16},
            ])
            objects = load_map_objects(raw, tile_px=16, map_dir=map_dir, map_name="testmap")
            self.assertEqual(len(objects), 1)
            trigger = objects[0]
            self.assertEqual(trigger.type, "trigger")
            self.assertEqual(trigger.cutscene_id, "intro_scene")
            # rectangle-tool anchoring (no `gid`) -- top-left, same rule as enemy/spawner
            self.assertEqual((trigger.row, trigger.col), (3, 2))

    def test_trigger_object_with_no_yaml_entry_has_none_cutscene_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            map_dir = Path(tmp)
            raw = _raw_map([
                {"id": 7, "name": "unfilled_trigger", "type": "trigger", "x": 0, "y": 0, "width": 16, "height": 16},
            ])
            objects = load_map_objects(raw, tile_px=16, map_dir=map_dir, map_name="testmap")
            self.assertIsNone(objects[0].cutscene_id)


if __name__ == "__main__":
    unittest.main()
