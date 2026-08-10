import unittest

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.cards import search_cards
    from database import Base
    from models import Card, Setting, User
    from services.text_search import accent_insensitive_contains, strip_diacritics
    API_TEST_DEPS_AVAILABLE = True
except ModuleNotFoundError:
    API_TEST_DEPS_AVAILABLE = False


@unittest.skipUnless(API_TEST_DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed in this lightweight test environment")
class AccentInsensitiveSearchTests(unittest.TestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        Session = sessionmaker(bind=engine)
        self.db = Session()
        self.user = User(username="ash", hashed_password="x", role="trainer", is_active=True)
        self.db.add_all([
            self.user,
            Setting(key="tcgdex_sync_languages", value="en,de"),
            Card(
                id="sv1-1_en",
                tcg_card_id="sv1-1",
                name="Pokégear 3.0",
                set_id="sv1",
                number="1",
                rarity="Spécial",
                types=["Item"],
                supertype="Trainer",
                subtypes=["Item"],
                trainer_type="Item",
                artist="José García",
                lang="en",
                is_custom=False,
            ),
            Card(
                id="sv1-2_en",
                tcg_card_id="sv1-2",
                name="Éclair Energy",
                set_id="sv1",
                number="2",
                types=["Lightning"],
                supertype="Energy",
                subtypes=["Basic"],
                energy_type="Basic",
                lang="en",
                is_custom=False,
            ),
            Card(
                id="sv1-3_en",
                tcg_card_id="sv1-3",
                name="Professor's Research",
                set_id="sv1",
                number="3",
                supertype="Trainer",
                subtypes=["Supporter"],
                trainer_type="Supporter",
                lang="en",
                is_custom=False,
            ),
            Card(
                id="sv2-1_fr",
                tcg_card_id="sv2-1",
                name="Flabébé",
                set_id="sv2",
                number="1",
                lang="fr",
                is_custom=False,
            ),
        ])
        self.db.commit()
        self.db.refresh(self.user)

    def tearDown(self):
        self.db.close()

    def _search_names(self, **kwargs):
        result = search_cards(type_filter=None, db=self.db, current_user=self.user, **kwargs)
        return [card["name"] for card in result["data"]]

    def test_strip_diacritics_normalizes_accents_and_case(self):
        self.assertEqual(strip_diacritics("Pokégear"), "pokegear")
        self.assertEqual(strip_diacritics("FLABÉBÉ"), "flabebe")

    def test_name_search_matches_without_diacritics(self):
        self.assertEqual(self._search_names(name="Pokegear 3.0"), ["Pokégear 3.0"])
        self.assertEqual(self._search_names(name="pokegear"), ["Pokégear 3.0"])
        self.assertEqual(self._search_names(name="Pokégear"), ["Pokégear 3.0"])
        self.assertEqual(self._search_names(name="eclair"), ["Éclair Energy"])

    def test_artist_and_rarity_filters_match_without_diacritics(self):
        self.assertEqual(self._search_names(artist="Jose Garcia"), ["Pokégear 3.0"])
        self.assertEqual(self._search_names(rarity="Special"), ["Pokégear 3.0"])

    def test_category_filter_matches_base_card_metadata(self):
        self.assertEqual(self._search_names(category="Energy"), ["Éclair Energy"])

    def test_subtype_filter_matches_base_card_metadata(self):
        self.assertEqual(self._search_names(subtype="Supporter"), ["Professor's Research"])

    def test_accent_search_still_respects_language_visibility(self):
        # French is not an enabled catalogue language, so the French card stays
        # hidden even though the unaccented term describes it.
        self.assertEqual(self._search_names(name="Flabebe"), [])

        # Enabling French must reveal it for the very same unaccented term.
        # Without this half, the empty result above would look identical to the
        # result of accent handling being broken outright.
        setting = self.db.query(Setting).filter(
            Setting.key == "tcgdex_sync_languages"
        ).first()
        setting.value = "en,de,fr"
        self.db.commit()
        self.assertEqual(self._search_names(name="Flabebe"), ["Flabébé"])

    def test_portable_expression_stays_shallow_enough_for_older_sqlite(self):
        """Structural guard, not a behaviour test.

        The portable fallback used to nest one replace() per accented
        character. That expression is deeper than the parser stack of older
        SQLite builds, which rejected every accent-insensitive search with
        "parser stack overflow", so none of the tests above could run on those
        hosts. Assert the depth stays constant so a reintroduction of the chain
        fails on every SQLite rather than only on old ones.
        """
        predicate = accent_insensitive_contains(self.db, Card.name, "Pokegear")
        compiled = str(predicate.compile(self.db.get_bind()))
        depth = 0
        deepest = 0
        for char in compiled:
            if char == "(":
                depth += 1
                deepest = max(deepest, depth)
            elif char == ")":
                depth -= 1
        self.assertLessEqual(deepest, 8, compiled)


if __name__ == "__main__":
    unittest.main()
