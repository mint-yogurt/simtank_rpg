#!/usr/bin/env python3
"""Sync a Tiled map's object layer(s) into editable YAML content files.

Not run at runtime and not imported by the engine -- this is a dev tool for
authoring dialogue/items/events. Each map lives in its own folder under
data/maps/ (e.g. data/maps/hub_fronthouse/hub_fronthouse.json). Given no
argument, this script walks every subfolder of data/maps/ looking for a
*.json map export; whatever folder a map is found in is where its
npcs_<map>.yaml / obj_<map>.yaml get written -- output always lands next to
the map that produced it, never in data/maps/ itself.

Objects are split into NPCs (type == "npc" or "shop") -> npcs_<map>.yaml and
everything else (containers, signs, warps, ...) -> obj_<map>.yaml. A shop is
a person first -- built from the same sprite/behavior/npc_id pipeline as any
other NPC (see engine.renderer.NPC) -- so it's synced alongside them, not
with the static objects.

id/name/type are synced from the map on every run. New entries also get
stub fields seeded by type (see STUB_FIELDS below) so there's a place to
hand-fill them. Once you've filled in a field, it's yours -- re-running
never overwrites or removes it, as long as that object's id still exists in
the map's object layer.

Every written file gets a generated header documenting only the object
types actually present in that file -- see TYPE_DOCS below, the single
source of truth for both the stub fields and the header text. Add a new
object type to Tiled, run this script, and its docs appear in the header
right along with the stub fields; nothing here needs updating by hand.

Usage:
    python data/maps/populate_yamls.py data/maps/hub_fronthouse/hub_fronthouse.json
    python data/maps/populate_yamls.py            # syncs every map folder under data/maps/
"""
import copy
import json
import sys
from pathlib import Path

import yaml

MAPS_DIR = Path(__file__).parent


def load_yaml(path: Path) -> dict:
    if not path.exists():
        return {}
    with path.open() as f:
        return yaml.safe_load(f) or {}


def write_yaml(path: Path, data: dict, header: str) -> None:
    with path.open("w") as f:
        f.write(header)
        yaml.safe_dump(data, f, sort_keys=True, default_flow_style=False)


def sync_map(map_path: Path) -> None:
    map_name = map_path.stem
    map_dir = map_path.parent
    with map_path.open() as f:
        map_data = json.load(f)

    objects = []
    for layer in map_data.get("layers", []):
        if layer.get("type") == "objectgroup":
            objects.extend(layer.get("objects", []))

    # A shop is a person first -- built from the exact same pipeline as any
    # other NPC (sprite/behavior/npc_id, engine.renderer.NPC), not a
    # separate static object -- so it's synced to npcs_<map>.yaml too.
    npc_objects = [o for o in objects if o.get("type") in ("npc", "shop")]
    other_objects = [o for o in objects if o.get("type") not in ("npc", "shop")]

    _sync_file(map_dir / f"npcs_{map_name}.yaml", npc_objects, map_name)
    _sync_file(map_dir / f"obj_{map_name}.yaml", other_objects, map_name)


# Stub fields seeded onto new entries, keyed by Tiled object type. Only
# applied when the field is missing -- never overwrites a hand-filled value.
STUB_FIELDS = {
    "container": {"contents": None, "gold": None, "dialogue": []},
    "sign": {"dialogue": []},
    "healer": {"dialogue": []},
    "npc": {"dialogue": [], "event": None, "sprite": None, "behavior": None, "npc_id": None},
    "warp": {"destination_map": None, "destination_warp": None, "facing": None, "distance": None},
    "trigger": {"cutscene_id": None},
    "enemy": {"enemy_id": None, "level": None},
    # `enemies` seeds with one blank candidate block, not an empty list --
    # a spawner is useless with `enemies: []` (nothing to pick from), so the
    # stub hands you a copy-pasteable block to fill in and duplicate, rather
    # than making you remember the enemy_id/chance shape from scratch.
    "spawner": {"enemies": [{"enemy_id": None, "chance": None}], "spawn_chance": None, "level": None},
    "shop": {"dialogue": [], "event": None, "sprite": None, "behavior": None,
             "npc_id": None, "stock": [], "farewell": []},
}

# Canonical display order for whichever of these types show up in one file's
# header -- not alphabetical, just a stable, readable order.
_TYPE_ORDER = ["container", "sign", "healer", "warp", "trigger", "enemy", "spawner", "npc", "shop"]

# Per-type header block: a bare copy-pastable template (every field, no
# values, no comments) followed by one filled-in example with a trailing
# comment on each field. No prose -- if a field's meaning isn't obvious from
# its example value + one comment, the comment needs fixing, not lengthening.
TYPE_DOCS = {
    "container": """\
# container -- template:
#   contents:
#   gold:
#   dialogue:
#
# container -- example:
#   contents: forgotten_onion   # item id from data/items/items.yaml, or null for no item
#   gold: 10                    # flat gold amount credited on open, or null for no gold
#   dialogue: ["Whoa! Cool!"]   # always shown; contents/gold (if set) only grant on the first open
""",
    "sign": """\
# sign -- template:
#   dialogue:
#
# sign -- example:
#   dialogue: ["Just a sign."]   # always shown, no side effect, every visit
""",
    "healer": """\
# healer -- template:
#   dialogue:
#
# healer -- example:
#   dialogue: []   # shown before the auto-appended "HP/MP FULLY RESTORED." page, every visit, free, no flag
""",
    "warp": """\
# warp -- template:
#   destination_map:
#   destination_warp:
#   facing:
#   distance:
#
# warp -- example:
#   destination_map: hub_fronthouse   # the OTHER map's folder/stem name
#   destination_warp: front_door      # name of the warp object THERE to land on
#   facing: S                         # which way the player faces after landing on THIS warp (blank = S)
#   distance: 1                       # tiles to offset the landing spot from THIS warp, toward `facing` (blank/0 = exact tile)
""",
    "trigger": """\
# trigger -- template:
#   cutscene_id:
#
# trigger -- example:
#   cutscene_id: debug_test   # key from data/cutscenes/ -- that cutscene's own trigger.event must be "tile"
# An invisible, one-tile zone (place with the rectangle tool, same as an enemy/spawner) --
# the instant the player's tile becomes this one, the named cutscene is checked (not
# unconditionally played -- its own when/unless still has to pass) and started if it matches.
""",
    "enemy": """\
# enemy -- template:
#   enemy_id:
#   level:
#
# enemy -- example:
#   enemy_id: fat_guy2   # key from data/enemy/enemies.yaml
#   level: null          # overrides that entry's own level -- null = use it as-is
""",
    "spawner": """\
# spawner -- template:
#   spawn_chance:
#   enemies:
#     - enemy_id:
#       chance:
#   level:
#
# spawner -- example:
#   spawn_chance: 0.4          # odds (0.0-1.0) THIS spawner produces anything at all, per map load -- NOT a percentage
#   enemies:                   # exactly one entry is picked if spawn_chance passes
#     - enemy_id: fat_guy2       # key from data/enemy/enemies.yaml
#       chance: 3                # weight vs the OTHER entries below (3 vs 1 = a 3:1, i.e. 75%/25%, split)
#     - enemy_id: parkinglotguy
#       chance: 1
#   level: null                # overrides whichever enemy gets picked -- null = use its own
""",
    "npc": """\
# npc -- template:
#   npc_id:
#   sprite:
#   behavior:
#   event:
#   dialogue:
#
# npc -- example:
#   npc_id: grocer      # key from data/npcs/npc.yaml, or null for a one-off character authored entirely here
#   sprite: null         # overrides npc.yaml's sprite for this placement only -- null = inherit it
#   behavior: null       # overrides npc.yaml's behavior ("static"/"wander") for this placement only -- null = inherit it
#   event: null          # reserved, not wired to anything yet
#   dialogue:            # flat page list, OR a list of variants gated on engine.game_state flags
#     - unless: ["npc_met:grocer"]       # first time ever (before this flag is set)
#       pages: ["First time seeing you!"]
#     - pages: ["Hey again."]            # fallback -- checked top-to-bottom, first match wins
""",
    "shop": """\
# shop -- template:
#   npc_id:
#   sprite:
#   behavior:
#   event:
#   dialogue:
#   stock:
#     - item:
#       price:
#   farewell:
#
# shop -- example:
#   npc_id: null
#   sprite: pizza_cashier
#   behavior: static
#   event: null
#   dialogue: ["Welcome to Pizza Hut!"]   # greeting -- opens before the buy/sell screen, every visit
#   stock:
#     - item: forgotten_onion   # key from data/items/items.yaml
#       price: 8                # gold cost to BUY one -- independent of that item's own `value` (what selling it pays out)
#     - item: bag_of_soup
#       price: 25
#   farewell: ["Come back soon!"]   # shown after backing all the way out of the buy/sell screen
""",
}


def _build_header(map_name: str, objects: list) -> str:
    types_present = sorted({o["type"] for o in objects if o["type"] in TYPE_DOCS},
                            key=_TYPE_ORDER.index)
    lines = [
        f"# Auto-synced from {map_name}.json by populate_yamls.py.\n",
        "# id/name/type refresh every run; every other field is seeded once per type\n",
        "# (see below) and yours from then on -- re-running never overwrites or clears\n",
        "# a field you've filled in, as long as this object's id still exists in the\n",
        "# map's object layer.\n",
        "#\n",
    ]
    for t in types_present:
        lines.append(TYPE_DOCS[t])
        lines.append("#\n")
    lines.append("\n")
    return "".join(lines)


def _sync_file(path: Path, objects: list, map_name: str) -> None:
    existing = load_yaml(path)
    seen_ids = set()

    for obj in objects:
        obj_id = obj["id"]
        seen_ids.add(obj_id)
        entry = existing.setdefault(obj_id, {})
        entry["name"] = obj["name"]
        entry["type"] = obj["type"]
        for key, default in STUB_FIELDS.get(obj["type"], {}).items():
            entry.setdefault(key, copy.deepcopy(default))

    orphaned = sorted(set(existing) - seen_ids)
    if orphaned:
        print(f"{path.name}: entry ids no longer in the map (left in place): {orphaned}")

    header = _build_header(map_name, objects)
    write_yaml(path, existing, header)
    print(f"wrote {path} ({len(objects)} objects)")


def main() -> None:
    map_paths = [Path(sys.argv[1])] if len(sys.argv) > 1 else sorted(MAPS_DIR.rglob("*.json"))
    for map_path in map_paths:
        sync_map(map_path)


if __name__ == "__main__":
    main()
