"""The catalogue host must be configurable, and every caller must honour it.

A TCGdex outage made every scan fail, and the fix for that is partly to stop
depending on one upstream host. That only works if pointing the app at a
self-hosted catalogue actually redirects the scanner, which had its own copies
of the URL: the sync would have moved and the scanner, the path that broke,
would have carried on calling the public host.
"""

import importlib
import os
import unittest
from unittest.mock import patch

try:
    import httpx  # noqa: F401
    from fastapi import HTTPException  # noqa: F401

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class CatalogueBaseUrlTests(unittest.TestCase):
    def _reload(self):
        import services.pokemon_api as pokemon_api
        importlib.reload(pokemon_api)
        return pokemon_api

    def tearDown(self):
        # Leave the module as the rest of the suite expects to find it.
        with patch.dict(os.environ, {}, clear=False):
            os.environ.pop("TCGDEX_API_BASE", None)
            self._reload()

    def test_the_public_catalogue_is_the_default(self):
        os.environ.pop("TCGDEX_API_BASE", None)
        pokemon_api = self._reload()
        self.assertEqual(pokemon_api.get_base_url("en"), "https://api.tcgdex.net/v2/en")

    def test_a_self_hosted_catalogue_is_used_when_configured(self):
        with patch.dict(os.environ, {"TCGDEX_API_BASE": "http://192.168.1.253:3000/v2"}):
            pokemon_api = self._reload()
            self.assertEqual(pokemon_api.get_base_url("fr"), "http://192.168.1.253:3000/v2/fr")

    def test_a_trailing_slash_does_not_produce_a_double_slash(self):
        # The obvious way to write the setting, and a doubled slash is the kind
        # of thing that works on one server and 404s on another.
        with patch.dict(os.environ, {"TCGDEX_API_BASE": "http://catalogue.local/v2/"}):
            pokemon_api = self._reload()
            self.assertEqual(pokemon_api.get_base_url("en"), "http://catalogue.local/v2/en")

    def test_the_scanner_uses_the_same_base_as_the_sync(self):
        # The scanner is the path that broke during the outage. If it keeps its
        # own copy of the URL, pointing the app at a self-hosted catalogue
        # moves everything except the thing that needed moving.
        import api.recognize as recognize
        # By module and name rather than by identity: these tests reload
        # services.pokemon_api to exercise the setting, which replaces the
        # function object while leaving the binding correct.
        self.assertEqual(recognize.tcgdex_base_url.__module__, "services.pokemon_api")
        self.assertEqual(recognize.tcgdex_base_url.__name__, "get_base_url")
        source = open(recognize.__file__).read()
        self.assertNotIn(
            "https://api.tcgdex.net",
            source,
            "the scanner must not carry its own copy of the catalogue URL",
        )


if __name__ == "__main__":
    unittest.main()
