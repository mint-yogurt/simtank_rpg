"""Interior scene — town or cave interior entered from the overworld.

Holds the generated interior data and manages the party while inside.
Stepping on entry_tile exits back to the overworld; the overworld loop
restores the party to the exact entrance tile they came from.

monster_spawn: True for caves/dungeons, False for towns.
"""

from dataclasses import dataclass, field


@dataclass
class Interior:
    """A single interior location (town or cave)."""
    feature_id:    int
    data:          dict   # deserialized interior dict from worlddb
    monster_spawn: bool   # True = cave/dungeon, False = town

    @property
    def spawn(self):
        """(row, col) where the party appears when entering."""
        raw = self.data.get('spawn')
        return tuple(raw) if raw is not None else None

    @property
    def entry_tile(self):
        """(row, col) of the exit trigger tile."""
        raw = self.data.get('entry_tile')
        return tuple(raw) if raw is not None else None

    def combined_grid(self):
        """Merge floor_grid + overlay/wall_grid into one (row,col)→tile dict.

        For caves: floor_grid then wall_grid (wall overwrites).
        For towns: ground_grid then overlay (overlay overwrites).
        Returns dict with tuple keys.
        """
        result = {}
        if 'floor_grid' in self.data:
            for key, tile in self.data['floor_grid'].items():
                r, c = map(int, key.split(','))
                result[(r, c)] = tile
            for key, tile in self.data.get('wall_grid', {}).items():
                r, c = map(int, key.split(','))
                result[(r, c)] = tile
        else:
            for key, tile in self.data.get('ground_grid', {}).items():
                r, c = map(int, key.split(','))
                result[(r, c)] = tile
            for key, tile in self.data.get('overlay', {}).items():
                r, c = map(int, key.split(','))
                result[(r, c)] = tile
        return result

    def is_exit(self, row: int, col: int) -> bool:
        """True when the party steps onto the exit-trigger tile."""
        return self.entry_tile is not None and (row, col) == self.entry_tile
