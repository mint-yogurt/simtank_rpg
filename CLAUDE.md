# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Front House Gaiden** (`simtank_rpg`) — an 8-bit RPG, developed **entirely locally** via a pygame renderer. Pre-alpha; engine is solid (movement, combat, procgen, tile rendering, enemy AI, battle loop), player-control layer (input, menus) is active work.

## History

This project started as an LLM-autonomous-party experiment (four AI-controlled characters playing themselves, watchable as a stream) with a browser/WebSocket client. That entire mode — the `web/`, `game/`, and `llm/` packages, `overworld_loop.py`, `run_cli.py`, `run_web.py`, `run_game.py`, `secrets.py`, and the AI-party-only engine modules (`context.py`, `goal.py`, `navlog.py`, `voting.py`, `party_state.py`, and the LLM-context half of `journal.py`) — has been **fully removed from the working tree**. It's recoverable from `git log` prior to the overhaul commit if any of it is ever needed again, but nothing in the current codebase should reference it, import it, or be modeled after it. The game is single-player, local-only, pygame-rendered — full stop.

`engine/battle.py` still has a leftover hard import of the now-deleted `llm` package (`from llm.client import ask`, etc.) — it is currently **broken and cannot be imported**. This is known and intentional; decoupling battle's action selection from the LLM call is deferred to a later pass, not something to patch reflexively. Don't "fix" it by re-adding an `llm` dependency or by guessing at a menu system that doesn't exist yet.

## Hard scope rule

Everything under `web/`, `game/`, and `llm/` is gone. If you find a reference to any of them (an import, a roadmap line, a comment), it's stale — flag it and fix the reference, don't try to make the import work again.

**In scope — the local pygame game:**
- `engine/player.py`, `engine/input.py`, `engine/map_loader.py`, `engine/battle.py` (currently broken, see above), `engine/combat.py`, `engine/config.py`, `engine/enemy_state.py`, `engine/tiles.py`, `engine/viewscan.py`, `engine/worlddb.py`, `engine/pathfinding.py`, `engine/journal.py` (generic milestone log only)
- `engine/scenes/interior.py`
- `procgen/` (enemygen, npcgen, worldgen, cavegen, towngen) — shared generation code
- `pygame_viewer/` — the actual renderer (primary display layer)
- `assets/`, `data/`
- `maptest.py` (primary dev/debug entry point), `config.json`

`engine/map_loader.py` owns all map loading: the CSV+tilerules format the hub actually uses today (`load_hub_grid`, `hub_str_grid`, `hub_spawn_point`, etc.), and a YAML `MapData` loader for future authored maps (not wired into any scene yet). `engine/enemy_state.py` owns all NPC/agent placement, including the hub's fixed NPCs (`place_hub_npcs`). Neither of these should be duplicated into a per-scene file — if a scene needs map or NPC data, it calls into these modules directly.

## Architecture constraints

**The engine is headless and deterministic.** No display logic lives in `engine/` — `pygame_viewer/` is the sole consumer/renderer. Do not mix rendering into engine code.

**Entity position is tile-discrete.** `Player.row/col` (and similar) are plain ints — the engine has no concept of sub-tile position. Smooth/"in-between-tile" movement is a **renderer-only** concern: `pygame_viewer/hub.py` tracks a separate float visual position that tweens toward the engine's logical tile over `config.json`'s `pacing.player_move_ms`, then snaps the logical state once the tween completes. Never add fractional position fields to engine dataclasses to get smooth movement — interpolate in the renderer instead.

**Movement is cardinal-only — N/S/E/W, never diagonal.** The renderer maps raw pygame key codes to direction strings and feeds them to `engine.input.HeldDirectionInput`, which resolves "last pressed wins" so two held keys never combine into a diagonal step or tween. Raw key→direction mapping and visual tweening stay in `pygame_viewer/`; the held-key resolution algorithm itself lives in `engine/input.py` because it's pure decision logic, not a rendering concern.

## Python environment

Always activate the venv before running Python:

```
source .venv/bin/activate
python maptest.py    # opens the pygame window, player-controlled hub
```

All dependencies (pygame, Pillow, PyYAML) are installed in `.venv/`.

## RNG

One global seeded RNG, seed logged at run start. Keep all randomness in engine code so replays work.
