"""Adding a card must work wherever the scanner found it.

The scanner can match through the standby catalogue during an outage. This is
the call that then adds the card to the collection, and without the same
fallback the user is shown a card they cannot add, which is a worse place to
be stuck than not matching it at all.
"""

import importlib
import os
import unittest
from unittest.mock import Mock, patch

try:
    import httpx
    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


class _Catalogues:
    def __init__(self, responses):
        self.responses = responses
        self.calls = []

    def __call__(self, *args, **kwargs):
        return self

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get(self, url, **kwargs):
        self.calls.append(url)
        for host, outcome in self.responses.items():
            if host in url:
                if isinstance(outcome, Exception):
                    raise outcome
                response = Mock()
                response.status_code = outcome[0]
                response.json = Mock(return_value=outcome[1])
                response.raise_for_status = Mock()
                return response
        raise AssertionError(f"unexpected host in {url}")


@unittest.skipUnless(DEPS_AVAILABLE, "httpx is not installed in this lightweight test environment")
class CardFetchStandbyTests(unittest.TestCase):
    def tearDown(self):
        os.environ.pop("TCGDEX_STANDBY_API_BASE", None)
        self._reload()

    def _reload(self):
        import services.pokemon_api as pokemon_api
        return importlib.reload(pokemon_api)

    def _with_standby(self):
        os.environ["TCGDEX_STANDBY_API_BASE"] = "http://standby.local/v2"
        return self._reload()

    def test_the_standby_supplies_the_card_when_the_primary_is_down(self):
        pokemon_api = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": httpx.ConnectError("no route"),
            "standby.local": (200, {"id": "sv03.5-027", "name": "Sandshrew"}),
        })
        with patch("services.pokemon_api.httpx.Client", catalogues):
            card = pokemon_api.get_card("sv03.5-027", "en")
        self.assertEqual(card["id"], "sv03.5-027")
        self.assertTrue(any("standby.local" in url for url in catalogues.calls))

    def test_the_standby_is_not_used_when_the_primary_answers(self):
        pokemon_api = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": (200, {"id": "sv03.5-027", "name": "Sandshrew"}),
            "standby.local": (200, {"id": "wrong", "name": "Wrong"}),
        })
        with patch("services.pokemon_api.httpx.Client", catalogues):
            card = pokemon_api.get_card("sv03.5-027", "en")
        self.assertEqual(card["id"], "sv03.5-027")
        self.assertTrue(all("standby.local" not in url for url in catalogues.calls))

    def test_a_missing_card_is_an_answer_not_a_reason_to_ask_again(self):
        # A 404 means the catalogue knows the card does not exist. Asking the
        # standby would be shopping for a different answer.
        pokemon_api = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": (404, {}),
            "standby.local": (200, {"id": "sv03.5-027"}),
        })
        with patch("services.pokemon_api.httpx.Client", catalogues):
            self.assertIsNone(pokemon_api.get_card("sv03.5-027", "en"))
        self.assertTrue(all("standby.local" not in url for url in catalogues.calls))

    def test_an_unreadable_body_falls_through_like_an_outage(self):
        # The scanner already counts a body it cannot read as the catalogue
        # being unavailable, so it can match through the standby. If this call
        # treated the same response as fatal, the user would be offered a card
        # and then be unable to add it: the exact split this fallback exists to
        # close.
        pokemon_api = self._with_standby()
        broken = Mock()
        broken.status_code = 200
        broken.raise_for_status = Mock()
        broken.json = Mock(side_effect=ValueError("malformed JSON"))
        catalogues = _Catalogues({
            "api.tcgdex.net": (200, {}),
            "standby.local": (200, {"id": "sv03.5-027", "name": "Sandshrew"}),
        })
        original_get = catalogues.get

        def get(url, **kwargs):
            if "api.tcgdex.net" in url:
                catalogues.calls.append(url)
                return broken
            return original_get(url, **kwargs)

        catalogues.get = get
        with patch("services.pokemon_api.httpx.Client", catalogues):
            card = pokemon_api.get_card("sv03.5-027", "en")
        self.assertEqual(card["id"], "sv03.5-027")
        self.assertTrue(any("standby.local" in url for url in catalogues.calls))

    def test_a_body_that_is_not_a_card_falls_through_like_an_outage(self):
        # A gateway answering 200 with a list or a null decodes perfectly well
        # and is not a card. Handing it back would push the failure into the
        # caller, which treats it as one and raises AttributeError; the scanner
        # meanwhile counts the same response as unreachable and matches through
        # the standby, so the user is offered a card that cannot be added.
        for junk in ([None], [], "not a card", {"error": "nope"}):
            with self.subTest(junk=junk):
                pokemon_api = self._with_standby()
                catalogues = _Catalogues({
                    "api.tcgdex.net": (200, junk),
                    "standby.local": (200, {"id": "sv03.5-027", "name": "Sandshrew"}),
                })
                with patch("services.pokemon_api.httpx.Client", catalogues):
                    card = pokemon_api.get_card("sv03.5-027", "en")
                self.assertEqual(card["id"], "sv03.5-027")

    def test_a_failure_the_scanner_would_tolerate_reaches_the_standby(self):
        # The scanner's boundary is a bare "except Exception", so any failure
        # it survives must be one this call survives too. An invalid URL is one
        # the earlier, narrower handling here would have let through.
        pokemon_api = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": httpx.InvalidURL("malformed"),
            "standby.local": (200, {"id": "sv03.5-027", "name": "Sandshrew"}),
        })
        with patch("services.pokemon_api.httpx.Client", catalogues):
            card = pokemon_api.get_card("sv03.5-027", "en")
        self.assertEqual(card["id"], "sv03.5-027")

    def test_the_failure_still_surfaces_when_neither_can_be_reached(self):
        pokemon_api = self._with_standby()
        catalogues = _Catalogues({
            "api.tcgdex.net": httpx.ConnectError("no route"),
            "standby.local": httpx.ConnectError("no route"),
        })
        with patch("services.pokemon_api.httpx.Client", catalogues):
            with self.assertRaises(httpx.HTTPError):
                pokemon_api.get_card("sv03.5-027", "en")

    def test_without_a_standby_the_original_failure_is_raised(self):
        os.environ.pop("TCGDEX_STANDBY_API_BASE", None)
        pokemon_api = self._reload()
        catalogues = _Catalogues({"api.tcgdex.net": httpx.ConnectError("no route")})
        with patch("services.pokemon_api.httpx.Client", catalogues):
            with self.assertRaises(httpx.HTTPError):
                pokemon_api.get_card("sv03.5-027", "en")
        self.assertEqual(len(catalogues.calls), 1)


if __name__ == "__main__":
    unittest.main()
