#!/usr/bin/env python3
"""Sync a Tiled map's object layer(s) into editable YAML content files.

Not run at runtime and not imported by the engine -- this is a dev tool for
authoring dialogue/items/events. Each map lives in its own folder under
data/maps/ (e.g. data/maps/hub_fronthouse/hub_fronthouse.json). Given no
argument, this script walks every subfolder of data/maps/ looking for a
*.json map export; whatever folder a map is found in is where its
npcs_<map>.yaml / obj_<map>.yaml get written -- output always lands next to
the map that produced it, never in data/maps/ itself.

Objects are split into NPCs (type == "npc") -> npcs_<map>.yaml and
everything else (containers, signs, warps, ...) -> obj_<map>.yaml.

id/name/type are synced from the map on every run. New entries also get
stub fields seeded by type (container: contents/dialogue, sign: dialogue,
npc: dialogue/event/sprite/behavior/npc_id, warp: destination_map/
destination_warp/facing/distance, enemy: enemy_id/level, spawner: enemies/
spawn_chance/level) so there's a place to hand-fill them. Once you've
filled in a field, it's yours -- re-running never overwrites or removes it,
as long as that object's id still exists in the map's object layer.

`npc_id`, if filled in, is a key into data/npcs/npc.yaml (the master NPC
list, loaded by engine.npc.load_npc_defs()) -- that placement's own
`sprite`/`behavior`/`dialogue` then become optional overrides on top of the
shared definition: set, they win for this one placement; left blank
(null / []), they fall back to whatever npc.yaml's entry says. `event` is
unrelated -- still just a reserved stub, not wired to anything yet.

`enemy` and `spawner` objects reference data/enemy/enemies.yaml (the
master enemy list, loaded by engine.enemy.load_enemy_defs()). `enemy` is a
hardcoded placement: `enemy_id` picks which entry (a key from
data/enemy/enemies.yaml, e.g. `fat_guy2`), `level` optionally overrides
that entry's own level/level-range for this one placement -- leave it
`null` to just use the entry's own level.

`spawner` rolls once every time the map loads (a fresh boot or any warp
arrival/re-arrival -- see engine/renderer.py's OverworldScene._load_map).
Two fields, both mandatory for a spawner to ever produce anything:

  spawn_chance: a plain number from 0.0 to 1.0 -- the probability THIS
    spawner produces an enemy at all on this load. 0.0 = never, 1.0 =
    always, 0.3 = roughly 3 times in 10. Not a percentage (don't write 30).

  enemies: a YAML list of mappings, each with an enemy_id (a key from
    data/enemy/enemies.yaml) and a chance (any positive number -- a
    RELATIVE WEIGHT against the other entries in this same list, not a
    probability by itself). Once spawn_chance passes, exactly one enemy
    from this list is picked, weighted by these numbers -- never zero,
    never more than one. Example, two candidates weighted 3:1 (75%/25%
    split of whichever enemy spawns), gated at a 40% chance to spawn
    anything at all:

      spawn_chance: 0.4
      enemies:
        - enemy_id: fat_guy2
          chance: 3
        - enemy_id: parkinglotguy
          chance: 1

`level` on a spawner optionally overrides whichever enemy gets picked,
same meaning as on a hardcoded `enemy` -- leave it `null` to use that
enemy's own level.

Warps are one-way and hand-paired by you: place a `warp`-type object on each
side of a door/exit, then fill in `destination_map` (the other map's folder/
stem name) and `destination_warp` (the *name* of the warp object on that
other map to land on). Names only need to be unique within a single map's
object layer, not globally -- lookup is always scoped to destination_map
first. `facing` is which way the player faces after spawning at this warp
(used when something else's destination_warp points here); leave it blank
to default to south. `distance` offsets the landing spot that many tiles
from this warp's own tile, in the `facing` direction -- e.g. facing: S,
distance: 1 lands one tile south of this warp, so the player visibly steps
out of a doorway instead of standing on it. Leave it blank (or 0) to land
exactly on this warp's tile.

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

    npc_objects = [o for o in objects if o.get("type") == "npc"]
    other_objects = [o for o in objects if o.get("type") != "npc"]

    _sync_file(map_dir / f"npcs_{map_name}.yaml", npc_objects, map_name)
    _sync_file(map_dir / f"obj_{map_name}.yaml", other_objects, map_name)


# Stub fields seeded onto new entries, keyed by Tiled object type. Only
# applied when the field is missing -- never overwrites a hand-filled value.
STUB_FIELDS = {
    "container": {"contents": None, "dialogue": []},
    "sign": {"dialogue": []},
    "npc": {"dialogue": [], "event": None, "sprite": None, "behavior": None, "npc_id": None},
    "warp": {"destination_map": None, "destination_warp": None, "facing": None, "distance": None},
    "enemy": {"enemy_id": None, "level": None},
    # `enemies` seeds with one blank candidate block, not an empty list --
    # a spawner is useless with `enemies: []` (nothing to pick from), so the
    # stub hands you a copy-pasteable block to fill in and duplicate, rather
    # than making you remember the enemy_id/chance shape from scratch.
    "spawner": {"enemies": [{"enemy_id": None, "chance": None}], "spawn_chance": None, "level": None},
}


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

    header = (
        f"# Auto-synced from {map_name}.json by populate_yamls.py.\n"
        f"# id/name/type are overwritten on every sync; missing stub fields\n"
        f"# (dialogue, contents, event, sprite, behavior, npc_id,\n"
        f"# destination_map, destination_warp, facing, distance, enemy_id,\n"
        f"# level, enemies, spawn_chance) are seeded per type but never\n"
        f"# overwritten once filled in -- see STUB_FIELDS in the script.\n"
        f"#\n"
        f"# npc: npc_id, if filled in, is a key into data/npcs/npc.yaml (the\n"
        f"#   master NPC list, loaded by engine.npc.load_npc_defs()) -- this\n"
        f"#   placement's own sprite/behavior/dialogue then become optional\n"
        f"#   overrides on top of that shared definition: set, they win for\n"
        f"#   this one placement; left blank (null / []), they fall back to\n"
        f"#   whatever npc.yaml's entry says. event is unrelated -- still\n"
        f"#   just a reserved stub, not wired to anything yet.\n"
        f"#\n"
        f"# enemy (hardcoded placement): enemy_id is a key from\n"
        f"#   data/enemy/enemies.yaml (e.g. fat_guy2); level optionally\n"
        f"#   overrides that entry's own level -- leave null to use it as-is.\n"
        f"#\n"
        f"# spawner (rolls once each time the map loads): spawn_chance is a\n"
        f"#   plain number from 0.0 to 1.0 -- the odds THIS spawner produces\n"
        f"#   anything at all (0.3 = ~3 times in 10; NOT a percentage, don't\n"
        f"#   write 30).\n"
        f"#\n"
        f"# A fresh spawner stub already seeds `enemies` with one blank\n"
        f"#   candidate block (enemy_id: null / chance: null) -- fill in\n"
        f"#   that enemy_id and chance, then copy the whole `- enemy_id: ...\n"
        f"#   chance: ...` block again (indented the same as the first) for\n"
        f"#   each additional enemy this spawner can produce. Left with\n"
        f"#   enemy_id: null (or emptied out to `enemies: []`), this spawner\n"
        f"#   can NEVER produce anything, no matter what spawn_chance says --\n"
        f"#   there's nothing valid to pick from. Each block is two fields,\n"
        f"#   an enemy_id (a key from data/enemy/enemies.yaml) and a chance\n"
        f"#   (any positive number -- a WEIGHT relative to the other blocks\n"
        f"#   in this same list, not a standalone probability, and it does\n"
        f"#   NOT need to add up to anything in particular). One enemy from\n"
        f"#   this list is picked -- weighted by these numbers -- every time\n"
        f"#   spawn_chance itself passes.\n"
        f"#\n"
        f"#   With only one enemy allowed at a spawner, its chance value\n"
        f"#   doesn't matter (nothing to weigh it against) -- e.g. \"only\n"
        f"#   fat_guy2 ever spawns here, 40% of the time\":\n"
        f"#\n"
        f"#     spawn_chance: 0.4\n"
        f"#     enemies:\n"
        f"#       - enemy_id: fat_guy2\n"
        f"#         chance: 1\n"
        f"#\n"
        f"#   With two-plus candidates, chance sets the split between them\n"
        f"#   -- e.g. \"40% chance to spawn anything, and when it does, it's\n"
        f"#   fat_guy2 three times out of four, parkinglotguy the other\n"
        f"#   quarter\" (chance: 3 vs chance: 1 -> a 3:1, i.e. 75%/25%, split\n"
        f"#   -- doubling both to 6 and 2 would mean the exact same thing):\n"
        f"#\n"
        f"#     spawn_chance: 0.4\n"
        f"#     enemies:\n"
        f"#       - enemy_id: fat_guy2\n"
        f"#         chance: 3\n"
        f"#       - enemy_id: parkinglotguy\n"
        f"#         chance: 1\n"
        f"#\n"
        f"#   Every `- enemy_id: ...` line must line up under `enemies:`\n"
        f"#   with the same indentation as the `-` above, and `chance:`\n"
        f"#   lines up under its own `enemy_id:` line, indented one level\n"
        f"#   further -- YAML reads structure from indentation, so a block\n"
        f"#   nested at the wrong depth silently becomes a different\n"
        f"#   (usually broken) structure instead of an error.\n"
        f"#\n"
        f"#   level (top-level, alongside spawn_chance/enemies -- not per\n"
        f"#   candidate) optionally overrides whichever enemy gets picked;\n"
        f"#   leave it null to use that enemy's own level from enemies.yaml.\n\n"
    )
    write_yaml(path, existing, header)
    print(f"wrote {path} ({len(objects)} objects)")


def main() -> None:
    map_paths = [Path(sys.argv[1])] if len(sys.argv) > 1 else sorted(MAPS_DIR.rglob("*.json"))
    for map_path in map_paths:
        sync_map(map_path)


if __name__ == "__main__":
    main()
