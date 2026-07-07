# simtank_rpg

An LLM-powered, spectator-only hybrid of a turn-based 8-bit RPG and a
Tamagotchi-style pet simulator. Four AI party members live, chatter, vote, and
fight on their own — no player input (at first). You watch.

**Status:** pre-alpha. Game starts on the **Front House** hub scene: four members roam the hand-authored home map independently for at least 12 ticks before a leave vote can trigger. Goal-setting and checkpoint decisions rotate through all four members so no single character dominates. On vote pass, the overworld begins. Battle loop is functional end-to-end. Overworld runs a three-tier goal-driven loop: the party sets a persistent navigation goal via LLM (Tier 1), executes BFS-guided movement silently toward that goal (Tier 2), and pauses at checkpoints (goal reached, path blocked, genuine branch point) to discuss continue / abandon / modify via LLM (Tier 3). When the party steps onto a cave or town entrance they now actually enter — navigating the interior via deterministic BFS (explore to farthest reachable point, then exit), rendered as a separate PNG with viewport/camera scrolling on the web canvas. `run_cli.py` defaults to hub → overworld; `run_cli.py battle` runs a single fight.

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

1. `llm/prompts.py` renders a compact context — member's own stats, party HP, situation, open vote tally if any, member's short-term journal window, and a numbered action menu that exactly matches the valid `available_actions` set. Edge directions with an open screen exit show `"can step? YES — screen edge (crossing available)"` so the LLM can propose crossing moves. The character sheet (personality + special move) lives in the system prompt, not repeated here. Target: ~600–800 tokens in.
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

SQLite persistence for the discovered world. Three tables:

- **screens** — one row per coordinate pair; grid cached on first visit, exits pre-computed
- **features** — one row per interactable feature (cave, town, chest, etc.) with mutable state (`entered`, `cleared`, `npc_flags`)
- **interiors** — one row per entered feature; interior grid cached on first entry keyed by `feature_id`

`WorldDB.get_or_create_screen(world_seed, sx, sy, generator)` generates once and caches; subsequent calls are read-only. `_compute_exits` classifies each edge direction as open (passable + non-enterable tile on that edge) or closed.

`WorldDB.get_or_create_interior(world_seed, feature_id, generator)` follows the same generate-once pattern for interior locations. `compute_feature_id(world_seed, sx, sy, local_row, local_col)` returns a stable signed 64-bit hash so the interior key doesn't change if the world reorders screens. Interior seed is derived from `(world_seed, feature_id)` — stable and independent of screen coordinates.

Replay guarantee: a fresh DB with the same world seed must produce identical grids, exits, features, and interiors.

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

`execute_move(pos, direction, steps, grid, db, journals=None, tick=0, generator=None)`
steps the party one tile at a time, scanning before each step. Stops on blocker,
screen edge (no generator), or arrival at an enterable feature (which marks it
entered in the DB). Mutates `PartyPos` in place.

**Multi-screen crossing:** when `generator` is provided and a step would exit the
screen edge, the function crosses seamlessly instead of stopping — `pos.sx/sy`
updates, `enter_screen()` generates/caches the adjacent screen via `get_or_create_screen`,
and the party appears at the mirrored entry tile (same column for N/S crossings,
same row for E/W). Remaining steps continue on the new screen. The crossing counts
as one step. `MoveResult.final_grid` always carries the grid the party ended on —
callers use it to refresh their local grid reference after any movement.

MOVE and ENTERED events fire at each stop. SCREEN events fire on each crossing.
All are journal-broadcast-safe (journals=None is fine in tests).

### Hub scene (`engine/scenes/hub.py`)

The starting scene, called the **Front House**. Four party members spawn 4 rows up from the bottom of a hand-authored 14×16 town map and roam it independently — each takes their own LLM turn: viewscan → direction pick → `execute_move`. No shared vote for movement; members wander freely.

**Leave vote.** A leave vote cannot trigger until `MIN_HUB_TICKS = 12` ticks have elapsed — this prevents the party from instantly bolting on startup. After that, when a member's `execute_move` returns `stop_reason='edge'`, they've reached a map boundary and `_run_leave_vote()` fires: a fresh `VotingState` is opened, the proposer's vote counts immediately, and the remaining alive members are polled one by one with a VOTE-only action set (no WAIT — guarantees resolution). Threshold is the standard 3-of-4. On pass, `run_hub()` returns `"overworld"` and the caller chains into the overworld at screen (0,0). On fail (or if the tick guard blocks the vote), the proposer is nudged one step back from the edge (perpendicular fallback if opposite is blocked) and a corrective `hub_move` event is emitted.

**Hub map.** `load_hub_coords()` parses `hub_map_coords_bracketed.csv` — each cell is `[tileset_col, tileset_row]` referencing `tiles_town.png`. `load_hub_grid()` maps those coords to tile names via `tiles_town_rules.txt` for passability checks. `render_hub_map()` (in `web/server.py`) blits all tiles from `tiles_town.png` into a single 256×224 PNG cached at `web/static/screens/hub.png`; the URL is included in the `hub_init` SSE event so the browser loads it on start.

**Web rendering.** `hub_init` carries `rows`, `cols`, `party` (list of `{name, row, col}`), and `screen_url`. `hub_move` carries `name`, `row`, `col`, `tick`. The JS renderer handles both: `handleHubInit` sets mode to `"hub"`, resizes the canvas, loads the background PNG, and `drawHubSprites()` draws all four members at their positions using the correct sprite sheet row per name (MELVIN=0, BILLY=1, SMELTRUD=2, POOTS=3). Each `hub_move` updates the member's position and redraws. Late-joiner snapshot tracks current member positions via `_update_snapshot` handling `hub_move` diffs.

### Interior scenes (`engine/scenes/interior.py`, `overworld_loop.py`)

`Interior` is a generic scene class used for both cave/dungeon and town interiors.
Constructed from a deserialized interior data dict (loaded from `worlddb`) and a
`monster_spawn: bool` flag (True for caves/dungeons, False for towns).

Key properties:
- `spawn` — (row, col) where the party appears on entry
- `entry_tile` — (row, col) of the exit-trigger tile; stepping on it returns the party to the overworld standing directly on the entrance tile
- `combined_grid()` — merges floor + wall (cave) or ground + overlay (town) into a single `{(r,c): tile_name}` dict
- `is_exit(row, col)` — True when the party is on the exit-trigger tile

**Interior navigation** (`_run_interior_loop` in `overworld_loop.py`). When the party steps onto an enterable tile the interior is generated (or loaded from cache) and the party actually navigates it — no immediate stub exit. The loop is deterministic (no LLM calls):

1. `_interior_find_far_tile` BFS-floods from spawn to find the most distant reachable tile, capped at 50 steps so the visit stays under ~20 seconds.
2. `_interior_bfs` finds the shortest path spawn → far point, then far point → exit tile.
3. The party walks that path; `interior_move` SSE events fire at each step (0.22 s/step).

`interior_init` carries `rows`, `cols`, `row`, `col`, `screen_url`, and `monster_spawn`. `interior_move` carries `row`, `col`. `interior_exit` signals the return to the overworld, followed immediately by a `move` event re-anchoring the party on the overworld entrance tile.

**Interior PNG rendering** (`web/server.py`). `render_interior_map(world_seed, feature_id, tag, data)` renders a cave or town interior PNG on first entry (applying the per-run NES palette via the same `remap_tileset` pipeline the generators use) and caches it to `web/static/screens/interior_{world_seed}_{feature_id}.png`. Separate thread-safe raw tile caches for cave and town tilesets.

**Canvas scrolling** (`web/static/app.js`). A new `"interior"` canvas mode uses a fixed 16×14-tile viewport regardless of the interior's actual dimensions. `updateInteriorCamera()` keeps the party centred; `drawInteriorMap()` draws a camera-offset slice of the full interior PNG using `drawImage` source-rect clipping. The party sprite is drawn at its viewport-relative tile position.

### Goal-driven navigation — three-tier loop (`overworld_loop.py`, `engine/pathfinding.py`, `engine/goal.py`)

The overworld loop is organised into three tiers so LLM calls happen only at decision points, not on every movement step.

**Tier 1 — Goal-setting.** When the party has no active goal, the current leader calls the LLM once to pick a target screen (`goal_type: explore | travel`, `target_sx/sy`). The result is a `Goal` object that persists until it completes, is abandoned, or is replaced. The leader rotates through all alive members via `leader_idx` (tracked across the whole session) so every character gets turns proposing goals and making checkpoint decisions — BILLY no longer dominates.

**Tier 2 — Silent execution.** `screen_direction_toward(sx, sy, target_sx, target_sy, exits)` picks the next screen exit using a greedy axis-preference rule (larger delta wins; tie broken by N/W). `bfs_to_exit(grid, row, col, direction)` finds the shortest intra-screen path to the target exit edge — no LLM call. `path_to_segments` compresses the BFS path into a minimal list of `(direction, steps)` pairs for `execute_move`. The party crosses screens automatically; the loop refreshes the grid from `MoveResult.final_grid` after each move.

`_find_connected_start(grid, rows, cols)` seeds a BFS from every walkable exit-edge tile inward to find the exit-connected component, then returns the tile in that component closest to screen center. Used for initial party placement and to recover from post-crossing disconnected-pocket landings (a single reconnect attempt before triggering `path_blocked`).

**Tier 3 — Checkpoint discussion.** Four trigger points pause execution and call the LLM:

| Trigger | When | Terminal? |
|---|---|---|
| `goal_reached` | Party is already on the target screen | Yes — goal marked complete before discussion |
| `path_blocked` | BFS finds no path to exit after reconnect attempt | Yes — goal abandoned |
| `all_exits_blocked` | `screen_direction_toward` returns None (all exits closed) | Yes — goal abandoned |
| `branch_point` | Both axes equal distance to goal (`|dx|==|dy|`) AND both exits open | No — goal stays active unless outcome is abandon/modify |

The LLM returns `continue | abandon | modify`. `modify` produces a new `Goal` immediately (skipping Tier 1). `branch_point` fires at most once per `(screen, target)` pair; `branch_points_seen` prevents re-triggering the same position.

**Interior entry.** When the party steps onto an enterable tile (`stop_reason='enterable'`), `_enter_interior(pos, db)` fires before the goal is abandoned. It looks up the feature, computes the feature_id, dispatches to `generate_cave_data` (dungeons) or `generate_town_data` (towns) based on `FEATURE_TYPES`, calls `get_or_create_interior` (generating and caching on first visit), constructs an `Interior` scene, and emits `interior_init`/`interior_exit` SSE events. The party lands on the entrance tile on return. `_enter_interior` returns early (no-op) if the feature type is not in `FEATURE_TYPES` — guards against hub tiles and any other enterable types that don't have interior generators.

**Feature detour.** Because `bfs_to_exit` explicitly avoids enterable tiles (they're treated as walls so the party can cross screens without stopping mid-path), the party would never organically step onto caves or towns. The detour block runs before each within-screen BFS: it queries `db.list_screen_features` for unvisited enterable features on the current screen, routes to the nearest one via `bfs_to_tile` (which allows enterable tiles as traversable), and calls `_enter_interior` on arrival. Only features whose `feature_type` is in `FEATURE_TYPES` are candidates — the `hub` tile on screen (0,0) and any unknowns are excluded. The feature is marked `entered=True` in the DB by `execute_move` on landing, so the same cave is never detouring to twice.

### Navigation log (`engine/navlog.py`)

Append-only, unbounded in-memory log of overworld events. The curated context builder reads from it; nothing ever trims it.

Notable event types: `SCREEN` (screen crossing), `GOAL` (new goal set), `CHECKPOINT` (discussion fired), `ENTERED` (arrived at enterable feature). `MOVE` events are excluded from "notable" — too numerous to surface to the LLM. `NavLog.last_notable(n)` returns the last *n* notable entries, oldest first.

### Curated context (`engine/context.py`)

`build_curated_context(active_goal, navlog, db, world_seed, party, pos_sx, pos_sy)` assembles a `CuratedContext` dataclass from live world state. This is the **only** feed into goal-setting and checkpoint prompts — no raw journal dumps, no wholesale DB queries in prompt builders.

Contains: current screen position, active goal, last 8 notable NavLog entries (formatted strings), known enterable POIs with visited flag, party HP snapshot, visited-screen count and a 12-item sample. `llm/prompts.py` renders `build_goal_context(member, ctx)` and `build_checkpoint_context(member, ctx, reason)` directly from this object.

### Per-member journal (`engine/journal.py`)

Two independent layers:

- **`Journal`** — global milestone log. Structured events in; ALL-CAPS retro
  narrative out (`PARTY DEFEATED LVL 2 GOBLIN.`). Unbounded; for display/recap.
- **`MemberJournal`** — per-member FIFO rolling window (default 12 entries,
  ~120 tokens). Each entry: `(tick, event_type, terse_desc)`. Events: MOVE,
  ENTERED, PROPOSE, VOTE, RESOLVED, SCREEN. SCREEN fires on every crossing
  (`"crossed N → screen (0,-1)"`). Injected into each member's overworld prompt
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

Three generators in `procgen/`, all output PNGs to `procgen/out/` when run as harnesses.

Both overworld and cave generators expose a data/render split: `generate_*_data(seed)` → dataclass (no PIL), `render_*_data(data, raw_tiles)` → PIL image. Town follows the same pattern. The data layer is what `engine/worlddb.py` consumes via `get_or_create_interior`.

**CRITICAL:** `llm/ascii_map.py`'s `render_map_overlay` is for human debug output only (used in `procgen/viewscan_test.py` and `procgen/voting_test.py`). It must **never** be fed into any LLM prompt context.

**Overworld** (`procgen/worldgen.py`) — infinite tiled world, one screen per coordinate pair:
- Base grass fill → blob placement (lakes with corner cuts, forest blobs, mountain rows, mnt blobs) → dirt patches → feature placement (towns, caves, castles, etc.) → jittered A\* path network → scatter (trees, ponds, individual mountains)
- Per-screen NES palette: 3 non-adjacent palette cells swapped into placeholder green/blue/brown
- Stable deterministic seed per (world\_seed, sx, sy) so any screen reproduces exactly
- `FEATURE_TYPES` dict maps tile names to `"dungeon"` or `"town"` tags — used by the overworld loop to dispatch the right interior generator

**Town** (`procgen/towngen.py`) — interior town maps:
- `generate_town_data(seed) → TownData` (no PIL) — canonical API
- Canvas sized to building count (min 16×14); cropped to actual building bounding box + 5-tile ground margin
- Healer hut (always present, fixed 3×2) seeds a cluster box; 1–9 additional buildings (houseA, houseB, stone) placed 92% within that cluster, 8% outlier
- Ground blobs: grass fill, gravel blobs (set-based neighbor derivation to fix overlap artifacts), dirt courtyards/paths
- Cobble paths via MST on building south-edges: ~50% of edges kept (min 1), one path trails to nearest crop-box boundary
- Vegetation clusters: density-falloff rings (radius 2–4) of bush/cactus/tall-bush tiles, never blocking building doors
- Scatter decorations: stumps, chairs, tyres, containers
- Per-run NES palette: 8 placeholder colours swapped to 8 non-adjacent palette cells; black and white preserved
- Entry tile at bottom center of crop_box; spawn 1 tile north

**Cave / dungeon** (`procgen/cavegen.py`) — interior maps for cave entrances placed on the overworld:
- `generate_cave_data(seed) → CaveData` (no PIL) — canonical API
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
│   ├── navlog.py           # append-only navigation event log (SCREEN/GOAL/CHECKPOINT/ENTERED)
│   ├── context.py          # curated context builder → CuratedContext fed to goal/checkpoint prompts
│   ├── goal.py             # Goal dataclass (goal_type, target_sx/sy, status lifecycle)
│   ├── pathfinding.py      # bfs_to_exit, path_to_segments, screen_direction_toward
│   ├── memory.py           # builds the context blob for each LLM call
│   ├── tiles.py            # tile passability/quality lookup (parses tilerules files)
│   ├── viewscan.py         # line-of-sight tile scan (N/S/E/W rays → ViewScan dataclass)
│   ├── worlddb.py          # persistent world-state DB (SQLite; screens + features + interiors)
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
- Activate `.venv/` before running: `source .venv/bin/activate` — all deps (Pillow etc.) live there.

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
8a. [x] Town interior generator — healer hut, buildings (houseA/B/stone), gravel/dirt/cobble ground, vegetation clusters, MST cobble paths, scatter; 8-colour NES palette
9. [x] Procgen/engine bridge — data/render split in both generators; `engine/tiles.py` (passability/quality); `engine/worlddb.py` (SQLite world state, replay-guaranteed)
10. [x] Viewscan — `engine/viewscan.py`: line-of-sight tile scan (N/S/E/W rays, terminates at enterable/blocker/edge); `procgen/preview_test.py`: party sprite preview harness
11. [x] Voting state machine — `engine/voting.py`: proposal/vote SM, early-lock resolution, threshold logic; overworld LLM prompts + `parse_overworld_action`; `ask_with_retry` + `LLMDecision`; `engine/party_state.py`: `execute_move`; `procgen/voting_test.py`: CLI harness with real LLM + movement
12. [x] Per-member short-term journal — `engine/journal.py`: `MemberJournal` FIFO window (12 entries), `journals_append` broadcast helper; `llm/prompts.py`: `RECENT EVENTS` section injected into overworld context; engine wiring in `execute_move`; validated with journal rollover in `voting_test.py`
13. [x] Multi-screen crossing — `execute_move` seamlessly crosses screen edges when a generator is supplied: updates `pos.sx/sy`, calls `enter_screen()` for the adjacent screen, places party at mirrored entry tile, continues remaining steps on new screen; `MoveResult.final_grid` carries the new grid back to callers; SCREEN journal event per crossing; `llm/prompts.py` surfaces open exits as `"crossing available"` in the direction summary; `voting_test.py`: direct crossing assertion test (no LLM) + LLM rounds that naturally span screen boundaries
14. [x] Wire overworld loop into production runners — `overworld_loop.py` extracts the propose→vote→move→journal cycle from the test harness (all test scaffolding removed; starts at walkable center; runs until interrupted); `run_cli.py` defaults to overworld, `battle` arg runs the battle loop; `run_web.py` wired headlessly (SSE layer deferred); confirmed end-to-end: real LLM decisions, 2+ screen crossings through the runner, journal populating, graceful-exit summary
15. [x] SSE web viewer (Part 1) — `web/server.py`: Flask SSE endpoint with per-client queue fan-out, late-joiner snapshot, heartbeat; screen PNGs rendered server-side via existing Pillow pipeline (per-screen palette applied, cached to `web/static/screens/`); `web/static/app.js` + canvas: two-layer canvas (map + sprite overlay), `billyS1` placeholder sprite, `updateSpriteFrame()` no-op stub; `run_web.py` runs loop in daemon thread alongside Flask; proposer rotation fix (round-robin by index so each member leads in turn); confirmed: `init`→`vote`/`resolve`→tile-by-tile `move`→`screen` on crossing, both screen PNGs visually correct
16. [x] Goal-driven navigation (JOB 11a) — replaces the propose→vote loop with a three-tier system that eliminates oscillation: `engine/goal.py` (`Goal` dataclass, status lifecycle); `engine/pathfinding.py` (`bfs_to_exit`, `path_to_segments`, `screen_direction_toward`); `overworld_loop.py` rewritten — Tier 1 LLM goal-setting, Tier 2 silent BFS execution, Tier 3 checkpoint stubs; `_find_connected_start` seeds BFS from exit-edge tiles inward so the party always starts exit-connected (fixes disconnected-pocket livelock on procgen maps); post-crossing reconnect step recovers from disconnected landing tiles; `procgen/movement_test.py`: BFS + crossing integration tests (no LLM)
17. [x] Curated context + checkpoint discussion (JOB 11b) — `engine/navlog.py`: append-only unbounded event log (SCREEN/GOAL/CHECKPOINT/ENTERED; MOVE excluded); `engine/context.py`: `build_curated_context()` assembles `CuratedContext` from NavLog + DB + party state — the sole feed for all goal/checkpoint prompts; `llm/prompts.py`: `build_goal_context(member, ctx)` and `build_checkpoint_context(member, ctx, reason)` updated to consume `CuratedContext`; `llm/schema.py`: `parse_checkpoint_decision` (continue/abandon/modify with goal fields for modify); four checkpoint triggers in the main loop with `continue|abandon|modify` outcomes; branch-point trigger fires only on axis tie (`|dx|==|dy|`) — genuine ambiguity, not every diagonal step; `branch_points_seen` dedup prevents re-triggering per `(screen, target)` pair
18. [x] Wire overworld to interior generation — `procgen/worldgen.py` + `procgen/cavegen.py` (renamed from `overworld_test.py`/`cave_test.py`); `procgen/towngen.py`: added `TownData` dataclass + `generate_town_data(seed)` (no PIL); `engine/worlddb.py`: `interiors` table, `compute_feature_id()`, `get_or_create_interior()` — generate-once/cache-forever matching screens pattern; `engine/scenes/interior.py`: `Interior` scene class (cave or town, `monster_spawn` flag, `entry_tile` exit, `combined_grid()`); `overworld_loop.py`: `_enter_interior()` fires on every `stop='enterable'`, dispatches by `FEATURE_TYPES`; `procgen/worlddb_test.py`: interior replay guarantee test added
19. [x] Interior navigation (basic) — deterministic BFS interior loop: party enters, walks to farthest reachable tile (max 50 steps), returns to exit; `interior_init`/`interior_move`/`interior_exit` SSE events; interior PNG rendered via `render_interior_map()` (cave + town tilesets, NES palette applied, cached); web canvas uses a 16×14 viewport with camera tracking (`updateInteriorCamera`, `drawInteriorMap`) so large interiors scroll correctly; feature detour block routes party to unvisited caves/towns via `bfs_to_tile` before each screen-crossing BFS (since `bfs_to_exit` avoids enterables by design); `hub` tile and unknown feature types excluded from detour via `FEATURE_TYPES` guard; eliminates the hallucination problem where party narrated entering places that were immediately stubbed out. Monster encounters and NPC interaction are future jobs.
20. [x] Hub scene — `engine/scenes/hub.py`: 4-member independent roam on hand-authored town map (called the **Front House**); `MIN_HUB_TICKS = 12` dwell guard prevents immediate leave-vote on startup; spawn 4 rows up from the bottom edge; edge detection triggers `_run_leave_vote()` (VotingState, 3-of-4 threshold, VOTE-only action set, nudge-on-fail with corrective `hub_move` emit); `run_hub()` returns `"overworld"` on pass; `render_hub_map()` blits `tiles_town.png` tiles into `screens/hub.png`; JS handles `hub_init`/`hub_move` with 4-sprite per-character drawing; game now starts on hub in both `run_web.py` and `run_cli.py` (default: hub→overworld; `overworld` arg skips hub)
20a. [x] Leader rotation — `leader_idx` tracked across the session in `run_overworld`; rotates through alive members for goal-setting and all checkpoint types so every character gets a turn making decisions (BILLY no longer always leads)
20b. [ ] Hub pet-sim mode — global chat, ambient reactions to events, NPC interaction
21. [ ] Long-term memory — compressed journal summary (templating, not a GM call)
22. [ ] (later) player inputs
