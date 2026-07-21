import unittest
from types import SimpleNamespace

from engine.dialogue import DialogueBox
from engine.game_state import GameState
from engine.input import _confirm_shop_transaction, _open_container, handle_a_button, handle_b_button
from engine.inventory import Inventory, ItemDef
from engine.menu import InventoryMenu, SaveMenu, SELL, SettingsMenu, ShopMenu, StartMenu
from engine.npc import DialogueVariant
from engine.player import Player, PlayerState
from engine.roster import Roster


def _defs() -> dict[str, ItemDef]:
    return {"potion": ItemDef(id="potion", name="POTION", category="consumables", value=5)}


def _shop(price: int = 10):
    return SimpleNamespace(stock=[{"item": "potion", "price": price}])


def _container(name="chest", dialogue=None, contents=None, gold=None):
    return SimpleNamespace(name=name, dialogue=dialogue or [], contents=contents, gold=gold)


def _shopkeeper(name="shopkeeper1", row=1, col=0, greeting=None, farewell=None, stock=None):
    """A shopkeeper NPC fixture -- shape matches engine.renderer.NPC's
    shop-relevant fields (a shop is built from the same pipeline as any
    other NPC, see engine.renderer.OverworldScene._load_map)."""
    return SimpleNamespace(
        name=name, type="shop", row=row, col=col, facing="N", npc_id=None,
        dialogue_variants=[DialogueVariant(pages=greeting or [])],
        farewell_variants=[DialogueVariant(pages=farewell or [])],
        stock=stock or [],
    )


def _handle_a(player, npcs, objects, shop_menu, dialogue, game_state, inventory, item_defs):
    return handle_a_button(
        player, StartMenu(), SaveMenu(), InventoryMenu(), SettingsMenu(), shop_menu,
        dialogue, npcs, objects, wrap_pages=lambda pages: pages,
        game_state=game_state, inventory=inventory, roster=Roster.fresh(), item_defs=item_defs,
        map_name="town", active_slot=1,
    )


def _handle_b(player, npcs, shop_menu, dialogue, game_state):
    return handle_b_button(
        player, StartMenu(), SaveMenu(), InventoryMenu(), SettingsMenu(), shop_menu,
        dialogue, npcs, game_state, wrap_pages=lambda pages: pages,
    )


class TestConfirmShopTransaction(unittest.TestCase):
    def test_buy_success_spends_gold_and_grants_item(self):
        gs = GameState()
        gs.add_gold(30)
        inv = Inventory()
        sm = ShopMenu()
        sm.open("shop1")
        sm.amount = 2
        _confirm_shop_transaction(sm, _shop(price=10), gs, inv, _defs())
        self.assertEqual(gs.gold, 10)
        self.assertEqual(inv.counts.get("potion"), 2)
        self.assertIn("BOUGHT", sm.message)
        self.assertFalse(sm.picking_amount)

    def test_buy_insufficient_funds_changes_nothing(self):
        gs = GameState()
        gs.add_gold(5)
        inv = Inventory()
        sm = ShopMenu()
        sm.open("shop1")
        sm.amount = 1
        _confirm_shop_transaction(sm, _shop(price=10), gs, inv, _defs())
        self.assertEqual(gs.gold, 5)
        self.assertNotIn("potion", inv.counts)
        self.assertEqual(sm.message, "NOT ENOUGH GOLD.")

    def test_sell_clamps_to_owned_quantity(self):
        gs = GameState()
        inv = Inventory(counts={"potion": 2})
        sm = ShopMenu()
        sm.open("shop1")
        sm.mode = SELL
        sm.amount = 5   # stale/larger than owned -- must clamp, not oversell
        _confirm_shop_transaction(sm, _shop(), gs, inv, _defs())
        self.assertEqual(gs.gold, 10)   # 2 owned * value(5)
        self.assertNotIn("potion", inv.counts)   # fully sold, popped
        self.assertIn("SOLD", sm.message)


class TestOpenContainer(unittest.TestCase):
    def test_gold_only_credits_wallet_and_appends_page(self):
        gs = GameState()
        inv = Inventory()
        pages = _open_container(_container(gold=15), gs, inv, _defs(), "town")
        self.assertEqual(gs.gold, 15)
        self.assertEqual(inv.counts, {})
        self.assertIn("Found $15.", pages)

    def test_item_only_unaffected_by_gold_change(self):
        gs = GameState()
        inv = Inventory()
        pages = _open_container(_container(contents="potion"), gs, inv, _defs(), "town")
        self.assertEqual(gs.gold, 0)
        self.assertEqual(inv.counts.get("potion"), 1)
        self.assertIn("Received POTION.", pages)

    def test_both_item_and_gold_are_independent(self):
        gs = GameState()
        inv = Inventory()
        pages = _open_container(_container(contents="potion", gold=7), gs, inv, _defs(), "town")
        self.assertEqual(gs.gold, 7)
        self.assertEqual(inv.counts.get("potion"), 1)
        self.assertIn("Received POTION.", pages)
        self.assertIn("Found $7.", pages)

    def test_neither_is_pure_flavor_text(self):
        gs = GameState()
        inv = Inventory()
        pages = _open_container(_container(dialogue=["Just an empty box."]), gs, inv, _defs(), "town")
        self.assertEqual(gs.gold, 0)
        self.assertEqual(inv.counts, {})
        self.assertEqual(pages, ["Just an empty box."])

    def test_already_opened_returns_none_and_grants_nothing_again(self):
        gs = GameState()
        inv = Inventory()
        container = _container(gold=15)
        _open_container(container, gs, inv, _defs(), "town")
        pages = _open_container(container, gs, inv, _defs(), "town")
        self.assertIsNone(pages)
        self.assertEqual(gs.gold, 15)   # not double-granted


class TestShopTalkThenShop(unittest.TestCase):
    """A shop is a person -- talking to one is talk-then-shop, not
    straight-to-menu: A opens the greeting in a normal dialogue box first;
    the buy/sell screen only opens once that closes (ShopMenu.pending_shop)
    -- see handle_a_button/handle_b_button in engine.input."""

    def _idle_player(self):
        player = Player.default()
        player.set_state(PlayerState.IDLE)
        return player

    def test_facing_shopkeeper_opens_greeting_not_shop_menu(self):
        player = self._idle_player()
        shop = _shopkeeper(greeting=["Welcome!"])
        dialogue = DialogueBox()
        shop_menu = ShopMenu()
        _handle_a(player, [shop], [], shop_menu, dialogue, GameState(), Inventory(), _defs())
        self.assertTrue(dialogue.is_open)
        self.assertEqual(dialogue.pages, ["Welcome!"])
        self.assertEqual(shop_menu.pending_shop, "shopkeeper1")
        self.assertFalse(shop_menu.is_open)
        self.assertEqual(player.state, PlayerState.IN_DIALOGUE)

    def test_closing_greeting_opens_shop_menu(self):
        player = self._idle_player()
        player.set_state(PlayerState.IN_DIALOGUE)
        shop = _shopkeeper(greeting=["Welcome!"])
        dialogue = DialogueBox()
        dialogue.open(["Welcome!"])
        dialogue.chars_shown = len("Welcome!")   # fully revealed -- next A closes it
        shop_menu = ShopMenu()
        shop_menu.pending_shop = "shopkeeper1"
        _handle_a(player, [shop], [], shop_menu, dialogue, GameState(), Inventory(), _defs())
        self.assertFalse(dialogue.is_open)
        self.assertTrue(shop_menu.is_open)
        self.assertEqual(shop_menu.shop_name, "shopkeeper1")
        self.assertIsNone(shop_menu.pending_shop)
        self.assertEqual(player.state, PlayerState.IN_SHOP)

    def test_closing_ordinary_dialogue_still_returns_to_idle(self):
        player = self._idle_player()
        player.set_state(PlayerState.IN_DIALOGUE)
        dialogue = DialogueBox()
        dialogue.open(["Hello."])
        dialogue.chars_shown = len("Hello.")
        shop_menu = ShopMenu()   # pending_shop stays None -- no shop involved
        _handle_a(player, [], [], shop_menu, dialogue, GameState(), Inventory(), _defs())
        self.assertFalse(dialogue.is_open)
        self.assertFalse(shop_menu.is_open)
        self.assertEqual(player.state, PlayerState.IDLE)

    def test_closing_shop_with_no_farewell_returns_to_idle(self):
        player = self._idle_player()
        player.set_state(PlayerState.IN_SHOP)
        shop_menu = ShopMenu()
        shop_menu.open("shopkeeper1")
        shop = _shopkeeper(farewell=[])
        dialogue = DialogueBox()
        _handle_b(player, [shop], shop_menu, dialogue, GameState())
        self.assertFalse(shop_menu.is_open)
        self.assertFalse(dialogue.is_open)
        self.assertEqual(player.state, PlayerState.IDLE)

    def test_closing_shop_with_farewell_shows_dialogue(self):
        player = self._idle_player()
        player.set_state(PlayerState.IN_SHOP)
        shop_menu = ShopMenu()
        shop_menu.open("shopkeeper1")
        shop = _shopkeeper(farewell=["Thanks for stopping by!"])
        dialogue = DialogueBox()
        _handle_b(player, [shop], shop_menu, dialogue, GameState())
        self.assertFalse(shop_menu.is_open)
        self.assertTrue(dialogue.is_open)
        self.assertEqual(dialogue.pages, ["Thanks for stopping by!"])
        self.assertEqual(player.state, PlayerState.IN_DIALOGUE)

    def test_b_mid_amount_pick_cancels_without_closing_shop(self):
        player = self._idle_player()
        player.set_state(PlayerState.IN_SHOP)
        shop_menu = ShopMenu()
        shop_menu.open("shopkeeper1")
        shop_menu.start_amount_pick()
        shop = _shopkeeper()
        dialogue = DialogueBox()
        _handle_b(player, [shop], shop_menu, dialogue, GameState())
        self.assertFalse(shop_menu.picking_amount)
        self.assertTrue(shop_menu.is_open)
        self.assertEqual(player.state, PlayerState.IN_SHOP)


if __name__ == "__main__":
    unittest.main()
