"""Binder layout through the API, and the paths that used to lose placements.

Foreign keys are enforced here, and the whole file runs against real PostgreSQL
when TEST_POSTGRES_URL is set, because pocket uniqueness and the composite
foreign key are database constraints rather than Python checks.
"""
import os
import unittest

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from api.binders import (
        add_card_to_binder,
        clear_binder_slot,
        convert_wishlist_binder_to_collection,
        get_binders,
        convert_collection_binder_to_wishlist,
        create_binder,
        get_binder_page,
        move_binder_slot,
        place_binder_slot,
        update_binder,
        update_binder_entry,
    )
    from database import Base
    from models import Binder, BinderCard, BinderSlot, Card, CollectionItem, User
    from schemas import BinderCardUpdate, BinderCreate, BinderSlotMove, BinderSlotPlace, BinderUpdate
    from services import binder_slots
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed")
class BinderLayoutApiTests(unittest.TestCase):
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
        self.stranger = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.db.add_all([self.user, self.stranger])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.stranger)

        self.card = Card(id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito",
                         set_id="sv1", number="1", lang="en")
        self.db.add(self.card)
        self.db.commit()

        self.binder = self._binder(grid_rows=3, grid_columns=3)
        self.entry = self._entry(self.binder, quantity=4)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _binder(self, **grid):
        response = create_binder(
            BinderCreate(name="Album", binder_type="collection", **grid),
            current_user=self.user, db=self.db,
        )
        return self.db.query(Binder).filter(Binder.id == response.id).one()

    def _entry(self, binder, quantity=1):
        item = CollectionItem(card_id=self.card.id, user_id=self.user.id, quantity=quantity,
                              condition="NM", variant="Normal", lang="en")
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        entry = BinderCard(binder_id=binder.id, card_id=self.card.id,
                           collection_item_id=item.id, required_quantity=quantity)
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def _place(self, page, pocket, entry=None):
        return place_binder_slot(
            self.binder.id,
            BinderSlotPlace(binder_card_id=(entry or self.entry).id, page=page, pocket=pocket),
            current_user=self.user, db=self.db,
        )

    def test_a_binder_can_be_created_mapped(self):
        self.assertEqual((self.binder.grid_rows, self.binder.grid_columns), (3, 3))

    def test_half_a_grid_is_refused(self):
        with self.assertRaises(HTTPException) as caught:
            create_binder(BinderCreate(name="Broken", grid_rows=3),
                          current_user=self.user, db=self.db)
        self.assertEqual(caught.exception.status_code, 422)

    def test_placing_and_reading_back_a_page(self):
        self._place(page=1, pocket=5)
        view = get_binder_page(self.binder.id, page=1, current_user=self.user, db=self.db)
        filled = [p for p in view["pockets"] if p["binder_card_id"] is not None]
        self.assertEqual(len(view["pockets"]), 9)
        self.assertEqual([p["pocket"] for p in filled], [5])

    def test_another_users_binder_is_not_reachable(self):
        with self.assertRaises(HTTPException) as caught:
            get_binder_page(self.binder.id, page=1, current_user=self.stranger, db=self.db)
        self.assertEqual(caught.exception.status_code, 404)

    def test_taking_an_occupied_pocket_is_refused(self):
        self._place(page=1, pocket=1)
        with self.assertRaises(HTTPException) as caught:
            self._place(page=1, pocket=1)
        self.assertEqual(caught.exception.status_code, 409)

    def test_moving_swaps_two_pockets(self):
        second = self._entry(self.binder, quantity=1)
        self._place(page=1, pocket=1)
        self._place(page=1, pocket=2, entry=second)

        move_binder_slot(
            self.binder.id,
            BinderSlotMove(from_page=1, from_pocket=1, to_page=1, to_pocket=2),
            current_user=self.user, db=self.db,
        )

        occupants = {
            (s.page, s.pocket): s.binder_card_id
            for s in self.db.query(BinderSlot).filter(BinderSlot.binder_id == self.binder.id)
        }
        self.assertEqual(occupants[(1, 1)], second.id)
        self.assertEqual(occupants[(1, 2)], self.entry.id)
        self.assertEqual(len(occupants), 2)

    def test_clearing_a_pocket_leaves_the_entry_alone(self):
        self._place(page=1, pocket=1)
        clear_binder_slot(self.binder.id, page=1, pocket=1,
                          current_user=self.user, db=self.db)
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 0)
        self.assertEqual(
            self.db.query(BinderCard).filter(BinderCard.id == self.entry.id).one().required_quantity,
            4,
        )

    def test_reducing_the_quantity_releases_surplus_pockets(self):
        for pocket in (1, 2, 3, 4):
            self._place(page=1, pocket=pocket)

        update_binder_entry(self.binder.id, self.entry.id, BinderCardUpdate(required_quantity=2),
                            current_user=self.user, db=self.db)

        remaining = sorted(
            s.pocket for s in self.db.query(BinderSlot).filter(
                BinderSlot.binder_card_id == self.entry.id
            )
        )
        self.assertEqual(remaining, [1, 2])

    def test_converting_to_a_wishlist_is_refused_while_a_layout_exists(self):
        self._place(page=1, pocket=1)
        with self.assertRaises(HTTPException) as caught:
            convert_collection_binder_to_wishlist(
                self.binder.id, current_user=self.user, db=self.db
            )
        self.assertEqual(caught.exception.status_code, 409)
        # Nothing may have changed on the way out.
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 1)
        self.assertEqual(
            self.db.query(Binder).filter(Binder.id == self.binder.id).one().binder_type,
            "collection",
        )

    def test_shrinking_a_grid_under_an_occupied_pocket_is_refused(self):
        self._place(page=1, pocket=9)
        with self.assertRaises(HTTPException) as caught:
            update_binder(self.binder.id, BinderUpdate(grid_rows=2, grid_columns=2),
                          current_user=self.user, db=self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 1)

    def test_growing_a_grid_is_allowed(self):
        self._place(page=1, pocket=9)
        update_binder(self.binder.id, BinderUpdate(grid_rows=4, grid_columns=3),
                      current_user=self.user, db=self.db)
        binder = self.db.query(Binder).filter(Binder.id == self.binder.id).one()
        self.assertEqual((binder.grid_rows, binder.grid_columns), (4, 3))
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 1)

    def test_converting_a_wishlist_to_collection_is_refused_while_a_layout_exists(self):
        """That conversion rebuilds entries, so placements would land on the wrong ones."""
        wishlist = create_binder(
            BinderCreate(name="Wants", binder_type="wishlist", grid_rows=3, grid_columns=3),
            current_user=self.user, db=self.db,
        )
        binder = self.db.query(Binder).filter(Binder.id == wishlist.id).one()
        entry = BinderCard(binder_id=binder.id, card_id=self.card.id, required_quantity=1)
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        binder_slots.place(self.db, binder, entry, page=1, pocket=1)
        self.db.commit()

        with self.assertRaises(HTTPException) as caught:
            convert_wishlist_binder_to_collection(binder.id, current_user=self.user, db=self.db)
        self.assertEqual(caught.exception.status_code, 409)
        # The status alone is not evidence: this conversion also returns 409
        # when the wishlist is incomplete, so an earlier version of this test
        # passed with the layout guard removed entirely.
        self.assertIn("layout", caught.exception.detail.lower())
        self.assertEqual(binder_slots.slot_count(self.db, binder.id), 1)

    def test_setting_a_lower_quantity_outright_releases_surplus_pockets(self):
        """add_card_to_binder overwrites rather than adds, so it must reconcile too."""
        wishlist = create_binder(
            BinderCreate(name="Wants", binder_type="wishlist", grid_rows=3, grid_columns=3),
            current_user=self.user, db=self.db,
        )
        binder = self.db.query(Binder).filter(Binder.id == wishlist.id).one()
        entry = BinderCard(binder_id=binder.id, card_id=self.card.id, required_quantity=3)
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        for pocket in (1, 2, 3):
            binder_slots.place(self.db, binder, entry, page=1, pocket=pocket)
        self.db.commit()

        add_card_to_binder(binder.id, card_id=self.card.id, required_quantity=1,
                           current_user=self.user, db=self.db)

        self.assertEqual(binder_slots.entry_slot_count(self.db, entry.id), 1)

    def test_the_binder_listing_reports_how_many_pockets_are_filled(self):
        self._place(page=1, pocket=1)
        listed = get_binders(current_user=self.user, db=self.db)
        mine = next(b for b in listed if b.id == self.binder.id)
        self.assertEqual(mine.placed_slot_count, 1)

    def test_clearing_the_layout_is_deliberate_and_keeps_the_entries(self):
        self._place(page=1, pocket=1)
        update_binder(self.binder.id, BinderUpdate(clear_layout=True),
                      current_user=self.user, db=self.db)
        binder = self.db.query(Binder).filter(Binder.id == self.binder.id).one()
        self.assertIsNone(binder.grid_rows)
        self.assertEqual(binder_slots.slot_count(self.db, self.binder.id), 0)
        self.assertEqual(self.db.query(BinderCard).count(), 1)


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class BinderLayoutApiPostgresTests(BinderLayoutApiTests):
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


if __name__ == "__main__":
    unittest.main()
