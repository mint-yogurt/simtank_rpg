"use strict";

const TILE_PX = 16;
const SCALE = 3;                      // 16px tiles → 48px on canvas
const TILE_DRAW = TILE_PX * SCALE;    // 48

// Sprite sheet: billyS1 at tx=0, ty=1 (standing-south, first frame)
const SPRITE_TX = 0;
const SPRITE_TY = 1;
const SPRITE_SRC = "static/sprites/party_sprites.png";

// ── DOM ───────────────────────────────────────────────────────────────────────
const mapCanvas    = document.getElementById("map");
const spriteCanvas = document.getElementById("sprite-layer");
const mapCtx       = mapCanvas.getContext("2d");
const spriteCtx    = spriteCanvas.getContext("2d");
const logEl        = document.getElementById("log");
const statusEl     = document.getElementById("status");

// ── State ─────────────────────────────────────────────────────────────────────
let state = {
    row: 0, col: 0,
    rows: 14, cols: 16,
    sx: 0, sy: 0,
    screenImg: null,   // HTMLImageElement for current screen PNG
};

const spriteSheet = new Image();
spriteSheet.src = SPRITE_SRC;
spriteSheet.onload = () => { drawSprite(); };

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

// ── Draw party sprite ─────────────────────────────────────────────────────────
function drawSprite() {
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;
    const sx = SPRITE_TX * TILE_PX;
    const sy = SPRITE_TY * TILE_PX;
    const dx = state.col * TILE_DRAW;
    const dy = state.row * TILE_DRAW;
    spriteCtx.imageSmoothingEnabled = false;
    spriteCtx.drawImage(spriteSheet, sx, sy, TILE_PX, TILE_PX,
                        dx, dy, TILE_DRAW, TILE_DRAW);
}

// ── Stub: frame-switching will hook in here later ─────────────────────────────
function updateSpriteFrame(_direction) {
    // TODO: select correct tx from partysprites.txt based on direction + step parity
}

// ── Load a screen PNG and draw it ─────────────────────────────────────────────
function loadScreen(url, callback) {
    const img = new Image();
    img.onload = () => {
        state.screenImg = img;
        drawMap();
        drawSprite();
        if (callback) callback();
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
function handleInit(e) {
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
    updateSpriteFrame(null);  // stub
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
        case "init":    handleInit(e); break;
        case "move":    handleMove(e); break;
        case "screen":  handleScreen(e); break;
        case "propose": appendLog("propose", e.text); break;
        case "vote":    appendLog("vote",    e.text); break;
        case "resolve": appendLog("resolve", e.text); break;
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
