# Front House Gaiden

An 8-bit RPG, developed entirely locally through a pygame renderer. Single-player.

---

## Content Pipeline

Two tools produce all game content. The engine's job is to **interpret** what they produce — maps and data files contain references, not gameplay logic.

| Content | Authored in | Format | Consumed by |
|---|---|---|---|
| Maps (tile layers, collision, object placement) | **Tiled** | JSON export | `engine/renderer.py` |
| Items, NPCs, dialogue, event flags, abilities | Hand-edited | **YAML** | `engine/` data loaders |

We are **not** building our own tile/map editor — Tiled is the map tool, full stop. Map loading and rendering are **one consolidated file**, `engine/renderer.py` — not split across multiple modules. This was a deliberate call, twice over: don't recreate an `engine/map_loader.py`, and don't recreate a separate `pygame_viewer/` package either (that split existed briefly and was removed).

### Maps (Tiled → JSON)

A map authored in Tiled and exported to JSON contains:

- Tile layer(s) — graphics
- Collision layer(s)
- Object layer — spawn points, NPCs, triggers, warps

Object layer entries are references only, e.g.:

```
NPC     id: old_man        sprite: old_man        logic: old_man_logic
Trigger script: castle_gate
Warp    destination_map: castle_entrance   spawn: south_gate
```

The engine loads the map, walks the object layer, and instantiates the right Python object per `type`. No gameplay logic is ever embedded in the map file itself.

**Current state:** the hub (`data/maps/hub_fronthouse.json`) is the first map built this way and the only one that exists — `engine/renderer.py` reads its tile layers plus the sibling tileset property export (`assets/tiles/tiles_town.json`, matched by basename) to resolve per-tile passability from a `walkable` bool property, and renders it with a camera that follows the player, clamped to map edges. The old hand-rolled CSV+tilerules loader and the draft YAML `MapData` loader are gone entirely — there is no `engine/map_loader.py` anymore. What's still missing: the map's **object layer** (`NPC`/`Trigger`/`Warp`/spawn points) — `hub_fronthouse.json` doesn't have one yet, so the hub currently has no NPCs or warps.

### Items, NPCs, Dialogue, Events (YAML)

Everything that isn't map geometry is YAML under `data/`, keyed by ID and referenced from maps/other YAML — never embedded as inline logic.

**NPCs** — sprite, logic ID:

```yaml
old_man:
  sprite: old_man
  logic: old_man_logic
```

**Dialogue** — plain pages, keyed by ID, never inline in code:

```yaml
old_man_intro:
  pages:
    - "Welcome to the village."
    - "Stay away from the forest."
```

**NPC logic / event flags** — conditions over a flat flag dict (`{"met_king": True, "boss1_dead": True, ...}`), resolved by the Event System to a dialogue ID or action list:

```yaml
old_man_logic:
  - if: boss1_dead
    dialogue: old_man_after_boss
  - else:
    dialogue: old_man_intro
```

**Triggers** — doors, chests, signs, switches, map transitions — reference a script ID; the script (also YAML) says what happens:

```yaml
castle_gate:
  if: has_castle_key
  then:
    - open_gate
    - set_flag: gate_open
  else:
    - dialogue: gate_locked
```

An NPC interaction resolves as: `Player → NPC.interact() → Event System resolves "old_man_logic" → Dialogue System renders the returned page(s)`.

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Content Pipeline (Tiled + YAML)
- [x] Tiled JSON importer (`engine/renderer.py`) — tile layers + tileset `walkable` property → passability grid, GID-based rendering, player-centered clamped camera
- [ ] Object layer (`NPC`/`Trigger`/`Warp`/spawn points) → instantiation — `hub_fronthouse.json` has no object layer yet
- [x] Retired the CSV+tilerules hub loader and the draft YAML `MapData` loader — `engine/map_loader.py` no longer exists
- [ ] Event System — flag dict + condition evaluation (`if`/`else` chains) → dialogue ID or action list
- [ ] Dialogue System — renders paged dialogue YAML, keyed by ID
- [ ] Trigger System — executes a referenced script YAML on activation (doors, chests, switches, warps)
- [ ] NPC logic YAML (`data/npcs/`) — sprite + logic ID + dialogue tree, replacing the current placeholder `dialogue`/`ideas` notes files
- [ ] Warp/exit objects wired from the Tiled object layer once it exists

### Input & Core Game Loop
- [x] Keyboard input: arrow keys move the player, held-key resolution ("last pressed wins", never diagonal) lives in `engine/input.py`'s `HeldDirectionInput`
- [x] Z (B button), X (A button), Enter (START) — wired to the start menu: START opens/closes it, B closes it, A confirms the highlighted option (stub — see below)
- [x] Player character entity (`engine/player.py`) — `Player` with `PlayerState` machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE), `try_move()`, `adjacent_interactable()`, serialise/deserialise
- [x] Config-driven movement speed — `player_move_ms` in `config.json`, read via the `engine.config.cfg` singleton
- [ ] Bluetooth / USB controller support (pygame joystick API)
- [ ] Title screen
- [x] Start menu shell (`engine/menu.py`) — START pauses gameplay and opens a 4-option vertical cursor menu (INVENTORY/PARTY/SETTINGS/SAVE), wraps top-to-bottom, closes on B or START; drawn by `engine/renderer.py` from `assets/menus/`
- [ ] Start menu sub-screens — INVENTORY/PARTY/SETTINGS/SAVE each currently just highlight; A-confirm is stubbed with no screen behind it yet
- [ ] Save/load through menu

### Debug & Testing Screens
- [x] `maptest.py` — boots the real game loop (`engine/renderer.py`'s `run()`) with the current `OverworldScene` (also `engine/renderer.py`); not a special reduced path — same input/update/render code the real game runs, just picking which scene loads. Player-controlled Melvin, passability collision, camera
- [ ] Debug screen loader — pick any Tiled JSON map by name and swap `maptest.py` to it without restart; works for the overworld, procgen cave/town, and hand-authored maps
- [ ] Debug overlay — toggle tile passability grid, NPC index/behavior labels, player tile coords, FPS counter
- [ ] Cave and town debug screens — same `handle_event`/`update`/`draw` scene pattern as the overworld; spawn player in any interior

### Items & Abilities
- [x] Item YAML (`data/items/items.yaml`) — healing, weapons, armour, key items; `ideas` file for brainstorming alongside
- [ ] Item YAML → engine integration — loader, inventory system, pickup, equip slots, stat effects
- [ ] Special abilities YAML (`data/abilities/`) — name, MP cost, effect, cooldown, flavor

### World & Content
- [ ] Rethink procgen scope — decide what stays procedural vs. hand-authored in Tiled (story, scripted towns/dungeons, procgen as supplement not spine)
- [ ] Procgen output → Tiled-compatible JSON, so generated and hand-authored maps share one loader
- [ ] Updated and expanded tilesets
- [ ] Inventory + equipment system (items in world, pickup, equip slots, stat effects)
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

Most of `engine/` is headless and deterministic. The one exception is `engine/renderer.py` — see Scenes below.

**Player (`engine/player.py`)** — `Player` dataclass with a `PlayerState` state machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE), `try_move()` (cardinal step + passability check), `adjacent_interactable()`, serialise/deserialise.

**Input (`engine/input.py`)** — `HeldDirectionInput` resolves which cardinal direction (if any) should be stepped each frame from a set of currently-held directions, "last pressed wins," so held keys never combine into a diagonal. Also owns discrete button routing: `handle_start_button`/`handle_b_button`/`handle_a_button`/`handle_menu_direction` decide what a START/B/A press or a direction press means given the player's current state (e.g. arrow keys drive the menu cursor instead of movement while `PlayerState.IN_MENU`), and call into `engine/menu.py`'s `StartMenu` accordingly. Pure logic, no pygame dependency — the renderer owns raw key→direction/button mapping and feeds abstract strings in.

**Menu (`engine/menu.py`)** — `StartMenu`: cursor state for the pause-menu (`is_open`, `selected`, four options — INVENTORY/PARTY/SETTINGS/SAVE). Vertical list, wraps top-to-bottom. Never reads input itself, only exposes `open()`/`close()`/`move_cursor()`/`confirm()` — `engine/input.py` decides when to call them, `engine/renderer.py` decides how it looks. `confirm()` is a stub — sub-screens per option aren't built yet.

**Config (`engine/config.py`)** — `config.json` singleton. Pacing (move_ms, screen crossing, interior entry/exit), battle limits, display geometry, interior exploration distance.

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

**Placement & movement (`engine/enemy_state.py`)** — Owns all NPC/agent placement. `place_enemies()` places deterministically on walkable tiles. `update_enemies()` advances all enemies one tile per party step. `place_hub_npcs()` places the hub's four fixed NPCs (healer always on the healer-hut tile). `update_town_npcs()` animates non-combat town/hub NPCs — no chasing, no combat.

### Scenes

**`engine/renderer.py`** — THE graphical renderer, one file, full stop. It owns: the generic pygame app loop (`run(scene_factory, ...)` — window, clock, event dispatch; constructs the scene *after* opening the window so asset loading like `.convert_alpha()` is safe); Tiled map + tileset JSON loading (parses `hub_fronthouse.json` and the sibling `tiles_town.json`, matched by basename, into GIDs and a `walkable`-derived passability grid); sprite-sheet slicing (`partysprites.txt` → named party/NPC surfaces); menu asset loading (`load_menu_assets()` — `assets/menus/startmenu_*.png`); and `OverworldScene` itself (player movement, tile-to-tile tweening, the player-centered clamped camera, drawing, and the start-menu overlay). This used to be split across a separate `pygame_viewer/` package (`app.py` + `renderer.py` + `sprites.py`, plus dead code in `tileset.py`) — that split was rejected and the package was deleted; see the architecture note in CLAUDE.md. NPC placement isn't wired up (no object layer on the map yet).

The start-menu overlay is drawn last, on top of everything, only while `scene.menu.is_open`: `startmenu_bg.png` (fills the camera, mostly transparent), then each option row (`startmenu_inventory/party/settings/save.png`, fixed pixel coordinates, the highlighted row shifted +7px right), then `startmenu_cursor.png` at the highlighted row's unshifted coordinate. `OverworldScene.update()` returns immediately while the menu is open — the menu is a full gameplay pause, not just an input redirect.

**Interior scenes (`engine/scenes/interior.py`)** — Generic class for cave/dungeon and town interiors. Key properties: `spawn`, `entry_tile`, `combined_grid()`, `healer_spawn` (towns), `is_exit()`. Not yet wired into the pygame path — no pygame interior renderer exists yet (see roadmap: cave/town debug screens).

### Procedural Generation

All three generators have a data/render split: `generate_*_data(seed)` returns a pure dataclass (no PIL), `render_*_data(data, raw_tiles)` returns a PIL image.

**Overworld (`procgen/worldgen.py`)** — Infinite tiled world (one screen per coordinate pair). Base grass → blob placement (lakes, forests, mountains) → dirt patches → feature placement (towns, caves, castles) → jittered A* paths → scatter. Per-screen NES palette. Stable deterministic seed per `(world_seed, sx, sy)`.

**Town (`procgen/towngen.py`)** — Canvas sized to building count, cropped to bounding box + margin. Healer hut always present (fixed 3×2). 1–9 additional buildings, 92% in-cluster / 8% outlier. Ground blobs, cobble MST paths, vegetation, scatter. 8-colour NES palette.

**Cave/dungeon (`procgen/cavegen.py`)** — Up to 8 rooms, two flavours (cave: cobble/cave walls; dungeon: brick/dungfloor). Rooms connected by L-shaped/zig-zag hallways. Water pools, waterfalls, scatter. 3-colour NES palette.

### Viewscan (`engine/viewscan.py`)

Headless line-of-sight scan in four cardinal directions from a given tile. Each ray terminates at the first enterable, impassable, or screen-edge tile. Returns a `ViewScan` dataclass with one `DirectionScan` per direction. Not currently consumed by anything in the pygame path — useful groundwork for a future minimap/fog-of-war.

### RNG

One global seeded RNG, seed logged at run start. All randomness is engine-side. Same seed → same world, same enemy placement, same combat outcomes. Replays are essentially free.

---

## Directory Structure

```
simtank_rpg/
├── engine/
│   ├── player.py           # Player entity; PlayerState machine; try_move(passable_grid), serialise
│   ├── input.py            # HeldDirectionInput + START/B/A button routing → engine/menu.py
│   ├── menu.py             # StartMenu — pause-menu cursor state (open/close/move_cursor/confirm)
│   ├── renderer.py         # THE renderer: app loop, Tiled map+tileset JSON loading, GID tile
│   │                       #   slicing, sprite-sheet slicing, camera, OverworldScene draw/update,
│   │                       #   start-menu overlay draw
│   ├── battle.py           # battle loop; run_battle() — currently broken, see roadmap
│   ├── combat.py           # stat math, hit/crit/parry/damage resolution
│   ├── config.py           # config singleton (config.json)
│   ├── enemy_state.py      # NPC/agent placement — stale, still imports a deleted module;
│   │                       #   unused by anything currently running
│   ├── journal.py          # Journal — generic milestone log, used by battle.py
│   ├── viewscan.py         # line-of-sight scan → ViewScan dataclass
│   └── scenes/
│       └── interior.py     # cave + town interior scene class
├── procgen/
│   ├── enemygen.py         # enemy generation (stats, sprite, behavior, NES palette)
│   ├── npcgen.py           # town NPC generation
│   ├── worldgen.py         # overworld screen generator
│   ├── cavegen.py          # cave/dungeon interior generator
│   └── towngen.py          # town interior generator
├── assets/
│   ├── dialogue/           # (reserved — dialogue currently lives under data/npcs/, see below)
│   ├── events/             # (reserved — event/trigger scripts, see roadmap)
│   ├── sprites/            # party_sprites.png + layout text
│   ├── fonts/              # ModernDOS8x8.ttf
│   ├── menus/               # start menu art: startmenu_bg/cursor/inventory/party/settings/save.png
│   │                       #   (consumed by engine/renderer.py's load_menu_assets()), + menuicons.pxo
│   ├── tiles/               # tiles_town.png (tileset image) + tiles_town.json (Tiled tileset
│   │                       #   export — per-tile `walkable` property, read by engine/renderer.py)
│   │                       #   + the old tilerules TXT / hub CSV, now unused
│   └── titlescreen/         # titlescreen art
├── data/
│   ├── items/              # items.yaml (definitions) + ideas (brainstorm notes)
│   ├── npcs/                # NPC + logic + dialogue YAML (placeholder `dialogue`/`ideas`
│   │                       #   notes files today; see roadmap for the real schema)
│   ├── maps/               # hub_fronthouse.json — the live Tiled map export, read by
│   │                       #   engine/renderer.py. (Also has a stale draft YAML
│   │                       #   MapData, npcs_hub_fronthouse.yaml, predating the Tiled
│   │                       #   decision and still pointing at a deleted web/ CSV path — unused.)
│   └── party/               # character sheet JSONs
├── story/                   # narrative/world notes
├── tests/                   # test suite
├── maptest.py               # debug entry point — boots the real game loop, picks the scene
└── config.json               # pacing, display geometry, tunable params
```

**Where new content goes:**
- New/edited maps → author in Tiled, export JSON to `data/maps/`.
- New tilesets → image + property export in `assets/tiles/`.
- New items → `data/items/items.yaml`.
- New NPCs, dialogue, event/trigger logic → `data/npcs/` (schema not finalized — see roadmap).
- New abilities → `data/abilities/` (not created yet — see roadmap).

---

## Dev Setup

```bash
source .venv/bin/activate
python maptest.py           # opens the pygame window, runs the real game loop
```

All dependencies (pygame, Pillow, PyYAML) are in `.venv/`.
