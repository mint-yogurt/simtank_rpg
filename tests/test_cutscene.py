import tempfile
import unittest
from pathlib import Path

from engine.cutscene import (
    CutsceneChoice,
    CutsceneDef,
    CutscenePlayer,
    CutsceneStep,
    CutsceneTrigger,
    load_cutscene,
    load_cutscene_defs,
    trigger_matches,
)


class TestLoadCutscene(unittest.TestCase):
    def test_parses_steps_and_trigger(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "intro.yaml"
            path.write_text("""
id: intro
map: hub_fronthouse
trigger:
  event: map_load
  when: [has_key]
  unless: ["cutscene_seen:intro"]
steps:
  - face: {actor: player, dir: N}
  - wait: {ms: 500}
  - dialogue: {pages: ["Hello."]}
""")
            cutscene = load_cutscene(path)
            self.assertEqual(cutscene.id, "intro")
            self.assertEqual(cutscene.map, "hub_fronthouse")
            self.assertEqual(cutscene.trigger.event, "map_load")
            self.assertEqual(cutscene.trigger.when, ["has_key"])
            self.assertEqual(cutscene.trigger.unless, ["cutscene_seen:intro"])
            self.assertIsNone(cutscene.trigger.actor)
            self.assertEqual(len(cutscene.steps), 3)
            self.assertEqual(cutscene.steps[0], CutsceneStep(kind="face", args={"actor": "player", "dir": "N"}))
            self.assertEqual(cutscene.steps[1].kind, "wait")
            self.assertEqual(cutscene.steps[2].args, {"pages": ["Hello."]})

    def test_npc_talk_trigger_parses_actor(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "talk.yaml"
            path.write_text("""
id: talk
map: hub_fronthouse
trigger:
  event: npc_talk
  actor: wizard
steps: []
""")
            cutscene = load_cutscene(path)
            self.assertEqual(cutscene.trigger.event, "npc_talk")
            self.assertEqual(cutscene.trigger.actor, "wizard")

    def test_no_trigger_is_none(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "notrigger.yaml"
            path.write_text("id: notrigger\nmap: hub_fronthouse\nsteps: []\n")
            cutscene = load_cutscene(path)
            self.assertIsNone(cutscene.trigger)
            self.assertEqual(cutscene.steps, [])

    def test_step_must_be_single_key_mapping(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "bad.yaml"
            path.write_text("""
id: bad
map: hub_fronthouse
steps:
  - face: {actor: player, dir: N}
    wait: {ms: 500}
""")
            with self.assertRaises(ValueError):
                load_cutscene(path)


class TestDialogueChoiceParsing(unittest.TestCase):
    def test_dialogue_step_parses_choices_into_dataclasses(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "branch.yaml"
            path.write_text("""
id: branch
map: hub_fronthouse
steps:
  - dialogue:
      pages: ["Would you like to hear the legend?"]
      choices:
        - label: "Yes"
          then:
            - start_cutscene: {id: legend_flashback}
        - label: "No"
          then:
            - set_flag: {flag: declined_legend}
""")
            cutscene = load_cutscene(path)
            dialogue_step = cutscene.steps[0]
            self.assertEqual(dialogue_step.kind, "dialogue")
            choices = dialogue_step.args["choices"]
            self.assertEqual(len(choices), 2)
            self.assertEqual(choices[0], CutsceneChoice(
                label="Yes", then=[CutsceneStep(kind="start_cutscene", args={"id": "legend_flashback"})]))
            self.assertEqual(choices[1], CutsceneChoice(
                label="No", then=[CutsceneStep(kind="set_flag", args={"flag": "declined_legend"})]))

    def test_dialogue_step_with_no_choices_has_no_choices_key(self):
        with tempfile.TemporaryDirectory() as tmp:
            path = Path(tmp) / "plain.yaml"
            path.write_text("""
id: plain
map: hub_fronthouse
steps:
  - dialogue: {pages: ["Hello."]}
""")
            cutscene = load_cutscene(path)
            self.assertNotIn("choices", cutscene.steps[0].args)


class TestLoadCutsceneDefs(unittest.TestCase):
    def test_loads_every_yaml_keyed_by_id(self):
        with tempfile.TemporaryDirectory() as tmp:
            tmp_path = Path(tmp)
            (tmp_path / "a.yaml").write_text("id: scene_a\nmap: hub_fronthouse\nsteps: []\n")
            (tmp_path / "b.yaml").write_text("id: scene_b\nmap: hub_fronthouse\nsteps: []\n")
            defs = load_cutscene_defs(tmp_path)
            self.assertEqual(set(defs.keys()), {"scene_a", "scene_b"})

    def test_missing_directory_returns_empty(self):
        self.assertEqual(load_cutscene_defs(Path("/no/such/directory/here")), {})


class TestTriggerMatches(unittest.TestCase):
    def test_no_conditions_always_matches(self):
        trigger = CutsceneTrigger(event="map_load")
        self.assertTrue(trigger_matches(trigger, flag=lambda k: False))

    def test_when_must_all_be_true(self):
        trigger = CutsceneTrigger(event="map_load", when=["has_key", "met_wizard"])
        flags = {"has_key": True, "met_wizard": False}
        self.assertFalse(trigger_matches(trigger, flag=flags.get))
        flags["met_wizard"] = True
        self.assertTrue(trigger_matches(trigger, flag=flags.get))

    def test_unless_must_all_be_false(self):
        trigger = CutsceneTrigger(event="map_load", unless=["cutscene_seen:intro"])
        self.assertTrue(trigger_matches(trigger, flag=lambda k: False))
        self.assertFalse(trigger_matches(trigger, flag=lambda k: True))


def _make_player(*kinds: str) -> CutscenePlayer:
    cutscene = CutsceneDef(
        id="test", map="hub_fronthouse", trigger=None,
        steps=[CutsceneStep(kind=k, args={}) for k in kinds],
    )
    return CutscenePlayer(cutscene=cutscene)


class TestCutscenePlayer(unittest.TestCase):
    def test_current_step_starts_at_first(self):
        player = _make_player("wait", "dialogue")
        self.assertEqual(player.current_step().kind, "wait")
        self.assertFalse(player.finished)

    def test_advance_moves_to_next_step(self):
        player = _make_player("wait", "dialogue")
        player.advance()
        self.assertEqual(player.current_step().kind, "dialogue")
        self.assertFalse(player.finished)

    def test_advance_past_last_step_finishes(self):
        player = _make_player("wait")
        player.advance()
        self.assertTrue(player.finished)
        self.assertIsNone(player.current_step())

    def test_empty_cutscene_has_no_current_step(self):
        player = _make_player()
        self.assertIsNone(player.current_step())


if __name__ == "__main__":
    unittest.main()
