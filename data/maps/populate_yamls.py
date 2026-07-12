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
npc: dialogue/event/sprite/behavior, warp: destination_map/destination_warp/
facing) so there's a place to hand-fill them. Once you've filled in a field,
it's yours -- re-running never overwrites or removes it, as long as that
object's id still exists in the map's object layer.

Warps are one-way and hand-paired by you: place a `warp`-type object on each
side of a door/exit, then fill in `destination_map` (the other map's folder/
stem name) and `destination_warp` (the *name* of the warp object on that
other map to land on). Names only need to be unique within a single map's
object layer, not globally -- lookup is always scoped to destination_map
first. `facing` is which way the player faces after spawning at this warp
(used when something else's destination_warp points here); leave it blank
to default to south.

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
    "container": {"contents": [], "dialogue": []},
    "sign": {"dialogue": []},
    "npc": {"dialogue": [], "event": None, "sprite": None, "behavior": None},
    "warp": {"destination_map": None, "destination_warp": None, "facing": None},
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
        f"# (dialogue, contents, event, sprite, behavior, destination_map,\n"
        f"# destination_warp, facing) are seeded per type but never overwritten\n"
        f"# once filled in -- see STUB_FIELDS in the script.\n\n"
    )
    write_yaml(path, existing, header)
    print(f"wrote {path} ({len(objects)} objects)")


def main() -> None:
    map_paths = [Path(sys.argv[1])] if len(sys.argv) > 1 else sorted(MAPS_DIR.rglob("*.json"))
    for map_path in map_paths:
        sync_map(map_path)


if __name__ == "__main__":
    main()
