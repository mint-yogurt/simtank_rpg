"""Persistent world-state database for simtank_rpg.

Single SQLite file. Three tables:
  screens   — one row per discovered screen; grid cached on first visit.
  features  — one row per interactable feature on a discovered screen.
  interiors — one row per entered feature; interior grid cached on first entry.

Generate-once rule: a screen or interior is generated exactly once (on first
visit), then always read from the DB. A fresh DB + the same world_seed must
rebuild identically as locations are visited (determinism / replay guarantee).

Coordinate convention: all (row, col) tuples match the grid indexing used in
ScreenData. local_row and local_col columns in the features table follow the
same convention.

Interior feature_id is a stable signed 64-bit hash of
(world_seed, sx, sy, local_row, local_col) so it doesn't change if the world
reorders screens, and the interiors table is keyed by feature_id alone.

Known interface warts:
  No read-only get_screen() — get_or_create_screen requires a generator even
  when the screen is already cached.  Add get_screen(world_seed, sx, sy) before
  any job that needs ad-hoc grid reads without a generator on hand.

  set_feature_state silently no-ops if the feature row is missing (UPDATE on a
  non-existent PK does nothing).  Safe today because get_or_create_screen always
  pre-inserts all features; a mistyped coordinate goes undetected.  Consider
  raising on zero rowcount if this becomes a bug magnet.
"""

import datetime
import hashlib
import json
import sqlite3
import struct

from engine.tiles import is_enterable, is_passable
from procgen import enemygen, npcgen


def _to_s64(n):
    """Fit an unsigned 64-bit int into SQLite's signed 64-bit INTEGER range."""
    return struct.unpack('>q', struct.pack('>Q', int(n) & 0xFFFFFFFFFFFFFFFF))[0]

_SCHEMA = """
CREATE TABLE IF NOT EXISTS session (
    id            INTEGER PRIMARY KEY DEFAULT 1,
    scene         TEXT    NOT NULL,
    world_seed    INTEGER NOT NULL,
    sx            INTEGER NOT NULL,
    sy            INTEGER NOT NULL,
    row_          INTEGER NOT NULL,
    col_          INTEGER NOT NULL,
    tick          INTEGER NOT NULL DEFAULT 0,
    leader_idx    INTEGER NOT NULL DEFAULT 0,
    party_json    TEXT    NOT NULL,
    goal_json     TEXT,
    navlog_json   TEXT,
    journals_json TEXT,
    saved_at      TEXT
);

CREATE TABLE IF NOT EXISTS screens (
    world_seed        INTEGER NOT NULL,
    sx                INTEGER NOT NULL,
    sy                INTEGER NOT NULL,
    screen_seed       INTEGER NOT NULL,
    rows              INTEGER NOT NULL,
    cols              INTEGER NOT NULL,
    grid_json         TEXT    NOT NULL,
    exits_json        TEXT    NOT NULL,
    visited           INTEGER NOT NULL DEFAULT 0,
    first_visit_tick  INTEGER,
    PRIMARY KEY (world_seed, sx, sy)
);

CREATE TABLE IF NOT EXISTS features (
    world_seed      INTEGER NOT NULL,
    sx              INTEGER NOT NULL,
    sy              INTEGER NOT NULL,
    local_row       INTEGER NOT NULL,
    local_col       INTEGER NOT NULL,
    feature_type    TEXT    NOT NULL,
    enterable       INTEGER NOT NULL DEFAULT 0,
    entered         INTEGER,
    cleared         INTEGER,
    npc_flags_json  TEXT,
    PRIMARY KEY (world_seed, sx, sy, local_row, local_col)
);

CREATE TABLE IF NOT EXISTS interiors (
    feature_id      INTEGER NOT NULL PRIMARY KEY,
    interior_seed   INTEGER NOT NULL,
    data_json       TEXT    NOT NULL
);

CREATE TABLE IF NOT EXISTS enemies (
    scope_type    TEXT    NOT NULL,
    scope_id      INTEGER NOT NULL,
    enemy_index   INTEGER NOT NULL,
    name          TEXT    NOT NULL,
    iq            INTEGER NOT NULL,
    weight        INTEGER NOT NULL,
    sweat         INTEGER NOT NULL,
    hair          INTEGER NOT NULL,
    level         INTEGER NOT NULL,
    npc_sprite    TEXT    NOT NULL,
    behavior_type INTEGER NOT NULL,
    behavior_axis TEXT,
    PRIMARY KEY (scope_type, scope_id, enemy_index)
);
"""


def compute_feature_id(world_seed: int, sx: int, sy: int,
                       local_row: int, local_col: int) -> int:
    """Return a stable signed 64-bit feature ID hashed from location components."""
    raw = struct.pack('>qqqqq', world_seed, sx, sy, local_row, local_col)
    digest = hashlib.sha256(raw).digest()[:8]
    return _to_s64(struct.unpack('>Q', digest)[0])


def _derive_interior_seed(world_seed: int, feature_id: int) -> int:
    raw = struct.pack('>qq', world_seed, feature_id)
    digest = hashlib.sha256(raw).digest()[:8]
    return _to_s64(struct.unpack('>Q', digest)[0])


def _derive_enemy_seed(base_seed: int) -> int:
    """Derive a separate seed for enemy generation from a screen/interior seed."""
    raw = struct.pack('>QQ', int(base_seed) & 0xFFFFFFFFFFFFFFFF, 0xE4E4E4E4E4E4E4E4)
    digest = hashlib.sha256(raw).digest()[:8]
    return _to_s64(struct.unpack('>Q', digest)[0])


def _derive_cave_enemy_seed(base_seed: int) -> int:
    """Derive the shared cave encounter pool seed for a screen."""
    raw = struct.pack('>QQ', int(base_seed) & 0xFFFFFFFFFFFFFFFF, 0xC0DE1234C0DE5678)
    digest = hashlib.sha256(raw).digest()[:8]
    return _to_s64(struct.unpack('>Q', digest)[0])


def _compute_exits(grid):
    """Return {'N': bool, 'S': bool, 'E': bool, 'W': bool}.

    An exit exists on an edge if at least one tile on that edge is passable
    AND not enterable (enterable features are destinations, not through-routes).
    """
    rows = len(grid)
    cols = len(grid[0]) if grid else 0

    def ok(t):
        return t and is_passable(t) and not is_enterable(t)

    return {
        'N': any(ok(grid[0][c])        for c in range(cols)),
        'S': any(ok(grid[rows-1][c])   for c in range(cols)),
        'E': any(ok(grid[r][cols-1])   for r in range(rows)),
        'W': any(ok(grid[r][0])        for r in range(rows)),
    }


def _row_to_dict(cursor, row):
    return {d[0]: v for d, v in zip(cursor.description, row)}


class WorldDB:
    def __init__(self, db_path):
        self._conn = sqlite3.connect(db_path, check_same_thread=False)
        self._conn.row_factory = sqlite3.Row
        self._conn.executescript(_SCHEMA)
        self._migrate()
        self._conn.commit()

    def _migrate(self):
        """Add columns introduced after initial schema without dropping existing data."""
        existing = {
            row[1]
            for row in self._conn.execute("PRAGMA table_info(enemies)").fetchall()
        }
        if "behavior_type" not in existing:
            self._conn.execute("ALTER TABLE enemies ADD COLUMN behavior_type INTEGER NOT NULL DEFAULT 1")
        if "behavior_axis" not in existing:
            self._conn.execute("ALTER TABLE enemies ADD COLUMN behavior_axis TEXT")
        if "overworld_sprite" not in existing:
            self._conn.execute("ALTER TABLE enemies ADD COLUMN overworld_sprite TEXT")
        if "sprite_palette_json" not in existing:
            self._conn.execute("ALTER TABLE enemies ADD COLUMN sprite_palette_json TEXT")
        # session table: created by _SCHEMA above; no column additions needed yet

    # ── SESSION SAVE / LOAD ───────────────────────────────────────────────────

    def save_session(self, *, scene: str, world_seed: int,
                     sx: int, sy: int, row: int, col: int,
                     tick: int, leader_idx: int,
                     party: list, goal, navlog, journals: dict) -> None:
        """Upsert the single-row session state.

        party:    list of OverworldMember
        goal:     Goal | None
        navlog:   NavLog (last 20 notable entries are saved)
        journals: dict[str, MemberJournal]
        """
        party_data = [
            {"name": m.name, "hp": m.hp, "max_hp": m.max_hp, "alive": m.alive}
            for m in party
        ]
        goal_data = goal.to_dict() if goal is not None else None

        navlog_data = None
        if navlog is not None:
            navlog_data = [
                {"tick": e.tick, "event_type": e.event_type, "desc": e.desc}
                for e in navlog.last_notable(20)
            ]

        journals_data = None
        if journals:
            journals_data = {
                name: [
                    {"tick": e.tick, "event_type": e.event_type, "desc": e.desc}
                    for e in mj._entries
                ]
                for name, mj in journals.items()
            }

        self._conn.execute(
            """INSERT OR REPLACE INTO session
               (id, scene, world_seed, sx, sy, row_, col_,
                tick, leader_idx, party_json, goal_json,
                navlog_json, journals_json, saved_at)
               VALUES (1, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
            (scene, world_seed, sx, sy, row, col, tick, leader_idx,
             json.dumps(party_data),
             json.dumps(goal_data) if goal_data is not None else None,
             json.dumps(navlog_data) if navlog_data is not None else None,
             json.dumps(journals_data) if journals_data is not None else None,
             datetime.datetime.utcnow().isoformat()),
        )
        self._conn.commit()

    def load_session(self) -> dict | None:
        """Return the saved session as a plain dict, or None if no session exists."""
        row = self._conn.execute("SELECT * FROM session WHERE id=1").fetchone()
        if row is None:
            return None
        d = dict(row)
        d["party"]    = json.loads(d.pop("party_json"))
        d["goal"]     = json.loads(d.pop("goal_json"))     if d.get("goal_json")     else None
        d["navlog"]   = json.loads(d.pop("navlog_json"))   if d.get("navlog_json")   else []
        d["journals"] = json.loads(d.pop("journals_json")) if d.get("journals_json") else {}
        d["row"]      = d.pop("row_")
        d["col"]      = d.pop("col_")
        return d

    def clear_session(self) -> None:
        """Delete the saved session (forces a fresh start on next boot)."""
        self._conn.execute("DELETE FROM session WHERE id=1")
        self._conn.commit()

    # ── READ / WRITE SCREENS ──────────────────────────────────────────────────

    def get_or_create_screen(self, world_seed, sx, sy, generator):
        """Return the screens row (as dict), generating and inserting if absent.

        generator: callable(world_seed, sx, sy) → ScreenData
        """
        row = self._conn.execute(
            "SELECT * FROM screens WHERE world_seed=? AND sx=? AND sy=?",
            (world_seed, sx, sy),
        ).fetchone()
        if row is not None:
            return dict(row)

        data   = generator(world_seed, sx, sy)
        exits  = _compute_exits(data.grid)

        self._conn.execute(
            """INSERT INTO screens
               (world_seed, sx, sy, screen_seed, rows, cols, grid_json, exits_json)
               VALUES (?, ?, ?, ?, ?, ?, ?, ?)""",
            (world_seed, sx, sy, _to_s64(data.screen_seed),
             data.rows, data.cols,
             json.dumps(data.grid),
             json.dumps(exits)),
        )

        for (row_idx, col_idx), ftype in data.feature_cells.items():
            self._conn.execute(
                """INSERT OR IGNORE INTO features
                   (world_seed, sx, sy, local_row, local_col, feature_type, enterable)
                   VALUES (?, ?, ?, ?, ?, ?, ?)""",
                (world_seed, sx, sy, row_idx, col_idx,
                 ftype, int(is_enterable(ftype))),
            )

        self._conn.commit()

        enemy_count = data.screen_seed % 4  # 0-3, deterministic
        if enemy_count > 0:
            enemy_seed = _derive_enemy_seed(data.screen_seed)
            self.get_or_create_enemies('screen', data.screen_seed, enemy_seed, enemy_count, level=1)

        # Cave encounter pool: 6 enemies shared across all caves on this screen.
        cave_seed = _derive_cave_enemy_seed(data.screen_seed)
        self.get_or_create_enemies('cave_screen', data.screen_seed, cave_seed, 6, level=2)

        return dict(self._conn.execute(
            "SELECT * FROM screens WHERE world_seed=? AND sx=? AND sy=?",
            (world_seed, sx, sy),
        ).fetchone())

    def mark_visited(self, world_seed, sx, sy, tick):
        self._conn.execute(
            """UPDATE screens
               SET visited=1, first_visit_tick=COALESCE(first_visit_tick, ?)
               WHERE world_seed=? AND sx=? AND sy=?""",
            (tick, world_seed, sx, sy),
        )
        self._conn.commit()

    def list_known_screens(self, world_seed):
        rows = self._conn.execute(
            "SELECT * FROM screens WHERE world_seed=? ORDER BY sy, sx",
            (world_seed,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── READ / WRITE FEATURES ─────────────────────────────────────────────────

    def get_feature(self, world_seed, sx, sy, row, col):
        """Return feature row as dict, or None if not found."""
        row_ = self._conn.execute(
            """SELECT * FROM features
               WHERE world_seed=? AND sx=? AND sy=? AND local_row=? AND local_col=?""",
            (world_seed, sx, sy, row, col),
        ).fetchone()
        return dict(row_) if row_ is not None else None

    def set_feature_state(self, world_seed, sx, sy, row, col,
                          *, entered=None, cleared=None, npc_flags=None):
        """Update mutable feature state. Only provided (non-None) fields are written."""
        updates = []
        params  = []
        if entered is not None:
            updates.append("entered=?");     params.append(int(entered))
        if cleared is not None:
            updates.append("cleared=?");     params.append(int(cleared))
        if npc_flags is not None:
            updates.append("npc_flags_json=?"); params.append(json.dumps(npc_flags))
        if not updates:
            return
        params += [world_seed, sx, sy, row, col]
        self._conn.execute(
            f"UPDATE features SET {', '.join(updates)}"
            " WHERE world_seed=? AND sx=? AND sy=? AND local_row=? AND local_col=?",
            params,
        )
        self._conn.commit()

    def list_screen_features(self, world_seed, sx, sy):
        """Return all feature rows for a single screen as dicts."""
        rows = self._conn.execute(
            """SELECT * FROM features
               WHERE world_seed=? AND sx=? AND sy=?
               ORDER BY local_row, local_col""",
            (world_seed, sx, sy),
        ).fetchall()
        return [dict(r) for r in rows]

    def list_known_features(self, world_seed):
        rows = self._conn.execute(
            """SELECT * FROM features WHERE world_seed=?
               ORDER BY sy, sx, local_row, local_col""",
            (world_seed,),
        ).fetchall()
        return [dict(r) for r in rows]

    # ── READ / WRITE INTERIORS ────────────────────────────────────────────────

    def get_or_create_interior(self, world_seed: int, feature_id: int, generator):
        """Return interior data dict, generating and caching if not yet stored.

        generator: callable(seed: int) → object with .to_dict() method
        Returns the deserialized data dict (keys converted back to tuples where
        applicable — see the generator's from_dict convention).
        """
        row = self._conn.execute(
            "SELECT data_json FROM interiors WHERE feature_id=?",
            (_to_s64(feature_id),),
        ).fetchone()
        if row is not None:
            return json.loads(row[0])

        interior_seed = _derive_interior_seed(world_seed, feature_id)
        data = generator(interior_seed)
        data_dict = data.to_dict()
        data_json = json.dumps(data_dict, sort_keys=True)

        self._conn.execute(
            "INSERT INTO interiors (feature_id, interior_seed, data_json) VALUES (?, ?, ?)",
            (_to_s64(feature_id), _to_s64(interior_seed), data_json),
        )
        self._conn.commit()

        return data_dict

    # ── READ / WRITE ENEMIES ──────────────────────────────────────────────────

    def get_or_create_enemies(self, scope_type: str, scope_id: int,
                              seed: int, count: int, level: int = 1) -> list[dict]:
        """Return enemies for this scope, generating and caching if absent.

        scope_type: 'screen' or 'interior'
        scope_id:   screen_seed (for screens) or feature_id (for interiors)
        seed:       enemy-specific seed derived from the terrain seed
        count:      number of enemies to generate
        level:      base level for generated enemies
        """
        rows = self._conn.execute(
            "SELECT * FROM enemies WHERE scope_type=? AND scope_id=? ORDER BY enemy_index",
            (scope_type, _to_s64(scope_id)),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        allow_overworld_sprite = (scope_type == 'screen')
        enemies = enemygen.generate_enemies(seed, count, level, allow_overworld_sprite)
        for i, e in enumerate(enemies):
            self._conn.execute(
                """INSERT INTO enemies
                   (scope_type, scope_id, enemy_index,
                    name, iq, weight, sweat, hair, level,
                    npc_sprite, overworld_sprite, sprite_palette_json,
                    behavior_type, behavior_axis)
                   VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (scope_type, _to_s64(scope_id), i,
                 e["name"], e["iq"], e["weight"], e["sweat"], e["hair"], e["level"],
                 e["npc_sprite"], e.get("overworld_sprite"),
                 json.dumps(e.get("sprite_palette") or []),
                 e["behavior_type"], e["behavior_axis"]),
            )
        self._conn.commit()

        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM enemies WHERE scope_type=? AND scope_id=? ORDER BY enemy_index",
            (scope_type, _to_s64(scope_id)),
        ).fetchall()]

    def list_enemies(self, scope_type: str, scope_id: int) -> list[dict]:
        """Return all enemies for a scope (screen or interior) as dicts."""
        rows = self._conn.execute(
            "SELECT * FROM enemies WHERE scope_type=? AND scope_id=? ORDER BY enemy_index",
            (scope_type, _to_s64(scope_id)),
        ).fetchall()
        return [dict(r) for r in rows]

    def get_or_create_town_npcs(self, feature_id: int) -> list[dict]:
        """Return town NPCs for this feature_id, generating and caching if absent.

        scope_type='town', scope_id=feature_id. Count is 3–8, derived from seed.
        """
        scope_id = _to_s64(feature_id)
        rows = self._conn.execute(
            "SELECT * FROM enemies WHERE scope_type='town' AND scope_id=? ORDER BY enemy_index",
            (scope_id,),
        ).fetchall()
        if rows:
            return [dict(r) for r in rows]

        seed = _derive_enemy_seed(feature_id)
        count = int(abs(seed) % 6) + 3   # 3–8 inclusive
        npcs = npcgen.generate_town_npcs(seed, count)
        for i, n in enumerate(npcs):
            self._conn.execute(
                """INSERT INTO enemies
                   (scope_type, scope_id, enemy_index,
                    name, iq, weight, sweat, hair, level,
                    npc_sprite, overworld_sprite, sprite_palette_json,
                    behavior_type, behavior_axis)
                   VALUES ('town', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)""",
                (scope_id, i,
                 n["name"], n["iq"], n["weight"], n["sweat"], n["hair"], n["level"],
                 n["npc_sprite"], None,
                 json.dumps(n.get("sprite_palette") or []),
                 n["behavior_type"], n["behavior_axis"]),
            )
        self._conn.commit()
        return [dict(r) for r in self._conn.execute(
            "SELECT * FROM enemies WHERE scope_type='town' AND scope_id=? ORDER BY enemy_index",
            (scope_id,),
        ).fetchall()]

    def close(self):
        self._conn.close()
