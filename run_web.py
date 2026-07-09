"""Run hub + overworld loop + SSE web server together.

    python run_web.py        # resume saved session, or hub → overworld
    python run_web.py hub    # force fresh start at the Front House

Opens http://localhost:8765/ — canvas viewer, SSE event stream.
"""

import random
import sys
import threading

from engine.scenes.hub import run_hub
from engine.worlddb import WorldDB
from overworld_loop import run_overworld
from web.server import (app, broadcast,
                        get_hub_tile_payload, get_screen_tile_payload,
                        get_interior_tile_payload, get_npc_sprite_url)


def _loop_thread_fresh(seed: int):
    result = run_hub(emit=broadcast, render_hub_fn=get_hub_tile_payload)
    if result == "overworld":
        run_overworld(seed, emit=broadcast, render_screen_fn=get_screen_tile_payload,
                      render_interior_fn=get_interior_tile_payload,
                      render_npc_sprite_fn=get_npc_sprite_url)


def _loop_thread_resume(seed: int, session: dict):
    run_overworld(seed, emit=broadcast, render_screen_fn=get_screen_tile_payload,
                  render_interior_fn=get_interior_tile_payload,
                  render_npc_sprite_fn=get_npc_sprite_url,
                  session=session)


def main():
    force_hub = len(sys.argv) > 1 and sys.argv[1] == "hub"

    if not force_hub:
        db = WorldDB("world.db")
        saved = db.load_session()
        db.close()
    else:
        saved = None

    if saved and saved.get("scene") == "overworld":
        seed = saved["world_seed"]
        print(f"Resuming saved session (seed={seed}) + web server on http://localhost:8765/",
              flush=True)
        t = threading.Thread(target=_loop_thread_resume, args=(seed, saved), daemon=True)
    else:
        seed = random.randint(0, 2**32 - 1)
        label = "forced fresh start" if force_hub else "hub → overworld"
        print(f"Starting game ({label}, seed={seed}) + web server on http://localhost:8765/",
              flush=True)
        t = threading.Thread(target=_loop_thread_fresh, args=(seed,), daemon=True)

    t.start()
    app.run(host="0.0.0.0", port=8765, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
