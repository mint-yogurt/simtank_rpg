# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

**Front House Gaiden** (`simtank_rpg`) — an 8-bit RPG, developed **entirely locally** via a pygame renderer. Pre-alpha; engine is solid (movement, combat, procgen, tile rendering, enemy AI, battle loop), player-control layer (input, menus) is active work.

## Hard scope rule — read this first

**Ignore all browser-related and LLM-related files completely.** Do not edit them, reference them, read them for context, or reason about them in any way, even if a task seems adjacent. The game is single-player and local-only now; there is no browser client and no AI/LLM mode in scope.

**Out of scope — never touch:**
- `web/` (entire directory — SSE viewer, tilemap gen, browser static assets)
- `game/` (entire directory — WebSocket transport + browser canvas client)
- `llm/` (entire directory — LLM client, prompts, schema, ascii debug renderer)
- `overworld_loop.py` (AI-mode autonomous party loop)
- `run_cli.py`, `run_web.py`, `run_game.py` (AI-mode / browser entry points)
- `secrets.py` (LLM provider API keys only)
- `engine/context.py`, `engine/goal.py`, `engine/journal.py`, `engine/navlog.py`, `engine/voting.py`, `engine/party_state.py` (AI-party-specific engine modules that only feed the LLM loop — not used by the local single-player path)

**In scope — the local pygame game:**
- `engine/player.py`, `map_loader.py`, `battle.py`, `combat.py`, `config.py`, `enemy_state.py`, `tiles.py`, `viewscan.py`, `worlddb.py`, `pathfinding.py`
- `engine/scenes/hub.py`, `engine/scenes/interior.py`
- `procgen/` (enemygen, npcgen, worldgen, cavegen, towngen) — shared generation code
- `pygame_viewer/` — the actual renderer (primary display layer now)
- `assets/`, `data/`
- `maptest.py` (primary dev/debug entry point), `config.json`

If a task looks like it might touch something in the out-of-scope list, say so and ask rather than guessing.

## Architecture constraints

**The engine is headless and deterministic.** No display logic lives in `engine/` — `pygame_viewer/` is the sole consumer/renderer. Do not mix rendering into engine code.

**Entity position is tile-discrete.** `Player.row/col` (and similar) are plain ints — the engine has no concept of sub-tile position. Smooth/"in-between-tile" movement is a **renderer-only** concern: `pygame_viewer/hub.py` tracks a separate float visual position that tweens toward the engine's logical tile over `config.json`'s `pacing.player_move_ms`, then snaps the logical state once the tween completes. Never add fractional position fields to engine dataclasses to get smooth movement — interpolate in the renderer instead.

**Movement is cardinal-only — N/S/E/W, never diagonal.** Input handling must guarantee at most one direction is resolved per tick (see the held-keys / last-pressed-wins pattern in `pygame_viewer/hub.py`); never let two direction keys combine into a single diagonal step or tween.

## Python environment

Always activate the venv before running Python:

```
source .venv/bin/activate
python maptest.py    # pick "hub" — opens the pygame window, player-controlled
```

All dependencies (including Pillow) are installed in `.venv/`.

## RNG

One global seeded RNG, seed logged at run start. Keep all randomness in engine code so replays work.
