"""A standby catalogue is consulted only when the first cannot be reached.

The outage that prompted this work was one upstream host being unreachable, so
the app can be pointed at a second catalogue as well. It is deliberately not an
equal peer: a self-hosted build returns the same cards in a different order,
which changes which candidate ranks first for a card whose name and number are
shared. Consulting it routinely would quietly change what the scanner suggests,
so it is only used when there is otherwise no answer at all.
"""

import importlib
import os
import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import httpx
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import Card  # noqa: F401  (registers the tables on Base.metadata)

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


def _card(card_id, name, local_id):
    return {"id": card_id, "name": name, "localId": local_id, "image": f"https://assets.tcgdex.net/{card_id}"}


class _Catalogues:
    """Answer per host, so a test can make one reachable and the other not."""

    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, *args, **kwargs):
        context = AsyncMock()
        context.__aenter__ = AsyncMock(return_value=self)
        context.__aexit__ = AsyncMock(return_value=False)
        return context

    async def get(self, url, **kwargs):
        self.calls.append(url)
        for host, outcome in self.responses.items():
            if host in url:
                if isinstance(outcome, Exception):
                    raise outcome
                response = Mock()
                response.status_code = outcome[0]
                response.json = Mock(return_value=outcome[1])
                return response
        raise AssertionError(f"unexpected catalogue host in {url}")


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class CatalogueStandbyTests(unittest.IsolatedAsyncioTestCase):
    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.card_info = {"name": "Sandshrew", "name_en": "Sandshrew", "number_local": "027", "language": "en"}

    def tearDown(self):
        self.db.close()
        os.environ.pop("TCGDEX_STANDBY_API_BASE", None)
        self._reload()

    def _reload(self):
        import services.pokemon_api as pokemon_api
        importlib.reload(pokemon_api)
        import api.recognize as recognize
        importlib.reload(recognize)
        return recognize

    def _with_standby(self, standby="http://standby.local/v2"):
        os.environ["TCGDEX_STANDBY_API_BASE"] = standby
        return self._reload()

    async def test_the_standby_is_not_consulted_when_the_primary_answers(self):
        # The case that must not change: a working catalogue is the only one
        # asked, so matching stays exactly as it is today.
        recognize = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": (200, [_card("sv03.5-027", "Sandshrew", "027")]),
            "standby.local": (200, [_card("other-1", "Sandshrew", "027")]),
        })
        with patch("api.recognize.httpx.AsyncClient", catalogues):
            candidates, _ = await recognize._search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertTrue(all("standby.local" not in url for url in catalogues.calls), catalogues.calls)
        self.assertEqual([c["tcg_card_id"] for c in candidates], ["sv03.5-027"])

    async def test_the_standby_is_not_consulted_when_the_primary_says_no(self):
        # An empty answer is an answer. Asking a second catalogue only because
        # the first said no is not a fallback, it is a coin toss.
        recognize = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": (200, []),
            "standby.local": (200, [_card("sv03.5-027", "Sandshrew", "027")]),
        })
        with patch("api.recognize.httpx.AsyncClient", catalogues):
            candidates, _ = await recognize._search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertTrue(all("standby.local" not in url for url in catalogues.calls), catalogues.calls)
        self.assertEqual(candidates, [])

    async def test_the_standby_answers_when_the_primary_is_unreachable(self):
        # The whole point: the outage no longer stops the scan.
        recognize = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": httpx.ConnectTimeout("timed out"),
            "standby.local": (200, [_card("sv03.5-027", "Sandshrew", "027")]),
        })
        with patch("api.recognize.httpx.AsyncClient", catalogues):
            candidates, _ = await recognize._search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertTrue(any("standby.local" in url for url in catalogues.calls), catalogues.calls)
        self.assertEqual([c["tcg_card_id"] for c in candidates], ["sv03.5-027"])

    async def test_details_are_fetched_from_the_catalogue_that_answered(self):
        # Enrichment must follow the candidates. Asking the primary about a
        # card the standby found means talking to a host already known to be
        # down: eight one-by-one timeouts, and artist, HP, regulation mark and
        # printed total left empty, all of which feed ranking and confidence.
        recognize = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": httpx.ConnectTimeout("timed out"),
            "standby.local": (200, [_card("sv03.5-027", "Sandshrew", "027")]),
        })
        # The enrichment only runs for fields the scan actually read off the
        # card, so the fixture has to have read one.
        card_info = dict(self.card_info, artist="kodama")
        with patch("api.recognize.httpx.AsyncClient", catalogues):
            candidates, _ = await recognize._search_and_rank_candidates(self.db, card_info, trace=None)
            await recognize._fill_candidate_details(self.db, candidates, card_info)

        detail_calls = [url for url in catalogues.calls if "/cards/" in url]
        self.assertTrue(detail_calls, "the detail lookup should have run")
        self.assertTrue(
            all("standby.local" in url for url in detail_calls),
            f"details must come from the catalogue that answered: {detail_calls}",
        )

    async def test_a_candidate_records_which_catalogue_found_it(self):
        recognize = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": httpx.ConnectTimeout("timed out"),
            "standby.local": (200, [_card("sv03.5-027", "Sandshrew", "027")]),
        })
        with patch("api.recognize.httpx.AsyncClient", catalogues):
            candidates, _ = await recognize._search_and_rank_candidates(self.db, self.card_info, trace=None)
        self.assertEqual([c["_catalogue"] for c in candidates], [recognize.CATALOGUE_STANDBY])

    async def test_both_unreachable_still_reports_the_outage(self):
        recognize = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": httpx.ConnectTimeout("timed out"),
            "standby.local": httpx.ConnectTimeout("timed out"),
        })
        with patch("api.recognize.httpx.AsyncClient", catalogues):
            with self.assertRaises(HTTPException) as raised:
                await recognize._search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_nothing_changes_when_no_standby_is_configured(self):
        os.environ.pop("TCGDEX_STANDBY_API_BASE", None)
        recognize = self._reload()
        catalogues = _Catalogues({"api.tcgdex.net": httpx.ConnectTimeout("timed out")})
        with patch("api.recognize.httpx.AsyncClient", catalogues):
            with self.assertRaises(HTTPException) as raised:
                await recognize._search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertTrue(all("api.tcgdex.net" in url for url in catalogues.calls), catalogues.calls)


if __name__ == "__main__":
    unittest.main()
