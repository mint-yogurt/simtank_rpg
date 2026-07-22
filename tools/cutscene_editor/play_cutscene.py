"""Launch one cutscene standalone in a real pygame window -- exactly
maptest.py's own `cutscene` mode, minus the interactive prompts, so the
editor's "Test in game" button can launch it with a single subprocess call
instead of emulating maptest.py's stdin prompts (which would be one more
thing to keep in sync with maptest.py's own wording/ordering). Reuses
engine.renderer.run/OverworldScene directly, the same call maptest.py's own
cutscene branch makes.

    python tools/cutscene_editor/play_cutscene.py <cutscene_id>

Must be run with a real display (no SDL_VIDEODRIVER=dummy) -- server.py
(which itself runs headless, for map-render snapshots) spawns this as a
subprocess with that variable stripped, not inherited, so the player
actually sees a window.
"""
import sys
from functools import partial
from pathlib import Path

_REPO = Path(__file__).resolve().parent.parent.parent
sys.path.insert(0, str(_REPO))

from engine.config import cfg  # noqa: E402
from engine.cutscene import load_cutscene_defs  # noqa: E402
from engine.renderer import OverworldScene, run  # noqa: E402


def main() -> None:
    if len(sys.argv) != 2:
        print("usage: play_cutscene.py <cutscene_id>", file=sys.stderr)
        sys.exit(1)
    cutscene_id = sys.argv[1]

    defs = load_cutscene_defs()
    cutscene = defs.get(cutscene_id)
    if cutscene is None:
        print(f"no such cutscene: {cutscene_id!r}", file=sys.stderr)
        sys.exit(1)

    map_path = _REPO / "data" / "maps" / cutscene.map / f"{cutscene.map}.json"
    view_size = (cfg.view_cols * cfg.tile_px, cfg.view_rows * cfg.tile_px)
    run(
        partial(OverworldScene, map_path=map_path, play_cutscene=cutscene_id),
        view_size=view_size,
        scale=cfg.pygame_scale,
        title=f"Front House Gaiden — debug cutscene [{cutscene_id}]",
    )


if __name__ == "__main__":
    main()
