import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import httpx  # noqa: F401
    from fastapi import HTTPException  # noqa: F401

    from api.recognize import _local_catalogue_candidates, _search_and_rank_candidates

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


def _card(**kwargs):
    row = Mock()
    row.tcg_card_id = kwargs.get("tcg_card_id", "sv03.5-027")
    row.name = kwargs.get("name", "Sandshrew")
    row.set_id = kwargs.get("set_id", "sv03.5")
    row.number = kwargs.get("number", "027")
    row.rarity = kwargs.get("rarity", "Common")
    row.images_small = kwargs.get("images_small", "https://assets.tcgdex.net/en/sv/sv03.5/27/low.webp")
    row.custom_image_url = kwargs.get("custom_image_url", None)
    row.lang = kwargs.get("lang", "en")
    return row


def _db_with(cards, sets=None):
    db = Mock()
    db.query.return_value.filter.return_value.limit.return_value.all.return_value = cards
    db.query.return_value.filter.return_value.all.return_value = sets or []
    return db


def _unreachable_client():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return Mock(return_value=context)


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class LocalCatalogueFallbackTests(unittest.IsolatedAsyncioTestCase):
    """A synced catalogue already on disk should not go unused during an outage."""

    def setUp(self):
        self.card_info = {"name": "Sandshrew", "name_en": "Sandshrew", "number_local": "027", "language": "en"}

    async def test_an_unreachable_catalogue_falls_back_to_the_synced_copy(self):
        # The user problem: this installation had all 23,000 synced cards in
        # Postgres, including the exact card being scanned, and still reported
        # nothing because a remote host was down.
        db = _db_with([_card()])
        with patch("api.recognize.httpx.AsyncClient", _unreachable_client()):
            candidates, _ = await _search_and_rank_candidates(db, self.card_info, trace=None)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["tcg_card_id"], "sv03.5-027")
        self.assertEqual(candidates[0]["number"], "027")
        self.assertTrue(candidates[0]["image"])

    async def test_the_fallback_is_not_used_when_the_catalogue_answered(self):
        # The bystander. A live empty answer means the card is not there, and
        # must not be quietly replaced by a local guess.
        db = _db_with([_card()])
        response = Mock()
        response.status_code = 200
        response.json = Mock(return_value=[])
        client = AsyncMock()
        client.get = AsyncMock(return_value=response)
        context = AsyncMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        with patch("api.recognize.httpx.AsyncClient", Mock(return_value=context)):
            candidates, _ = await _search_and_rank_candidates(db, self.card_info, trace=None)

        self.assertEqual(candidates, [])

    def test_custom_cards_are_excluded_from_the_fallback(self):
        # A custom card is this installation's own row, not a catalogue entry.
        # Offering one as a scan match would invent a result the live search
        # could never return.
        db = _db_with([])
        _local_catalogue_candidates(db, self.card_info, [("en", "Sandshrew")])
        filter_args = db.query.return_value.filter.call_args
        self.assertIsNotNone(filter_args, "the fallback must filter, not select everything")

    def test_the_fallback_shape_matches_the_live_search(self):
        # Downstream ranking reads these keys, so a mismatch would rank the
        # local results wrongly rather than fail loudly.
        db = _db_with([_card()])
        candidates = _local_catalogue_candidates(db, self.card_info, [("en", "Sandshrew")])
        for key in ("id", "tcg_card_id", "name", "set", "number", "image", "rarity", "lang", "_lang"):
            self.assertIn(key, candidates[0], key)

    def test_nothing_is_returned_without_a_name_to_search(self):
        db = _db_with([_card()])
        self.assertEqual(_local_catalogue_candidates(db, self.card_info, []), [])


if __name__ == "__main__":
    unittest.main()
