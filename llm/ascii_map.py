"""ASCII map renderer for overworld screens.

Shared between llm/prompts.py (LLM context) and procgen/viewscan_test.py (debug output).
No PIL, no display system — pure text, no game state.

Used in two modes:
  clean  — tile chars + @ party marker only (used in LLM prompts — less noise)
  overlay — adds n/s/e/w ray path, X blocker hit, ! enterable hit, uppercase edge markers
             (used in debug/test output)
"""

from engine.tiles import is_enterable, is_passable
from engine.viewscan import _DIRS

_ENTRY_CHARS = {
    'hub': 'H', 'town1': 'T', 'castle1': 'K', 'cave1': 'c',
    'building1': 'B', 'house1': 'h', 'tower1': 'W', 'tower2': 'W',
    'skullhouse1': 'S', 'mnt_cave': 'M', 'enter': 'E',
}


def tile_char(raw: str) -> str:
    """Single-character tile representation."""
    base = (raw or 'grass1').split(':')[0]
    if is_enterable(base):
        return _ENTRY_CHARS.get(base, base[0].upper())
    if not is_passable(base):
        return '#'
    if base.startswith('path_'):
        return '+'
    if base.startswith('dirt_'):
        return ','
    return '.'


def render_map_clean(grid, rows: int, cols: int, party_row: int, party_col: int) -> str:
    """Tile chars + @ marker, no ray overlay. Used in LLM prompts."""
    chars = [[tile_char(grid[r][c]) for c in range(cols)] for r in range(rows)]
    chars[party_row][party_col] = '@'
    return '\n'.join('  ' + ' '.join(row) for row in chars)


def render_map_overlay(grid, rows: int, cols: int, vs) -> str:
    """Tile chars + @ + ray overlay. Used in debug/test output.

    Ray chars: n/s/e/w = traversed path  X = blocker hit  ! = enterable hit
               N/S/E/W = last in-bounds tile before screen edge
    """
    pc, pr = vs.party_col, vs.party_row
    chars = [[tile_char(grid[r][c]) for c in range(cols)] for r in range(rows)]

    for label, (dc, dr) in _DIRS.items():
        ds = getattr(vs, label)
        for d in range(1, ds.distance):
            c, r = pc + dc * d, pr + dr * d
            if 0 <= r < rows and 0 <= c < cols:
                chars[r][c] = label.lower()
        tc, tr = pc + dc * ds.distance, pr + dr * ds.distance
        if ds.kind == 'blocker' and 0 <= tr < rows and 0 <= tc < cols:
            chars[tr][tc] = 'X'
        elif ds.kind == 'enterable' and 0 <= tr < rows and 0 <= tc < cols:
            chars[tr][tc] = '!'
        elif ds.kind == 'edge' and ds.distance > 1:
            lc, lr = pc + dc * (ds.distance - 1), pr + dr * (ds.distance - 1)
            if 0 <= lr < rows and 0 <= lc < cols:
                chars[lr][lc] = label

    chars[pr][pc] = '@'
    return '\n'.join('  ' + ' '.join(row) for row in chars)
