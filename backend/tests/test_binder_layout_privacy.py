"""Where a person's cards physically live is not public.

A public binder tells visitors which cards it holds. It must not also tell them
the shelf arrangement: which page and pocket each card sits in, or how the album
is laid out. That is information about someone's home, not their collection.

These pin the boundary rather than relying on it having been drawn correctly
once. Public serialisers build their own shapes, so a field added to the private
binder response cannot leak into them by accident - but a future serialiser
could be written against the ORM object instead, and this would catch it.
"""
import inspect
import unittest

try:
    from services import public_profile
    from api import public as public_api
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


LAYOUT_FIELDS = ("binder_slot", "grid_rows", "grid_columns", "pocket", "placed_slot_count")


@unittest.skipUnless(DEPS_AVAILABLE, "public modules are not importable")
class BinderLayoutPrivacyTests(unittest.TestCase):
    def test_the_public_profile_serialiser_exposes_no_layout(self):
        source = inspect.getsource(public_profile)
        found = [field for field in LAYOUT_FIELDS if field in source]
        self.assertEqual(found, [], f"public profile would expose {found}")

    def test_the_public_api_exposes_no_layout(self):
        source = inspect.getsource(public_api)
        found = [field for field in LAYOUT_FIELDS if field in source]
        self.assertEqual(found, [], f"public API would expose {found}")

    def test_the_private_binder_response_is_not_reused_publicly(self):
        """The response carrying the grid belongs to the owner's routes only."""
        for module in (public_profile, public_api):
            source = inspect.getsource(module)
            self.assertNotIn("BinderResponse", source)
            self.assertNotIn("_binder_response", source)


if __name__ == "__main__":
    unittest.main()
