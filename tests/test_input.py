import unittest
from types import SimpleNamespace

from engine.cutscene import CutsceneDef, CutsceneTrigger
from engine.dialogue import DialogueBox
from engine.game_state import GameState
from engine.input import (
    _confirm_shop_transaction,
    _open_container,
    cutscene_id_from_result,
    handle_a_button,
    handle_b_button,
)
from engine.inventory import Inventory, ItemDef
from engine.menu import (
    InventoryMenu, ItemActionMenu, PartyMenu, SaveMenu, SELL, SettingsMenu, ShopMenu, StartMenu,
)
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
        width_span=1, height_span=1,
        dialogue_variants=[DialogueVariant(pages=greeting or [])],
        farewell_variants=[DialogueVariant(pages=farewell or [])],
        stock=stock or [],
    )


def _handle_a(player, npcs, objects, shop_menu, dialogue, game_state, inventory, item_defs):
    return handle_a_button(
        player, StartMenu(), SaveMenu(), InventoryMenu(), SettingsMenu(), shop_menu,
        ItemActionMenu(), PartyMenu(),
        dialogue, npcs, objects, wrap_pages=lambda pages: pages,
        game_state=game_state, inventory=inventory, roster=Roster.fresh(), item_defs=item_defs,
        map_name="town", active_slot=1,
    )


def _npc_fixture(name="wizard", row=1, col=0, npc_id=None, greeting=None):
    """An `npc`-type NPC fixture -- shape matches engine.renderer.NPC's
    dialogue-relevant fields."""
    return SimpleNamespace(
        name=name, type="npc", row=row, col=col, facing="N", npc_id=npc_id,
        width_span=1, height_span=1,
        dialogue_variants=[DialogueVariant(pages=greeting or [])],
    )


def _handle_a_full(player, npcs, objects, dialogue, game_state, shop_menu=None,
                    cutscene_defs=None, map_name="town"):
    """Same shape as _handle_a, but with the full, correct handle_a_button
    signature (item_action_menu/party_menu included) plus cutscene_defs --
    _handle_a's positional args are missing those two and would TypeError,
    see TestShopTalkThenShop's pre-existing (unrelated) failures."""
    return handle_a_button(
        player, StartMenu(), SaveMenu(), InventoryMenu(), SettingsMenu(), shop_menu or ShopMenu(),
        ItemActionMenu(), PartyMenu(),
        dialogue, npcs, objects, wrap_pages=lambda pages: pages,
        game_state=game_state, inventory=Inventory(), roster=Roster.fresh(), item_defs=_defs(),
        map_name=map_name, active_slot=1, cutscene_defs=cutscene_defs,
    )


def _handle_b(player, npcs, shop_menu, dialogue, game_state):
    return handle_b_button(
        player, StartMenu(), SaveMenu(), InventoryMenu(), SettingsMenu(), shop_menu,
        ItemActionMenu(), PartyMenu(), dialogue, npcs, game_state, wrap_pages=lambda pages: pages,
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


class TestNpcTalkCutsceneTrigger(unittest.TestCase):
    """A cutscene with an `event: npc_talk` trigger matching this NPC's
    `name` (+ map + when/unless) plays instead of its ordinary dialogue --
    see engine.input._npc_talk_cutscene/cutscene_id_from_result and
    engine.renderer.OverworldScene's handling of handle_a_button's result."""

    def _idle_player(self):
        player = Player.default()
        player.set_state(PlayerState.IDLE)
        return player

    def _cutscene_defs(self, actor="wizard", when=(), unless=(), map_name="town"):
        cutscene = CutsceneDef(
            id="wizard_intro", map=map_name,
            trigger=CutsceneTrigger(event="npc_talk", actor=actor, when=list(when), unless=list(unless)),
            steps=[],
        )
        return {cutscene.id: cutscene}

    def test_matching_trigger_preempts_dialogue(self):
        player = self._idle_player()
        npc = _npc_fixture(name="wizard", greeting=["Just an ordinary line."])
        dialogue = DialogueBox()
        result = _handle_a_full(player, [npc], [], dialogue, GameState(),
                                 cutscene_defs=self._cutscene_defs())
        self.assertEqual(cutscene_id_from_result(result), "wizard_intro")
        self.assertFalse(dialogue.is_open)   # ordinary dialogue never opened

    def test_no_matching_actor_falls_through_to_ordinary_dialogue(self):
        player = self._idle_player()
        npc = _npc_fixture(name="wizard", greeting=["Just an ordinary line."])
        dialogue = DialogueBox()
        result = _handle_a_full(player, [npc], [], dialogue, GameState(),
                                 cutscene_defs=self._cutscene_defs(actor="someone_else"))
        self.assertIsNone(cutscene_id_from_result(result))
        self.assertTrue(dialogue.is_open)
        self.assertEqual(dialogue.pages, ["Just an ordinary line."])

    def test_unless_flag_blocks_the_cutscene(self):
        player = self._idle_player()
        npc = _npc_fixture(name="wizard", npc_id="wizard", greeting=["Ordinary line."])
        dialogue = DialogueBox()
        game_state = GameState()
        game_state.set_flag("cutscene_seen:wizard_intro")
        cutscene_defs = self._cutscene_defs(unless=["cutscene_seen:wizard_intro"])
        result = _handle_a_full(player, [npc], [], dialogue, game_state, cutscene_defs=cutscene_defs)
        self.assertIsNone(cutscene_id_from_result(result))
        self.assertTrue(dialogue.is_open)

    def test_npc_met_flag_still_sets_when_cutscene_plays(self):
        player = self._idle_player()
        npc = _npc_fixture(name="wizard", npc_id="wizard")
        dialogue = DialogueBox()
        game_state = GameState()
        result = _handle_a_full(player, [npc], [], dialogue, game_state,
                                 cutscene_defs=self._cutscene_defs())
        self.assertIsNotNone(cutscene_id_from_result(result))
        self.assertTrue(game_state.flag("npc_met:wizard"))

    def test_shop_cutscene_trigger_skips_pending_shop(self):
        player = self._idle_player()
        shop = _shopkeeper(greeting=["Welcome!"])
        dialogue = DialogueBox()
        shop_menu = ShopMenu()
        cutscene_defs = self._cutscene_defs(actor="shopkeeper1")
        result = _handle_a_full(player, [shop], [], dialogue, GameState(),
                                 shop_menu=shop_menu, cutscene_defs=cutscene_defs)
        self.assertEqual(cutscene_id_from_result(result), "wizard_intro")
        self.assertFalse(dialogue.is_open)
        self.assertIsNone(shop_menu.pending_shop)
        self.assertFalse(shop_menu.is_open)

    def test_wrong_map_does_not_match(self):
        player = self._idle_player()
        npc = _npc_fixture(name="wizard", greeting=["Ordinary line."])
        dialogue = DialogueBox()
        cutscene_defs = self._cutscene_defs(map_name="some_other_map")
        result = _handle_a_full(player, [npc], [], dialogue, GameState(),
                                 cutscene_defs=cutscene_defs, map_name="town")
        self.assertIsNone(cutscene_id_from_result(result))
        self.assertTrue(dialogue.is_open)


if __name__ == "__main__":
    unittest.main()
