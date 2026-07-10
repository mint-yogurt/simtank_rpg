"""Map test / dev launcher.

    python maptest.py

Choices:
  hub   — pygame window (primary dev tool): player-controlled hub,
          arrow keys, NPC animation, collision.

Web viewer (SSE + browser, legacy dev paths):
  cave  — generate a cave, push to SSE web viewer
  town  — generate a town, push to SSE web viewer
  web   — web viewer paths start a Flask server on http://localhost:8765/
"""

import random
import sys


def main():
    choice = input("hub / cave / town? ").strip().lower()

    # ── primary: pygame hub ───────────────────────────────────────────────────
    if choice == "hub":
        from pygame_viewer.hub import run_hub_pygame
        run_hub_pygame()
        return

    # ── legacy web-viewer paths (SSE + browser) ───────────────────────────────
    import threading
    from web.server import app, broadcast

    seed = random.randint(0, 2 ** 32 - 1)

    if choice == "cave":
        from tests.test_cave import run_cave_test
        target = lambda: run_cave_test(seed, broadcast)
        label = f"cave  seed={seed}"
    elif choice == "town":
        from tests.test_town import run_town_test
        target = lambda: run_town_test(seed, broadcast)
        label = f"town  seed={seed}"
    else:
        print(f"Unknown choice {choice!r}. Use: hub, cave, or town")
        sys.exit(1)

    print(f"Starting web viewer → http://localhost:8765/  [{label}]", flush=True)
    threading.Thread(target=target, daemon=True).start()
    app.run(host="0.0.0.0", port=8765, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
