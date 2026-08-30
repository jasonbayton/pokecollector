"""Regression coverage for atomic rapid entry from one cached set checklist."""

import unittest
from types import SimpleNamespace
from unittest.mock import patch

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import Card, CollectionItem, Set, User
    from services import rapid_set_entry

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "Rapid entry dependencies are not installed")
class RapidSetEntryTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite://", connect_args={"check_same_thread": False}, poolclass=StaticPool)
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(username="rapid-entry-owner", hashed_password="x", is_active=True)
        self.set = Set(id="rapid-set_en", tcg_set_id="rapid-set", name="Rapid set", lang="en")
        self.card_one = Card(id="rapid-set-001_en", tcg_card_id="rapid-set-001", name="One", set_id="rapid-set", number="001", lang="en")
        self.card_one_de = Card(id="rapid-set-001_de", tcg_card_id="rapid-set-001", name="Eins", set_id="rapid-set", number="001", lang="de")
        self.card_two = Card(id="rapid-set-002_en", tcg_card_id="rapid-set-002", name="Two", set_id="rapid-set", number="002", lang="en")
        self.bystander = Card(id="other-001_en", tcg_card_id="other-001", name="Other", set_id="other", number="001", lang="en")
        self.db.add_all([self.user, self.set, self.card_one, self.card_one_de, self.card_two, self.bystander])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    @staticmethod
    def _item(card_id, quantity=1, condition="Mint", variant="Normal", lang="en"):
        return SimpleNamespace(card_id=card_id, quantity=quantity, condition=condition, variant=variant, lang=lang)

    def _commit(self, items):
        return rapid_set_entry.commit_rapid_set_entry(
            self.db, set_id=self.set.id, items=items, current_user=self.user,
        )

    def test_merges_session_rows_and_preserves_a_bystander(self):
        existing = CollectionItem(card_id=self.card_one.id, user_id=self.user.id, quantity=4, condition="Mint", variant="Normal", lang="en")
        bystander = CollectionItem(card_id=self.bystander.id, user_id=self.user.id, quantity=8, condition="LP", variant="Holo", lang="en")
        self.db.add_all([existing, bystander])
        self.db.commit()

        result = self._commit([
            self._item(self.card_one.id, 2),
            self._item(self.card_two.id, 1, condition="NM", variant="Holo"),
            self._item(self.card_two.id, 3, condition="NM", variant="Holo"),
        ])

        self.db.expire_all()
        self.assertEqual(result, {"added": 1, "updated": 1, "quantity": 6})
        self.assertEqual(self.db.get(CollectionItem, existing.id).quantity, 6)
        self.assertEqual(self.db.get(CollectionItem, bystander.id).quantity, 8)
        created = self.db.query(CollectionItem).filter(CollectionItem.card_id == self.card_two.id).one()
        self.assertEqual((created.quantity, created.condition, created.variant), (4, "NM", "Holo"))

    def test_resolves_a_selected_language_from_the_local_set_catalogue(self):
        self._commit([self._item(self.card_one.id, lang="de")])

        row = self.db.query(CollectionItem).one()
        self.assertEqual((row.card_id, row.lang), (self.card_one_de.id, "de"))

    def test_rejects_invalid_metadata_and_cards_outside_the_pinned_set(self):
        for item, expected in (
            (self._item(self.card_one.id, condition="Damaged"), "condition"),
            (self._item(self.card_one.id, variant="Rainbow"), "variant"),
            (self._item(self.bystander.id), "set"),
        ):
            with self.subTest(item=item):
                with self.assertRaises(Exception) as raised:
                    self._commit([item])
                self.assertEqual(raised.exception.status_code, 422)
                self.assertIn(expected, raised.exception.detail["message"])
                self.assertEqual(self.db.query(CollectionItem).count(), 0)

    def test_rows_are_locked_in_a_fixed_order_whatever_order_they_arrive_in(self):
        # Two sessions filing the same cards in opposite orders would deadlock
        # if each locked rows in the order its browser happened to send: one
        # holds the row the other is waiting for. Ordering is what prevents
        # it, so ordering is what this asserts - the deadlock itself is a
        # PostgreSQL behaviour, and reproducing it would be a race.
        locked = []
        original = rapid_set_entry._locked_collection_item

        def record(db, **kwargs):
            locked.append((kwargs["card_id"], kwargs["condition"], kwargs["variant"], kwargs["lang"]))
            return original(db, **kwargs)

        with patch.object(rapid_set_entry, "_locked_collection_item", side_effect=record):
            self._commit([
                self._item(self.card_two.id),
                self._item(self.card_one.id),
                self._item(self.card_one.id, condition="NM"),
            ])

        self.assertEqual(locked, sorted(locked))
        self.assertEqual(len(locked), 3)

    def test_a_failed_commit_rolls_back_every_rapid_row_and_bystander(self):
        existing = CollectionItem(card_id=self.card_one.id, user_id=self.user.id, quantity=7, condition="Mint", variant="Normal", lang="en")
        bystander = CollectionItem(card_id=self.bystander.id, user_id=self.user.id, quantity=9, condition="LP", variant="Holo", lang="en")
        self.db.add_all([existing, bystander])
        self.db.commit()

        with patch.object(self.db, "commit", side_effect=RuntimeError("test-only commit failure")):
            with self.assertRaisesRegex(RuntimeError, "test-only commit failure"):
                self._commit([self._item(self.card_one.id, 2), self._item(self.card_two.id, 1)])

        self.db.expire_all()
        self.assertEqual(self.db.get(CollectionItem, existing.id).quantity, 7)
        self.assertEqual(self.db.get(CollectionItem, bystander.id).quantity, 9)
        self.assertEqual(self.db.query(CollectionItem).count(), 2)


if __name__ == "__main__":
    unittest.main()
