"""Real PostgreSQL row-lock coverage for rapid set entry."""

import os
import threading
import unittest
import uuid
from types import SimpleNamespace
from unittest.mock import patch

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker

    from database import Base
    from models import Card, CollectionItem, Set, User
    from services import rapid_set_entry

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(
    DEPS_AVAILABLE and POSTGRES_TEST_URL.startswith("postgresql"),
    "TEST_POSTGRES_URL is required for PostgreSQL rapid-entry locking coverage",
)
class RapidSetEntryPostgresTests(unittest.TestCase):
    def setUp(self):
        # A throwaway schema per test, as the trade concurrency tests do.
        # Creating the tables in the database's default schema and dropping
        # them again would destroy whatever else lives there: TEST_POSTGRES_URL
        # is a connection string, not a promise that the database is empty.
        self.schema = f"rapid_set_entry_test_{uuid.uuid4().hex}"
        self.admin_engine = create_engine(POSTGRES_TEST_URL, isolation_level="AUTOCOMMIT")
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'CREATE SCHEMA "{self.schema}"'))
        self.engine = create_engine(
            POSTGRES_TEST_URL,
            connect_args={"options": f"-csearch_path={self.schema}"},
            pool_pre_ping=True,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)

        db = self.Session()
        try:
            user = User(username="pg-rapid-entry-owner", hashed_password="x", is_active=True)
            set_row = Set(id="pg-rapid_en", tcg_set_id="pg-rapid", name="Postgres rapid", lang="en")
            card = Card(id="pg-rapid-001_en", tcg_card_id="pg-rapid-001", name="Postgres card", set_id="pg-rapid", number="001", lang="en")
            db.add_all([user, set_row, card])
            db.commit()
            self.user_id = user.id
            self.set_id = set_row.id
            self.card_id = card.id
            # The existing row is the locking target. This directly exercises
            # the production race where two rapid sessions merge the same copy.
            db.add(CollectionItem(card_id=card.id, user_id=user.id, quantity=1, condition="Mint", variant="Normal", lang="en"))
            db.commit()
        finally:
            db.close()

    def tearDown(self):
        self.engine.dispose()
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin_engine.dispose()

    def test_two_concurrent_sessions_merge_without_losing_a_copy(self):
        start = threading.Barrier(2)
        entered_write = threading.Event()
        release_first = threading.Event()
        results = []
        errors = []
        guard = threading.Lock()
        lock_entries = 0
        original_locked_item = rapid_set_entry._locked_collection_item

        def pause_after_lock(*args, **kwargs):
            nonlocal lock_entries
            item = original_locked_item(*args, **kwargs)
            with guard:
                lock_entries += 1
                entered_write.set()
            self.assertTrue(release_first.wait(timeout=5), "second session did not block on the collection row")
            return item

        def run_session():
            db = self.Session()
            try:
                start.wait(timeout=5)
                result = rapid_set_entry.commit_rapid_set_entry(
                    db,
                    set_id=self.set_id,
                    items=[SimpleNamespace(card_id=self.card_id, quantity=1, condition="Mint", variant="Normal", lang="en")],
                    current_user=User(id=self.user_id),
                )
                with guard:
                    results.append(result)
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                db.close()

        # PostgreSQL itself, rather than SQLite's no-op FOR UPDATE behaviour,
        # is the concurrency control. Both workers race the same existing row.
        with patch("services.rapid_set_entry._locked_collection_item", side_effect=pause_after_lock):
            first = threading.Thread(target=run_session, name="rapid-entry-first")
            second = threading.Thread(target=run_session, name="rapid-entry-second")
            first.start()
            second.start()
            self.assertTrue(entered_write.wait(timeout=5), "no session reached the locked collection row")
            # The bystander session must still be blocked on SELECT FOR UPDATE.
            self.assertEqual(lock_entries, 1)
            release_first.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        db = self.Session()
        try:
            self.assertEqual(db.query(CollectionItem).one().quantity, 3)
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
