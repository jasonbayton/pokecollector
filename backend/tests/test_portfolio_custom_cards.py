"""An owned manual card is part of its owner's portfolio.

Valuation loaded collection rows through the catalogue visibility filter, which
decides what to show based on synced sets and languages. A manual card has no
synced set behind it, so it could fall out of the portfolio entirely: the card
shows in the collection, but its value never reaches snapshots or investment
history.
"""
import unittest

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from database import Base
    from models import Card, CollectionItem, Setting, User
    from services.portfolio_valuation import _collection_items
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class PortfolioCustomCardTests(unittest.TestCase):
    def setUp(self):
        self.engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.user = User(username="jason", hashed_password="x", role="admin", is_active=True)
        self.other = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.db.add_all([self.user, self.other])
        self.db.commit()
        self.db.refresh(self.user)
        self.db.refresh(self.other)

        # Only English is synced, which is what makes the filter bite.
        self.db.add(Setting(key="tcgdex_sync_languages", value="en"))
        self.db.commit()

        self.db.add_all([
            Card(id="custom-mine", name="Binacle", set_id="mep", number="067",
                 lang="en", is_custom=True, custom_owner_id=self.user.id,
                 price_trend=12),
            Card(id="custom-theirs", name="Sprigatito", set_id="mep", number="061",
                 lang="en", is_custom=True, custom_owner_id=self.other.id,
                 price_trend=30),
        ])
        self.db.commit()

        self.db.add_all([
            CollectionItem(card_id="custom-mine", user_id=self.user.id, quantity=1,
                           condition="NM", variant="Normal", lang="en"),
            CollectionItem(card_id="custom-theirs", user_id=self.other.id, quantity=1,
                           condition="NM", variant="Normal", lang="en"),
        ])
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def test_an_owned_manual_card_counts_towards_its_owners_portfolio(self):
        items = _collection_items(self.db, self.user.id)
        self.assertEqual({item.card_id for item in items}, {"custom-mine"})

    def test_another_trainers_manual_card_does_not(self):
        items = _collection_items(self.db, self.user.id)
        self.assertNotIn("custom-theirs", {item.card_id for item in items})

    def test_a_manual_card_in_an_unsynced_language_still_counts(self):
        # The case that actually broke. Only English is synced, so catalogue
        # visibility rejected a German manual card even though its owner holds
        # it, and its value disappeared from the portfolio.
        self.db.add(Card(
            id="custom-de", name="Binacle", set_id="mep", number="067",
            lang="de", is_custom=True, custom_owner_id=self.user.id,
            price_trend=12,
        ))
        self.db.commit()
        self.db.add(CollectionItem(
            card_id="custom-de", user_id=self.user.id, quantity=1,
            condition="NM", variant="Normal", lang="de",
        ))
        self.db.commit()

        items = _collection_items(self.db, self.user.id)
        self.assertIn("custom-de", {item.card_id for item in items})


if __name__ == "__main__":
    unittest.main()
