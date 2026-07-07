"use strict";

const TILE_PX   = 16;
const SCALE     = 3;
const TILE_DRAW = TILE_PX * SCALE;   // 48px on canvas

const SPRITE_SRC = "static/sprites/party_sprites.png";

// Sprite sheet row per character name
const PARTY_SPRITE_ROW = { MELVIN: 0, BILLY: 1, SMELTRUD: 2, POOTS: 3 };

// Sprite sheet cols per facing direction: [frame1_col, frame2_col]
const FACING_COL = { S: [0, 1], N: [2, 3], W: [4, 5], E: [6, 7] };

// ── DOM ───────────────────────────────────────────────────────────────────────
const mapCanvas    = document.getElementById("map");
const spriteCanvas = document.getElementById("sprite-layer");
const mapCtx       = mapCanvas.getContext("2d");
const spriteCtx    = spriteCanvas.getContext("2d");
const logEl        = document.getElementById("log");
const statusEl     = document.getElementById("status");

// Viewport size in tiles — canvas is always exactly this.
const VIEW_COLS = 16;
const VIEW_ROWS = 14;

// ── Tilemaps: tile_name → [col, row] or [col, row, cw_degrees] ───────────────
const TILEMAP = {};   // populated by loadTilemaps()

async function loadTilemaps() {
    const names = ["overworld", "cave", "town"];
    const results = await Promise.all(
        names.map(n => fetch(`static/tilemap_${n}.json`).then(r => r.json()))
    );
    names.forEach((n, i) => { TILEMAP[n] = results[i]; });
}

// ── State ─────────────────────────────────────────────────────────────────────
let mode = "none";   // "hub" | "overworld" | "interior"

let state = {
    row: 0, col: 0,
    rows: VIEW_ROWS, cols: VIEW_COLS,
    sx: 0, sy: 0,
    tilesetImg: null,
    tileGrid: null,
    tilemap: null,
    camRow: 0, camCol: 0,
    camRowSrc: 0, camColSrc: 0, camAnimStart: null,
    activeMember: "BILLY",
    facing: "S",
    history: [],   // last 3 leader positions [{row,col}] for follower chain
};

let hubParty = [];   // [{name, row, col, facing}, ...]

let interior = {
    row: 0, col: 0,
    rows: 0, cols: 0,
    camRow: 0, camCol: 0,
    camRowSrc: 0, camColSrc: 0, camAnimStart: null,
    tilesetImg: null,
    tileGrid: null,
    tilemap: null,
    monsterSpawn: false,
    party: [],
    history: [],   // last 3 leader positions [{row,col}] for follower chain
    facing: "S",
};

// ── Animation ─────────────────────────────────────────────────────────────────
const ANIM_DURATION_MS = 400;
const OVERWORLD_PARTY  = ["MELVIN", "BILLY", "SMELTRUD", "POOTS"];

// Per-member walk animation. Keyed by member name.
const memberAnims = new Map();
let rafId = null;

function startMemberAnim(name, srcRow, srcCol, dstRow, dstCol, facing, now) {
    const existing = memberAnims.get(name);
    let actualSrc = { row: srcRow, col: srcCol };
    if (existing) {
        const t = Math.min(1, (now - existing.startTime) / ANIM_DURATION_MS);
        actualSrc = {
            row: existing.srcRow + (existing.dstRow - existing.srcRow) * t,
            col: existing.srcCol + (existing.dstCol - existing.srcCol) * t,
        };
    }
    memberAnims.set(name, {
        srcRow: actualSrc.row, srcCol: actualSrc.col,
        dstRow, dstCol, facing, startTime: now,
    });
}

function getVisualPos(name, settledRow, settledCol, settledFacing, now) {
    const a = memberAnims.get(name);
    if (!a) return { row: settledRow, col: settledCol, facing: settledFacing, frame: 1 };
    const t = Math.min(1, (now - a.startTime) / ANIM_DURATION_MS);
    return {
        row:    a.srcRow + (a.dstRow - a.srcRow) * t,
        col:    a.srcCol + (a.dstCol - a.srcCol) * t,
        facing: a.facing,
        frame:  t < 0.5 ? 1 : 2,
    };
}

// chain[0] = leader, chain[1..3] = followers (stacked at leader when history short)
function buildChain(leaderRow, leaderCol, history) {
    const chain = [{ row: leaderRow, col: leaderCol }];
    for (let i = 0; i < 3; i++) {
        const idx = history.length - 1 - i;
        chain.push(idx >= 0 ? history[idx] : { row: leaderRow, col: leaderCol });
    }
    return chain;
}

function getVisualCam(settled, src, animStart, now) {
    if (animStart === null) return settled;
    const t = Math.min(1, (now - animStart) / ANIM_DURATION_MS);
    return src + (settled - src) * t;
}

function _camStillAnimating(now) {
    return (state.camAnimStart !== null && now - state.camAnimStart < ANIM_DURATION_MS) ||
           (interior.camAnimStart !== null && now - interior.camAnimStart < ANIM_DURATION_MS);
}

function cancelAllAnims() {
    memberAnims.clear();
    state.camAnimStart = null;
    interior.camAnimStart = null;
    if (rafId !== null) { cancelAnimationFrame(rafId); rafId = null; }
}

function startRafLoop() {
    if (rafId !== null) return;
    rafId = requestAnimationFrame(rafTick);
}

function rafTick(now) {
    const stillAnimating =
        _camStillAnimating(now) ||
        (memberAnims.size > 0 && [...memberAnims.values()].some(a => now - a.startTime < ANIM_DURATION_MS));
    redrawAt(now);
    if (stillAnimating) {
        rafId = requestAnimationFrame(rafTick);
    } else {
        memberAnims.clear();
        rafId = null;
        redrawAt(performance.now());
    }
}

const spriteSheet = new Image();
spriteSheet.src = SPRITE_SRC;
spriteSheet.onload = () => { redraw(); };

// ── Canvas sizing ─────────────────────────────────────────────────────────────
function resizeCanvases(cols, rows) {
    const w = cols * TILE_DRAW;
    const h = rows * TILE_DRAW;
    [mapCanvas, spriteCanvas].forEach(c => {
        c.width  = w; c.height = h;
        c.style.width = w + "px"; c.style.height = h + "px";
    });
}

// ── Camera clamping ───────────────────────────────────────────────────────────
function clampCamera(row, col, totalRows, totalCols) {
    const camRow = Math.max(0,
        Math.min(row - Math.floor(VIEW_ROWS / 2), Math.max(0, totalRows - VIEW_ROWS)));
    const camCol = Math.max(0,
        Math.min(col - Math.floor(VIEW_COLS / 2), Math.max(0, totalCols - VIEW_COLS)));
    return { camRow, camCol };
}

// ── Core tile draw ────────────────────────────────────────────────────────────
function drawTile(ctx, tileName, destCol, destRow, tilesetImg) {
    if (!tilesetImg) return;
    const tilemap = TILEMAP[currentTilemapName()] || {};
    let name = tileName;
    let rot  = 0;
    if (tileName && tileName.includes(":")) {
        [name, rot] = tileName.split(":");
        rot = parseInt(rot, 10);
    }
    const entry = tilemap[name];
    if (!entry) return;  // unknown tile — draw nothing rather than crashing
    const [srcCol, srcRow, entryRot = 0] = entry;
    const totalRot = (rot + entryRot) % 360;

    const sx = srcCol * TILE_PX;
    const sy = srcRow * TILE_PX;
    const dx = destCol * TILE_DRAW;
    const dy = destRow * TILE_DRAW;

    if (totalRot === 0) {
        ctx.drawImage(tilesetImg, sx, sy, TILE_PX, TILE_PX, dx, dy, TILE_DRAW, TILE_DRAW);
    } else {
        ctx.save();
        ctx.translate(dx + TILE_DRAW / 2, dy + TILE_DRAW / 2);
        ctx.rotate(totalRot * Math.PI / 180);
        ctx.drawImage(tilesetImg, sx, sy, TILE_PX, TILE_PX,
                      -TILE_DRAW / 2, -TILE_DRAW / 2, TILE_DRAW, TILE_DRAW);
        ctx.restore();
    }
}

// Returns the tilemap key for the current mode.
function currentTilemapName() {
    if (mode === "hub") return "town";
    if (mode === "interior") return interior.monsterSpawn ? "cave" : "town";
    return "overworld";
}

// ── Tile grid draw ────────────────────────────────────────────────────────────
function drawTileGrid(ctx, grid, tilesetImg, camRow, camCol) {
    if (!grid || !tilesetImg) return;
    ctx.imageSmoothingEnabled = false;
    const baseCamRow = Math.floor(camRow);
    const baseCamCol = Math.floor(camCol);
    const fracRow = camRow - baseCamRow;
    const fracCol = camCol - baseCamCol;
    ctx.save();
    ctx.translate(Math.round(-fracCol * TILE_DRAW), Math.round(-fracRow * TILE_DRAW));
    for (let vr = 0; vr <= VIEW_ROWS; vr++) {    // +1 row to fill fractional gap
        const gr = vr + baseCamRow;
        if (gr < 0 || gr >= grid.length) continue;
        for (let vc = 0; vc <= VIEW_COLS; vc++) { // +1 col to fill fractional gap
            const gc = vc + baseCamCol;
            if (gc < 0 || gc >= grid[gr].length) continue;
            const cell = grid[gr][gc];
            if (Array.isArray(cell)) {
                drawTile(ctx, cell[0], vc, vr, tilesetImg);
                drawTile(ctx, cell[1], vc, vr, tilesetImg);
            } else {
                drawTile(ctx, cell, vc, vr, tilesetImg);
            }
        }
    }
    ctx.restore();
}

// ── Sprite helpers ────────────────────────────────────────────────────────────
function deriveFacing(prevRow, prevCol, newRow, newCol) {
    if (newRow > prevRow) return "S";
    if (newRow < prevRow) return "N";
    if (newCol > prevCol) return "E";
    if (newCol < prevCol) return "W";
    return null;  // no movement — caller keeps current facing
}

function drawMemberSprite(ctx, name, destCol, destRow, facing, frame) {
    const sprRow = PARTY_SPRITE_ROW[name] ?? 0;
    const cols = FACING_COL[facing] ?? FACING_COL["S"];
    const sprCol = cols[frame === 2 ? 1 : 0];
    ctx.drawImage(spriteSheet,
        sprCol * TILE_PX, sprRow * TILE_PX, TILE_PX, TILE_PX,
        Math.round(destCol * TILE_DRAW), Math.round(destRow * TILE_DRAW), TILE_DRAW, TILE_DRAW);
}

// ── Sprite draws ──────────────────────────────────────────────────────────────
function drawSprite(now, camRow, camCol) {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    spriteCtx.imageSmoothingEnabled = false;
    const chain = buildChain(state.row, state.col, state.history);
    // Draw back-to-front so Melvin is on top when stacked
    for (let i = OVERWORLD_PARTY.length - 1; i >= 0; i--) {
        const name = OVERWORLD_PARTY[i];
        const vp = getVisualPos(name, chain[i].row, chain[i].col, state.facing, now);
        const destCol = vp.col - camCol;
        const destRow = vp.row - camRow;
        if (destRow >= -1 && destRow < VIEW_ROWS + 1 && destCol >= -1 && destCol < VIEW_COLS + 1)
            drawMemberSprite(spriteCtx, name, destCol, destRow, vp.facing, vp.frame);
    }
}

function drawHubSprites(now) {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    spriteCtx.imageSmoothingEnabled = false;
    for (const m of hubParty) {
        const vp = getVisualPos(m.name, m.row, m.col, m.facing, now);
        drawMemberSprite(spriteCtx, m.name,
                         vp.col - state.camCol, vp.row - state.camRow,
                         vp.facing, vp.frame);
    }
}

function drawInteriorSprites(now, camRow, camCol) {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    spriteCtx.imageSmoothingEnabled = false;
    if (!interior.party.length) return;
    const chain = buildChain(interior.row, interior.col, interior.history);
    // Draw back-to-front so leader is on top when stacked
    for (let i = interior.party.length - 1; i >= 0; i--) {
        const name = interior.party[i];
        const settled = chain[i] || chain[0];
        const vp = getVisualPos(name, settled.row, settled.col, interior.facing, now);
        const destCol = vp.col - camCol;
        const destRow = vp.row - camRow;
        if (destRow >= -1 && destRow < VIEW_ROWS + 1 && destCol >= -1 && destCol < VIEW_COLS + 1)
            drawMemberSprite(spriteCtx, name, destCol, destRow, vp.facing, vp.frame);
    }
}

// ── Mode-aware redraw ─────────────────────────────────────────────────────────
function redrawAt(now) {
    mapCtx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);
    if (mode === "interior") {
        const camR = getVisualCam(interior.camRow, interior.camRowSrc, interior.camAnimStart, now);
        const camC = getVisualCam(interior.camCol, interior.camColSrc, interior.camAnimStart, now);
        drawTileGrid(mapCtx, interior.tileGrid, interior.tilesetImg, camR, camC);
        drawInteriorSprites(now, camR, camC);
    } else if (mode === "hub") {
        drawTileGrid(mapCtx, state.tileGrid, state.tilesetImg,
                     state.camRow, state.camCol);
        drawHubSprites(now);
    } else if (mode === "overworld") {
        const camR = getVisualCam(state.camRow, state.camRowSrc, state.camAnimStart, now);
        const camC = getVisualCam(state.camCol, state.camColSrc, state.camAnimStart, now);
        drawTileGrid(mapCtx, state.tileGrid, state.tilesetImg, camR, camC);
        drawSprite(now, camR, camC);
    }
}

function redraw() { redrawAt(performance.now()); }


// ── Tileset loader ────────────────────────────────────────────────────────────
function loadTileset(url, onLoad) {
    const img = new Image();
    img.onload = onLoad;
    img.src = url;
    return img;
}

// ── Log helper ────────────────────────────────────────────────────────────────
function appendLog(cls, text) {
    const div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 200) logEl.removeChild(logEl.firstChild);
}

// ── SSE event handlers ────────────────────────────────────────────────────────
function handleHubInit(e) {
    mode = "hub";
    state.rows = e.rows; state.cols = e.cols;
    state.tileGrid = e.tile_grid || null;
    hubParty = (e.party || []).map(m => ({ name: m.name, row: m.row, col: m.col, facing: "S" }));
    const { camRow, camCol } = clampCamera(0, 0, state.rows, state.cols);
    state.camRow = camRow; state.camCol = camCol;
    resizeCanvases(VIEW_COLS, VIEW_ROWS);
    if (e.tileset_url) {
        state.tilesetImg = loadTileset(e.tileset_url, () => redraw());
    } else {
        mapCtx.fillStyle = "#2a3a2a";
        mapCtx.fillRect(0, 0, mapCanvas.width, mapCanvas.height);
        redraw();
    }
    statusEl.textContent = "Front House — party roaming";
}

function handleHubMove(e) {
    const now = performance.now();
    const m = hubParty.find(p => p.name === e.name);
    if (m) {
        const f = e.direction || deriveFacing(m.row, m.col, e.row, e.col);
        if (f) m.facing = f;
        startMemberAnim(m.name, m.row, m.col, e.row, e.col, m.facing, now);
        m.row = e.row; m.col = e.col;
    }
    startRafLoop();
    statusEl.textContent = `HUB — ${e.name} (${e.row},${e.col})  tick=${e.tick}`;
}

function handleInit(e) {
    mode = "overworld";
    state.sx = e.sx; state.sy = e.sy;
    state.row = e.row; state.col = e.col;
    state.rows = e.rows; state.cols = e.cols;
    state.tileGrid = e.tile_grid || null;
    state.activeMember = e.member || "BILLY";
    state.facing = "S";
    state.history = [];
    const { camRow, camCol } = clampCamera(e.row, e.col, e.rows, e.cols);
    state.camRow = camRow; state.camCol = camCol;
    resizeCanvases(VIEW_COLS, VIEW_ROWS);
    if (e.tileset_url) {
        state.tilesetImg = loadTileset(e.tileset_url, () => redraw());
    }
    statusEl.textContent = `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleMove(e) {
    const now = performance.now();
    const f = deriveFacing(state.row, state.col, e.row, e.col) || state.facing;
    state.facing = f;
    if (e.member) state.activeMember = e.member;

    const oldChain = buildChain(state.row, state.col, state.history);

    state.history.push({ row: state.row, col: state.col });
    if (state.history.length > 3) state.history.shift();
    state.row = e.row; state.col = e.col;
    const newChain = buildChain(state.row, state.col, state.history);

    const { camRow, camCol } = clampCamera(e.row, e.col, state.rows, state.cols);
    state.camRowSrc = getVisualCam(state.camRow, state.camRowSrc, state.camAnimStart, now);
    state.camColSrc = getVisualCam(state.camCol, state.camColSrc, state.camAnimStart, now);
    state.camAnimStart = now;
    state.camRow = camRow; state.camCol = camCol;

    OVERWORLD_PARTY.forEach((name, i) => {
        startMemberAnim(name, oldChain[i].row, oldChain[i].col,
                        newChain[i].row, newChain[i].col, f, now);
    });
    startRafLoop();
    statusEl.textContent = `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleScreen(e) {
    cancelAllAnims();
    state.history = [];
    state.sx = e.sx; state.sy = e.sy;
    state.row = e.row; state.col = e.col;
    state.rows = e.rows; state.cols = e.cols;
    state.tileGrid = e.tile_grid || null;
    const { camRow, camCol } = clampCamera(e.row, e.col, e.rows, e.cols);
    state.camRow = camRow; state.camCol = camCol;
    resizeCanvases(VIEW_COLS, VIEW_ROWS);
    if (e.tileset_url) {
        state.tilesetImg = loadTileset(e.tileset_url, () => redraw());
    }
    appendLog("screen", `⟶ crossed to screen (${e.sx},${e.sy})`);
    statusEl.textContent = `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleInteriorInit(e) {
    cancelAllAnims();
    mode = "interior";
    interior.row = e.row; interior.col = e.col;
    interior.rows = e.rows; interior.cols = e.cols;
    interior.monsterSpawn = e.monster_spawn;
    interior.tileGrid = e.tile_grid || null;
    interior.tilesetImg = null;
    interior.party = e.party || [];
    interior.history = [];
    interior.facing = "S";
    const { camRow, camCol } = clampCamera(e.row, e.col, e.rows, e.cols);
    interior.camRow = camRow; interior.camCol = camCol;
    interior.camRowSrc = camRow; interior.camColSrc = camCol; interior.camAnimStart = null;
    resizeCanvases(VIEW_COLS, VIEW_ROWS);
    const kind = e.monster_spawn ? "cave" : "town";
    if (e.tileset_url) {
        interior.tilesetImg = loadTileset(e.tileset_url, () => redraw());
    } else {
        mapCtx.fillStyle = e.monster_spawn ? "#111" : "#2a3a2a";
        mapCtx.fillRect(0, 0, mapCanvas.width, mapCanvas.height);
    }
    appendLog("screen", `↓ entered ${kind}`);
    statusEl.textContent = `${kind} — row=${e.row} col=${e.col}`;
}

function handleInteriorMove(e) {
    const now = performance.now();
    const f = deriveFacing(interior.row, interior.col, e.row, e.col) || interior.facing;
    interior.facing = f;

    const oldChain = buildChain(interior.row, interior.col, interior.history);

    interior.history.push({ row: interior.row, col: interior.col });
    if (interior.history.length > 3) interior.history.shift();
    interior.row = e.row; interior.col = e.col;
    const newChain = buildChain(interior.row, interior.col, interior.history);

    const { camRow, camCol } = clampCamera(e.row, e.col, interior.rows, interior.cols);
    interior.camRowSrc = getVisualCam(interior.camRow, interior.camRowSrc, interior.camAnimStart, now);
    interior.camColSrc = getVisualCam(interior.camCol, interior.camColSrc, interior.camAnimStart, now);
    interior.camAnimStart = now;
    interior.camRow = camRow; interior.camCol = camCol;

    interior.party.forEach((name, i) => {
        const src = oldChain[i] || oldChain[0];
        const dst = newChain[i] || newChain[0];
        startMemberAnim(name, src.row, src.col, dst.row, dst.col, f, now);
    });
    startRafLoop();
    statusEl.textContent = `interior — row=${e.row} col=${e.col}`;
}

function handleInteriorExit(e) {
    cancelAllAnims();
    mode = "overworld";
    appendLog("screen", `↑ exited interior`);
    statusEl.textContent = `overworld — screen (${state.sx},${state.sy})`;
    resizeCanvases(VIEW_COLS, VIEW_ROWS);
    redraw();
}

function handleEvent(raw) {
    let e;
    try { e = JSON.parse(raw); } catch { return; }
    switch (e.type) {
        case "hub_init":       handleHubInit(e);      break;
        case "hub_move":       handleHubMove(e);      break;
        case "init":           handleInit(e);         break;
        case "move":           handleMove(e);         break;
        case "screen":         handleScreen(e);       break;
        case "interior_init":  handleInteriorInit(e); break;
        case "interior_move":  handleInteriorMove(e); break;
        case "interior_exit":  handleInteriorExit(e); break;
        case "propose":  appendLog("propose", e.text); break;
        case "vote":     appendLog("vote",    e.text); break;
        case "resolve":  appendLog("resolve", e.text); break;
    }
}

// ── SSE connection with auto-reconnect ────────────────────────────────────────
function connect() {
    statusEl.textContent = "connecting…";
    const es = new EventSource("/events");
    es.onopen = () => { statusEl.textContent = "connected — waiting for loop…"; };
    es.onmessage = (ev) => { handleEvent(ev.data); };
    es.onerror = () => {
        statusEl.textContent = "disconnected — reconnecting…";
        es.close();
        setTimeout(connect, 2000);
    };
}

// ── Boot ──────────────────────────────────────────────────────────────────────
loadTilemaps().then(() => {
    resizeCanvases(VIEW_COLS, VIEW_ROWS);
    connect();
});
