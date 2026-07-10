"use strict";

// ── Constants ─────────────────────────────────────────────────────────────────
const TILE_PX   = 16;
const SCALE     = 3;
const TILE_DRAW = TILE_PX * SCALE;   // 48px on canvas

const VIEW_COLS = 16;
const VIEW_ROWS = 14;

const SPRITE_SRC = "assets/sprites/party_sprites.png";

const PARTY_SPRITE_ROW = { MELVIN: 0, BILLY: 1, SMELTRUD: 2, POOTS: 3 };
const FACING_COL = { S: [0, 1], N: [2, 3], W: [4, 5], E: [6, 7] };

const NPC_SPRITE = {
    npc01: [[4, 0], [4, 1]],
    npc02: [[4, 2], [4, 3]],
    npc03: [[5, 0], [5, 1]],
    npc04: [[5, 2], [5, 3]],
    npc05: [[4, 4], [4, 5]],
    npc06: [[5, 4], [5, 5]],
    npc07: [[6, 0], [6, 1]],
    npc08: [[6, 2], [6, 3]],
};
const NPC_ANIM_MS = 500;

// ── DOM ───────────────────────────────────────────────────────────────────────
const mapCanvas    = document.getElementById("map");
const spriteCanvas = document.getElementById("sprite-layer");
const mapCtx       = mapCanvas.getContext("2d");
const spriteCtx    = spriteCanvas.getContext("2d");
const logEl        = document.getElementById("log");
const statusEl     = document.getElementById("status");
const wsStatusEl   = document.getElementById("ws-status");

// ── Tilemap ───────────────────────────────────────────────────────────────────
let tilemap = {};   // tile_name → [col, row] or [col, row, cwDeg]

async function loadTilemap() {
    const data = await fetch("static/tilemap_town.json").then(r => r.json());
    tilemap = data;
}

// ── Sprites ───────────────────────────────────────────────────────────────────
const spriteSheet = new Image();
spriteSheet.src = SPRITE_SRC;
spriteSheet.onload = () => redraw();

// ── Game state ────────────────────────────────────────────────────────────────
let tileGrid    = null;
let tilesetImg  = null;
let mapRows     = 0;
let mapCols     = 0;

let player = { name: "MELVIN", row: 0, col: 0, facing: "S" };
let npcs   = [];   // [{index, row, col, npc_sprite, anim_ms}]

let camRow = 0;
let camCol = 0;

// ── Timing (overwritten by hub_init config payload) ───────────────────────────
let ANIM_MS = 220;   // ms per tile — replaced at runtime from server config

// Per-character animation state
const anims = new Map();  // name → {srcRow, srcCol, dstRow, dstCol, facing, t0}

function startAnim(name, srcRow, srcCol, dstRow, dstCol, facing) {
    const now  = performance.now();
    const prev = anims.get(name);
    let actualSrc = { row: srcRow, col: srcCol };
    if (prev) {
        const t = Math.min(1, (now - prev.t0) / ANIM_MS);
        actualSrc = {
            row: prev.srcRow + (prev.dstRow - prev.srcRow) * t,
            col: prev.srcCol + (prev.dstCol - prev.srcCol) * t,
        };
    }
    anims.set(name, {
        srcRow: actualSrc.row, srcCol: actualSrc.col,
        dstRow, dstCol, facing, t0: now,
    });
}

function visualPos(name, settledRow, settledCol, settledFacing) {
    const a = anims.get(name);
    if (!a) return { row: settledRow, col: settledCol, facing: settledFacing, frame: 1 };
    const t = Math.min(1, (performance.now() - a.t0) / ANIM_MS);
    return {
        row:    a.srcRow + (a.dstRow - a.srcRow) * t,
        col:    a.srcCol + (a.dstCol - a.srcCol) * t,
        facing: a.facing,
        frame:  t < 0.5 ? 1 : 2,
    };
}

let rafId = null;
function scheduleRaf() {
    if (rafId === null) rafId = requestAnimationFrame(rafTick);
}
function rafTick(now) {
    redrawAt(now);
    const anyActive = anims.size > 0 &&
        [...anims.values()].some(a => now - a.t0 < ANIM_MS);
    if (anyActive || npcs.length > 0) {
        rafId = requestAnimationFrame(rafTick);
    } else {
        anims.clear();
        rafId = null;
        redrawAt(performance.now());
    }
}

// ── Camera ────────────────────────────────────────────────────────────────────
function updateCamera(row, col) {
    camRow = Math.max(0, Math.min(row - Math.floor(VIEW_ROWS / 2),
                                  Math.max(0, mapRows - VIEW_ROWS)));
    camCol = Math.max(0, Math.min(col - Math.floor(VIEW_COLS / 2),
                                  Math.max(0, mapCols - VIEW_COLS)));
}

// ── Canvas sizing ─────────────────────────────────────────────────────────────
function resizeCanvases() {
    const w = VIEW_COLS * TILE_DRAW;
    const h = VIEW_ROWS * TILE_DRAW;
    [mapCanvas, spriteCanvas].forEach(c => {
        c.width = w; c.height = h;
        c.style.width  = w + "px";
        c.style.height = h + "px";
    });
}

// ── Tile drawing ──────────────────────────────────────────────────────────────
function drawTile(ctx, name, destCol, destRow) {
    if (!tilesetImg || !name) return;
    let tileName = name;
    let rot = 0;
    if (name.includes(":")) {
        [tileName, rot] = name.split(":");
        rot = parseInt(rot, 10);
    }
    const entry = tilemap[tileName];
    if (!entry) return;
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

function drawTileGrid(ctx) {
    if (!tileGrid || !tilesetImg) return;
    ctx.imageSmoothingEnabled = false;
    const baseRow = Math.floor(camRow);
    const baseCol = Math.floor(camCol);
    for (let vr = 0; vr <= VIEW_ROWS; vr++) {
        const gr = vr + baseRow;
        if (gr < 0 || gr >= tileGrid.length) continue;
        for (let vc = 0; vc <= VIEW_COLS; vc++) {
            const gc = vc + baseCol;
            if (gc < 0 || gc >= tileGrid[gr].length) continue;
            const cell = tileGrid[gr][gc];
            if (Array.isArray(cell)) {
                drawTile(ctx, cell[0], vc, vr);
                drawTile(ctx, cell[1], vc, vr);
            } else {
                drawTile(ctx, cell, vc, vr);
            }
        }
    }
}

// ── Sprite drawing ────────────────────────────────────────────────────────────
function drawMemberSprite(name, destCol, destRow, facing, frame) {
    spriteCtx.imageSmoothingEnabled = false;
    const sprRow = PARTY_SPRITE_ROW[name] ?? 0;
    const cols   = FACING_COL[facing]    ?? FACING_COL["S"];
    const sprCol = cols[frame === 2 ? 1 : 0];
    spriteCtx.drawImage(
        spriteSheet,
        sprCol * TILE_PX, sprRow * TILE_PX, TILE_PX, TILE_PX,
        Math.round(destCol * TILE_DRAW), Math.round(destRow * TILE_DRAW),
        TILE_DRAW, TILE_DRAW,
    );
}

function drawNpcSprites(now) {
    spriteCtx.imageSmoothingEnabled = false;
    for (const npc of npcs) {
        const animMs = npc.anim_ms || NPC_ANIM_MS;
        const frame  = Math.floor(now / animMs) % 2;
        const dstCol = npc.col - camCol;
        const dstRow = npc.row - camRow;
        if (dstRow < -1 || dstRow > VIEW_ROWS || dstCol < -1 || dstCol > VIEW_COLS) continue;
        const frames = NPC_SPRITE[npc.npc_sprite];
        if (!frames) continue;
        const [sheetRow, sheetCol] = frames[frame];
        spriteCtx.drawImage(
            spriteSheet,
            sheetCol * TILE_PX, sheetRow * TILE_PX, TILE_PX, TILE_PX,
            Math.round(dstCol * TILE_DRAW), Math.round(dstRow * TILE_DRAW),
            TILE_DRAW, TILE_DRAW,
        );
    }
}

// ── Redraw ────────────────────────────────────────────────────────────────────
function redrawAt(now) {
    mapCtx.clearRect(0, 0, mapCanvas.width, mapCanvas.height);
    spriteCtx.clearRect(0, 0, spriteCanvas.width, spriteCanvas.height);
    if (!spriteSheet.complete || spriteSheet.naturalWidth === 0) return;

    drawTileGrid(mapCtx);

    // NPCs behind player
    drawNpcSprites(now);

    // Player
    const vp = visualPos(player.name, player.row, player.col, player.facing);
    drawMemberSprite(player.name, vp.col - camCol, vp.row - camRow, vp.facing, vp.frame);
}

function redraw() { redrawAt(performance.now()); }

// ── Event handlers ────────────────────────────────────────────────────────────
function onHubInit(e) {
    // Apply server-side config before anything else
    if (e.config) {
        ANIM_MS = e.config.player_move_ms ?? ANIM_MS;
        REPEAT_INTERVAL_MS = ANIM_MS;   // key repeat in lock-step with animation
    }

    mapRows = e.rows;
    mapCols = e.cols;
    tileGrid = e.tile_grid || null;

    const party = e.party || [];
    if (party.length) {
        player.row = party[0].row;
        player.col = party[0].col;
    }
    updateCamera(player.row, player.col);
    resizeCanvases();

    if (e.tileset_url) {
        const img = new Image();
        img.onload = () => { tilesetImg = img; redraw(); };
        img.src = e.tileset_url;
    }
    statusEl.textContent = "Front House — use arrow keys to move";
    log("event", `hub_init ${e.rows}×${e.cols}  move_ms=${ANIM_MS}`);
}

function onHubMove(e) {
    const prevRow = player.row;
    const prevCol = player.col;
    startAnim(player.name, prevRow, prevCol, e.row, e.col, e.direction || player.facing);
    player.row = e.row;
    player.col = e.col;
    if (e.direction) player.facing = e.direction;
    updateCamera(player.row, player.col);
    scheduleRaf();
    statusEl.textContent = `MELVIN (${e.row},${e.col})  tick=${e.tick}`;
}

function onIntEnemies(e) {
    npcs = e.enemies || [];
    scheduleRaf();
}

function handleServerEvent(data) {
    switch (data.type) {
        case "hub_init":    onHubInit(data);    break;
        case "hub_move":    onHubMove(data);    break;
        case "int_enemies": onIntEnemies(data); break;
        default:
            log("info", `unhandled: ${data.type}`);
    }
}

// ── WebSocket ─────────────────────────────────────────────────────────────────
let ws = null;
let wsReconnectTimer = null;

function connectWS() {
    if (wsReconnectTimer) { clearTimeout(wsReconnectTimer); wsReconnectTimer = null; }
    const proto = location.protocol === "https:" ? "wss:" : "ws:";
    ws = new WebSocket(`${proto}//${location.host}/ws`);

    ws.onopen = () => {
        wsStatusEl.textContent = "connected";
        wsStatusEl.style.color = "#4a8";
        log("event", "websocket connected");
    };

    ws.onmessage = (evt) => {
        try { handleServerEvent(JSON.parse(evt.data)); }
        catch (err) { log("warn", `parse error: ${err}`); }
    };

    ws.onclose = () => {
        wsStatusEl.textContent = "disconnected";
        wsStatusEl.style.color = "#a44";
        log("warn", "websocket closed — reconnecting in 2s");
        wsReconnectTimer = setTimeout(connectWS, 2000);
    };

    ws.onerror = () => {
        wsStatusEl.textContent = "error";
        wsStatusEl.style.color = "#a44";
    };
}

function sendKey(key) {
    if (ws && ws.readyState === WebSocket.OPEN) {
        ws.send(JSON.stringify({ type: "key", key }));
    }
}

// ── Keyboard input ────────────────────────────────────────────────────────────
const MOVE_KEYS = new Set(["ArrowUp", "ArrowDown", "ArrowLeft", "ArrowRight"]);
const ACTION_KEYS = new Set(["z", "Z", "x", "X", "Enter"]);

const heldKeys = new Set();
let repeatTimer = null;
// Repeat interval is driven by ANIM_MS so held-key speed matches animation.
// Recalculated in onHubInit once server config arrives.
let REPEAT_INTERVAL_MS = 220;

function startRepeat() {
    if (repeatTimer !== null) return;
    repeatTimer = setInterval(() => {
        // Send the most recently held move key (last pressed wins)
        for (const k of [...heldKeys].reverse()) {
            if (MOVE_KEYS.has(k)) { sendKey(k); break; }
        }
    }, REPEAT_INTERVAL_MS);
}

function stopRepeat() {
    if (repeatTimer !== null) { clearInterval(repeatTimer); repeatTimer = null; }
}

document.addEventListener("keydown", (e) => {
    if (MOVE_KEYS.has(e.key)) {
        e.preventDefault();
        if (!heldKeys.has(e.key)) {
            heldKeys.add(e.key);
            sendKey(e.key);    // immediate on first press
            startRepeat();
        }
        return;
    }
    if (ACTION_KEYS.has(e.key)) {
        e.preventDefault();
        sendKey(e.key);
    }
});

document.addEventListener("keyup", (e) => {
    heldKeys.delete(e.key);
    if ([...heldKeys].every(k => !MOVE_KEYS.has(k))) stopRepeat();
});

// ── Log ───────────────────────────────────────────────────────────────────────
function log(cls, text) {
    const div = document.createElement("div");
    div.className = cls;
    div.textContent = text;
    logEl.appendChild(div);
    logEl.scrollTop = logEl.scrollHeight;
    while (logEl.children.length > 150) logEl.removeChild(logEl.firstChild);
}

// ── Boot ──────────────────────────────────────────────────────────────────────
(async () => {
    resizeCanvases();
    await loadTilemap();
    connectWS();
})();
