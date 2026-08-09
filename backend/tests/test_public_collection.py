import unittest

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.public import get_public_collection
    from database import Base
    from models import Card, CollectionItem, Setting, User
    from services import public_profile as pp
    DEPS = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS = False


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class PublicCollectionTests(unittest.TestCase):
    """A public collection is a second opt-in on top of a public profile, and it
    must never expose what the owner records privately."""

    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(Setting(key="public_profiles_enabled", value="true"))

        self.user = User(
            username="jason", hashed_password="x", role="trainer", is_active=True,
            public_handle="jason", is_profile_public=True,
            public_show_values=False, public_show_collection=True,
        )
        self.db.add(self.user)
        self.db.commit()

        self.card = Card(
            id="sv08-050_en", name="Quaxly", set_id="sv08", number="050", lang="en",
            rarity="Common", price_market=25.0, price_trend=25.0,
            custom_image_url="https://example.invalid/secret.png",
        )
        self.db.add(self.card)
        self.db.commit()

        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.user.id, quantity=2,
            condition="Mint", variant="Normal", lang="en", purchase_price=12.34,
        ))
        self.db.commit()

    def _fetch(self):
        return get_public_collection("jason", db=self.db, response=None)

    def test_the_collection_is_served_when_shared(self):
        out = self._fetch()
        self.assertEqual(out["unique_card_count"], 1)
        self.assertEqual(out["card_count"], 2)
        self.assertEqual(out["cards"][0]["name"], "Quaxly")

    def test_a_profile_being_public_does_not_share_the_collection(self):
        self.user.public_show_collection = False
        self.db.commit()
        with self.assertRaises(HTTPException) as caught:
            self._fetch()
        # 404 not 403: a closed collection should look like one that never existed.
        self.assertEqual(caught.exception.status_code, 404)

    def test_what_the_owner_records_privately_is_never_exposed(self):
        payload = str(self._fetch())
        for private in ("purchase_price", "12.34", "condition", "Mint"):
            self.assertNotIn(private, payload, f"{private} leaked into a public payload")

    def test_the_raw_custom_image_url_is_never_exposed(self):
        out = self._fetch()
        self.assertNotIn("example.invalid", str(out))
        self.assertNotIn("custom_image_url", out["cards"][0])
        self.assertIn("has_custom_image_fallback", out["cards"][0])

    def test_values_are_withheld_unless_the_owner_shows_them(self):
        out = self._fetch()
        self.assertIsNone(out["cards"][0]["market_value"])
        self.assertIsNone(out["total_value"])

        self.user.public_show_values = True
        self.db.commit()
        out = self._fetch()
        self.assertIsNotNone(out["cards"][0]["market_value"])
        self.assertEqual(out["total_value"], 50.0)

    def test_copies_of_one_card_are_grouped_rather_than_listed(self):
        # A second row in a different condition must not reveal that two rows exist.
        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.user.id, quantity=1,
            condition="NM", variant="Normal", lang="en",
        ))
        self.db.commit()
        out = self._fetch()
        self.assertEqual(out["unique_card_count"], 1)
        self.assertEqual(out["card_count"], 3)

    def test_unique_count_means_distinct_cards_not_variants(self):
        # A binder counts distinct card ids; the collection must agree, or the
        # same card in two variants reads as two cards here and one there.
        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.user.id, quantity=1,
            condition="Mint", variant="Reverse Holo", lang="en",
        ))
        self.db.commit()
        out = self._fetch()
        self.assertEqual(out["unique_card_count"], 1)
        self.assertEqual(out["card_count"], 3)
        self.assertEqual(len(out["cards"]), 2, "variants still render separately")

    def test_another_trainers_cards_are_not_included(self):
        other = User(username="mika", hashed_password="x", role="trainer", is_active=True)
        self.db.add(other)
        self.db.commit()
        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=other.id, quantity=5,
            condition="Mint", variant="Normal", lang="en",
        ))
        self.db.commit()
        self.assertEqual(self._fetch()["card_count"], 2)

    def test_empty_rows_are_not_advertised_as_owned(self):
        self.db.query(CollectionItem).delete()
        self.db.add(CollectionItem(
            card_id=self.card.id, user_id=self.user.id, quantity=0,
            condition="Mint", variant="Normal", lang="en",
        ))
        self.db.commit()
        out = self._fetch()
        self.assertEqual(out["card_count"], 0)
        self.assertEqual(out["unique_card_count"], 0)


@unittest.skipUnless(DEPS, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class SharedSerialiserTests(unittest.TestCase):
    """Binders and the collection must render cards through the same function,
    so a leak cannot be fixed in one surface and left in the other."""

    def test_the_card_shape_has_no_private_fields(self):
        card = Card(
            id="sv1-1_en", name="Sprigatito", set_id="sv1", number="1", lang="en",
            price_market=1.0, price_trend=1.0, custom_image_url="https://example.invalid/x.png",
        )
        out = pp._serialize_card(card, quantity=1, variant="Normal", show_values=False)
        for private in ("purchase_price", "condition", "grade", "custom_image_url", "user_id"):
            self.assertNotIn(private, out)
        self.assertIn("has_custom_image_fallback", out)
        self.assertIsNone(out["market_value"])


if __name__ == "__main__":
    unittest.main()
