"""Placements must not outlive the things they hang off.

Slots cascade from their parent entry through a database foreign key, not an
ORM relationship, so bulk deletes that bypass the ORM still clean up. These
cover the deletion paths outside the binder layout code itself, where a leftover
slot would be an orphan pointing at an entry that no longer exists.
"""
import os
import unittest

try:
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from database import Base
    from models import Binder, BinderCard, BinderSlot, Card, CollectionItem, User
    from services import binder_slots
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class BinderSlotCascadeTests(unittest.TestCase):
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
        self.other = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.db.add_all([self.user, self.other])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.other)

        self.card = Card(id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito",
                         set_id="sv1", number="1", lang="en")
        self.db.add(self.card)
        self.db.commit()

        self.binder = self._binder(self.user)
        self.entry = self._entry(self.binder, self.user)
        binder_slots.place(self.db, self.binder, self.entry, page=1, pocket=1)

        # Another user's binder, which none of these deletions may touch.
        self.other_binder = self._binder(self.other)
        self.other_entry = self._entry(self.other_binder, self.other)
        binder_slots.place(self.db, self.other_binder, self.other_entry, page=1, pocket=1)
        self.db.commit()

        # Held separately: reading .id off a deleted instance makes SQLAlchemy
        # reload a row that is no longer there.
        self.binder_id = self.binder.id
        self.other_binder_id = self.other_binder.id

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _binder(self, owner):
        binder = Binder(name="Album", user_id=owner.id, binder_type="collection",
                        grid_rows=3, grid_columns=3)
        self.db.add(binder)
        self.db.commit()
        self.db.refresh(binder)
        return binder

    def _entry(self, binder, owner):
        item = CollectionItem(card_id=self.card.id, user_id=owner.id, quantity=1,
                              condition="NM", variant="Normal", lang="en")
        self.db.add(item)
        self.db.commit()
        self.db.refresh(item)
        entry = BinderCard(binder_id=binder.id, card_id=self.card.id,
                           collection_item_id=item.id, required_quantity=1)
        self.db.add(entry)
        self.db.commit()
        self.db.refresh(entry)
        return entry

    def assertBystanderIntact(self):
        self.assertEqual(binder_slots.slot_count(self.db, self.other_binder_id), 1)

    def test_deleting_an_entry_through_the_orm_takes_its_slots(self):
        self.db.delete(self.entry)
        self.db.commit()
        self.assertEqual(binder_slots.slot_count(self.db, self.binder_id), 0)
        self.assertBystanderIntact()

    def test_a_bulk_entry_delete_still_takes_its_slots(self):
        """Custom card deletion removes binder entries in bulk, bypassing the ORM."""
        self.db.query(BinderCard).filter(
            BinderCard.card_id == self.card.id,
            BinderCard.binder_id == self.binder_id,
        ).delete(synchronize_session=False)
        self.db.commit()
        self.assertEqual(binder_slots.slot_count(self.db, self.binder_id), 0)
        self.assertBystanderIntact()

    def test_deleting_a_binder_takes_its_entries_and_their_slots(self):
        self.db.delete(self.binder)
        self.db.commit()
        self.assertEqual(self.db.query(BinderSlot).filter(
            BinderSlot.binder_id == self.binder_id).count(), 0)
        self.assertBystanderIntact()

    def test_account_deletion_order_leaves_no_orphans(self):
        """Mirrors api/auth.py: entries go before binders, so the cascade fires."""
        self.db.query(BinderCard).filter(
            BinderCard.binder_id.in_(
                self.db.query(Binder.id).filter(Binder.user_id == self.user.id)
            )
        ).delete(synchronize_session=False)
        self.db.query(Binder).filter(Binder.user_id == self.user.id).delete()
        self.db.commit()

        self.assertEqual(self.db.query(BinderSlot).filter(
            BinderSlot.binder_id == self.binder_id).count(), 0)
        self.assertBystanderIntact()


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class BinderSlotCascadePostgresTests(BinderSlotCascadeTests):
    """Where ON DELETE CASCADE is genuinely enforced."""

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
