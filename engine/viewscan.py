"""Line-of-sight tile scan for simtank_rpg.

Given a screen grid and party position, scans outward in the four cardinal
directions and reports what terminates each ray.

Each ray walks tile-by-tile from the party until it hits:
  1. An enterable feature (town, cave, castle …) → kind='enterable'
  2. An impassable tile (cliff, lake, forest …)   → kind='blocker'
  3. The screen edge (walked off the grid)         → kind='edge'

Distance semantics: 1 = the adjacent tile. For kind='edge', distance is the
first step index that leaves the grid (so distance=1 means the adjacent cell
is already off-grid).

No display, no LLM, no DB — pure data.
"""

from dataclasses import dataclass

from engine.tiles import is_enterable, is_passable


@dataclass(frozen=True)
class DirectionScan:
    kind: str               # 'enterable' | 'blocker' | 'edge'
    tile: str               # tile name at termination; '' when kind='edge'
    distance: int           # steps from party (1 = adjacent tile)
    adjacent_passable: bool # True if the immediately adjacent tile can be stepped onto


@dataclass(frozen=True)
class ViewScan:
    N: DirectionScan
    S: DirectionScan
    E: DirectionScan
    W: DirectionScan
    party_col: int
    party_row: int


# (dc, dr): col-delta, row-delta
_DIRS = {'N': (0, -1), 'S': (0, 1), 'E': (1, 0), 'W': (-1, 0)}


def scan(grid, party_row, party_col, feature_cells=None):
    """Scan from (party_row, party_col) outward in N/S/E/W.

    Args:
        grid:          2D list of tile-name strings; indexed as grid[row][col].
        party_row:     party's tile row.
        party_col:     party's tile column.
        feature_cells: (row, col) → feature_type map from generate_screen_data.
                       Accepted for interface consistency; not used internally —
                       grid tile names already encode feature types, and
                       is_enterable() classifies them directly.

    Returns:
        ViewScan with one DirectionScan per cardinal direction.
    """
    rows = len(grid)
    cols = len(grid[0]) if rows else 0

    results = {}
    for label, (dc, dr) in _DIRS.items():
        # Step-ability of the immediately adjacent tile (movement cares about this)
        adj_c, adj_r = party_col + dc, party_row + dr
        if 0 <= adj_r < rows and 0 <= adj_c < cols:
            adj_tile = (grid[adj_r][adj_c] or 'grass1').split(':')[0]
            adjacent_passable = is_passable(adj_tile)
        else:
            adjacent_passable = False  # screen edge is adjacent

        # Walk the ray
        c, r, dist = party_col, party_row, 0
        while True:
            c += dc
            r += dr
            dist += 1
            if not (0 <= r < rows and 0 <= c < cols):
                results[label] = DirectionScan(
                    kind='edge', tile='', distance=dist,
                    adjacent_passable=adjacent_passable)
                break
            tile = (grid[r][c] or 'grass1').split(':')[0]
            if is_enterable(tile):
                # enterable checked before passable: wall+enterable tiles (mnt_cave,
                # cave 'enter') resolve as enterable, matching is_passable's own logic
                results[label] = DirectionScan(
                    kind='enterable', tile=tile, distance=dist,
                    adjacent_passable=adjacent_passable)
                break
            if not is_passable(tile):
                results[label] = DirectionScan(
                    kind='blocker', tile=tile, distance=dist,
                    adjacent_passable=adjacent_passable)
                break
            # passable and not enterable — silently pass through

    return ViewScan(
        N=results['N'], S=results['S'], E=results['E'], W=results['W'],
        party_col=party_col, party_row=party_row,
    )
