"""A confident scan identifies the card, not the copy.

Automatic adds file a condition and a variant that nobody assessed. The
condition is a constant, and the variant is inferred from what the catalogue
says the card can be - so a card that exists as both normal and reverse holo is
always filed as Normal. That guess reaches money: effective_market_price values
a Reverse Holo from the holo price fields, so a reverse holo filed as Normal is
valued at the wrong price.

The defaults stay, because a row needs some value and most cards are normal.
What changes is that the row now records whether anybody actually said so.
"""

import unittest

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import Card, CollectionItem, User
    from services.scan_bulk_add import DEFAULT_CONDITION, _add_collection_copy

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed in this lightweight test environment")
class UnconfirmedAttributeTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = User(username="collector", hashed_password="x")
        self.db.add(self.user)
        self.card = Card(
            id="sv03.5-027_en",
            tcg_card_id="sv03.5-027",
            name="Sandshrew",
            set_id="sv03.5",
            number="027",
            lang="en",
            is_custom=False,
            variants_normal=True,
            variants_reverse=True,
        )
        self.db.add(self.card)
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _rows(self):
        return self.db.query(CollectionItem).order_by(CollectionItem.id).all()

    def test_an_automatic_add_records_that_nobody_assessed_it(self):
        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()
        row = self._rows()[0]
        self.assertEqual(row.condition, DEFAULT_CONDITION)
        self.assertIs(row.attributes_confirmed, False)

    def test_a_second_automatic_copy_joins_the_unconfirmed_row(self):
        for _ in range(2):
            _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 2)
        self.assertIs(rows[0].attributes_confirmed, False)

    def test_an_automatic_add_never_joins_a_row_somebody_confirmed(self):
        # The case that matters. Merging here would silently extend the
        # owner's statement about condition and variant to a copy nobody has
        # looked at.
        stated = CollectionItem(
            card_id=self.card.id,
            quantity=1,
            condition=DEFAULT_CONDITION,
            variant="Normal",
            purchase_price=None,
            lang="en",
            user_id=self.user.id,
            attributes_confirmed=True,
        )
        self.db.add(stated)
        self.db.commit()

        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()

        rows = self._rows()
        self.assertEqual(len(rows), 2, "the confirmed row should not have absorbed it")
        self.assertEqual(rows[0].quantity, 1)
        self.assertIs(rows[0].attributes_confirmed, True)
        self.assertIs(rows[1].attributes_confirmed, False)

    def test_a_row_predating_the_flag_becomes_unconfirmed_when_one_joins_it(self):
        # NULL means nobody knows. Once an unassessed copy lands in the row,
        # something is known: it contains one.
        legacy = CollectionItem(
            card_id=self.card.id,
            quantity=3,
            condition=DEFAULT_CONDITION,
            variant="Normal",
            purchase_price=None,
            lang="en",
            user_id=self.user.id,
            attributes_confirmed=None,
        )
        self.db.add(legacy)
        self.db.commit()

        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()

        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 4)
        self.assertIs(rows[0].attributes_confirmed, False)

    def test_rows_predating_the_flag_are_left_unknown_rather_than_guessed(self):
        legacy = CollectionItem(
            card_id=self.card.id,
            quantity=1,
            condition="NM",
            variant="Holo",
            lang="en",
            user_id=self.user.id,
        )
        self.db.add(legacy)
        self.db.commit()
        self.db.refresh(legacy)
        self.assertIsNone(
            legacy.attributes_confirmed,
            "an untouched row must not claim to have been assessed or not",
        )


if __name__ == "__main__":
    unittest.main()
