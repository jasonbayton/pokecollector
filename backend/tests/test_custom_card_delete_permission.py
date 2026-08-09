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
    """Custom cards are global. Deleting one removes it from every user's
    collection, wishlist and binders, so it must not be something an ordinary
    trainer can do to other people's data."""

    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

        self.admin = User(username="admin", hashed_password="x", role="admin", is_active=True)
        self.trainer = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.owner = User(username="jason", hashed_password="x", role="trainer", is_active=True)
        self.db.add_all([self.admin, self.trainer, self.owner])
        self.db.commit()

        self.card = Card(
            id="custom-mep-067",
            name="Binacle",
            set_id="mep",
            number="067",
            lang="en",
            is_custom=True,
        )
        self.db.add(self.card)
        self.db.commit()

        # Someone else's copy - the data at risk.
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

    def test_a_trainer_cannot_delete_a_custom_card(self):
        with self.assertRaises(HTTPException) as caught:
            delete_custom_card(self.card.id, db=self.db, current_user=self.trainer)
        self.assertEqual(caught.exception.status_code, 403)

    def test_a_refused_delete_leaves_everyone_elses_data_alone(self):
        # The point of the gate: not just a 403, but nothing destroyed.
        with self.assertRaises(HTTPException):
            delete_custom_card(self.card.id, db=self.db, current_user=self.trainer)
        self.assertEqual(self._owner_rows(), (1, 1))
        self.assertIsNotNone(self.db.query(Card).filter(Card.id == self.card.id).first())

    def test_the_gate_is_checked_before_the_card_is_looked_up(self):
        # A trainer must not be able to probe which card ids exist by comparing
        # 404 against 403.
        with self.assertRaises(HTTPException) as caught:
            delete_custom_card("custom-does-not-exist", db=self.db, current_user=self.trainer)
        self.assertEqual(caught.exception.status_code, 403)

    def test_an_admin_can_still_delete(self):
        delete_custom_card(self.card.id, db=self.db, current_user=self.admin)
        self.assertIsNone(self.db.query(Card).filter(Card.id == self.card.id).first())
        self.assertEqual(self._owner_rows(), (0, 0))


if __name__ == "__main__":
    unittest.main()
