"""Town map test — generate a town and push it to the web viewer.

No party members, no LLM calls, no NPCs (not yet implemented in towngen).
"""

from procgen.towngen import generate_town_data
from web.server import get_interior_tile_payload


def run_town_test(seed: int, emit) -> None:
    town = generate_town_data(seed)
    data = town.to_dict()

    tile_payload = get_interior_tile_payload(seed, seed, "town", data)

    rows = len(tile_payload["tile_grid"])
    cols = len(tile_payload["tile_grid"][0]) if rows else 0

    # Spawn is expressed in absolute town coords; offset by crop_box origin.
    r0, c0, _r1, _c1 = town.crop_box
    spawn_r = town.spawn[0] - r0
    spawn_c = town.spawn[1] - c0

    emit({
        "type": "interior_init",
        "rows": rows, "cols": cols,
        "row": spawn_r, "col": spawn_c,
        "monster_spawn": False,
        "party": [],
        **tile_payload,
    })

    emit({"type": "enemies", "enemies": []})
    print(f"[town test] seed={seed}  crop={town.crop_box}  palette={town.palette}",
          flush=True)
