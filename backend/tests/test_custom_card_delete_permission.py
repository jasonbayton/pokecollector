import unittest

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.cards import delete_custom_card
    from database import Base
    from models import Card, CollectionItem, User, WishlistItem
    API_TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    API_TEST_DEPS_AVAILABLE = False


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class DeleteCustomCardPermissionTests(unittest.TestCase):
    """Deleting a manual card removes it from its owner's collection, wishlist
    and binders, so only its owner may do it.

    These previously asserted an admin-only gate, which was an interim fix while
    manual cards were global and unowned. Ownership replaced that, and ownership
    is deliberately independent of account role: an admin who does not own a
    card cannot delete it either.
    """

    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        self.admin = User(username="admin", hashed_password="x", role="admin", is_active=True)
        self.owner = User(username="jason", hashed_password="x", role="trainer", is_active=True)
        self.other = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.db.add_all([self.admin, self.owner, self.other])
        self.db.commit()

        self.card = Card(
            id="custom-mep-067",
            name="Binacle",
            set_id="mep",
            number="067",
            lang="en",
            is_custom=True,
            custom_owner_id=self.owner.id,
        )
        self.db.add(self.card)
        self.db.commit()

        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.owner.id, quantity=1,
            condition="Mint", variant="Normal", lang="en",
        ))
        self.db.add(WishlistItem(card_id=self.card.id, user_id=self.owner.id))
        self.db.commit()

    def _owner_rows(self):
        return (
            self.db.query(CollectionItem).filter(CollectionItem.card_id == self.card.id).count(),
            self.db.query(WishlistItem).filter(WishlistItem.card_id == self.card.id).count(),
        )

    def test_someone_elses_private_card_is_not_even_acknowledged(self):
        # 404 rather than 403, so card ids cannot be probed.
        with self.assertRaises(HTTPException) as caught:
            delete_custom_card(self.card.id, db=self.db, current_user=self.other)
        self.assertEqual(caught.exception.status_code, 404)

    def test_a_shared_template_is_refused_but_acknowledged(self):
        self.card.is_shared_template = True
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            delete_custom_card(self.card.id, db=self.db, current_user=self.other)
        self.assertEqual(caught.exception.status_code, 403)

    def test_an_admin_who_does_not_own_it_cannot_delete_it(self):
        # The behaviour that changed: being an admin is no longer sufficient.
        with self.assertRaises(HTTPException) as caught:
            delete_custom_card(self.card.id, db=self.db, current_user=self.admin)
        self.assertIn(caught.exception.status_code, (403, 404))
        self.assertIsNotNone(self.db.query(Card).filter(Card.id == self.card.id).first())

    def test_a_refused_delete_leaves_the_owners_data_alone(self):
        # Not just a refusal, but nothing destroyed on the way out.
        with self.assertRaises(HTTPException):
            delete_custom_card(self.card.id, db=self.db, current_user=self.other)
        self.assertEqual(self._owner_rows(), (1, 1))
        self.assertIsNotNone(self.db.query(Card).filter(Card.id == self.card.id).first())

    def test_the_owner_can_delete_their_own_card(self):
        delete_custom_card(self.card.id, db=self.db, current_user=self.owner)
        self.assertIsNone(self.db.query(Card).filter(Card.id == self.card.id).first())
        self.assertEqual(self._owner_rows(), (0, 0))


if __name__ == "__main__":
    unittest.main()
