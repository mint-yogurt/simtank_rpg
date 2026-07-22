"""Master NPC definitions -- pure logic, no pygame, no Tiled knowledge.

Mirrors the enemy split (engine.enemy): this module only knows what NPCs
exist (NpcDef, loaded from data/npcs/npc.yaml) and what sprite strip/
placeholder-color info each named sprite carries (NpcSpriteSpec). All
pygame-specific work -- actually slicing a sprite strip into Surfaces,
recoloring one per NPC, drawing -- stays in engine.renderer, same as it
does for enemies.

A map's npcs_<map>.yaml entry (see data/maps/populate_yamls.py) references
an NpcDef by `npc_id`; its own sprite/behavior/dialogue fields are optional
per-placement overrides on top of it, resolved in
engine.renderer.OverworldScene._load_map. An npc-type object with no
npc_id set (or one that doesn't resolve to a def) falls back to its
inline fields exactly like before this module existed -- npc_id is
additive, not required.

NPC sprites live under assets/sprites/npcs/, one PNG strip per sprite --
NOT the old shared party_sprites.png + partysprites.txt mechanism, which
is deprecated for NPCs. A strip's width decides how it animates: 32x16 (2
frames) is a south-only idle loop with no facing; 128x16 (8 frames) is a
full walk cycle in the same S1,S2,N1,N2,W1,W2,E1,E2 order the party sheet
uses, and does support facing. A frame can also be wider and/or taller
than one tile (e.g. manager, 64x32 -- 2 frames, each 32x32): the extra
rows/columns beyond the bottom-left 16x16 cell are a fixed overhang with
no animation of their own, and a placement anchors at that bottom-left
cell -- see engine.renderer's "NPC sprite sheet loading" section and
NPC.width_span/height_span.

Recoloring: a sprite's `colors` (NpcSpriteSpec) are the placeholder hex
colors actually baked into its PNG; an NpcDef's own `colors`, if set,
recolors that sprite for this NPC specifically, position-matched against
the sprite's placeholder list -- e.g. sprite colors[0] swaps to this NPC's
colors[0]. Two NPCs can share one sprite strip and still look different.
See engine.renderer.recolor_surface for the actual pixel remap.

Dialogue can branch on engine.game_state flags -- see DialogueVariant/
resolve_dialogue below. Every npc_id'd NPC gets a free flag,
`npc_met:<npc_id>`, set the instant it's ever talked to (engine.input.
handle_a_button, after resolving that interaction's pages but before the
next one) -- deliberately global (not map-scoped like
engine.game_state.persistent_id, since a named NpcDef is one character
regardless of which map places it), and needs no authoring: reference it
in a variant's `unless` to write "the first time we've ever met" dialogue,
same as wizard's does.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_NPC_DEFS_PATH = Path(__file__).parent.parent / "data" / "npcs" / "npc.yaml"


def _validated_colors(raw_colors: list, context: str) -> list[str]:
    """Guard against YAML's numeric auto-typing corrupting a hex color: an
    unquoted all-digit value like `000000` parses as the *integer* 0, and
    `001100` parses as *octal* 576 -- both silently wrong, no YAML error.
    A wrong color drawn silently is worse than a crash, so this fails loud
    (unlike the fail-soft convention elsewhere in this codebase, e.g.
    engine.enemy.resolve_spawn dropping bad candidates) with a message that
    points straight at the fix: quote the value in npc.yaml."""
    for c in raw_colors:
        if not isinstance(c, str):
            raise ValueError(
                f"{context}: color {c!r} isn't a string (got {type(c).__name__}) -- "
                f"hex values must be quoted in npc.yaml, e.g. \"000000\" not 000000, "
                f"or YAML silently reads it as a number instead of a color"
            )
    return list(raw_colors)


@dataclass(frozen=True)
class NpcSpriteSpec:
    """One entry of npc.yaml's `sprites:` section: which file to load from
    assets/sprites/npcs/ and its own placeholder hex colors (6-digit, no
    '#') -- the swap keys an NpcDef's `colors` recolors against."""
    file:   str
    colors: list[str] = field(default_factory=list)


@dataclass(frozen=True)
class DialogueVariant:
    """One candidate page-list for an NpcDef's `dialogue:` -- see
    resolve_dialogue. `when` flags must ALL be true and `unless` flags must
    ALL be false for this variant to match; either/both left empty means
    that side imposes no restriction, so a variant with neither set always
    matches (an unconditional catch-all)."""
    pages:  list[str] = field(default_factory=list)
    when:   list[str] = field(default_factory=list)
    unless: list[str] = field(default_factory=list)


def npc_met_flag(npc_id: str) -> str:
    """The engine.game_state flag key set the first time npc_id is ever
    talked to (engine.input.handle_a_button) -- global, not per-map (unlike
    engine.game_state.persistent_id), since an NpcDef is one character
    regardless of which map places it. Reference this same string literally
    (e.g. "npc_met:wizard") in a DialogueVariant's `unless` in npc.yaml to
    write that character's first-ever-meeting dialogue."""
    return f"npc_met:{npc_id}"


def resolve_dialogue(variants: list[DialogueVariant], flag) -> list[str]:
    """First variant (authoring order) whose `when`/`unless` both pass,
    given `flag(key) -> bool` (engine.game_state.GameState.flag, or
    equivalent) -- see DialogueVariant. Put the most specific/rare
    conditions first and an unconditional variant last as a catch-all; a
    def with no variant matching the current flags (author error -- no
    catch-all present) shows no dialogue at all (empty list), same as an
    NPC with no `dialogue:` set."""
    for variant in variants:
        if all(flag(f) for f in variant.when) and not any(flag(f) for f in variant.unless):
            return variant.pages
    return []


@dataclass(frozen=True)
class NpcDef:
    """One entry of data/npcs/npc.yaml's `npcs:` section. Static -- never
    mutated at runtime."""
    id:       str
    sprite:   str                      # key into load_npc_sprite_specs()'s result
    behavior: str = "static"           # "static" | "wander"
    facing:   str = "S"                # "N" | "S" | "E" | "W" -- static only;
                                        #   a "wander" NPC faces its movement instead
    colors:   list[str] | None = None  # replacement palette, position-matched
                                        #   against this sprite's own placeholder
                                        #   colors; None = use them as authored
    dialogue: list[DialogueVariant] = field(default_factory=list)


def parse_dialogue(raw: list) -> list[DialogueVariant]:
    """A yaml `dialogue:` list is either the old flat page-list shape (a
    list of strings -- wrapped here into one unconditional DialogueVariant,
    so every pre-existing/simple NPC needs no changes) or a list of
    variant dicts (`pages:`, plus optional `when:`/`unless:`) for
    conditional dialogue -- see DialogueVariant/resolve_dialogue. Mixing
    the two shapes in one list isn't supported -- the first element decides
    which shape the whole list is read as.

    Shared by both npc.yaml's own `npcs:` entries (load_npc_defs, below)
    and a placement's own `dialogue:` override in a map's npcs_<map>.yaml
    (engine.renderer.OverworldScene._load_map) -- either one can be a flat
    list or a conditional variant list, same rule either place."""
    if not raw:
        return []
    if isinstance(raw[0], str):
        return [DialogueVariant(pages=list(raw))]
    return [
        DialogueVariant(
            pages  = entry.get("pages") or [],
            when   = entry.get("when") or [],
            unless = entry.get("unless") or [],
        )
        for entry in raw
    ]


def load_npc_defs(path: Path = _NPC_DEFS_PATH) -> dict[str, NpcDef]:
    """Parse npc.yaml's `npcs:` section into id -> NpcDef."""
    raw = yaml.safe_load(path.read_text()) or {}
    defs: dict[str, NpcDef] = {}
    for npc_id, entry in (raw.get("npcs") or {}).items():
        colors = entry.get("colors")
        defs[npc_id] = NpcDef(
            id       = npc_id,
            sprite   = entry["sprite"],
            behavior = entry.get("behavior") or "static",
            facing   = entry.get("facing") or "S",
            colors   = _validated_colors(colors, f"npc.yaml npcs.{npc_id}.colors") if colors else None,
            dialogue = parse_dialogue(entry.get("dialogue") or []),
        )
    return defs


def load_npc_sprite_specs(path: Path = _NPC_DEFS_PATH) -> dict[str, NpcSpriteSpec]:
    """Parse npc.yaml's `sprites:` section into sprite id -> NpcSpriteSpec."""
    raw = yaml.safe_load(path.read_text()) or {}
    return {
        sprite_id: NpcSpriteSpec(
            file=entry["file"],
            colors=_validated_colors(entry.get("colors") or [], f"npc.yaml sprites.{sprite_id}.colors"),
        )
        for sprite_id, entry in (raw.get("sprites") or {}).items()
    }
