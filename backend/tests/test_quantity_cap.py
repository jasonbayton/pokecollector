"""The per-row copy limit, and the startup migration that used to undo it.

The cap lived as a literal in check constraints, request validation, merge
arithmetic, CSV import and translated copy. The dangerous copy was in
run_migrations(), which executes on every boot: while it said 99, raising the
limit anywhere else would work until the next restart quietly clamped real
data back down.
"""
import os
import unittest

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from database import Base, _run_migrations
    from models import Binder, BinderCard, Card, User, WishlistItem
    from services.quantity_limits import MAX_CARD_QUANTITY
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class QuantityCapTests(unittest.TestCase):
    def test_the_cap_is_well_above_the_old_limit(self):
        self.assertGreater(MAX_CARD_QUANTITY, 99)

    def test_no_stray_99_literals_remain_in_the_quantity_paths(self):
        """The limit drifted once because it was written out in many places."""
        import pathlib
        root = pathlib.Path(__file__).resolve().parent.parent
        offenders = []
        for name in ("api/binders.py", "api/wishlist.py", "schemas.py", "models.py"):
            text_body = (root / name).read_text()
            for number, line in enumerate(text_body.splitlines(), 1):
                stripped = line.strip()
                if stripped.startswith("#"):
                    continue
                if "99" in stripped.replace("999", "").replace("MAX_CARD_QUANTITY", ""):
                    offenders.append(f"{name}:{number}: {stripped[:70]}")
        self.assertEqual(offenders, [], "a hard-coded 99 crept back in")


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class QuantityCapMigrationTests(unittest.TestCase):
    """Runs against real PostgreSQL, where the check constraints actually exist."""

    def setUp(self):
        database = POSTGRES_TEST_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in database.lower():
            raise unittest.SkipTest(f"refusing to touch database {database!r}")
        self.engine = create_engine(POSTGRES_TEST_URL)
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        self.db = sessionmaker(bind=self.engine)()

        self.user = User(username="jason", hashed_password="x", role="admin", is_active=True)
        self.db.add(self.user)
        self.db.commit()
        self.db.refresh(self.user)

        self.card = Card(id="sv1-1_en", tcg_card_id="sv1-1", name="Sprigatito",
                         set_id="sv1", number="1", lang="en")
        self.binder = Binder(name="Bulk", user_id=self.user.id, binder_type="collection")
        self.db.add_all([self.card, self.binder])
        self.db.commit()
        self.db.refresh(self.binder)

    def tearDown(self):
        self.db.close()
        self.engine.dispose()

    def _migrate(self):
        """Run the startup migrations with no session holding a lock.

        _run_migrations issues ALTER TABLE against binders, wishlist and
        binder_cards, which needs ACCESS EXCLUSIVE. An open ORM session sitting
        idle in transaction blocks it indefinitely, so the session has to be
        closed first and reopened afterwards.
        """
        self.db.close()
        with self.engine.connect() as conn:
            _run_migrations(conn)
        self.db = sessionmaker(bind=self.engine)()

    def test_a_quantity_above_the_old_limit_survives_a_restart(self):
        """The regression that would only have appeared on the next deploy."""
        self.db.add(BinderCard(binder_id=self.binder.id, card_id=self.card.id,
                               required_quantity=500))
        self.db.add(WishlistItem(card_id=self.card.id, user_id=self.user.id, quantity=500))
        self.db.commit()

        self._migrate()

        self.assertEqual(self.db.query(BinderCard).one().required_quantity, 500)
        self.assertEqual(self.db.query(WishlistItem).one().quantity, 500)

    def test_the_check_constraints_actually_allow_the_new_ceiling(self):
        self._migrate()
        with self.engine.connect() as conn:
            bounds = conn.execute(text(
                "SELECT conname, pg_get_constraintdef(oid) FROM pg_constraint "
                "WHERE conname IN ('ck_wishlist_quantity_range', "
                "'ck_binder_card_quantity_range')"
            )).fetchall()
        self.assertEqual(len(bounds), 2, "both check constraints should exist")
        for name, definition in bounds:
            self.assertIn(str(MAX_CARD_QUANTITY), definition,
                          f"{name} still carries the old bound: {definition}")


if __name__ == "__main__":
    unittest.main()
