// Cutscene editor front end. Talks only to tools/cutscene_editor/server.py's
// /api/* routes (see that file's module docstring for the overall design).
//
// State-mutation convention: continuous-typing fields (text/number inputs
// bound to a step's args) get a direct 'input'/'change' listener that
// mutates state.data in place WITHOUT re-rendering -- re-rendering on every
// keystroke would blow away focus/cursor position. A full renderSteps() (or
// renderTrigger()) only happens after a *structural* change: add/remove/
// move a step or choice, add/remove a flag tag, switch cutscene/map, or
// finish a "pick on map" click. Those are all discrete button/canvas clicks,
// never continuous typing, so losing focus there is a non-issue.

const MIN_SCALE = 0.05;
const MAX_SCALE = 16;

const state = {
  maps: [],
  cutscenes: [],
  flags: [],
  items: [],
  npcIds: [],
  mapMeta: null,
  data: null,       // { id, map, trigger, steps }
  view: { x: 0, y: 0, scale: 1 },   // pan/zoom transform on #map-wrapper (see applyView)
  pick: null,       // callback(row, col) while a "pick on map" is armed
  pickPreview: null,   // 'camera' while an armed pick should live-preview the camera border, else null
  showObjects: true,
  showPlayer: true,
  showSpawned: true,
};

const $ = (sel) => document.querySelector(sel);

async function getJSON(url) {
  const res = await fetch(url);
  const body = await res.json();
  if (!res.ok) throw new Error(body.error || res.statusText);
  return body;
}

function setStatus(msg, kind) {
  const el = $('#status');
  el.textContent = msg;
  el.className = kind || '';
}

// ── bootstrap ────────────────────────────────────────────────────────────

async function init() {
  const [maps, cutscenes, flags, items, npcIds] = await Promise.all([
    getJSON('/api/maps'),
    getJSON('/api/cutscenes'),
    getJSON('/api/flags'),
    getJSON('/api/items'),
    getJSON('/api/npc_ids'),
  ]);
  state.maps = maps;
  state.cutscenes = cutscenes;
  state.flags = flags;
  state.items = items;
  state.npcIds = npcIds;

  fillOptions($('#map-picker'), maps.map((m) => ({ value: m, label: m })));
  fillOptions($('#cutscene-picker'), [{ value: '', label: '— new —' }]
    .concat(cutscenes.map((c) => ({ value: c.id, label: `${c.id} (${c.map})` }))));
  fillDatalist('#dl-flags', flags.map((f) => ({ value: f })));
  fillDatalist('#dl-items', items.map((i) => ({ value: i.id, label: i.name })));
  fillDatalist('#dl-npc-ids', npcIds.map((n) => ({ value: n })));
  fillDatalist('#dl-cutscenes', cutscenes.map((c) => ({ value: c.id })));

  wireTopbar();
  wireMapPanZoom();
  $('#pick-cancel').addEventListener('click', disarmPick);
  wireAddStep();

  if (cutscenes.length) {
    $('#cutscene-picker').value = cutscenes[0].id;
    await loadCutscene(cutscenes[0].id);
  } else if (maps.length) {
    await startNewCutscene(maps[0]);
  }
}

function fillOptions(select, opts) {
  select.innerHTML = '';
  for (const o of opts) {
    const el = document.createElement('option');
    el.value = o.value;
    el.textContent = o.label ?? o.value;
    select.appendChild(el);
  }
}

function fillDatalist(sel, opts) {
  const dl = $(sel);
  dl.innerHTML = '';
  for (const o of opts) {
    const el = document.createElement('option');
    el.value = o.value;
    if (o.label) el.label = o.label;
    dl.appendChild(el);
  }
}

// ── topbar ───────────────────────────────────────────────────────────────

function wireTopbar() {
  $('#cutscene-picker').addEventListener('change', async (e) => {
    const id = e.target.value;
    $('#new-id').style.display = id ? 'none' : '';
    if (id) await loadCutscene(id);
    else await startNewCutscene($('#map-picker').value || state.maps[0]);
  });

  $('#map-picker').addEventListener('change', async (e) => {
    await loadMap(e.target.value);
    if (state.data && !state.cutsceneExists) state.data.map = e.target.value;
    renderAll();
  });

  $('#fit-btn').addEventListener('click', fitToView);

  $('#show-objects-cb').addEventListener('change', (e) => {
    state.showObjects = e.target.checked;
    renderOverlay();
  });
  $('#show-player-cb').addEventListener('change', (e) => {
    state.showPlayer = e.target.checked;
    renderOverlay();
  });
  $('#show-spawned-cb').addEventListener('change', (e) => {
    state.showSpawned = e.target.checked;
    renderSpawnedActors();
  });

  $('#save-btn').addEventListener('click', saveCutscene);
  $('#test-btn').addEventListener('click', testInGame);
}

async function testInGame() {
  const ok = await saveCutscene();
  if (!ok) return;
  setStatus('Launching game window…');
  try {
    const res = await fetch(`/api/play/${encodeURIComponent(state.data.id)}`, { method: 'POST' });
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || 'launch failed');
    setStatus(`Playing ${state.data.id} in a game window`, 'ok');
  } catch (err) {
    setStatus(`Test failed: ${err.message}`, 'err');
  }
}

async function startNewCutscene(mapName) {
  state.cutsceneExists = false;
  state.data = { id: '', map: mapName, trigger: null, steps: [] };
  $('#cutscene-picker').value = '';
  $('#new-id').style.display = '';
  $('#new-id').value = '';
  $('#map-picker').value = mapName;
  await loadMap(mapName);
  renderAll();
}

async function loadCutscene(id) {
  state.cutsceneExists = true;
  const data = await getJSON(`/api/cutscenes/${encodeURIComponent(id)}`);
  state.data = data;
  $('#new-id').style.display = 'none';
  $('#map-picker').value = data.map;
  await loadMap(data.map);
  renderAll();
}

async function loadMap(name) {
  const meta = await getJSON(`/api/maps/${encodeURIComponent(name)}`);
  state.mapMeta = meta;
  hideCameraBorder();   // stale border from the previous map would be the wrong size/position
  const img = $('#map-bg');
  img.draggable = false;
  img.src = `${meta.background_url}?t=${Date.now()}`;
  img.onload = () => { fitToView(); renderOverlay(); };
  fillDatalist('#dl-layers', meta.tile_layers.map((l) => ({ value: l })));
}

// ── pan/zoom ─────────────────────────────────────────────────────────────
//
// #map-wrapper (the <img> + #map-overlay together) is transformed as one
// unit via CSS `transform: translate(x, y) scale(scale)` with transform-
// origin (0, 0) -- the image and overlay both stay at their natural pixel
// size always, so overlay marker positions are always plain row/col *
// tile_px with no separate "* zoom" bookkeeping to keep in sync. Maps vary
// from a tiny single room to a multi-thousand-pixel overworld, so there's
// no fixed zoom range that suits both -- fitToView() computes an initial
// scale per map (fit the whole thing in the viewport, whether that means
// zooming a tiny map in or a huge one out), then the wheel/drag handlers
// below take over for the rest of the session.

function applyView() {
  const { x, y, scale } = state.view;
  $('#map-wrapper').style.transform = `translate(${x}px, ${y}px) scale(${scale})`;
}

function fitToView() {
  const img = $('#map-bg');
  const rect = $('#map-scroll').getBoundingClientRect();
  if (!img.naturalWidth || !rect.width) return;
  const scale = clampScale(Math.min(rect.width / img.naturalWidth, rect.height / img.naturalHeight));
  state.view = {
    scale,
    x: (rect.width - img.naturalWidth * scale) / 2,
    y: (rect.height - img.naturalHeight * scale) / 2,
  };
  applyView();
}

function clampScale(scale) {
  return Math.min(MAX_SCALE, Math.max(MIN_SCALE, scale));
}

function wireMapPanZoom() {
  const scroll = $('#map-scroll');

  scroll.addEventListener('wheel', (e) => {
    e.preventDefault();
    const rect = scroll.getBoundingClientRect();
    const cx = e.clientX - rect.left;
    const cy = e.clientY - rect.top;
    const before = state.view;
    const mapX = (cx - before.x) / before.scale;
    const mapY = (cy - before.y) / before.scale;
    const factor = Math.exp(-e.deltaY * 0.0015);
    const scale = clampScale(before.scale * factor);
    state.view = { scale, x: cx - mapX * scale, y: cy - mapY * scale };
    applyView();
  }, { passive: false });

  let dragging = false;
  let dragMoved = false;
  let dragStart = { clientX: 0, clientY: 0, viewX: 0, viewY: 0 };

  scroll.addEventListener('pointerdown', (e) => {
    if (e.button !== 0) return;
    dragging = true;
    dragMoved = false;
    dragStart = { clientX: e.clientX, clientY: e.clientY, viewX: state.view.x, viewY: state.view.y };
    scroll.setPointerCapture(e.pointerId);
    scroll.classList.add('dragging');
  });

  scroll.addEventListener('pointermove', (e) => {
    if (dragging) {
      const dx = e.clientX - dragStart.clientX;
      const dy = e.clientY - dragStart.clientY;
      if (Math.abs(dx) > 3 || Math.abs(dy) > 3) dragMoved = true;
      state.view.x = dragStart.viewX + dx;
      state.view.y = dragStart.viewY + dy;
      applyView();
      return;
    }
    // Live camera-border preview while a pan_camera "pick on map" is armed
    // (see armPick's previewKind) -- lets you see exactly what the real
    // in-game camera would show *before* you commit to a tile, which is
    // the whole point of showing the border at all.
    if (state.pick && state.pickPreview === 'camera' && state.mapMeta) {
      const { row, col } = mapTileFromEvent(e);
      updateCameraBorder(row, col);
    }
  });

  scroll.addEventListener('pointerup', (e) => {
    if (!dragging) return;
    dragging = false;
    scroll.classList.remove('dragging');
    if (!dragMoved) handleMapClick(e);
  });

  window.addEventListener('resize', () => { if (state.mapMeta) fitToView(); });
}

function mapTileFromEvent(e) {
  const rect = $('#map-scroll').getBoundingClientRect();
  const localX = e.clientX - rect.left;
  const localY = e.clientY - rect.top;
  const mapX = (localX - state.view.x) / state.view.scale;
  const mapY = (localY - state.view.y) / state.view.scale;
  const px = state.mapMeta.tile_px;
  return { row: Math.floor(mapY / px), col: Math.floor(mapX / px) };
}

function handleMapClick(e) {
  if (!state.pick || !state.mapMeta) return;
  const { row, col } = mapTileFromEvent(e);
  const cb = state.pick;
  disarmPick();
  cb(row, col);
}

// ── map overlay + tile picking ───────────────────────────────────────────

function renderOverlay() {
  const meta = state.mapMeta;
  const overlay = $('#map-overlay');
  overlay.innerHTML = '';
  if (!meta) return;
  const px = meta.tile_px;

  for (const a of meta.actors) {
    if (a.kind === 'player' && !state.showPlayer) continue;
    const el = document.createElement('div');
    el.className = 'actor-marker';
    el.style.left = `${a.col * px}px`;
    el.style.top = `${a.row * px}px`;
    el.textContent = a.name;
    el.title = `${a.name} (${a.kind}) @ ${a.row},${a.col}`;
    overlay.appendChild(el);
  }
  if (state.showObjects) {
    for (const o of meta.objects) {
      const el = document.createElement('div');
      el.className = 'object-marker';
      el.style.left = `${o.col * px}px`;
      el.style.top = `${o.row * px}px`;
      el.textContent = o.type === 'trigger' ? `⚑${o.cutscene_id ?? ''}` : o.name;
      el.title = `${o.name} (${o.type}) @ ${o.row},${o.col}`;
      overlay.appendChild(el);
    }
  }
}

// A spawn_actor step only ever takes effect at cutscene *runtime* -- there's
// nothing on the server's static map snapshot to show it (see the "static
// render" conversation: the background PNG and meta.actors/objects are a
// one-time snapshot of the map as it exists before any cutscene runs). So
// every spawn_actor step in the currently-edited cutscene gets its own
// overlay sprite here instead, positioned from its own row/col args and
// re-rendered live as those args change (see rowColFields/textField's
// onChange hook in stepBody) -- not a simulation of *when* it would
// actually appear relative to other steps, just "here's where it'll land
// and what it'll look like," which is what authoring positioning needs.
function renderSpawnedActors() {
  const container = $('#spawned-overlay');
  container.innerHTML = '';
  if (!state.showSpawned || !state.mapMeta) return;
  const px = state.mapMeta.tile_px;

  for (const args of spawnActorSteps()) {
    const row = Number(args.row) || 0;
    const col = Number(args.col) || 0;
    const facing = args.facing || 'S';
    const label = args.id || '(no id)';

    if (!args.npc_id) {
      const marker = document.createElement('div');
      marker.className = 'actor-marker spawned-marker';
      marker.style.left = `${col * px}px`;
      marker.style.top = `${row * px}px`;
      marker.textContent = `${label}?`;
      marker.title = `spawn_actor ${label}: no npc_id set yet`;
      container.appendChild(marker);
      continue;
    }

    const img = document.createElement('img');
    img.className = 'spawned-sprite';
    img.alt = label;
    img.title = `spawn_actor ${label} (${args.npc_id}) @ ${row},${col}`;
    img.src = `/api/npc_sprite/${encodeURIComponent(args.npc_id)}?facing=${encodeURIComponent(facing)}`;
    img.onload = () => {
      // Bottom-left anchor, same as real play (OverworldScene._draw_entities):
      // row/col is the sprite's bottom-left tile, so a frame taller than one
      // tile extends upward from there, not downward.
      img.style.left = `${col * px}px`;
      img.style.top = `${(row + 1) * px - img.naturalHeight}px`;
    };
    img.onerror = () => {
      // e.g. an npc_id whose sprite PNG doesn't exist yet (npc.yaml
      // references it, but the art hasn't been dropped into
      // assets/sprites/npcs/ yet) -- fall back to a plain marker instead of
      // a broken-image icon, same spirit as the engine's own "spawns but
      // renders as nothing" console warning for a missing sprite file.
      const marker = document.createElement('div');
      marker.className = 'actor-marker spawned-marker';
      marker.textContent = `${label} (missing sprite)`;
      marker.title = `spawn_actor ${label}: no sprite file for npc_id "${args.npc_id}"`;
      marker.style.left = `${col * px}px`;
      marker.style.top = `${row * px}px`;
      img.replaceWith(marker);
    };
    container.appendChild(img);
  }
}

function spawnActorSteps() {
  const found = [];
  const scan = (steps) => {
    for (const s of steps || []) {
      if (s.kind === 'spawn_actor') found.push(s.args);
      if (s.kind === 'dialogue') {
        for (const c of s.args.choices || []) scan(c.then);
      }
    }
  };
  scan(state.data ? state.data.steps : []);
  return found;
}

function actorNames() {
  const names = (state.mapMeta ? state.mapMeta.actors.map((a) => a.name) : []);
  const spawned = spawnActorSteps().map((args) => args.id).filter(Boolean);
  return [...new Set([...names, ...spawned])];
}

function armPick(cb, previewKind = null) {
  state.pick = cb;
  state.pickPreview = previewKind;
  $('#pick-banner').classList.remove('hidden');
}

function disarmPick() {
  state.pick = null;
  state.pickPreview = null;
  $('#pick-banner').classList.add('hidden');
}

// ── camera border preview ───────────────────────────────────────────────
//
// Mirrors engine.renderer.OverworldScene._camera_axis exactly: the real
// camera clamps to the map edges rather than always centering dead-on the
// target, so a naive "just center a view_cols x view_rows box on (row,
// col)" preview would lie near an edge -- this reproduces that same clamp
// so the box shown here is exactly what pan_camera would actually frame.

function cameraAxis(pos, mapLen, viewLen) {
  if (mapLen <= viewLen) return (mapLen - viewLen) / 2;
  return Math.min(Math.max(pos - viewLen / 2, 0), mapLen - viewLen);
}

function updateCameraBorder(row, col) {
  const meta = state.mapMeta;
  const el = $('#camera-border');
  if (!meta || !meta.view_cols || !meta.view_rows) { el.classList.add('hidden'); return; }
  const px = meta.tile_px;
  const camCol = cameraAxis(col, meta.width, meta.view_cols);
  const camRow = cameraAxis(row, meta.height, meta.view_rows);
  el.style.left = `${camCol * px}px`;
  el.style.top = `${camRow * px}px`;
  el.style.width = `${meta.view_cols * px}px`;
  el.style.height = `${meta.view_rows * px}px`;
  el.classList.remove('hidden');
}

function hideCameraBorder() {
  $('#camera-border').classList.add('hidden');
}

// ── trigger panel ────────────────────────────────────────────────────────

function renderTrigger() {
  const body = $('#trigger-body');
  body.innerHTML = '';
  const t = state.data.trigger;

  if (!t) {
    const btn = document.createElement('button');
    btn.textContent = '+ Add trigger';
    btn.addEventListener('click', () => {
      state.data.trigger = { event: 'map_load', when: [], unless: [], actor: null };
      renderTrigger();
    });
    body.appendChild(btn);
    return;
  }

  const eventRow = document.createElement('div');
  eventRow.className = 'field-row';
  const eventLabel = document.createElement('label');
  eventLabel.textContent = 'Fires on';
  const eventSelect = document.createElement('select');
  fillOptions(eventSelect, ['map_load', 'tile', 'npc_talk', 'flag'].map((v) => ({ value: v })));
  eventSelect.value = t.event;
  eventSelect.addEventListener('change', () => {
    t.event = eventSelect.value;
    renderTrigger();
  });
  const removeBtn = document.createElement('button');
  removeBtn.textContent = 'remove trigger';
  removeBtn.className = 'danger';
  removeBtn.addEventListener('click', () => { state.data.trigger = null; renderTrigger(); });
  eventRow.append(eventLabel, eventSelect, removeBtn);
  body.appendChild(eventRow);

  if (t.event === 'tile') {
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'A "tile" trigger fires from a Tiled trigger object\'s own cutscene_id ' +
      '(placed in Tiled itself, not here) -- this panel only authors this cutscene\'s own ' +
      'when/unless conditions, which that tile object\'s cutscene_id still has to pass.';
    body.appendChild(hint);
  }

  if (t.event === 'flag') {
    const hint = document.createElement('div');
    hint.className = 'hint';
    hint.textContent = 'Fires the instant its "when" flags are true, checked every frame -- no map ' +
      'load / tile-step / NPC-talk needed. Use this to chain straight from another cutscene on this ' +
      'same map (that cutscene sets a flag, this one\'s "when" names it) -- e.g. a multi-part intro ' +
      'before the player has control. Always fires once: this cutscene auto-marks itself seen the ' +
      'instant it starts, no "unless" guard needed.';
    body.appendChild(hint);
  }

  if (t.event === 'npc_talk') {
    const row = document.createElement('div');
    row.className = 'field-row';
    const label = document.createElement('label');
    label.textContent = 'NPC name';
    const input = document.createElement('input');
    input.value = t.actor || '';
    input.setAttribute('list', 'dl-actors');
    input.placeholder = 'e.g. wizard';
    input.addEventListener('input', () => { t.actor = input.value || null; });
    row.append(label, input);
    body.appendChild(row);
  }

  body.appendChild(flagListWidget('When (all must be set)', t.when));
  body.appendChild(flagListWidget('Unless (none may be set)', t.unless));
}

function flagListWidget(label, list) {
  const wrap = document.createElement('div');
  const title = document.createElement('div');
  title.className = 'hint';
  title.textContent = label;
  wrap.appendChild(title);

  const tags = document.createElement('div');
  tags.className = 'flag-tags';
  const redraw = () => {
    tags.innerHTML = '';
    list.forEach((flag, i) => {
      const tag = document.createElement('span');
      tag.className = 'flag-tag';
      const text = document.createElement('span');
      text.textContent = flag;
      const remove = document.createElement('button');
      remove.textContent = '×';
      remove.addEventListener('click', () => { list.splice(i, 1); redraw(); });
      tag.append(text, remove);
      tags.appendChild(tag);
    });
  };
  redraw();
  wrap.appendChild(tags);

  const addRow = document.createElement('div');
  addRow.className = 'field-row';
  const input = document.createElement('input');
  input.setAttribute('list', 'dl-flags');
  input.placeholder = 'flag name';
  const addBtn = document.createElement('button');
  addBtn.textContent = '+';
  const commit = () => {
    if (input.value.trim()) {
      list.push(input.value.trim());
      input.value = '';
      redraw();
    }
  };
  addBtn.addEventListener('click', commit);
  input.addEventListener('keydown', (e) => { if (e.key === 'Enter') { e.preventDefault(); commit(); } });
  addRow.append(input, addBtn);
  wrap.appendChild(addRow);
  return wrap;
}

// ── step list ────────────────────────────────────────────────────────────

const STEP_DEFAULTS = {
  face: { actor: 'player', dir: 'S' },
  wait: { ms: 500 },
  move_actor: { actor: 'player', to: [0, 0] },
  teleport_actor: { actor: 'player', to: [0, 0] },
  dialogue: { pages: ['...'] },
  set_flag: { flag: '', value: true },
  clear_flag: { flag: '' },
  give_item: { item: '', qty: 1 },
  spawn_actor: { id: '', npc_id: '', row: 0, col: 0, facing: 'S' },
  despawn_actor: { id: '' },
  set_tile: { layer: '', row: 0, col: 0, gid: 0 },
  pan_camera: { to: [0, 0], duration_ms: 400 },
  fade: { direction: 'out', color: '#000000', duration_ms: 500, see_through: false },
  start_cutscene: { id: '' },
};

// One accent color per step kind so a card is identifiable at a glance
// without reading its label -- grouped loosely by what the step affects
// (actor movement in blue/teal, flags in violet, camera/screen fx in
// cyan/magenta, ...) rather than assigned arbitrarily.
const STEP_COLORS = {
  face: '#5b8dee',
  wait: '#9099a6',
  move_actor: '#4caf6e',
  teleport_actor: '#2bbfae',
  dialogue: '#e0b23e',
  set_flag: '#8a6fe0',
  clear_flag: '#6a5db8',
  give_item: '#e07ecb',
  spawn_actor: '#a3d939',
  despawn_actor: '#6b8e23',
  set_tile: '#c9862f',
  pan_camera: '#3fa7d6',
  fade: '#b46fe0',
  start_cutscene: '#e2604f',
};

function hexToRgb(hex) {
  const h = hex.replace('#', '');
  return [parseInt(h.slice(0, 2), 16), parseInt(h.slice(2, 4), 16), parseInt(h.slice(4, 6), 16)];
}

// Drag-and-drop reorder state, shared across every stepCard/stepCardForNested
// instance (top-level steps and every choice's nested `then:` list are each
// their own siblings array -- `siblings` here identifies *which* one a drag
// started in, so a card can't be dropped into a different list's DOM by
// mistake -- e.g. a nested choice nested inside another step's card).
const dragState = { siblings: null, fromIndex: null };

function wireStepDrag(card, siblings, index, rerender) {
  const handle = document.createElement('span');
  handle.className = 'drag-handle';
  handle.textContent = '⠿';
  handle.title = 'Drag to reorder';
  handle.draggable = true;

  handle.addEventListener('dragstart', (e) => {
    dragState.siblings = siblings;
    dragState.fromIndex = index;
    e.dataTransfer.effectAllowed = 'move';
    e.dataTransfer.setData('text/plain', '');
    requestAnimationFrame(() => card.classList.add('dragging'));
  });
  handle.addEventListener('dragend', () => {
    card.classList.remove('dragging');
    dragState.siblings = null;
    dragState.fromIndex = null;
  });

  card.addEventListener('dragover', (e) => {
    if (dragState.siblings !== siblings || dragState.fromIndex === null) return;
    e.preventDefault();
    e.dataTransfer.dropEffect = 'move';
    const rect = card.getBoundingClientRect();
    const before = (e.clientY - rect.top) < rect.height / 2;
    card.classList.toggle('drag-over-top', before);
    card.classList.toggle('drag-over-bottom', !before);
  });
  card.addEventListener('dragleave', () => {
    card.classList.remove('drag-over-top', 'drag-over-bottom');
  });
  card.addEventListener('drop', (e) => {
    e.preventDefault();
    card.classList.remove('drag-over-top', 'drag-over-bottom');
    if (dragState.siblings !== siblings || dragState.fromIndex === null) return;
    const from = dragState.fromIndex;
    const rect = card.getBoundingClientRect();
    const before = (e.clientY - rect.top) < rect.height / 2;
    let to = index + (before ? 0 : 1);
    if (to > from) to -= 1;   // account for the shift left by the removal below
    dragState.siblings = null;
    dragState.fromIndex = null;
    if (to === from) return;
    const [moved] = siblings.splice(from, 1);
    siblings.splice(to, 0, moved);
    rerender();
  });

  return handle;
}

function wireAddStep() {
  $('#add-step-btn').addEventListener('click', () => {
    const kind = $('#add-step-kind').value;
    state.data.steps.push({ kind, args: structuredClone(STEP_DEFAULTS[kind]) });
    renderSteps();
  });
}

function renderAll() {
  renderTrigger();
  renderSteps();
  renderOverlay();
  renderSpawnedActors();
  updateActorDatalist();
}

function updateActorDatalist() {
  fillDatalist('#dl-actors', actorNames().map((n) => ({ value: n })));
}

function renderSteps() {
  updateActorDatalist();
  const list = $('#steps-list');
  list.innerHTML = '';
  state.data.steps.forEach((step, i) => {
    list.appendChild(stepCard(step, state.data.steps, i));
  });
  // Explicit, not just the incidental onChange auto-fire inside a
  // spawn_actor step's own rowColFields: with zero spawn_actor steps left
  // (e.g. the last one just got deleted) nothing else would ever clear a
  // stale marker left over from before.
  renderSpawnedActors();
}

function stepCard(step, siblings, index) {
  const card = document.createElement('div');
  card.className = 'step-card';
  const color = STEP_COLORS[step.kind] || '#5b8dee';
  const [r, g, b] = hexToRgb(color);
  card.style.borderLeftColor = color;

  const head = document.createElement('div');
  head.className = 'step-head';
  head.style.background = `rgba(${r}, ${g}, ${b}, 0.16)`;
  const handle = wireStepDrag(card, siblings, index, renderSteps);
  const kind = document.createElement('span');
  kind.className = 'kind';
  kind.style.color = color;
  kind.textContent = `${index + 1}. ${step.kind}`;
  const up = document.createElement('button');
  up.textContent = '↑';
  up.disabled = index === 0;
  up.addEventListener('click', () => {
    [siblings[index - 1], siblings[index]] = [siblings[index], siblings[index - 1]];
    renderSteps();
  });
  const down = document.createElement('button');
  down.textContent = '↓';
  down.disabled = index === siblings.length - 1;
  down.addEventListener('click', () => {
    [siblings[index + 1], siblings[index]] = [siblings[index], siblings[index + 1]];
    renderSteps();
  });
  const del = document.createElement('button');
  del.textContent = 'delete';
  del.className = 'danger';
  del.addEventListener('click', () => { siblings.splice(index, 1); renderSteps(); });
  head.append(handle, kind, up, down, del);
  card.appendChild(head);

  const body = document.createElement('div');
  body.className = 'step-body';
  body.appendChild(stepBody(step));
  card.appendChild(body);

  return card;
}

function stepBody(step) {
  const frag = document.createDocumentFragment();
  const a = step.args;

  const textField = (key, label, opts = {}) => frag.appendChild(fieldRow(label, () => {
    const input = document.createElement('input');
    input.type = opts.number ? 'number' : 'text';
    input.value = a[key] ?? '';
    if (opts.list) input.setAttribute('list', opts.list);
    if (opts.placeholder) input.placeholder = opts.placeholder;
    input.addEventListener('input', () => {
      a[key] = opts.number ? Number(input.value) : input.value;
      if (opts.onChange) opts.onChange();
    });
    return input;
  }));

  const selectField = (key, label, options, opts = {}) => frag.appendChild(fieldRow(label, () => {
    const sel = document.createElement('select');
    fillOptions(sel, options.map((v) => ({ value: v })));
    sel.value = a[key] ?? options[0];
    sel.addEventListener('change', () => {
      a[key] = sel.value;
      if (opts.onChange) opts.onChange();
    });
    return sel;
  }));

  const checkField = (key, label, dflt) => frag.appendChild(fieldRow(label, () => {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = a[key] ?? dflt;
    input.addEventListener('change', () => { a[key] = input.checked; });
    return input;
  }));

  const tileField = (key, label, opts = {}) => frag.appendChild(fieldRow(label, () => {
    const wrap = document.createElement('div');
    wrap.className = 'tile-pick-row';
    const row = document.createElement('input');
    row.type = 'number';
    const col = document.createElement('input');
    col.type = 'number';
    const target = a[key] || [0, 0];
    row.value = target[0];
    col.value = target[1];
    const sync = () => {
      a[key] = [Number(row.value) || 0, Number(col.value) || 0];
      if (opts.cameraPreview) updateCameraBorder(a[key][0], a[key][1]);
    };
    row.addEventListener('input', sync);
    col.addEventListener('input', sync);
    const pickBtn = document.createElement('button');
    pickBtn.textContent = 'pick on map';
    pickBtn.addEventListener('click', () => {
      armPick((r, c) => { row.value = r; col.value = c; sync(); },
        opts.cameraPreview ? 'camera' : null);
    });
    wrap.append(row, col, pickBtn);
    if (opts.cameraPreview) updateCameraBorder(target[0], target[1]);
    return wrap;
  }));

  const rowColFields = (rowKey, colKey, label, opts = {}) => frag.appendChild(fieldRow(label, () => {
    const wrap = document.createElement('div');
    wrap.className = 'tile-pick-row';
    const row = document.createElement('input');
    row.type = 'number';
    row.value = a[rowKey] ?? 0;
    const col = document.createElement('input');
    col.type = 'number';
    col.value = a[colKey] ?? 0;
    row.addEventListener('input', () => { a[rowKey] = Number(row.value) || 0; if (opts.onChange) opts.onChange(); });
    col.addEventListener('input', () => { a[colKey] = Number(col.value) || 0; if (opts.onChange) opts.onChange(); });
    const pickBtn = document.createElement('button');
    pickBtn.textContent = 'pick on map';
    pickBtn.addEventListener('click', () => {
      armPick((r, c) => {
        row.value = r; col.value = c;
        a[rowKey] = r; a[colKey] = c;
        if (opts.onChange) opts.onChange();
      });
    });
    wrap.append(row, col, pickBtn);
    if (opts.onChange) opts.onChange();
    return wrap;
  }));

  switch (step.kind) {
    case 'face':
      textField('actor', 'Actor', { list: 'dl-actors' });
      selectField('dir', 'Direction', ['N', 'S', 'E', 'W']);
      break;
    case 'wait':
      textField('ms', 'Milliseconds', { number: true });
      break;
    case 'move_actor':
      textField('actor', 'Actor', { list: 'dl-actors' });
      tileField('to', 'Target (row, col)');
      frag.appendChild(hint('Target must share a row or column with the actor\'s position ' +
        'at the time this step runs -- movement is cardinal-only.'));
      break;
    case 'teleport_actor':
      textField('actor', 'Actor', { list: 'dl-actors' });
      tileField('to', 'Target (row, col)');
      frag.appendChild(hint('Instant, no walk animation, any target -- no cardinal-line ' +
        'requirement. Use this instead of move_actor for repositioning while the camera is ' +
        'looking elsewhere (see pan_camera), so the reposition itself is never seen.'));
      break;
    case 'dialogue':
      frag.appendChild(dialogueBody(step));
      break;
    case 'set_flag':
      textField('flag', 'Flag', { list: 'dl-flags', placeholder: 'e.g. has_key' });
      checkField('value', 'Set to true', true);
      break;
    case 'clear_flag':
      textField('flag', 'Flag', { list: 'dl-flags' });
      break;
    case 'give_item':
      textField('item', 'Item id', { list: 'dl-items' });
      textField('qty', 'Qty', { number: true });
      break;
    case 'spawn_actor':
      textField('id', 'Temp actor id', { placeholder: 'e.g. temp_wizard', onChange: renderSpawnedActors });
      textField('npc_id', 'NPC def (npc_id)', { list: 'dl-npc-ids', onChange: renderSpawnedActors });
      rowColFields('row', 'col', 'Spawn at (row, col)', { onChange: renderSpawnedActors });
      selectField('facing', 'Facing', ['N', 'S', 'E', 'W'], { onChange: renderSpawnedActors });
      break;
    case 'despawn_actor':
      textField('id', 'Temp actor id', { list: 'dl-actors' });
      break;
    case 'set_tile':
      textField('layer', 'Tile layer', { list: 'dl-layers' });
      rowColFields('row', 'col', 'Tile (row, col)');
      textField('gid', 'GID', { number: true });
      break;
    case 'pan_camera':
      frag.appendChild(fieldRow('Return to following player', () => {
        const input = document.createElement('input');
        input.type = 'checkbox';
        input.checked = a.to_player ?? false;
        input.addEventListener('change', () => {
          a.to_player = input.checked;
          if (input.checked) hideCameraBorder();
          else updateCameraBorder((a.to || [0, 0])[0], (a.to || [0, 0])[1]);
        });
        return input;
      }));
      tileField('to', 'Pan to (row, col)', { cameraPreview: true });
      textField('duration_ms', 'Duration (ms)', { number: true });
      frag.appendChild(hint('The yellow box shows the real in-game camera view if this step ' +
        'fired right now -- it clamps at map edges the same way the real camera does, so it ' +
        "won't always be perfectly centered on the target near a border."));
      break;
    case 'fade':
      selectField('direction', 'Direction', ['out', 'in']);
      frag.appendChild(fieldRow('Color', () => {
        const wrap = document.createElement('div');
        wrap.className = 'tile-pick-row';
        const hex = document.createElement('input');
        hex.type = 'text';
        hex.value = a.color || '#000000';
        hex.placeholder = '#000000';
        hex.size = 8;
        const picker = document.createElement('input');
        picker.type = 'color';
        picker.value = /^#[0-9a-fA-F]{6}$/.test(a.color) ? a.color : '#000000';
        hex.addEventListener('input', () => {
          a.color = hex.value;
          if (/^#[0-9a-fA-F]{6}$/.test(hex.value)) picker.value = hex.value;
        });
        picker.addEventListener('input', () => {
          a.color = picker.value;
          hex.value = picker.value;
        });
        wrap.append(picker, hex);
        return wrap;
      }));
      textField('duration_ms', 'Duration (ms)', { number: true });
      checkField('see_through', 'Show sprites through the fade', false);
      frag.appendChild(hint('"out" ramps from clear to this color; "in" ramps back to clear. ' +
        'It holds at wherever it lands -- a fade-out stays solid until a later fade-in step or ' +
        'the cutscene ends, it does not clear itself on a timer. Dialogue boxes always draw on ' +
        'top of it either way.'));
      break;
    case 'start_cutscene':
      textField('id', 'Cutscene id', { list: 'dl-cutscenes' });
      frag.appendChild(hint('Jumps to this cutscene outright -- replaces the running one, ' +
        'same map only. Not call-and-return.'));
      break;
  }

  return frag;
}

function fieldRow(label, buildControl) {
  const row = document.createElement('div');
  row.className = 'field-row';
  const l = document.createElement('label');
  l.textContent = label;
  row.appendChild(l);
  row.appendChild(buildControl());
  return row;
}

function hint(text) {
  const el = document.createElement('div');
  el.className = 'hint';
  el.textContent = text;
  return el;
}

function dialogueBody(step) {
  const wrap = document.createElement('div');

  wrap.appendChild(fieldRow('Position', () => {
    const sel = document.createElement('select');
    fillOptions(sel, [
      { value: 'auto', label: 'Auto (follow player, same as ordinary dialogue)' },
      { value: 'top', label: 'Top' },
      { value: 'bottom', label: 'Bottom' },
    ]);
    sel.value = step.args.position || 'auto';
    sel.addEventListener('change', () => { step.args.position = sel.value; });
    return sel;
  }));

  const pagesLabel = document.createElement('div');
  pagesLabel.className = 'hint';
  pagesLabel.textContent = 'Pages (one per line):';
  wrap.appendChild(pagesLabel);

  const textarea = document.createElement('textarea');
  textarea.value = (step.args.pages || []).join('\n');
  textarea.addEventListener('input', () => {
    step.args.pages = textarea.value.split('\n');
  });
  wrap.appendChild(textarea);

  const choicesLabel = document.createElement('div');
  choicesLabel.className = 'hint';
  choicesLabel.textContent = 'Choices (optional -- last page becomes a response prompt):';
  wrap.appendChild(choicesLabel);

  const choicesHost = document.createElement('div');
  wrap.appendChild(choicesHost);

  const renderChoices = () => {
    choicesHost.innerHTML = '';
    const choices = step.args.choices || [];
    choices.forEach((choice, i) => {
      choicesHost.appendChild(choiceBlock(choice, choices, i, renderChoices));
    });
  };
  renderChoices();

  const addChoiceBtn = document.createElement('button');
  addChoiceBtn.textContent = '+ Add choice';
  addChoiceBtn.addEventListener('click', () => {
    if (!step.args.choices) step.args.choices = [];
    step.args.choices.push({ label: 'Yes', then: [] });
    renderChoices();
  });
  wrap.appendChild(addChoiceBtn);

  return wrap;
}

function choiceBlock(choice, siblings, index, rerenderChoices) {
  const block = document.createElement('div');
  block.className = 'choice-block';

  const head = document.createElement('div');
  head.className = 'choice-head';
  const label = document.createElement('input');
  label.value = choice.label;
  label.addEventListener('input', () => { choice.label = label.value; });
  const del = document.createElement('button');
  del.textContent = 'remove choice';
  del.className = 'danger';
  del.addEventListener('click', () => { siblings.splice(index, 1); rerenderChoices(); updateActorDatalist(); });
  head.append(label, del);
  block.appendChild(head);

  const nested = document.createElement('div');
  nested.className = 'nested-steps';
  const renderNested = () => {
    nested.innerHTML = '';
    if (!choice.then) choice.then = [];
    choice.then.forEach((s, i) => nested.appendChild(stepCardForNested(s, choice.then, i, renderNested)));
    const addRow = document.createElement('div');
    addRow.className = 'field-row';
    const sel = document.createElement('select');
    fillOptions(sel, Object.keys(STEP_DEFAULTS).map((v) => ({ value: v })));
    const addBtn = document.createElement('button');
    addBtn.textContent = '+ add step to this choice';
    addBtn.addEventListener('click', () => {
      choice.then.push({ kind: sel.value, args: structuredClone(STEP_DEFAULTS[sel.value]) });
      renderNested();
      updateActorDatalist();
    });
    addRow.append(sel, addBtn);
    nested.appendChild(addRow);
  };
  renderNested();
  block.appendChild(nested);
  return block;
}

function stepCardForNested(step, siblings, index, rerender) {
  const card = document.createElement('div');
  card.className = 'step-card';
  const color = STEP_COLORS[step.kind] || '#5b8dee';
  const [r, g, b] = hexToRgb(color);
  card.style.borderLeftColor = color;
  const head = document.createElement('div');
  head.className = 'step-head';
  head.style.background = `rgba(${r}, ${g}, ${b}, 0.16)`;
  const handle = wireStepDrag(card, siblings, index, rerender);
  const kind = document.createElement('span');
  kind.className = 'kind';
  kind.style.color = color;
  kind.textContent = `${index + 1}. ${step.kind}`;
  const up = document.createElement('button');
  up.textContent = '↑';
  up.disabled = index === 0;
  up.addEventListener('click', () => {
    [siblings[index - 1], siblings[index]] = [siblings[index], siblings[index - 1]];
    rerender();
  });
  const down = document.createElement('button');
  down.textContent = '↓';
  down.disabled = index === siblings.length - 1;
  down.addEventListener('click', () => {
    [siblings[index + 1], siblings[index]] = [siblings[index], siblings[index + 1]];
    rerender();
  });
  const del = document.createElement('button');
  del.textContent = 'delete';
  del.className = 'danger';
  del.addEventListener('click', () => { siblings.splice(index, 1); rerender(); });
  head.append(handle, kind, up, down, del);
  card.appendChild(head);

  const body = document.createElement('div');
  body.className = 'step-body';
  body.appendChild(stepBody(step));
  card.appendChild(body);
  return card;
}

// ── save ─────────────────────────────────────────────────────────────────

async function saveCutscene() {
  const idInput = $('#new-id');
  const id = state.cutsceneExists ? state.data.id : idInput.value.trim();
  if (!id) { setStatus('Cutscene needs an id before saving.', 'err'); return false; }
  state.data.id = id;

  setStatus('Saving…');
  try {
    const res = await fetch(`/api/cutscenes/${encodeURIComponent(id)}`, {
      method: 'POST',
      headers: { 'Content-Type': 'application/json' },
      body: JSON.stringify(state.data),
    });
    const body = await res.json();
    if (!res.ok || !body.ok) throw new Error(body.error || 'save failed');
    setStatus(`Saved data/cutscenes/${id}.yaml`, 'ok');

    if (!state.cutsceneExists) {
      state.cutsceneExists = true;
      state.cutscenes.push({ id, map: state.data.map });
      fillOptions($('#cutscene-picker'), [{ value: '', label: '— new —' }]
        .concat(state.cutscenes.map((c) => ({ value: c.id, label: `${c.id} (${c.map})` }))));
      fillDatalist('#dl-cutscenes', state.cutscenes.map((c) => ({ value: c.id })));
      $('#cutscene-picker').value = id;
      $('#new-id').style.display = 'none';
    }
    return true;
  } catch (err) {
    setStatus(`Save failed: ${err.message}`, 'err');
    return false;
  }
}

init().catch((err) => setStatus(`Failed to start: ${err.message}`, 'err'));
