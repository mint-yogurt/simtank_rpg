"""Generate tilemap JSON files from tilerules text files.

Run once from repo root:
    python web/gen_tilemaps.py

Outputs three JSON files to web/static/:
    tilemap_overworld.json  — tile_name: [col, row]
    tilemap_cave.json
    tilemap_town.json

These are static assets loaded by app.js at startup.  Rotation suffixes
(:90/:180/:270) are NOT stored here — app.js parses them at draw time
and rotates the base tile on the canvas.
"""

import json
from pathlib import Path

REPO_ROOT = Path(__file__).parent.parent
TILES_DIR = REPO_ROOT / "web" / "static" / "tiles"
OUT_DIR   = REPO_ROOT / "web" / "static"

TILESETS = {
    "overworld": TILES_DIR / "overworld_1_tilerules.txt",
    "cave":      TILES_DIR / "tiles_cave_rules.txt",
    "town":      TILES_DIR / "tiles_town_rules.txt",
}

# Tiles that towngen synthesises by rotating a base tile.
# Format: derived_name → [base_col, base_row, cw_degrees]
# CW degrees match JS canvas.rotate() convention.
# PIL ROTATE_270 (CCW 270) = CW 90; PIL ROTATE_90 (CCW 90) = CW 270.
TOWN_DERIVED = {
    "gravelN": [15, 0,  90],
    "gravelS": [15, 0, 270],
    "gravelE": [15, 0, 180],
}


def parse_tilerules(path: Path) -> dict:
    """Return {tile_name: [col, row]} from a tilerules file."""
    result = {}
    for line in path.read_text().splitlines():
        line = line.strip()
        if not line or "=" not in line:
            continue
        eq = line.index("=")
        coord_part = line[:eq].strip()
        rest = line[eq + 1:]
        if "#" in rest:
            rest = rest[: rest.index("#")]
        name = rest.split(",")[0].strip().rstrip("_").replace(" ", "")
        if not name:
            continue
        try:
            col, row = [int(v) for v in coord_part.split(",")]
        except ValueError:
            continue
        result[name] = [col, row]
    return result


def main():
    for tileset_name, rules_path in TILESETS.items():
        tilemap = parse_tilerules(rules_path)
        if tileset_name == "town":
            tilemap.update(TOWN_DERIVED)
        out_path = OUT_DIR / f"tilemap_{tileset_name}.json"
        out_path.write_text(json.dumps(tilemap, indent=2) + "\n")
        print(f"Wrote {out_path.name}  ({len(tilemap)} tiles)")


if __name__ == "__main__":
    main()
