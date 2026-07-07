"""Global game config — loaded once from config.json at repo root.

Access via the `cfg` singleton:
    from engine.config import cfg
    time.sleep(cfg.move_ms / 1000)
"""

import json
from pathlib import Path
from types import SimpleNamespace

_CONFIG_PATH = Path(__file__).parent.parent / "config.json"


def _load() -> SimpleNamespace:
    raw = json.loads(_CONFIG_PATH.read_text())

    pacing   = raw["pacing"]
    battle   = raw["battle"]
    display  = raw["display"]
    interior = raw["interior"]

    ns = SimpleNamespace(
        # Pacing (seconds, converted from ms for direct use in time.sleep)
        move_ms                  = pacing["move_ms"],
        screen_cross_ms          = pacing["screen_cross_ms"],
        interior_entry_ms        = pacing["interior_entry_ms"],
        interior_exit_prepare_ms = pacing["interior_exit_prepare_ms"],
        interior_exit_complete_ms= pacing["interior_exit_complete_ms"],

        # Battle
        battle_max_rounds        = battle["max_rounds"],

        # Display geometry
        tile_px                  = display["tile_px"],
        scale                    = display["scale"],
        view_cols                = display["view_cols"],
        view_rows                = display["view_rows"],

        # Interior navigation
        interior_max_explore_dist= interior["max_explore_dist"],
    )
    return ns


cfg: SimpleNamespace = _load()
