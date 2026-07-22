import unittest

from engine.dialogue import DialogueBox


def _fully_reveal(box: DialogueBox) -> None:
    box.chars_shown = len(box.current_page())


class TestDialogueChoices(unittest.TestCase):
    def test_no_choices_never_shows_choices(self):
        box = DialogueBox()
        box.open(["Hello."])
        _fully_reveal(box)
        self.assertFalse(box.is_showing_choices())
        self.assertIsNone(box.confirm_choice())

    def test_choices_not_showing_until_last_page_fully_revealed(self):
        box = DialogueBox()
        box.open(["First page.", "Pick one:"], choices=["Yes", "No"])
        self.assertFalse(box.is_showing_choices())   # first page, not even fully revealed yet
        _fully_reveal(box)
        self.assertFalse(box.is_showing_choices())   # fully revealed, but not the LAST page
        box.advance()
        self.assertEqual(box.page_index, 1)
        self.assertFalse(box.is_showing_choices())   # last page, not fully revealed yet
        _fully_reveal(box)
        self.assertTrue(box.is_showing_choices())

    def test_advance_is_a_no_op_while_choices_showing(self):
        box = DialogueBox()
        box.open(["Pick one:"], choices=["Yes", "No"])
        _fully_reveal(box)
        self.assertTrue(box.is_showing_choices())
        closed = box.advance()
        self.assertFalse(closed)
        self.assertTrue(box.is_open)
        self.assertTrue(box.is_showing_choices())

    def test_move_choice_cursor_wraps(self):
        box = DialogueBox()
        box.open(["Pick one:"], choices=["A", "B", "C"])
        _fully_reveal(box)
        self.assertEqual(box.choice_cursor, 0)
        box.move_choice_cursor(-1)
        self.assertEqual(box.choice_cursor, 2)
        box.move_choice_cursor(1)
        self.assertEqual(box.choice_cursor, 0)

    def test_confirm_choice_returns_cursor_and_caller_must_close(self):
        box = DialogueBox()
        box.open(["Pick one:"], choices=["Yes", "No"])
        _fully_reveal(box)
        box.move_choice_cursor(1)
        self.assertEqual(box.confirm_choice(), 1)
        self.assertTrue(box.is_open)   # confirm_choice doesn't close it itself
        box.close()
        self.assertFalse(box.is_open)
        self.assertEqual(box.choices, [])

    def test_confirm_choice_none_when_not_showing(self):
        box = DialogueBox()
        box.open(["Pick one:"], choices=["Yes", "No"])
        self.assertIsNone(box.confirm_choice())   # not fully revealed yet


if __name__ == "__main__":
    unittest.main()
