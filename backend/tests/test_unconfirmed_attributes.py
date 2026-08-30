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

    def test_a_second_automatic_copy_joins_it(self):
        for _ in range(2):
            _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 2)
        self.assertIs(rows[0].attributes_confirmed, False)

    def _seed_row(self, confirmed):
        from models import CollectionItem as CI
        row = CI(
            card_id=self.card.id, quantity=1, condition=DEFAULT_CONDITION,
            variant="Normal", purchase_price=None, lang="en",
            user_id=self.user.id, attributes_confirmed=confirmed,
        )
        self.db.add(row)
        self.db.commit()
        return row

    def test_an_unassessed_copy_taints_whatever_row_it_joins(self):
        # The flag describes the row's contents, so a row that gains a copy
        # nobody assessed wants checking - whether it was previously settled or
        # merely unknown. Merging rather than splitting matters: every existing
        # collection is entirely unknown rows, and splitting them would grow a
        # second row per card while hiding nothing that this does not surface.
        for previous in (True, None):
            with self.subTest(previous=previous):
                self.setUp()
                row = self._seed_row(previous)
                _add_collection_copy(self.db, card=self.card, current_user=self.user)
                self.db.commit()

                rows = self._rows()
                self.assertEqual(len(rows), 1, "it should merge, not split")
                self.assertEqual(rows[0].quantity, 2)
                self.assertIs(
                    rows[0].attributes_confirmed, False,
                    "the row now holds a copy nobody assessed, so it needs checking",
                )

    def test_a_stated_copy_does_not_clear_an_existing_taint(self):
        # One stated copy arriving says nothing about the copies already there.
        from api.collection import _add_collection_item
        from schemas import CollectionItemCreate

        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()

        _add_collection_item(
            self.db, self.user,
            CollectionItemCreate(
                card_id=self.card.id, quantity=1,
                condition=DEFAULT_CONDITION, variant="Normal",
            ),
            commit=True,
        )
        rows = self._rows()
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0].quantity, 2)
        self.assertIs(rows[0].attributes_confirmed, False)

    def test_stating_only_a_condition_does_not_count_as_assessed(self):
        # The wishlist "I have this now" path sends a condition and no variant.
        # Treating that as fully stated reproduced the expensive half of the
        # original defect, because the variant is what reaches valuation.
        from api.collection import _attributes_stated
        from schemas import CollectionItemCreate

        self.assertFalse(_attributes_stated(CollectionItemCreate(card_id="x", condition="NM")))
        self.assertFalse(_attributes_stated(CollectionItemCreate(card_id="x", variant="Holo")))
        self.assertFalse(_attributes_stated(CollectionItemCreate(card_id="x")))
        self.assertTrue(_attributes_stated(CollectionItemCreate(card_id="x", condition="NM", variant="Holo")))

    def test_an_unstated_add_is_written_as_mint_and_needs_checking(self):
        # Through the real add path, reading the row back, so it would fail if
        # the write path stopped storing what it claims to store.
        from api.collection import _add_collection_item
        from schemas import CollectionItemCreate

        _add_collection_item(
            self.db, self.user,
            CollectionItemCreate(card_id=self.card.id, quantity=1),
            commit=True,
        )
        row = self._rows()[0]
        self.assertEqual(row.condition, "Mint")
        self.assertEqual(row.variant, "Normal")
        self.assertIs(row.attributes_confirmed, False)

    def test_a_fully_stated_add_is_written_as_confirmed(self):
        from api.collection import _add_collection_item
        from schemas import CollectionItemCreate

        _add_collection_item(
            self.db, self.user,
            CollectionItemCreate(
                card_id=self.card.id, quantity=1,
                condition="NM", variant="Reverse Holo",
            ),
            commit=True,
        )
        row = self._rows()[0]
        self.assertEqual(row.condition, "NM")
        self.assertEqual(row.variant, "Reverse Holo")
        self.assertIs(row.attributes_confirmed, True)

    def test_editing_a_row_with_both_values_settles_it(self):
        from api.collection import update_collection_item
        from schemas import CollectionItemUpdate

        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()
        row = self._rows()[0]

        update_collection_item(
            row.id,
            CollectionItemUpdate(condition="LP", variant="Reverse Holo"),
            current_user=self.user,
            db=self.db,
        )
        self.db.refresh(row)
        self.assertIs(row.attributes_confirmed, True)

    def test_editing_with_explicit_nulls_settles_nothing(self):
        # exclude_unset keeps a field explicitly sent as null, and both are
        # nullable in the update schema, so presence is not a statement.
        from api.collection import update_collection_item
        from schemas import CollectionItemUpdate

        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()
        row = self._rows()[0]

        update_collection_item(
            row.id,
            CollectionItemUpdate(condition=None, variant=None),
            current_user=self.user,
            db=self.db,
        )
        self.db.refresh(row)
        self.assertIs(row.attributes_confirmed, False)

    def test_rows_predating_the_flag_are_left_unknown_rather_than_guessed(self):
        row = self._seed_row(None)
        self.db.refresh(row)
        self.assertIsNone(
            row.attributes_confirmed,
            "an untouched row must not claim to have been assessed or not",
        )


if __name__ == "__main__":
    unittest.main()
