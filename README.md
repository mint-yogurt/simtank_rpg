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

Layers composite in **real, authored order** — whatever order they're listed in Tiled, tile layers and the Object Layer alike. `engine/renderer.py`'s `load_tiled_map()` walks the JSON's `layers` list once and keeps every tile layer plus a marker for the Object Layer, in that exact sequence (`TiledLayer.kind`, `"tile"` or `"objects"`); `OverworldScene.draw()` then walks that same list, blitting each tile layer (`_draw_tile_layer`) or the whole containers/signs/NPCs/enemies/player bundle (`_draw_entities`) as it reaches each one. A tile layer placed *before* the Object Layer in Tiled draws under the player/objects; one placed *after* draws over them (roofs, tree canopies, an "empty" tile a container's full-tile art should hide until it's opened — see Items, NPCs, Dialogue, Events below). Nothing is hardcoded to a specific layer name or index — `hub_fronthouse`/`town_town1`/etc. happen to author their Object Layer last (`Tile Layer 1, Tile Layer 2, Object Layer 1`), while `interior_deptstore`/`interior_wc_deptstore` author it in the *middle* (`Tile Layer 1, Object Layer 1, Tile Layer 2[, Tile Layer 3]`) — both composite correctly from the same code path.

Object layer entries are references only, e.g.:

```
NPC     id: old_man        sprite: old_man        logic: old_man_logic
Trigger script: castle_gate
Warp    destination_map: castle_entrance   spawn: south_gate
```

The engine loads the map, walks the object layer, and instantiates the right Python object per `type`. No gameplay logic is ever embedded in the map file itself.

**Current state:** `data/maps/` holds several map folders — `hub_fronthouse` and `town_town1` (both on the town tileset), `interior_deptstore` and `interior_wc_deptstore` (their own tileset) — `engine/renderer.py`'s `OverworldScene` takes a `map_path` and reads that map's tile layers plus its sibling tileset property export (matched by basename, e.g. `assets/tiles/tiles_town.json` or `assets/tiles/tiles_interior_deptstore.json`) to resolve per-tile passability from a `walkable` bool property, and renders it with a camera that follows the player, clamped to map edges. `maptest.py` prompts for which map to load — see Dev Setup below. The old hand-rolled CSV+tilerules loader and the draft YAML `MapData` loader are gone entirely — there is no `engine/map_loader.py` anymore. Object-layer `container`/`sign` placement (see below) is synced to YAML on the hub map, and `interior_deptstore` now has one hand-placed `npc` object (`npc1`) synced to its own `obj_`/`npcs_` YAML too; `town_town1` still has no `obj_`/`npcs_` YAML at all. Warps are wired up (see below) — `interior_wc_deptstore` has one placed (`wc_warp1`), not yet pointed at a destination.

The object layer is now actually read at runtime: `engine/renderer.py`'s `load_map_objects()` parses it into `MapObject` records (`id`/`name`/`type`/`row`/`col`/`gid`), merging in each object's `dialogue` from `obj_<map>.yaml`/`npcs_<map>.yaml` — `npc`-type objects also merge `sprite`/`behavior` this way (converting Tiled's pixel position to a tile row/col along the way, plus an un-floored `row_exact`/`col_exact` pair kept alongside it — see below). That conversion branches on whether the object has a `gid`: tile-stamp objects (containers/signs, placed with Tiled's tile tool) anchor at their **bottom-left** corner, so their height is subtracted first; objects with no `gid` (NPCs, which don't need a tileset graphic — placed with the rectangle/point tool instead) anchor **top-left** like any plain Tiled rectangle and must NOT get that correction, or they land one tile off from where Tiled shows them (a real bug hit and fixed while wiring up `interior_deptstore`'s NPC). Containers/signs render at their tile using their own `gid`, snapped to the grid via `row`/`col`, same tileset as the map; `npc`-type objects are pulled into a separate `NPC` list instead, rendered from the party sprite sheet, and — unlike containers/signs — placed at their *exact* Tiled pixel position rather than snapped to the tile grid (`row_exact`/`col_exact`; see NPC behaviors under What's Built for how that reconciles with NPCs otherwise being tile-discrete). `type == "sign"`, `type == "npc"`, and `type == "container"` are all interactable — face one and press A to open the Dialogue System (see below) on its `dialogue` pages; a container also grants its `contents` item (if any) and, either way, stops drawing its own `gid` once opened (see Items, NPCs, Dialogue, Events below). `type == "warp"` objects are neither rendered nor A-press interactable — they're invisible, one-tile trigger zones parsed into their own `OverworldScene.warps` list (not `objects`), and instead of pressing A the player just has to walk onto one (see Warps below).

**Warps.** A `warp`-type object is both a trigger tile (stepping onto it fires a map transition) and a possible landing spot (another map's warp can target it). Each warp is authored with four more YAML-only fields beyond `dialogue`/`sprite`/`behavior` (see Map Object Sync below): `destination_map` (the destination map's folder/stem name) and `destination_warp` (the *name* of the warp object on that destination map to land on). That name lookup is always scoped to `destination_map` first, so warp names only need to be unique within one map's object layer, never across the whole game — reuse conventional names like `door_north`/`entry`/`exit` everywhere. The other two, `facing` and `distance`, do double duty as *this* warp's own landing spot whenever some other warp's `destination_warp` points here: `facing` (default south if left blank) is both which way the player faces after spawning and which direction `distance` (default 0) offsets the landing tile from this warp's own `row`/`col` — e.g. `facing: S, distance: 1` lands one tile south of the warp, so the player visibly steps out of a doorway instead of standing on it; `distance: 0` or unset lands exactly on the warp's own tile. Warps are one-way and hand-paired by the map author: a two-way door is two separate warp objects, one on each map, each pointing at the other.

Warps also change where a scene boots into a map fresh (not via an in-game warp trip) — see `maptest.py`'s map picker. `OverworldScene._load_map()` only falls back to the old arbitrary `tiled_spawn_point()` heuristic when the map has *no* warp objects at all; if it has any, it spawns the player on a random one (`_warp_landing`, the same row/col-plus-offset math `_swap_map` uses), so debugging a map drops you at one of its own authored entrances instead of an arbitrary tile.

`OverworldScene._update_player_movement()` checks the player's rounded current tile (`Player.current_tile()`) against `self.warps` (`_warp_at`) every frame movement is processed, but only acts when that rounded tile differs from `_player_last_tile` — player position is continuous now (see What's Built → Engine), not tile-discrete, so there's no discrete "step landed" event to hang the check on; this edge-trigger reproduces the same one-shot-per-entry behavior from any approach angle, including diagonal. A hit calls `_begin_warp()`, which — if both `destination_map` and `destination_warp` are filled in (an unconfigured warp, e.g. a freshly-placed stub, just prints a warning and does nothing) — starts a scene-level transition state machine (`_transition_phase`: `None` → `"out"` → `"in"` → `None`), driven from `OverworldScene.update()` exactly like the dialogue box and start menu are: a full gameplay pause while active. `"out"` fades a solid black overlay in over `cfg.warp_fade_out_ms` (1000ms); the instant it completes, `_swap_map()` loads the destination map's JSON, finds the object named `destination_warp` in its object layer, resolves its landing tile (its own `row`/`col`, offset by `distance` tiles in its own `facing` direction — see Warps above) and calls `_load_map()` again with that tile as an explicit spawn override (and the same `facing`) — so the entire map/tileset/object/NPC reload happens in one frame, fully hidden behind solid black, before `"in"` fades the same overlay back out over `cfg.warp_fade_in_ms` (750ms) on the new map. This is safe against an immediate bounce-back even when `distance` is 0 (landing exactly on the destination warp's own tile): `_load_map()` seeds `_player_last_tile` directly from the spawn tile, so arriving there never reads as "just entered" and re-fires it. `OverworldScene.__init__` no longer inlines all of this map-dependent setup itself; it was pulled out into `_load_map()` (map/tileset/passability/objects/warps/NPCs/spawn/timer resets) precisely so a warp mid-game and the initial map boot can share the same code path — pygame resources that don't vary by map (sprite sheet, menu/dialogue assets) stay loaded once in `__init__` instead.

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

**Current state:** the `logic` half of this sketch (conditional dialogue resolved through the Event System) isn't built yet, but the master list itself now is — `data/npcs/npc.yaml`, loaded by `engine.npc.load_npc_defs()`/`load_npc_sprite_specs()` (`engine/npc.py`, headless, no pygame/Tiled — same split as `engine/enemy.py`). It's two sections. `sprites:` names each NPC sprite *strip* living under `assets/sprites/npcs/` — one PNG per sprite, same convention as `data/enemy/enemies.yaml`'s `sprite` field, **not** the old shared `party_sprites.png` + `partysprites.txt` mechanism, which is now deprecated for NPCs (nothing reads that file for NPC purposes anymore — its old `npc01`-`npc08` rows were deleted). Each entry is `{file: <stem>, colors: [...]}` — `file` a bare filename stem, `colors` the placeholder hex colors actually baked into that PNG (see Recoloring below). A strip's width picks how it animates: 32×16 (2 frames) is a facing-less south-only idle loop, exactly the old `npc01` look; 128×16 (8 frames) is a full walk cycle in the party sheet's own `S1,S2,N1,N2,W1,W2,E1,E2` frame order, and *does* respond to facing — a `"static"` NPC using one faces whatever its `facing:` field says (`N`/`S`/`E`/`W`, defaults `S`), a `"wander"` one turns to face its current movement direction. `npcs:` maps id → `sprite`/`behavior`/`facing`/`colors`/`dialogue`. Any `npc`-type object's entry in `npcs_<map>.yaml` can set `npc_id` to reference one of these — its own `sprite`/`behavior`/`dialogue` fields become optional overrides on top of it: filled in, they win for that one placement; left blank (`null`/`[]`), they fall back to the `npc.yaml` definition. `npc_id` is additive, not required — an entry with no `npc_id` behaves exactly as before this existed, fully inline (and never recolored — see below). Resolved in `OverworldScene._load_map`'s NPC-construction loop, not `load_map_objects()` (which still just merges raw per-map YAML, unmerged against any def).

**Recoloring.** An `npcs:` entry's own `colors`, if set, replaces its sprite's placeholder colors for that NPC specifically, position-matched against the sprite's own `colors` list (sprite `colors[0]` → this NPC's `colors[0]`, and so on) — this is what lets two different NPCs share one sprite strip but read as visually distinct characters. Recoloring happens once per unique NPC def when the game boots (`OverworldScene.__init__` builds `self.npc_frames_by_id`, a precomputed recolored frame set per `npc_id`), not per frame drawn — `engine.renderer.recolor_surface()` does the actual pixel remap (a `pygame.surfarray`/numpy RGB mask-and-swap, alpha untouched). An NPC with no `npc_id` at all always draws its sprite's raw, unrecolored frames straight from `self.npc_sprites`. **Hex values must be quoted in `npc.yaml`** (`"000000"`, not `000000`) — YAML's implicit typing reads an unquoted all-digit scalar as a number, not text, and for some digit patterns not even the *right* number: `000000` unquoted parses as the integer `0`, `001100` unquoted parses as *octal* `001100` = decimal `576` — both silently wrong, no YAML error at all. `engine.npc`'s loaders validate every `colors` entry is actually a string and raise loudly if one isn't, rather than risk drawing a silently-wrong color.

**Dialogue** — plain pages, keyed by ID, never inline in code:

```yaml
old_man_intro:
  pages:
    - "Welcome to the village."
    - "Stay away from the forest."
```

**Current state:** signs and NPCs both use this same page-list shape today, but inline — a sign's `dialogue` lives directly on its entry in `obj_<map>.yaml`, an NPC's on its entry in `npcs_<map>.yaml` (see Map Object Sync above), rather than in a separate ID-keyed file like the sketch above. `engine/dialogue.py` + `engine/renderer.py` render it as a typed-in, paginated box (see What's Built) — the exact same box and typewriter logic for both. A separate ID-keyed dialogue file resolved conditionally through NPC logic, as sketched above, isn't built yet; see roadmap (Event System).

**Event System — conditions and actions.** One flag primitive, `GameState.flags` (`engine/game_state.py`), is the only thing a condition ever checks — deliberately not two (flags *and* a live inventory lookup): "has the castle key" is modeled as a flag set the moment the key's granted (a `set_flag` action, below, paired with `give_item`), not `Inventory.has()`, so the condition side never needs to know `Inventory` exists.

**Condition** — a single flag check:

```yaml
if: boss1_dead        # true only if the flag's set
if_not: gate_open      # true only if the flag's NOT set
```

**Action** — an ordered list of `{action: params}` steps, run in sequence when something's triggered (an NPC talked to, a door opened, a switch pulled):

```yaml
then:
  - set_flag: gate_open
  - give_item: rusty_key
  - dialogue: gate_now_open
```

Built now (containers, see Current state below) or next in line: `set_flag`, `clear_flag`, `give_item`, `remove_item`, `dialogue` (open a page list by id). Reserved for later, as more entries in this *same* list shape — not a separate system, so nothing here needs reworking to add them: `pan_camera` (move the camera to a point and back for a scripted reveal), `force_move` (step the player N tiles in a direction, ignoring input), `wait` (pause the action list for a beat). Camera pans and forced movement are explicitly scoped this way, not designed yet.

**NPC logic** — an NPC's dialogue picks whichever entry's condition matches, top-to-bottom, first match wins (an entry with no `if`/`if_not` always matches, so it goes last as the default):

```yaml
old_man_logic:
  - if: boss1_dead
    dialogue: old_man_after_boss
  - dialogue: old_man_intro
```

**Triggers** — doors, chests, signs, switches, map transitions — reference a script ID; the script (also YAML) gates on a condition, with a fallback:

```yaml
castle_gate:
  if: has_castle_key
  then:
    - set_flag: gate_open
  else:
    - dialogue: gate_locked
```

**Current state:** the flag store this all sits on is real and save-able — `GameState.flags`/`persistent_id` — and so is real, order-preserving layer compositing (see Maps above), which is what lets a triggered object's tile disappear into whatever's painted underneath it. What's actually built *on* that foundation is still one concrete case, not the general `if`/`then`/`else` evaluator sketched above: containers. Every container (`type == "container"`) gets its flag for free via `persistent_id(map_name, object_name)` — no registry, no per-object declaration. Opening one — loot or not — sets that flag and is inert afterward, one open, ever; a container with `contents` set (an item id from `data/items/items.yaml`) also grants it, appending a synthesized `"Received {item name}."` page. That same flag makes `OverworldScene._draw_entities` (`engine/renderer.py`) stop drawing the container's `gid`, revealing whatever's on the tile layer beneath it — the actual mechanic behind "the trash can empties out once you loot it": paint the empty state on a tile layer, stamp the full state as the container object on top, anywhere in the layer stack now that compositing is order-preserving (see Maps above). See `engine.input._open_container` for the logic.

Still not built: the general `if`/`if_not` condition check and `then`/`else` action-list executor themselves (nothing resolves a script id into a decision yet), conditional dialogue for signs/NPCs (`GameState` can already answer "is this flag set," nothing reads it for page selection yet), locked warps (a `requires`-style gate on `type == "warp"`), and the `pan_camera`/`force_move`/`wait` action kinds. Two more pieces are scoped but not designed at all yet: **animated tiles** (frame-cycling GIDs — how that interacts with the current one-surface-per-GID tile lookup isn't decided) and the **master NPC YAML** described under NPCs above.

A sign or NPC interaction resolves today as: `Player.adjacent_interactable()` finds whichever's on the faced tile (checking `npcs` before `objects`) → `engine.input.handle_a_button` checks `target.type in ("sign", "npc")` and wraps its dialogue pages to fit the box (via a `wrap_pages` callback into `engine/renderer.py`, which owns the `pygame.font` metrics) → `engine.dialogue.DialogueBox` opens and types them in. A container interaction goes through the same `handle_a_button`, `target.type == "container"` branch instead, via `_open_container` (see above) for the pages to show. Conditional dialogue — picking a page list based on flag state instead of always showing the same one — is planned once the general Event System exists: `Player → NPC.interact() → Event System resolves "old_man_logic" → Dialogue System renders the returned page(s)`.

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Content Pipeline (Tiled + YAML)
- [x] Tiled JSON importer (`engine/renderer.py`) — tile layers + tileset `walkable` property → passability grid, GID-based rendering, player-centered clamped camera
- [x] Retired the CSV+tilerules hub loader and the draft YAML `MapData` loader — `engine/map_loader.py` no longer exists
- [x] `container`/`sign` objects placed on the hub's object layer, synced to editable YAML via `data/maps/populate_yamls.py` — see Map Object Sync above
- [x] `npc` objects placed — one so far, `interior_deptstore`'s `npc1` (static, haunted-store dialogue); `npcs_hub_fronthouse.yaml` is still empty and `town_town1` has no object-layer YAML at all yet
- [x] `container`/`sign` objects → engine instantiation — `engine/renderer.py`'s `load_map_objects()` reads the object layer plus `obj_*`/`npcs_*` YAML at runtime, renders each object at its tile, and makes `sign`- and `container`-type objects interactable (opens the Dialogue System below; containers also grant their `contents` item — see Global Game State below)
- [x] `npc` object type → engine instantiation — `engine/renderer.py`'s `NPC` dataclass, built per npc-type object: renders from the party sprite sheet (not the tileset), moves per its authored `behavior` (`"static"` idles in place, `"wander"` roams walkable tiles avoiding the player and other NPCs), is impassable to the player, and is interactable exactly like a sign — see What's Built
- [x] `Warp` object type → engine instantiation — `engine/renderer.py`'s `OverworldScene.warps`/`_warp_at`/`_begin_warp`/`_tick_transition`/`_swap_map` (see Warps above): auto-triggers on step-in, fades to black, swaps the loaded map, fades back in on a warp of the same name in `destination_map`. `Trigger`/spawn-point object types (doors/chests/switches that run a scripted action rather than just changing screens) are still nothing — blocked on the Trigger System below
- [x] Global game state / flags (`engine/game_state.py`'s `GameState`) — the flat flag dict + freeform `variables` dict the Event System sketch below assumes, real and save-able. `persistent_id(map_name, object_name)` gives every object on every map a stable flag key with nothing to declare or sync — see Items, NPCs, Dialogue, Events above
- [x] Order-preserving layer compositing — `engine/renderer.py`'s `load_tiled_map()`/`OverworldScene.draw()` walk Tiled's real layer order (tile layers + the Object Layer) instead of the old hardcoded "first tile layer below, rest above" rule; any layer arrangement composites correctly, including maps that author their Object Layer mid-stack (`interior_deptstore`, `interior_wc_deptstore`) — see Maps above
- [x] Container → inventory wiring + open/closed visual state — `engine.input._open_container`: every container sets its flag the first time it's opened, loot or not (single-use, one open, ever); one with `contents` set (an item id) also grants it to the shared `Inventory`, appending a `"Received {name}."` page. That same flag makes `OverworldScene._draw_entities` stop drawing the container's `gid` once opened, revealing whatever's painted on the tile layer beneath it — see Items, NPCs, Dialogue, Events above. Still a narrow, hardcoded case of "trigger," not the general Trigger System below
- [ ] Event System — the general `if`/`if_not` condition check and `then`/`else` action-list executor (`set_flag`/`clear_flag`/`give_item`/`remove_item`/`dialogue` now, `pan_camera`/`force_move`/`wait` reserved) sketched under Items, NPCs, Dialogue, Events above. The flags themselves and the container case built on top of them already exist (see above) — this is the "resolve a script ID into a decision" piece, still unbuilt
- [x] Dialogue System (`engine/dialogue.py` + `engine/renderer.py`) — paged dialogue box: word-wrapped and paginated to fit the box art, typewriter reveal (hold A/B to speed it up), A advances/closes once the current page's fully revealed, docks to the bottom of the screen or flips to the top (mirrored art + text rect) when the player's in the lower screen half. Wired to `sign`-, `npc`-, and `container`-type objects; the Event System that would pick a dialogue ID conditionally instead of always showing the same page list is still open (see above)
- [ ] Trigger System — executes a referenced script YAML on activation (doors, switches, more elaborate chest logic than a flat item grant) — distinct from `Warp` (above, already wired, no conditional behavior) and from the container item-grant wiring (above, a fixed narrow case, not a general script). Locked warps (gate a warp on a flag, show a "locked" message instead of transitioning) fall under this too, still unbuilt
- [ ] NPC logic YAML (`data/npcs/`) — a separate ID-keyed file of conditional dialogue trees (resolved through the Event System), replacing the current placeholder `dialogue`/`ideas` notes files. Distinct from the `sprite`/`behavior`/`dialogue` fields already wired on `npcs_<map>.yaml` (see above) — those aren't going away, this would add flag-conditional page selection on top
- [x] Master NPC YAML (`data/npcs/npc.yaml`) — `engine/npc.py`'s `NpcDef`/`NpcSpriteSpec`/`load_npc_defs()`/`load_npc_sprite_specs()` (headless, mirrors `engine/enemy.py`); a `sprites:` section names each NPC sprite *strip* under `assets/sprites/npcs/` (replacing the old shared `party_sprites.png`/`partysprites.txt` mechanism for NPCs — deprecated, its `npc01`-`npc08` rows deleted), an `npcs:` section holds shared `sprite`/`behavior`/`facing`/`colors`/`dialogue` defs any placement's `npc_id` can reference and override — see Items, NPCs, Dialogue, Events above. `logic` (conditional dialogue via the Event System) still isn't part of it — no consumer exists yet
- [x] NPC facing + per-NPC sprite recoloring — an 8-frame (128×16) NPC sprite strip supports real facing (`static` NPCs via an authored `facing:` field, `wander` ones by turning to face their movement), unlike the old facing-less 2-frame-only mechanism; `engine.renderer.recolor_surface()` remaps a sprite's placeholder hex colors to a per-NPC replacement palette once at boot (`OverworldScene.npc_frames_by_id`), so multiple NPCs can share one sprite strip and still look distinct — see Items, NPCs, Dialogue, Events above
- [ ] Animated tiles — scoped, not designed yet: how frame-cycling GIDs would interact with the current one-surface-per-GID tile lookup (`get_tile_by_gid`) isn't decided
- [x] `enemy`/`spawner` object types → engine instantiation — `engine/renderer.py` reads them via `load_map_objects()` same as every other object type; `engine/enemy.py` (new, headless) owns `EnemyDef`/`Enemy`, the master list loader, spawn resolution, and continuous sub-tile movement — see Enemy System below. Placement/spawning/sprites/movement only, not battle — enemies don't collide with the player yet, no battle trigger exists

### Input & Core Game Loop
- [x] Keyboard input: arrow keys move the player, held-key resolution (one active direction per axis, "last pressed wins" within an axis — so holding two cardinal keys moves diagonally, deliberately, rather than as an artifact of key-repeat timing) lives in `engine/input.py`'s `HeldDirectionInput`
- [x] Z (B button), X (A button), Enter (START) — wired to the start menu (START opens/closes it, B closes it, A confirms the highlighted option — stub, see below) and to dialogue (A opens a dialogue box by facing an interactable sign, advances/closes it once the current page's fully typed in; holding A or B speeds up the typewriter reveal)
- [x] Player character entity (`engine/player.py`) — `Player` with `PlayerState` machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE), continuous free movement (`move()`, 8-directional, collision via `engine/movement.py`'s `step_continuous` — same primitive `engine/enemy.py` uses), `adjacent_interactable()`, serialise/deserialise
- [x] Config-driven movement speed — `player_move_ms` in `config.json` (ms to cross one tile at normal speed, converted to a tiles/sec speed for continuous movement), read via the `engine.config.cfg` singleton; `dialogue_char_ms`/`dialogue_char_fast_ms` follow the same pattern for the dialogue box's typewriter reveal speed (normal vs. holding A/B)
- [ ] Bluetooth / USB controller support (pygame joystick API)
- [ ] Title screen
- [x] Start menu shell (`engine/menu.py`) — START pauses gameplay and opens a 4-option vertical cursor menu (INVENTORY/PARTY/SETTINGS/SAVE), wraps top-to-bottom, closes on B or START; drawn by `engine/renderer.py` from `assets/menus/`
- [x] Save-confirm overlay (`engine/menu.py`'s `SaveMenu`) — SAVE on the start menu opens a YES/NO overlay on top of it (defaults to NO, only E/W move the cursor); B or confirming NO closes it back to the start menu
- [x] Inventory screen (`engine/menu.py`'s `InventoryMenu`) — INVENTORY on the start menu opens a full-black, font-only alpha screen (no art assets yet): E/W pages through the four categories from `engine/inventory.py` (ITEMS/WEAPONS/EQUIPMENT/KEY ITEMS, wraps), N/S scrolls the current category's owned-item list (clamps at either end, resets to the top on a category switch), and the right half shows the highlighted item's icon placeholder/name/description/effect. View-only for now — no USE/EQUIP action, since there's no party-select UI or equip screen to hook into yet. See `engine/inventory.py`'s `Inventory` for what actually populates the list (currently nothing — no pickup system exists yet either).
- [ ] Start menu sub-screens — PARTY/SETTINGS still just highlight, no screen behind them yet
- [x] Save through menu (`engine/save.py`) — confirming YES on the save-confirm overlay writes `Player`/`Inventory`/`GameState` + the current map name to the active numbered slot (`engine.input.handle_a_button`, since `SaveMenu` itself does no file I/O, same as every other menu class), then closes back to the start menu, same as NO. v1 scope: player position/facing, the shared inventory, and flags/variables — not party member HP/MP/XP, since nothing at runtime mutates those yet (battle's still broken/unwired — see "Save" under What's Built > Engine)
- [ ] Load through menu — loading mid-game (as opposed to picking a slot at boot via `maptest.py`, which is built — see Debug & Testing Screens) isn't wired to any in-game menu option yet

### Debug & Testing Screens
- [x] `maptest.py` — prompts `map / battle / debug screen?`; `map` boots the real game loop (`engine/renderer.py`'s `run()`) with `OverworldScene` (also `engine/renderer.py`) on the chosen map — not a special reduced path — same input/update/render code the real game runs, just picking which map loads. Player-controlled Melvin, passability collision, camera. `battles` prompts for an enemy id from `data/enemy/enemies.yaml` and boots `engine.renderer.BattleScene` against it through the same `run()` loop — see Battle below. `debug screen` is still a stub — prints a not-yet-implemented message and exits
- [x] Debug map picker — `maptest.py`'s `map` option lists every folder under `data/maps/` (any folder with a matching `<name>.json`) and loads whichever one is chosen via `OverworldScene(map_path=...)`; works for any map with a matching tileset export, not just the hub; only runs for a fresh/new-game boot — loading an existing save slot uses that slot's own remembered map instead (see below)
- [x] Debug save-slot picker — `maptest.py`'s `map` option first prompts for one of `engine.save.SLOT_COUNT` (3) fixed numbered slots: an in-use slot loads straight into its saved map with `Player`/`Inventory`/`GameState` restored (spawning at the saved position, not a random warp — see `OverworldScene.__init__`'s `_loaded_save` branch), an empty slot falls through to the map picker above for a fresh start, and `c<N>` clears slot N (`engine.save.clear_slot`) and re-prompts — lets different chapters/scenarios be tested by slot without hand-editing save files
- [ ] Hot-swap the loaded map without restarting the process
- [ ] Debug overlay — toggle tile passability grid, NPC index/behavior labels, player tile coords, FPS counter
- [x] Battle debug screen — `maptest.py`'s `battles` prompt option, `engine.renderer.BattleScene` (see Battle below)
- [ ] Dedicated interior/cave debug screen — `maptest.py`'s `debug screen` prompt option is still stubbed in, no scene behind it yet

### Items & Abilities
- [x] Item YAML (`data/items/items.yaml`) — consumables, weapons, armour, key items; `ideas` file for brainstorming alongside
- [x] Item YAML → engine loader + inventory data (`engine/inventory.py`'s `load_item_defs()`/`Inventory`) and the inventory screen (`engine/menu.py`'s `InventoryMenu`, see above)
- [ ] Pickup system (nothing currently adds items to `Inventory` — it starts and stays empty), equip slots, stat effects actually applying in battle, USE action on the inventory screen
- [ ] Special abilities YAML (`data/abilities/`) — name, MP cost, effect, cooldown, flavor

### World & Content
- [ ] Rethink procgen scope — decide what stays procedural vs. hand-authored in Tiled (story, scripted towns/dungeons, procgen as supplement not spine)
- [ ] Procgen output → Tiled-compatible JSON, so generated and hand-authored maps share one loader
- [ ] Updated and expanded tilesets
- [ ] Items in the world — pickup system to actually populate `Inventory`; equip slots + stat effects (see Items & Abilities above for what's already built)
- [ ] Named locations — towns, dungeons, overworld landmarks

### Visuals & UI
- [x] `engine/battle.py` decoupled from the (now-deleted) `llm` package — rewritten as a headless `Fighter`/`BattleState` combat resolver (`engine/combat.py`'s hit/crit/parry/damage math merged in, that file is now deleted) plus a graphical debug battle screen, `engine.renderer.BattleScene` — see Battle below and Debug & Testing Screens
- [ ] Battle screen overhaul — real art (currently plain black textboxes + `procgen/visualizer.py` backgrounds, no dedicated battle tileset/bitmap font yet)
- [ ] Player-driven battle action menu — `BattleScene`'s ATTACK/ITEM/SPECIAL/RUN row is drawn but not interactive yet (auto-attack only); wiring it to `engine.input`/`engine.menu` is the next pass. ITEM and SPECIAL's exact in-battle scope/spec isn't decided — ask before implementing either
- [ ] Full party in battle (currently MELVIN-only, 1v1)
- [ ] Tile-swipe battle transition
- [ ] Party status panel (HP, MP, level, XP per member) — currently just MELVIN's line in `BattleScene`'s bottom textbox
- [ ] Battle-speed config (separate from movement speed) — currently a fixed `_BATTLE_HOLD_MS` in `engine/renderer.py`

---

## What's Built

### Engine

Most of `engine/` is headless and deterministic. The one exception is `engine/renderer.py` — see Scenes below.

**Player (`engine/player.py`)** — `Player` dataclass with a `PlayerState` state machine (IDLE/WALKING/INTERACTING/IN_DIALOGUE/IN_MENU/IN_BATTLE). Position is continuous (`row`/`col` floats, same convention as `engine.enemy.Enemy` — the value *is* the position, not a renderer-side tween), moved via `move(dt_ms, vertical, horizontal, speed, passable)`: up to one direction per axis, resolved against `passable` through `engine/movement.py`'s `step_continuous` (one call per active axis, distance normalized by `1/sqrt(2)` when both are active so diagonal speed matches cardinal speed) — axis-independent resolution is what gives wall-sliding for free. No diagonal sprites exist, so facing is vertical-dominant: any active N/S component shows that sprite even while also moving E/W. `current_tile()` rounds the continuous position to the nearest tile for grid-keyed lookups (interactions, warps, NPC occupancy); `facing_tile()`/`adjacent_interactable()` are built on top of it. `Player` always turns to face a wall it walks into, blocked or not, same as before.

**Input (`engine/input.py`)** — `HeldDirectionInput` tracks held directions per axis and resolves up to one active direction per axis each frame (vertical N/S, horizontal E/W — "last pressed wins" within an axis, but both axes can be active at once, giving real 8-directional movement) via `tick()`, which returns a `(vertical, horizontal)` pair for `engine.renderer` to feed into `Player.move()`. A freshly-pressed direction that differs from the player's current facing, pressed while genuinely at rest, is held in a `_pending` set — excluded from what `tick()` reports as active — until a short shared cooldown (`_TURN_COOLDOWN_MS`, 75ms) expires: a brief tap turns the player to face a new way without moving, holding past the cooldown commits to walking. Already moving, a new direction (including one that starts a diagonal) is active immediately, no added delay. Also owns discrete button routing: `handle_start_button`/`handle_b_button`/`handle_a_button`/`handle_menu_direction` decide what a START/B/A press or a direction press means given the player's current state and which of `engine/menu.py`'s three overlays is "on top" (`SaveMenu`/`InventoryMenu`, when either is open, always get input first over `StartMenu` underneath — e.g. arrow keys drive whichever's cursor instead of player movement while `PlayerState.IN_MENU`; only one of the two is ever open at once, both only ever opening from `StartMenu`). `handle_a_button` also drives `engine/dialogue.py`'s `DialogueBox`: with no menu open, A advances/closes an already-open dialogue box (a no-op while the current page is still typing in), or — if the player's idle and facing an interactable `sign`/`npc`/`container` — opens one via `Player.adjacent_interactable()` (a container's dialogue comes from `_open_container`, which also grants its item and sets its flag — see Items, NPCs, Dialogue, Events above). On the save-confirm overlay, YES calls `engine.save.save_to_slot()` with the scene's `Player`/`Inventory`/`GameState` before closing back to the start menu. It takes a `wrap_pages` callback so the actual pixel-width word-wrapping (which needs `pygame.font` metrics) stays in `engine/renderer.py`. Pure logic, no pygame dependency — the renderer owns raw key→direction/button mapping and feeds abstract strings in.

**Game state (`engine/game_state.py`)** — `GameState`: two flat dicts, `flags` (bool) and `variables` (anything JSON-serialisable, e.g. currency, an entered player name). `flag()`/`set_flag()`/`get_var()`/`set_var()`, `to_dict`/`from_dict`. `persistent_id(map_name, object_name)` is the module-level helper that turns any map object into a stable flag key (`"hub_fronthouse:can1"`) — see Items, NPCs, Dialogue, Events above for how containers use it and why there's no separate registry to keep in sync.

**Save (`engine/save.py`)** — `save_to_slot()`/`load_from_slot()`/`clear_slot()`/`slot_exists()`, JSON files under `saves/` (gitignored — player run state, not source content), numbered `slot1.json` through `slot{SLOT_COUNT}.json` (3 slots). One file bundles `Player.to_dict()`, `Inventory.to_dict()`, `GameState.to_dict()`, and the current map's name. No pygame; `maptest.py`'s debug save-slot picker and `engine.input.handle_a_button`'s SAVE handling are the only two callers today — there's no in-game load option yet (see roadmap).

**Menu (`engine/menu.py`)** — `StartMenu`: cursor state for the pause-menu (`is_open`, `selected`, four options — INVENTORY/PARTY/SETTINGS/SAVE). Vertical list, wraps top-to-bottom. `SaveMenu`: a YES/NO confirm overlay opened from SAVE and drawn on top of `StartMenu`; horizontal pair (only E/W move the cursor), defaults to NO. `InventoryMenu`: the inventory-screen overlay opened from INVENTORY, same tier as `SaveMenu` (only one of the two is ever open at once). Tracks `category` (index into `engine.inventory.CATEGORIES`) and `selected` (scroll index into that category's item list); E/W switches category and wraps, resetting `selected` to 0 — this class holds no item data itself, so it can't clamp into the new category's length, only reset to the top; N/S scrolls and *clamps* at either end instead of wrapping, the one cursor in this module that doesn't wrap, since a scrollable list reads as stuck if it loops but a fixed 4-option row doesn't. `move_cursor()` takes the current category's item count as a parameter for exactly this clamping — the caller (`engine/renderer.py`, which owns the scene's `Inventory` + item defs) computes it. None of the three menus read input themselves — all three only expose `open()`/`close()`/`move_cursor()`/`confirm()` (`InventoryMenu` has no `confirm()` — it's view-only, no sub-action on A yet); `engine/input.py` decides when to call them, `engine/renderer.py` decides how they look. `StartMenu.confirm()` is a stub for PARTY/SETTINGS; SAVE opens `SaveMenu`, INVENTORY opens `InventoryMenu`. `SaveMenu.confirm()` itself does no file I/O (same as every menu class here staying pygame/IO-free) — `engine.input.handle_a_button` is what calls `engine.save.save_to_slot()` on YES.

**Dialogue (`engine/dialogue.py`)** — `DialogueBox`: pure state for a paged, typed-in text overlay opened by facing an interactable sign and pressing A. Tracks `is_open`, the current `pages` list (pre-wrapped to fit the box's text area — see Scenes below), `page_index`, and a `chars_shown`/`tick(dt_ms, ms_per_char)` typewriter reveal. `advance()` (an A press) only turns the page or closes the box once the current page's fully revealed — otherwise it's a no-op, so the player always gets a beat to read before moving on. Same split as `engine/menu.py`: no pygame, no drawing; `engine/renderer.py` feeds it elapsed time each frame and decides how it looks.

**Config (`engine/config.py`)** — `config.json` singleton. Pacing (move_ms, screen crossing, interior entry/exit, dialogue typewriter speed, warp fade-out/fade-in), battle limits, display geometry, interior exploration distance.

**Journal (`engine/journal.py`)** — `Journal`: a generic milestone log + ALL-CAPS narrative renderer (e.g. "PARTY DEFEATED LVL 2 GOBLIN"), used by `engine/battle.py`.

### Battle

**Combat resolver + battle state machine (`engine/battle.py`)** — one file, headless (no pygame). Used to be split across this file (the turn loop) and `engine/combat.py` (the math); merged into one once the loop itself shrank down to auto-attack-only — `engine/combat.py` no longer exists.

- `Fighter` — stats (iq/weight/sweat/hair/level) + hp/mp, percentage-based hit chance/crit/parry/damage math (stat fractions feed float probability knobs), `resolve_attack()`. Enemy HP scales with level at construction time.
- `load_fighter(path)` — one party member's `Fighter` from their `data/party/<name>.json` (only the combat-relevant fields; personality/special/items/xp aren't read here — specials/items aren't wired into battle at all yet, see below).
- `BattleState` — `party`/`enemy` Fighters + a `phase` (`"party_turn"|"enemy_turn"|"win"|"loss"`); `step()` resolves whichever side's turn it is and returns the flavor text for it. Pacing (when to call `step()`) is left to the caller — see `engine.renderer.BattleScene` below — so this class never blocks or sleeps.

**Current scope, deliberately narrow (first graphical wire-up):** MELVIN vs. one enemy, auto-attack only. No player-driven action menu, no DEFEND/RUN logic, no party specials (SING/LAUGH/SNACK/TICKLE, still just data on each `data/party/*.json`) or items wired in — see roadmap. **Party wipe** (revive at half HP/MP, return to hub) and the specials table below aren't implemented yet either; documenting the intended design here for when they are:

| Member | Special | Effect | MP cost |
|---|---|---|---|
| BILLY | SING | MESMERIZE — 50% skip chance, escalating break probability | 8 |
| MELVIN | LAUGH | CRINGE — 35% enemy self-attack per turn, 3–5 turns | 7 |
| POOTS | SNACK | Heals a party member 15–25% max HP | 6 |
| SMELTRUD | TICKLE | +15% ally damage for 2 turns | 5 |

**Battle screen (`engine.renderer.BattleScene`)** — graphical debug test, booted via `maptest.py`'s `battles` mode (prompts for an enemy id from `data/enemy/enemies.yaml`), not reachable from normal play yet. Top black textbox (reuses `engine.dialogue.DialogueBox` for the same typed-in reveal the overworld's dialogue uses) narrates each `BattleState.step()` result; a fixed hold (`_BATTLE_HOLD_MS`) after a line's fully typed advances to the next action automatically — no player input, since there's no menu wired up. Bottom black textbox: MELVIN's `NAME  HP n/max  MP n/max` on the left, the four-option ATTACK/ITEM/SPECIAL/RUN list on the right — drawn for layout, not interactive (only ATTACK actually happens). The enemy's `battle_art` (a full-size static portrait in `assets/tiles/enemies/`, distinct from the 16px overworld `sprite` strip) renders centered in the middle strip, over a live-animated `procgen/visualizer.py` effect (`battle_bg` on the enemy's `enemies.yaml` entry pins one by name; null/omitted — the case for both enemies today — picks one at random per battle). On win/loss the final message just stays on screen; there's no return-to-map flow yet.

### Enemy System

Placement, spawning, sprites, and overworld movement only — not battle (see roadmap). Built on the same object-layer pattern as containers/signs/NPCs: `enemy` (hardcoded placement) and `spawner` (rolls once per map load — a fresh boot or any warp arrival/re-arrival, so leaving and re-entering a screen re-rolls for free) objects are placed in Tiled exactly like `npc` objects (rectangle tool, no `gid`, top-left anchor), stubbed by `data/maps/populate_yamls.py` into `obj_<map>.yaml`, and read by `engine/renderer.py`'s `load_map_objects()` same as everything else.

**Master enemy list (`data/enemy/enemies.yaml`)** — hand-edited, one entry per enemy id: `sprite` (a stem in `assets/sprites/enemies/`, the 16px overworld strip), `battle_art` (a filename in `assets/tiles/enemies/`, the full-size static battle portrait — see Battle above), `battle_bg` (a `procgen/visualizer.py` effect name for the battle background, or null to pick one at random per battle), stats (`iq`/`weight`/`sweat`/`hair`, matching `engine.battle.Fighter`'s fields), `level` (a flat int or a `[min, max]` range — overridable per placement), `move_speed` (tiles/sec, fractional), and `behavior` (+ `behavior_axis` for pacers). Loaded once by `engine.enemy.load_enemy_defs()`.

**`engine/enemy.py`** — headless (no pygame/Tiled knowledge), same split as `engine/player.py`/`engine/input.py`. `EnemyDef`/`load_enemy_defs()` are the static data above; `Enemy` is the live per-instance state. `resolve_level()` rolls a level (flat or ranged); `resolve_spawn()` is a `spawner`'s roll — an overall gate (`spawn_chance`, a plain 0.0–1.0 fraction, not a percentage), then a weighted pick among its candidate `enemies` list (a YAML list of `{enemy_id, chance}` mappings — `chance` is a relative weight against the other entries in the same list, not an independent probability) so a successful roll always produces exactly one enemy, never zero or several. `check_overlap()` is a no-op today — enemies never block or are blocked by the player — that only flags where a future battle-trigger-on-touch + post-battle blink-immunity window will hook in.

**Movement — deliberately not tile-discrete.** `Enemy.row`/`col` are floats, in tile units, and that float pair *is* the logical position — not a renderer-only tween the way `NPC` gets one. Every frame, `update_enemy()` advances the enemy along its current cardinal direction by `move_speed × dt`, hard-stopping (via binary search on the still-clear fraction of that frame's step) at the first non-walkable/out-of-bounds tile its 1-tile bounding box would enter — same `passable` grid the player uses, checked continuously instead of tile-by-tile. Direction is re-evaluated on a fixed interval (`engine.enemy.DECISION_MS`, 800ms, matching the NPC wander tick's cadence) rather than every frame, so movement reads as smooth committed strides rather than jitter. Three behaviors, authored per-enemy: `wanderer` (65% chance to head toward the party, 35% random, re-rolled each decision tick), `pacer` (walks back and forth along `behavior_axis`, reversing when the next tile in its direction is blocked), `sentinel` (frozen until the party enters 8-tile cardinal line-of-sight, then chases). Player and NPC movement are untouched by any of this — they stay exactly as described under NPCs below.

**Not yet built:** `procgen/enemygen.py` (deterministic per-seed name/stat/sprite/behavior generation, SHA-256-derived from terrain seed) predates this system and isn't wired to it — it also has a stale comment referencing the deleted `web/` package's sprite path. Left as-is for now, out of scope for placement/spawning/movement.

### Scenes

**`engine/renderer.py`** — THE graphical renderer, one file, full stop. It owns: the generic pygame app loop (`run(scene_factory, ...)` — window, clock, event dispatch; constructs the scene *after* opening the window so asset loading like `.convert_alpha()` is safe); Tiled map + tileset JSON loading (parses `hub_fronthouse.json` and the sibling `tiles_town.json`, matched by basename, into GIDs and a `walkable`-derived passability grid); Tiled **object layer** loading (`load_map_objects()` — parses `container`/`sign`/`npc`/`enemy`/`spawner` objects into `MapObject` records and merges in each one's `dialogue`, plus `sprite`/`behavior` for npc-type ones or `enemy_id`/`level`/`enemies`/`spawn_chance` for enemy-type ones, from `obj_<map>.yaml`/`npcs_<map>.yaml`; converts each object's Tiled pixel position to a tile row/col — bottom-left-anchored for tile-stamp objects with a `gid` like containers/signs, top-left like a plain rectangle for objects with no `gid` like NPCs and enemies — and also keeps the un-floored `row_exact`/`col_exact`, which NPCs and enemies both use, to render/place at their exact authored position instead of snapping to the grid); sprite-sheet slicing (`partysprites.txt` → named party/NPC surfaces; `load_enemy_sprites()` → one strip file per enemy under `assets/sprites/enemies/`); menu asset loading (`load_menu_assets()` — `assets/menus/startmenu_*.png`, `save_menu_*.png`, and `dialogue_box.png` plus a pre-flipped copy of it); dialogue font loading (`ModernDOS8x8.ttf` at 16pt, which renders at 8px); and `OverworldScene` itself (player movement, tile-to-tile tweening, the player-centered clamped camera, drawing map objects/NPCs/player, the start-menu/save-confirm/dialogue-box overlays, warp-triggered fade transitions, and `_load_map()` — the map-dependent half of scene setup, factored out of `__init__` so a warp mid-game can reload it the same way the initial boot does). This used to be split across a separate `pygame_viewer/` package (`app.py` + `renderer.py` + `sprites.py`, plus dead code in `tileset.py`) — that split was rejected and the package was deleted; see the architecture note in CLAUDE.md. The object layer's `container`/`sign` objects are placed and rendered (`can1`, `can2`, `container3`, `sign1` on the hub map today); `sign1` and all three containers are interactable (see Items, NPCs, Dialogue, Events above for the container item-grant/flag/visual-state behavior — none of the three have a real `contents` filled in yet, so opening one today just shows its dialogue once and goes inert, no loot).

**NPCs (`NPC` dataclass in `engine/renderer.py`)** — built per `npc`-type object (one exists today: `interior_deptstore`'s `npc1`). Rendered via `get_npc_frame` from its own sprite strip under `assets/sprites/npcs/` (`engine.npc.load_npc_sprite_specs()`/`engine.renderer.load_npc_sprites()`, see Items, NPCs, Dialogue, Events above), not the party sheet and not the tileset. Two authored behaviors: `"static"` just plays its 2-frame idle animation in place (or, on an 8-frame sprite, faces its authored `facing:`); `"wander"` additionally rolls a coin flip every `_NPC_MOVE_MS` (800ms) tick to take one step in a random cardinal direction, checked against the same walkable grid the player uses plus current occupancy (won't step onto the player or another NPC) — turning to face whichever direction it just stepped, on an 8-frame sprite. Position is tile-discrete exactly like the player (`NPC.row`/`col`, plain ints) with the same renderer-only float tween toward the logical tile on move — except the tween's *initial* rest position isn't `(float(row), float(col))` like the player's always is: it's seeded from `MapObject.row_exact`/`col_exact` (the object's un-floored Tiled pixel placement, in tile units), so an NPC renders exactly where it was placed in Tiled rather than snapped to the grid (`interior_deptstore`'s `npc1` is deliberately placed a few pixels off-grid to exercise this). `NPC.row`/`col` themselves are still always the floored tile and still drive every gameplay concern (wander stepping, collision, occupancy) — only the *drawn* position starts off-grid. A `"wander"` NPC's first step tweens it from that exact spot onto the tile grid like any other move, and it stays grid-aligned from then on; a `"static"` NPC never moves, so it stays at its exact authored pixel position for its whole lifetime. The player, symmetrically, can't walk onto an NPC's tile — `OverworldScene._passable_with_npcs()` builds a per-step copy of the walkable grid with every NPC's current tile marked blocked, so `engine/player.py` stays headless and never has to know NPCs exist. Facing an NPC and pressing A opens its `dialogue` exactly like a sign (see Dialogue System below) — same `Player.adjacent_interactable()` → `handle_a_button` → `DialogueBox` path, just checking `target.type in ("sign", "npc")` instead of signs only. **Unrelated to `Enemy`** (`engine/enemy.py`, see Enemy System below) — a separate class for combat enemies, with continuous sub-tile movement instead of NPC's tile-discrete tween; the two share no code, just the same object-layer/YAML-sync pattern.

The start-menu overlay is drawn on top of everything while `scene.menu.is_open`: `startmenu_bg.png` (fills the camera, mostly transparent), then each option row (`startmenu_inventory/party/settings/save.png`, fixed pixel coordinates, the highlighted row shifted +7px right), then `startmenu_cursor.png` at the highlighted row's unshifted coordinate. If SAVE was confirmed, the save-confirm overlay draws on top of that in turn while `scene.save_menu.is_open`: `save_menu_bg.png`, then `save_menu_cursor.png` at a fixed spot per option (only the cursor moves — YES/NO aren't separate images). If INVENTORY was confirmed instead, the inventory screen draws on top while `scene.inventory_menu.is_open` — `_draw_inventory_menu()` — with no `menu_assets` image at all (there's no art for it yet): a solid black fill, `dialogue_font`-rendered category header with `<`/`>` arrows, a scrollable list of owned item names on the left (long names truncated with `...` via `_fit_text()` so they can't run into the detail pane — see `data/items/items.yaml` for how long some names actually are), a column of `|` glyphs as the divider, and on the right an outlined placeholder box standing in for the icon, the item's name, its word-wrapped description, and an `_format_item_effect()`-formatted line for any `effect` keys (`{hp: 15, mp: 5}` → `"HP +15  MP +5"`). Empty categories just show `(nothing here)` in the list column with a blank detail pane. `OverworldScene.update()` returns immediately while the start menu is open (which covers the save-confirm and inventory overlays too, since both only ever open from within the start menu) — the menu is a full gameplay pause, not just an input redirect.

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
│   ├── player.py           # Player entity; PlayerState machine; continuous move(passable_grid),
│   │                       #   8-directional via engine/movement.py, serialise
│   ├── input.py            # HeldDirectionInput + START/B/A button routing → engine/menu.py,
│   │                       #   engine/dialogue.py
│   ├── menu.py             # StartMenu + SaveMenu + InventoryMenu — pause-menu, save-confirm, and
│   │                       #   inventory-screen cursor state (open/close/move_cursor/confirm on each)
│   ├── inventory.py        # ItemDef + load_item_defs() (data/items/items.yaml loader) + Inventory
│   │                       #   (party-shared item-id → quantity pool); no pygame, no UI state
│   ├── game_state.py       # GameState (flags/variables) + persistent_id() — per-map-object flag
│   │                       #   keys with nothing to sync; no pygame
│   ├── save.py             # save_to_slot/load_from_slot/clear_slot/slot_exists — JSON under saves/
│   │                       #   (gitignored); bundles Player+Inventory+GameState+map name
│   ├── dialogue.py         # DialogueBox — paged, typewriter-revealed dialogue state
│   │                       #   (open/close/advance/tick/visible_text)
│   ├── renderer.py         # THE renderer: app loop, Tiled map+tileset+object-layer JSON loading,
│   │                       #   GID tile slicing, sprite-sheet slicing, camera, OverworldScene
│   │                       #   draw/update, start-menu + save-confirm + inventory-screen +
│   │                       #   dialogue-box overlay draw, BattleScene draw/update (see Battle)
│   ├── battle.py           # Fighter + hit/crit/parry/damage math + BattleState (headless
│   │                       #   combat resolver/state machine — engine/combat.py merged in,
│   │                       #   no longer exists); see roadmap for current auto-attack-only scope
│   ├── config.py           # config singleton (config.json)
│   ├── enemy.py            # EnemyDef/Enemy + data/enemy/enemies.yaml loader + spawn resolution +
│   │                       #   continuous sub-tile movement (via engine/movement.py); headless,
│   │                       #   no pygame (see Enemy System)
│   ├── movement.py         # step_continuous + collision helpers — continuous cardinal movement
│   │                       #   against a passable grid, shared by engine/player.py + engine/enemy.py
│   ├── npc.py               # NpcDef/NpcSpriteSpec + data/npcs/npc.yaml loader (defs + sprite
│   │                       #   strip/placeholder-color specs); headless, no pygame — same split
│   │                       #   as enemy.py (see Items, NPCs, Dialogue, Events above)
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
│   ├── sprites/            # party_sprites.png + layout text (player only -- deprecated for NPCs)
│   │   ├── enemies/         # one strip PNG per enemy (16px tall, frame count = width/16),
│   │   │                   #   filename stem == the `sprite` key in data/enemy/enemies.yaml
│   │   └── npcs/            # one strip PNG per NPC sprite (16px tall, 32x16 = 2 frames south-only,
│   │                       #   128x16 = 8 frames full facing), filename stem == the `file` key
│   │                       #   in data/npcs/npc.yaml's sprites: section
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
│   ├── items/              # items.yaml (definitions, loaded by engine/inventory.py) + ideas (brainstorm notes)
│   ├── enemy/               # enemies.yaml (master enemy list, loaded by engine/enemy.py's
│   │                       #   load_enemy_defs()) — sprite/stats/level/move_speed/behavior per enemy id
│   ├── npcs/                # npc.yaml (master NPC list -- sprites: + npcs: sections incl. facing
│   │                       #   + recolor palettes, loaded by engine/npc.py's load_npc_defs()/
│   │                       #   load_npc_sprite_specs()) + dialogue/ideas (unrelated personal
│   │                       #   planning notes, not consumed by anything -- see roadmap for
│   │                       #   conditional-dialogue logic)
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
├── saves/                   # gitignored — slot1.json.. written by engine/save.py, nothing checked in
├── maptest.py               # debug entry point — prompts map/battle/debug screen, boots the real game loop
│                            #   (map mode first prompts a save slot — see engine/save.py)
└── config.json               # pacing, display geometry, tunable params
```

**Where new content goes:**
- New/edited maps → author in Tiled, export JSON to `data/maps/<map_name>/<map_name>.json` (its own folder), then run `python data/maps/populate_yamls.py` to sync `obj_`/`npcs_` YAML stubs into that same folder.
- New tilesets → image + property export in `assets/tiles/`.
- New items → `data/items/items.yaml`.
- New enemies → add an entry to `data/enemy/enemies.yaml` and drop its overworld sprite strip in `assets/sprites/enemies/` (filename stem must match the entry's `sprite` field) and, for a battle-screen portrait, a full-size PNG in `assets/tiles/enemies/` (matched by the entry's `battle_art` field — see Battle). Place it on a map by adding an `enemy`/`spawner` object in Tiled, then running `populate_yamls.py` to stub `obj_<map>.yaml` and hand-filling `enemy_id`/`level` (or `enemies`/`spawn_chance`/`level` for a spawner) — see `populate_yamls.py`'s generated header comment for the exact `spawn_chance`/`enemies` YAML format.
- New container loot → set that container's `contents:` (in its map's `obj_<map>.yaml`, hand-filled after `populate_yamls.py` stubs it to `null`) to an item id from `data/items/items.yaml`. Leave it `null` for a container that's dialogue-only, no loot — either way, opening it is one-time (see Items, NPCs, Dialogue, Events above). To make it visually empty out on open, paint the "opened" tile (empty can, opened chest, ...) on a tile layer beneath where the container object sits, on whichever side of the Object Layer in Tiled draws under objects (see Maps above) — nothing extra to author beyond that.
- New NPC sprite → drop a strip PNG in `assets/sprites/npcs/` (32×16 for a south-only 2-frame idle, 128×16 for a full 8-frame `S1,S2,N1,N2,W1,W2,E1,E2` walk cycle), then add an entry under `sprites:` in `data/npcs/npc.yaml` pointing `file:` at its filename stem and listing its placeholder `colors:` (hex, **quoted** — see Items, NPCs, Dialogue, Events above for why unquoted hex silently corrupts).
- New reusable NPC → add an entry under `npcs:` in `data/npcs/npc.yaml` referencing one of those sprite ids, optionally its own `colors:` (same length/order as the sprite's, to recolor it just for this NPC) and `facing:` (static only), then set `npc_id` to that entry's key on any placement's `npcs_<map>.yaml` entry (stubbed by `populate_yamls.py`, see Map Object Sync above). Leave a placement's own `sprite`/`behavior`/`dialogue` blank to inherit from the def, fill one in to override it for just that placement. Conditional dialogue/event-trigger logic still isn't finalized — see roadmap (Event System).
- New abilities → `data/abilities/` (not created yet — see roadmap).

---

## Dev Setup

```bash
source .venv/bin/activate
python maptest.py           # prompts: map / battles / debug screen (debug screen is a stub)
                             # `map` lists every folder under data/maps/ — pick one to open it
                             # in the real game loop
                             # `battles` lists every enemy in data/enemy/enemies.yaml — pick
                             # one to fight MELVIN 1v1 (auto-attack only, see Battle)
```

All dependencies (pygame, Pillow, PyYAML) are in `.venv/`.
