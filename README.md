# Front House Gaiden

An 8-bit RPG with two lives: a **shippable single-player game** (itch.io) and a **watchable 24/7 stream** where an LLM party plays autonomously. Both run on the same engine. The single-player game is the primary build target going forward; the AI mode is a companion project built on top of it.

**Current state:** Engine is solid — movement, combat, procgen, tile rendering, enemy AI, battle loop all work. The player-control layer (keyboard/controller input, menus, WebSocket game server) is next.

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Input & Core Game Loop
- [x] WebSocket game server (`game/server.py`) — transport only; all game logic in engine
- [x] Keyboard input: arrow keys (move), Z (B button), X (X button), Enter (start/confirm)
- [x] Player character entity (`engine/player.py`) — `Player` with `PlayerState` machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE), `try_move()`, `adjacent_interactable()`, serialise/deserialise; wired to config and used directly by the WebSocket server
- [x] Config-driven movement speed — `player_move_ms` in `config.json`; stamped into `hub_init` event so client timing matches server config with no JS constants
- [ ] Bluetooth / USB controller support (browser Gamepad API)
- [ ] Title screen
- [ ] In-game menu system (pause, save, settings)
- [ ] Save/load through menu

### Debug & Testing Screens
- [x] `maptest.py hub` — loads hub via engine directly (`engine/player.py`, `engine/scenes/hub.py`); player-controlled Melvin, passability collision, NPC animation; `game/server.py` is transport only, no duplicate game logic
- [ ] Debug screen loader — pick any map file by name, load it into the running server without restart; works for hub, procgen cave/town, and hand-authored YAML maps
- [ ] Debug overlay — toggle tile passability grid, NPC index/behavior labels, player tile coords, FPS counter
- [ ] Cave and town debug screens — same pattern as hub; spawn player in any interior

### Content Authoring Tools
- [x] Map file format — YAML (`data/maps/`); tileset, palette, NPC placements, warp points, spawn; engine reads directly via `engine/map_loader.py`; DB stores only mutable runtime state (flags, opened chests), never the tile layout
- [x] Warp/exit system designed — runtime warp stack handles nested interiors at arbitrary depth; no YAML needs to know where it was entered from; `"__return__"` target pops back to caller tile (see `engine/map_loader.py` docstring and `data/maps/hub.yaml` comments)
- [x] `data/maps/hub.yaml` — annotated reference map; all fields documented inline
- [ ] Tiler / map creator — in-browser tool: paint tiles, place/move NPCs and containers, set warp destinations, export to YAML
- [ ] NPC definition files (`data/npcs/`) — sprite, behavior, dialogue tree; referenced from map YAML by ID
- [ ] Item YAML (`data/items/`) — name, description, icon, effects, stack rules
- [ ] Special abilities YAML (`data/abilities/`) — name, MP cost, effect, cooldown, flavor
- [ ] Tileset tooling — import tilesets, tag tiles visually (passable/impassable/enterable), export tilerules

### World & Content
- [ ] Rethink procgen scope — decide what stays procedural vs. hand-authored for the single-player game (story, scripted towns/dungeons, procgen as supplement not spine)
- [ ] Updated and expanded tilesets
- [ ] Inventory + equipment system (items in world, pickup, equip slots, stat effects)
- [ ] NPC dialogue system (face-to-face trigger, branching, per-NPC state)
- [ ] Named locations — towns, dungeons, overworld landmarks

### Visuals & UI
- [ ] Battle screen overhaul — full tile + text renderer using custom bitmap font, replace DOM overlay
- [ ] Tile-swipe battle transition
- [ ] Party status panel (HP, MP, level, XP per member)
- [ ] Battle-speed config (separate from movement speed)

### Online / AI Mode
- [ ] WebSocket 4-player online couch-coop (through website, optional future target)
- [ ] AI mode fully separated and documented (see [LLM Mode](#llm-mode--ai-plays) below)

---

## What's Built

### Engine

The engine is headless and deterministic. No display logic lives here.

**Movement (`engine/party_state.py`)** — `execute_move(pos, direction, steps, grid, db)` steps the party one tile at a time. Stops on blockers, screen edges, or arrival at an enterable feature. When a generator is supplied and a step would exit the screen, it crosses seamlessly — updates screen coords, generates/caches the adjacent screen, places the party at the mirrored entry tile, and continues.

**Pathfinding (`engine/pathfinding.py`)** — `bfs_to_exit()` finds the shortest intra-screen path to an exit edge. `bfs_to_tile()` routes to a specific tile (allows enterables). `path_to_segments()` compresses a BFS path into `(direction, steps)` pairs. `screen_direction_toward()` picks the next screen exit by greedy axis preference.

**Tile rules (`engine/tiles.py`)** — `is_passable()`, `is_enterable()`, `tile_quality()`. Handles `:rot` rotation suffixes. Parses both tilerules files once at startup.

**Config (`engine/config.py`)** — `config.json` singleton. Pacing (move_ms, screen crossing, interior entry/exit), battle limits, display geometry, interior exploration distance. LLM provider stays in `secrets.py`.

**World database (`engine/worlddb.py`)** — SQLite. Four tables: screens (grid cached on first visit, exits pre-computed), features (interactable state: entered/cleared/npc_flags), interiors (cached on first entry by feature_id), enemies (stats, sprite, behavior, palette per scope). `get_or_create_screen()` / `get_or_create_interior()` generate once and cache — all subsequent calls are read-only. Replay guarantee: same seed → identical world.

**Session persistence** — `session` table saves scene, world seed, position, tick, leader index, per-member HP/MP/XP/level, active goal, navlog, and per-member journals. Resumes on restart. `hub` arg forces fresh start.

### Battle

**Combat resolver (`engine/combat.py`)** — Percentage-based hit chance, crit, parry, damage. Stat fractions (IQ, weight, sweat, hair) feed float probability knobs. Enemy HP scales with party level at generation time (locked forever after — generate-once guarantee).

**Battle loop (`engine/battle.py`)** — `run_battle(party, enemy, rng, emit=None)` returns `{"outcome": "win"|"loss"|"flee"|"timeout", "rounds": N}`. Turn order: all four party members, then the enemy. Ends on enemy defeat, party wipe, or 100-round timeout. In AI mode, each member's action is LLM-chosen; the player-controlled path will present a menu.

**Party specials:**

| Member | Special | Effect | MP cost |
|---|---|---|---|
| BILLY | SING | MESMERIZE — 50% skip chance, escalating break probability | 8 |
| MELVIN | LAUGH | CRINGE — 35% enemy self-attack per turn, 3–5 turns | 7 |
| POOTS | SNACK | Heals a party member 15–25% max HP | 6 |
| SMELTRUD | TICKLE | +15% ally damage for 2 turns | 5 |

Status effects don't stack. MP restored only by the healer. XP awarded on win; level-up bumps max HP/MP.

**Party wipe** — on battle loss, all members revive at half HP/MP and return to the hub. Active goal clears. Game continues.

### Enemy System

**Generation (`procgen/enemygen.py`)** — Deterministic per seed: name, combat stats, NPC sprite + NES palette recolor, behavior type. Seed derived via SHA-256 from terrain seed — enemies survive screen regen. Scopes: `screen` (0–3 per overworld screen) and `cave_screen` (6-entry pool per screen, shared across all caves on that screen; each room rolls 0–3).

**Behavior types:** 1 — Wanderer/chaser (65% toward party, 35% random); 2 — Pacer (walks axis, reverses at obstacles); 3 — Sentinel (frozen until 8-tile cardinal LOS, then chases).

**Placement & movement (`engine/enemy_state.py`)** — `place_enemies()` places deterministically on walkable tiles. `update_enemies()` advances all enemies one tile per party step. Collision triggers battle.

### Scenes

**Hub — Front House (`engine/scenes/hub.py`)** — Hand-authored 14×16 town map. Starting area. In AI mode, four members roam independently. Leave vote triggers when a member hits an edge (3-of-4 threshold).

**Interior scenes (`engine/scenes/interior.py`)** — Generic class for cave/dungeon and town interiors. Key properties: `spawn`, `entry_tile`, `combined_grid()`, `healer_spawn` (towns), `is_exit()`. Healer restores full HP/MP to living members.

**Town NPCs (`procgen/npcgen.py`)** — 3–8 NPCs per town, generated once, cached. Index 0 is always the healer (fixed position, behavior type 3). Remainder: wanderer/pacer/static, placed on walkable tiles. `update_town_npcs()` animates them; no party-chasing, no combat.

### Procedural Generation

All three generators have a data/render split: `generate_*_data(seed)` returns a pure dataclass (no PIL), `render_*_data(data, raw_tiles)` returns a PIL image. `worlddb.py` consumes only the data layer.

**Overworld (`procgen/worldgen.py`)** — Infinite tiled world (one screen per coordinate pair). Base grass → blob placement (lakes, forests, mountains) → dirt patches → feature placement (towns, caves, castles) → jittered A* paths → scatter. Per-screen NES palette. Stable deterministic seed per `(world_seed, sx, sy)`.

**Town (`procgen/towngen.py`)** — Canvas sized to building count, cropped to bounding box + margin. Healer hut always present (fixed 3×2). 1–9 additional buildings, 92% in-cluster / 8% outlier. Ground blobs, cobble MST paths, vegetation, scatter. 8-colour NES palette.

**Cave/dungeon (`procgen/cavegen.py`)** — Up to 8 rooms, two flavours (cave: cobble/cave walls; dungeon: brick/dungfloor). Rooms connected by L-shaped/zig-zag hallways. Water pools, waterfalls, scatter. 3-colour NES palette.

### Game Client (`game/`)

WebSocket server + minimal browser client for player-controlled debug and testing. This is a **temporary bridge** — the plan is to replace the browser canvas with a pygame renderer that runs locally.

- `game/server.py` — transport only; receives key events from the browser, calls engine, sends events back. No game logic.
- `game/static/` — throwaway HTML/JS canvas renderer for browser testing; will be replaced by pygame.

**Running the player-controlled game:**
```
source .venv/bin/activate
python maptest.py       # → pick "hub"; opens player-controlled Melvin at localhost:8765
```

### Asset Layout

Binary game assets live under `assets/` — shared by both the pygame renderer (planned) and the browser debug frontend. The web layer does not own these files.

```
assets/
├── fonts/    — ModernDOS8x8.ttf
├── sprites/  — party_sprites.png + layout text
└── tiles/    — tileset PNGs, tilerules TXT files, hub map CSV
```

`web/static/` holds only web-specific generated JSON (tilemap indices) and the AI-mode SSE viewer. It is shelved pending pygame.

### SSE Web Viewer (AI mode — shelved)

Canvas-based tile renderer in `web/static/app.js`. Draws scenes from a live tile-ID grid. Viewport: 16×14 tiles at 16px × 3× scale (48px drawn). Sprite movement interpolated over `move_ms` with a 2-frame walk cycle. Sprites: party sheet (4 members × 8 directions × 2 frames), NPC/enemy sheet, per-enemy recolored palette strips.

One-directional: engine → browser via Server-Sent Events. No game logic in JS. Not the primary build target; kept for the AI-mode stream.

```
source .venv/bin/activate
python run_web.py       # AI mode: SSE server + autonomous party loop; localhost:5000
python run_cli.py       # AI mode: text output only, no browser
```

### Viewscan (`engine/viewscan.py`)

Headless line-of-sight scan in four cardinal directions from the party tile. Each ray terminates at the first enterable, impassable, or screen-edge tile. Returns a `ViewScan` dataclass with one `DirectionScan` per direction. Used for LLM context and can feed future player minimap/fog-of-war logic. No display, no LLM, no DB.

### RNG

One global seeded RNG, seed logged at run start. All randomness is engine-side. Same seed → same world, same enemy placement, same combat outcomes. Replays are essentially free.

---

## Directory Structure

```
simtank_rpg/
├── engine/
│   ├── player.py           # Player entity; PlayerState machine; try_move, serialise
│   ├── map_loader.py       # YAML map loader → MapData, WarpPoint, NpcPlacement
│   ├── battle.py           # battle loop; run_battle() — emit= hooks for renderer
│   ├── combat.py           # stat math, hit/crit/parry/damage resolution
│   ├── config.py           # config singleton (config.json)
│   ├── context.py          # curated context builder for LLM prompts
│   ├── enemy_state.py      # EnemyAgent; place_enemies(); update_enemies()
│   ├── goal.py             # Goal dataclass (LLM mode)
│   ├── journal.py          # Journal (global) + MemberJournal (per-member LLM window)
│   ├── navlog.py           # append-only navigation event log (LLM mode)
│   ├── party_state.py      # PartyPos; execute_move(); enter_screen()
│   ├── pathfinding.py      # bfs_to_exit, bfs_to_tile, path_to_segments
│   ├── tiles.py            # passability/enterable/quality lookup
│   ├── viewscan.py         # line-of-sight scan → ViewScan dataclass
│   ├── voting.py           # proposal/vote state machine (LLM mode hub)
│   ├── worlddb.py          # SQLite world/session persistence
│   └── scenes/
│       ├── hub.py          # Front House scene
│       └── interior.py     # cave + town interior scene class
├── game/                   # WebSocket server + browser debug client (temporary)
│   ├── server.py           # transport only — keys in, events out; no game logic
│   └── static/             # throwaway browser canvas; will be replaced by pygame
├── llm/                    # LLM mode only — not used by single-player path
│   ├── client.py           # provider-agnostic LLM call; ask_with_retry
│   ├── schema.py           # response parsers; action validation
│   ├── prompts.py          # all prompt builders
│   └── ascii_map.py        # ASCII debug renderer (never in prompts)
├── procgen/
│   ├── enemygen.py         # enemy generation (stats, sprite, behavior, NES palette)
│   ├── npcgen.py           # town NPC generation
│   ├── worldgen.py         # overworld screen generator
│   ├── cavegen.py          # cave/dungeon interior generator
│   └── towngen.py          # town interior generator
├── assets/                 # binary game assets — shared across all renderers
│   ├── fonts/              # ModernDOS8x8.ttf
│   ├── sprites/            # party_sprites.png + layout text
│   └── tiles/              # tileset PNGs, tilerules TXT, hub map CSV
├── data/
│   ├── maps/               # YAML map files (hub.yaml + future authored maps)
│   └── party/              # character sheet JSONs
├── web/                    # AI-mode SSE viewer (shelved — not primary build target)
│   ├── server.py           # SSE server + screen/sprite render pipeline
│   ├── gen_tilemaps.py     # generate tilemap JSONs from tilerules (run once)
│   └── static/             # app.js SSE renderer, generated tilemap JSONs
├── overworld_loop.py       # AI mode: goal-driven navigation loop (LLM)
├── run_cli.py              # AI mode runner (text output)
├── run_web.py              # AI mode runner (SSE web server)
├── maptest.py              # debug entry point — hub/cave/town screens, player-controlled
├── secrets.py              # API keys (gitignored)
└── config.json             # pacing, display geometry, tunable params
```

---

## Dev Setup

```bash
source .venv/bin/activate

# Single-player debug (primary)
python maptest.py           # pick hub → player-controlled Melvin; localhost:8765

# AI mode (shelved — needs API keys in secrets.py)
python run_cli.py           # text output, no browser
python run_web.py           # SSE web viewer; localhost:5000
python run_cli.py hub       # force fresh start at Front House
```

All dependencies (including Pillow and flask-sock) are in `.venv/`. `secrets.py` holds API keys and is gitignored — it shadows Python's stdlib `secrets` module intentionally.

---

## LLM Mode — "AI Plays"

The 24/7 watchable stream. Four AI party members run autonomously; no player input. The engine is identical to the single-player game — the LLM is just the input layer.

### Core principle

The LLM is a **thin decision layer**. Given a constrained situation and an enumerated list of valid actions, it picks one and optionally emits flavor text. It does **not** roll dice, resolve combat, track inventory, or manage memory. All of that is engine code.

Token budget: ~600–800 tokens in, small out.

### LLM providers

Priority order: Ollama (local, no rate limit, primary) → Mistral API → AI Horde. All go through the provider-agnostic client in `llm/client.py`. `ask_with_retry` validates output, reprompts once on bad output, falls back to a safe default — never crashes.

### Three-tier overworld loop (`overworld_loop.py`)

**Tier 1 — Goal-setting.** When the party has no active goal, the current leader calls the LLM to pick a target screen (`goal_type: explore | travel`, `target_sx/sy`). Leader rotates through all alive members so every character gets turns.

**Tier 2 — Silent execution.** BFS-guided movement toward the goal screen. No LLM calls on individual steps.

**Tier 3 — Checkpoints.** Four trigger points pause and call the LLM: `goal_reached`, `path_blocked`, `all_exits_blocked`, `branch_point`. Returns `continue | abandon | modify`.

### Context strategy

Tiered, rebuilt every call:
1. Character sheet (system prompt, re-injected fresh — no chat history)
2. Situation (code-rendered: location, scene, valid actions, HP)
3. Short-term memory — `MemberJournal`: FIFO rolling window, last 12 events per member (~120 tokens), injected as `RECENT EVENTS`
4. Long-term memory — compression planned, not yet built

### Interior navigation (LLM-driven)

Inside caves and towns, the leader picks from a menu of POIs (healer, explore spots, exit) via LLM call. BFS executes each path. On arrival the LLM picks again until it chooses exit.

### Voting state machine (`engine/voting.py`)

Group decisions (hub leave vote) run through proposal → vote. Any alive member can propose; each subsequent member votes YES/NO. Resolves as soon as outcome is mathematically locked. Thresholds: 4 alive → 3 yes; 3 → 2; 2 → unanimous; 1 → auto-pass. Used in hub scene for the leave-to-overworld decision.

### SSE web viewer

One-directional: engine → browser via Server-Sent Events. The canvas renderer in `app.js` handles all display; no game logic in JS. `run_web.py` starts both the game loop and the SSE server. The WebSocket upgrade (roadmap item) will keep the same renderer but add the return channel for player input.

### Determinism / replay

One global seeded RNG. Same seed → same world, same enemy placement, same combat outcomes. Replay is nearly free: re-run the seed and re-emit the event log.
