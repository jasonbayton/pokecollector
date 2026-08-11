"""Deleting a manual card has to unwind every reference, in an order that works.

Sibling of the promotion reference tests, and for the same reason: these run
with foreign keys ENFORCED, which the rest of the suite does not. Both bugs
covered here are invisible under SQLite's default of ignoring foreign keys, and
both fail in PostgreSQL, which is what production runs.
"""
import datetime
import os
import unittest

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from api.cards import delete_custom_card
    from database import Base
    from models import (
        Binder,
        BinderCard,
        Card,
        CollectionItem,
        DeletedCollectionItem,
        ImageCache,
        User,
    )
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed")
class DeleteCustomCardReferenceTests(unittest.TestCase):
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

        self.card = Card(
            id="custom-doomed", name="Bergmite", set_id="me04", number="24",
            lang="en", is_custom=True, custom_owner_id=self.user.id,
        )
        # A bystander, so a global delete cannot pass for a scoped one.
        self.other_card = Card(
            id="custom-mikas", name="Sprigatito", set_id="me04", number="25",
            lang="en", is_custom=True, custom_owner_id=self.other.id,
        )
        self.db.add_all([self.card, self.other_card])
        self.db.commit()

        self.db.add_all([
            DeletedCollectionItem(
                card_id=self.other_card.id, user_id=self.other.id, quantity=1,
                condition="NM", variant="Normal", lang="en",
                deleted_at=datetime.datetime.utcnow(),
            ),
            ImageCache(image_key=f"card:{self.other_card.id}:small",
                       data=b"x", content_type="image/png"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def assertBystanderUntouched(self):
        self.assertIsNotNone(
            self.db.query(Card).filter(Card.id == "custom-mikas").first())
        self.assertEqual(
            self.db.query(DeletedCollectionItem).filter(
                DeletedCollectionItem.card_id == "custom-mikas").count(), 1)
        self.assertEqual(
            self.db.query(ImageCache).filter(
                ImageCache.image_key == "card:custom-mikas:small").count(), 1)

    def test_a_card_in_a_binder_can_still_be_deleted(self):
        """binder_cards.collection_item_id is a NO ACTION FK to collection.id.

        Removing the collection rows first left a binder slot pointing at a row
        that was going away, so PostgreSQL refused the delete outright and the
        owner could not remove their own card.
        """
        item = CollectionItem(
            card_id=self.card.id, user_id=self.user.id, quantity=1,
            condition="NM", variant="Normal", lang="en",
        )
        binder = Binder(name="Deck", user_id=self.user.id, binder_type="collection")
        self.db.add_all([item, binder])
        self.db.commit()
        self.db.refresh(item)
        self.db.refresh(binder)
        self.db.add(BinderCard(
            binder_id=binder.id, card_id=self.card.id,
            collection_item_id=item.id, required_quantity=1,
        ))
        self.db.commit()

        delete_custom_card(self.card.id, db=self.db, current_user=self.user)

        self.assertIsNone(self.db.query(Card).filter(Card.id == self.card.id).first())
        self.assertEqual(self.db.query(BinderCard).count(), 0)
        self.assertEqual(self.db.query(CollectionItem).count(), 0)
        self.assertBystanderUntouched()

    def test_an_archived_copy_does_not_outlive_the_card(self):
        """The recycle bin outlives the collection row it came from.

        Deleting the card left the archived entry pointing at nothing, and
        restore only checks that the card exists, so the row sat there as a
        permanently unrestorable "card missing" entry.
        """
        self.db.add(DeletedCollectionItem(
            card_id=self.card.id, user_id=self.user.id, quantity=1,
            condition="NM", variant="Normal", lang="en",
            deleted_at=datetime.datetime.utcnow(),
        ))
        self.db.commit()

        delete_custom_card(self.card.id, db=self.db, current_user=self.user)

        self.assertEqual(
            self.db.query(DeletedCollectionItem).filter(
                DeletedCollectionItem.card_id == self.card.id).count(), 0)
        self.assertBystanderUntouched()

    def test_the_cards_cached_images_go_with_it(self):
        self.db.add(ImageCache(
            image_key=f"card:{self.card.id}:small", data=b"x", content_type="image/png"))
        self.db.commit()

        delete_custom_card(self.card.id, db=self.db, current_user=self.user)

        self.assertEqual(
            self.db.query(ImageCache).filter(
                ImageCache.image_key == f"card:{self.card.id}:small").count(), 0)
        self.assertBystanderUntouched()


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class DeleteCustomCardReferencePostgresTests(DeleteCustomCardReferenceTests):
    """The same cases against the real schema, where these actually failed."""

    def _engine(self):
        return create_engine(POSTGRES_TEST_URL)

    def setUp(self):
        database = POSTGRES_TEST_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in database.lower():
            raise unittest.SkipTest(
                f"refusing to drop tables in database {database!r}: "
                "name it something containing 'test' to opt in"
            )
        engine = self._engine()
        try:
            Base.metadata.drop_all(engine)
        finally:
            engine.dispose()
        super().setUp()


if __name__ == "__main__":
    unittest.main()
