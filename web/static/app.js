"use strict";

const TILE_PX = 16;
const SCALE = 3;                      // 16px tiles → 48px on canvas
const TILE_DRAW = TILE_PX * SCALE;    // 48

const SPRITE_SRC = "static/sprites/party_sprites.png";

// Sprite sheet row per character name (partysprites.txt: MELVIN=0, BILLY=1, SMELTRUD=2, POOTS=3)
const PARTY_SPRITE_ROW = { MELVIN: 0, BILLY: 1, SMELTRUD: 2, POOTS: 3 };

// ── DOM ───────────────────────────────────────────────────────────────────────
const mapCanvas    = document.getElementById("map");
const spriteCanvas = document.getElementById("sprite-layer");
const mapCtx       = mapCanvas.getContext("2d");
const spriteCtx    = spriteCanvas.getContext("2d");
const logEl        = document.getElementById("log");
const statusEl     = document.getElementById("status");

// ── State ─────────────────────────────────────────────────────────────────────
let mode = "none";   // "hub" | "overworld"

// Overworld state (single party blob)
let state = {
    row: 0, col: 0,
    rows: 14, cols: 16,
    sx: 0, sy: 0,
    screenImg: null,
};

// Hub state (four individual members)
let hubParty = [];   // [{name, row, col}, ...]

const spriteSheet = new Image();
spriteSheet.src = SPRITE_SRC;
spriteSheet.onload = () => { redraw(); };

// ── Resize canvases for current screen dimensions ─────────────────────────────
function resizeCanvases() {
    const w = state.cols * TILE_DRAW;
    const h = state.rows * TILE_DRAW;
    [mapCanvas, spriteCanvas].forEach(c => {
        c.width  = w;
        c.height = h;
        c.style.width  = w + "px";
        c.style.height = h + "px";
    });
}

// ── Draw screen background PNG ────────────────────────────────────────────────
function drawMap() {
    if (!state.screenImg) return;
    mapCtx.imageSmoothingEnabled = false;
    mapCtx.drawImage(state.screenImg, 0, 0,
        state.cols * TILE_DRAW, state.rows * TILE_DRAW);
}

// ── Draw overworld single party sprite ───────────────────────────────────────
function drawSprite() {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    // BILLY S1 standing frame as the overworld party marker
    const sx = 0 * TILE_PX;
    const sy = 1 * TILE_PX;
    spriteCtx.imageSmoothingEnabled = false;
    spriteCtx.drawImage(spriteSheet, sx, sy, TILE_PX, TILE_PX,
                        state.col * TILE_DRAW, state.row * TILE_DRAW,
                        TILE_DRAW, TILE_DRAW);
}

// ── Draw all four hub party sprites ──────────────────────────────────────────
function drawHubSprites() {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    spriteCtx.imageSmoothingEnabled = false;
    for (const m of hubParty) {
        const sprRow = PARTY_SPRITE_ROW[m.name] ?? 0;
        spriteCtx.drawImage(
            spriteSheet,
            0 * TILE_PX, sprRow * TILE_PX, TILE_PX, TILE_PX,  // S1 frame
            m.col * TILE_DRAW, m.row * TILE_DRAW, TILE_DRAW, TILE_DRAW
        );
    }
}

// ── Mode-aware full redraw ────────────────────────────────────────────────────
function redraw() {
    drawMap();
    if (mode === "hub") {
        drawHubSprites();
    } else {
        drawSprite();
    }
}

// ── Stub: frame-switching will hook in here later ─────────────────────────────
function updateSpriteFrame(_direction) {
    // TODO: select correct tx from partysprites.txt based on direction + step parity
}

// ── Load a screen PNG and redraw ──────────────────────────────────────────────
function loadScreen(url) {
    const img = new Image();
    img.onload = () => {
        state.screenImg = img;
        redraw();
    };
    img.src = url;
}

// ── Log helper ────────────────────────────────────────────────────────────────
function appendLog(cls, text) {
    const div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    // Keep log from growing without bound
    while (logEl.children.length > 200) logEl.removeChild(logEl.firstChild);
}

// ── SSE event handlers ────────────────────────────────────────────────────────
function handleHubInit(e) {
    mode = "hub";
    state.rows = e.rows;
    state.cols = e.cols;
    hubParty = (e.party || []).map(m => ({ name: m.name, row: m.row, col: m.col }));
    resizeCanvases();
    if (e.screen_url) {
        loadScreen(e.screen_url);
    } else {
        mapCtx.fillStyle = "#2a3a2a";
        mapCtx.fillRect(0, 0, mapCanvas.width, mapCanvas.height);
        drawHubSprites();
    }
    statusEl.textContent = "HUB — party roaming";
}

function handleHubMove(e) {
    const m = hubParty.find(p => p.name === e.name);
    if (m) { m.row = e.row; m.col = e.col; }
    drawHubSprites();
    statusEl.textContent = `HUB — ${e.name} (${e.row},${e.col})  tick=${e.tick}`;
}

function handleInit(e) {
    mode = "overworld";
    state.sx   = e.sx;   state.sy   = e.sy;
    state.row  = e.row;  state.col  = e.col;
    state.rows = e.rows; state.cols = e.cols;
    resizeCanvases();
    loadScreen(e.screen_url);
    statusEl.textContent =
        `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleMove(e) {
    state.row = e.row;
    state.col = e.col;
    updateSpriteFrame(null);
    drawSprite();
    statusEl.textContent =
        `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleScreen(e) {
    state.sx   = e.sx;   state.sy   = e.sy;
    state.row  = e.row;  state.col  = e.col;
    state.rows = e.rows; state.cols = e.cols;
    resizeCanvases();
    loadScreen(e.screen_url);
    appendLog("screen", `⟶ crossed to screen (${e.sx},${e.sy})`);
    statusEl.textContent =
        `screen (${e.sx},${e.sy})  pos row=${e.row} col=${e.col}`;
}

function handleEvent(raw) {
    let e;
    try { e = JSON.parse(raw); } catch { return; }
    switch (e.type) {
        case "hub_init": handleHubInit(e); break;
        case "hub_move": handleHubMove(e); break;
        case "init":     handleInit(e);    break;
        case "move":     handleMove(e);    break;
        case "screen":   handleScreen(e);  break;
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

connect();
