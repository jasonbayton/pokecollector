import unittest

try:
    from schemas import CardCustomCreate, CustomCardUpdate
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False

skip_without_deps = unittest.skipUnless(
    DEPS_AVAILABLE, "pydantic is not installed in this lightweight test environment"
)


@skip_without_deps
class CustomCardVariantSchemaTests(unittest.TestCase):
    """A manually created card had no variant fields at all, so it could not be
    recorded as holo or reverse holo - the distinction that decides what a card
    is worth. Cards are entered by hand precisely when the catalogue has nothing,
    so there is no later source to recover the printing from."""

    def test_create_accepts_every_variant(self):
        card = CardCustomCreate(
            name="Binacle",
            variants_normal=True,
            variants_reverse=True,
            variants_holo=False,
            variants_first_edition=False,
        )
        self.assertTrue(card.variants_normal)
        self.assertTrue(card.variants_reverse)
        self.assertFalse(card.variants_holo)

    def test_create_leaves_variants_unset_when_not_given(self):
        # None, not False: the endpoint distinguishes "not stated" from "absent"
        # so it can default a card with no variant details to a normal printing.
        card = CardCustomCreate(name="Binacle")
        self.assertIsNone(card.variants_normal)
        self.assertIsNone(card.variants_reverse)
        self.assertIsNone(card.variants_holo)

    def test_reverse_holo_is_expressible_on_its_own(self):
        card = CardCustomCreate(name="Binacle", variants_normal=False, variants_reverse=True)
        self.assertTrue(card.variants_reverse)
        self.assertFalse(card.variants_normal)

    def test_update_accepts_variants(self):
        update = CustomCardUpdate(variants_reverse=True)
        self.assertTrue(update.variants_reverse)

    def test_update_only_reports_fields_actually_set(self):
        # The endpoint applies model_dump(exclude_unset=True), so an untouched
        # variant must not be written back as None and wipe an existing value.
        update = CustomCardUpdate(variants_holo=True)
        touched = update.model_dump(exclude_unset=True)
        self.assertEqual(set(touched), {"variants_holo"})


if __name__ == "__main__":
    unittest.main()
