"""Persistent world-state database for simtank_rpg.

Single SQLite file. Two tables:
  screens  — one row per discovered screen; grid cached on first visit.
  features — one row per interactable feature on a discovered screen.

Generate-once rule: a screen is generated exactly once (on first visit),
then always read from the DB. A fresh DB + the same world_seed must rebuild
identically as screens are visited (determinism / replay guarantee).

Coordinate convention: all (row, col) tuples match the grid indexing used in
ScreenData. local_row and local_col columns in the features table follow the
same convention.
"""

import json
import sqlite3
import struct

from engine.tiles import is_enterable, is_passable


def _to_s64(n):
    """Fit an unsigned 64-bit int into SQLite's signed 64-bit INTEGER range."""
    return struct.unpack('>q', struct.pack('>Q', int(n) & 0xFFFFFFFFFFFFFFFF))[0]

_SCHEMA = """
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
"""


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

    def list_known_features(self, world_seed):
        rows = self._conn.execute(
            """SELECT * FROM features WHERE world_seed=?
               ORDER BY sy, sx, local_row, local_col""",
            (world_seed,),
        ).fetchall()
        return [dict(r) for r in rows]

    def close(self):
        self._conn.close()
