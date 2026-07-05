# simtank_rpg

An LLM-powered, spectator-only hybrid of a turn-based 8-bit RPG and a
Tamagotchi-style pet simulator. Four AI party members live, chatter, vote, and
fight on their own — no player input (at first). You watch.

**Status:** pre-alpha. Battle loop is functional end-to-end. Overworld and cave/dungeon map generators are working with tile art, palette randomisation, and full feature scatter. Engine tile rules, SQLite world DB, line-of-sight tile scan, voting state machine, per-member journal, and overworld movement are all in place. Hub/overworld scenes not yet wired into the main game loop.

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

## What's built

### LLM decision layer

Each party member's turn works like this:

1. `llm/prompts.py` renders a compact context — member's own stats, party HP, situation, open vote tally if any, member's short-term journal window, and a numbered action menu that exactly matches the valid `available_actions` set. The character sheet (personality + special move) lives in the system prompt, not repeated here. Target: ~600–800 tokens in.
2. `llm/client.py` sends (system prompt, context) to the configured provider — either Ollama (local, primary) or Mistral API. `ask_with_retry` validates the response, reprompts once on bad output, then falls back to a safe default — never crashes. `LLMDecision` dataclass records raw output, retry, and whether fallback was used.
3. `llm/schema.py` parses the JSON response and validates the action against the `available_actions` set passed in — structural enforcement means the LLM cannot produce an action that isn't valid this turn even if it tries.
4. The controller (harness or future `game.py`) executes the resolved action. The engine does all resolution; the LLM only picked.

### Battle loop

Turn order: all four party members act, then the enemy. After each member's action, enemy death is checked. The loop ends on enemy defeat, party wipe, or after 100 rounds.

**Party members and specials:**

| Member | Special | Effect |
|---|---|---|
| BILLY | SING | Attempts to MESMERIZE the enemy — 50% skip chance, escalating break probability each turn |
| MELVIN | LAUGH | Attempts to inflict CRINGE — 35% chance enemy self-attacks each turn, lasts 3–5 turns |
| POOTS | SNACK | Heals a party member for 15–25% of max HP |
| SMELTRUD | TICKLE | Buffs an ally's damage by +15% for 2 turns |

**Status effects** are blocked from stacking — only one active at a time on the enemy. MESMERIZE uses an escalating drop-chance mechanic (10% → 25% → 50% → 80% per turn). CRINGE has a fixed randomly-rolled duration.

**Prompt tuning:** The action menu is situationally adjusted. SNACK is flagged as "low value" when nobody in the party is below 70% HP. SING and LAUGH are flagged as "no additional effect" when the enemy already has an active status, preventing the LLM from wasting turns trying to stack.

### Tile rules (`engine/tiles.py`)

Parses both tilerules files once on first call. Three public functions:

- `is_passable(tile)` — False if tile has `impassable` tag, or `wall` without `enterable`
- `is_enterable(tile)` — True if tile triggers a scene entry (cave, town, castle…)
- `tile_quality(tile)` — raw frozenset of quality strings from the tilerules file

Handles `:rot` rotation suffixes (e.g. `path_corner_N+E:90`). No PIL, no game-state deps.

### World database (`engine/worlddb.py`)

SQLite persistence for the discovered world. Two tables:

- **screens** — one row per coordinate pair; grid cached on first visit, exits pre-computed
- **features** — one row per interactable feature (cave, town, chest, etc.) with mutable state

`WorldDB.get_or_create_screen(world_seed, sx, sy, generator)` generates once and caches; subsequent calls are read-only. `_compute_exits` classifies each edge direction as open (passable + non-enterable tile on that edge) or closed. Replay guarantee: a fresh DB with the same world seed must produce identical grids, exits, and features.

### Voting state machine (`engine/voting.py`)

Group movement decisions run through a proposal → vote state machine. Any alive
member can `PROPOSE` a direction and step count. Each subsequent member votes
`YES` or `NO` on their turn. The proposal resolves as soon as the outcome is
mathematically locked — no need to wait for remaining votes.

Thresholds: 4 alive → 3 yes required; 3 alive → 2; 2 alive → unanimous; 1 alive → auto-pass.

`available_actions(member_name, voting_state)` returns the exact action set for
each member's turn: `{PROPOSE, WAIT}` when no proposal is open; `{VOTE, WAIT}`
when one is open and they haven't voted; `{WAIT}` if already voted. This set is
threaded into both the prompt menu and the schema parser — the LLM cannot output
an invalid action even if it tries.

Validated end-to-end against a real overworld screen in `procgen/voting_test.py`.

### Overworld movement (`engine/party_state.py`)

`execute_move(pos, direction, steps, grid, db, journals=None, tick=0)` steps the
party one tile at a time, scanning before each step. Stops on blocker, screen
edge, or arrival at an enterable feature (which marks it entered in the DB).
Mutates `PartyPos` in place. Accepts an optional journals dict and tick — MOVE and
ENTERED events fire inside the engine function so any caller (harness or future
game loop) gets journaling for free.

### Per-member journal (`engine/journal.py`)

Two independent layers:

- **`Journal`** — global milestone log. Structured events in; ALL-CAPS retro
  narrative out (`PARTY DEFEATED LVL 2 GOBLIN.`). Unbounded; for display/recap.
- **`MemberJournal`** — per-member FIFO rolling window (default 12 entries,
  ~120 tokens). Each entry: `(tick, event_type, terse_desc)`. Events: MOVE,
  ENTERED, PROPOSE, VOTE, RESOLVED. Injected into each member's overworld prompt
  as `RECENT EVENTS (your memory)`. Oldest entries drop automatically when the
  window is full. Battle events will be wired when the battle loop integrates.

`journals_append(journals, tick, type, desc)` broadcasts to all members' windows and is None-safe, so engine functions work cleanly in tests without journals.

### Viewscan (`engine/viewscan.py`)

Pure, headless, deterministic line-of-sight scan from the party's tile position
outward in the four cardinal directions. Used to build the spatial situational
context fed to each LLM call — no visual analysis, just a scripted data query.

Each ray walks tile-by-tile and terminates at the first:

- **enterable** tile (town, cave, castle…) → `kind='enterable'`
- **impassable** tile (cliff, lake, forest…) → `kind='blocker'`
- **screen edge** (walked off the grid) → `kind='edge'`

Returns a `ViewScan` dataclass with one frozen `DirectionScan` per direction
(`kind`, `tile`, `distance`, `adjacent_passable`). Distance 1 = the adjacent
tile. `adjacent_passable` tells the movement layer whether a step is legal
without re-querying the grid.

No display, no LLM, no DB. `procgen/viewscan_test.py` runs synthetic grid
assertions and real overworld screens with ASCII ray overlays.

### Procedural world generation

Two standalone test harnesses in `procgen/`, both output PNGs to `procgen/out/`.

Both generators expose a data/render split: `generate_*_data(seed)` → dataclass (no PIL), `render_*_data(data, raw_tiles)` → PIL image. The data layer is what `engine/worlddb.py` consumes.

**Overworld** (`procgen/overworld_test.py`) — infinite tiled world, one screen per coordinate pair:
- Base grass fill → blob placement (lakes with corner cuts, forest blobs, mountain rows, mnt blobs) → dirt patches → feature placement (towns, caves, castles, etc.) → jittered A\* path network → scatter (trees, ponds, individual mountains)
- Per-screen NES palette: 3 non-adjacent palette cells swapped into placeholder green/blue/brown
- Stable deterministic seed per (world\_seed, sx, sy) so any screen reproduces exactly

**Cave / dungeon** (`procgen/cave_test.py`) — interior maps for cave entrances placed on the overworld:
- Variable-size screen fitted to generated content
- Up to 8 rooms (tunable), two flavours mixed per map:
  - **Cave rooms** — cobble floor, 2-tile-tall cave walls (topper + wall) on north, `cavewallS` on south, void sides
  - **Dungeon rooms** — mixed `dungfloor`/`minidungfloor` tiles, brick walls; north wall is either `brickwallN` (1-tall) or `dungwallN + topper` (2-tall) per room
- Rooms connected by 1-tile-wide L-shaped or zig-zag hallways (random cave or dungeon style); hallways never receive south walls
- Two-pass wall derivation: north walls claim cells first so hallway side walls never cut off room north walls, and room side walls never block hallway north walls at junctions
- Entry tile (`enter`) placed on north wall of northernmost room; party spawns 1 tile south
- Water pools: 2×2 corner-only or 3×3+ edge+mid layout, size biased toward room size; `waterNW/N/NE/W/Mid/E/SW/S/SE` tiles
- Waterfalls: `waterfall1/2` alternating frames on cave north walls, terminating in `puddle1/2` on floor
- Scatter: skulls, socks, puddles in cave rooms; chests and trashcans in dungeon rooms
- Per-run NES palette: 3 placeholder colours (`#787878` grey, `#004058` teal, `#ac7c00` gold) swapped; black and white preserved

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
- **Journal.** Two layers. Global milestone log (`Journal`) for display/recap.
  Per-member short-term rolling window (`MemberJournal`, 12 entries) injected
  into each LLM call as `RECENT EVENTS`. Long-term compression is a later job.
- **Inventory.** Simple. 3 items per member, basic effects.
- **RNG.** Percentage-based checks for now. To-hit, crit, parry, saving throw,
  and damage each use float probability knobs tuned against normalized stat
  fractions. May re-express as d6 rolls later for animated dice. Number ranges
  TBD.
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
3. **Short-term memory** — `MemberJournal`: FIFO rolling window, last 12 events
   per member (~120 tokens). Built. Rendered as `RECENT EVENTS (your memory)`.
4. **Long-term memory** — old journal entries compressed into a few terse lines
   **in code** (templating, not a GM LLM call). Not yet built.

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
│   ├── combat.py           #
│   ├── voting.py           # proposal/vote state machine; available_actions()
│   ├── journal.py          # Journal (global narrative) + MemberJournal (per-member LLM window)
│   ├── memory.py           # builds the context blob for each LLM call
│   ├── tiles.py            # tile passability/quality lookup (parses tilerules files)
│   ├── viewscan.py         # line-of-sight tile scan (N/S/E/W rays → ViewScan dataclass)
│   ├── worlddb.py          # persistent world-state DB (SQLite; screens + features)
│   └── scenes/
│       ├── base.py         # Scene interface
│       ├── hub.py          # free-roam pet-sim mode
│       ├── overworld.py    # travel / exploration
│       └── battle.py       # combat mode
├── llm/
│   ├── client.py           # provider-agnostic call + routing; LLMDecision; ask_with_retry
│   ├── schema.py           # action schemas; parse_overworld_action (strict available_actions)
│   ├── prompts.py          # battle + overworld/voting prompt builders
│   └── ascii_map.py        # ASCII tile renderer (harness/debug only — not in LLM prompts)
├── procgen/
│   ├── names.py            # procedural name generation
│   ├── spritegen.py        # 16x16 enemy sprite generator
│   ├── overworld_test.py   # overworld screen generator — outputs PNGs to procgen/out/
│   ├── cave_test.py        # cave/dungeon interior generator — outputs PNGs to procgen/out/
│   ├── worlddb_test.py     # WorldDB integration tests incl. replay guarantee
│   ├── preview_test.py     # visual harness: party sprite composited onto generated screen → PNG
│   ├── viewscan_test.py    # viewscan tests: synthetic grids + real screens with ASCII ray overlay
│   └── voting_test.py      # voting + journal harness: real LLM, real movement, journal rollover
├── data/
│   └── party/              # character sheet JSONs
├── web/
│   ├── server.py           # SSE endpoint + serves static files
│   └── static/
│       ├── index.html
│       ├── app.js
│       ├── style.css
│       └── tiles/
│           ├── overworld_1.png             # overworld tileset (placeholder-coloured)
│           ├── overworld_1_tilerules.txt   # tile name ↔ grid coord map
│           ├── tiles_cave1.png             # cave/dungeon tileset
│           └── tiles_cave_rules.txt        # cave tile name ↔ grid coord map
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
2. [x] Procedural name generator (`procgen/names.py`)
3. [x] Engine skeleton: combat resolver, journal
4. [x] LLM client + schema + prompts (Ollama cloud / Mistral)
5. [x] Battle loop — full LLM-driven party vs. enemy, CLI output
6. [x] Sprite gen (proof of concept)
7. [x] Overworld map generator — infinite tiled world, NES palette, full feature set
8. [x] Cave/dungeon interior generator — rooms, hallways, water, waterfalls, palette
9. [x] Procgen/engine bridge — data/render split in both generators; `engine/tiles.py` (passability/quality); `engine/worlddb.py` (SQLite world state, replay-guaranteed)
10. [x] Viewscan — `engine/viewscan.py`: line-of-sight tile scan (N/S/E/W rays, terminates at enterable/blocker/edge); `procgen/preview_test.py`: party sprite preview harness
11. [x] Voting state machine — `engine/voting.py`: proposal/vote SM, early-lock resolution, threshold logic; overworld LLM prompts + `parse_overworld_action`; `ask_with_retry` + `LLMDecision`; `engine/party_state.py`: `execute_move`; `procgen/voting_test.py`: CLI harness with real LLM + movement
12. [x] Per-member short-term journal — `engine/journal.py`: `MemberJournal` FIFO window (12 entries), `journals_append` broadcast helper; `llm/prompts.py`: `RECENT EVENTS` section injected into overworld context; engine wiring in `execute_move`; validated with journal rollover in `voting_test.py`
13. [ ] Hub scene + free-roam / pet-sim mode
14. [ ] Wire procgen into engine scenes (overworld + cave entry/exit)
15. [ ] Long-term memory — compressed journal summary (templating, not a GM call)
16. [ ] SSE web viewer (text panel + canvas tile map)
17. [ ] (later) player inputs
