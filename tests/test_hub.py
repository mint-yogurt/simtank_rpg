"""Hub map test — display the static hub in the web viewer.

No party members, no LLM calls.
"""

from engine.scenes.hub import load_hub_grid
from web.server import get_hub_tile_payload


def run_hub_test(emit) -> None:
    tile_payload = get_hub_tile_payload()
    grid = load_hub_grid()
    rows = len(grid)
    cols = len(grid[0]) if grid else 0

    emit({
        "type": "hub_init",
        "rows": rows, "cols": cols,
        "party": [],
        **tile_payload,
    })

    print(f"[hub test] grid={rows}×{cols}", flush=True)
