import unittest

try:
    from services.sync_service import _is_plausible_match
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False

skip_without_deps = unittest.skipUnless(
    DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed in this lightweight test environment"
)


class FakeCard:
    def __init__(self, name=None, number=None, set_id="me02", lang="en"):
        self.id = "custom-probe"
        self.name = name
        self.number = number
        self.set_id = set_id
        self.lang = lang


@skip_without_deps
class PlausibleMatchTests(unittest.TestCase):
    """Migration moves collection, wishlist and binder rows and then deletes the
    manual card, so a suggestion has to be confirmed rather than assumed from an
    id resolving."""

    def test_matching_name_and_number_is_accepted(self):
        card = FakeCard(name="Charmeleon", number="12")
        api = {"name": "Charmeleon", "localId": "012"}
        self.assertTrue(_is_plausible_match(card, api))

    def test_a_suffixed_number_is_not_satisfied_by_its_base(self):
        # The wrong-card scenario: 4a must never accept the real card 4.
        card = FakeCard(name="Charizard", number="4a")
        api = {"name": "Charizard", "localId": "4"}
        self.assertFalse(_is_plausible_match(card, api))

    def test_a_different_name_is_rejected_even_when_the_number_agrees(self):
        card = FakeCard(name="Pikachu", number="12")
        api = {"name": "Charmeleon", "localId": "012"}
        self.assertFalse(_is_plausible_match(card, api))

    def test_name_comparison_ignores_case_and_padding(self):
        card = FakeCard(name="  charMELEON ", number="012/094")
        api = {"name": "Charmeleon", "localId": "12"}
        self.assertTrue(_is_plausible_match(card, api))

    def test_a_card_entered_without_a_number_falls_back_to_the_name(self):
        # Common: a manual entry with only a name. The name then has to carry it.
        card = FakeCard(name="Charmeleon", number=None)
        self.assertTrue(_is_plausible_match(card, {"name": "Charmeleon"}))
        self.assertFalse(_is_plausible_match(card, {"name": "Charizard"}))

    def test_a_card_entered_without_a_name_falls_back_to_the_number(self):
        card = FakeCard(name=None, number="12")
        self.assertTrue(_is_plausible_match(card, {"localId": "012"}))
        self.assertFalse(_is_plausible_match(card, {"localId": "13"}))

    def test_nothing_to_check_is_rejected(self):
        # No name and no number means no evidence; refusing is the safe default.
        self.assertFalse(_is_plausible_match(FakeCard(), {"name": "Charmeleon", "localId": "012"}))

    def test_an_empty_api_payload_is_rejected(self):
        card = FakeCard(name="Charmeleon", number="12")
        self.assertFalse(_is_plausible_match(card, {}))


if __name__ == "__main__":
    unittest.main()
