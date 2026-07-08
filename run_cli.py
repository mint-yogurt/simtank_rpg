"""Dev entry point: run the loop, print to terminal.

Usage:
  python run_cli.py            # full game: hub → overworld (default)
  python run_cli.py hub        # standalone hub test (exits after vote passes)
  python run_cli.py overworld  # skip hub, start directly in overworld
  python run_cli.py battle     # battle mode (single fight, then exit)
"""

import logging
import random
import sys
from pathlib import Path

from engine.battle import BattleEnemy, load_party, run_battle
from engine.combat import Fighter

logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")


# ---------------------------------------------------------------------------
# Entry points
# ---------------------------------------------------------------------------

def _run_battle_main() -> None:
    """Single battle, then exit."""
    seed = random.randint(0, 2**32 - 1)
    rng = random.Random(seed)
    print(f"SEED: {seed}", flush=True)

    party = load_party(Path(__file__).parent / "data" / "party")

    enemy_fighter = Fighter(
        "GORBUSHUS", iq=60, weight=180, sweat=4, hair=0, level=1, is_enemy=True
    )
    enemy = BattleEnemy(enemy_fighter)

    result = run_battle(party, enemy, rng)
    print(f"\nResult: {result['outcome']} in {result['rounds']} rounds.", flush=True)


def main() -> None:
    mode = sys.argv[1] if len(sys.argv) > 1 else "full"

    if mode == "battle":
        _run_battle_main()
    elif mode == "hub":
        from engine.scenes.hub import run_hub
        run_hub()
    elif mode == "overworld":
        from overworld_loop import run_overworld
        seed = random.randint(0, 2**32 - 1)
        run_overworld(seed)
    else:
        from engine.scenes.hub import run_hub
        from overworld_loop import run_overworld
        seed = random.randint(0, 2**32 - 1)
        result = run_hub()
        if result == "overworld":
            run_overworld(seed)


if __name__ == "__main__":
    main()
