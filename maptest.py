"""Debug entry point — runs ordinary engine/renderer code, not a reduced path.

    python maptest.py

The only thing this file decides is which scene to boot; the loop, input
handling, and rendering are the same code the real game runs.
"""

from engine.config import cfg
from engine.renderer import OverworldScene, run


def main():
    view_size = (cfg.view_cols * cfg.tile_px, cfg.view_rows * cfg.tile_px)
    run(OverworldScene, view_size=view_size, scale=cfg.pygame_scale, title="Front House Gaiden — debug")


if __name__ == "__main__":
    main()
