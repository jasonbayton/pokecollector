import unittest

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.collection import (
        list_deleted_collection_items,
        remove_from_collection,
        restore_deleted_collection_item,
    )
    from database import Base
    from models import Card, CollectionItem, DeletedCollectionItem, User
    from services.deleted_collection import rewrite_archived_card_id
    DEPS = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS = False


class _Fixture:
    """Shared setup only. Not a TestCase, or every subclass would re-run
    the parent's tests as well as its own."""

    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        self.owner = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.other = User(username="jordan", hashed_password="x", role="trainer", is_active=True)
        self.admin = User(username="jason", hashed_password="x", role="admin", is_active=True)
        self.db.add_all([self.owner, self.other, self.admin])
        self.db.commit()

        self.card = Card(id="sv08-050_en", name="Quaxly", set_id="sv08", number="050", lang="en")
        self.db.add(self.card)
        self.db.commit()

        self.item = CollectionItem(
            card_id=self.card.id, user_id=self.owner.id, quantity=2,
            condition="Mint", variant="Normal", lang="en",
        )
        self.db.add(self.item)
        self.db.commit()
        self.item_id = self.item.id

    def _delete_as(self, actor):
        return remove_from_collection(self.item_id, current_user=actor, db=self.db)

    def _entry(self):
        return self.db.query(DeletedCollectionItem).first()


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class RecycleBinTests(_Fixture, unittest.TestCase):
    """Undo for the one path a card is lost by accident: manual deletion."""

    def test_deleting_a_card_puts_it_in_the_recycle_bin(self):
        self._delete_as(self.owner)
        entry = self._entry()
        self.assertIsNotNone(entry)
        self.assertEqual(entry.card_id, self.card.id)
        self.assertEqual(entry.quantity, 2)
        self.assertEqual(entry.condition, "Mint")
        self.assertEqual(self.db.query(CollectionItem).count(), 0)

    def test_it_records_who_deleted_it(self):
        # The whole reason for this feature: the access log could say a row was
        # deleted, but not by whom.
        self._delete_as(self.owner)
        self.assertEqual(self._entry().deleted_by_username, "mika")

    def test_the_actor_is_remembered_even_if_the_account_goes(self):
        self._delete_as(self.owner)
        self.db.delete(self.owner)
        self.db.commit()
        # The username is a stored snapshot, not a lookup, so it survives.
        self.assertEqual(self._entry().deleted_by_username, "mika")

    def test_restoring_recreates_the_row_as_it_was(self):
        self._delete_as(self.owner)
        result = restore_deleted_collection_item(self._entry().id, current_user=self.owner, db=self.db)
        self.assertEqual(result["outcome"], "recreated")
        restored = self.db.query(CollectionItem).one()
        self.assertEqual(restored.user_id, self.owner.id)
        self.assertEqual(restored.quantity, 2)
        self.assertEqual(restored.condition, "Mint")
        self.assertEqual(self.db.query(DeletedCollectionItem).count(), 0)

    def test_restoring_onto_an_equivalent_row_merges_quantities(self):
        self._delete_as(self.owner)
        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.owner.id, quantity=1,
            condition="Mint", variant="Normal", lang="en",
        ))
        self.db.commit()
        result = restore_deleted_collection_item(self._entry().id, current_user=self.owner, db=self.db)
        self.assertEqual(result["outcome"], "merged")
        self.assertEqual(self.db.query(CollectionItem).one().quantity, 3)

    def test_a_different_condition_does_not_merge(self):
        self._delete_as(self.owner)
        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.owner.id, quantity=1,
            condition="NM", variant="Normal", lang="en",
        ))
        self.db.commit()
        result = restore_deleted_collection_item(self._entry().id, current_user=self.owner, db=self.db)
        self.assertEqual(result["outcome"], "recreated")
        self.assertEqual(self.db.query(CollectionItem).count(), 2)


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class RecycleBinPermissionTests(_Fixture, unittest.TestCase):
    """Your own deletions are yours; an admin can see and undo everyone's."""

    def test_a_user_sees_only_their_own(self):
        self._delete_as(self.owner)
        self.assertEqual(len(list_deleted_collection_items(current_user=self.owner, db=self.db)), 1)
        self.assertEqual(len(list_deleted_collection_items(current_user=self.other, db=self.db)), 0)

    def test_an_admin_sees_everyones_with_the_owner_named(self):
        self._delete_as(self.owner)
        rows = list_deleted_collection_items(current_user=self.admin, db=self.db)
        self.assertEqual(len(rows), 1)
        self.assertEqual(rows[0]["owner"], "mika")
        self.assertEqual(rows[0]["deleted_by"], "mika")

    def test_another_user_cannot_restore_it_and_cannot_tell_it_exists(self):
        self._delete_as(self.owner)
        with self.assertRaises(HTTPException) as caught:
            restore_deleted_collection_item(self._entry().id, current_user=self.other, db=self.db)
        # 404 not 403, so ids cannot be probed
        self.assertEqual(caught.exception.status_code, 404)
        self.assertEqual(self.db.query(DeletedCollectionItem).count(), 1)

    def test_an_admin_restores_to_the_owner_not_to_themselves(self):
        self._delete_as(self.owner)
        restore_deleted_collection_item(self._entry().id, current_user=self.admin, db=self.db)
        restored = self.db.query(CollectionItem).one()
        self.assertEqual(restored.user_id, self.owner.id)


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class RecycleBinBlockerTests(_Fixture, unittest.TestCase):
    """A restore that cannot succeed is refused, and the snapshot is kept."""

    def test_a_missing_card_blocks_the_restore_and_keeps_the_entry(self):
        self._delete_as(self.owner)
        self.db.delete(self.card)
        self.db.commit()
        rows = list_deleted_collection_items(current_user=self.owner, db=self.db)
        self.assertFalse(rows[0]["restorable"])
        self.assertEqual(rows[0]["restore_blocker"], "card_missing")
        with self.assertRaises(HTTPException) as caught:
            restore_deleted_collection_item(self._entry().id, current_user=self.owner, db=self.db)
        self.assertEqual(caught.exception.status_code, 409)
        self.assertEqual(self.db.query(DeletedCollectionItem).count(), 1)

    def test_a_card_matched_into_the_catalogue_stays_restorable(self):
        # Custom card migration repoints live rows and deletes the old card. An
        # entry archived beforehand must follow, or it is stranded forever.
        custom = Card(id="custom-mep-067", name="Binacle", set_id="mep", number="067", lang="en", is_custom=True)
        self.db.add(custom)
        self.db.commit()
        entry = DeletedCollectionItem(
            user_id=self.owner.id, card_id="custom-mep-067", quantity=1,
            condition="Mint", variant="Normal", lang="en", deleted_by_username="mika",
        )
        self.db.add(entry)
        self.db.commit()

        rewrite_archived_card_id(self.db, "custom-mep-067", self.card.id)
        self.db.commit()

        self.db.refresh(entry)
        self.assertEqual(entry.card_id, self.card.id)
        result = restore_deleted_collection_item(entry.id, current_user=self.owner, db=self.db)
        # The point is that it is restorable at all; setUp leaves a live row for
        # this card, so it merges onto that.
        self.assertIn(result["outcome"], ("merged", "recreated"))


if __name__ == "__main__":
    unittest.main()
