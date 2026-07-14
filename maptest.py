"""Debug entry point — runs ordinary engine/renderer code, not a reduced path.

    python maptest.py

Prompts with a numbered list: 1. map 2. battles 3. debug screen. Accepts
either the number or the typed name. `map` first prompts for a save slot
(engine/save.py) — pick an in-use slot to resume exactly where it left off
(map, player position, inventory, flags), an empty one to start fresh, or
`c<N>` to wipe slot N back to nothing, so different chapters/scenarios can
be tested without hand-editing save files. A fresh/new-game boot then lists
every folder under data/maps/ (each expected to hold a Tiled export named
<folder>.json) same as before. `battles` and `debug screen` are stubs for
later.
"""
import os
import sys
from functools import partial
from pathlib import Path

from engine.config import cfg
from engine.renderer import OverworldScene, run
from engine.save import SLOT_COUNT, clear_slot, load_from_slot, slot_exists

_MAPS_DIR = Path(__file__).parent / "data" / "maps"


def _clear_screen() -> None:
    os.system("cls" if os.name == "nt" else "clear")


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


def _choose_save_slot() -> tuple[int, bool]:
    """Prompts for a save slot to play. Returns (slot, is_new_game).

    `c<N>` clears slot N (engine.save.clear_slot) and re-prompts, so a
    slot can be reset to a blank start without leaving the terminal.
    """
    print("Save slots:")
    for slot in range(1, SLOT_COUNT + 1):
        status = "in use" if slot_exists(slot) else "empty"
        print(f"  {slot}. slot {slot} ({status})")
    print("  c<N> - clear slot N, e.g. c2")
    choice = input(f"slot (1-{SLOT_COUNT}, or c<N>)? ").strip().lower()

    if choice.startswith("c") and choice[1:].isdigit():
        n = int(choice[1:])
        if not 1 <= n <= SLOT_COUNT:
            print(f"Unknown slot {n}", flush=True)
            sys.exit(1)
        clear_slot(n)
        print(f"cleared slot {n}\n", flush=True)
        return _choose_save_slot()

    try:
        slot = int(choice)
        if not 1 <= slot <= SLOT_COUNT:
            raise ValueError
    except ValueError:
        print(f"Unknown choice {choice!r}", flush=True)
        sys.exit(1)
    return slot, not slot_exists(slot)


_MODES = ["map", "battles", "debug screen"]


def _choose_mode() -> str:
    for i, name in enumerate(_MODES, start=1):
        print(f"  {i}. {name}")
    choice = input("? ").strip().lower()
    if choice.isdigit():
        try:
            mode = _MODES[int(choice) - 1]
        except IndexError:
            print(f"Unknown choice {choice!r}. Use: 1-{len(_MODES)}, or type a name", flush=True)
            sys.exit(1)
    elif choice in _MODES:
        mode = choice
    else:
        print(f"Unknown choice {choice!r}. Use: {', '.join(_MODES)}", flush=True)
        sys.exit(1)

    _clear_screen()
    return mode


def main():
    view_size = (cfg.view_cols * cfg.tile_px, cfg.view_rows * cfg.tile_px)
    choice = _choose_mode()

    if choice == "map":
        slot, is_new = _choose_save_slot()
        if is_new:
            map_path = _choose_map()
            player = inventory = game_state = None
        else:
            map_name, player, inventory, game_state = load_from_slot(slot)
            map_path = _MAPS_DIR / map_name / f"{map_name}.json"
            print(f"Loaded slot {slot}: {map_name}, player at "
                  f"({player.row},{player.col})", flush=True)

        run(
            partial(OverworldScene, map_path=map_path, player=player,
                    inventory=inventory, game_state=game_state, active_slot=slot),
            view_size=view_size,
            scale=cfg.pygame_scale,
            title=f"Front House Gaiden — debug [{map_path.parent.name}] (slot {slot})",
        )
    elif choice == "battles":
        print("battles: not yet implemented", flush=True)
        sys.exit(1)
    elif choice == "debug screen":
        print("debug screen: not yet implemented", flush=True)
        sys.exit(1)


if __name__ == "__main__":
    main()
