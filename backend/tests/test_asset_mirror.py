"""Card artwork can be served from a mirror instead of the catalogue's CDN.

The CDN is a single host, and when it is unreachable the cards in this
installation lose their pictures even though everything else about them is
known locally. TCGDEX_ASSETS_BASE points image fetches at a mirror, typically
a caching proxy on the same LAN.

The design rule these tests hold in place is that turning the mirror on can
never make things worse than leaving it off: the mirror is tried first and the
CDN it mirrors is still tried second, the cache is not invalidated, and images
that were never the catalogue's are not touched at all.
"""

import importlib
import os
import unittest
from unittest.mock import Mock, patch

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import ImageCache  # noqa: F401  (registers the table)

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False

CANONICAL = "https://assets.tcgdex.net/en/sv/sv03.5/027/low.webp"


def _reload(base=None, mode=None):
    if base is None:
        os.environ.pop("TCGDEX_ASSETS_BASE", None)
    else:
        os.environ["TCGDEX_ASSETS_BASE"] = base
    if mode is None:
        os.environ.pop("TCGDEX_ASSETS_MODE", None)
    else:
        os.environ["TCGDEX_ASSETS_MODE"] = mode
    import services.tcgdex_assets as assets
    return importlib.reload(assets)


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed in this lightweight test environment")
class AssetMirrorUrlTests(unittest.TestCase):
    def tearDown(self):
        _reload(None)

    def test_without_a_mirror_nothing_changes(self):
        assets = _reload(None)
        self.assertIsNone(assets.mirror_asset_url(CANONICAL))
        self.assertEqual(assets.asset_urls_to_try(CANONICAL), [CANONICAL])
        self.assertEqual(assets.trusted_asset_hosts(), {"assets.tcgdex.net"})

    def test_a_mirror_is_tried_first_and_the_cdn_second(self):
        # The order is the whole safety argument: a mirror that is off, wrong
        # or missing an image costs one failed request and then behaves
        # exactly as it did before it was configured.
        assets = _reload("http://192.168.1.253:8080")
        self.assertEqual(
            assets.asset_urls_to_try(CANONICAL),
            ["http://192.168.1.253:8080/en/sv/sv03.5/027/low.webp", CANONICAL],
        )

    def test_a_mirror_under_a_path_keeps_the_prefix(self):
        assets = _reload("https://cache.example.org/tcgdex/")
        self.assertEqual(
            assets.mirror_asset_url(CANONICAL),
            "https://cache.example.org/tcgdex/en/sv/sv03.5/027/low.webp",
        )

    def test_a_query_string_survives_the_rewrite(self):
        assets = _reload("http://mirror.local")
        self.assertEqual(
            assets.mirror_asset_url(CANONICAL + "?v=2"),
            "http://mirror.local/en/sv/sv03.5/027/low.webp?v=2",
        )

    def test_images_that_were_never_the_catalogue_are_left_alone(self):
        # A mirror of one known host, not a general proxy. Rewriting a user's
        # own upload or a manually supplied URL would send it somewhere it
        # does not exist, and would make this a way to reach arbitrary hosts.
        assets = _reload("http://mirror.local")
        for url in (
            "https://example.com/some/card.png",
            "/uploads/custom-1.jpg",
            "https://assets.tcgdex.net.evil.example/en/x/low.webp",
            "",
            None,
        ):
            with self.subTest(url=url):
                self.assertIsNone(assets.mirror_asset_url(url))

    def test_a_malformed_mirror_setting_is_ignored(self):
        for base in ("not-a-url", "://broken", "  "):
            with self.subTest(base=base):
                assets = _reload(base)
                self.assertIsNone(assets.mirror_asset_url(CANONICAL))
                self.assertEqual(assets.asset_urls_to_try(CANONICAL), [CANONICAL])


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed in this lightweight test environment")
class MirrorModeTests(unittest.TestCase):
    """Whether the mirror is asked before or after the CDN it mirrors."""

    def tearDown(self):
        _reload(None, None)

    def test_the_mirror_leads_by_default(self):
        assets = _reload("http://mirror.local")
        self.assertEqual(assets.asset_mirror_mode(), "primary")
        self.assertEqual(
            assets.asset_urls_to_try(CANONICAL)[0],
            "http://mirror.local/en/sv/sv03.5/027/low.webp",
        )

    def test_standby_mode_puts_the_cdn_first(self):
        assets = _reload("http://mirror.local", "standby")
        self.assertEqual(
            assets.asset_urls_to_try(CANONICAL),
            [CANONICAL, "http://mirror.local/en/sv/sv03.5/027/low.webp"],
        )

    def test_both_hosts_are_tried_whichever_order_is_asked_for(self):
        # The mode is a preference, not a restriction: neither order gives up
        # after one host, which is what makes either safe to configure.
        for mode in (None, "primary", "standby"):
            with self.subTest(mode=mode):
                assets = _reload("http://mirror.local", mode)
                self.assertEqual(len(assets.asset_urls_to_try(CANONICAL)), 2)

    def test_the_usual_spellings_of_standby_are_understood(self):
        # Rejecting these would not stop anyone writing them; it would just
        # use the mirror in the order they did not ask for.
        for spelling in ("standby", "STANDBY", " fallback ", "secondary"):
            with self.subTest(spelling=spelling):
                assets = _reload("http://mirror.local", spelling)
                self.assertEqual(assets.asset_urls_to_try(CANONICAL)[0], CANONICAL)

    def test_an_unrecognised_mode_leaves_the_mirror_leading(self):
        assets = _reload("http://mirror.local", "sideways")
        self.assertEqual(assets.asset_mirror_mode(), "primary")

    def test_a_standby_mirror_is_not_used_for_reference_images(self):
        # That path picks one URL with no second attempt, so a mirror held in
        # reserve is not the one to pick, HTTPS or not.
        assets = _reload("https://cache.example.org", "standby")
        self.assertEqual(assets.secure_asset_url(CANONICAL), CANONICAL)
        self.assertEqual(assets.trusted_asset_hosts(), {"assets.tcgdex.net"})


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed in this lightweight test environment")
class ReferenceImageTrustTests(unittest.TestCase):
    """The path that downloads artwork to compare against the user's photo."""

    def tearDown(self):
        _reload(None)

    def test_an_http_mirror_is_not_used_for_reference_images(self):
        # These bytes are decoded as an image and sent to the vision model, so
        # the HTTPS requirement stands. A LAN mirror on plain HTTP still serves
        # the pictures people look at, through the app's own image endpoint.
        assets = _reload("http://192.168.1.253:8080")
        self.assertEqual(assets.secure_asset_url(CANONICAL), CANONICAL)
        self.assertEqual(assets.trusted_asset_hosts(), {"assets.tcgdex.net"})

    def test_an_https_mirror_is_used_for_reference_images(self):
        assets = _reload("https://cache.example.org")
        self.assertEqual(
            assets.secure_asset_url(CANONICAL),
            "https://cache.example.org/en/sv/sv03.5/027/low.webp",
        )
        self.assertEqual(
            assets.trusted_asset_hosts(),
            {"assets.tcgdex.net", "cache.example.org"},
        )


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed in this lightweight test environment")
class ImageFetchTests(unittest.TestCase):
    """What actually happens when the app goes and gets a picture."""

    def setUp(self):
        engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()

    def tearDown(self):
        self.db.close()
        _reload(None)
        importlib.reload(importlib.import_module("api.images"))

    def _images_module(self, base):
        _reload(base)
        import api.images as images
        return importlib.reload(images)

    def _client(self, responses):
        """A fake HTTP client answering per host."""
        calls = []

        def get(url, **kwargs):
            calls.append(url)
            for fragment, outcome in responses.items():
                if fragment in url:
                    if isinstance(outcome, Exception):
                        raise outcome
                    response = Mock()
                    response.status_code = 200
                    response.content = outcome
                    response.headers = {"content-type": "image/webp"}
                    response.raise_for_status = Mock()
                    return response
            raise AssertionError(f"unexpected host in {url}")

        client = Mock()
        client.get = get
        return client, calls

    def test_the_mirror_serves_the_image_when_it_has_it(self):
        images = self._images_module("http://mirror.local")
        client, calls = self._client({"mirror.local": b"from-the-mirror"})
        with patch.object(images, "_client", client):
            data, content_type = images._get_or_fetch(self.db, "card:x:small:h", CANONICAL)
        self.assertEqual(data, b"from-the-mirror")
        self.assertEqual(content_type, "image/webp")
        self.assertEqual(len(calls), 1, "the CDN should not have been troubled")

    def test_a_missing_mirror_falls_back_to_the_cdn(self):
        # The case that makes enabling a mirror safe. A container that is off,
        # or an image it has never seen, must not cost the user their picture.
        images = self._images_module("http://mirror.local")
        client, calls = self._client({
            "mirror.local": ConnectionError("no route to host"),
            "assets.tcgdex.net": b"from-the-cdn",
        })
        with patch.object(images, "_client", client):
            data, _ = images._get_or_fetch(self.db, "card:x:small:h", CANONICAL)
        self.assertEqual(data, b"from-the-cdn")
        self.assertEqual(len(calls), 2)

    def test_both_unavailable_still_reports_the_failure(self):
        from fastapi import HTTPException
        images = self._images_module("http://mirror.local")
        client, _ = self._client({
            "mirror.local": ConnectionError("no route"),
            "assets.tcgdex.net": ConnectionError("no route"),
        })
        with patch.object(images, "_client", client):
            with self.assertRaises(HTTPException) as raised:
                images._get_or_fetch(self.db, "card:x:small:h", CANONICAL)
        self.assertEqual(raised.exception.status_code, 502)

    def test_turning_the_mirror_on_does_not_re_fetch_cached_images(self):
        # The cache key belongs to the canonical URL, so where the bytes came
        # from is not part of what they are. Were it otherwise, configuring a
        # mirror would silently re-download every image already held.
        images = self._images_module(None)
        client, calls = self._client({"assets.tcgdex.net": b"bytes"})
        with patch.object(images, "_client", client):
            images._get_or_fetch(self.db, "card:x:small:h", CANONICAL)
        self.assertEqual(len(calls), 1)

        images = self._images_module("http://mirror.local")
        client, calls = self._client({"mirror.local": b"different-bytes"})
        with patch.object(images, "_client", client):
            data, _ = images._get_or_fetch(self.db, "card:x:small:h", CANONICAL)
        self.assertEqual(data, b"bytes", "the cached image should have been served")
        self.assertEqual(calls, [], "nothing should have been fetched at all")


if __name__ == "__main__":
    unittest.main()
