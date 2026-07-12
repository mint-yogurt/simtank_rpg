# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

For directory structure, file-by-file responsibilities, and content-pipeline format examples, **read README.md** — that information lives there and is not duplicated here.

## Project

**Front House Gaiden** (`simtank_rpg`) — an 8-bit RPG, developed **entirely locally** via a pygame renderer. Pre-alpha; engine is solid (movement, combat, procgen, tile rendering, enemy AI, battle loop), player-control layer (input, menus) is active work.

## History

This project started as an LLM-autonomous-party experiment (four AI-controlled characters playing themselves, watchable as a stream) with a browser/WebSocket client. That entire mode — the `web/`, `game/`, and `llm/` packages, `overworld_loop.py`, `run_cli.py`, `run_web.py`, `run_game.py`, `secrets.py`, and the AI-party-only engine modules (`context.py`, `goal.py`, `navlog.py`, `voting.py`, `party_state.py`, and the LLM-context half of `journal.py`) — has been **fully removed from the working tree**. It's recoverable from `git log` prior to the overhaul commit if any of it is ever needed again, but nothing in the current codebase should reference it, import it, or be modeled after it. The game is single-player, local-only, pygame-rendered — full stop.

`engine/battle.py` still has a leftover hard import of the now-deleted `llm` package (`from llm.client import ask`, etc.) — it is currently **broken and cannot be imported**. This is known and intentional; decoupling battle's action selection from the LLM call is deferred to a later pass, not something to patch reflexively. Don't "fix" it by re-adding an `llm` dependency or by guessing at a menu system that doesn't exist yet.

`engine/enemy_state.py` has a dangling import left over from a deleted module. Nothing currently imports `enemy_state.py`, so this doesn't break anything at runtime — same situation as `engine/battle.py` above: known, intentional, not something to reflexively "fix." NPC/agent placement will be rebuilt against the map's Tiled object layer once that exists — don't resurrect whatever old placement scheme it used as a stopgap.

## Hard scope rule

Everything under `web/`, `game/`, and `llm/` is gone. If you find a reference to any of them (an import, a roadmap line, a comment), it's stale — flag it and fix the reference, don't try to make the import work again.

## Architecture constraints

**The renderer is ONE file: `engine/renderer.py`.** The pygame app loop (window/clock/event dispatch), Tiled JSON map + tileset loading, sprite-sheet slicing, the camera, and the scene's handle_event/update/draw loop all live together in that single script, in `engine/`. This was previously split across a separate `pygame_viewer/` package (an `app.py`, `renderer.py`, `sprites.py`, plus dead code in `tileset.py`) — that split was explicitly rejected and the whole `pygame_viewer/` directory was deleted. **Do not recreate it.** Do not pull rendering back out into a second file, and do not invent a reason ("separation of concerns," "the engine should be headless") to re-split map loading from map drawing, or the app loop from the scene. One file. If it grows unwieldy, that's a conversation to have explicitly before acting, not a default to fall back on.

`engine/player.py` (movement rules) and `engine/input.py` (held-key direction resolution) stay separate, genuinely headless modules — they contain pure decision logic with zero knowledge of Tiled/GIDs/pygame surfaces, and `engine/renderer.py` imports them rather than duplicating them. That's a different kind of split (logic vs. its one consumer) from the rendering split above, which was purely "the same concern, spread across files for no reason" — don't conflate the two when deciding what belongs where.

**Entity position is tile-discrete.** `Player.row/col` (and similar) are plain ints — the engine has no concept of sub-tile position. Smooth/"in-between-tile" movement is a renderer-only concern, implemented as a separate float visual position that tweens toward the logical tile over `config.json`'s `pacing.player_move_ms`, then snaps once the tween completes. Never add fractional position fields to engine dataclasses to get smooth movement — interpolate in the renderer instead.

**Open tension, flagged not resolved:** `OverworldScene`'s NPC tweening (`engine/renderer.py`) now seeds a `"static"`-behavior NPC's *initial* visual position from its exact, un-floored Tiled pixel placement (`MapObject.row_exact`/`col_exact`) instead of `(float(row), float(col))`, so it renders exactly where it was placed in Tiled rather than snapped to the grid — see README's NPC section. `NPC.row/col` themselves are still plain ints and still drive every gameplay concern (wander stepping, collision, occupancy), and the float position still lives only in the renderer's tween dict, not on any engine dataclass — so this is compliant with the letter of the rule above. But it breaks an assumption the rule's wording implies: that a tween's visual position always eventually "snaps once the tween completes" back to a logical int tile. A static NPC never moves, so it never tweens again after being placed — its visual position can diverge from its logical tile *permanently*, not just mid-glide. That's a genuinely new case the rule didn't anticipate when it was written (it was describing player walk-cycle smoothing, not permanent off-grid placement of a non-moving entity). Whether the rule's wording should be tightened to explicitly carve out "a stationary entity's one-time initial visual position" as an allowed exception, whether this pattern should extend to other entity types, or whether it shouldn't exist at all, is a call for the user to make — don't extend this pattern further or "clean it up" one way or the other without asking first.

**Movement is cardinal-only — N/S/E/W, never diagonal.** Raw pygame key codes are mapped to direction strings and fed to `engine.input.HeldDirectionInput`, which resolves "last pressed wins" so two held keys never combine into a diagonal step or tween.

## Python environment

Always activate the venv before running Python:

```
source .venv/bin/activate
python maptest.py    # opens the pygame window, runs the real game loop
```

All dependencies (pygame, Pillow, PyYAML) are installed in `.venv/`.

## RNG

One global seeded RNG, seed logged at run start. Keep all randomness in engine code so replays work.
