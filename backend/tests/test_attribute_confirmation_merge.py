"""Every path that moves copies into a row has to combine the flag the same way.

The flag says whether every copy in a row has had its condition and variant
stated. Four separate paths were each getting this wrong in their own way -
trade reversal recreated rows as settled, incoming trades treated an omitted
variant as chosen, recycle-bin restore propagated only one of the three states,
and custom-card promotion ignored it entirely - so the rule now lives in one
place and this pins both it and its callers.
"""

import unittest

try:
    from services.collection_attributes import merged_confirmation
    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "backend imports unavailable in this environment")
class MergedConfirmationTests(unittest.TestCase):
    def test_an_unassessed_copy_wins_over_everything(self):
        # One copy nobody assessed makes the row want checking, whatever else
        # is in it. This is the direction that must never be lost.
        for current in (True, False, None):
            with self.subTest(current=current):
                self.assertIs(merged_confirmation(current, False), False)
                self.assertIs(merged_confirmation(False, current), False)

    def test_unknown_beats_stated(self):
        # If either side's history is unknown, the result is unknown rather
        # than a claim that every copy has been stated. Restoring an unknown
        # snapshot into a settled row used to leave it claiming otherwise.
        self.assertIsNone(merged_confirmation(True, None))
        self.assertIsNone(merged_confirmation(None, True))
        self.assertIsNone(merged_confirmation(None, None))

    def test_only_two_stated_sides_stay_stated(self):
        self.assertIs(merged_confirmation(True, True), True)

    def test_the_rule_is_symmetric(self):
        states = (True, False, None)
        for a in states:
            for b in states:
                with self.subTest(a=a, b=b):
                    self.assertIs(merged_confirmation(a, b), merged_confirmation(b, a))


@unittest.skipUnless(DEPS_AVAILABLE, "backend imports unavailable in this environment")
class TradeSchemaOmissionTests(unittest.TestCase):
    def test_an_incoming_trade_omitting_either_half_is_not_stated(self):
        # The trade form sends both, but an API caller need not, and the
        # variant is the half that reaches valuation.
        from schemas import TradeIncomingItemCreate

        def stated(payload):
            return payload.condition is not None and payload.variant is not None

        self.assertFalse(stated(TradeIncomingItemCreate(card_id="x")))
        self.assertFalse(stated(TradeIncomingItemCreate(card_id="x", condition="NM")))
        self.assertFalse(stated(TradeIncomingItemCreate(card_id="x", variant="Holo")))
        self.assertTrue(stated(TradeIncomingItemCreate(card_id="x", condition="NM", variant="Holo")))


@unittest.skipUnless(DEPS_AVAILABLE, "backend imports unavailable in this environment")
class CsvPreMergeTests(unittest.TestCase):
    """Duplicate CSV rows are merged before anything reaches the database."""

    def _merge(self, first, second):
        from services.collection_csv import collection_import_key, merge_collection_import_item
        from schemas import CollectionItemCreate

        planned = {}
        for row in (first, second):
            item = CollectionItemCreate(card_id="base1-4_en", quantity=1, **row)
            key = collection_import_key(
                item.card_id, item.variant, item.lang, item.condition, item.purchase_price
            )
            merge_collection_import_item(planned, key, item)
        return list(planned.values())

    def test_a_blank_variant_row_unsettles_the_row_it_merges_with(self):
        # The key treats a blank variant as Normal, so these share a key. The
        # merged pair must not claim somebody chose the variant for both.
        merged = self._merge({"variant": "Normal"}, {})
        self.assertEqual(len(merged), 1)
        self.assertEqual(merged[0].quantity, 2)
        self.assertIsNone(merged[0].variant, "the pair is no longer fully stated")

    def test_the_result_does_not_depend_on_csv_row_order(self):
        forwards = self._merge({"variant": "Normal"}, {})
        backwards = self._merge({}, {"variant": "Normal"})
        self.assertEqual(forwards[0].variant, backwards[0].variant)
        self.assertEqual(forwards[0].quantity, backwards[0].quantity)


if __name__ == "__main__":
    unittest.main()
