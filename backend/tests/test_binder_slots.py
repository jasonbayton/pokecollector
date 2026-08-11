"""Placement of physical copies into binder pockets.

Run with foreign keys enforced, and against real PostgreSQL when
TEST_POSTGRES_URL is set. The uniqueness of a pocket and the composite foreign
key that ties a slot to its parent entry are database constraints, so a test
backend that ignores them cannot show that they work.
"""
import os
import unittest

try:
    from sqlalchemy import create_engine, event, func
    from sqlalchemy.orm import sessionmaker

    from database import Base
    from models import Binder, BinderCard, BinderSlot, Card, CollectionItem, User
    from services import binder_slots
    from services.binder_layout import MAX_LAYOUT_SLOTS
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class BinderSlotTests(unittest.TestCase):
    def _engine(self):
        engine = create_engine("sqlite:///:memory:")

        @event.listens_for(engine, "connect")
        def _enforce_foreign_keys(dbapi_connection, _record):
            cursor = dbapi_connection.cursor()
            cursor.execute("PRAGMA foreign_keys=ON")
            cursor.close()

        return engine

    def setUp(self):
        self.engine = self._engine()
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.user = User(username="jason", hashed_password="x", role="admin", is_active=True)
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

        self.card = Card(id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito",
                         set_id="sv1", number="1", lang="en")
        self.other_card = Card(id="sv1-2_en", tcg_card_id="sv1-2", name="Floragato",
                               set_id="sv1", number="2", lang="en")
        self.binder = Binder(name="Album", user_id=self.user.id, binder_type="collection",
                             grid_rows=3, grid_columns=3)
        # A second binder, so binder-wide uniqueness cannot be mistaken for
        # global uniqueness and vice versa.
        self.other_binder = Binder(name="Spare", user_id=self.user.id,
                                   binder_type="collection", grid_rows=2, grid_columns=2)
        self.db.add_all([self.card, self.other_card, self.binder, self.other_binder])
        self.db.commit()
        self.db.refresh(self.binder)
        self.db.refresh(self.other_binder)

        self.entry = self._entry(self.binder, self.card, quantity=4)
        self.bystander = self._entry(self.other_binder, self.other_card, quantity=1)
        binder_slots.place(self.db, self.other_binder, self.bystander, page=1, pocket=1)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _entry(self, binder, card, quantity=1):
        item = CollectionItem(card_id=card.id, user_id=self.user.id, quantity=quantity,
                              condition="NM", variant="Normal", lang="en")
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        entry = BinderCard(binder_id=binder.id, card_id=card.id,
                           collection_item_id=item.id, required_quantity=quantity)
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def assertBystanderUntouched(self):
        slot = self.db.query(BinderSlot).filter(
            BinderSlot.binder_id == self.other_binder.id
        ).one()
        self.assertEqual((slot.page, slot.pocket), (1, 1))
        self.assertEqual(slot.binder_card_id, self.bystander.id)

    def test_a_copy_can_be_placed_in_a_pocket(self):
        slot = binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=5)
        self.db.commit()
        self.assertEqual((slot.page, slot.pocket), (1, 5))
        self.assertEqual(binder_slots.entry_slot_count(self.db, self.entry.id), 1)
        self.assertBystanderUntouched()

    def test_the_same_pocket_cannot_hold_two_cards(self):
        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=5)
        self.db.commit()
        with self.assertRaises(binder_slots.SlotError) as caught:
            binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=5)
        self.assertEqual(caught.exception.code, "pocket_taken")

    def test_the_same_pocket_number_in_another_binder_is_fine(self):
        # Uniqueness is per binder, not global: every binder has a page 1.
        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)
        self.db.commit()
        self.assertBystanderUntouched()

    def test_an_entry_cannot_be_placed_more_times_than_it_has_copies(self):
        for pocket in range(1, 5):
            binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=pocket)
        self.db.commit()
        with self.assertRaises(binder_slots.SlotError) as caught:
            binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=6)
        self.assertEqual(caught.exception.code, "entry_fully_placed")

    def test_a_pocket_beyond_the_page_is_refused(self):
        with self.assertRaises(binder_slots.SlotError) as caught:
            binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=10)
        self.assertEqual(caught.exception.code, "bad_position")

    def test_an_unmapped_binder_cannot_hold_placements(self):
        plain = Binder(name="Unmapped", user_id=self.user.id, binder_type="collection")
        self.db.add(plain)
        self.db.commit()
        self.db.refresh(plain)
        entry = self._entry(plain, self.card)
        with self.assertRaises(binder_slots.SlotError) as caught:
            binder_slots.place(self.db, plain, entry, page=1, pocket=1)
        self.assertEqual(caught.exception.code, "not_mapped")

    def test_moving_to_an_empty_pocket(self):
        slot = binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)
        self.db.commit()
        binder_slots.move(self.db, self.binder, slot, page=2, pocket=3)
        self.db.commit()
        self.assertEqual((slot.page, slot.pocket), (2, 3))
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 1)
        self.assertBystanderUntouched()

    def test_moving_onto_an_occupied_pocket_swaps_the_two(self):
        second = self._entry(self.binder, self.other_card, quantity=1)
        first_slot = binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)
        binder_slots.place(self.db, self.binder, second, page=1, pocket=2)
        self.db.commit()

        binder_slots.move(self.db, self.binder, first_slot, page=1, pocket=2)
        self.db.commit()

        occupants = {
            (slot.page, slot.pocket): slot.binder_card_id
            for slot in self.db.query(BinderSlot).filter(
                BinderSlot.binder_id == self.binder.id
            ).all()
        }
        self.assertEqual(occupants[(1, 2)], self.entry.id)
        self.assertEqual(occupants[(1, 1)], second.id)
        # A swap must not lose or invent a placement.
        self.assertEqual(len(occupants), 2)
        self.assertBystanderUntouched()

    def test_reducing_an_entrys_quantity_drops_surplus_placements(self):
        for pocket in range(1, 5):
            binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=pocket)
        self.db.commit()

        self.entry.required_quantity = 2
        removed = binder_slots.reconcile_entry(self.db, self.entry)
        self.db.commit()

        self.assertEqual(removed, 2)
        remaining = sorted(
            slot.pocket for slot in self.db.query(BinderSlot).filter(
                BinderSlot.binder_card_id == self.entry.id
            ).all()
        )
        # The earliest pockets survive, so a deliberate arrangement at the front
        # of the binder is not the part that gets thrown away.
        self.assertEqual(remaining, [1, 2])
        self.assertBystanderUntouched()

    def test_merging_entries_keeps_both_sets_of_placements(self):
        """The regression the three merge callers previously caused."""
        source = self._entry(self.binder, self.other_card, quantity=2)
        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)
        binder_slots.place(self.db, self.binder, source, page=1, pocket=2)
        binder_slots.place(self.db, self.binder, source, page=1, pocket=3)
        self.db.commit()

        binder_slots.merge_binder_cards(self.db, source, self.entry, combined_quantity=3)
        self.db.commit()

        surviving = self.db.query(BinderSlot).filter(
            BinderSlot.binder_id == self.binder.id
        ).all()
        self.assertEqual(len(surviving), 3, "a merge must not discard placements")
        self.assertTrue(all(slot.binder_card_id == self.entry.id for slot in surviving))
        self.assertEqual(
            sorted((slot.page, slot.pocket) for slot in surviving),
            [(1, 1), (1, 2), (1, 3)],
        )
        self.assertIsNone(
            self.db.query(BinderCard).filter(BinderCard.id == source.id).first()
        )
        self.assertBystanderUntouched()

    def test_deleting_an_entry_takes_its_placements_with_it(self):
        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)
        self.db.commit()
        self.db.delete(self.entry)
        self.db.commit()
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 0)
        self.assertBystanderUntouched()

    def test_first_free_positions_skips_what_is_taken(self):
        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)
        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=3)
        self.db.commit()
        self.assertEqual(
            binder_slots.first_free_positions(self.db, self.binder, 3),
            [(1, 2), (1, 4), (1, 5)],
        )

    def test_a_page_view_generates_empty_pockets_rather_than_storing_them(self):
        binder_slots.place(self.db, self.binder, self.entry, page=2, pocket=4)
        self.db.commit()
        view = binder_slots.page_view(self.db, self.binder, page=2)
        self.assertEqual(len(view["pockets"]), 9)
        filled = [p for p in view["pockets"] if p["binder_card_id"] is not None]
        self.assertEqual(len(filled), 1)
        self.assertEqual(filled[0]["pocket"], 4)
        # Nothing was written for the eight empty pockets.
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 1)


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class BinderSlotPostgresTests(BinderSlotTests):
    """The same cases where the constraints are genuinely enforced."""

    def _engine(self):
        return create_engine(POSTGRES_TEST_URL)

    def setUp(self):
        database = POSTGRES_TEST_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in database.lower():
            raise unittest.SkipTest(f"refusing to drop tables in database {database!r}")
        engine = self._engine()
        try:
            Base.metadata.drop_all(engine)
        finally:
            engine.dispose()
        super().setUp()

    def test_the_database_itself_refuses_a_duplicate_pocket(self):
        """Not the service check: the constraint, which is what holds under a race."""
        from sqlalchemy.exc import IntegrityError

        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)
        self.db.commit()
        self.db.add(BinderSlot(binder_card_id=self.entry.id, binder_id=self.binder.id,
                               page=1, pocket=1))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()

    def test_a_slot_cannot_claim_a_binder_its_entry_does_not_belong_to(self):
        from sqlalchemy.exc import IntegrityError

        self.db.add(BinderSlot(binder_card_id=self.entry.id,
                               binder_id=self.other_binder.id, page=1, pocket=9))
        with self.assertRaises(IntegrityError):
            self.db.commit()
        self.db.rollback()


if __name__ == "__main__":
    unittest.main()
