# Front House Gaiden

An 8-bit RPG, developed entirely locally through a pygame renderer. Single-player.

---

## Content Pipeline

Two tools produce all game content. The engine's job is to **interpret** what they produce — maps and data files contain references, not gameplay logic.

| Content | Authored in | Format | Consumed by |
|---|---|---|---|
| Maps (tile layers, collision, object placement) | **Tiled** | JSON export | `engine/renderer.py` |
| Items, NPCs, dialogue, event flags, abilities | Hand-edited | **YAML** | `engine/` data loaders |

### Maps (Tiled → JSON)

A map authored in Tiled and exported to JSON contains:

- Tile layer(s) — graphics
- Collision layer(s)
- Object layer — spawn points, NPCs, triggers, warps

Layers composite in **real, authored order** — whatever order they're listed in Tiled, tile layers and the Object Layer alike. `engine/renderer.py`'s `load_tiled_map()` walks the JSON's `layers` list once and keeps every tile layer plus a marker for the Object Layer, in that exact sequence (`TiledLayer.kind`, `"tile"` or `"objects"`); `OverworldScene.draw()` then walks that same list, blitting each tile layer (`_draw_tile_layer`) or the whole containers/signs/NPCs/enemies/player bundle (`_draw_entities`) as it reaches each one. A tile layer placed *before* the Object Layer in Tiled draws under the player/objects; one placed *after* draws over them (roofs, tree canopies, an "empty" tile a container's full-tile art should hide until it's opened — see Items, NPCs, Dialogue, Events below). Nothing is hardcoded to a specific layer name or index — different maps author their Object Layer at different points in the stack, and all of them composite correctly from the same code path. `_draw_tile_layer` only walks the rows/cols actually inside the camera's viewport (plus a small margin for sub-tile scroll), not the whole grid — a map far bigger than the 16x14-tile viewport (e.g. `town.json` at 166x85) is mostly off-screen at any moment, and walking every cell of every layer every frame was a measured performance problem on slower hardware (Raspberry Pi). `_draw_entities` (NPCs/enemies/objects) isn't culled the same way yet — those lists are small enough on current maps that it hasn't been worth it.

A map may also paint from more than one tileset (e.g. `town.json` draws its ground from `tiles_town.json` and its buildings from `tiles_houses.json`) — `load_tiled_map()` loads every tileset a map declares, not just the first, and resolves each layer GID against whichever tileset's ID range it falls in. GIDs may also carry Tiled's per-cell mirror-flip flags (the "flip" brush) in their top 3 bits; `get_tile_by_gid()` strips and applies those rather than treating a flipped tile as a missing one.

Object layer entries are references only, e.g.:

```
NPC     id: old_man        sprite: old_man        logic: old_man_logic
Trigger script: castle_gate
Warp    destination_map: castle_entrance   spawn: south_gate
```

The engine loads the map, walks the object layer, and instantiates the right Python object per `type`. No gameplay logic is ever embedded in the map file itself.

Each map lives in its own folder under `data/maps/` (e.g. `data/maps/hub_fronthouse/`), holding the Tiled JSON export plus the YAML files described next. `engine/renderer.py`'s `load_map_objects()` parses the object layer into `MapObject` records, merging in each object's `dialogue`/`sprite`/`behavior`/etc. from that folder's `obj_<map>.yaml`/`npcs_<map>.yaml`. Tile-stamp objects with a `gid` (containers/signs/healers/NPCs, placed with Tiled's tile tool) anchor at their **bottom-left** corner; objects with no `gid` (enemies/spawners — placed with the rectangle/point tool) anchor **top-left** like a plain Tiled rectangle. `type == "sign"`, `"healer"`, `"npc"`, `"shop"`, and `"container"` are all interactable — face one and press A to open the Dialogue System on its `dialogue` pages (a container also grants its `contents` item and/or `gold` amount, either or both if set, and stops drawing its own `gid` once opened). A `healer` (e.g. a map's saladbar) fully heals the party for free, every visit, no flag, Pokemon-Center style (`engine/input.py`'s `handle_a_button`), appending a synthesized "HP/MP FULLY RESTORED." dialogue page. A `shop` is a shopkeeper — a person, built from the exact same pipeline as `npc` (sprite/behavior/`npc_id`), not a static fixture — see the NPCs paragraph below for how its greeting/buy-sell-screen/farewell flow works. `type == "warp"` objects aren't rendered or A-press interactable — they're invisible trigger tiles; walking onto one fires a transition instead (see Warps below).

**Warps.** A `warp`-type object is both a trigger tile (stepping onto it fires a map transition) and a possible landing spot (another map's warp can target it). Each warp is authored with `destination_map` (the destination map's folder/stem name) and `destination_warp` (the *name* of the warp object on that destination map to land on) — that name lookup is scoped to `destination_map`, so warp names only need to be unique within one map's object layer, never globally. `facing` (default south) and `distance` (default 0, in tiles) describe *this* warp's own landing spot whenever another warp points here — e.g. `facing: S, distance: 1` lands one tile south of the warp, so the player steps out of a doorway instead of standing on it. Warps are one-way and hand-paired: a two-way door is two separate warp objects, one on each map, each pointing at the other. A step onto a warp fades to black (`cfg.warp_fade_out_ms`), swaps the loaded map behind the fade, then fades back in (`cfg.warp_fade_in_ms`) on the destination map's landing tile — see `OverworldScene._begin_warp`/`_tick_transition`/`_swap_map`.

Booting into a map fresh (not via an in-game warp trip, e.g. `maptest.py`'s map picker) spawns the player on a random one of the map's own warp objects, if it has any, rather than an arbitrary fallback tile — so debugging a map drops you at one of its own authored entrances.

### Map Object Sync (`data/maps/populate_yamls.py`)

A dev-only script, never imported by the engine or run at runtime. It reads a map's Tiled object layer and, per object, writes a stub entry keyed by the object's Tiled `id` into either `npcs_<map>.yaml` (type `npc` or `shop` — a shop is a person, synced alongside every other NPC) or `obj_<map>.yaml` (everything else — `container`, `sign`, `warp`, `enemy`, `spawner`) in that same map's folder. Only `id`/`name`/`type` are synced from Tiled; new entries also get type-specific stub fields to hand-fill. Re-running never overwrites a field you've already filled in — it only touches `id`/`name`/`type`, adds stubs for genuinely new objects, and warns (without deleting) if an object disappears from the map.

```
python data/maps/populate_yamls.py                                          # syncs every map folder under data/maps/
python data/maps/populate_yamls.py data/maps/hub_fronthouse/hub_fronthouse.json  # syncs one map
```

This is how sign/container/NPC dialogue, container loot, enemy placement, and (later) scripted events get authored today — not by hand-writing the object registry, but by running the sync then filling in the stub fields it leaves behind.

### Items, NPCs, Dialogue, Events (YAML)

Everything that isn't map geometry is YAML under `data/`, keyed by ID and referenced from maps/other YAML — never embedded as inline logic.

**NPCs.** The master list is `data/npcs/npc.yaml`, loaded by `engine.npc.load_npc_defs()`/`load_npc_sprite_specs()`. It's two sections: `sprites:` names each NPC sprite *strip*, keyed by filename stem (`{file: <stem>, colors: [...]}` — `colors` the placeholder hex colors actually baked into that PNG); `npcs:` maps id → `sprite`/`behavior`/`facing`/`colors`/`dialogue`. `engine.renderer.load_npc_sprites` resolves a `file` stem by searching `assets/sprites/npcs/` *and* `assets/sprites/party/` (in that order, `npcs/` winning on a name collision) — most NPC strips live under `npcs/`, but a one-off pose for a cutscene-only NPC (e.g. `melvin_sleep`) can live under `party/` instead, alongside that character's other party art, without needing a copy under `npcs/` too. A strip's width picks how it animates: 32×16 (2 frames) is a facing-less south-only idle loop; 128×16 (8 frames) is a full walk cycle and responds to `facing`. Any `npc`-type object's entry in `npcs_<map>.yaml` can set `npc_id` to reference a shared def — its own `sprite`/`behavior`/`dialogue` fields become optional overrides on top of it, filled in they win for that placement, left blank they fall back to the def. `npc_id` is additive, not required — an entry with none behaves fully inline, as before this existed.

**Shops.** A `shop`-type entry lives in `npcs_<map>.yaml` right alongside `npc` ones and shares its `sprite`/`behavior`/`npc_id` fields — a shopkeeper is a person first, drawn and placed exactly like any other NPC (`engine.renderer.NPC`), not a static object. Talking to one is talk-then-shop: A opens the shopkeeper's `dialogue` (greeting) in a normal dialogue box first, same as any NPC; closing that greeting is what actually opens the buy/sell screen (`engine.menu.ShopMenu.pending_shop`, resolved in `engine.input.handle_a_button`). The buy/sell screen itself has two modes (BUY/SELL, E/W toggles) each with a scrollable, cursor-driven list — BUY shows `stock` (a list of `{item, price}` mappings, `price` independent of that item's own sell `value` in `items.yaml` — a shop can mark things up or down), SELL shows `Inventory.sellable_items()` (owned items with a nonzero `value`). Confirming a row (A) enters a quantity picker (E/W adjusts the amount, clamped to affordability/owned quantity) before actually transacting. Backing all the way out (B, not mid quantity-pick) closes the shop and, if the shopkeeper has a `farewell` line, shows it in a dialogue box too, the same paired way as the greeting — see `engine.input.handle_b_button`. A shop has no one-shot flag; both the greeting and the buy/sell screen are available every visit.

**Recoloring.** An `npcs:` entry's own `colors`, if set, replaces its sprite's placeholder colors for that NPC specifically, position-matched against the sprite's own `colors` list — this is what lets two NPCs share one sprite strip but read as visually distinct characters (`engine.renderer.recolor_surface()`, computed once per unique NPC def at boot, not per frame). **Hex values must be quoted in `npc.yaml`** (`"000000"`, not `000000`) — YAML's implicit typing reads an unquoted all-digit scalar as a number, and for some digit patterns not even the *right* number (`001100` unquoted parses as octal, decimal 576) — silently wrong, no YAML error at all.

**Dialogue** — plain pages, keyed by ID:

```yaml
old_man_intro:
  pages:
    - "Welcome to the village."
    - "Stay away from the forest."
```

Signs and NPCs both use this page-list shape today, but inline — a sign's `dialogue` lives directly on its entry in `obj_<map>.yaml`, an NPC's on its entry in `npcs_<map>.yaml` (or on its shared def in `data/npcs/npc.yaml`, if the placement doesn't override it), rather than in a separate ID-keyed file. A separate ID-keyed dialogue file isn't built yet, but an NPC's inline `dialogue` can already be conditional: instead of a flat page list, it can be a list of `{when, unless, pages}` variants checked top-to-bottom against `GameState.flags`, first match wins (`engine.npc.DialogueVariant`/`resolve_dialogue`). Every `npc_id` gets a free flag, `npc_met:<npc_id>`, set the instant it's ever talked to, so a first-ever-meeting variant is just `unless: [npc_met:<npc_id>]`. This is a narrower, purpose-built mechanism (conditions only, no actions) — not the general executor described below in Cutscenes. Signs don't have this yet, only NPCs.

**Cutscenes — the general condition/action system.** `engine/cutscene.py` is a headless step sequencer (`CutsceneDef`/`CutsceneTrigger`/`CutsceneStep`/`CutscenePlayer`) — same battle.py/BattleState split as everywhere else in this codebase: it only tracks *what* a cutscene should be doing and *which* step is current, never touching pygame/Tiled itself. `engine.renderer.OverworldScene` is the executor — a new full-pause gate in `update()`/`handle_event()`, same tier as a battle or an open dialogue box, so nothing else (wander AI, enemy movement, player input) ticks underneath a running cutscene. A cutscene file (`data/cutscenes/<id>.yaml`) is a flat, ordered step list, each entry a single-key mapping:

```yaml
id: intro_meeting
map: hub_fronthouse
trigger:
  event: map_load
  when: []
  unless: ["cutscene_seen:intro_meeting"]
steps:
  - move_actor:  {actor: old_man, to: [10, 12]}
  - wait:        {ms: 500}
  - dialogue:    {pages: ["Welcome home."]}
  - set_flag:    {flag: "cutscene_seen:intro_meeting"}
  - give_item:   {item: rusty_key}
```

Built step kinds: `move_actor` (cardinal-only, same rule as every other kind of movement in this game — an actor's target must share a row or column with its current position; an L-shaped path is two consecutive steps, one per leg), `teleport_actor` (`move_actor`'s no-walk counterpart — an instant position set, any target, no cardinal-line requirement, finished the same frame it runs; the tool for repositioning an actor while the camera's looking elsewhere — e.g. moving the player from a fresh boot's arbitrary default spawn point to wherever an intro cutscene actually needs them — without an on-screen-looking detour or a pointless off-camera walk), `face`, `wait`, `dialogue` (see Dialogue choices below), `set_flag`, `clear_flag`, `give_item`, `spawn_actor`/`despawn_actor` (a temporary NPC-shaped actor, always cleaned up the instant the scene ends — never left on the map afterward), `set_tile` (a one-shot GID swap on a tile layer — a door flipping open — not the same thing as *animated/cycling* tiles, which remain unbuilt and unrelated, see below), `pan_camera` (moves the camera off the player temporarily; `{to_player: true}` snaps it back), `fade` (`{direction: "in"|"out", color: "#rrggbb", duration_ms, see_through}` — a full-screen color overlay, default black, that holds at wherever its ramp lands rather than clearing itself: a fade-out stays solid until a later fade-in step or the cutscene ending; `see_through` keeps every sprite drawn on top of it instead of hidden underneath, for a silhouette-against-color look; dialogue boxes always draw on top of it regardless), and `start_cutscene` (jumps straight into a different cutscene, replacing the current one outright — a one-way jump, not a call-and-return; only valid targeting the same map, same "no mid-cutscene map switching" rule as everywhere else).

The `trigger:` block is the same `when`/`unless` flag-list shape `DialogueVariant` already uses (compound conditions like "has the key AND hasn't seen this scene" fall out for free), plus an `event` naming which real game surface checks it — deliberately `event`, not the more obvious `on`, since PyYAML reads a bare `on`/`off`/`yes`/`no` key as a *boolean*, not a string (same class of gotcha as `npc.yaml`'s unquoted hex colors). Four surfaces are wired:

- `map_load` — checked once per map load/warp arrival (`OverworldScene._check_cutscene_triggers`, called from `_load_map`). This is also how a story beat chains across two maps without the executor ever switching maps itself: cutscene A ends by setting a flag, the player leaves through an ordinary warp, and map B's own `map_load` check sees that flag and starts cutscene B.
- `tile` — a `trigger`-type Tiled object (invisible, modeled directly on `warp`; synced via `populate_yamls.py` same as every other object type), whose own `cutscene_id` names which cutscene to check the instant the player's tile becomes its tile.
- `npc_talk` — a `trigger.actor` naming an NPC/shop placement's own `name`; checked in `engine.input.handle_a_button` ahead of that NPC's ordinary `resolve_dialogue`, so the cutscene plays instead (the NPC's `npc_met_flag` still gets set either way).
- `flag` — checked every ordinary-gameplay frame (`OverworldScene.update()`), not off any physical action at all, against cutscenes matching the currently loaded map. This is the surface for chaining straight off another cutscene's own `set_flag` — no map transition, tile-step, or NPC-talk in between (e.g. a multi-part intro that plays entirely before the player ever gets control: cutscene A sets a flag as its last step, cutscene B's `trigger.when` names that flag and picks up immediately). Since it's polled with no physical gate, `OverworldScene` enforces one-shot-ness itself for this surface alone — it skips a `flag`-event cutscene outright once its own `cutscene_seen:<id>` flag is set (an unconditional check, not merely something added to the author's own `unless` list) and sets that flag the instant it fires — so no author-written `unless: [cutscene_seen:<id>]` guard is needed here the way it is for the other three surfaces.

**Dialogue choices.** `engine/dialogue.py`'s `DialogueBox` gained a response-list mode: a `dialogue` step's last page can carry `choices:`, and once that page fully reveals, an N/S-cursor + A-confirm prompt (`DialogueBox.is_showing_choices`/`move_choice_cursor`/`confirm_choice`) replaces "press A to close" — same interaction idiom as every other list menu in this game. Ordinary sign/NPC/container dialogue is unaffected (it never sets `choices`, so this mode never engages there); this is a cutscene-only feature so far.

A `dialogue` step's pages are word-wrapped the same way ordinary sign/NPC dialogue is (`OverworldScene._wrap_dialogue_pages`) — an authored page longer than the box's text area splits into multiple on-screen screens rather than overflowing past the edge. An optional `position: "top"|"bottom"` pins which edge the box docks to for just that step (read straight off the currently-active cutscene step, see `_draw_dialogue_box`), overriding the normal "auto-dock based on where the player is on screen" rule — useful once the camera's panned somewhere that rule would otherwise put the box right over.

```yaml
- dialogue:
    pages: ["Would you like to hear the legend?"]
    choices:
      - label: "Yes"
        then:
          - start_cutscene: {id: legend_flashback}
      - label: "No"
        then:
          - set_flag: {flag: declined_legend}
```

Each choice's own `then:` is a nested step list — parsed the same way the cutscene's own top-level `steps:` is (`engine.cutscene.CutsceneChoice`) — spliced directly into the running cutscene's step list the instant that choice is confirmed (`OverworldScene._confirm_dialogue_choice`), so its consequences get ordinary multi-frame step handling (another `wait`/`move_actor`/`dialogue`, not just instantaneous ones) rather than a separate one-shot path. This was a deliberate design choice over the alternative of a choice only setting a flag for some later `map_load`/`flag`/`npc_talk` trigger to notice elsewhere: it supports an immediate, same-beat branch ("Yes" instantly kicks off a flashback) that a flag-only design can't — at the cost of there being no standalone "dialogue_choice" trigger *event* the way `map_load`/`tile`/`npc_talk`/`flag` are; branching lives entirely in the choice's own `then:`, not in a fifth global trigger surface.

Also not built: mid-cutscene map switching (chaining, above, covers the common case on purpose), a cutscene leaving a permanently-added/removed map object behind, and locked warps. `maptest.py`'s debug `cutscene` mode lists every file under `data/cutscenes/` and plays one standalone, no save involved, same spirit as its `battles` mode.

**Containers — the original, narrower case.** Every container gets its flag for free via `persistent_id(map_name, object_name)` — no registry, no per-object declaration, and not (yet) migrated onto the general cutscene step executor even though it's the same `set_flag`/`give_item` shape. Opening one sets that flag and is inert afterward, one open, ever; a container with `contents` set grants that item, appending a synthesized `"Received {item name}."` page, and a container with `gold` set independently credits that amount to the party's wallet (`GameState.add_gold`), appending a synthesized `"Found ${amount}."` page — a container can grant either, both, or neither — see `engine.input._open_container`.

Also scoped but not designed at all yet: **animated tiles** (frame-cycling GIDs — how that interacts with the current one-surface-per-GID tile lookup isn't decided). This is unrelated to `set_tile` above, which is a single discrete swap, not a cycling animation.

---

## Roadmap

*Draft — order and grouping subject to revision.*

### Content Pipeline (Tiled + YAML)
- [x] Tiled JSON import — tile layers, tileset `walkable` property → passability grid, GID rendering, clamped camera; multiple tilesets per map, mirrored tiles; viewport-culled tile-layer draw (only visible rows/cols walked per frame, not the whole map grid)
- [x] Object layer → engine objects — `container`/`sign`/`healer`/`npc`/`enemy`/`spawner`/`warp`/`trigger`, synced to editable YAML via `data/maps/populate_yamls.py`
- [x] Order-preserving layer compositing — real Tiled layer order, not a hardcoded "first layer below" rule
- [x] Warps — fade-to-black map transitions, landing-spot offset/facing
- [x] Global game state / flags (`engine/game_state.py`) + `persistent_id()` per-object keys
- [x] Container → inventory wiring, one-time open, visual empty-out via layer compositing
- [x] Dialogue System (`engine/dialogue.py`) — paged, typewriter box; wired to sign/npc/container
- [x] Master NPC YAML (`data/npcs/npc.yaml`) — shared defs, per-NPC recoloring, `npc_id` override pattern
- [x] Enemy/spawner placement, spawn-chance resolution, continuous movement (not battle-triggered yet)
- [x] Cutscene/Event System — general step-list executor (`engine/cutscene.py` headless def/sequencer + `engine.renderer.OverworldScene` execution), `move_actor`/`teleport_actor`/`face`/`wait`/`dialogue`/`set_flag`/`clear_flag`/`give_item`/`spawn_actor`/`despawn_actor`/`set_tile`/`pan_camera`/`fade`/`start_cutscene` step kinds, triggered by `map_load`/`tile`/`npc_talk`/`flag` (the last polled continuously, for chaining cutscenes off each other's own flags with no physical action in between — self-guarding, always one-shot; containers remain their own older, narrower flag-driven case, not migrated onto this); mid-cutscene map switching still not built (chaining across maps via `map_load` covers the common case on purpose) — see Cutscenes above
- [x] Dialogue choices — a cutscene `dialogue` step's last page can carry a `choices:` response list (N/S cursor + A confirm, `engine/dialogue.py`'s `DialogueBox`), each choice carrying its own `then:` consequences (including jumping straight into another cutscene via `start_cutscene`) rather than a fourth global trigger surface — see Cutscenes above
- [x] Visual cutscene editor (`tools/cutscene_editor/`) — local browser tool for authoring `data/cutscenes/<id>.yaml` without hand-typing it: loads a real map's actual tiles/NPCs (server-side, via `engine.renderer.OverworldScene` itself, so the preview matches real playback exactly), builds the step list/trigger through a form, saves through the same round-trip-validated path either way
- [x] Conditional NPC dialogue — flag-gated `{when, unless, pages}` variants, inline on an NPC's def or placement (`engine.npc.DialogueVariant`/`resolve_dialogue`); narrower than the Cutscene/Event System above (conditions only, no actions), and signs don't have it yet
- [ ] NPC logic YAML — richer scripted NPC behavior (actions, not just dialogue conditions) via the general Cutscene/Event System
- [ ] Locked warps — script-driven doors/switches gating a warp on a flag
- [ ] Animated tiles — not designed yet
- [x] Healer object type (`healer`, e.g. a map's saladbar) — full-party HP/MP restore, free, no flag, every visit (`engine.input.handle_a_button`)
- [x] Currency — `GameState.gold`/`add_gold`/`spend_gold` (saves for free via `variables`), HUD readout on the start menu + inventory screen, container `gold` grants, battle win reward computed (`BattleState.gold_reward`) but not yet credited — see Visuals & UI below for the battle-crediting blocker
- [x] Shops — `shop`-type object, a shopkeeper built from the same pipeline as `npc` (sprite/behavior/`npc_id`, `engine.renderer.NPC`), talk-then-shop dialogue flow (greeting → buy/sell screen → farewell, `engine.menu.ShopMenu`), quantity-picker BUY/SELL against `Inventory`/`GameState.gold`

### Input & Core Game Loop
- [x] Keyboard input, held-key axis resolution (`engine/input.py`) — real 8-directional movement, tap-to-turn vs. hold-to-walk
- [x] Player entity (`engine/player.py`) — state machine, continuous movement against a passable grid
- [x] Config-driven movement/dialogue pacing (`config.json`)
- [x] Start menu shell, save-confirm overlay, settings screen, inventory screen (USE/EQUIP/UNEQUIP via an A-button item-action popup), party status screen (HP/MP/level/XP-to-next/equipment, view-only) (`engine/menu.py`)
- [x] Save through menu (`engine/save.py`)
- [ ] Bluetooth / USB controller support
- [ ] Title screen
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
- [x] Equip slots (`engine.roster.PartyMember.equipped_weapon`/`equipped_armour`) + stat effects in battle (flat `attack`/`defense` bonus off the equipped item's `effect`, see `engine.battle.fighter_from_roster`) + USE action (heals a chosen party member from the inventory screen, or a consumable off the battle ITEM row)
- [ ] Pickup system — nothing currently adds items to `Inventory` from the overworld (battle drops and container grants are the only sources)
- [ ] Special abilities YAML (`data/abilities/`)

### World & Content
- [ ] Rethink procgen scope — decide what stays procedural vs. hand-authored in Tiled
- [ ] Procgen output → Tiled-compatible JSON, so generated and hand-authored maps share one loader
- [ ] Updated and expanded tilesets
- [ ] Items in the world — pickup system (equip slots + stat effects are done, see Items & Abilities above)
- [ ] Named locations — towns, dungeons, overworld landmarks
- [ ] Enemy respawn tuning — a defeated `enemy`/`spawner` placement currently respawns on any map reload (leaving and re-entering), the same mechanism spawners already use every reload; consider an Earthbound-style refinement where respawn only happens once the defeated tile has scrolled off-camera, since maps are large

### Visuals & UI
**Stats.** `iq`/`weight`/`sweat`/`hair` (`data/party/<name>.json`, `data/enemy/enemies.yaml`) are fixed per character/enemy — they never grow over the course of the game; only `level` and equipped gear (`engine.roster.PartyMember.equipped_weapon`/`equipped_armour`) get stronger over time. Stat roles: `weight` is **physical power** (both damage dealt and toughness/HP); `iq` is **magic/special power** (damage and status-effect potency); `hair` is **magic defense/resist**, the counterpart to `weight` on the other track; `sweat` is **accuracy/evasion**, shared across both tracks. `level` is meant to be the dominant term in every formula, with the four stats acting as smaller fixed modifiers on top of a level-driven baseline rather than the thing that decides a fight — this is a redesign in progress; `engine/battle.py`'s current KNOBS/FORMULAS math is still the older all-stat-driven version and doesn't reflect this yet.

- [x] Battle resolver (`engine/battle.py`, headless `Fighter`/`BattleState`) + graphical debug screen (`engine.renderer.BattleScene`) — MELVIN vs. one enemy. Wired into real play: touching an enemy on the overworld starts a battle (`OverworldScene.start_battle`, gated by a post-battle immunity window so the player isn't instantly regrabbed); a win credits gold/XP/an item drop (`enemies.yaml`'s `xp`/`drop_item`/`drop_chance` fields) through `engine.roster.Roster`/`GameState`/`Inventory` and returns the player to their exact overworld spot; a loss shows a GAME OVER screen and reverts to the last save (discarding everything since, no other penalty). `maptest.py`'s isolated `battles` debug mode still works unchanged, for testing any enemy without a save. A scripted-encounter stub (`OverworldScene.trigger_scripted_encounter`) exists for a future cutscene-triggered fight — not callable from anywhere yet; the Cutscene/Event System above exists now, but nothing has wired a `start_battle`-style step kind onto it. Post-battle, the player gets a 2-second immunity window (blinking sprite) before touch-triggering another battle, so a win/flee doesn't instantly re-grab them.
- [x] Battle-entry transition — touching an enemy no longer cuts straight to the battle screen. A randomly-picked 8-frame dissolve animation (`assets/fx/transitions.png`, one row per variant, `_BATTLE_TRANSITION_ANIM_COUNT` finished so far) tiles across the whole screen over 2 seconds while player/NPC/enemy sprites stay visible on top, then holds on solid black for 0.5 seconds before the battle screen actually appears (`OverworldScene._tick_battle_transition`/`_draw_battle_transition_overlay`). Fully pauses gameplay and swallows input throughout.
- [ ] Battle screen overhaul — real art (currently plain textboxes + procgen backgrounds)
- [x] Player-driven battle action menu — ATTACK/ITEM/DEFEND/RUN row, N/S cursor + A confirm, hidden until the player's turn actually starts (a ~3s post-turn text hold, skippable with A/B, precedes it so results are readable before the menu reappears). ATTACK, ITEM, and RUN all do something when confirmed (ITEM opens a scrollable list of usable consumables); DEFEND is still a real selectable row with no effect wired in yet — ask before implementing. SPECIAL isn't in the row at all — see the per-member specials table below, unlocked by a story flag or player level, not just by existing in the party
- [x] RUN — level-scaled escape chance (`engine.battle.try_run`): 40% at equal average-party-vs-enemy level (currently just MELVIN's level, since battle is still 1v1 — becomes a real average once full-party battles exist), ±3%/level of difference, clamped to 5–95%, plus +15% per failed attempt this battle so a run always eventually succeeds. Success ends the battle with no rewards and leaves the enemy on the map (it wasn't defeated); failure costs the turn, same as a miss
- [ ] Full party in battle (currently MELVIN-only, 1v1) — three active party members, each with a distinct role; exact specials/numbers aren't finalized yet:

  | Member | Role |
  |---|---|
  | MELVIN | Balanced generalist — physical attacker, fast (good at ITEM use), some status-effect specials |
  | BILLY | Magic/special damage + status-effect specialist |
  | POOTS | Healer, durable physical support |

  `data/party/*.json`'s `special` fields (SING/LAUGH/SNACK/TICKLE) are stale placeholders from an earlier version of the project — don't treat them as current design. SMELTRUD (`data/party/smeltrud.json`) is also a leftover from that earlier version and isn't part of the roster; the party is likely staying at 3 members, not 4.

- [x] Party status panel (HP, MP, level, XP-to-next-level per member) — start menu's PARTY option, view-only
- [ ] Show ATTACK/DEFENSE on the party status panel alongside HP/MP/level/XP — see the TODO on `engine.renderer.OverworldScene._draw_party_detail`
- [ ] Battle-speed config (separate from movement speed)
- [x] XP → level-up conversion — `engine.battle.xp_to_next_level`/`apply_level_ups`: a simple `base * level^exponent` curve, checked after every battle win's XP award; each level crossed bumps `max_hp`/`max_mp` by the same per-level step a fresh Fighter uses, current HP/MP unchanged

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
│   ├── roster.py           # Roster/PartyMember — live party HP/MP/XP/level, save-round-trippable
│   ├── save.py             # save_to_slot/load_from_slot/clear_slot/slot_exists — JSON under saves/
│   ├── dialogue.py         # DialogueBox — paged, typewriter-revealed dialogue state + choice prompts
│   ├── renderer.py         # THE renderer: app loop, Tiled map/tileset/object-layer loading,
│   │                       #   sprite slicing, camera, OverworldScene + BattleScene
│   ├── battle.py           # Fighter + hit/crit/parry/damage math + BattleState (headless)
│   ├── config.py           # config singleton (config.json)
│   ├── enemy.py            # EnemyDef/Enemy + enemies.yaml loader + spawn resolution + movement
│   ├── movement.py         # step_continuous + collision helpers, shared by player.py/enemy.py
│   ├── npc.py              # NpcDef/NpcSpriteSpec + data/npcs/npc.yaml loader
│   ├── cutscene.py         # CutsceneDef/CutsceneTrigger/CutsceneStep/CutscenePlayer — headless
│   │                       #   data/cutscenes/*.yaml loader + step sequencer; OverworldScene executes it
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
│   ├── party/               # character sheet JSONs
│   └── cutscenes/           # <id>.yaml step lists — engine.cutscene.load_cutscene_defs, played by
│                             #   OverworldScene (real triggers) or maptest.py's debug `cutscene` mode
├── tests/                   # unittest suite (no pytest in .venv) — game_state/inventory/menu/battle/input/cutscene coverage
├── saves/                   # gitignored — slot1.json.. written by engine/save.py, nothing checked in
├── tools/
│   └── cutscene_editor/     # browser-based cutscene authoring tool — its own local HTTP server
│       ├── server.py        #   (stdlib only, no new dependency), reuses engine.cutscene/engine.renderer
│       └── static/          #   directly rather than re-deriving map/tileset logic in JS; run with
│                             #   `python tools/cutscene_editor/server.py`, open localhost:8420. Saves
│                             #   straight to data/cutscenes/<id>.yaml — the exact format the real game
│                             #   plays, whether a file was authored here or by hand.
├── maptest.py               # debug entry point — prompts map/battles/debug screen/cutscene
└── config.json               # pacing, display geometry, tunable params
```

**Where new content goes:**
- New/edited maps → author in Tiled, export JSON to `data/maps/<map_name>/<map_name>.json` (its own folder), then run `python data/maps/populate_yamls.py` to sync `obj_`/`npcs_` YAML stubs into that same folder.
- New tilesets → image + property export in `assets/tiles/`.
- New items → `data/items/items.yaml`.
- New enemies → add an entry to `data/enemy/enemies.yaml` and drop its overworld sprite strip in `assets/sprites/enemies/` (filename stem must match the entry's `sprite` field) and, for a battle-screen portrait, a full-size PNG in `assets/tiles/enemies/` (matched by the entry's `battle_art` field). Place it on a map by adding an `enemy`/`spawner` object in Tiled, then running `populate_yamls.py` to stub `obj_<map>.yaml` and hand-filling `enemy_id`/`level` (or `enemies`/`spawn_chance`/`level` for a spawner).
- New container loot → in its map's `obj_<map>.yaml` (hand-filled after `populate_yamls.py` stubs both to `null`), set `contents:` to an item id from `data/items/items.yaml` and/or `gold:` to a flat amount — independent fields, set either, both, or leave both `null` for a container that's dialogue-only. To make it visually empty out on open, paint the "opened" tile on a tile layer beneath where the container object sits.
- New healer (e.g. a saladbar) → place a `healer`-type object in Tiled, run `populate_yamls.py` to stub its `dialogue:` in `obj_<map>.yaml`, fill in the pages. Facing it and pressing A fully restores the active party's HP/MP for free, every visit, then shows `dialogue` with an auto-appended "HP/MP FULLY RESTORED." page.
- New NPC sprite → drop a strip PNG in `assets/sprites/npcs/` (32×16 south-only 2-frame idle, or 128×16 full 8-frame walk cycle), then add an entry under `sprites:` in `data/npcs/npc.yaml` pointing `file:` at its filename stem and listing its placeholder `colors:` (hex, **quoted**).
- New reusable NPC → add an entry under `npcs:` in `data/npcs/npc.yaml` referencing one of those sprite ids, optionally its own `colors:`/`facing:`, then set `npc_id` to that entry's key on any placement's `npcs_<map>.yaml` entry. Leave a placement's own `sprite`/`behavior`/`dialogue` blank to inherit from the def, fill one in to override it for just that placement.
- New shop → place a `shop`-type object in Tiled (a shopkeeper is a person — same sprite/behavior rules as any `npc`), run `populate_yamls.py` to stub it into `npcs_<map>.yaml`, then fill in `sprite`/`behavior`/`npc_id` same as an NPC, `dialogue:` for the greeting, `farewell:` for the goodbye line, and `stock:` (a list of `{item, price}` mappings) for what it sells.
- New cutscene → author it visually via `tools/cutscene_editor/` (`python tools/cutscene_editor/server.py`, open `http://localhost:8420/`) rather than hand-typing YAML — it loads a real map's actual tiles/NPCs, builds the step list and trigger through a form, and saves straight to `data/cutscenes/<id>.yaml`. (Hand-writing the YAML directly, per the step/trigger shape documented above, still works fine too — the editor is a convenience, not the only path.) Either way, test it standalone via `python maptest.py` → `cutscene`. To fire it for real: a `map_load` trigger needs nothing further; a `tile` trigger needs a `trigger`-type object placed in Tiled (run `populate_yamls.py` to stub its `cutscene_id:` in `obj_<map>.yaml`); an `npc_talk` trigger just needs the cutscene's own `trigger.actor` to name that NPC/shop placement's `name`; a `flag` trigger needs nothing further either — it fires the instant its `when` passes, so it's the right choice for chaining straight off a flag another cutscene on the same map already set (see Cutscenes above).
- New abilities → `data/abilities/` (not created yet — see Roadmap).

---

## Dev Setup

```bash
source .venv/bin/activate
python maptest.py           # prompts: map / battles / debug screen (debug screen is a stub)
                             # `map` lists every folder under data/maps/ — pick one to open it
                             # in the real game loop
                             # `battles` lists every enemy in data/enemy/enemies.yaml — pick
                             # one to fight MELVIN 1v1 in an isolated debug battle, no
                             # save/overworld involved (see Roadmap for the real, in-game
                             # touch-trigger path via `map` mode)
```

All dependencies (pygame, NumPy, Pillow, PyYAML) are in `.venv/`.
