"""Debug entry point — runs ordinary engine/renderer code, not a reduced path.

    python maptest.py

Prompts for: map / battle / debug screen. `map` lists every folder under
data/maps/ (each expected to hold a Tiled export named <folder>.json) and
loads the chosen one via engine/renderer.py's OverworldScene. `battle` and
`debug screen` are stubs for later.
"""
import sys
from functools import partial
from pathlib import Path

from engine.config import cfg
from engine.renderer import OverworldScene, run

_MAPS_DIR = Path(__file__).parent / "data" / "maps"


def _available_maps() -> list[str]:
    return sorted(
        entry.name for entry in _MAPS_DIR.iterdir()
        if entry.is_dir() and (entry / f"{entry.name}.json").exists()
    )


def _choose_map() -> Path:
    maps = _available_maps()
    if not maps:
        print(f"No maps found under {_MAPS_DIR}", flush=True)
        sys.exit(1)

    for i, name in enumerate(maps, start=1):
        print(f"  {i}. {name}")
    choice = input(f"map (1-{len(maps)})? ").strip()
    try:
        name = maps[int(choice) - 1]
    except (ValueError, IndexError):
        print(f"Unknown choice {choice!r}", flush=True)
        sys.exit(1)
    return _MAPS_DIR / name / f"{name}.json"


def main():
    view_size = (cfg.view_cols * cfg.tile_px, cfg.view_rows * cfg.tile_px)
    choice = input("map / battle / debug screen? ").strip().lower()

    if choice == "map":
        map_path = _choose_map()
        run(
            partial(OverworldScene, map_path=map_path),
            view_size=view_size,
            scale=cfg.pygame_scale,
            title=f"Front House Gaiden — debug [{map_path.parent.name}]",
        )
    elif choice == "battle":
        print("battle: not yet implemented", flush=True)
        sys.exit(1)
    elif choice == "debug screen":
        print("debug screen: not yet implemented", flush=True)
        sys.exit(1)
    else:
        print(f"Unknown choice {choice!r}. Use: map, battle, or debug screen", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
