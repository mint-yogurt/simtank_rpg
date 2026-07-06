"""Pathfinding for simtank_rpg.

Two levels:
  Within-screen: BFS over passable, non-enterable tiles to the nearest
                 exit edge tile in a given direction.
  Screen-level:  greedy direction selection toward a target screen,
                 checking exits_json before committing.

No LLM, no display, no RNG — pure engine data.
"""

from collections import deque

from engine.tiles import is_enterable, is_passable


# (direction, col_delta, row_delta)
_MOVES = [('N', 0, -1), ('S', 0, 1), ('E', 1, 0), ('W', -1, 0)]

_DELTA_TO_DIR: dict[tuple[int, int], str] = {
    (0, -1): 'N',
    (0,  1): 'S',
    (1,  0): 'E',
    (-1, 0): 'W',
}

# Which (row, col) pairs form the target exit edge for each direction.
_EXIT_EDGE: dict[str, callable] = {
    'N': lambda rows, cols: [(0, c)        for c in range(cols)],
    'S': lambda rows, cols: [(rows-1, c)   for c in range(cols)],
    'E': lambda rows, cols: [(r, cols-1)   for r in range(rows)],
    'W': lambda rows, cols: [(r, 0)        for r in range(rows)],
}


def _walkable(grid, row: int, col: int) -> bool:
    """Passable and not enterable — tiles the party can traverse without stopping."""
    tile = (grid[row][col] or 'grass1').split(':')[0]
    return is_passable(tile) and not is_enterable(tile)


def bfs_to_exit(grid, start_row: int, start_col: int,
                direction: str) -> list[tuple[int, int]] | None:
    """BFS from (start_row, start_col) to the nearest exit tile in `direction`.

    Exit tiles are passable, non-enterable tiles on the far edge (row 0 for
    North, row rows-1 for South, etc.).  Enterable tiles are treated as walls
    so execute_move won't stop prematurely mid-path.

    Returns the path as [(row, col), ...] from start (inclusive) to goal
    (inclusive), or None if no path exists.
    """
    rows = len(grid)
    cols = len(grid[0]) if rows else 0

    goal_set = {
        (r, c)
        for r, c in _EXIT_EDGE[direction](rows, cols)
        if _walkable(grid, r, c)
    }
    if not goal_set:
        return None

    if (start_row, start_col) in goal_set:
        return [(start_row, start_col)]

    queue: deque[tuple[int, int]] = deque([(start_row, start_col)])
    parent: dict[tuple[int, int], tuple[int, int] | None] = {
        (start_row, start_col): None
    }

    while queue:
        r, c = queue.popleft()
        for _, dc, dr in _MOVES:
            nr, nc = r + dr, c + dc
            if not (0 <= nr < rows and 0 <= nc < cols):
                continue
            if (nr, nc) in parent:
                continue
            if not _walkable(grid, nr, nc):
                continue
            parent[(nr, nc)] = (r, c)
            if (nr, nc) in goal_set:
                path: list[tuple[int, int]] = []
                node: tuple[int, int] | None = (nr, nc)
                while node is not None:
                    path.append(node)
                    node = parent[node]
                path.reverse()
                return path
            queue.append((nr, nc))

    return None


def path_to_segments(path: list[tuple[int, int]]) -> list[tuple[str, int]]:
    """Convert a (row, col) path to [(direction, steps)] run-length segments.

    Consecutive steps in the same direction are merged into one segment.
    A path of length < 2 yields an empty list.
    """
    if len(path) < 2:
        return []

    segments: list[tuple[str, int]] = []
    current_dir: str | None = None
    current_count = 0

    for i in range(1, len(path)):
        r0, c0 = path[i - 1]
        r1, c1 = path[i]
        direction = _DELTA_TO_DIR.get((c1 - c0, r1 - r0))
        if direction == current_dir:
            current_count += 1
        else:
            if current_dir is not None:
                segments.append((current_dir, current_count))
            current_dir = direction
            current_count = 1

    if current_dir is not None:
        segments.append((current_dir, current_count))

    return segments


def screen_direction_toward(sx: int, sy: int,
                            target_sx: int, target_sy: int,
                            exits: dict) -> str | None:
    """Return the best available exit direction toward (target_sx, target_sy).

    Prefers the axis with greater screen distance so progress is maximised.
    Falls back to the secondary axis if the primary direction's exit is closed.
    Returns None if no useful exit is open (all candidates blocked).
    """
    dx = target_sx - sx
    dy = target_sy - sy

    if dx == 0 and dy == 0:
        return None  # already at target screen

    # Build preference list: primary axis (larger delta) first.
    candidates: list[str] = []
    if abs(dx) >= abs(dy):
        if dx > 0:
            candidates.append('E')
        elif dx < 0:
            candidates.append('W')
        if dy > 0:
            candidates.append('S')
        elif dy < 0:
            candidates.append('N')
    else:
        if dy > 0:
            candidates.append('S')
        elif dy < 0:
            candidates.append('N')
        if dx > 0:
            candidates.append('E')
        elif dx < 0:
            candidates.append('W')

    for d in candidates:
        if exits.get(d, False):
            return d

    return None
