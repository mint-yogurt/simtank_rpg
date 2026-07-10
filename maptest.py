"""Map test entrypoint.

    python maptest.py

Prompts for: cave / town / hub

cave / town — SSE viewer, no party, no LLM.
hub         — WebSocket game server with player-controlled Melvin.
              Arrow keys move, collision works, NPCs animate.
              Open http://localhost:8765/ in your browser.
"""

import random
import sys
import threading


def main():
    choice = input("cave / town / hub? ").strip().lower()

    if choice == "hub":
        from game.server import app as game_app
        print("Starting hub (player mode) → http://localhost:8765/", flush=True)
        print("Arrow keys: move  |  Z: confirm  |  X: cancel", flush=True)
        game_app.run(host="0.0.0.0", port=8765, threaded=True, use_reloader=False)
        return

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
        print(f"Unknown choice {choice!r}. Use: cave, town, or hub")
        sys.exit(1)

    print(f"Starting web viewer → http://localhost:8765/  [{label}]", flush=True)
    threading.Thread(target=target, daemon=True).start()
    app.run(host="0.0.0.0", port=8765, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
