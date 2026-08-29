import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import httpx  # noqa: F401
    from fastapi import HTTPException

    from api.recognize import _search_and_rank_candidates

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


def _response(status_code, payload):
    response = Mock()
    response.status_code = status_code
    response.json = Mock(return_value=payload)
    return response


def _client_returning(response=None, error=None):
    """Stand in for the httpx.AsyncClient context manager the search uses."""
    client = AsyncMock()
    if error is not None:
        client.get = AsyncMock(side_effect=error)
    else:
        client.get = AsyncMock(return_value=response)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return Mock(return_value=context)


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class CatalogueOutageTests(unittest.IsolatedAsyncioTestCase):
    """An unreachable catalogue must not be reported as a card that does not exist."""

    def setUp(self):
        self.card_info = {"name": "Sandshrew", "name_en": "Sandshrew", "number_local": "027", "language": "en"}
        self.db = Mock()

    async def test_a_network_failure_is_reported_rather_than_returning_no_matches(self):
        # The user-visible problem this fixes: while the catalogue was down,
        # every scan completed with zero matches and no error, which reads as
        # "this card is not in the database" and sends people looking at their
        # own installation.
        with patch("api.recognize.httpx.AsyncClient", _client_returning(error=httpx.ConnectTimeout("timed out"))):
            with self.assertRaises(HTTPException) as raised:
                await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(raised.exception.status_code, 503)
        self.assertIn("catalogue", str(raised.exception.detail).lower())

    async def test_a_catalogue_server_error_is_treated_the_same_way(self):
        with patch("api.recognize.httpx.AsyncClient", _client_returning(_response(503, []))):
            with self.assertRaises(HTTPException) as raised:
                await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_a_body_that_cannot_be_read_is_an_outage_not_an_answer(self):
        # A gateway serving an error page with a 200 is still the catalogue
        # being unavailable. This also pins the attempt accounting: the failure
        # happens after the response arrives, so counting the lookup twice
        # would leave attempts and failures unequal and silently return no
        # matches again.
        response = _response(200, [])
        response.json = Mock(side_effect=ValueError("not json"))
        with patch("api.recognize.httpx.AsyncClient", _client_returning(response)):
            with self.assertRaises(HTTPException) as raised:
                await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_a_genuine_empty_result_still_reports_no_matches(self):
        # The bystander. The catalogue answered; it simply has no such card.
        # Raising here would turn every unknown card into a retry loop.
        with patch("api.recognize.httpx.AsyncClient", _client_returning(_response(200, []))):
            candidates, _ = await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(candidates, [])

    async def test_a_rate_limited_lookup_is_an_outage_not_an_answer(self):
        # A 429 is the catalogue declining to answer this request, not a
        # statement that the card does not exist. Treating it as an answer
        # reproduces the misleading "no matches" under rate limiting, which is
        # the outcome this change exists to remove.
        with patch("api.recognize.httpx.AsyncClient", _client_returning(_response(429, []))):
            with self.assertRaises(HTTPException) as raised:
                await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(raised.exception.status_code, 503)

    async def test_a_client_error_is_an_answer_not_an_outage(self):
        # A 4xx is the catalogue rejecting the request. Retrying it forever
        # would not help, so it must not be classed as unreachable.
        with patch("api.recognize.httpx.AsyncClient", _client_returning(_response(400, []))):
            candidates, _ = await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(candidates, [])


if __name__ == "__main__":
    unittest.main()
