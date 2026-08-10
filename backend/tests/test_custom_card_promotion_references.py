"""Promotion has to account for every reference to the manual card.

These run with foreign keys ENFORCED. The rest of the suite does not: SQLite
ignores foreign keys unless asked, and that is why promotion could leave a
NOT NULL price_history row pointing at a card it then deleted, and still pass.
PostgreSQL enforces them always, so this only ever failed in production.

Set TEST_POSTGRES_URL to run the same cases against a real PostgreSQL, which
additionally enforces uq_price_history_card_date.
"""
import datetime
import os
import unittest
from unittest.mock import patch

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import sessionmaker

    from api.cards import migrate_custom_card
    from database import Base
    from models import (
        Card,
        CollectionItem,
        CustomCardMatch,
        PriceHistory,
        ProductPurchase,
        ProductCard,
        ProductLedgerEntry,
        Trade,
        TradeItem,
        User,
    )
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")

FETCHED = {"id": "me04-024", "name": "Bergmite", "localId": "24"}
PARSED = {
    "id": "me04-024_en", "tcg_card_id": "me04-024", "name": "Bergmite",
    "set_id": None, "number": "24", "lang": "en",
}


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed")
class PromotionReferenceTests(unittest.TestCase):
    """The manual card is not gone after promotion, it has become the catalogue
    card, so references to it should follow rather than be cut loose."""

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

        self.card = Card(
            id="custom-legacy", name="Bergmite", set_id="me04", number="24",
            lang="en", is_custom=True,
        )
        self.db.add(self.card)
        self.db.commit()

        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.user.id, quantity=1,
            condition="NM", variant="Normal", lang="en",
        ))
        self.db.add(CustomCardMatch(
            custom_card_id=self.card.id, api_card_id="me04-024", status="pending",
        ))
        self.db.commit()
        self.match = self.db.query(CustomCardMatch).filter(
            CustomCardMatch.custom_card_id == self.card.id
        ).one()

        # A bystander. Every assertion below checks this survived untouched,
        # because "drop the promoted card's price history" and "drop all price
        # history" look identical from a fixture with one card in it, as do
        # "re-point this card's rows" and "re-point every row in the table".
        self.other_card = Card(
            id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito",
            set_id="sv1", number="1", lang="en",
        )
        self.db.add(self.other_card)
        self.db.commit()
        self.other_product = ProductPurchase(
            product_name="Untouched ETB", user_id=self.user.id,
            purchase_price=20, purchase_date=datetime.date(2026, 8, 1),
        )
        self.other_trade = Trade(user_id=self.user.id, trade_date=datetime.date(2026, 8, 1))
        self.db.add_all([self.other_product, self.other_trade])
        self.db.commit()
        self.db.add_all([
            PriceHistory(card_id=self.other_card.id, date=datetime.date(2026, 8, 1), price_trend=99),
            ProductCard(product_id=self.other_product.id, card_id=self.other_card.id,
                        user_id=self.user.id, active_quantity=1),
            ProductLedgerEntry(card_id=self.other_card.id, user_id=self.user.id, quantity=1,
                               product_id=self.other_product.id, amount=9,
                               event_date=datetime.date(2026, 8, 1)),
            TradeItem(trade_id=self.other_trade.id, card_id=self.other_card.id,
                      user_id=self.user.id, direction="incoming", quantity=1),
            CustomCardMatch(custom_card_id=self.other_card.id,
                            api_card_id="sv1-1", status="dismissed"),
        ])
        self.db.commit()

    def assertBystanderUntouched(self):
        """Nothing belonging to another card may move or disappear."""
        self.assertEqual(
            self.db.query(PriceHistory).filter(
                PriceHistory.card_id == self.other_card.id
            ).count(), 1, "another card's price history was destroyed")
        for model in (ProductCard, ProductLedgerEntry, TradeItem):
            self.assertEqual(
                self.db.query(model).filter(model.card_id == self.other_card.id).count(),
                1, f"another card's {model.__name__} row was moved or removed")
        self.assertEqual(
            self.db.query(CustomCardMatch).filter(
                CustomCardMatch.custom_card_id == self.other_card.id
            ).count(), 1, "another card's match row was removed")

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _product(self):
        product = ProductPurchase(
            product_name="ETB", user_id=self.user.id,
            purchase_price=10, purchase_date=datetime.date(2026, 8, 1),
        )
        self.db.add(product)
        self.db.commit()
        return product

    def _promote(self):
        with patch("api.cards.pokemon_api.get_card", return_value=FETCHED), \
             patch("api.cards.pokemon_api.parse_card_for_db",
                   side_effect=lambda *args, **kwargs: dict(PARSED)), \
             patch("api.cards.apply_cross_language_fallbacks",
                   side_effect=lambda _db, value: value):
            return migrate_custom_card(self.match.id, db=self.db, current_user=self.user)

    def test_price_history_does_not_block_promotion(self):
        # price_history.card_id is NOT NULL with no cascade, so an unhandled row
        # made the delete at the end of promotion fail and roll the whole thing
        # back. The user saw "Migration failed" and nothing moved.
        self.db.add(PriceHistory(
            card_id=self.card.id, date=datetime.date(2026, 8, 1), price_trend=10,
        ))
        self.db.commit()

        result = self._promote()

        self.assertEqual(result["api_card_id"], "me04-024_en")
        self.assertIsNone(self.db.query(Card).filter(Card.id == "custom-legacy").first())
        self.assertBystanderUntouched()

    def test_the_manual_cards_price_history_is_dropped_not_carried_over(self):
        # The catalogue card has properly sourced history; a manual card's is a
        # guess, and the unique index on (card_id, date) would reject it anyway.
        self.db.add(PriceHistory(
            card_id=self.card.id, date=datetime.date(2026, 8, 1), price_trend=10,
        ))
        self.db.commit()

        self._promote()

        self.assertEqual(
            self.db.query(PriceHistory).filter(
                PriceHistory.card_id == "custom-legacy"
            ).count(), 0)
        self.assertBystanderUntouched()

    def test_a_product_link_follows_the_card(self):
        product = ProductPurchase(
            product_name="ETB", user_id=self.user.id,
            purchase_price=10, purchase_date=datetime.date(2026, 8, 1),
        )
        self.db.add(product)
        self.db.commit()
        self.db.add(ProductCard(
            product_id=product.id, card_id=self.card.id,
            user_id=self.user.id, active_quantity=1,
        ))
        self.db.commit()

        self._promote()

        link = self.db.query(ProductCard).filter(
            ProductCard.card_id == "me04-024_en").one()
        self.assertEqual(link.card_id, "me04-024_en")
        self.assertBystanderUntouched()

    def test_a_ledger_entry_follows_the_card(self):
        self.db.add(ProductLedgerEntry(
            card_id=self.card.id, user_id=self.user.id, quantity=1,
            product_id=self._product().id, amount=5,
            event_date=datetime.date(2026, 8, 1),
        ))
        self.db.commit()

        self._promote()

        entry = self.db.query(ProductLedgerEntry).filter(
            ProductLedgerEntry.card_id == "me04-024_en").one()
        self.assertEqual(entry.card_id, "me04-024_en")
        self.assertBystanderUntouched()

    def test_a_recorded_trade_follows_the_card(self):
        trade = Trade(user_id=self.user.id, trade_date=datetime.date(2026, 8, 1))
        self.db.add(trade)
        self.db.commit()
        self.db.add(TradeItem(
            trade_id=trade.id, card_id=self.card.id, user_id=self.user.id,
            direction="incoming", quantity=1,
        ))
        self.db.commit()

        self._promote()

        item = self.db.query(TradeItem).filter(
            TradeItem.card_id == "me04-024_en").one()
        self.assertEqual(item.card_id, "me04-024_en")
        self.assertBystanderUntouched()

    def test_an_earlier_dismissed_match_does_not_block_promotion(self):
        """A card can carry more than one match row, and promotion updated one.

        Dismissing a match leaves the row with status "dismissed", and the
        matcher only declines to record a new one while a pending or migrated
        match exists. So dismiss, wait for the catalogue to be re-checked, and
        the card now has two matches. Promoting the live one re-pointed only
        itself, leaving the dismissed row referencing a card about to be
        deleted. custom_card_id is NOT NULL with no cascade, so that is the
        price_history failure again from a different direction.
        """
        self.db.add(CustomCardMatch(
            custom_card_id=self.card.id, api_card_id="me04-024", status="dismissed",
        ))
        self.db.commit()

        result = self._promote()

        self.assertEqual(result["api_card_id"], "me04-024_en")
        self.assertIsNone(self.db.query(Card).filter(Card.id == "custom-legacy").first())
        # Nothing may still point at the deleted card, whatever its status.
        self.assertEqual(
            self.db.query(CustomCardMatch).filter(
                CustomCardMatch.custom_card_id == "custom-legacy"
            ).count(),
            0,
        )
        self.assertBystanderUntouched()

    def test_every_reference_survives_a_promotion_together(self):
        # All of them at once, because they share one transaction and a failure
        # in any rolls back the rest.
        product = ProductPurchase(
            product_name="ETB", user_id=self.user.id,
            purchase_price=10, purchase_date=datetime.date(2026, 8, 1),
        )
        trade = Trade(user_id=self.user.id, trade_date=datetime.date(2026, 8, 1))
        self.db.add_all([product, trade])
        self.db.commit()
        self.db.add_all([
            PriceHistory(card_id=self.card.id, date=datetime.date(2026, 8, 1), price_trend=10),
            ProductCard(product_id=product.id, card_id=self.card.id,
                        user_id=self.user.id, active_quantity=1),
            ProductLedgerEntry(card_id=self.card.id, user_id=self.user.id, quantity=1,
                              product_id=product.id, amount=5,
                              event_date=datetime.date(2026, 8, 1)),
            TradeItem(trade_id=trade.id, card_id=self.card.id, user_id=self.user.id,
                      direction="incoming", quantity=1),
        ])
        self.db.commit()

        self._promote()

        self.assertEqual(
            self.db.query(PriceHistory).filter(
                PriceHistory.card_id == "custom-legacy"
            ).count(), 0)
        self.assertBystanderUntouched()
        self.assertEqual(self.db.query(ProductCard).filter(
            ProductCard.card_id == "me04-024_en").count(), 1)
        self.assertEqual(self.db.query(ProductLedgerEntry).filter(
            ProductLedgerEntry.card_id == "me04-024_en").count(), 1)
        self.assertEqual(self.db.query(TradeItem).filter(
            TradeItem.card_id == "me04-024_en").count(), 1)
        self.assertEqual(self.db.query(CollectionItem).one().card_id, "me04-024_en")
        self.assertBystanderUntouched()
        self.assertIsNone(self.db.query(Card).filter(Card.id == "custom-legacy").first())


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class PromotionReferencePostgresTests(PromotionReferenceTests):
    """The same cases against the real schema.

    PostgreSQL enforces uq_price_history_card_date, which SQLite's in-memory
    schema also declares but which only bites when a row is genuinely
    re-pointed onto an occupied date.
    """

    def _engine(self):
        return create_engine(POSTGRES_TEST_URL)

    def setUp(self):
        # This drops every table in the target database. Refuse to run unless
        # the database is named as a test one, so a TEST_POSTGRES_URL left
        # pointing at something real cannot be wiped by running the suite.
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
