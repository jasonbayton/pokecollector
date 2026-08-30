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

    def test_a_stated_copy_does_not_join_a_row_of_unassessed_copies(self):
        # The row is a bundle. One stated copy arriving says nothing about the
        # copies already in it, and marking the whole row confirmed hid a
        # possibly misvalued variant from review.
        from api.collection import _row_to_merge_into
        from models import CollectionItem as CI

        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()
        self.assertIs(self._rows()[0].attributes_confirmed, False)

        joined = _row_to_merge_into(
            self.db,
            (
                CI.card_id == self.card.id,
                CI.variant == "Normal",
                CI.lang == "en",
                CI.condition == DEFAULT_CONDITION,
                CI.user_id == self.user.id,
            ),
            stated=True,
        )
        self.assertIsNone(joined)

    def test_an_unassessed_copy_does_not_join_a_stated_row(self):
        from api.collection import _row_to_merge_into
        from models import CollectionItem as CI

        self.db.add(CI(
            card_id=self.card.id, quantity=1, condition=DEFAULT_CONDITION,
            variant="Normal", lang="en", user_id=self.user.id,
            attributes_confirmed=True,
        ))
        self.db.commit()
        joined = _row_to_merge_into(
            self.db,
            (
                CI.card_id == self.card.id,
                CI.variant == "Normal",
                CI.lang == "en",
                CI.condition == DEFAULT_CONDITION,
                CI.user_id == self.user.id,
            ),
            stated=False,
        )
        self.assertIsNone(joined)

    def test_a_row_predating_the_flag_takes_stated_copies_but_not_unassessed_ones(self):
        # Review caught the hole this closes: an unassessed copy merged into an
        # unknown row would never show up in review, because such a row stays
        # unknown - so on any collection that predates the flag, which is every
        # existing one, the misvalued variant would go on being invisible.
        from api.collection import _row_to_merge_into
        from models import CollectionItem as CI

        self.db.add(CI(
            card_id=self.card.id, quantity=1, condition=DEFAULT_CONDITION,
            variant="Normal", lang="en", user_id=self.user.id,
            attributes_confirmed=None,
        ))
        self.db.commit()
        filters = (
            CI.card_id == self.card.id,
            CI.variant == "Normal",
            CI.lang == "en",
            CI.condition == DEFAULT_CONDITION,
            CI.user_id == self.user.id,
        )
        self.assertIsNotNone(
            _row_to_merge_into(self.db, filters, stated=True),
            "a stated copy may join it: nothing there is awaiting review",
        )
        self.assertIsNone(
            _row_to_merge_into(self.db, filters, stated=False),
            "an unassessed copy must get its own row so it stays reviewable",
        )

    def test_an_automatic_copy_does_not_disappear_into_a_legacy_row(self):
        # The same rule through the real automatic path, on the shape every
        # existing collection has: rows that predate the flag.
        from models import CollectionItem as CI

        self.db.add(CI(
            card_id=self.card.id, quantity=3, condition=DEFAULT_CONDITION,
            variant="Normal", lang="en", user_id=self.user.id,
            attributes_confirmed=None,
        ))
        self.db.commit()

        _add_collection_copy(self.db, card=self.card, current_user=self.user)
        self.db.commit()

        rows = self._rows()
        self.assertEqual(len(rows), 2, "the unassessed copy needs its own row")
        self.assertIsNone(rows[0].attributes_confirmed)
        self.assertEqual(rows[0].quantity, 3, "the legacy row is untouched")
        self.assertIs(rows[1].attributes_confirmed, False)
        self.assertEqual(rows[1].quantity, 1)

    def test_an_unstated_condition_is_written_as_mint_by_the_write_path(self):
        # Review rightly called the first version of this weak: it recomputed
        # "condition or DEFAULT_CONDITION" inside the test, so it would have
        # passed even if the write path stopped applying the default. This
        # goes through the real add and reads the row back.
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
        self.assertIs(
            row.attributes_confirmed, False,
            "nobody chose either value, so the row must say so",
        )

    def test_a_fully_stated_add_is_written_as_confirmed(self):
        from api.collection import _add_collection_item
        from schemas import CollectionItemCreate

        _add_collection_item(
            self.db, self.user,
            CollectionItemCreate(card_id=self.card.id, quantity=1, condition="NM", variant="Reverse Holo"),
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
        self.assertIs(row.attributes_confirmed, False)

        update_collection_item(
            row.id,
            CollectionItemUpdate(condition="LP", variant="Reverse Holo"),
            current_user=self.user,
            db=self.db,
        )
        self.db.refresh(row)
        self.assertIs(row.attributes_confirmed, True)

    def test_editing_with_explicit_nulls_settles_nothing(self):
        # exclude_unset keeps a field the caller explicitly sent as null, and
        # both are nullable in the update schema, so presence is not a
        # statement. Review found this reachable.
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
        self.assertIs(
            row.attributes_confirmed, False,
            "sending nulls states nothing, so the row still needs checking",
        )

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
