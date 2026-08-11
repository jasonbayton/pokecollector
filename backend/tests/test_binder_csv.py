import unittest

from services.binder_csv import BINDER_CSV_DUPLICATE_QUANTITY_ERROR, combine_binder_required_quantity
from services.quantity_limits import MAX_CARD_QUANTITY


class BinderCsvTests(unittest.TestCase):
    def test_duplicate_required_quantities_are_summed(self):
        self.assertEqual(combine_binder_required_quantity(2, 3), 5)

    def test_import_quantity_is_added_to_existing_quantity(self):
        current_required_quantity = 3
        imported_csv_quantity = 4
        self.assertEqual(combine_binder_required_quantity(current_required_quantity, imported_csv_quantity), 7)

    def test_combined_required_quantity_may_reach_the_cap(self):
        self.assertEqual(
            combine_binder_required_quantity(MAX_CARD_QUANTITY - 1, 1), MAX_CARD_QUANTITY
        )

    def test_combined_required_quantity_rejects_values_over_the_cap(self):
        # Expressed against the shared constant: this file and its test both
        # carried the old literal, so they agreed with each other and the suite
        # stayed green while CSV import rejected quantities the rest of the app
        # had already started accepting.
        with self.assertRaises(ValueError) as context:
            combine_binder_required_quantity(MAX_CARD_QUANTITY - 1, 2)
        self.assertEqual(str(context.exception), BINDER_CSV_DUPLICATE_QUANTITY_ERROR)


if __name__ == "__main__":
    unittest.main()
