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

By convention, a map's **"Tile Layer 2"** renders above the player/NPC sprites (not below, like every other tile layer) — this lets sprites pass behind tall map features such as roofs and tree canopies. `engine/renderer.py` special-cases this layer name (`_ABOVE_SPRITE_LAYERS`) when drawing.

Object layer entries are references only, e.g.:

```
NPC     id: old_man        sprite: old_man        logic: old_man_logic
Trigger script: castle_gate
Warp    destination_map: castle_entrance   spawn: south_gate
```

The engine loads the map, walks the object layer, and instantiates the right Python object per `type`. No gameplay logic is ever embedded in the map file itself.

**Current state:** the hub (`data/maps/hub_fronthouse/hub_fronthouse.json`) is the first map built this way and the only one that exists — `engine/renderer.py` reads its tile layers plus the sibling tileset property export (`assets/tiles/tiles_town.json`, matched by basename) to resolve per-tile passability from a `walkable` bool property, and renders it with a camera that follows the player, clamped to map edges. The old hand-rolled CSV+tilerules loader and the draft YAML `MapData` loader are gone entirely — there is no `engine/map_loader.py` anymore. The map's **object layer** has `container`/`sign` object placement (see below) but no `npc` objects yet, so the hub still has no NPCs; warps also aren't wired up yet.

The object layer is now actually read at runtime: `engine/renderer.py`'s `load_map_objects()` parses it into `MapObject` records (`id`/`name`/`type`/`row`/`col`/`gid`), merging in each object's `dialogue` from `obj_<map>.yaml`/`npcs_<map>.yaml` (converting Tiled's bottom-left-anchored pixel position to a tile row/col along the way). Every object renders at its tile using its own `gid`, same tileset as the map. Only `type == "sign"` is interactable so far — face one and press A to open the Dialogue System (see below) on its `dialogue` pages; containers render but don't open yet (out of scope until the Event System exists).

Each map lives in its own folder under `data/maps/` (e.g. `data/maps/hub_fronthouse/`), holding the Tiled JSON export plus the YAML files described next.

### Map Object Sync (`data/maps/populate_yamls.py`)

A dev-only script, never imported by the engine or run at runtime. It reads a map's Tiled object layer and, per object, writes a stub entry keyed by the object's Tiled `id` into either `npcs_<map>.yaml` (type `npc`) or `obj_<map>.yaml` (everything else — `container`, `sign`, ...) in that same map's folder. Only `id`/`name`/`type` are synced from Tiled; new entries also get type-specific stub fields to hand-fill (`container`: `contents`/`dialogue`, `sign`: `dialogue`, `npc`: `dialogue`/`event`). Re-running never overwrites a field you've already filled in — it only touches `id`/`name`/`type` and adds stubs for genuinely new objects, and warns (without deleting) if an object disappears from the map.

```
python data/maps/populate_yamls.py                                          # syncs every map folder under data/maps/
python data/maps/populate_yamls.py data/maps/hub_fronthouse/hub_fronthouse.json  # syncs one map
```

This is how sign/container/NPC dialogue, container loot, and (later) scripted events get authored today — not by hand-writing the object registry, but by running the sync then filling in the stub fields it leaves behind.

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

**Current state:** signs use this same page-list shape today, but inline — each sign's `dialogue` list lives directly on its entry in `obj_<map>.yaml` (see Map Object Sync above) rather than in a separate ID-keyed file like the sketch above. `engine/dialogue.py` + `engine/renderer.py` render it as a typed-in, paginated box (see What's Built). NPC dialogue — a separate ID-keyed file resolved through NPC logic, as sketched here — isn't built yet; see roadmap.

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

A sign interaction resolves today as: `Player.adjacent_interactable()` finds the sign → `engine.input.handle_a_button` wraps its dialogue pages to fit the box (via a `wrap_pages` callback into `engine/renderer.py`, which owns the `pygame.font` metrics) → `engine.dialogue.DialogueBox` opens and types them in. An NPC interaction is planned to resolve the same way once NPC objects and the Event System exist: `Player → NPC.interact() → Event System resolves "old_man_logic" → Dialogue System renders the returned page(s)`.

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Content Pipeline (Tiled + YAML)
- [x] Tiled JSON importer (`engine/renderer.py`) — tile layers + tileset `walkable` property → passability grid, GID-based rendering, player-centered clamped camera
- [x] Retired the CSV+tilerules hub loader and the draft YAML `MapData` loader — `engine/map_loader.py` no longer exists
- [x] `container`/`sign` objects placed on the hub's object layer, synced to editable YAML via `data/maps/populate_yamls.py` — see Map Object Sync above
- [ ] `npc` objects placed on the hub's object layer — none placed yet, `npcs_hub_fronthouse.yaml` is currently empty
- [x] `container`/`sign` objects → engine instantiation — `engine/renderer.py`'s `load_map_objects()` reads the object layer plus `obj_*`/`npcs_*` YAML at runtime, renders each object at its tile, and makes `sign`-type objects interactable (opens the Dialogue System below); containers render but aren't interactable yet
- [ ] `NPC`/`Trigger`/`Warp`/spawn-point object types → engine instantiation — still nothing reads these; blocked on NPC objects existing on the map (see above) and the Trigger/Warp systems below
- [ ] Event System — flag dict + condition evaluation (`if`/`else` chains) → dialogue ID or action list
- [x] Dialogue System (`engine/dialogue.py` + `engine/renderer.py`) — paged dialogue box: word-wrapped and paginated to fit the box art, typewriter reveal (hold A/B to speed it up), A advances/closes once the current page's fully revealed, docks to the bottom of the screen or flips to the top (mirrored art + text rect) when the player's in the lower screen half. Wired to `sign`-type objects only — NPC dialogue and the Event System that would pick a dialogue ID conditionally are still open (see above)
- [ ] Trigger System — executes a referenced script YAML on activation (doors, chests, switches, warps)
- [ ] NPC logic YAML (`data/npcs/`) — sprite + logic ID + dialogue tree, replacing the current placeholder `dialogue`/`ideas` notes files
- [ ] Warp/exit objects wired from the Tiled object layer once it exists

### Input & Core Game Loop
- [x] Keyboard input: arrow keys move the player, held-key resolution ("last pressed wins", never diagonal) lives in `engine/input.py`'s `HeldDirectionInput`
- [x] Z (B button), X (A button), Enter (START) — wired to the start menu (START opens/closes it, B closes it, A confirms the highlighted option — stub, see below) and to dialogue (A opens a dialogue box by facing an interactable sign, advances/closes it once the current page's fully typed in; holding A or B speeds up the typewriter reveal)
- [x] Player character entity (`engine/player.py`) — `Player` with `PlayerState` machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE), `try_move()`, `adjacent_interactable()`, serialise/deserialise
- [x] Config-driven movement speed — `player_move_ms` in `config.json`, read via the `engine.config.cfg` singleton; `dialogue_char_ms`/`dialogue_char_fast_ms` follow the same pattern for the dialogue box's typewriter reveal speed (normal vs. holding A/B)
- [ ] Bluetooth / USB controller support (pygame joystick API)
- [ ] Title screen
- [x] Start menu shell (`engine/menu.py`) — START pauses gameplay and opens a 4-option vertical cursor menu (INVENTORY/PARTY/SETTINGS/SAVE), wraps top-to-bottom, closes on B or START; drawn by `engine/renderer.py` from `assets/menus/`
- [x] Save-confirm overlay (`engine/menu.py`'s `SaveMenu`) — SAVE on the start menu opens a YES/NO overlay on top of it (defaults to NO, only E/W move the cursor); B or confirming NO closes it back to the start menu
- [ ] Start menu sub-screens — INVENTORY/PARTY/SETTINGS each currently just highlight, no screen behind them yet; SAVE opens the confirm overlay above, but confirming YES there is still a stub — see `SaveMenu.confirm()`'s TODO for the actual save/write-to-disk path
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

**Input (`engine/input.py`)** — `HeldDirectionInput` resolves which cardinal direction (if any) should be stepped each frame from a set of currently-held directions, "last pressed wins," so held keys never combine into a diagonal. Also owns discrete button routing: `handle_start_button`/`handle_b_button`/`handle_a_button`/`handle_menu_direction` decide what a START/B/A press or a direction press means given the player's current state and which of `engine/menu.py`'s two menus is "on top" (the save-confirm overlay, when open, always gets input first — e.g. arrow keys drive whichever menu's cursor instead of player movement while `PlayerState.IN_MENU`). `handle_a_button` also drives `engine/dialogue.py`'s `DialogueBox`: with no menu open, A advances/closes an already-open dialogue box (a no-op while the current page is still typing in), or — if the player's idle and facing an interactable `sign`-type object — opens one via `Player.adjacent_interactable()`. It takes a `wrap_pages` callback so the actual pixel-width word-wrapping (which needs `pygame.font` metrics) stays in `engine/renderer.py`. Pure logic, no pygame dependency — the renderer owns raw key→direction/button mapping and feeds abstract strings in.

**Menu (`engine/menu.py`)** — `StartMenu`: cursor state for the pause-menu (`is_open`, `selected`, four options — INVENTORY/PARTY/SETTINGS/SAVE). Vertical list, wraps top-to-bottom. `SaveMenu`: a YES/NO confirm overlay opened from SAVE and drawn on top of `StartMenu`; horizontal pair (only E/W move the cursor), defaults to NO. Neither reads input itself — both only expose `open()`/`close()`/`move_cursor()`/`confirm()`; `engine/input.py` decides when to call them, `engine/renderer.py` decides how they look. `StartMenu.confirm()` is a stub for every option except SAVE (which opens `SaveMenu`); `SaveMenu.confirm()` on YES is also still a stub — see its TODO for the actual save/write path.

**Dialogue (`engine/dialogue.py`)** — `DialogueBox`: pure state for a paged, typed-in text overlay opened by facing an interactable sign and pressing A. Tracks `is_open`, the current `pages` list (pre-wrapped to fit the box's text area — see Scenes below), `page_index`, and a `chars_shown`/`tick(dt_ms, ms_per_char)` typewriter reveal. `advance()` (an A press) only turns the page or closes the box once the current page's fully revealed — otherwise it's a no-op, so the player always gets a beat to read before moving on. Same split as `engine/menu.py`: no pygame, no drawing; `engine/renderer.py` feeds it elapsed time each frame and decides how it looks.

**Config (`engine/config.py`)** — `config.json` singleton. Pacing (move_ms, screen crossing, interior entry/exit, dialogue typewriter speed), battle limits, display geometry, interior exploration distance.

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

**`engine/renderer.py`** — THE graphical renderer, one file, full stop. It owns: the generic pygame app loop (`run(scene_factory, ...)` — window, clock, event dispatch; constructs the scene *after* opening the window so asset loading like `.convert_alpha()` is safe); Tiled map + tileset JSON loading (parses `hub_fronthouse.json` and the sibling `tiles_town.json`, matched by basename, into GIDs and a `walkable`-derived passability grid); Tiled **object layer** loading (`load_map_objects()` — parses `container`/`sign`/`npc` objects into `MapObject` records and merges in each one's `dialogue` from `obj_<map>.yaml`/`npcs_<map>.yaml`, converting each object's bottom-left-anchored Tiled pixel position to a tile row/col); sprite-sheet slicing (`partysprites.txt` → named party/NPC surfaces); menu asset loading (`load_menu_assets()` — `assets/menus/startmenu_*.png`, `save_menu_*.png`, and `dialogue_box.png` plus a pre-flipped copy of it); dialogue font loading (`ModernDOS8x8.ttf` at 16pt, which renders at 8px); and `OverworldScene` itself (player movement, tile-to-tile tweening, the player-centered clamped camera, drawing map objects/NPCs/player, and the start-menu/save-confirm/dialogue-box overlays). This used to be split across a separate `pygame_viewer/` package (`app.py` + `renderer.py` + `sprites.py`, plus dead code in `tileset.py`) — that split was rejected and the package was deleted; see the architecture note in CLAUDE.md. The object layer's `container`/`sign` objects are placed and rendered (`can1`, `can2`, `container3`, `sign1` on the hub map today); `sign1` is interactable. `npc`-type objects still aren't placed on any map, so NPC placement/AI (`engine/enemy_state.py`) remains unwired — see Content Pipeline roadmap.

The start-menu overlay is drawn on top of everything while `scene.menu.is_open`: `startmenu_bg.png` (fills the camera, mostly transparent), then each option row (`startmenu_inventory/party/settings/save.png`, fixed pixel coordinates, the highlighted row shifted +7px right), then `startmenu_cursor.png` at the highlighted row's unshifted coordinate. If SAVE was confirmed, the save-confirm overlay draws on top of that in turn while `scene.save_menu.is_open`: `save_menu_bg.png`, then `save_menu_cursor.png` at a fixed spot per option (only the cursor moves — YES/NO aren't separate images). `OverworldScene.update()` returns immediately while the start menu is open (which covers the save-confirm overlay too, since it only ever opens from within the start menu) — the menu is a full gameplay pause, not just an input redirect.

The dialogue box overlay draws on top of everything while `scene.dialogue.is_open`: `dialogue_box.png` is 256×240 — 16px taller than the 256×224 game screen — so it docks to one screen edge with the other 16px cropped off (NES-overscan-style), normally the *bottom* edge (`blit_y = view_h - box_h`, cropping the extra off the top). When the player is in the lower half of the screen — which happens near a map edge, where the camera clamp stops re-centering them — it flips to dock to the *top* instead, using the pre-flipped copy of the image and a mirrored text-rect offset, so the box never ends up sitting on top of the player it was opened next to. Text is word-wrapped and paginated to the box's free-space rect (inset by a 4px margin) by `_wrap_text_to_screens()`, using `pygame.font` metrics — a page that overflows one screen's worth of lines becomes multiple screens, paged through one A press at a time. `OverworldScene.update()` calls `dialogue.tick(dt_ms, ms_per_char)` every frame while open to drive the typewriter reveal — `cfg.dialogue_char_ms` normally, or the faster `cfg.dialogue_char_fast_ms` while A or B is held (tracked via `_held_buttons`, independent of `HeldDirectionInput`) — and is otherwise a full gameplay pause like the start menu.

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
│   ├── input.py            # HeldDirectionInput + START/B/A button routing → engine/menu.py,
│   │                       #   engine/dialogue.py
│   ├── menu.py             # StartMenu + SaveMenu — pause-menu and save-confirm cursor state
│   │                       #   (open/close/move_cursor/confirm on each)
│   ├── dialogue.py         # DialogueBox — paged, typewriter-revealed dialogue state
│   │                       #   (open/close/advance/tick/visible_text)
│   ├── renderer.py         # THE renderer: app loop, Tiled map+tileset+object-layer JSON loading,
│   │                       #   GID tile slicing, sprite-sheet slicing, camera, OverworldScene
│   │                       #   draw/update, start-menu + save-confirm + dialogue-box overlay draw
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
│   ├── fonts/              # ModernDOS8x8.ttf — dialogue text font, loaded by engine/renderer.py
│   │                       #   at 16pt (renders at 8px)
│   ├── menus/               # start menu art: startmenu_bg/cursor/inventory/party/settings/save.png
│   │                       #   + save-confirm overlay art: save_menu_bg/cursor.png
│   │                       #   + dialogue box art: dialogue_box.png (dialogue_cursor.png unused so far)
│   │                       #   (all consumed by engine/renderer.py's load_menu_assets()),
│   │                       #   + menuicons.pxo, savegame.pxo, startmenu.pxo, diaglogue.pxo (source files)
│   ├── tiles/               # tiles_town.png (tileset image) + tiles_town.json (Tiled tileset
│   │                       #   export — per-tile `walkable` property, read by engine/renderer.py)
│   │                       #   + the old tilerules TXT / hub CSV, now unused
│   └── titlescreen/         # titlescreen art
├── data/
│   ├── items/              # items.yaml (definitions) + ideas (brainstorm notes)
│   ├── npcs/                # NPC + logic + dialogue YAML (placeholder `dialogue`/`ideas`
│   │                       #   notes files today; see roadmap for the real schema)
│   ├── maps/               # populate_yamls.py (dev tool, not runtime) + one folder per map
│   │   ├── populate_yamls.py  # syncs a map's Tiled object layer into obj_/npcs_ YAML stubs
│   │   └── hub_fronthouse/    # the live map folder — only one that exists today
│   │       ├── hub_fronthouse.json      # Tiled map export, read by engine/renderer.py
│   │       ├── obj_hub_fronthouse.yaml  # container/sign objects — dialogue, contents (hand-edited,
│   │       │                           #   read at runtime by engine/renderer.py's load_map_objects())
│   │       └── npcs_hub_fronthouse.yaml # npc objects — dialogue, event flag (empty, none placed yet)
│   └── party/               # character sheet JSONs
├── story/                   # narrative/world notes
├── tests/                   # test suite
├── maptest.py               # debug entry point — boots the real game loop, picks the scene
└── config.json               # pacing, display geometry, tunable params
```

**Where new content goes:**
- New/edited maps → author in Tiled, export JSON to `data/maps/<map_name>/<map_name>.json` (its own folder), then run `python data/maps/populate_yamls.py` to sync `obj_`/`npcs_` YAML stubs into that same folder.
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
