# Front House Gaiden

An 8-bit RPG, developed entirely locally through a pygame renderer. Single-player.

---

## Content Pipeline

Two tools produce all game content. The engine's job is to **interpret** what they produce — maps and data files contain references, not gameplay logic.

| Content | Authored in | Format | Consumed by |
|---|---|---|---|
| Maps (tile layers, collision, object placement) | **Tiled** | JSON export | `engine/renderer.py` |
| Items, NPCs, dialogue, event flags, abilities | Hand-edited | **YAML** | `engine/` data loaders |

We are **not** building our own tile/map editor — Tiled is the map tool, full stop. Map loading and rendering live in one file, `engine/renderer.py` — see CLAUDE.md's architecture constraints for why.

### Maps (Tiled → JSON)

A map authored in Tiled and exported to JSON contains:

- Tile layer(s) — graphics
- Collision layer(s)
- Object layer — spawn points, NPCs, triggers, warps

Layers composite in **real, authored order** — whatever order they're listed in Tiled, tile layers and the Object Layer alike. `engine/renderer.py`'s `load_tiled_map()` walks the JSON's `layers` list once and keeps every tile layer plus a marker for the Object Layer, in that exact sequence (`TiledLayer.kind`, `"tile"` or `"objects"`); `OverworldScene.draw()` then walks that same list, blitting each tile layer (`_draw_tile_layer`) or the whole containers/signs/NPCs/enemies/player bundle (`_draw_entities`) as it reaches each one. A tile layer placed *before* the Object Layer in Tiled draws under the player/objects; one placed *after* draws over them (roofs, tree canopies, an "empty" tile a container's full-tile art should hide until it's opened — see Items, NPCs, Dialogue, Events below). Nothing is hardcoded to a specific layer name or index — different maps author their Object Layer at different points in the stack, and all of them composite correctly from the same code path.

A map may also paint from more than one tileset (e.g. `town.json` draws its ground from `tiles_town.json` and its buildings from `tiles_houses.json`) — `load_tiled_map()` loads every tileset a map declares, not just the first, and resolves each layer GID against whichever tileset's ID range it falls in. GIDs may also carry Tiled's per-cell mirror-flip flags (the "flip" brush) in their top 3 bits; `get_tile_by_gid()` strips and applies those rather than treating a flipped tile as a missing one.

Object layer entries are references only, e.g.:

```
NPC     id: old_man        sprite: old_man        logic: old_man_logic
Trigger script: castle_gate
Warp    destination_map: castle_entrance   spawn: south_gate
```

The engine loads the map, walks the object layer, and instantiates the right Python object per `type`. No gameplay logic is ever embedded in the map file itself.

Each map lives in its own folder under `data/maps/` (e.g. `data/maps/hub_fronthouse/`), holding the Tiled JSON export plus the YAML files described next. `engine/renderer.py`'s `load_map_objects()` parses the object layer into `MapObject` records, merging in each object's `dialogue`/`sprite`/`behavior`/etc. from that folder's `obj_<map>.yaml`/`npcs_<map>.yaml`. Tile-stamp objects with a `gid` (containers/signs, placed with Tiled's tile tool) anchor at their **bottom-left** corner; objects with no `gid` (NPCs, enemies — placed with the rectangle/point tool) anchor **top-left** like a plain Tiled rectangle. `type == "sign"`, `"npc"`, and `"container"` are all interactable — face one and press A to open the Dialogue System on its `dialogue` pages (a container also grants its `contents` item, if any, and stops drawing its own `gid` once opened). `type == "warp"` objects aren't rendered or A-press interactable — they're invisible trigger tiles; walking onto one fires a transition instead (see Warps below).

**Warps.** A `warp`-type object is both a trigger tile (stepping onto it fires a map transition) and a possible landing spot (another map's warp can target it). Each warp is authored with `destination_map` (the destination map's folder/stem name) and `destination_warp` (the *name* of the warp object on that destination map to land on) — that name lookup is scoped to `destination_map`, so warp names only need to be unique within one map's object layer, never globally. `facing` (default south) and `distance` (default 0, in tiles) describe *this* warp's own landing spot whenever another warp points here — e.g. `facing: S, distance: 1` lands one tile south of the warp, so the player steps out of a doorway instead of standing on it. Warps are one-way and hand-paired: a two-way door is two separate warp objects, one on each map, each pointing at the other. A step onto a warp fades to black (`cfg.warp_fade_out_ms`), swaps the loaded map behind the fade, then fades back in (`cfg.warp_fade_in_ms`) on the destination map's landing tile — see `OverworldScene._begin_warp`/`_tick_transition`/`_swap_map`.

Booting into a map fresh (not via an in-game warp trip, e.g. `maptest.py`'s map picker) spawns the player on a random one of the map's own warp objects, if it has any, rather than an arbitrary fallback tile — so debugging a map drops you at one of its own authored entrances.

### Map Object Sync (`data/maps/populate_yamls.py`)

A dev-only script, never imported by the engine or run at runtime. It reads a map's Tiled object layer and, per object, writes a stub entry keyed by the object's Tiled `id` into either `npcs_<map>.yaml` (type `npc`) or `obj_<map>.yaml` (everything else — `container`, `sign`, `warp`, `enemy`, `spawner`) in that same map's folder. Only `id`/`name`/`type` are synced from Tiled; new entries also get type-specific stub fields to hand-fill. Re-running never overwrites a field you've already filled in — it only touches `id`/`name`/`type`, adds stubs for genuinely new objects, and warns (without deleting) if an object disappears from the map.

```
python data/maps/populate_yamls.py                                          # syncs every map folder under data/maps/
python data/maps/populate_yamls.py data/maps/hub_fronthouse/hub_fronthouse.json  # syncs one map
```

This is how sign/container/NPC dialogue, container loot, enemy placement, and (later) scripted events get authored today — not by hand-writing the object registry, but by running the sync then filling in the stub fields it leaves behind.

### Items, NPCs, Dialogue, Events (YAML)

Everything that isn't map geometry is YAML under `data/`, keyed by ID and referenced from maps/other YAML — never embedded as inline logic.

**NPCs.** The master list is `data/npcs/npc.yaml`, loaded by `engine.npc.load_npc_defs()`/`load_npc_sprite_specs()`. It's two sections: `sprites:` names each NPC sprite *strip* under `assets/sprites/npcs/` (`{file: <stem>, colors: [...]}` — `colors` the placeholder hex colors actually baked into that PNG); `npcs:` maps id → `sprite`/`behavior`/`facing`/`colors`/`dialogue`. A strip's width picks how it animates: 32×16 (2 frames) is a facing-less south-only idle loop; 128×16 (8 frames) is a full walk cycle and responds to `facing`. Any `npc`-type object's entry in `npcs_<map>.yaml` can set `npc_id` to reference a shared def — its own `sprite`/`behavior`/`dialogue` fields become optional overrides on top of it, filled in they win for that placement, left blank they fall back to the def. `npc_id` is additive, not required — an entry with none behaves fully inline, as before this existed.

**Recoloring.** An `npcs:` entry's own `colors`, if set, replaces its sprite's placeholder colors for that NPC specifically, position-matched against the sprite's own `colors` list — this is what lets two NPCs share one sprite strip but read as visually distinct characters (`engine.renderer.recolor_surface()`, computed once per unique NPC def at boot, not per frame). **Hex values must be quoted in `npc.yaml`** (`"000000"`, not `000000`) — YAML's implicit typing reads an unquoted all-digit scalar as a number, and for some digit patterns not even the *right* number (`001100` unquoted parses as octal, decimal 576) — silently wrong, no YAML error at all.

**Dialogue** — plain pages, keyed by ID:

```yaml
old_man_intro:
  pages:
    - "Welcome to the village."
    - "Stay away from the forest."
```

Signs and NPCs both use this page-list shape today, but inline — a sign's `dialogue` lives directly on its entry in `obj_<map>.yaml`, an NPC's on its entry in `npcs_<map>.yaml`, rather than in a separate ID-keyed file. A separate ID-keyed dialogue file resolved conditionally through NPC logic isn't built yet — see Roadmap (Event System).

**Event System — conditions and actions.** One flag primitive, `GameState.flags` (`engine/game_state.py`), is the only thing a condition ever checks — deliberately not two (flags *and* a live inventory lookup): "has the castle key" is modeled as a flag set the moment the key's granted, not `Inventory.has()`.

```yaml
if: boss1_dead        # true only if the flag's set
if_not: gate_open      # true only if the flag's NOT set

then:
  - set_flag: gate_open
  - give_item: rusty_key
  - dialogue: gate_now_open
```

Built now (containers) or next in line: `set_flag`, `clear_flag`, `give_item`, `remove_item`, `dialogue`. Reserved for later, as more entries in this *same* list shape: `pan_camera`, `force_move`, `wait`.

**Current state:** the flag store this all sits on is real and save-able (`GameState.flags`/`persistent_id`), and so is order-preserving layer compositing (see Maps above), which is what lets a triggered object's tile disappear into whatever's painted underneath it. What's actually built *on* that foundation is still one concrete case, not the general `if`/`then`/`else` evaluator sketched above: containers. Every container gets its flag for free via `persistent_id(map_name, object_name)` — no registry, no per-object declaration. Opening one sets that flag and is inert afterward, one open, ever; a container with `contents` set also grants it, appending a synthesized `"Received {item name}."` page — see `engine.input._open_container`.

Still not built: the general `if`/`if_not` condition check and `then`/`else` action-list executor, conditional dialogue for signs/NPCs, locked warps, and the `pan_camera`/`force_move`/`wait` action kinds. Also scoped but not designed at all yet: **animated tiles** (frame-cycling GIDs — how that interacts with the current one-surface-per-GID tile lookup isn't decided).

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Content Pipeline (Tiled + YAML)
- [x] Tiled JSON import — tile layers, tileset `walkable` property → passability grid, GID rendering, clamped camera; multiple tilesets per map, mirrored tiles
- [x] Object layer → engine objects — `container`/`sign`/`npc`/`enemy`/`spawner`/`warp`, synced to editable YAML via `data/maps/populate_yamls.py`
- [x] Order-preserving layer compositing — real Tiled layer order, not a hardcoded "first layer below" rule
- [x] Warps — fade-to-black map transitions, landing-spot offset/facing
- [x] Global game state / flags (`engine/game_state.py`) + `persistent_id()` per-object keys
- [x] Container → inventory wiring, one-time open, visual empty-out via layer compositing
- [x] Dialogue System (`engine/dialogue.py`) — paged, typewriter box; wired to sign/npc/container
- [x] Master NPC YAML (`data/npcs/npc.yaml`) — shared defs, per-NPC recoloring, `npc_id` override pattern
- [x] Enemy/spawner placement, spawn-chance resolution, continuous movement (not battle-triggered yet)
- [ ] Event System — general `if`/`then`/`else` script executor (containers are the only flag-driven case built so far)
- [ ] Trigger System — script-driven doors/switches, locked warps
- [ ] NPC logic YAML — conditional dialogue via the Event System
- [ ] Animated tiles — not designed yet

### Input & Core Game Loop
- [x] Keyboard input, held-key axis resolution (`engine/input.py`) — real 8-directional movement, tap-to-turn vs. hold-to-walk
- [x] Player entity (`engine/player.py`) — state machine, continuous movement against a passable grid
- [x] Config-driven movement/dialogue pacing (`config.json`)
- [x] Start menu shell, save-confirm overlay, inventory screen (view-only) (`engine/menu.py`)
- [x] Save through menu (`engine/save.py`)
- [ ] Bluetooth / USB controller support
- [ ] Title screen
- [ ] Start menu sub-screens — PARTY/SETTINGS still just highlight, no screen behind them
- [ ] Load through menu (mid-game — slot picking at boot via `maptest.py` is built)

### Debug & Testing Screens
- [x] `maptest.py` — map / battles / debug screen prompt, boots the real game loop, not a reduced path
- [x] Debug map picker + debug save-slot picker
- [x] Battle debug screen (`engine.renderer.BattleScene`)
- [x] Map hot-reload — **R key**, re-reads the current map's JSON/tileset off disk without restarting the process, keeping player position. Dev-only, cut before release
- [ ] Debug overlay — passability grid, NPC labels, tile coords, FPS
- [ ] Dedicated interior/cave debug screen — `maptest.py`'s `debug screen` prompt is still a stub

### Items & Abilities
- [x] Item YAML (`data/items/items.yaml`) + loader/inventory (`engine/inventory.py`) + inventory screen
- [ ] Pickup system (nothing currently adds items to `Inventory`), equip slots, stat effects in battle, USE action
- [ ] Special abilities YAML (`data/abilities/`)

### World & Content
- [ ] Rethink procgen scope — decide what stays procedural vs. hand-authored in Tiled
- [ ] Procgen output → Tiled-compatible JSON, so generated and hand-authored maps share one loader
- [ ] Updated and expanded tilesets
- [ ] Items in the world — pickup system, equip slots + stat effects
- [ ] Named locations — towns, dungeons, overworld landmarks

### Visuals & UI
- [x] Battle resolver (`engine/battle.py`, headless `Fighter`/`BattleState`) + graphical debug screen (`engine.renderer.BattleScene`) — MELVIN vs. one enemy, auto-attack only
- [ ] Battle screen overhaul — real art (currently plain textboxes + procgen backgrounds)
- [ ] Player-driven battle action menu — ATTACK/ITEM/SPECIAL/RUN row is drawn but not interactive; ITEM/SPECIAL scope isn't decided, ask before implementing
- [ ] Full party in battle (currently MELVIN-only, 1v1) — party specials are designed but not implemented:

  | Member | Special | Effect | MP cost |
  |---|---|---|---|
  | BILLY | SING | MESMERIZE — 50% skip chance, escalating break probability | 8 |
  | MELVIN | LAUGH | CRINGE — 35% enemy self-attack per turn, 3–5 turns | 7 |
  | POOTS | SNACK | Heals a party member 15–25% max HP | 6 |
  | SMELTRUD | TICKLE | +15% ally damage for 2 turns | 5 |

- [ ] Tile-swipe battle transition
- [ ] Party status panel (HP, MP, level, XP per member)
- [ ] Battle-speed config (separate from movement speed)

---

## Directory Structure

```
simtank_rpg/
├── engine/
│   ├── player.py           # Player entity, PlayerState machine, continuous 8-directional move()
│   ├── input.py            # HeldDirectionInput + START/B/A button routing
│   ├── menu.py             # StartMenu + SaveMenu + InventoryMenu — cursor state, no pygame
│   ├── inventory.py        # ItemDef/load_item_defs() + Inventory (shared item-id → quantity pool)
│   ├── game_state.py       # GameState (flags/variables) + persistent_id()
│   ├── save.py             # save_to_slot/load_from_slot/clear_slot/slot_exists — JSON under saves/
│   ├── dialogue.py         # DialogueBox — paged, typewriter-revealed dialogue state
│   ├── renderer.py         # THE renderer: app loop, Tiled map/tileset/object-layer loading,
│   │                       #   sprite slicing, camera, OverworldScene + BattleScene
│   ├── battle.py           # Fighter + hit/crit/parry/damage math + BattleState (headless)
│   ├── config.py           # config singleton (config.json)
│   ├── enemy.py            # EnemyDef/Enemy + enemies.yaml loader + spawn resolution + movement
│   ├── movement.py         # step_continuous + collision helpers, shared by player.py/enemy.py
│   ├── npc.py              # NpcDef/NpcSpriteSpec + data/npcs/npc.yaml loader
│   ├── journal.py          # Journal — generic milestone log, used by battle.py
│   ├── viewscan.py         # line-of-sight scan → ViewScan dataclass (built, not consumed yet)
│   └── scenes/
│       └── interior.py     # cave + town interior scene class (not wired into the pygame path yet)
├── procgen/
│   ├── worldgen.py         # overworld screen generator
│   ├── towngen.py          # town interior generator
│   ├── cavegen.py          # cave/dungeon interior generator
│   ├── enemygen.py         # enemy generation (stats/sprite/behavior) — not wired to engine/enemy.py
│   ├── npcgen.py           # town NPC generation
│   ├── spritegen.py, sprites.py, names.py   # sprite/name generation helpers
│   ├── visualizer.py       # animated battle-background effects (engine.renderer.BattleScene)
│   └── *_test.py           # ad hoc preview/test scripts, not part of the pytest suite
├── assets/
│   ├── sprites/            # party_sprites.png + layout text (deprecated for NPCs)
│   │   ├── enemies/         # one strip PNG per enemy, stem == enemies.yaml's `sprite`
│   │   └── npcs/            # one strip PNG per NPC sprite, stem == npc.yaml's `file`
│   ├── fonts/              # ModernDOS8x8.ttf — dialogue text font
│   ├── menus/               # start menu / save-confirm / dialogue box art + .pxo sources
│   ├── tiles/               # tileset images + Tiled tileset JSON exports (walkable property)
│   ├── titlescreen/         # titlescreen art
│   └── fx/                  # transition-effect art (source .pxo, not consumed yet)
├── data/
│   ├── items/               # items.yaml + ideas (brainstorm notes)
│   ├── enemy/               # enemies.yaml — master enemy list
│   ├── npcs/                # npc.yaml — master NPC list, + dialogue/ideas (planning notes, unused)
│   ├── maps/                # populate_yamls.py (dev tool) + one folder per map:
│   │   │                    #   <name>.json (Tiled export) + obj_<name>.yaml + npcs_<name>.yaml
│   │   ├── hub_fronthouse/, town/, town_town1/, town_parkinglot/, town_tunnel/
│   │   └── interior_deptstore/, interior_office_deptstore/, interior_pizzahutparkinglot/,
│   │       interior_town1/, interior_wc_deptstore/, interior_hub_rearhouse/
│   │       # all synced to obj_/npcs_ YAML except `town`, still fresh — see populate_yamls.py
│   └── party/               # character sheet JSONs
├── tests/                   # pytest suite — currently scaffolding only, no cases yet
├── saves/                   # gitignored — slot1.json.. written by engine/save.py, nothing checked in
├── maptest.py               # debug entry point — prompts map/battle/debug screen
└── config.json               # pacing, display geometry, tunable params
```

**Where new content goes:**
- New/edited maps → author in Tiled, export JSON to `data/maps/<map_name>/<map_name>.json` (its own folder), then run `python data/maps/populate_yamls.py` to sync `obj_`/`npcs_` YAML stubs into that same folder.
- New tilesets → image + property export in `assets/tiles/`.
- New items → `data/items/items.yaml`.
- New enemies → add an entry to `data/enemy/enemies.yaml` and drop its overworld sprite strip in `assets/sprites/enemies/` (filename stem must match the entry's `sprite` field) and, for a battle-screen portrait, a full-size PNG in `assets/tiles/enemies/` (matched by the entry's `battle_art` field). Place it on a map by adding an `enemy`/`spawner` object in Tiled, then running `populate_yamls.py` to stub `obj_<map>.yaml` and hand-filling `enemy_id`/`level` (or `enemies`/`spawn_chance`/`level` for a spawner).
- New container loot → set that container's `contents:` (in its map's `obj_<map>.yaml`, hand-filled after `populate_yamls.py` stubs it to `null`) to an item id from `data/items/items.yaml`. Leave it `null` for a container that's dialogue-only. To make it visually empty out on open, paint the "opened" tile on a tile layer beneath where the container object sits.
- New NPC sprite → drop a strip PNG in `assets/sprites/npcs/` (32×16 south-only 2-frame idle, or 128×16 full 8-frame walk cycle), then add an entry under `sprites:` in `data/npcs/npc.yaml` pointing `file:` at its filename stem and listing its placeholder `colors:` (hex, **quoted**).
- New reusable NPC → add an entry under `npcs:` in `data/npcs/npc.yaml` referencing one of those sprite ids, optionally its own `colors:`/`facing:`, then set `npc_id` to that entry's key on any placement's `npcs_<map>.yaml` entry. Leave a placement's own `sprite`/`behavior`/`dialogue` blank to inherit from the def, fill one in to override it for just that placement.
- New abilities → `data/abilities/` (not created yet — see Roadmap).

---

## Dev Setup

```bash
source .venv/bin/activate
python maptest.py           # prompts: map / battles / debug screen (debug screen is a stub)
                             # `map` lists every folder under data/maps/ — pick one to open it
                             # in the real game loop
                             # `battles` lists every enemy in data/enemy/enemies.yaml — pick
                             # one to fight MELVIN 1v1 (auto-attack only, see Roadmap)
```

All dependencies (pygame, Pillow, PyYAML) are in `.venv/`.
