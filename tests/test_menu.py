import unittest

from engine.menu import BUY, SELL, ShopMenu


class TestShopMenu(unittest.TestCase):
    def test_open_resets_state(self):
        m = ShopMenu()
        m.cursor = 3
        m.mode = SELL
        m.message = "leftover"
        m.open("shop1")
        self.assertTrue(m.is_open)
        self.assertEqual(m.shop_name, "shop1")
        self.assertEqual(m.mode, BUY)
        self.assertEqual(m.cursor, 0)
        self.assertFalse(m.picking_amount)
        self.assertEqual(m.amount, 1)
        self.assertIsNone(m.message)

    def test_open_clears_pending_shop(self):
        m = ShopMenu()
        m.pending_shop = "shopkeeper1"
        m.open("shopkeeper1")
        self.assertIsNone(m.pending_shop)

    def test_close_clears_pending_shop(self):
        m = ShopMenu()
        m.pending_shop = "shopkeeper1"
        m.close()
        self.assertIsNone(m.pending_shop)

    def test_mode_toggle_resets_cursor(self):
        m = ShopMenu()
        m.open("shop1")
        m.cursor = 2
        m.move_cursor("E", list_len=5)
        self.assertEqual(m.mode, SELL)
        self.assertEqual(m.cursor, 0)
        m.move_cursor("W", list_len=5)
        self.assertEqual(m.mode, BUY)
        self.assertEqual(m.cursor, 0)

    def test_list_scroll_clamps_not_wraps(self):
        m = ShopMenu()
        m.open("shop1")
        for _ in range(5):
            m.move_cursor("S", list_len=3)
        self.assertEqual(m.cursor, 2)   # clamped at list_len - 1
        m.move_cursor("N", list_len=3)
        self.assertEqual(m.cursor, 1)
        m.move_cursor("N", list_len=3)
        m.move_cursor("N", list_len=3)
        self.assertEqual(m.cursor, 0)   # clamped at 0, not negative

    def test_amount_pick_clamps_at_max_and_one(self):
        m = ShopMenu()
        m.open("shop1")
        m.start_amount_pick()
        self.assertTrue(m.picking_amount)
        self.assertEqual(m.amount, 1)
        for _ in range(5):
            m.move_cursor("E", list_len=1, max_amount=3)
        self.assertEqual(m.amount, 3)
        for _ in range(5):
            m.move_cursor("W", list_len=1, max_amount=3)
        self.assertEqual(m.amount, 1)

    def test_cancel_amount_resets_without_touching_mode_or_cursor(self):
        m = ShopMenu()
        m.open("shop1")
        m.cursor = 2
        m.mode = SELL
        m.start_amount_pick()
        m.amount = 3
        m.cancel_amount()
        self.assertFalse(m.picking_amount)
        self.assertEqual(m.amount, 1)
        self.assertEqual(m.cursor, 2)
        self.assertEqual(m.mode, SELL)


if __name__ == "__main__":
    unittest.main()
