"""Another trainer's private manual card must not surface in search.

Ownership makes manual cards private by default. The ordinary search excludes
custom cards outright. The "SET 123" code-and-number path did not: it filtered
on catalogue visibility only, which says nothing about who owns a manual card,
so searching a set and number that happened to match somebody else's private
card returned it.

These drive _search_by_code_number itself. An earlier version of this file
asserted against the visibility filters directly, which proved the filters work
and nothing about whether the endpoint uses them - it passed with the leak
reinstated.
"""
import unittest
from unittest.mock import patch

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.cards import _search_by_code_number
    from database import Base
    from models import Card, Set, User
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CustomCardSearchPrivacyTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.owner = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.other = User(username="jason", hashed_password="x", role="admin", is_active=True)
        self.db.add_all([self.owner, self.other])
        self.db.commit()
        self.db.refresh(self.owner)
        self.db.refresh(self.other)

        self.db.add(Set(id="me04_en", tcg_set_id="me04", name="Mega Evolution",
                        abbreviation="ME04", lang="en"))
        self.db.commit()

        self.db.add_all([
            # A real catalogue card, which anyone may see.
            Card(id="me04-024_en", tcg_card_id="me04-024", name="Bergmite",
                 set_id="me04", number="24", lang="en"),
            # mika's private manual card, same set and number.
            Card(id="custom-mika", name="Bergmite (mika's)", set_id="me04",
                 number="24", lang="en", is_custom=True,
                 custom_owner_id=self.owner.id),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _search_as(self, user):
        # The catalogue fetch only runs when nothing local matches, and it must
        # not reach the network from a test either way.
        with patch("api.cards.pokemon_api.get_card", return_value=None), \
             patch("api.cards.pokemon_api.get_set_cards", return_value=[], create=True):
            result = _search_by_code_number(
                db=self.db, current_user=user, set_code="ME04",
                card_number="24", page=1, page_size=20, lang="en",
            )
        return {card["id"] for card in result["data"]}

    def test_another_trainers_private_card_is_not_returned(self):
        found = self._search_as(self.other)
        self.assertNotIn("custom-mika", found)

    def test_the_catalogue_card_is_still_returned(self):
        found = self._search_as(self.other)
        self.assertIn("me04-024_en", found)

    def test_the_owner_still_finds_their_own(self):
        found = self._search_as(self.owner)
        self.assertIn("custom-mika", found)

    def test_a_shared_template_is_still_reachable(self):
        # Sharing is the deliberate opt-in, so it must survive the fix.
        template = self.db.query(Card).filter(Card.id == "custom-mika").one()
        template.is_shared_template = True
        self.db.commit()
        self.assertIn("custom-mika", self._search_as(self.other))


if __name__ == "__main__":
    unittest.main()
