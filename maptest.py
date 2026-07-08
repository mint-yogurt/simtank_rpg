"""Map test entrypoint.

    python maptest.py

Prompts for: cave / town / hub
Generates the chosen map and displays it in the normal web viewer at
http://localhost:8765/ with no party members and no LLM calls.
"""

import random
import sys
import threading

from web.server import app, broadcast


def main():
    choice = input("cave / town / hub? ").strip().lower()

    seed = random.randint(0, 2 ** 32 - 1)

    if choice == "cave":
        from tests.test_cave import run_cave_test
        target = lambda: run_cave_test(seed, broadcast)
        label = f"cave  seed={seed}"
    elif choice == "town":
        from tests.test_town import run_town_test
        target = lambda: run_town_test(seed, broadcast)
        label = f"town  seed={seed}"
    elif choice == "hub":
        from tests.test_hub import run_hub_test
        target = lambda: run_hub_test(broadcast)
        label = "hub (static)"
    else:
        print(f"Unknown choice {choice!r}. Use: cave, town, or hub")
        sys.exit(1)

    print(f"Starting web viewer → http://localhost:8765/  [{label}]", flush=True)
    threading.Thread(target=target, daemon=True).start()
    app.run(host="0.0.0.0", port=8765, threaded=True, use_reloader=False)


if __name__ == "__main__":
    main()
