"""Run hub + overworld loop + SSE web server together.

    python run_web.py

Opens http://localhost:8765/ — canvas viewer, SSE event stream.

Game flow: hub scene (party roams, votes to leave) → overworld at screen (0,0).
"""

import random
import threading

from engine.scenes.hub import run_hub
from overworld_loop import run_overworld
from web.server import app, broadcast, render_hub_map, render_screen


def _loop_thread(seed: int):
    result = run_hub(emit=broadcast, render_hub_fn=render_hub_map)
    if result == "overworld":
        run_overworld(seed, emit=broadcast, render_screen_fn=render_screen)


def main():
    seed = random.randint(0, 2**32 - 1)
    print(f"Starting game (hub → overworld, seed={seed}) + web server on http://localhost:8765/",
          flush=True)

    t = threading.Thread(target=_loop_thread, args=(seed,), daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=8765, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
