# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Project

LLM-powered spectator RPG (`simtank_rpg`). Four AI party members run autonomously; the player only watches. Pre-alpha — mostly scaffolding.

## Architecture constraints

**The LLM is a thin decision layer.** Given a constrained situation and an enumerated list of valid actions, it picks one and optionally emits flavor text. It does **not** roll dice, resolve combat, track inventory, manage turn order, or manage its own memory. Violations here are bugs.

**The engine emits events and knows nothing about display.** `engine/` is headless and deterministic. CLI and web are both consumers of the event stream. Do not mix display logic into engine code.

**Context is rebuilt every LLM call** — tiered: character sheet → situation → short-term journal window → compressed long-term summary. Character sheets are re-injected fresh each call (not chat history) to keep members in character. Target: ~600–800 tokens in, small out.

## Secrets

`secrets.py` is gitignored and holds API keys. It **shadows Python's stdlib `secrets` module** — intentional. Do not `import secrets` expecting the stdlib module.

LLM providers in priority order: Ollama (local, no rate limit, primary), Mistral API, AI Horde. All go through the provider-agnostic client in `llm/client.py`.

## Python environment

Always activate the venv before running Python:

```
source .venv/bin/activate
python run_cli.py    # dev loop, text to terminal
python run_web.py    # loop + SSE web server
```

All dependencies (including Pillow) are installed in `.venv/`.

## Web viewer

One-directional: engine → viewer via Server-Sent Events. Text panel + `<canvas>` tile map. JS in `web/static/` is a dumb renderer only — no game logic there.

## RNG

One global seeded RNG, seed logged at run start. Keep all randomness in engine code (not LLM calls) so replays work.
