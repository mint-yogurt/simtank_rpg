"use strict";

const TILE_PX   = 16;
const SCALE     = 3;
const TILE_DRAW = TILE_PX * SCALE;   // 48px on canvas

const SPRITE_SRC = "static/sprites/party_sprites.png";

// Sprite sheet row per character name
const PARTY_SPRITE_ROW = { MELVIN: 0, BILLY: 1, SMELTRUD: 2, POOTS: 3 };

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
};

let hubParty = [];   // [{name, row, col}, ...]

let interior = {
    row: 0, col: 0,
    rows: 0, cols: 0,
    camRow: 0, camCol: 0,
    tilesetImg: null,
    tileGrid: null,
    tilemap: null,
    monsterSpawn: false,
};

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
    for (let vr = 0; vr < VIEW_ROWS; vr++) {
        const gr = vr + camRow;
        if (gr >= grid.length) break;
        for (let vc = 0; vc < VIEW_COLS; vc++) {
            const gc = vc + camCol;
            if (gc >= grid[gr].length) break;
            const cell = grid[gr][gc];
            if (Array.isArray(cell)) {
                // [ground, overlay] — transparent-bg overlay tile; draw ground first
                drawTile(ctx, cell[0], vc, vr, tilesetImg);
                drawTile(ctx, cell[1], vc, vr, tilesetImg);
            } else {
                drawTile(ctx, cell, vc, vr, tilesetImg);
            }
        }
    }
}

// ── Sprite draws ──────────────────────────────────────────────────────────────
function drawSprite() {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    // BILLY S1 standing frame as overworld party marker (Issue #2 will add walk cycles)
    spriteCtx.imageSmoothingEnabled = false;
    const destCol = state.col - state.camCol;
    const destRow = state.row - state.camRow;
    spriteCtx.drawImage(spriteSheet, 0, TILE_PX, TILE_PX, TILE_PX,
                        destCol * TILE_DRAW, destRow * TILE_DRAW, TILE_DRAW, TILE_DRAW);
}

function drawHubSprites() {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    spriteCtx.imageSmoothingEnabled = false;
    for (const m of hubParty) {
        const sprRow = PARTY_SPRITE_ROW[m.name] ?? 0;
        spriteCtx.drawImage(
            spriteSheet,
            0, sprRow * TILE_PX, TILE_PX, TILE_PX,
            (m.col - state.camCol) * TILE_DRAW,
            (m.row - state.camRow) * TILE_DRAW,
            TILE_DRAW, TILE_DRAW
        );
    }
}

function drawInteriorSprite() {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    const relRow = interior.row - interior.camRow;
    const relCol = interior.col - interior.camCol;
    if (relRow < 0 || relRow >= VIEW_ROWS || relCol < 0 || relCol >= VIEW_COLS) return;
    spriteCtx.imageSmoothingEnabled = false;
    spriteCtx.drawImage(spriteSheet, 0, TILE_PX, TILE_PX, TILE_PX,
                        relCol * TILE_DRAW, relRow * TILE_DRAW, TILE_DRAW, TILE_DRAW);
}

// ── Mode-aware redraw ─────────────────────────────────────────────────────────
function redraw() {
    mapCtx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);
    if (mode === "interior") {
        drawTileGrid(mapCtx, interior.tileGrid, interior.tilesetImg,
                     interior.camRow, interior.camCol);
        drawInteriorSprite();
    } else if (mode === "hub") {
        drawTileGrid(mapCtx, state.tileGrid, state.tilesetImg,
                     state.camRow, state.camCol);
        drawHubSprites();
    } else if (mode === "overworld") {
        drawTileGrid(mapCtx, state.tileGrid, state.tilesetImg,
                     state.camRow, state.camCol);
        drawSprite();
    }
}

// ── Stub: walk cycle frame selection (Issue #2) ───────────────────────────────
function updateSpriteFrame(_direction) { }

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
    hubParty = (e.party || []).map(m => ({ name: m.name, row: m.row, col: m.col }));
    const { camRow, camCol } = clampCamera(0, 0, state.rows, state.cols);
    state.camRow = camRow; state.camCol = camCol;
    resizeCanvases(VIEW_COLS, VIEW_ROWS);
    if (e.tileset_url) {
        state.tilesetImg = loadTileset(e.tileset_url, () => redraw());
    } else {
        mapCtx.fillStyle = "#2a3a2a";
        mapCtx.fillRect(0, 0, mapCanvas.width, mapCanvas.height);
        drawHubSprites();
    }
    statusEl.textContent = "Front House — party roaming";
}

function handleHubMove(e) {
    const m = hubParty.find(p => p.name === e.name);
    if (m) { m.row = e.row; m.col = e.col; }
    redraw();
    statusEl.textContent = `HUB — ${e.name} (${e.row},${e.col})  tick=${e.tick}`;
}

function handleInit(e) {
    mode = "overworld";
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
    statusEl.textContent = `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleMove(e) {
    state.row = e.row; state.col = e.col;
    const { camRow, camCol } = clampCamera(e.row, e.col, state.rows, state.cols);
    state.camRow = camRow; state.camCol = camCol;
    updateSpriteFrame(null);
    redraw();
    statusEl.textContent = `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleScreen(e) {
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
    mode = "interior";
    interior.row = e.row; interior.col = e.col;
    interior.rows = e.rows; interior.cols = e.cols;
    interior.monsterSpawn = e.monster_spawn;
    interior.tileGrid = e.tile_grid || null;
    interior.tilesetImg = null;
    const { camRow, camCol } = clampCamera(e.row, e.col, e.rows, e.cols);
    interior.camRow = camRow; interior.camCol = camCol;
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
    interior.row = e.row; interior.col = e.col;
    const { camRow, camCol } = clampCamera(e.row, e.col, interior.rows, interior.cols);
    interior.camRow = camRow; interior.camCol = camCol;
    redraw();
    statusEl.textContent = `interior — row=${e.row} col=${e.col}`;
}

function handleInteriorExit(e) {
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
