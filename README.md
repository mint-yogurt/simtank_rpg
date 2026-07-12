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

A map's **first tile layer** (the ground) renders below the player/NPC/object sprites; every tile layer after it renders above them, stacked in the same order Tiled lists them — this lets sprites pass behind tall map features such as roofs and tree canopies, and works for any number of overlay layers (not just a hardcoded "Tile Layer 2"). `engine/renderer.py`'s `load_tiled_map()` derives this from layer position (`TiledLayer.above`) rather than matching layer names.

Object layer entries are references only, e.g.:

```
NPC     id: old_man        sprite: old_man        logic: old_man_logic
Trigger script: castle_gate
Warp    destination_map: castle_entrance   spawn: south_gate
```

The engine loads the map, walks the object layer, and instantiates the right Python object per `type`. No gameplay logic is ever embedded in the map file itself.

**Current state:** `data/maps/` holds several map folders — `hub_fronthouse` and `town_town1` (both on the town tileset), `interior_deptstore` and `interior_wc_deptstore` (their own tileset) — `engine/renderer.py`'s `OverworldScene` takes a `map_path` and reads that map's tile layers plus its sibling tileset property export (matched by basename, e.g. `assets/tiles/tiles_town.json` or `assets/tiles/tiles_interior_deptstore.json`) to resolve per-tile passability from a `walkable` bool property, and renders it with a camera that follows the player, clamped to map edges. `maptest.py` prompts for which map to load — see Dev Setup below. The old hand-rolled CSV+tilerules loader and the draft YAML `MapData` loader are gone entirely — there is no `engine/map_loader.py` anymore. Object-layer `container`/`sign` placement (see below) is synced to YAML on the hub map, and `interior_deptstore` now has one hand-placed `npc` object (`npc1`) synced to its own `obj_`/`npcs_` YAML too; `town_town1` still has no `obj_`/`npcs_` YAML at all. Warps are wired up (see below) — `interior_wc_deptstore` has one placed (`wc_warp1`), not yet pointed at a destination.

The object layer is now actually read at runtime: `engine/renderer.py`'s `load_map_objects()` parses it into `MapObject` records (`id`/`name`/`type`/`row`/`col`/`gid`), merging in each object's `dialogue` from `obj_<map>.yaml`/`npcs_<map>.yaml` — `npc`-type objects also merge `sprite`/`behavior` this way (converting Tiled's pixel position to a tile row/col along the way, plus an un-floored `row_exact`/`col_exact` pair kept alongside it — see below). That conversion branches on whether the object has a `gid`: tile-stamp objects (containers/signs, placed with Tiled's tile tool) anchor at their **bottom-left** corner, so their height is subtracted first; objects with no `gid` (NPCs, which don't need a tileset graphic — placed with the rectangle/point tool instead) anchor **top-left** like any plain Tiled rectangle and must NOT get that correction, or they land one tile off from where Tiled shows them (a real bug hit and fixed while wiring up `interior_deptstore`'s NPC). Containers/signs render at their tile using their own `gid`, snapped to the grid via `row`/`col`, same tileset as the map; `npc`-type objects are pulled into a separate `NPC` list instead, rendered from the party sprite sheet, and — unlike containers/signs — placed at their *exact* Tiled pixel position rather than snapped to the tile grid (`row_exact`/`col_exact`; see NPC behaviors under What's Built for how that reconciles with NPCs otherwise being tile-discrete). `type == "sign"` and `type == "npc"` are both interactable — face one and press A to open the Dialogue System (see below) on its `dialogue` pages; containers render but don't open yet (out of scope until the Event System exists). `type == "warp"` objects are neither rendered nor A-press interactable — they're invisible, one-tile trigger zones parsed into their own `OverworldScene.warps` list (not `objects`), and instead of pressing A the player just has to walk onto one (see Warps below).

**Warps.** A `warp`-type object is both a trigger tile (stepping onto it fires a map transition) and a possible landing spot (another map's warp can target it). Each warp is authored with four more YAML-only fields beyond `dialogue`/`sprite`/`behavior` (see Map Object Sync below): `destination_map` (the destination map's folder/stem name) and `destination_warp` (the *name* of the warp object on that destination map to land on). That name lookup is always scoped to `destination_map` first, so warp names only need to be unique within one map's object layer, never across the whole game — reuse conventional names like `door_north`/`entry`/`exit` everywhere. The other two, `facing` and `distance`, do double duty as *this* warp's own landing spot whenever some other warp's `destination_warp` points here: `facing` (default south if left blank) is both which way the player faces after spawning and which direction `distance` (default 0) offsets the landing tile from this warp's own `row`/`col` — e.g. `facing: S, distance: 1` lands one tile south of the warp, so the player visibly steps out of a doorway instead of standing on it; `distance: 0` or unset lands exactly on the warp's own tile. Warps are one-way and hand-paired by the map author: a two-way door is two separate warp objects, one on each map, each pointing at the other.

`OverworldScene._step_player()` checks the player's new tile against `self.warps` right after a successful step (`_warp_at`); a hit calls `_begin_warp()`, which — if both `destination_map` and `destination_warp` are filled in (an unconfigured warp, e.g. a freshly-placed stub, just prints a warning and does nothing) — starts a scene-level transition state machine (`_transition_phase`: `None` → `"out"` → `"in"` → `None`), driven from `OverworldScene.update()` exactly like the dialogue box and start menu are: a full gameplay pause while active. `"out"` fades a solid black overlay in over `cfg.warp_fade_out_ms` (1000ms); the instant it completes, `_swap_map()` loads the destination map's JSON, finds the object named `destination_warp` in its object layer, resolves its landing tile (its own `row`/`col`, offset by `distance` tiles in its own `facing` direction — see Warps above) and calls `_load_map()` again with that tile as an explicit spawn override (and the same `facing`) — so the entire map/tileset/object/NPC reload happens in one frame, fully hidden behind solid black, before `"in"` fades the same overlay back out over `cfg.warp_fade_in_ms` (750ms) on the new map. This is safe against an immediate bounce-back even when `distance` is 0 (landing exactly on the destination warp's own tile): `_warp_at` is only ever checked after `Player.try_move()` succeeds, and spawning sets `player.row/col` directly without going through `try_move`, so arriving at a tile never re-fires its warp — only a genuine subsequent step onto it does. `OverworldScene.__init__` no longer inlines all of this map-dependent setup itself; it was pulled out into `_load_map()` (map/tileset/passability/objects/warps/NPCs/spawn/tween-and-timer resets) precisely so a warp mid-game and the initial map boot can share the same code path — pygame resources that don't vary by map (sprite sheet, menu/dialogue assets) stay loaded once in `__init__` instead.

Each map lives in its own folder under `data/maps/` (e.g. `data/maps/hub_fronthouse/`), holding the Tiled JSON export plus the YAML files described next.

### Map Object Sync (`data/maps/populate_yamls.py`)

A dev-only script, never imported by the engine or run at runtime. It reads a map's Tiled object layer and, per object, writes a stub entry keyed by the object's Tiled `id` into either `npcs_<map>.yaml` (type `npc`) or `obj_<map>.yaml` (everything else — `container`, `sign`, `warp`, ...) in that same map's folder. Only `id`/`name`/`type` are synced from Tiled; new entries also get type-specific stub fields to hand-fill (`container`: `contents`/`dialogue`, `sign`: `dialogue`, `npc`: `dialogue`/`event`/`sprite`/`behavior`, `warp`: `destination_map`/`destination_warp`/`facing`/`distance`). `sprite` is a key into `assets/sprites/partysprites.txt`'s NPC frames (e.g. `npc01` — no underscore, matching the `.txt`, not `npc_01`); `behavior` is `"static"` (idles in place) or `"wander"` (roams walkable tiles), defaulting to `"static"` if left blank. `destination_map`/`destination_warp` point a warp at another warp object elsewhere — the latter is a *name*, looked up only within `destination_map`'s own object layer, so it never needs to be globally unique (see Warps above); `facing` is which way the player faces after spawning there, defaulting to south if left blank; `distance` offsets the landing tile that many tiles from this warp's own tile in its `facing` direction, defaulting to 0 (land exactly on it) if left blank. Re-running never overwrites a field you've already filled in — it only touches `id`/`name`/`type` and adds stubs for genuinely new objects, and warns (without deleting) if an object disappears from the map.

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

**Current state:** this ID-keyed sketch (a `logic` ID resolved conditionally through the Event System, below) isn't built yet. What exists today is simpler and inline, mirroring how signs already work: each `npc`-type object's `sprite`/`behavior`/`dialogue` are hand-filled directly on its entry in `npcs_<map>.yaml` (see Map Object Sync above) — `sprite` picks its frames from `assets/sprites/partysprites.txt` (same sheet-slicing mechanism the player uses), `behavior` is `"static"` or `"wander"`, and `dialogue` is a flat page list, same shape as a sign's. `engine/renderer.py` reads all three into a runtime `NPC` per npc-type object.

**Dialogue** — plain pages, keyed by ID, never inline in code:

```yaml
old_man_intro:
  pages:
    - "Welcome to the village."
    - "Stay away from the forest."
```

**Current state:** signs and NPCs both use this same page-list shape today, but inline — a sign's `dialogue` lives directly on its entry in `obj_<map>.yaml`, an NPC's on its entry in `npcs_<map>.yaml` (see Map Object Sync above), rather than in a separate ID-keyed file like the sketch above. `engine/dialogue.py` + `engine/renderer.py` render it as a typed-in, paginated box (see What's Built) — the exact same box and typewriter logic for both. A separate ID-keyed dialogue file resolved conditionally through NPC logic, as sketched above, isn't built yet; see roadmap (Event System).

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

A sign or NPC interaction resolves today as: `Player.adjacent_interactable()` finds whichever's on the faced tile (checking `npcs` before `objects`) → `engine.input.handle_a_button` checks `target.type in ("sign", "npc")` and wraps its dialogue pages to fit the box (via a `wrap_pages` callback into `engine/renderer.py`, which owns the `pygame.font` metrics) → `engine.dialogue.DialogueBox` opens and types them in. Conditional dialogue — picking a page list based on flag state instead of always showing the same one — is planned once the Event System exists: `Player → NPC.interact() → Event System resolves "old_man_logic" → Dialogue System renders the returned page(s)`.

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Content Pipeline (Tiled + YAML)
- [x] Tiled JSON importer (`engine/renderer.py`) — tile layers + tileset `walkable` property → passability grid, GID-based rendering, player-centered clamped camera
- [x] Retired the CSV+tilerules hub loader and the draft YAML `MapData` loader — `engine/map_loader.py` no longer exists
- [x] `container`/`sign` objects placed on the hub's object layer, synced to editable YAML via `data/maps/populate_yamls.py` — see Map Object Sync above
- [x] `npc` objects placed — one so far, `interior_deptstore`'s `npc1` (static, haunted-store dialogue); `npcs_hub_fronthouse.yaml` is still empty and `town_town1` has no object-layer YAML at all yet
- [x] `container`/`sign` objects → engine instantiation — `engine/renderer.py`'s `load_map_objects()` reads the object layer plus `obj_*`/`npcs_*` YAML at runtime, renders each object at its tile, and makes `sign`-type objects interactable (opens the Dialogue System below); containers render but aren't interactable yet
- [x] `npc` object type → engine instantiation — `engine/renderer.py`'s `NPC` dataclass, built per npc-type object: renders from the party sprite sheet (not the tileset), moves per its authored `behavior` (`"static"` idles in place, `"wander"` roams walkable tiles avoiding the player and other NPCs), is impassable to the player, and is interactable exactly like a sign — see What's Built
- [x] `Warp` object type → engine instantiation — `engine/renderer.py`'s `OverworldScene.warps`/`_warp_at`/`_begin_warp`/`_tick_transition`/`_swap_map` (see Warps above): auto-triggers on step-in, fades to black, swaps the loaded map, fades back in on a warp of the same name in `destination_map`. `Trigger`/spawn-point object types (doors/chests/switches that run a scripted action rather than just changing screens) are still nothing — blocked on the Trigger System below
- [ ] Event System — flag dict + condition evaluation (`if`/`else` chains) → dialogue ID or action list
- [x] Dialogue System (`engine/dialogue.py` + `engine/renderer.py`) — paged dialogue box: word-wrapped and paginated to fit the box art, typewriter reveal (hold A/B to speed it up), A advances/closes once the current page's fully revealed, docks to the bottom of the screen or flips to the top (mirrored art + text rect) when the player's in the lower screen half. Wired to `sign`- and `npc`-type objects; the Event System that would pick a dialogue ID conditionally instead of always showing the same page list is still open (see above)
- [ ] Trigger System — executes a referenced script YAML on activation (doors, chests, switches) — distinct from `Warp` (above), which is already wired and doesn't need the Event System since it has no conditional behavior, just a fixed destination
- [ ] NPC logic YAML (`data/npcs/`) — a separate ID-keyed file of conditional dialogue trees (resolved through the Event System), replacing the current placeholder `dialogue`/`ideas` notes files. Distinct from the `sprite`/`behavior`/`dialogue` fields already wired on `npcs_<map>.yaml` (see above) — those aren't going away, this would add flag-conditional page selection on top

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
- [x] `maptest.py` — prompts `map / battle / debug screen?`; `map` boots the real game loop (`engine/renderer.py`'s `run()`) with `OverworldScene` (also `engine/renderer.py`) on the chosen map — not a special reduced path — same input/update/render code the real game runs, just picking which map loads. Player-controlled Melvin, passability collision, camera. `battle`/`debug screen` are stubs — they print a not-yet-implemented message and exit
- [x] Debug map picker — `maptest.py`'s `map` option lists every folder under `data/maps/` (any folder with a matching `<name>.json`) and loads whichever one is chosen via `OverworldScene(map_path=...)`; works for any map with a matching tileset export, not just the hub
- [ ] Hot-swap the loaded map without restarting the process
- [ ] Debug overlay — toggle tile passability grid, NPC index/behavior labels, player tile coords, FPS counter
- [ ] Battle debug screen and dedicated interior/cave debug screen — `maptest.py`'s `battle`/`debug screen` prompt options are stubbed in, no scene behind them yet

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

**Config (`engine/config.py`)** — `config.json` singleton. Pacing (move_ms, screen crossing, interior entry/exit, dialogue typewriter speed, warp fade-out/fade-in), battle limits, display geometry, interior exploration distance.

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

**`engine/renderer.py`** — THE graphical renderer, one file, full stop. It owns: the generic pygame app loop (`run(scene_factory, ...)` — window, clock, event dispatch; constructs the scene *after* opening the window so asset loading like `.convert_alpha()` is safe); Tiled map + tileset JSON loading (parses `hub_fronthouse.json` and the sibling `tiles_town.json`, matched by basename, into GIDs and a `walkable`-derived passability grid); Tiled **object layer** loading (`load_map_objects()` — parses `container`/`sign`/`npc` objects into `MapObject` records and merges in each one's `dialogue`, plus `sprite`/`behavior` for npc-type ones, from `obj_<map>.yaml`/`npcs_<map>.yaml`; converts each object's Tiled pixel position to a tile row/col — bottom-left-anchored for tile-stamp objects with a `gid` like containers/signs, top-left like a plain rectangle for objects with no `gid` like NPCs — and also keeps the un-floored `row_exact`/`col_exact`, which only NPCs use, to render at their exact authored position instead of snapping to the grid); sprite-sheet slicing (`partysprites.txt` → named party/NPC surfaces); menu asset loading (`load_menu_assets()` — `assets/menus/startmenu_*.png`, `save_menu_*.png`, and `dialogue_box.png` plus a pre-flipped copy of it); dialogue font loading (`ModernDOS8x8.ttf` at 16pt, which renders at 8px); and `OverworldScene` itself (player movement, tile-to-tile tweening, the player-centered clamped camera, drawing map objects/NPCs/player, the start-menu/save-confirm/dialogue-box overlays, warp-triggered fade transitions, and `_load_map()` — the map-dependent half of scene setup, factored out of `__init__` so a warp mid-game can reload it the same way the initial boot does). This used to be split across a separate `pygame_viewer/` package (`app.py` + `renderer.py` + `sprites.py`, plus dead code in `tileset.py`) — that split was rejected and the package was deleted; see the architecture note in CLAUDE.md. The object layer's `container`/`sign` objects are placed and rendered (`can1`, `can2`, `container3`, `sign1` on the hub map today); `sign1` is interactable.

**NPCs (`NPC` dataclass in `engine/renderer.py`)** — built per `npc`-type object (one exists today: `interior_deptstore`'s `npc1`). Rendered from the party sprite sheet via `get_npc_frame`/`_NPC_FRAME_NAMES`, the same slicing (`partysprites.txt`) and per-frame lookup the player uses, not the tileset. Two authored behaviors: `"static"` just plays its 2-frame idle animation in place; `"wander"` additionally rolls a coin flip every `_NPC_MOVE_MS` (800ms) tick to take one step in a random cardinal direction, checked against the same walkable grid the player uses plus current occupancy (won't step onto the player or another NPC). Position is tile-discrete exactly like the player (`NPC.row`/`col`, plain ints) with the same renderer-only float tween toward the logical tile on move — except the tween's *initial* rest position isn't `(float(row), float(col))` like the player's always is: it's seeded from `MapObject.row_exact`/`col_exact` (the object's un-floored Tiled pixel placement, in tile units), so an NPC renders exactly where it was placed in Tiled rather than snapped to the grid (`interior_deptstore`'s `npc1` is deliberately placed a few pixels off-grid to exercise this). `NPC.row`/`col` themselves are still always the floored tile and still drive every gameplay concern (wander stepping, collision, occupancy) — only the *drawn* position starts off-grid. A `"wander"` NPC's first step tweens it from that exact spot onto the tile grid like any other move, and it stays grid-aligned from then on; a `"static"` NPC never moves, so it stays at its exact authored pixel position for its whole lifetime — see the note on this in CLAUDE.md's tile-discrete-entity rule, which didn't originally anticipate a visual position that never converges. The player, symmetrically, can't walk onto an NPC's tile — `OverworldScene._passable_with_npcs()` builds a per-step copy of the walkable grid with every NPC's current tile marked blocked, so `engine/player.py` stays headless and never has to know NPCs exist. Facing an NPC and pressing A opens its `dialogue` exactly like a sign (see Dialogue System below) — same `Player.adjacent_interactable()` → `handle_a_button` → `DialogueBox` path, just checking `target.type in ("sign", "npc")` instead of signs only. **Unrelated to `engine/enemy_state.py`** — that module is a separate, still-unwired system for procgen-driven combat enemies and fixed town NPCs (see Enemy System below); nothing in the pygame path calls it, and the `NPC` class here doesn't either.

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
│   │   ├── hub_fronthouse/    # object-layer YAML synced; container/sign objects placed, no npcs
│   │   │   ├── hub_fronthouse.json      # Tiled map export, read by engine/renderer.py
│   │   │   ├── obj_hub_fronthouse.yaml  # container/sign objects — dialogue, contents (hand-edited,
│   │   │   │                           #   read at runtime by engine/renderer.py's load_map_objects())
│   │   │   └── npcs_hub_fronthouse.yaml # npc objects — dialogue, event, sprite, behavior (empty, none placed yet)
│   │   ├── town_town1/        # Tiled map export only, town tileset — no obj_/npcs_ YAML yet
│   │   ├── interior_deptstore/  # own tileset (tiles_interior_deptstore.*); obj_/npcs_ YAML synced
│   │   │   ├── interior_deptstore.json      # Tiled map export
│   │   │   ├── obj_interior_deptstore.yaml  # empty — no container/sign objects placed yet
│   │   │   └── npcs_interior_deptstore.yaml # one npc: npc1, static, sprite npc01, haunted-store dialogue
│   │   └── interior_wc_deptstore/  # own tileset; one warp object placed (wc_warp1), destination fields still blank
│   │       ├── interior_wc_deptstore.json
│   │       └── obj_interior_wc_deptstore.yaml
│   └── party/               # character sheet JSONs
├── story/                   # narrative/world notes
├── tests/                   # test suite
├── maptest.py               # debug entry point — prompts map/battle/debug screen, boots the real game loop
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
python maptest.py           # prompts: map / battle / debug screen (battle, debug screen are stubs)
                             # `map` lists every folder under data/maps/ — pick one to open it
                             # in the real game loop
```

All dependencies (pygame, Pillow, PyYAML) are in `.venv/`.
