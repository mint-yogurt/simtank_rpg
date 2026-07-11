# Front House Gaiden

An 8-bit RPG, developed entirely locally through a pygame renderer. Single-player.

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Input & Core Game Loop
- [x] Keyboard input: arrow keys move the player, held-key resolution ("last pressed wins", never diagonal) lives in `engine/input.py`'s `HeldDirectionInput`
- [ ] Z (B button), X (X button), Enter (start/confirm) — not wired yet; there's no dialogue or menu system for them to drive
- [x] Player character entity (`engine/player.py`) — `Player` with `PlayerState` machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE), `try_move()`, `adjacent_interactable()`, serialise/deserialise
- [x] Config-driven movement speed — `player_move_ms` in `config.json`, read via the `engine.config.cfg` singleton
- [ ] Bluetooth / USB controller support (pygame joystick API)
- [ ] Title screen
- [ ] In-game menu system (pause, save, settings)
- [ ] Save/load through menu

### Debug & Testing Screens
- [x] `maptest.py` — loads the hub scene via the engine directly (`engine/player.py`, `engine/map_loader.py`, `engine/enemy_state.py`) and renders it with `pygame_viewer/hub.py`; player-controlled Melvin, passability collision, NPC animation
- [ ] Debug screen loader — pick any map file by name, load it into the running window without restart; works for hub, procgen cave/town, and hand-authored YAML maps
- [ ] Debug overlay — toggle tile passability grid, NPC index/behavior labels, player tile coords, FPS counter
- [ ] Cave and town debug screens — same pattern as hub; spawn player in any interior

### Content Authoring Tools
- [ ] Map file format — YAML. `engine/map_loader.py` has a working `load_map()`/`MapData` loader for it, but nothing wired it into a scene yet — the hub still loads from a hardcoded CSV (`assets/tiles/hub_map_coords_bracketed.csv`) + tilerules text file via the same module's CSV loader (`load_hub_grid`, etc). Next step: point the hub scene at `data/maps/hub.yaml` instead.
- [ ] Warp/exit system designed — runtime warp stack handles nested interiors at arbitrary depth; no YAML needs to know where it was entered from; `"__return__"` target pops back to caller tile (see `engine/map_loader.py` docstring)
- [x] `data/maps/hub.yaml` — annotated reference map; not yet consumed by any scene (see above)
- [ ] Tiler / map creator — paint tiles, place/move NPCs and containers, set warp destinations, export to YAML
- [ ] NPC definition YAML (`data/npcs/`) — sprite, behavior, dialogue tree; referenced from map YAML by ID; `ideas` file has character brainstorm
- [x] Item YAML (`data/items/items.yaml`) — healing, weapons, armour, key items; `ideas` file for brainstorming alongside
- [ ] Item YAML → engine integration — loader, inventory system, pickup, equip slots, stat effects
- [ ] Special abilities YAML (`data/abilities/`) — name, MP cost, effect, cooldown, flavor
- [ ] Tileset tooling — import tilesets, tag tiles visually (passable/impassable/enterable), export tilerules

### World & Content
- [ ] Rethink procgen scope — decide what stays procedural vs. hand-authored (story, scripted towns/dungeons, procgen as supplement not spine)
- [ ] Updated and expanded tilesets
- [ ] Inventory + equipment system (items in world, pickup, equip slots, stat effects)
- [ ] NPC dialogue system (face-to-face trigger, branching, per-NPC state)
- [ ] Named locations — towns, dungeons, overworld landmarks

### Visuals & UI
- [ ] Battle screen overhaul — full tile + text renderer using custom bitmap font
- [ ] `engine/battle.py` decoupled from the (now-deleted) `llm` package — action selection needs to take an injected callback instead of calling an LLM directly, so a player-menu battle path can be built on top of it. Currently `engine/battle.py` cannot even be imported.
- [ ] Tile-swipe battle transition
- [ ] Party status panel (HP, MP, level, XP per member)
- [ ] Battle-speed config (separate from movement speed)

---

## What's Built

### Engine

The engine is headless and deterministic. No display logic lives here.

**Player (`engine/player.py`)** — `Player` dataclass with a `PlayerState` state machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE), `try_move()` (cardinal step + passability check), `adjacent_interactable()`, `on_warp_tile()`, serialise/deserialise.

**Input (`engine/input.py`)** — `HeldDirectionInput` resolves which cardinal direction (if any) should be stepped each frame from a set of currently-held directions, "last pressed wins," so held keys never combine into a diagonal. Pure logic, no pygame dependency — the renderer owns raw key→direction mapping and feeds abstract direction strings in.

**Map loading (`engine/map_loader.py`)** — Two loaders live here. The CSV+tilerules loader (`load_hub_grid`, `hub_str_grid`, `hub_spawn_point`, etc.) is what the hub actually uses today. The YAML loader (`load_map()` → `MapData`, with `NpcPlacement` and `WarpPoint`) is a complete design for future authored maps but isn't wired into any scene yet.

**Pathfinding (`engine/pathfinding.py`)** — `bfs_to_exit()` finds the shortest intra-screen path to an exit edge. `bfs_to_tile()` routes to a specific tile (allows enterables). `path_to_segments()` compresses a BFS path into `(direction, steps)` pairs.

**Tile rules (`engine/tiles.py`)** — `is_passable()`, `is_enterable()`, `tile_quality()`. Handles `:rot` rotation suffixes. Parses tilerules files once at startup.

**Config (`engine/config.py`)** — `config.json` singleton. Pacing (move_ms, screen crossing, interior entry/exit), battle limits, display geometry, interior exploration distance.

**World database (`engine/worlddb.py`)** — SQLite. Four tables: screens (grid cached on first visit, exits pre-computed), features (interactable state: entered/cleared/npc_flags), interiors (cached on first entry by feature_id), enemies (stats, sprite, behavior, palette per scope). `get_or_create_screen()` / `get_or_create_interior()` generate once and cache — all subsequent calls are read-only. Replay guarantee: same seed → identical world. Not yet wired into the pygame hub path (hub is a static single screen today).

**Journal (`engine/journal.py`)** — `Journal`: a generic milestone log + ALL-CAPS narrative renderer (e.g. "PARTY DEFEATED LVL 2 GOBLIN"), used by `engine/battle.py`.

### Battle

**Combat resolver (`engine/combat.py`)** — Percentage-based hit chance, crit, parry, damage. Stat fractions (IQ, weight, sweat, hair) feed float probability knobs. Enemy HP scales with party level at generation time (locked forever after — generate-once guarantee).

**Battle loop (`engine/battle.py`)** — `run_battle(party, enemy, rng, emit=None)` returns `{"outcome": "win"|"loss"|"flee"|"timeout", "rounds": N}`. Turn order: all four party members, then the enemy. Ends on enemy defeat, party wipe, or 100-round timeout. **Currently broken** — still imports the deleted `llm` package for per-member action selection; needs decoupling before it can run again (see roadmap).

**Party specials:**

| Member | Special | Effect | MP cost |
|---|---|---|---|
| BILLY | SING | MESMERIZE — 50% skip chance, escalating break probability | 8 |
| MELVIN | LAUGH | CRINGE — 35% enemy self-attack per turn, 3–5 turns | 7 |
| POOTS | SNACK | Heals a party member 15–25% max HP | 6 |
| SMELTRUD | TICKLE | +15% ally damage for 2 turns | 5 |

Status effects don't stack. MP restored only by the healer. XP awarded on win; level-up bumps max HP/MP.

**Party wipe** — on battle loss, all members revive at half HP/MP and return to the hub.

### Enemy System

**Generation (`procgen/enemygen.py`)** — Deterministic per seed: name, combat stats, NPC sprite + NES palette recolor, behavior type. Seed derived via SHA-256 from terrain seed — enemies survive screen regen. Scopes: `screen` (0–3 per overworld screen) and `cave_screen` (6-entry pool per screen, shared across all caves on that screen; each room rolls 0–3).

**Behavior types:** 1 — Wanderer/chaser (65% toward party, 35% random); 2 — Pacer (walks axis, reverses at obstacles); 3 — Sentinel (frozen until 8-tile cardinal LOS, then chases).

**Placement & movement (`engine/enemy_state.py`)** — `place_enemies()` places deterministically on walkable tiles. `update_enemies()` advances all enemies one tile per party step. `place_hub_npcs()` places the hub's four fixed NPCs (healer always on the healer-hut tile). `update_town_npcs()` animates non-combat town/hub NPCs — no chasing, no combat.

### Scenes

**Hub — Front House** — Hand-authored 14×16 town map, loaded via `engine/map_loader.py`'s CSV+tilerules path and rendered by `pygame_viewer/hub.py`. Starting area.

**Interior scenes (`engine/scenes/interior.py`)** — Generic class for cave/dungeon and town interiors. Key properties: `spawn`, `entry_tile`, `combined_grid()`, `healer_spawn` (towns), `is_exit()`. Not yet wired into the pygame path — no pygame interior renderer exists yet (see roadmap: cave/town debug screens).

### Procedural Generation

All three generators have a data/render split: `generate_*_data(seed)` returns a pure dataclass (no PIL), `render_*_data(data, raw_tiles)` returns a PIL image. `worlddb.py` consumes only the data layer.

**Overworld (`procgen/worldgen.py`)** — Infinite tiled world (one screen per coordinate pair). Base grass → blob placement (lakes, forests, mountains) → dirt patches → feature placement (towns, caves, castles) → jittered A* paths → scatter. Per-screen NES palette. Stable deterministic seed per `(world_seed, sx, sy)`.

**Town (`procgen/towngen.py`)** — Canvas sized to building count, cropped to bounding box + margin. Healer hut always present (fixed 3×2). 1–9 additional buildings, 92% in-cluster / 8% outlier. Ground blobs, cobble MST paths, vegetation, scatter. 8-colour NES palette.

**Cave/dungeon (`procgen/cavegen.py`)** — Up to 8 rooms, two flavours (cave: cobble/cave walls; dungeon: brick/dungfloor). Rooms connected by L-shaped/zig-zag hallways. Water pools, waterfalls, scatter. 3-colour NES palette.

### Asset Layout

Binary game assets live under `assets/`.

```
assets/
├── fonts/    — ModernDOS8x8.ttf
├── menus/    — menu art (startmenu, menuicons)
├── sprites/  — party_sprites.png + layout text
├── tiles/    — tileset PNGs, tilerules TXT files, hub map CSV
└── titlescreen/ — titlescreen art
```

### Viewscan (`engine/viewscan.py`)

Headless line-of-sight scan in four cardinal directions from a given tile. Each ray terminates at the first enterable, impassable, or screen-edge tile. Returns a `ViewScan` dataclass with one `DirectionScan` per direction. Not currently consumed by anything in the pygame path — useful groundwork for a future minimap/fog-of-war.

### RNG

One global seeded RNG, seed logged at run start. All randomness is engine-side. Same seed → same world, same enemy placement, same combat outcomes. Replays are essentially free.

---

## Directory Structure

```
simtank_rpg/
├── engine/
│   ├── player.py           # Player entity; PlayerState machine; try_move, serialise
│   ├── input.py            # HeldDirectionInput — held-key/last-pressed-wins resolver
│   ├── map_loader.py       # CSV+tilerules loader (hub, in use) + YAML MapData loader (future maps)
│   ├── battle.py           # battle loop; run_battle() — currently broken, see roadmap
│   ├── combat.py           # stat math, hit/crit/parry/damage resolution
│   ├── config.py           # config singleton (config.json)
│   ├── enemy_state.py      # EnemyAgent; place_enemies(); place_hub_npcs(); update_enemies()
│   ├── journal.py          # Journal — generic milestone log, used by battle.py
│   ├── pathfinding.py      # bfs_to_exit, bfs_to_tile, path_to_segments
│   ├── tiles.py            # passability/enterable/quality lookup
│   ├── viewscan.py         # line-of-sight scan → ViewScan dataclass
│   ├── worlddb.py          # SQLite world/session persistence
│   └── scenes/
│       └── interior.py     # cave + town interior scene class
├── pygame_viewer/          # the renderer — sole display layer
│   ├── hub.py               # player-controlled hub window: input, tweening, drawing
│   ├── sprites.py           # party/NPC sprite sheet slicing
│   └── tileset.py           # tileset PNG slicing, rotation variants
├── procgen/
│   ├── enemygen.py         # enemy generation (stats, sprite, behavior, NES palette)
│   ├── npcgen.py           # town NPC generation
│   ├── worldgen.py         # overworld screen generator
│   ├── cavegen.py          # cave/dungeon interior generator
│   └── towngen.py          # town interior generator
├── assets/                 # binary game assets
│   ├── fonts/              # ModernDOS8x8.ttf
│   ├── menus/               # menu art
│   ├── sprites/            # party_sprites.png + layout text
│   ├── tiles/               # tileset PNGs, tilerules TXT, hub map CSV
│   └── titlescreen/         # titlescreen art
├── data/
│   ├── items/              # items.yaml (definitions) + ideas (brainstorm notes)
│   ├── maps/               # YAML map files (hub.yaml — not yet wired in, see roadmap)
│   ├── npcs/                # future NPC definition YAMLs + ideas (brainstorm notes)
│   └── party/               # character sheet JSONs
├── maptest.py               # debug entry point — hub screen, player-controlled
└── config.json               # pacing, display geometry, tunable params
```

---

## Dev Setup

```bash
source .venv/bin/activate
python maptest.py           # opens the pygame window, player-controlled hub
```

All dependencies (pygame, Pillow, PyYAML) are in `.venv/`.
