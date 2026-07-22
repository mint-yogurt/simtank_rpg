"""Cutscene definitions + a headless step sequencer -- pure logic, no pygame.

Mirrors the engine.battle split: this module only knows the *shape* of a
cutscene (an ordered list of steps, loaded from YAML) and tracks *which*
step is currently active -- same as engine.battle.BattleState tracks turn
order without knowing how a hit lands on screen. Every actual effect (moving
a sprite, tweening it, opening the dialogue box, swapping a tile's GID,
panning the camera) is executed by engine.renderer.OverworldScene, the only
thing that knows about pygame surfaces, NPC tweens, and Tiled layer grids --
same reasoning CLAUDE.md gives for engine.player/engine.input staying
headless.

A cutscene file (data/cutscenes/<id>.yaml) is a flat step list, each entry a
single-key mapping naming the action plus its arguments -- the same shape
README's `then:` action list sketches, extended with the move/wait/camera
kinds README reserved but never built:

    id: debug_test
    map: interior_fronthouse_1f
    trigger:
      event: map_load
      when: []
      unless: ["cutscene_seen:debug_test"]
    steps:
      - face:       {actor: player, dir: N}
      - move_actor:  {actor: wizard, to: [14, 20]}
      - wait:        {ms: 500}
      - dialogue:    {pages: ["Welcome home."]}
      - set_flag:    {flag: "cutscene_seen:debug_test"}

`trigger` is evaluated by engine.renderer.OverworldScene, at whichever real
game event its own `event` names (see CutsceneTrigger) -- map-load/tile/
npc-talk are all wired up; see OverworldScene._check_cutscene_triggers/
_try_start_tile_trigger and engine.input.handle_a_button's cutscene_defs
param. A cutscene can also always be started directly regardless of its
trigger, e.g. maptest.py's debug picker (OverworldScene.start_cutscene).
The field is named `event`, not `on` -- PyYAML (like npc.yaml's
unquoted-hex-color gotcha, see engine.npc._validated_colors) reads a bare
`on`/`off`/`yes`/`no` YAML key or value as the *boolean* True/False, not the
string "on" -- `on: map_load` would silently parse as `{True: "map_load"}`
and this field would never be found. `event` sidesteps the whole class of
gotcha rather than requiring every author to remember to quote it.

`move_actor`'s `to` must share a row or a column with the actor's current
position at the moment the step runs -- cardinal-only, same rule as every
other kind of movement in this game (see CLAUDE.md). An L-shaped path is
authored as two consecutive move_actor steps, one per leg; a target that
isn't a straight cardinal line from wherever the actor happens to be is an
authoring error the renderer logs and skips past, rather than something
resolved here.

A `dialogue` step's last page can carry a `choices:` list -- once that page
fully reveals, an N/S-cursor + A-confirm response prompt replaces "press A
to close" (see engine.dialogue.DialogueBox.is_showing_choices), same
interaction idiom as every other list menu in this game. Each choice's own
`then:` is a nested step list, same shape as the cutscene's own top-level
`steps:` -- spliced directly into the running cutscene right after the
dialogue step the instant it's confirmed (OverworldScene.
_confirm_dialogue_choice), so a choice's consequences get full ordinary
step handling (multi-frame ones included), not a separate one-shot path:

    - dialogue:
        pages: ["Would you like to hear the legend?"]
        choices:
          - label: "Yes"
            then:
              - start_cutscene: {id: legend_flashback}
          - label: "No"
            then:
              - set_flag: {flag: declined_legend}

`start_cutscene` (only valid targeting the *same* map as the one currently
running -- see the "no mid-cutscene map switching" decision) replaces the
running cutscene with a different one outright, useful both inside a
choice's `then:` and as an ordinary top-level step. This -- a choice
carrying its own direct consequences -- was a deliberate design choice over
the alternative (a choice only sets a flag, and some *later* map_load/
npc_talk trigger elsewhere notices it): it supports an immediate, same-beat
branch ("Yes" instantly kicks off a flashback) that a flag-only design
can't, at the cost of not being an instance of the same generic "trigger
surface" pattern map_load/tile/npc_talk share -- there is no standalone
"dialogue_choice" trigger event; branching lives entirely in the choice's
own `then:`.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_CUTSCENES_DIR = Path(__file__).parent.parent / "data" / "cutscenes"


@dataclass(frozen=True)
class CutsceneTrigger:
    """One cutscene's own `trigger:` block. `when`/`unless` are flag lists
    checked the same way engine.npc.DialogueVariant already checks its own
    `when`/`unless` (see trigger_matches) -- deliberately the same shape, so
    compound conditions ("has the key AND hasn't seen this scene") fall out
    for free with no new condition language. `event` names which surface
    checks it -- not validated here, since matching it to an actual game
    event is that surface's own job (engine.renderer.OverworldScene), not
    this module's:

      "map_load"   -- checked once per map load/warp arrival
                      (OverworldScene._check_cutscene_triggers, called from
                      _load_map); `actor` unused.
      "tile"       -- a Tiled `trigger`-type object's own `cutscene_id`
                      names which cutscene to check the instant the
                      player's tile becomes that object's tile (modeled on
                      how a `warp` object works) -- `actor` unused, since
                      the object itself already identifies which cutscene.
      "npc_talk"   -- checked when the player interacts with an NPC/shop;
                      `actor` must match that placement's own `name` on
                      this trigger's `map`, same as it'd be referenced by
                      engine.game_state.persistent_id.

    See this module's docstring for why the field is called `event` and
    not the more obvious `on`."""
    event:  str
    when:   list[str] = field(default_factory=list)
    unless: list[str] = field(default_factory=list)
    actor:  str | None = None   # "npc_talk" only -- the NPC/shop placement's own `name`


def trigger_matches(trigger: CutsceneTrigger, flag) -> bool:
    """True if every `when` flag is set and no `unless` flag is set, given
    `flag(key) -> bool` (engine.game_state.GameState.flag or equivalent) --
    identical rule to engine.npc.resolve_dialogue's variant check."""
    return all(flag(f) for f in trigger.when) and not any(flag(f) for f in trigger.unless)


@dataclass(frozen=True)
class CutsceneStep:
    """One entry of a cutscene's `steps:` list -- a single-key YAML mapping
    (`kind`, e.g. "move_actor") plus whatever arguments it carried. Kept as
    a plain dict rather than one dataclass per kind: new kinds (a future
    editor's action palette) only ever need a new handler in
    engine.renderer, not a new class here.

    Exception: a `dialogue` step's `args["choices"]`, if present, is parsed
    into `list[CutsceneChoice]` (see _parse_choices) rather than left as
    raw dicts, same as `trigger`/`steps` themselves get parsed into
    dataclasses at load time instead of staying raw -- the renderer
    consumes typed data, not a YAML dict shape, same as everywhere else in
    this module."""
    kind: str
    args: dict = field(default_factory=dict)


@dataclass(frozen=True)
class CutsceneChoice:
    """One selectable response in a `dialogue` step's `choices:` list --
    `then` is a nested step list, parsed the exact same way a cutscene's
    own top-level `steps:` is (see _parse_steps). engine.renderer splices
    these steps directly into the running cutscene's own step list the
    instant this choice is confirmed (OverworldScene._confirm_dialogue_choice)
    -- not a separate one-shot execution path -- so a choice's consequences
    get full multi-frame handling (wait/move_actor/another dialogue/...)
    for free, the same as any other step. See this module's docstring for
    the YAML shape and the build plan for why choices carry their own
    direct consequences rather than only setting a flag for some later
    trigger to notice."""
    label: str
    then:  list[CutsceneStep] = field(default_factory=list)


@dataclass(frozen=True)
class CutsceneDef:
    """One cutscene file, loaded whole. `map` is the map folder/stem name
    this cutscene is authored against -- see the "no mid-cutscene map
    switching" decision in the build plan; a cutscene never targets more
    than one map."""
    id:      str
    map:     str
    trigger: CutsceneTrigger | None
    steps:   list[CutsceneStep] = field(default_factory=list)


def _parse_trigger(raw: dict | None) -> CutsceneTrigger | None:
    if not raw:
        return None
    return CutsceneTrigger(
        event  = raw["event"],
        when   = list(raw.get("when") or []),
        unless = list(raw.get("unless") or []),
        actor  = raw.get("actor"),
    )


def _parse_choices(raw: list | None) -> list[CutsceneChoice]:
    if not raw:
        return []
    return [
        CutsceneChoice(label=entry["label"], then=_parse_steps(entry.get("then") or []))
        for entry in raw
    ]


def _parse_steps(raw: list) -> list[CutsceneStep]:
    steps = []
    for entry in raw:
        if not isinstance(entry, dict) or len(entry) != 1:
            raise ValueError(f"cutscene step must be a single-key mapping, got {entry!r}")
        (kind, args), = entry.items()
        args = dict(args or {})
        if kind == "dialogue" and args.get("choices"):
            args["choices"] = _parse_choices(args["choices"])
        steps.append(CutsceneStep(kind=kind, args=args))
    return steps


def load_cutscene(path: Path) -> CutsceneDef:
    raw = yaml.safe_load(path.read_text()) or {}
    return CutsceneDef(
        id      = raw["id"],
        map     = raw["map"],
        trigger = _parse_trigger(raw.get("trigger")),
        steps   = _parse_steps(raw.get("steps") or []),
    )


def load_cutscene_defs(directory: Path = _CUTSCENES_DIR) -> dict[str, CutsceneDef]:
    """Every *.yaml file under `directory`, keyed by its own `id` (not its
    filename -- the two are conventionally the same but nothing enforces
    it). Missing directory (a fresh checkout before any cutscene's been
    authored) returns an empty dict rather than erroring, same spirit as
    other optional-content loaders in this codebase."""
    if not directory.exists():
        return {}
    return {
        cutscene.id: cutscene
        for cutscene in (load_cutscene(path) for path in sorted(directory.glob("*.yaml")))
    }


@dataclass
class CutscenePlayer:
    """Headless step pointer for one in-progress cutscene.

    Tracks *position* in the step list only -- current index, and whether
    playback has run past the last step. Deciding *when* the current step
    counts as finished (a move's tween completing, the dialogue box
    closing, a wait timer elapsing) is engine.renderer.OverworldScene's job,
    since only it holds that state; this class just advances the pointer
    when told to.

    `steps` is a *private copy* of `cutscene.steps`, made once at
    construction (see __post_init__) -- never the shared list living on the
    cached CutsceneDef (engine.renderer.OverworldScene.cutscene_defs is
    built once and reused for the process lifetime). A confirmed dialogue
    choice splices its `then:` steps into this list
    (OverworldScene._confirm_dialogue_choice); operating on the def's own
    list directly would permanently graft those steps onto every future
    play of that cutscene, since CutsceneStep/CutsceneDef being frozen only
    stops *reassigning* their fields, not mutating a list one of them
    points to.
    """
    cutscene: CutsceneDef
    index:    int  = 0
    finished: bool = False
    steps:    list[CutsceneStep] = field(init=False, default_factory=list, repr=False)

    def __post_init__(self) -> None:
        self.steps = list(self.cutscene.steps)

    def current_step(self) -> CutsceneStep | None:
        if self.finished or self.index >= len(self.steps):
            return None
        return self.steps[self.index]

    def advance(self) -> None:
        self.index += 1
        if self.index >= len(self.steps):
            self.finished = True
