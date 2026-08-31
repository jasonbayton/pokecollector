"""Recognition retrieval should prefer a printing's set code and number."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.recognize import (
        _code_number_name_agrees,
        COMPOSITE_PROMPT,
        RECOGNIZE_PROMPT,
        _search_and_rank_candidates,
        match_card_info,
    )
    from database import Base
    from models import Card, Set

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


def _catalogue_client(cards):
    response = Mock(status_code=200)
    response.json = Mock(return_value=cards)
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return Mock(return_value=context)


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/httpx are not installed")
class ScanIdentifyByCodeTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.db.add(Set(
            id="sv1_en", tcg_set_id="sv1", abbreviation="SVI",
            name="Scarlet & Violet", lang="en",
        ))
        self.db.add(Card(
            id="sv1-025_en", tcg_card_id="sv1-025", name="Pikachu",
            set_id="sv1", number="025", lang="en", is_custom=False,
        ))
        self.db.add(Card(
            id="sv1-206_en", tcg_card_id="sv1-206", name="Bystander",
            set_id="sv1", number="206", lang="en", is_custom=False,
        ))
        self.db.commit()

    def tearDown(self):
        self.db.close()

    async def test_code_and_number_identify_a_card_when_the_name_is_unreadable(self):
        candidates, number_matches = await _search_and_rank_candidates(
            self.db,
            {"set_code": "SVI", "number_local": "25", "language": "en"},
            trace=None,
        )

        self.assertEqual([card["tcg_card_id"] for card in candidates], ["sv1-025"])
        self.assertEqual(number_matches, 1)

    async def test_partial_number_is_a_pattern_not_an_exact_identity_match(self):
        self.db.add(Card(
            id="sv1-205_en", tcg_card_id="sv1-205", name="Pattern match",
            set_id="sv1", number="205", lang="en", is_custom=False,
        ))
        self.db.commit()

        candidates, number_matches = await _search_and_rank_candidates(
            self.db,
            {"set_code": "SVI", "number_local": "2?5", "language": "en"},
            trace=None,
        )

        self.assertEqual([card["tcg_card_id"] for card in candidates], ["sv1-205"])
        self.assertEqual(number_matches, 0)

    async def test_name_disagreement_with_a_code_hit_recovers_by_name(self):
        # This mirrors a Fire Energy whose ``SVI 010`` was read as the number
        # for Vivillon.  The wrong code hit must not be the only candidate the
        # review screen offers.
        name_result = [{
            "id": "sv03-230_en",
            "tcg_card_id": "sv03-230",
            "name": "Basic Fire Energy",
            "number": "230",
            "lang": "en",
            "_lang": "en",
        }]
        with patch(
            "api.recognize._search_one_catalogue",
            new=AsyncMock(return_value=(name_result, 1, 0)),
        ):
            result = await match_card_info(
                self.db,
                {
                    "set_code": "SVI",
                    "number_local": "25",
                    "name": "Basic Fire Energy",
                    "language": "en",
                    "card_type": "Energy",
                },
            )

        self.assertFalse(result["_identity_confident"])
        self.assertEqual(result["_identity_decision"], "code_number_unresolved")
        self.assertEqual(result["matches"], [])

    async def test_partial_number_keeps_matching_candidates_reviewable(self):
        result = await match_card_info(
            self.db,
            {
                "set_code": "SVI",
                "number_local": "0?5",
                "name": "Pikachu",
                "language": "en",
            },
        )

        self.assertFalse(result["_identity_confident"])
        self.assertEqual([card["name"] for card in result["matches"]], ["Pikachu"])

    async def test_no_name_or_identifiers_explains_what_could_not_be_read(self):
        with self.assertRaises(HTTPException) as raised:
            await _search_and_rank_candidates(self.db, {"language": "en"}, trace=None)

        self.assertEqual(raised.exception.status_code, 422)
        self.assertIn("name", str(raised.exception.detail).lower())
        self.assertIn("set code", str(raised.exception.detail).lower())

    async def test_name_only_retrieval_still_works(self):
        cards = [{"id": "sv1-025", "name": "Pikachu", "localId": "025"}]
        with patch("api.recognize.httpx.AsyncClient", _catalogue_client(cards)):
            candidates, _ = await _search_and_rank_candidates(
                self.db,
                {"name": "Pikachu", "language": "en"},
                trace=None,
            )

        self.assertEqual([card["tcg_card_id"] for card in candidates], ["sv1-025"])

    def test_prompts_lead_with_identifiers_and_describe_partial_numbers(self):
        for prompt in (RECOGNIZE_PROMPT, COMPOSITE_PROMPT):
            self.assertLess(prompt.index("set code"), prompt.index("name"))
            self.assertIn("`?`", prompt)


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class CodeNumberConfirmationTests(unittest.TestCase):
    """The name check that decides whether a code lookup may be trusted."""

    def test_two_different_japanese_names_do_not_agree(self):
        # Reducing a name to ASCII a-z0-9 left every Japanese, Chinese and
        # Korean name empty, so two different cards both normalised to nothing
        # and read as agreeing. A code-and-number lookup then filed a card
        # whose name contradicted the photograph, confidently and
        # automatically.
        info = {"set_code": "SVI", "number_local": "25", "name": "リザードン", "language": "ja"}
        self.assertFalse(_code_number_name_agrees(info, {"name": "ピカチュウ", "lang": "ja"}))

    def test_the_same_japanese_name_still_agrees(self):
        # The bystander: rejecting everything non-Latin would satisfy the test
        # above while breaking every Japanese scan.
        info = {"set_code": "SVI", "number_local": "25", "name": "リザードン", "language": "ja"}
        self.assertTrue(_code_number_name_agrees(info, {"name": "リザードン", "lang": "ja"}))

    def test_an_english_candidate_is_confirmed_by_the_english_name(self):
        # A Japanese scan of a card retrieved in English must compare against
        # name_en, not the printed Japanese name, or every cross-language hit
        # would look like a contradiction.
        info = {"name": "リザードン", "name_en": "Charizard", "language": "ja"}
        self.assertTrue(_code_number_name_agrees(info, {"name": "Charizard", "lang": "en"}))

    def test_full_width_and_half_width_forms_are_the_same_name(self):
        info = {"name": "Ｃｈａｒｉｚａｒｄ", "language": "en"}
        self.assertTrue(_code_number_name_agrees(info, {"name": "Charizard", "lang": "en"}))

    def test_gender_symbols_are_identity_bearing(self):
        info = {"name": "Nidoran♀", "language": "en"}
        self.assertFalse(_code_number_name_agrees(info, {"name": "Nidoran♂", "lang": "en"}))

    def test_suffixes_are_identity_bearing(self):
        info = {"name": "Charizard ex", "language": "en"}
        self.assertFalse(_code_number_name_agrees(info, {"name": "Charizard", "lang": "en"}))

    def test_unicode_symbols_are_identity_bearing(self):
        info = {"name": "Mew ☆ δ", "language": "en"}
        self.assertFalse(_code_number_name_agrees(info, {"name": "Mew δ", "lang": "en"}))

    def test_a_candidate_with_no_name_is_unknown_rather_than_disagreement(self):
        info = {"name": "Charizard", "language": "en"}
        self.assertTrue(_code_number_name_agrees(info, {"name": "", "lang": "en"}))


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class RemoteSetFilterTests(unittest.TestCase):
    """The remote lookup has to work on the shape TCGdex actually returns."""

    @staticmethod
    def _brief(card_id):
        # A real card brief: id, localId, name, image. No set object.
        return {"id": card_id, "localId": card_id.rsplit("-", 1)[1], "name": "x", "image": "i"}

    def test_the_set_comes_from_the_card_id_because_a_brief_has_no_set(self):
        # TCGdex filters by containment, so a query for sv03 also answers with
        # sv03.5. Reading card["set"]["id"] to tell them apart discarded EVERY
        # remote result, because that key does not exist on a brief.
        from api.recognize import _brief_set_id

        self.assertEqual(_brief_set_id(self._brief("sv03-025")), "sv03")
        self.assertEqual(_brief_set_id(self._brief("sv03.5-025")), "sv03.5")

    def test_an_id_with_no_separator_yields_no_set_rather_than_itself(self):
        from api.recognize import _brief_set_id

        self.assertEqual(_brief_set_id({"id": "oddity"}), "")
        self.assertEqual(_brief_set_id({}), "")


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class LoneCodeNumberConfidenceTests(unittest.TestCase):
    """One observation must not file a card by itself.

    A misread digit retrieves a DIFFERENT real card, which then matches its own
    collector number perfectly. The number agrees with itself, nothing
    contradicts it, and the artwork is never looked at - so a confident
    automatic add files a card that looks nothing like the photograph.
    """

    @staticmethod
    def _candidate(**overrides):
        card = {
            "id": "sv1-25_en", "tcg_card_id": "sv1-25", "name": "Sprigatito",
            "number": "25", "lang": "en", "_lang": "en",
            "_retrieved_by_code_number": True,
        }
        card.update(overrides)
        return card

    def test_a_code_number_hit_alone_does_not_decide(self):
        from api.recognize import _metadata_decision

        confident, decision = _metadata_decision(
            {"set_code": "SVI", "number_local": "25"}, [self._candidate()],
        )
        self.assertFalse(confident)
        self.assertIsNone(decision)

    def test_language_does_not_corrobate_a_code_number_hit(self):
        # Language is extracted by the same vision observation as the number,
        # so it cannot turn a possibly misread digit into an automatic add.
        from api.recognize import _metadata_decision

        confident, decision = _metadata_decision(
            {"set_code": "SVI", "number_local": "25", "language": "en"},
            [self._candidate()],
        )
        self.assertFalse(confident)
        self.assertIsNone(decision)

    def test_a_name_search_hit_on_a_unique_number_still_decides(self):
        # Retrieval by name already constrains the candidate set, so a unique
        # number there carries more than one observation. Unchanged.
        from api.recognize import _metadata_decision

        confident, _ = _metadata_decision(
            {"number_local": "25"},
            [self._candidate(_retrieved_by_code_number=False)],
        )
        self.assertTrue(confident)
