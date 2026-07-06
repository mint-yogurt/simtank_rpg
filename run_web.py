"""Run overworld loop + SSE web server together.

    python run_web.py

Opens http://localhost:8765/ — canvas viewer, SSE event stream.
"""

import random
import threading

from overworld_loop import run_overworld
from web.server import app, broadcast, render_screen


def _loop_thread(seed: int):
    run_overworld(seed, emit=broadcast, render_screen_fn=render_screen)


def main():
    seed = random.randint(0, 2**32 - 1)
    print(f"Starting overworld loop (seed={seed}) + web server on http://localhost:8765/",
          flush=True)

    t = threading.Thread(target=_loop_thread, args=(seed,), daemon=True)
    t.start()

    app.run(host="0.0.0.0", port=8765, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
