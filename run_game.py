"""Entry point for the player-controlled game.

    source .venv/bin/activate
    python run_game.py

Opens a WebSocket game server at http://localhost:8766/.
Connect from your browser — arrow keys move Melvin.
"""

import sys
from game.server import app

if __name__ == "__main__":
    port = int(sys.argv[1]) if len(sys.argv) > 1 else 8766
    print(f"Front House Gaiden → http://localhost:{port}/", flush=True)
    app.run(host="0.0.0.0", port=port, threaded=True, use_reloader=False)
