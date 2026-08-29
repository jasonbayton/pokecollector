"""During a catalogue outage the synced copy on disk should be used.

A user reported every scan failing to match while the installation held a
fully synced catalogue in Postgres, because an unreachable remote host was
being reported as "no matches". These tests cover the fallback that searches
the local copy instead, and the two ways that fallback could do harm: by
answering when the catalogue actually replied, and by offering a row the live
search could never have returned.

The queries run against a real SQLite session rather than a mocked one, so a
filter that stops being applied changes the rows that come back rather than
only changing which mock methods were called.
"""

import unittest
from unittest.mock import AsyncMock, Mock, patch

try:
    import httpx  # noqa: F401
    from fastapi import HTTPException  # noqa: F401
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from api.recognize import _local_catalogue_candidates, _search_and_rank_candidates
    from database import Base
    from models import Card, Set

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


def _add_card(db, **kwargs):
    tcg_card_id = kwargs.pop("tcg_card_id", "sv03.5-027")
    lang = kwargs.pop("lang", "en")
    row = Card(
        id=kwargs.pop("id", f"{tcg_card_id or 'custom'}_{lang}"),
        tcg_card_id=tcg_card_id,
        name=kwargs.pop("name", "Sandshrew"),
        set_id=kwargs.pop("set_id", "sv03.5"),
        number=kwargs.pop("number", "027"),
        rarity=kwargs.pop("rarity", "Common"),
        images_small=kwargs.pop(
            "images_small", "https://assets.tcgdex.net/en/sv/sv03.5/27/low.webp"
        ),
        custom_image_url=kwargs.pop("custom_image_url", None),
        is_custom=kwargs.pop("is_custom", False),
        lang=lang,
        **kwargs,
    )
    db.add(row)
    db.commit()
    return row


def _unreachable_client():
    client = AsyncMock()
    client.get = AsyncMock(side_effect=httpx.ConnectTimeout("timed out"))
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return Mock(return_value=context)


def _answering_client(payload):
    response = Mock()
    response.status_code = 200
    response.json = Mock(return_value=payload)
    client = AsyncMock()
    client.get = AsyncMock(return_value=response)
    context = AsyncMock()
    context.__aenter__ = AsyncMock(return_value=client)
    context.__aexit__ = AsyncMock(return_value=False)
    return Mock(return_value=context)


@unittest.skipUnless(DEPS_AVAILABLE, "FastAPI/httpx are not installed in this lightweight test environment")
class LocalCatalogueFallbackTests(unittest.IsolatedAsyncioTestCase):
    """A synced catalogue already on disk should not go unused during an outage."""

    def setUp(self):
        engine = create_engine("sqlite:///:memory:")
        Base.metadata.create_all(engine)
        self.db = sessionmaker(bind=engine)()
        self.card_info = {
            "name": "Sandshrew",
            "name_en": "Sandshrew",
            "number_local": "027",
            "language": "en",
        }
        self.pairs = [("en", "Sandshrew")]

    def tearDown(self):
        self.db.close()

    async def test_an_unreachable_catalogue_falls_back_to_the_synced_copy(self):
        # The user problem: this installation held the exact card being
        # scanned and still reported nothing, because a remote host was down.
        _add_card(self.db)
        with patch("api.recognize.httpx.AsyncClient", _unreachable_client()):
            candidates, _ = await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(len(candidates), 1)
        self.assertEqual(candidates[0]["tcg_card_id"], "sv03.5-027")
        self.assertEqual(candidates[0]["number"], "027")
        self.assertTrue(candidates[0]["image"])

    async def test_the_fallback_is_not_used_when_the_catalogue_answered(self):
        # The bystander. A live empty answer means the card is not there, and
        # must not be quietly replaced by a local guess.
        _add_card(self.db)
        with patch("api.recognize.httpx.AsyncClient", _answering_client([])):
            candidates, _ = await _search_and_rank_candidates(self.db, self.card_info, trace=None)

        self.assertEqual(candidates, [])

    async def test_a_partial_outage_does_not_trigger_the_fallback(self):
        # One lookup answered "no such card" and another timed out. The
        # catalogue has spoken, so a local guess must not overrule it; the
        # fallback is only for knowing nothing at all.
        _add_card(self.db)
        response = Mock()
        response.status_code = 200
        response.json = Mock(return_value=[])
        client = AsyncMock()
        client.get = AsyncMock(side_effect=[response, httpx.ConnectTimeout("timed out")])
        context = AsyncMock()
        context.__aenter__ = AsyncMock(return_value=client)
        context.__aexit__ = AsyncMock(return_value=False)
        card_info = dict(self.card_info, name="Sabelette", language="fr")
        with patch("api.recognize.httpx.AsyncClient", Mock(return_value=context)):
            candidates, _ = await _search_and_rank_candidates(self.db, card_info, trace=None)

        self.assertGreaterEqual(client.get.await_count, 2, "both lookups should have been tried")
        self.assertEqual(candidates, [])

    def test_custom_cards_are_excluded_from_the_fallback(self):
        # A custom card is this installation's own row, not a catalogue entry.
        # Offering one as a scan match would invent a result the live search
        # could never return. It is stored with the same name and number as the
        # real card, so only the is_custom filter can keep it out.
        _add_card(
            self.db,
            id="custom-1_en",
            tcg_card_id="sv03.5-027",
            is_custom=True,
            custom_image_url="/uploads/custom-1.jpg",
        )
        self.assertEqual(_local_catalogue_candidates(self.db, self.card_info, self.pairs), [])

    def test_rows_predating_the_custom_flag_are_still_catalogue_rows(self):
        # is_custom is nullable, and the card-id migration in database.py
        # already treats "NULL or false" as catalogue data. A synced row left
        # NULL must not become invisible to the fallback, which would put the
        # user back where they started: their own catalogue on disk, unused.
        row = _add_card(self.db)
        # The column carries a Python-side default of False, which SQLAlchemy
        # applies to a None on insert, so the NULL has to be written directly.
        self.db.execute(text("UPDATE cards SET is_custom = NULL WHERE id = :id"), {"id": row.id})
        self.db.commit()
        stored = self.db.execute(text("SELECT is_custom FROM cards WHERE id = :id"), {"id": row.id}).scalar()
        self.assertIsNone(stored, "this test is worthless unless the row really is NULL")

        candidates = _local_catalogue_candidates(self.db, self.card_info, self.pairs)
        self.assertEqual([card["tcg_card_id"] for card in candidates], ["sv03.5-027"])

    def test_rows_without_a_catalogue_id_are_excluded(self):
        # A row with no tcg_card_id cannot be resolved by anything downstream,
        # so returning it would produce a candidate that fails to add.
        _add_card(self.db, id="orphan_en", tcg_card_id=None)
        self.assertEqual(_local_catalogue_candidates(self.db, self.card_info, self.pairs), [])

    def test_a_different_printed_number_is_not_offered(self):
        # The dangerous case. Another printing of the same name must not be
        # returned for this number: the ranker reads a lone result as a unique
        # number and marks it confident, so "add all confident" would file the
        # wrong card. Worse than no match at all.
        _add_card(self.db, id="sv03.5-104_en", tcg_card_id="sv03.5-104", number="104")
        self.assertEqual(_local_catalogue_candidates(self.db, self.card_info, self.pairs), [])

    def test_the_padded_form_of_the_printed_number_matches(self):
        # "27" on the card and "027" in the catalogue are the same printing.
        _add_card(self.db, id="sv03.5-27_en", tcg_card_id="sv03.5-27", number="27")
        candidates = _local_catalogue_candidates(self.db, self.card_info, self.pairs)
        self.assertEqual([card["number"] for card in candidates], ["27"])

    def test_a_different_language_is_not_offered(self):
        _add_card(self.db, id="sv03.5-027_fr", lang="fr")
        self.assertEqual(_local_catalogue_candidates(self.db, self.card_info, self.pairs), [])

    def test_a_name_is_only_matched_in_the_language_it_was_searched_in(self):
        # The real collision: swsh2-201 is Milo in English and Yarrow in
        # Italian, and swsh7-201 is Milo in Italian, all printed 201. Scanning
        # the Italian Yarrow searches ("it", "Yarrow") and ("en", "Milo"). If
        # the pairs are split into a list of names and a list of languages,
        # their product admits the Italian Milo, which no live search would
        # ever have returned.
        _add_card(
            self.db,
            id="swsh7-201_it",
            tcg_card_id="swsh7-201",
            name="Milo",
            set_id="swsh7",
            number="201",
            lang="it",
        )
        _add_card(
            self.db,
            id="swsh2-201_it",
            tcg_card_id="swsh2-201",
            name="Yarrow",
            set_id="swsh2",
            number="201",
            lang="it",
        )
        card_info = {"name": "Yarrow", "name_en": "Milo", "number_local": "201", "language": "it"}
        candidates = _local_catalogue_candidates(
            self.db, card_info, [("it", "Yarrow"), ("en", "Milo")]
        )
        self.assertEqual([card["tcg_card_id"] for card in candidates], ["swsh2-201"])

    def test_the_fallback_shape_matches_the_live_search(self):
        # Downstream ranking reads these keys, so a mismatch would rank the
        # local results wrongly rather than fail loudly.
        _add_card(self.db)
        self.db.add(Set(id="sv03.5_en", tcg_set_id="sv03.5", name="151", lang="en"))
        self.db.commit()
        candidates = _local_catalogue_candidates(self.db, self.card_info, self.pairs)
        for key in ("id", "tcg_card_id", "name", "set", "number", "image", "rarity", "lang", "_lang"):
            self.assertIn(key, candidates[0], key)
        self.assertEqual(candidates[0]["set"], "151")

    def test_nothing_is_returned_without_a_name_to_search(self):
        _add_card(self.db)
        self.assertEqual(_local_catalogue_candidates(self.db, self.card_info, []), [])


if __name__ == "__main__":
    unittest.main()
