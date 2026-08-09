import os
import re
import unittest

try:
    from api.social import _contributing_user_ids
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False

skip_without_deps = unittest.skipUnless(
    DEPS_AVAILABLE, "FastAPI/SQLAlchemy are not installed in this lightweight test environment"
)

_ROOT = os.path.dirname(os.path.dirname(os.path.dirname(os.path.abspath(__file__))))
PAGE = os.path.join(_ROOT, "frontend", "src", "pages", "ServerCollection.jsx")
CARD_ITEM = os.path.join(_ROOT, "frontend", "src", "components", "CardItem.jsx")


class SharedViewCannotEditOtherPeoplesCardsTests(unittest.TestCase):
    """The shared view shows other people's cards, so it must not be able to
    change them.

    It reuses the existing CardModal rather than carrying a parallel one. Two
    things keep that safe, and both are easy to undo by accident:

    * `onEdit` is the only route to CustomCardModal, which holds the unscoped
      deleteCustomCard call. Not passing it closes that path.
    * `custom_image_url` is a column on the Card row, so the modal's custom
      image editor rewrites the image for every user. Only `readOnly` gates it.
    """

    def setUp(self):
        for path in (PAGE, CARD_ITEM):
            if not os.path.exists(path):
                self.skipTest(f"{os.path.basename(path)} not present")
        with open(PAGE, encoding="utf-8") as handle:
            self.source = handle.read()
        with open(CARD_ITEM, encoding="utf-8") as handle:
            self.card_item = handle.read()

    def test_imports_nothing_that_can_mutate(self):
        forbidden = [
            "addToCollection", "addToWishlist", "removeFromCollection",
            "updateCollectionItem", "createCustomCard", "updateCustomCard",
            "deleteCustomCard", "updateCardCustomImage",
        ]
        found = [name for name in forbidden if name in self.source]
        self.assertEqual(found, [], f"read-only page can reach mutating calls: {found}")

    def test_reuses_the_shared_modal_rather_than_a_parallel_one(self):
        self.assertIn("import { CardModal } from '../components/CardItem'", self.source)

    def test_does_not_import_the_editable_tile(self):
        # The default export is a tile that renders add/wishlist actions inline.
        self.assertNotIn("import CardItem", self.source)
        self.assertNotIn("<CardItem", self.source)

    def test_never_passes_onEdit(self):
        # onEdit is the only way to reach CustomCardModal and its unscoped delete.
        self.assertNotIn("onEdit", self.source)

    def test_opens_the_modal_read_only(self):
        modal = self.source[self.source.index("<CardModal"):]
        modal = modal[:modal.index("/>")]
        self.assertIn("readOnly", modal, "shared view must open CardModal read-only")

    def test_readOnly_actually_gates_the_shared_image_editor(self):
        # A readOnly prop the component ignores would be worse than none at all.
        line = next(
            l for l in self.card_item.splitlines() if "const canEditCustomImage" in l
        )
        self.assertIn("!readOnly", line, f"custom image editor not gated: {line.strip()}")

    def test_only_client_import_is_the_read_endpoint(self):
        imports = re.findall(r"from '\.\./api/client'", self.source)
        self.assertEqual(len(imports), 1, "expected exactly one api/client import")
        line = next(l for l in self.source.splitlines() if "../api/client" in l)
        self.assertIn("getServerCollection", line)
        self.assertNotIn(",", line.split("{")[1].split("}")[0],
                         "only the read endpoint may be imported")


class _Row:
    def __init__(self, value):
        self.value = value


class _Query:
    def __init__(self, rows):
        self._rows = rows

    def filter(self, *_a, **_k):
        return self

    def all(self):
        return self._rows


class _Db:
    """Returns opted-in ids first, then the active subset."""

    def __init__(self, opted_in, active):
        self._responses = [[(i,) for i in opted_in], [(i,) for i in active]]

    def query(self, *_a, **_k):
        return _Query(self._responses.pop(0) if self._responses else [])


@skip_without_deps
class ContributorBoundaryTests(unittest.TestCase):
    """Opting in is the whole boundary: anyone who has not opted in must never
    reach the aggregation at all."""

    def test_only_opted_in_and_active_users_contribute(self):
        db = _Db(opted_in=[1, 2, 3], active=[1, 3])
        self.assertEqual(_contributing_user_ids(db), [1, 3])

    def test_nobody_opted_in_yields_nobody(self):
        db = _Db(opted_in=[], active=[])
        self.assertEqual(_contributing_user_ids(db), [])

    def test_inactive_contributors_are_dropped(self):
        db = _Db(opted_in=[7], active=[])
        self.assertEqual(_contributing_user_ids(db), [])


@skip_without_deps
class SharingDefaultTests(unittest.TestCase):
    def test_sharing_is_off_by_default(self):
        from api.settings import DEFAULT_SETTINGS, PER_USER_KEYS

        self.assertEqual(DEFAULT_SETTINGS.get("share_collection"), "false")
        self.assertIn("share_collection", PER_USER_KEYS)

    def test_the_setting_key_is_a_separate_entry(self):
        # A missing comma in the set literal silently concatenates two keys into
        # one, which type-checks and parses but breaks both.
        from api.settings import PER_USER_KEYS

        joined = [k for k in PER_USER_KEYS if "share_collection" in k and k != "share_collection"]
        self.assertEqual(joined, [], f"concatenated key present: {joined}")

    def test_sharing_is_coerced_to_a_boolean_string(self):
        from api.settings import _coerce_setting_value

        self.assertEqual(_coerce_setting_value("share_collection", True), "true")
        self.assertEqual(_coerce_setting_value("share_collection", "yes"), "true")
        self.assertEqual(_coerce_setting_value("share_collection", "nonsense"), "false")


if __name__ == "__main__":
    unittest.main()
