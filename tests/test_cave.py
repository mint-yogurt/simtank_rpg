"""Cave map test — generate a cave and push it to the web viewer.

No party members, no LLM calls. Just procgen + SSE broadcast.
"""

import random

from procgen.cavegen import generate_cave_data, CANVAS_PAD
from procgen.enemygen import generate_enemies
from web.server import get_interior_tile_payload, get_npc_sprite_url


def run_cave_test(seed: int, emit) -> None:
    cave = generate_cave_data(seed)
    data = cave.to_dict()

    tile_payload = get_interior_tile_payload(seed, seed, "dungeon", data)

    # Compute the same origin offsets the server uses when building tile_grid,
    # so spawn coords map into the rendered grid space.
    all_cells = set(cave.floor_grid) | set(cave.wall_grid)
    if all_cells:
        min_r = min(r for r, c in all_cells)
        min_c = min(c for r, c in all_cells)
        origin_r = min_r - CANVAS_PAD
        origin_c = min_c - CANVAS_PAD
    else:
        origin_r = origin_c = 0

    rows = len(tile_payload["tile_grid"])
    cols = len(tile_payload["tile_grid"][0]) if rows else 0
    spawn_r = (cave.spawn[0] - origin_r) if cave.spawn else 0
    spawn_c = (cave.spawn[1] - origin_c) if cave.spawn else 0

    emit({
        "type": "interior_init",
        "rows": rows, "cols": cols,
        "row": spawn_r, "col": spawn_c,
        "monster_spawn": True,
        "party": [],
        **tile_payload,
    })

    # Place enemies on random walkable floor tiles.
    rng = random.Random(seed ^ 0xBEEF)
    pool = generate_enemies(seed, count=10)
    walkable = list(cave.floor_grid.keys())
    rng.shuffle(walkable)

    enemy_list = []
    for i, (r, c) in enumerate(walkable[:min(6, len(walkable))]):
        e = pool[i % len(pool)]
        entry = {
            "index": i,
            "row": r - origin_r,
            "col": c - origin_c,
            "npc_sprite": e["npc_sprite"],
            "name": e["name"],
        }
        if e.get("sprite_palette"):
            url = get_npc_sprite_url(e["npc_sprite"], e["sprite_palette"])
            if url:
                entry["sprite_url"] = url
        enemy_list.append(entry)

    emit({"type": "enemies", "enemies": enemy_list})
    print(f"[cave test] seed={seed}  rooms={len(cave.rooms)}  enemies={len(enemy_list)}",
          flush=True)
