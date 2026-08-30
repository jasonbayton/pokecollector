"""Recognition retrieval should prefer a printing's set code and number."""

import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker

    from api.recognize import (
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

    async def test_name_disagreement_with_a_code_hit_stays_for_review(self):
        result = await match_card_info(
            self.db,
            {
                "set_code": "SVI",
                "number_local": "25",
                "name": "Charizard",
                "language": "en",
            },
        )

        self.assertFalse(result["_identity_confident"])
        self.assertEqual(result["_identity_decision"], "code_number_name_disagrees")
        self.assertEqual(result["matches"][0]["name"], "Pikachu")

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
