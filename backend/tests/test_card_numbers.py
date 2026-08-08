import unittest

from services.card_numbers import (
    candidate_card_ids,
    card_number_matches,
    card_number_variants,
    normalize_card_number,
    number_matches_candidate,
    printed_number_variants,
)


class CardNumberTests(unittest.TestCase):
    """Original cover for the pre-existing helpers. Do not remove: these guard
    behaviour other modules rely on, independently of the matcher below."""

    def test_numeric_numbers_match_with_or_without_leading_zeroes(self):
        self.assertTrue(card_number_matches("044", "44"))
        self.assertTrue(card_number_matches("44", "044"))
        self.assertTrue(card_number_matches("000", "0"))

    def test_different_numeric_numbers_do_not_match(self):
        self.assertFalse(card_number_matches("045", "44"))

    def test_non_numeric_numbers_still_match_case_insensitively(self):
        self.assertTrue(card_number_matches("TG01", "tg01"))
        self.assertFalse(card_number_matches("TG01", "1"))

    def test_normalization_preserves_empty_value(self):
        self.assertEqual(normalize_card_number(None), "")
        self.assertEqual(normalize_card_number(""), "")


class PrintedNumberVariantTests(unittest.TestCase):
    def test_drops_the_set_total(self):
        self.assertEqual(printed_number_variants("012/094")[0], "012")

    def test_numeric_values_offer_an_unpadded_form(self):
        self.assertEqual(printed_number_variants("012/094"), ["012", "12"])

    def test_no_duplicate_when_padding_is_irrelevant(self):
        self.assertEqual(printed_number_variants("4/102"), ["4"])

    def test_a_numeric_suffix_is_never_stripped(self):
        # "74a" and "74" are different, real cards.
        self.assertEqual(printed_number_variants("74a/102"), ["74a"])

    def test_alphanumeric_prefixes_survive(self):
        self.assertEqual(printed_number_variants("TG01/TG30"), ["TG01"])
        self.assertEqual(printed_number_variants("H04"), ["H04"])

    def test_unparsable_values_yield_nothing(self):
        for value in (None, "", "   ", "/", " / ", "12/", "12/102/garbage"):
            with self.subTest(value=value):
                self.assertEqual(printed_number_variants(value), [])


class CardNumberVariantTests(unittest.TestCase):
    def test_hand_typed_number_offers_the_padded_form(self):
        self.assertIn("012", card_number_variants("12"))

    def test_padded_number_offers_the_bare_form(self):
        self.assertIn("4", card_number_variants("004"))

    def test_printed_number_with_total_is_handled(self):
        variants = card_number_variants("001/093")
        self.assertIn("001", variants)
        self.assertIn("1", variants)

    def test_suffixed_numbers_are_offered_only_as_written(self):
        self.assertEqual(card_number_variants("74a"), ["74a"])

    def test_alphanumeric_numbers_are_offered_only_as_written(self):
        self.assertEqual(card_number_variants("TG01"), ["TG01"])

    def test_most_literal_form_comes_first(self):
        self.assertEqual(card_number_variants("012/094")[0], "012")

    def test_no_duplicates(self):
        variants = card_number_variants("012/094")
        self.assertEqual(len(variants), len(set(variants)))


class NumberMatchesCandidateTests(unittest.TestCase):
    """Confirming a suggested match, rather than trusting that an id resolves."""

    def test_padded_and_unpadded_forms_are_accepted(self):
        self.assertTrue(number_matches_candidate("12", "012"))
        self.assertTrue(number_matches_candidate("012/094", "12"))

    def test_a_suffixed_number_is_not_satisfied_by_its_base(self):
        # The wrong-card scenario: 74a must never accept card 74.
        self.assertFalse(number_matches_candidate("74a", "74"))

    def test_a_suffixed_number_accepts_itself(self):
        self.assertTrue(number_matches_candidate("74a/102", "74a"))

    def test_case_is_ignored_for_alphanumerics(self):
        self.assertTrue(number_matches_candidate("TG01", "tg01"))

    def test_a_different_number_is_rejected(self):
        self.assertFalse(number_matches_candidate("12", "13"))

    def test_missing_local_id_is_rejected(self):
        for value in (None, "", "  "):
            with self.subTest(value=value):
                self.assertFalse(number_matches_candidate("12", value))


class CandidateCardIdTests(unittest.TestCase):
    def test_matches_a_card_entered_without_padding(self):
        self.assertIn("me02-012", candidate_card_ids("me02", "12"))

    def test_matches_a_card_whose_number_kept_the_set_total(self):
        self.assertIn("B2a-001", candidate_card_ids("B2a", "001/093"))

    def test_alphanumeric_numbers_still_produce_a_candidate(self):
        # The old matcher built the id verbatim, so these used to work; a
        # numeric-only parser would regress them to no candidates at all.
        self.assertEqual(candidate_card_ids("swsh45", "TG01"), ["swsh45-TG01"])
        self.assertEqual(candidate_card_ids("cel25", "H04"), ["cel25-H04"])

    def test_never_builds_an_id_containing_a_slash(self):
        for card_id in candidate_card_ids("B2a", "001/093"):
            self.assertNotIn("/", card_id)

    def test_missing_set_or_number_means_no_candidates(self):
        self.assertEqual(candidate_card_ids(None, "012"), [])
        self.assertEqual(candidate_card_ids("", "012"), [])
        self.assertEqual(candidate_card_ids("me02", None), [])

    def test_every_candidate_is_prefixed_with_the_set(self):
        for card_id in candidate_card_ids("me02", "012/094"):
            self.assertTrue(card_id.startswith("me02-"))


if __name__ == "__main__":
    unittest.main()
