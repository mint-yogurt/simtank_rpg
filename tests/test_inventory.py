import unittest

from engine.inventory import Inventory, ItemDef


def _defs() -> dict[str, ItemDef]:
    # Insertion order matters -- sellable_items must preserve it.
    return {
        "a": ItemDef(id="a", name="A", category="consumables", value=5),
        "b": ItemDef(id="b", name="B", category="consumables", value=0),   # unsellable
        "c": ItemDef(id="c", name="C", category="key", value=10),
    }


class TestSellableItems(unittest.TestCase):
    def test_excludes_zero_value_items(self):
        inv = Inventory(counts={"a": 1, "b": 1})
        self.assertEqual(inv.sellable_items(_defs()), ["a"])

    def test_excludes_unowned_items(self):
        inv = Inventory(counts={"a": 1})
        self.assertNotIn("c", inv.sellable_items(_defs()))

    def test_preserves_authored_order(self):
        inv = Inventory(counts={"c": 1, "a": 1})
        self.assertEqual(inv.sellable_items(_defs()), ["a", "c"])


if __name__ == "__main__":
    unittest.main()
