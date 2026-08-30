"""A bounded search for the pickers that used to download everything.

The trade and binder pickers loaded the whole collection and filtered it in the
browser to show at most twelve or twenty-four rows. This endpoint does the same
matching server-side and returns only what will be shown, so the cost stops
growing with the collection - and so the next screen that wants "cards I own"
has something to call other than "give me all of them".
"""

import unittest

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api.collection import search_collection
    from database import Base
    from models import Card, CollectionItem, Set, User

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "backend dependencies unavailable")
class CollectionSearchTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.user = User(username="collector", hashed_password="x")
        self.db.add(self.user)
        self.db.add(Set(id="sv1_en", tcg_set_id="sv1", name="Scarlet & Violet",
                        abbreviation="SVI", lang="en"))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    def _card(self, card_id, name, number, set_id="sv1"):
        card = Card(id=card_id, tcg_card_id=card_id.rsplit("_", 1)[0], name=name,
                    set_id=set_id, number=number, lang="en", is_custom=False)
        self.db.add(card)
        self.db.add(CollectionItem(card_id=card_id, user_id=self.user.id, quantity=1,
                                   condition="NM", variant="Normal", lang="en"))
        self.db.commit()
        return card

    def _search(self, **kwargs):
        return search_collection(current_user=self.user, db=self.db, **kwargs)

    def test_it_finds_a_card_by_name(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        self._card("sv1-30_en", "Fuecoco", "30")
        found = self._search(q="sprig")
        self.assertEqual([item.card_id for item in found], ["sv1-25_en"])

    def test_it_finds_a_card_by_set_name(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        self.assertEqual(len(self._search(q="Scarlet")), 1)

    def test_it_finds_a_card_by_collector_number(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        self._card("sv1-30_en", "Fuecoco", "30")
        found = self._search(q="30")
        self.assertEqual([item.card_id for item in found], ["sv1-30_en"])

    def test_a_number_matches_that_collector_number_and_names_containing_it(self):
        # Parity: both pickers searched names by substring, so digits inside a
        # name still match. The number itself is compared on its normalised
        # forms, which is what the binder picker did.
        self._card("sv1-25_en", "Sprigatito", "25")
        self._card("sv1-99_en", "Route 25 Trainer", "99")
        found = {item.card_id for item in self._search(q="25")}
        self.assertEqual(found, {"sv1-25_en", "sv1-99_en"})

    def test_a_partial_number_is_not_a_way_of_asking_for_a_longer_one(self):
        # "2" must not return card 25. The binder picker compared numbers after
        # normalisation, not as substrings.
        self._card("sv1-25_en", "Sprigatito", "25")
        self.assertEqual(self._search(q="2"), [])

    def test_words_may_match_different_fields(self):
        # The trade picker searched one concatenated string, so a card name and
        # its set name together found the card. Checking the whole phrase
        # against each field separately loses that.
        self._card("sv1-25_en", "Sprigatito", "25")
        self._card("sv1-30_en", "Fuecoco", "30")
        found = [item.card_id for item in self._search(q="sprigatito scarlet")]
        self.assertEqual(found, ["sv1-25_en"])

    def test_every_word_has_to_match_something(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        self.assertEqual(self._search(q="sprigatito fuecoco"), [])

    def test_it_understands_the_set_code_and_number_shortcode(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        self._card("sv1-30_en", "Fuecoco", "30")
        for term in ("SVI 25", "svi 25", "sv1 25"):
            with self.subTest(term=term):
                found = self._search(q=term)
                self.assertEqual([item.card_id for item in found], ["sv1-25_en"])

    def test_a_shortcode_does_not_drag_in_unrelated_cards_of_that_number(self):
        # The bystander. "MEW 25" asks for card 25 of the MEW set. Evaluating
        # the query as a shortcode AND as two loose words also returned any
        # card named Mew that happened to be numbered 25, which neither picker
        # did.
        self.db.add(Set(id="mew_en", tcg_set_id="mew", name="151",
                        abbreviation="MEW", lang="en"))
        self.db.commit()
        wanted = self._card("mew-25_en", "Bulbasaur", "25", set_id="mew")
        self._card("sv1-25_en", "Mew", "25")

        found = [item.card_id for item in self._search(q="MEW 25")]
        self.assertEqual(found, [wanted.id])

    def test_a_name_that_looks_like_a_shortcode_still_finds_the_card(self):
        # "Energy Removal 2" searched as "Removal 2" has the shape of a set
        # code and a number, but Removal is not a set this collection holds.
        # Treating it as a set lookup found nothing, where both pickers found
        # the card by name.
        card = self._card("ex1-80_en", "Energy Removal 2", "80")
        found = [item.card_id for item in self._search(q="Removal 2")]
        self.assertEqual(found, [card.id])

    def test_a_padded_number_matches_the_shortcode(self):
        self._card("sv1-007_en", "Charmander", "007")
        self.assertEqual(len(self._search(q="SVI 7")), 1)

    def test_it_returns_no_more_than_the_limit(self):
        for index in range(30):
            self._card(f"sv1-{index}_en", f"Card {index}", str(index))
        self.assertEqual(len(self._search(limit=5)), 5)

    def test_the_limit_is_capped_so_a_caller_cannot_ask_for_everything(self):
        for index in range(60):
            self._card(f"sv1-{index}_en", f"Card {index}", str(index))
        self.assertEqual(len(self._search(limit=10_000)), 50)

    def test_filters_narrow_the_result(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        row = self.db.query(CollectionItem).filter(CollectionItem.card_id == "sv1-25_en").one()
        row.variant = "Holo"
        self.db.commit()
        self.assertEqual(len(self._search(variant="Holo")), 1)
        self.assertEqual(len(self._search(variant="Normal")), 0)
        self.assertEqual(len(self._search(condition="NM")), 1)
        self.assertEqual(len(self._search(condition="LP")), 0)

    def test_an_unsupported_filter_is_refused_rather_than_ignored(self):
        # Silently ignoring it would show the picker cards it was asked to
        # exclude, which is worse than an error.
        for kwargs in ({"variant": "Sparkly"}, {"condition": "Pristine"}):
            with self.subTest(kwargs=kwargs):
                with self.assertRaises(HTTPException) as raised:
                    self._search(**kwargs)
                self.assertEqual(raised.exception.status_code, 422)

    def test_another_users_cards_are_never_returned(self):
        other = User(username="someone-else", hashed_password="x")
        self.db.add(other)
        self.db.commit()
        card = self._card("sv1-25_en", "Sprigatito", "25")
        self.db.query(CollectionItem).filter(CollectionItem.card_id == card.id).update(
            {CollectionItem.user_id: other.id}
        )
        self.db.commit()
        self.assertEqual(self._search(q="sprig"), [])


@unittest.skipUnless(DEPS_AVAILABLE, "backend dependencies unavailable")
class CollectionFacetTests(CollectionSearchTests):
    """The dropdowns that were the binder picker's other reason to load everything."""

    def _facets(self):
        from api.collection import get_collection_facets
        return get_collection_facets(current_user=self.user, db=self.db)

    def test_it_lists_the_sets_present_in_the_collection(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        facets = self._facets()
        # The composite id, which identifies a set in one language.
        self.assertEqual(facets["sets"], [{"id": "sv1_en", "name": "Scarlet & Violet"}])

    def test_a_set_in_two_languages_is_two_options(self):
        # Collapsing to the bare TCGdex id made an English and a German copy of
        # one set the same option, and choosing either returned both.
        self.db.add(Set(id="sv1_de", tcg_set_id="sv1", name="Karmesin & Purpur",
                        abbreviation="SVI", lang="de"))
        self.db.commit()
        self._card("sv1-25_en", "Sprigatito", "25")
        german = Card(id="sv1-25_de", tcg_card_id="sv1-25", name="Felori",
                      set_id="sv1", number="25", lang="de", is_custom=False)
        self.db.add(german)
        self.db.add(CollectionItem(card_id="sv1-25_de", user_id=self.user.id, quantity=1,
                                   condition="NM", variant="Normal", lang="de"))
        self.db.commit()

        ids = [entry["id"] for entry in self._facets()["sets"]]
        self.assertEqual(sorted(ids), ["sv1_de", "sv1_en"])
        self.assertEqual([i.card_id for i in self._search(set_id="sv1_en")], ["sv1-25_en"])
        self.assertEqual([i.card_id for i in self._search(set_id="sv1_de")], ["sv1-25_de"])

    def test_a_facet_id_actually_filters_the_search(self):
        # These two endpoints are used together: the dropdown is built from
        # one and its value is passed to the other. Returning the composite
        # Set.id here made every set filter match nothing.
        self._card("sv1-25_en", "Sprigatito", "25")
        set_id = self._facets()["sets"][0]["id"]
        self.assertEqual(len(self._search(set_id=set_id)), 1)

    def test_it_lists_each_set_once_however_many_cards_are_owned(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        self._card("sv1-30_en", "Fuecoco", "30")
        self.assertEqual(len(self._facets()["sets"]), 1)

    def test_it_lists_the_variants_present(self):
        self._card("sv1-25_en", "Sprigatito", "25")
        row = self.db.query(CollectionItem).one()
        row.variant = "Reverse Holo"
        self.db.commit()
        self.assertEqual(self._facets()["variants"], ["Reverse Holo"])

    def test_it_does_not_leak_another_users_sets(self):
        other = User(username="someone-else", hashed_password="x")
        self.db.add(other)
        self.db.commit()
        self._card("sv1-25_en", "Sprigatito", "25")
        self.db.query(CollectionItem).update({CollectionItem.user_id: other.id})
        self.db.commit()
        facets = self._facets()
        self.assertEqual(facets["sets"], [])
        self.assertEqual(facets["variants"], [])


if __name__ == "__main__":
    unittest.main()
