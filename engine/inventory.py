"""Item definitions + the party's shared item pool — pure logic, no pygame.

Mirrors the player/menu split: this module only knows what items exist and
how many of each the party owns. engine.menu.InventoryMenu is the cursor
state for the inventory *screen*, kept separate from this — this module has
no notion of what's currently highlighted or which category page is showing,
same way engine.player has no notion of the start menu being open.

Item *definitions* (name/category/description/effect/etc.) are static,
hand-authored data loaded once from data/items/items.yaml. Item *ownership*
(how many of each the party currently has) is the mutable Inventory below —
shared across the whole party, not per-character (party members will each
hold their own equipped_weapon/equipped_armour once an equip screen exists,
but that's a separate, not-yet-built concern from what's in the bag). It's
the only piece of this module that will ever need to round-trip through a
save file.
"""

from dataclasses import dataclass, field
from pathlib import Path

import yaml

_ITEMS_PATH = Path(__file__).parent.parent / "data" / "items" / "items.yaml"

# Fixed page order for the inventory screen — see engine.menu.InventoryMenu.
CATEGORIES: tuple[str, ...] = ("consumables", "weapon", "armour", "key")
CATEGORY_LABELS: dict[str, str] = {
    "consumables": "ITEMS",
    "weapon":      "WEAPONS",
    "armour":      "EQUIPMENT",
    "key":         "KEY ITEMS",
}


@dataclass(frozen=True)
class ItemDef:
    """One row of data/items/items.yaml. Static — never mutated at runtime."""
    id:          str
    name:        str
    category:    str
    description: str = ""
    icon:        str | None = None
    effect:      dict | None = None
    slot:        str | None = None
    stackable:   bool = False
    value:       int = 0


def load_item_defs(path: Path = _ITEMS_PATH) -> dict[str, ItemDef]:
    """Parse items.yaml into id -> ItemDef, preserving file order (each
    category's items stay contiguous and in authored order, since Python
    dicts preserve insertion order) — Inventory.items_in relies on that
    order for how the list draws on the inventory screen."""
    raw = yaml.safe_load(path.read_text()) or {}
    defs: dict[str, ItemDef] = {}
    for entries in raw.values():
        for entry in entries:
            defs[entry["id"]] = ItemDef(
                id          = entry["id"],
                name        = entry["name"],
                category    = entry["category"],
                description = entry.get("description", ""),
                icon        = entry.get("icon"),
                effect      = entry.get("effect"),
                slot        = entry.get("slot"),
                stackable   = entry.get("stackable", False),
                value       = entry.get("value", 0),
            )
    return defs


@dataclass
class Inventory:
    """The party's shared item pool: item id -> quantity owned.

    Uniform storage for every category — whether an item is "stackable" is
    purely a display distinction (an xN suffix on the inventory screen), not
    a different storage shape, since nothing here tracks per-instance state
    (e.g. weapon durability) that would force items apart into individual
    slots.
    """
    counts: dict[str, int] = field(default_factory=dict)

    def add(self, item_id: str, qty: int = 1) -> None:
        self.counts[item_id] = self.counts.get(item_id, 0) + qty

    def remove(self, item_id: str, qty: int = 1) -> None:
        left = self.counts.get(item_id, 0) - qty
        if left > 0:
            self.counts[item_id] = left
        else:
            self.counts.pop(item_id, None)

    def has(self, item_id: str) -> bool:
        return self.counts.get(item_id, 0) > 0

    def items_in(self, category: str, defs: dict[str, ItemDef]) -> list[str]:
        """Owned item ids in `category`, in items.yaml's authored order —
        what the inventory screen's list is built from for one page."""
        return [item_id for item_id, item_def in defs.items()
                if item_def.category == category and self.has(item_id)]

    def to_dict(self) -> dict:
        return dict(self.counts)

    @classmethod
    def from_dict(cls, d: dict) -> "Inventory":
        return cls(counts=dict(d))
