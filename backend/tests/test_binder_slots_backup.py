"""A partial collection backup has to carry the binder layout.

BACKUP_GROUPS decides which tables a partial dump contains. Omitting
binder_slots returns every binder and entry on restore with the record of where
each card physically sits silently gone, which is data loss rather than an
inconvenience: nothing errors, the binder simply forgets its own arrangement.
"""
import unittest

try:
    from api.backup import BACKUP_GROUPS
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "backup module is not importable")
class BinderSlotBackupTests(unittest.TestCase):
    def test_the_collection_group_carries_the_layout(self):
        self.assertIn("binder_slots", BACKUP_GROUPS["collection"])

    def test_the_layout_travels_with_the_binders_it_belongs_to(self):
        """A layout in a different group could restore without its parents."""
        collection = BACKUP_GROUPS["collection"]
        for table in ("binders", "binder_cards", "binder_slots"):
            self.assertIn(table, collection)

    def test_no_other_group_claims_it(self):
        others = [name for name, tables in BACKUP_GROUPS.items()
                  if name != "collection" and "binder_slots" in tables]
        self.assertEqual(others, [], "binder_slots must belong to exactly one group")


if __name__ == "__main__":
    unittest.main()
