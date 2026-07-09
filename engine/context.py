"""Curated context builder for LLM prompt slices.

Builds a small, stable summary from raw world state + NavLog.  This is the
ONLY thing that feeds goal-setting and checkpoint prompts — no raw journal
dumps, no wholesale DB queries in prompt builders.
"""

from dataclasses import dataclass, field

from engine.navlog import NavLog

_DEFAULT_N_EVENTS = 8

# Maps raw feature_type keys → broad kind label consumed by prompt builders.
# Keep in sync with procgen/worldgen.py FEATURE_TYPES.
_FEATURE_KIND: dict[str, str] = {
    "town1":      "town",
    "tower1":     "dungeon",
    "tower2":     "dungeon",
    "castle1":    "dungeon",
    "skullhouse1":"dungeon",
    "cave1":      "dungeon",
    "mnt_cave":   "dungeon",
    "mowdenpass": "dungeon",
}


@dataclass
class CuratedContext:
    pos_sx: int
    pos_sy: int
    active_goal: object | None          # Goal | None
    recent_events: list[str]            # formatted strings, oldest first
    pois: list[dict]                    # {sx, sy, feature_type, visited}
    party_status: list[dict]            # {name, lvl, hp, max_hp, alive}
    visited_count: int = 0
    visited_sample: list[tuple] = field(default_factory=list)  # up to 12 (sx,sy)


def build_curated_context(
    active_goal,
    navlog: NavLog,
    db,
    world_seed: int,
    party: list,
    pos_sx: int,
    pos_sy: int,
    n_events: int = _DEFAULT_N_EVENTS,
) -> CuratedContext:
    """Build a CuratedContext from live world state and the unbounded NavLog."""

    # Recent notable navigation events
    entries = navlog.last_notable(n_events)
    recent_events = [f"t{e.tick} [{e.event_type}] {e.desc}" for e in entries]

    # POI list: enterable features from all known screens, annotated with kind + distance
    raw_features = db.list_known_features(world_seed)
    pois = sorted(
        [
            {
                'sx': f['sx'],
                'sy': f['sy'],
                'feature_type': f['feature_type'],
                'kind': _FEATURE_KIND.get(f['feature_type'], 'dungeon'),
                'visited': bool(f.get('entered')),
                'dist': abs(f['sx'] - pos_sx) + abs(f['sy'] - pos_sy),
            }
            for f in raw_features
            if f.get('enterable')
        ],
        key=lambda p: p['dist'],
    )

    # Visited screen summary
    known_screens = db.list_known_screens(world_seed)
    visited_coords = [(s['sx'], s['sy']) for s in known_screens if s.get('visited')]
    visited_count = len(visited_coords)
    visited_sample = sorted(visited_coords)[:12]

    # Party status snapshot
    party_status = [
        {
            'name': m.name,
            'lvl': m.lvl,
            'hp': m.hp,
            'max_hp': m.max_hp,
            'alive': m.alive,
        }
        for m in party
    ]

    return CuratedContext(
        pos_sx=pos_sx,
        pos_sy=pos_sy,
        active_goal=active_goal,
        recent_events=recent_events,
        pois=pois,
        party_status=party_status,
        visited_count=visited_count,
        visited_sample=visited_sample,
    )
