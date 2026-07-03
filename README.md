# simtank_rpg

An LLM-powered, spectator-only hybrid of a turn-based 8-bit RPG and a
Tamagotchi-style pet simulator. Four AI party members live, chatter, vote, and
fight on their own — no player input (at first). You watch.

**Status:** pre-alpha / scaffolding. CLI-first, web viewer later.

---

## Core principle

The LLM is a **thin decision layer**. Everything else is deterministic code.

Given a tightly-constrained situation and an enumerated menu of valid actions,
an LLM picks one and optionally emits a line of flavor. It does **not** roll
dice, resolve combat, track inventory, manage turn order, or manage its own
memory. All of that is engine code.

This keeps token usage low, output reliable, runs reproducible, and bugs
debuggable.

---

## Scope (proof-of-concept)

- **4 party members.** Minimal stats + short personality blurb, stored as JSON
  character sheets. Sheet is re-injected into the system prompt every call —
  that, not chat history, is what keeps them in character.
- **Hub ("home").** Hand-authored, non-procedural. Starting area and main hub.
  Members move freely and talk via a shared "global chat," reacting to stimuli
  and making group decisions. This is the pet-sim mode.
- **Procedural overworld, enemies, NPCs, towns.** Generated. (Details later —
  not day one.)
- **Voting.** Group decisions (e.g. leaving the hub to adventure) run through a
  proposal → vote state machine. A member's response can open a proposal; each
  subsequent member flags yes/no on their turn; threshold (e.g. 3 of 4) carries
  it. Threshold is configurable per situation.
- **Combat.** Members decide their own actions (attack / defend / run / item).
  Whole party gets combat context.
- **Journal.** Terse, scripted event log. Structured events in, dry retro-log
  narration out. `PARTY FOUGHT LVL 2 GOBLIN... AND WON. BILLY REACHED LEVEL 3.`
- **Inventory.** Simple. 3 items per member, basic effects.
- **RNG.** d6-based. Compare a stat or two vs. the enemy, roll N d6, resolve on
  threshold. Numbers stay small — no 1000-damage hits.
- **Viewing.** Watchable on the website. One-directional data flow
  (engine → viewer) over Server-Sent Events. Text panel + `<canvas>` tile map.
  CLI text output first; graphics bolted on later.

---

## Memory / context strategy

Tiered context, rebuilt each call. Only the last tier is non-trivial.

1. **Character sheet** — re-injected fresh every call. Small, static.
2. **Situation** — code renders the current scene to a compact description:
   location, what's happening, whose turn, valid actions this turn, open vote
   tally. Written by code, not the model.
3. **Short-term memory** — rolling window of the last ~6–10 journal lines.
4. **Long-term memory** — old journal entries compressed into a few terse lines
   **in code** (templating, not a GM LLM call).

Rough budget: ~600–800 tokens in, tiny out. Comfortable on free tiers.

**Real constraint is rate limits, not context.** Mitigations: not every member
calls the LLM every tick (ambient chatter can be table-driven); local Ollama has
no rate limit and is the dev/prod workhorse; hosted providers (Mistral, AI
Horde) sit behind the same client interface as fallbacks.

---

## Determinism / replay

One global seeded RNG, seed logged at run start. Everything deterministic is
reproducible, so replay comes nearly free: re-run a seed, re-emit the event log.
The engine runs whether or not anyone is watching.

---

## Directory structure

```
simtank_rpg/
├── engine/                 # headless, deterministic, knows nothing about display
│   ├── game.py             # main loop; drives turns, owns the tick
│   ├── state.py            # full game state (party, world, scene, vote)
│   ├── party.py            # character model: stats, inventory, personality
│   ├── combat.py           # d6 resolver — all dice live here
│   ├── voting.py           # proposal/vote state machine
│   ├── journal.py          # event log (structured + narrative views)
│   ├── memory.py           # builds the context blob for each LLM call
│   └── scenes/
│       ├── base.py         # Scene interface
│       ├── hub.py          # free-roam pet-sim mode
│       ├── overworld.py    # travel / exploration
│       └── battle.py       # combat mode
├── llm/
│   ├── client.py           # provider-agnostic call + routing (Ollama/Mistral/Horde)
│   ├── schema.py           # action schemas + forgiving JSON parse/validate
│   └── prompts.py          # prompt templates
├── procgen/
│   ├── names.py            # procedural name generation (scope TBD)
│   ├── sprites.py          # 16x16 pixel gen (later)
│   └── world.py            # map gen (later)
├── data/
│   └── party/              # character sheet JSONs
├── web/
│   ├── server.py           # SSE endpoint + serves static files
│   └── static/
│       ├── index.html
│       ├── app.js
│       └── style.css
├── run_cli.py              # dev entry: loop → terminal text
├── run_web.py              # loop + web server
├── secrets.py              # API keys (gitignored)
├── .gitignore
└── README.md
```

The load-bearing seam: **the engine emits events and knows nothing about how
they're shown.** CLI and web renderers are both just consumers of that event
stream. Get the whole game working in text, then bolt on the web layer.

---

## Tooling / ops

- Engine + all game logic in **Python**. JS only for the dumb web renderer.
- Dev via SSH; coding with Claude Code in-terminal.
- Web service (FastAPI/Flask) behind Caddy reverse proxy, run as a systemd unit
  — same pattern as existing infra.
- `secrets.py` holds API keys and is gitignored. Repo backed up on GitHub.

---

## Roadmap

1. [x] Scaffold + README
2. [ ] Procedural name generator (`procgen/names.py`) — scope pending
3. [ ] Engine skeleton: state, party model, journal
4. [ ] d6 combat resolver
5. [ ] Voting state machine
6. [ ] LLM client + schema + prompts (Ollama first)
7. [ ] Hub scene, CLI loop
8. [ ] Overworld + battle scenes
9. [ ] Memory tiers wired into calls
10. [ ] SSE web viewer (text panel)
11. [ ] Sprite gen + canvas tile map
12. [ ] World gen
13. [ ] (later) player inputs
