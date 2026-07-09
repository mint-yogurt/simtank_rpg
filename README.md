# simtank_rpg

An LLM-powered, spectator-only hybrid of a turn-based 8-bit RPG and a
Tamagotchi-style pet simulator. Four AI party members live, chatter, vote, and
fight on their own — no player input (at first). You watch.

**Status:** alpha → beta transition. Game starts on the **Front House** hub scene: four members roam the hand-authored home map independently; a leave vote can trigger at any time once someone reaches a map edge. Goal-setting and checkpoint decisions rotate through all alive members so no single character dominates. On vote pass, the overworld begins. Battle loop is functional end-to-end. Overworld runs a three-tier goal-driven loop: the party sets a persistent navigation goal via LLM (Tier 1), executes BFS-guided movement silently toward that goal (Tier 2), and pauses at checkpoints (goal reached, path blocked, genuine branch point) to discuss continue / abandon / modify via LLM (Tier 3). When the party steps onto a cave or town entrance they actually enter — navigating the interior via deterministic BFS. All four party members are rendered simultaneously in overworld and interior using a follow-the-leader formation: MELVIN leads at the current position, with BILLY, SMELTRUD, and POOTS each trailing 1 tile behind (stacked on spawn, spreading out as Melvin moves). Sprite movement is fully interpolated — each step animates smoothly over 400 ms with a 2-frame walk cycle that flips at the halfway point; the camera lerps to match. The web canvas draws scenes from a live tile-ID grid (SSE `tileset_url` + `tile_grid` per event) rather than baked PNGs. All pacing, display geometry, and tunable game parameters live in `config.json` — overworld and interior share a single `move_ms` value. Enemies roam the overworld and cave interiors (0–3 per overworld screen; 0–3 per cave room from a shared 6-entry pool per screen), generated once and cached in SQLite, with recoloured NPC sprites and one of three behavior types (wanderer/chaser, pacer, sentinel). Touching an enemy triggers a full LLM-driven battle with a DOM overlay (animated sprite, HP bars, action log). Towns are populated with 3–8 NPCs (one fixed healer + a mix of wanderers/pacers/statics), all with stub `"..."` dialogue awaiting the interaction system below. **Party wipe** teleports all members back to the hub at half HP/MP and clears the active goal, so the game continues automatically rather than stalling.

**Beta plan:** five phases remain before NPCs/encounters — see [Beta Roadmap](#beta-roadmap) below. Foundation (config) and persistent session state are done; navigation pacing, battle overhaul, healer interaction, and the battle screen redesign are planned in that order.

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
- **Procedural overworld, enemies, NPCs, towns.** Generated.
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
  fractions. May re-express as d6 rolls later for animated dice.
- **Viewing.** Watchable on the website. One-directional data flow
  (engine → viewer) over Server-Sent Events. Text panel + `<canvas>` tile map.

---

## What's built

### LLM decision layer

Each party member's turn works like this:

1. `llm/prompts.py` renders a compact context — member's own stats, party HP, situation, open vote tally if any, member's short-term journal window, and a numbered action menu that exactly matches the valid `available_actions` set. Edge directions with an open screen exit show `"can step? YES — screen edge (crossing available)"` so the LLM can propose crossing moves. The character sheet (personality + special move) lives in the system prompt, not repeated here. Target: ~600–800 tokens in.
2. `llm/client.py` sends (system prompt, context) to the configured provider — either Ollama (local, primary) or Mistral API. `ask_with_retry` validates the response, reprompts once on bad output, then falls back to a safe default — never crashes. `LLMDecision` dataclass records raw output, retry, and whether fallback was used.
3. `llm/schema.py` parses the JSON response and validates the action against the `available_actions` set passed in — structural enforcement means the LLM cannot produce an action that isn't valid this turn even if it tries.
4. The controller (harness or future `game.py`) executes the resolved action. The engine does all resolution; the LLM only picked.

### Voting state machine (`engine/voting.py`)

Group movement decisions run through a proposal → vote state machine. Any alive member can `PROPOSE` a direction and step count. Each subsequent member votes `YES` or `NO` on their turn. The proposal resolves as soon as the outcome is mathematically locked — no need to wait for remaining votes.

Thresholds: 4 alive → 3 yes required; 3 alive → 2; 2 alive → unanimous; 1 alive → auto-pass.

`available_actions(member_name, voting_state)` returns the exact action set for each member's turn: `{PROPOSE, WAIT}` when no proposal is open; `{VOTE, WAIT}` when one is open and they haven't voted; `{WAIT}` if already voted. This set is threaded into both the prompt menu and the schema parser — the LLM cannot output an invalid action even if it tries.

### Viewscan (`engine/viewscan.py`)

Pure, headless, deterministic line-of-sight scan from the party's tile position outward in the four cardinal directions. Used to build the spatial situational context fed to each LLM call — no visual analysis, just a scripted data query.

Each ray walks tile-by-tile and terminates at the first: **enterable** tile (town, cave, castle…) → `kind='enterable'`; **impassable** tile (cliff, lake, forest…) → `kind='blocker'`; **screen edge** → `kind='edge'`.

Returns a `ViewScan` dataclass with one frozen `DirectionScan` per direction (`kind`, `tile`, `distance`, `adjacent_passable`). No display, no LLM, no DB.

> **CRITICAL:** `llm/ascii_map.py`'s `render_map_overlay` (used by `viewscan_test.py`/`voting_test.py`) is for human debug output only and must **never** be fed into any LLM prompt context.

### Goal-driven navigation — three-tier loop (`overworld_loop.py`, `engine/pathfinding.py`, `engine/goal.py`)

The overworld loop is organised into three tiers so LLM calls happen only at decision points, not on every movement step.

**Tier 1 — Goal-setting.** When the party has no active goal, the current leader calls the LLM once to pick a target screen (`goal_type: explore | travel`, `target_sx/sy`). The result is a `Goal` object that persists until it completes, is abandoned, or is replaced. The leader rotates through all alive members via `leader_idx` (tracked across the whole session) so every character gets turns proposing goals and making checkpoint decisions.

**Tier 2 — Silent execution.** `screen_direction_toward()` picks the next screen exit via a greedy axis-preference rule. `bfs_to_exit()` finds the shortest intra-screen path — no LLM call. `path_to_segments()` compresses the BFS path into `(direction, steps)` pairs for `execute_move`. The party crosses screens automatically.

`_find_connected_start()` seeds a BFS from every walkable exit-edge tile inward to find the exit-connected component — used for initial placement and to recover from disconnected-pocket landings after a crossing.

**Tier 3 — Checkpoint discussion.** Four trigger points pause execution and call the LLM:

| Trigger | When | Terminal? |
|---|---|---|
| `goal_reached` | Party is already on the target screen | Yes — goal marked complete before discussion |
| `path_blocked` | BFS finds no path to exit after reconnect attempt | Yes — goal abandoned |
| `all_exits_blocked` | `screen_direction_toward` returns None (all exits closed) | Yes — goal abandoned |
| `branch_point` | Both axes equal distance to goal AND both exits open | No — goal stays active unless outcome is abandon/modify |

The LLM returns `continue | abandon | modify`. `modify` produces a new `Goal` immediately (skipping Tier 1). `branch_point` fires at most once per `(screen, target)` pair.

> **Phase 2 status:** Tier 2 step pacing is wired (`cfg.move_ms` per tile in `_execute_proposal`). Checkpoint discussion now includes a `WHAT YOU CAN SEE` ViewScan block and a `ENEMIES NEARBY` list for decision input. Interior navigation is LLM-driven (see below).

**Interior entry & feature detour.** When the party steps onto an enterable tile, `_enter_interior()` looks up the feature, computes the feature_id, dispatches to `generate_cave_data`/`generate_town_data`, calls `get_or_create_interior()` (generate-once/cache-forever), and emits `interior_init`/`interior_exit`. Since `bfs_to_exit` treats enterable tiles as walls (so screen-crossing doesn't stop mid-path), a detour block runs before each within-screen BFS: it routes to the nearest unvisited enterable feature via `bfs_to_tile()` (which allows enterables) and calls `_enter_interior()` on arrival. Only `FEATURE_TYPES`-recognized tiles are candidates.

### Navigation log & curated context (`engine/navlog.py`, `engine/context.py`)

`NavLog` is an append-only, unbounded event log (`SCREEN`, `GOAL`, `CHECKPOINT`, `ENTERED` — `MOVE` excluded as too numerous). `build_curated_context()` assembles a `CuratedContext` dataclass from NavLog + DB + party state: current position, active goal, last 8 notable NavLog entries, known enterable POIs with visited flags, party HP snapshot, visited-screen sample. This is the **only** feed into goal-setting and checkpoint prompts — no raw journal dumps, no wholesale DB queries in prompt builders. `llm/prompts.py` renders `build_goal_context()` and `build_checkpoint_context()` directly from this object.

### Per-member journal (`engine/journal.py`)

Two independent layers:

- **`Journal`** — global milestone log. Structured events in; ALL-CAPS retro narrative out (`PARTY DEFEATED LVL 2 GOBLIN.`). Unbounded; for display/recap.
- **`MemberJournal`** — per-member FIFO rolling window (default 12 entries, ~120 tokens). Events: MOVE, ENTERED, PROPOSE, VOTE, RESOLVED, SCREEN. Injected into each member's overworld prompt as `RECENT EVENTS (your memory)`. Oldest entries drop automatically when full.

`journals_append()` broadcasts to all members' windows and is None-safe.

### Hub scene (`engine/scenes/hub.py`)

The starting scene, the **Front House**. Four party members spawn 4 rows up from the bottom of a hand-authored 14×16 town map and roam it independently — each takes their own LLM turn: viewscan → direction pick → `execute_move`. No shared vote for movement; members wander freely.

**Leave vote.** When a member's `execute_move` returns `stop_reason='edge'`, `_run_leave_vote()` fires immediately: a fresh `VotingState` opens, the proposer's vote counts immediately, remaining alive members are polled one by one (VOTE-only action set — guarantees resolution). Threshold is the standard 3-of-4. On pass, `run_hub()` returns `"overworld"`. On fail, the proposer is nudged one step back from the edge.

### Interior scenes (`engine/scenes/interior.py`, `overworld_loop.py`)

`Interior` is a generic scene class for both cave/dungeon and town interiors, built from a deserialized interior data dict + `monster_spawn: bool` flag (True for caves, False for towns).

Key properties: `spawn`, `entry_tile` (exit-trigger tile), `combined_grid()` (merged floor/wall or ground/overlay dict), `healer_spawn` (towns only — the `healerHutwallMid` tile), `is_exit(row, col)`.

**Interior navigation** is LLM-driven: `_build_interior_pois` assembles a menu of Points of Interest (healer, explore spots, exit) from the interior layout. The leader picks a destination via LLM call, `_interior_bfs` finds the BFS path, and the party walks it with `interior_move` SSE events per step (`cfg.move_ms` sleep per tile). On arrival the LLM picks again — including "exit" — until the party leaves. `_interior_find_far_tile` floods from spawn for explore-spot discovery; `_do_healer_interaction` restores full HP when the party visits the healer.

### Overworld movement (`engine/party_state.py`)

`execute_move(pos, direction, steps, grid, db, journals, tick, generator)` steps the party one tile at a time, scanning before each step. Stops on blocker, screen edge (no generator), or arrival at an enterable feature. When a `generator` is supplied and a step would exit the screen, it crosses seamlessly instead of stopping — updates `pos.sx/sy`, generates/caches the adjacent screen, places the party at the mirrored entry tile, and continues remaining steps on the new screen. MOVE/ENTERED/SCREEN journal events fire accordingly.

### Enemy system (`procgen/enemygen.py`, `engine/enemy_state.py`, `engine/worlddb.py`)

Enemies are generated once per scope (screen or cave pool), stored in SQLite, and re-loaded on every visit.

**Generation.** `generate_enemies(seed, count, level, allow_overworld_sprite)` produces a deterministic list: name, combat stats (IQ, weight, sweat, hair, level), a random NPC sprite (`npc01`–`npc08`) with a `sprite_palette` (NES recolor triples), and a behavior type. Seed is derived via SHA-256 from the terrain seed so enemies survive screen regen.

**Scopes.** `screen` (overworld, 0–3 per screen) and `cave_screen` (a 6-entry level-2 pool per screen, shared across all caves on that screen; each room independently rolls 0–3 from the pool).

**Behavior types:** 1 — Wanderer/chaser (65% step toward party, 35% random); 2 — Pacer (walks an axis, reverses at obstacles); 3 — Sentinel (frozen until 8-tile cardinal LOS, then chases).

**Placement & movement.** `place_enemies()` deterministically places enemies on walkable tiles. `update_enemies()` advances all enemies one tile per party step (occupied-set-at-tick-start rule so movement isn't serially blocking). Cave interior placement is per-room via `_place_interior_enemies()`.

**Collision → battle.** Overworld: `_enemy_step` sets `pending_battle`; `_trigger_battle` fires after the move resolves. Caves: collision check runs inline in the step loop, battle runs synchronously, walk resumes.

**Sprite recoloring.** `web/server.py` extracts each NPC sprite's 2 frames, remaps placeholder pixel colors to the enemy's `sprite_palette`, and caches a 32×16 RGBA strip to `screens/npcsprite_{key}_{hash}.png`, referenced via `sprite_url` in SSE events (mirrors the tileset palette-swap pipeline). Overworld-scope enemies additionally get a large `overworld_sprite` (`enemy_overworld1`–`3`) drawn on the map; battle/cave contexts always use the recoloured NPC sprite.

### Town NPC system (`procgen/npcgen.py`, `engine/enemy_state.py`, `engine/worlddb.py`)

Each town interior contains 3–8 friendly NPCs, generated once and cached (`scope_type='town'`, `scope_id=feature_id`). Index 0 is always the healer (`name='HEALER'`, behavior type 3, fixed at the `healerHutwallMid` tile — impassable, so the party can never stand on it). The rest are random types 1/2/3 (wanderer/pacer/static) placed on random walkable tiles, seeded from `feature_id ^ 0xABCD1234`.

Movement runs through `update_town_npcs()` — no party-chasing, no collision-to-battle check. Rendered at `anim_ms: 500` (slower than the 350 ms enemy rate) via the same `enemies` SSE event and sprite pipeline as combat enemies.

**Dialogue stub.** All NPCs carry placeholder `"..."` dialogue. Face-to-face interaction is not yet implemented — see [Beta Phase 4](#phase-4--healer-npc-interaction).

### Battle loop (`engine/battle.py`)

Shared battle loop used by both CLI and web. `run_battle(party, enemy, rng, emit=None, battle_sleep_ms=800)` returns `{"outcome": "win"|"loss"|"flee"|"timeout", "rounds": N}`. Turn order: all four party members act, then the enemy; loop ends on enemy defeat, party wipe, or 100 rounds.

**Party wipe recovery.** On `outcome == "loss"` (overworld battle) or when all members are dead after exiting a cave interior, `_respawn_at_hub()` fires: all members are revived at half HP/MP, `pos` is teleported to screen (0,0), the hub screen grid and enemy list are reloaded, and `active_goal` is cleared so Tier 1 goal-setting triggers fresh on the next tick. The session is saved and an `init` SSE event re-anchors the web viewer on the hub screen.

**Party members and specials:**

| Member | Special | Effect |
|---|---|---|
| BILLY | SING | Attempts to MESMERIZE the enemy — 50% skip chance, escalating break probability each turn |
| MELVIN | LAUGH | Attempts to inflict CRINGE — 35% chance enemy self-attacks each turn, lasts 3–5 turns |
| POOTS | SNACK | Heals a party member for 15–25% of max HP |
| SMELTRUD | TICKLE | Buffs an ally's damage by +15% for 2 turns |

Status effects don't stack — one active at a time on the enemy. The action menu is situationally adjusted: SNACK flagged "low value" when nobody is below 70% HP; SING/LAUGH flagged "no additional effect" when the enemy already has a status.

> **Known gap (Beta Phase 3):** enemy HP currently makes even generic lvl-1 fights run boss-length; there's no mana/ability cost, no enemy level scaling with party progress, and no XP/leveling. See [Beta Phase 3](#phase-3--battle-overhaul).

**Web battle panel.** A DOM overlay (`#battle-overlay`) becomes visible on `battle_start` with an animated enemy sprite, per-member HP bars, and a VS label; `battle_action` updates HP/log; `battle_end` shows a result label for 2 s then hides. `web/server.py` snapshots the pre-battle overworld and restores it after, so late-joining clients see the battle in progress.

> **Known gap (Beta Phase 5):** this DOM overlay is a placeholder — the real battle screen (tileset-based, showing mana, with a tile-swipe entry transition) hasn't been built yet. See [Beta Phase 5](#phase-5--battle-screen-redesign).

### Tile rules (`engine/tiles.py`)

Parses both tilerules files once. `is_passable(tile)` (False if `impassable` tag, or `wall` without `enterable`; `is_passable(None)` guards sparse interior grids), `is_enterable(tile)`, `tile_quality(tile)`. Handles `:rot` rotation suffixes.

### World database (`engine/worlddb.py`)

SQLite persistence for the discovered world. Four tables:

- **screens** — one row per coordinate pair; grid cached on first visit, exits pre-computed
- **features** — one row per interactable feature, with mutable state (`entered`, `cleared`, `npc_flags`)
- **interiors** — one row per entered feature; grid cached on first entry keyed by `feature_id`
- **enemies** — one row per enemy/NPC per scope (`screen` / `cave_screen` / `town`); includes stats, sprite, behavior, `sprite_palette_json`, `overworld_sprite`

`get_or_create_screen()` / `get_or_create_interior()` generate once and cache; subsequent calls are read-only. `compute_feature_id()` returns a stable signed 64-bit hash so interior keys survive world reordering.

> **Note:** world/screen/feature/interior/enemy state persists across runs. Party session state (position, scene, HP, tick, active goal) also persists as of Beta Phase 1 — the game resumes where it left off on restart.

Replay guarantee: a fresh DB with the same world seed produces identical grids, exits, features, and interiors.

### Config (`engine/config.py`)

`config.json` at repo root + a `cfg` singleton. Covers pacing delays (single `move_ms` shared by overworld/interior, screen crossing, interior entry/exit), battle max rounds, display geometry (tile_px, scale, view_cols, view_rows), and interior max exploration distance. LLM provider stays in `secrets.py` by design. Vote thresholds are game-rule math tied to party size, not config values.

### Procedural world generation

Three generators in `procgen/`, all output PNGs to `procgen/out/` when run as harnesses. Overworld and cave/town generators expose a data/render split: `generate_*_data(seed)` → dataclass (no PIL), `render_*_data(data, raw_tiles)` → PIL image. The data layer is what `worlddb.py` consumes via `get_or_create_interior`.

**Overworld** (`procgen/worldgen.py`) — infinite tiled world, one screen per coordinate pair: base grass fill → blob placement (lakes, forests, mountains) → dirt patches → feature placement (towns, caves, castles) → jittered A* path network → scatter. Per-screen NES palette (3 non-adjacent cells swapped). Stable deterministic seed per `(world_seed, sx, sy)`. `FEATURE_TYPES` maps tile names to `"dungeon"`/`"town"`.

**Town** (`procgen/towngen.py`) — `generate_town_data(seed) → TownData`. Canvas sized to building count; cropped to bounding box + 5-tile margin. Healer hut (always present, fixed 3×2) seeds a cluster box; 1–9 additional buildings placed 92% in-cluster / 8% outlier. `TownData.healer_spawn` stores the healer wall tile's absolute position. Ground blobs, cobble MST paths, vegetation density-falloff rings, scatter decorations. 8-colour NES palette per run.

**Cave/dungeon** (`procgen/cavegen.py`) — `generate_cave_data(seed) → CaveData`. Up to 8 rooms, two flavours mixed per map (cave: cobble floor, 2-tile cave walls; dungeon: brick/dungfloor mix). Rooms connected by 1-tile L-shaped/zig-zag hallways. Two-pass wall derivation avoids junction conflicts. Water pools, waterfalls, scatter. 3-colour NES palette per run.

---

## Memory / context strategy

Tiered context, rebuilt each call:

1. **Character sheet** — re-injected fresh every call. Small, static.
2. **Situation** — code renders the current scene: location, what's happening, whose turn, valid actions, open vote tally. Written by code, not the model.
3. **Short-term memory** — `MemberJournal`: FIFO rolling window, last 12 events per member (~120 tokens). Rendered as `RECENT EVENTS (your memory)`.
4. **Long-term memory** — old journal entries compressed into terse lines **in code** (templating, not a GM LLM call). Not yet built.

Rough budget: ~600–800 tokens in, tiny out.

**Real constraint is rate limits, not context.** Not every member calls the LLM every tick; local Ollama has no rate limit and is the dev/prod workhorse; hosted providers (Mistral, AI Horde) sit behind the same client interface as fallbacks. A self-hosted Raspberry Pi LLM endpoint is planned as an additional provider.

---

## Determinism / replay

One global seeded RNG, seed logged at run start. Everything deterministic is reproducible, so replay comes nearly free: re-run a seed, re-emit the event log. The engine runs whether or not anyone is watching.

---

## Directory structure

```
simtank_rpg/
├── engine/                 # headless, deterministic, knows nothing about display
│   ├── game.py             # main loop; drives turns, owns the tick
│   ├── state.py            # full game state (party, world, scene, vote)
│   ├── party.py            # character model: stats, inventory, personality
│   ├── combat.py           #
│   ├── battle.py           # shared battle loop (CLI + web); run_battle() with emit= SSE hooks
│   ├── enemy_state.py      # EnemyAgent dataclass; place_enemies(); update_enemies() per-step AI
│   ├── voting.py           # proposal/vote state machine; available_actions()
│   ├── journal.py          # Journal (global narrative) + MemberJournal (per-member LLM window)
│   ├── navlog.py           # append-only navigation event log (SCREEN/GOAL/CHECKPOINT/ENTERED)
│   ├── context.py          # curated context builder → CuratedContext fed to goal/checkpoint prompts
│   ├── goal.py             # Goal dataclass (goal_type, target_sx/sy, status lifecycle)
│   ├── pathfinding.py      # bfs_to_exit, path_to_segments, screen_direction_toward
│   ├── memory.py           # builds the context blob for each LLM call
│   ├── tiles.py            # tile passability/quality lookup (parses tilerules files)
│   ├── viewscan.py         # line-of-sight tile scan (N/S/E/W rays → ViewScan dataclass)
│   ├── worlddb.py          # persistent world-state DB (SQLite; screens + features + interiors + enemies)
│   ├── config.py           # global config: tick length, LLM provider, viewport/follow/vote params
│   └── scenes/
│       ├── __init__.py
│       ├── hub.py          # Hub scene: 4-member independent roam + leave-vote; load_hub_coords/grid
│       └── interior.py     # Interior scene (cave or town); monster_spawn flag; combined_grid(); exit-tile return
├── llm/
│   ├── client.py           # provider-agnostic call + routing; LLMDecision; ask_with_retry
│   ├── schema.py           # action schemas; parse_overworld_action (strict available_actions)
│   ├── prompts.py          # battle + overworld/voting prompt builders
│   └── ascii_map.py        # ASCII tile renderer (harness/debug ONLY — never in LLM prompts)
├── procgen/
│   ├── names.py            # procedural name generation
│   ├── enemygen.py         # deterministic enemy generation (stats, sprite, behavior type, NES palette, overworld_sprite)
│   ├── npcgen.py           # town NPC generation (healer + wanderer/pacer/static pool, reuses enemygen sprite/palette)
│   ├── spritegen.py        # 16x16 enemy sprite generator
│   ├── worldgen.py         # overworld screen generator — outputs PNGs to procgen/out/
│   ├── cavegen.py          # cave/dungeon interior generator — outputs PNGs to procgen/out/
│   ├── towngen.py          # town interior generator — outputs PNGs to procgen/out/
│   ├── worlddb_test.py     # WorldDB integration tests incl. replay guarantee + interior replay
│   ├── preview_test.py     # visual harness: party sprite composited onto generated screen → PNG
│   ├── viewscan_test.py    # viewscan tests: synthetic grids + real screens with ASCII ray overlay
│   ├── voting_test.py      # voting + journal harness: real LLM, real movement, journal rollover
│   └── movement_test.py    # BFS pathfinding + screen-crossing harness (no LLM)
├── data/
│   └── party/              # character sheet JSONs
├── web/
│   ├── server.py           # SSE endpoint; render_screen (overworld); render_hub_map (hub PNG blit)
│   └── static/
│       ├── index.html
│       ├── app.js          # hub_init/hub_move + init/move/screen handlers; mode-aware 4-sprite draw
│       ├── style.css
│       └── tiles/
│           ├── overworld_1.png                  # overworld tileset (placeholder-coloured)
│           ├── overworld_1_tilerules.txt         # tile name ↔ grid coord map
│           ├── tiles_cave1.png                  # cave/dungeon tileset
│           ├── tiles_cave_rules.txt             # cave tile name ↔ grid coord map
│           ├── tiles_town.png                   # hub/town tileset (16×12 tiles, 16px each)
│           ├── tiles_town_rules.txt             # town tile name ↔ grid coord map
│           └── hub_map_coords_bracketed.csv     # hub layout: [tileset_col,tileset_row] per cell
├── overworld_loop.py       # production overworld loop (propose→vote→move→journal); shared by both runners
├── run_cli.py              # dev entry: hub→overworld by default; `overworld` skips hub; `battle` for single fight
├── run_web.py              # hub→overworld + SSE web server; hub map PNG rendered on startup
├── maptest.py              # map debug tool: prompts cave/town/hub, renders in web viewer, no party/LLM
├── tests/
│   ├── test_cave.py        # generate cave + enemies → interior_init + enemies SSE
│   ├── test_town.py        # generate town → interior_init SSE
│   └── test_hub.py         # load static hub → hub_init SSE
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
- Web service (FastAPI/Flask) behind Caddy reverse proxy, run as a systemd unit — same pattern as existing infra.
- `secrets.py` holds API keys and is gitignored. Repo backed up on GitHub.
- Activate `.venv/` before running: `source .venv/bin/activate` — all deps (Pillow etc.) live there.

---

## Beta Roadmap

Foundation is done (config, live tile-grid renderer, sprite animation). Five phases remain, in dependency order. Each should be scoped into its own Claude Code prompt/session.

### Phase 1 — Foundation (persistence)

- [x] **Global config** (`engine/config.py` / `config.json`) — done, see [Config](#config-engineconfigpy).
- [x] **Persistent party/session state.** `session` table added to `world.db` (`engine/worlddb.py`). Saves scene, world seed, position, tick, leader index, per-member HP, active goal, last 20 navlog entries, and per-member journal windows. Written on every notable event (goal-set, checkpoint, screen crossing, battle end, interior exit) — mirrors `NavLog`'s notable/non-notable split. `run_cli.py` / `run_web.py` load the session before picking a seed and resume directly in the overworld; a `hub` argument forces a fresh start at the Front House.

### Phase 2 — Navigation & decision-making overhaul

Closes the long-standing gap where the party performs a programmatic, pointless walk instead of actually playing. Builds on the existing three-tier goal system (Tiers 1–3 already eliminate oscillation — see [Goal-driven navigation](#goal-driven-navigation--three-tier-loop-overworld_loop-py-enginepathfinding-py-enginegoal-py)); this phase is about pacing and giving the LLM something real to react to.

- [x] **Tier 2 step pacing.** `cfg.move_ms` sleep per tile already wired in `_execute_proposal` — screen crossings are watchable.
- [x] **Viewscan-informed checkpoints.** `ViewScan` + `ENEMIES NEARBY` injected into checkpoint prompts as decision input only *(not for generating spoken lines)* — discussion reacts to what's actually visible right now.
- [x] **Real interior navigation.** Party makes LLM-driven decisions inside caves and towns: leader picks a POI, BFS walks there, LLM picks again on arrival (including exit). No scripted paths.

### Phase 3 — Battle overhaul

Do the numeric fixes before touching visuals (Phase 5 depends on the data this phase adds — mana, level — existing).

- [ ] **Enemy HP tuning.** Generic lvl-1 fights currently run boss-length. Pure numeric pass on `procgen/enemygen.py` HP generation — lower the base/scaling formula. Do this first; it's the fastest quality-of-life win and unblocks meaningful playtesting of everything else in this phase.
- [ ] **Mana / ability costs.** Add an `MP` stat to `engine/party.py` alongside HP; each special move (SING/LAUGH/SNACK/TICKLE) gets a cost. `llm/prompts.py` action menu must show current MP and grey out (mark unavailable) specials the member can't afford — mirrors the existing SNACK "low value" flagging pattern. `engine/battle.py` deducts MP on use; regen behavior (per-turn trickle vs. only-at-healer) is a design call — leaning toward "only restored by the healer" (Phase 4) to give that NPC's interaction actual stakes.
- [ ] **Enemy level scaling.** `generate_enemies()` already accepts a `level` param — wire average party level into the call. Since enemy generation already follows the DB's generate-once/cache-forever pattern (`screen`/`cave_screen` scopes), scaling naturally locks at first generation per screen/pool with no extra bookkeeping: pass `avg_party_level ± 2` (clamped ≥1) as `level` the first time a scope is generated.
- [ ] **XP tracking / level-up.** Add `XP` + `LVL` progression to `engine/party.py`. `engine/battle.py` awards XP to the party on `outcome == "win"`; a leveling formula bumps stats (HP/MP ceiling, IQ/WEIGHT/SWEAT/HAIR) on threshold crossing. Feeds the level value used by enemy scaling above, and the XP bar planned for the web UI party panel (backlog).

### Phase 4 — Healer NPC interaction

Self-contained, good as a breather task between Phases 2/3 and 5.

- [ ] **Face-to-face interaction trigger.** Party member steps adjacent to and facing an NPC (direction check against NPC tile). Start narrow: hardcode the trigger for the healer only (`name == 'HEALER'`), rather than building the general NPC dialogue system (`27a` backlog) up front.
- [ ] **Heal effect.** Deterministic, no LLM needed: on interaction, restore full HP (and, once Phase 3 lands, full MP) to the whole party. Optional single LLM-flavor line for the healer's "dialogue" instead of the current `"..."` stub — small enough to not need the full dialogue system.
- [ ] Town NPC placement/behavior already exists (`procgen/npcgen.py`, `engine/scenes/interior.py Interior.healer_spawn`) — this phase is purely the trigger + effect, no new generation work.

### Phase 5 — Battle screen redesign

Last, since it's the most visually involved and should sit on stable data (mana, level, HP scaling from Phase 3).

- [ ] **Tile-swipe transition.** On battle entry, animate each overworld/interior tile swiping to black before the battle screen appears. Likely a new SSE event (or a flag on the existing `battle_start` event) carrying enough info for the JS canvas to animate the current tile grid out; reverse (or fade back to the SSE-restored snapshot) on `battle_end`.
- [ ] **New battle screen (tileset-based).** Replace the current DOM overlay (`#battle-overlay`) with a canvas-rendered screen using the tileset pipeline already in place for overworld/interior — background art TBD/user-designed. Must show: enemy HP, party HP **and mana** (new, from Phase 3), and all five sprites (party ×4 + enemy) with existing frame-flip animation reused from `drawEnemySprites`/party sprite drawing.
- [ ] Battle-speed config (separate `battle_action_delay` from `move_ms`) is a natural companion to this phase — see Backlog.

---

## Backlog (post-beta — each needs its own expanded spec before work starts)

- **NPC dialogue + interaction (general)** — extend Phase 4's healer-only trigger into a general face-to-face system for all NPC types, LLM-generated dialogue replacing the `"..."` stub, per-NPC persistent state via `features.npc_flags_json`.
- **Enemy respawn / persistence** — enemies are currently cleared after battle regardless of outcome; add a respawn cooldown (or cleared flag) so beaten enemies don't reappear until the screen is reloaded.
- **Web UI party status panel** — HTML/JS panel alongside the canvas viewport: per-member HP, level, XP bar, animated sprite per row.
- **Battle-speed config** — separate config value for battle action delay, independent of `move_ms`.
- **Random encounters** — beyond the current touch-to-battle model.
- **Long-term memory** — compressed journal summary (templating, not a GM call).
- **Overworld/cave NPC expansion** — see Post-beta priority note in README status.
- **Namegen for towns and dungeons** — assign procedurally generated proper names to each feature on first discovery: "Village of Keth", "Mowden Pass", "Skull Keep", etc. Hook into `procgen/names.py` (already exists) via `get_or_create_interior` or on feature first-enter; store in the `features` table (`name` column); surface in context POI lists and interior prompts so the party refers to locations by name rather than screen coordinates. This would replace the bare `feature_type` labels currently shown to the LLM and on any future UI.
- **Heal in place** — currently the only way to restore HP/MP is by visiting a town healer. A future "rest" action (camp at current tile, recover a fixed % HP, costs ticks) or item-based healing would give the party a real option when wounded far from a town. The `battle_wounds` checkpoint already asks the party to decide, but right now "continue" and "go to a town" are the only viable paths.
- **(later) player inputs.**

---

## Roadmap history

1. [x] Scaffold + README
2. [x] Procedural name generator (`procgen/names.py`)
3. [x] Engine skeleton: combat resolver, journal
4. [x] LLM client + schema + prompts (Ollama cloud / Mistral)
5. [x] Battle loop — full LLM-driven party vs. enemy, CLI output
6. [x] Sprite gen (proof of concept)
7. [x] Overworld map generator — infinite tiled world, NES palette, full feature set
8. [x] Cave/dungeon interior generator — rooms, hallways, water, waterfalls, palette
8a. [x] Town interior generator — healer hut, buildings, ground, vegetation, MST cobble paths, scatter; 8-colour NES palette
9. [x] Procgen/engine bridge — data/render split; `engine/tiles.py`; `engine/worlddb.py` (replay-guaranteed)
10. [x] Viewscan + `procgen/preview_test.py` sprite preview harness
11. [x] Voting state machine, overworld LLM prompts, `execute_move`, `procgen/voting_test.py`
12. [x] Per-member short-term journal (`MemberJournal`, `RECENT EVENTS` injection)
13. [x] Multi-screen crossing in `execute_move`
14. [x] Wire overworld loop into production runners (`overworld_loop.py`, `run_cli.py`, `run_web.py`)
15. [x] SSE web viewer (Part 1) — Flask SSE endpoint, canvas rendering, proposer rotation fix
16. [x] Goal-driven navigation (Tier 1/2) — `engine/goal.py`, `engine/pathfinding.py`, disconnected-pocket recovery
17. [x] Curated context + checkpoint discussion (Tier 3) — `engine/navlog.py`, `engine/context.py`
18. [x] Wire overworld to interior generation — `worlddb.py` interiors table, `Interior` scene class
19. [x] Interior navigation (basic BFS walk-and-return)
20. [x] Hub scene — Front House, independent roam, leave-vote
20a. [x] Leader rotation across goal-setting and checkpoints
21. [x] Global config (`config.json` / `engine/config.py`)
22. [x] Live tile-grid renderer — `{tileset_url, tile_grid}` SSE payloads, `drawTile()`/`drawTileGrid()`
23. [x] Sprite animation + follow-the-leader formation
24. [x] Enemies on overworld — generation, placement, movement, battle trigger, SSE, DOM battle overlay
25. [x] Enemies in cave interiors — `cave_screen` scope, per-room placement, inline collision
25a. [x] NPC sprite recoloring + expanded sprite pool + overworld-exclusive sprites
26. [x] Town NPCs — healer + wanderer/pacer/static pool, town crop-origin fix

27. [x] Persistent session state — `session` table in `world.db`; resume on boot; `hub` arg forces fresh start
28. [x] Party wipe respawn — on battle loss or cave wipe, revive at half HP/MP and teleport to hub; goal clears; game continues

*(Beta Phase 2–5 above are the active continuation of this roadmap.)*
