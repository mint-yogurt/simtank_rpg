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

const state = {
  maps: [],
  cutscenes: [],
  flags: [],
  items: [],
  npcIds: [],
  mapMeta: null,
  data: null,       // { id, map, trigger, steps }
  zoom: 1,
  pick: null,       // callback(row, col) while a "pick on map" is armed
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
  wireMapClicks();
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

  $('#zoom-picker').addEventListener('change', (e) => {
    state.zoom = parseFloat(e.target.value);
    applyZoom();
  });

  $('#save-btn').addEventListener('click', saveCutscene);
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
  const img = $('#map-bg');
  img.src = `${meta.background_url}?t=${Date.now()}`;
  img.onload = () => { applyZoom(); renderOverlay(); };
  fillDatalist('#dl-layers', meta.tile_layers.map((l) => ({ value: l })));
}

function applyZoom() {
  const img = $('#map-bg');
  const w = img.naturalWidth * state.zoom;
  const h = img.naturalHeight * state.zoom;
  img.style.width = `${w}px`;
  img.style.height = `${h}px`;
  $('#map-overlay').style.width = `${w}px`;
  $('#map-overlay').style.height = `${h}px`;
  renderOverlay();
}

// ── map overlay + tile picking ───────────────────────────────────────────

function renderOverlay() {
  const meta = state.mapMeta;
  const overlay = $('#map-overlay');
  overlay.innerHTML = '';
  if (!meta) return;
  const px = meta.tile_px * state.zoom;

  for (const a of meta.actors) {
    const el = document.createElement('div');
    el.className = 'actor-marker';
    el.style.left = `${a.col * px}px`;
    el.style.top = `${a.row * px}px`;
    el.textContent = a.name;
    el.title = `${a.name} (${a.kind}) @ ${a.row},${a.col}`;
    overlay.appendChild(el);
  }
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

function actorNames() {
  const names = (state.mapMeta ? state.mapMeta.actors.map((a) => a.name) : []);
  const spawned = [];
  const scan = (steps) => {
    for (const s of steps || []) {
      if (s.kind === 'spawn_actor' && s.args.id) spawned.push(s.args.id);
      if (s.kind === 'dialogue') {
        for (const c of s.args.choices || []) scan(c.then);
      }
    }
  };
  scan(state.data ? state.data.steps : []);
  return [...new Set([...names, ...spawned])];
}

function wireMapClicks() {
  $('#map-wrapper').addEventListener('click', (e) => {
    if (!state.pick || !state.mapMeta) return;
    const rect = $('#map-bg').getBoundingClientRect();
    const px = state.mapMeta.tile_px * state.zoom;
    const col = Math.floor((e.clientX - rect.left) / px);
    const row = Math.floor((e.clientY - rect.top) / px);
    const cb = state.pick;
    disarmPick();
    cb(row, col);
  });
  $('#pick-cancel').addEventListener('click', disarmPick);
}

function armPick(cb) {
  state.pick = cb;
  $('#pick-banner').classList.remove('hidden');
}

function disarmPick() {
  state.pick = null;
  $('#pick-banner').classList.add('hidden');
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
  fillOptions(eventSelect, ['map_load', 'tile', 'npc_talk'].map((v) => ({ value: v })));
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
  dialogue: { pages: ['...'] },
  set_flag: { flag: '', value: true },
  clear_flag: { flag: '' },
  give_item: { item: '', qty: 1 },
  spawn_actor: { id: '', npc_id: '', row: 0, col: 0, facing: 'S' },
  despawn_actor: { id: '' },
  set_tile: { layer: '', row: 0, col: 0, gid: 0 },
  pan_camera: { to: [0, 0], duration_ms: 400 },
  start_cutscene: { id: '' },
};

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
}

function stepCard(step, siblings, index) {
  const card = document.createElement('div');
  card.className = 'step-card';

  const head = document.createElement('div');
  head.className = 'step-head';
  const kind = document.createElement('span');
  kind.className = 'kind';
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
  head.append(kind, up, down, del);
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
    });
    return input;
  }));

  const selectField = (key, label, options) => frag.appendChild(fieldRow(label, () => {
    const sel = document.createElement('select');
    fillOptions(sel, options.map((v) => ({ value: v })));
    sel.value = a[key] ?? options[0];
    sel.addEventListener('change', () => { a[key] = sel.value; });
    return sel;
  }));

  const checkField = (key, label, dflt) => frag.appendChild(fieldRow(label, () => {
    const input = document.createElement('input');
    input.type = 'checkbox';
    input.checked = a[key] ?? dflt;
    input.addEventListener('change', () => { a[key] = input.checked; });
    return input;
  }));

  const tileField = (key, label) => frag.appendChild(fieldRow(label, () => {
    const wrap = document.createElement('div');
    wrap.className = 'tile-pick-row';
    const row = document.createElement('input');
    row.type = 'number';
    const col = document.createElement('input');
    col.type = 'number';
    const target = a[key] || [0, 0];
    row.value = target[0];
    col.value = target[1];
    const sync = () => { a[key] = [Number(row.value) || 0, Number(col.value) || 0]; };
    row.addEventListener('input', sync);
    col.addEventListener('input', sync);
    const pickBtn = document.createElement('button');
    pickBtn.textContent = 'pick on map';
    pickBtn.addEventListener('click', () => {
      armPick((r, c) => { row.value = r; col.value = c; sync(); });
    });
    wrap.append(row, col, pickBtn);
    return wrap;
  }));

  const rowColFields = (rowKey, colKey, label) => frag.appendChild(fieldRow(label, () => {
    const wrap = document.createElement('div');
    wrap.className = 'tile-pick-row';
    const row = document.createElement('input');
    row.type = 'number';
    row.value = a[rowKey] ?? 0;
    const col = document.createElement('input');
    col.type = 'number';
    col.value = a[colKey] ?? 0;
    row.addEventListener('input', () => { a[rowKey] = Number(row.value) || 0; });
    col.addEventListener('input', () => { a[colKey] = Number(col.value) || 0; });
    const pickBtn = document.createElement('button');
    pickBtn.textContent = 'pick on map';
    pickBtn.addEventListener('click', () => {
      armPick((r, c) => {
        row.value = r; col.value = c;
        a[rowKey] = r; a[colKey] = c;
      });
    });
    wrap.append(row, col, pickBtn);
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
      textField('id', 'Temp actor id', { placeholder: 'e.g. temp_wizard' });
      textField('npc_id', 'NPC def (npc_id)', { list: 'dl-npc-ids' });
      rowColFields('row', 'col', 'Spawn at (row, col)');
      selectField('facing', 'Facing', ['N', 'S', 'E', 'W']);
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
      checkField('to_player', 'Return to following player', false);
      tileField('to', 'Pan to (row, col)');
      textField('duration_ms', 'Duration (ms)', { number: true });
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
  const head = document.createElement('div');
  head.className = 'step-head';
  const kind = document.createElement('span');
  kind.className = 'kind';
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
  head.append(kind, up, down, del);
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
  if (!id) { setStatus('Cutscene needs an id before saving.', 'err'); return; }
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
  } catch (err) {
    setStatus(`Save failed: ${err.message}`, 'err');
  }
}

init().catch((err) => setStatus(`Failed to start: ${err.message}`, 'err'));
