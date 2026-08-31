"""Real PostgreSQL row-lock coverage for rapid set entry."""

import os
import threading
import unittest
import uuid
from types import SimpleNamespace

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
            first_card = Card(id="pg-rapid-001_en", tcg_card_id="pg-rapid-001", name="Postgres card one", set_id="pg-rapid", number="001", lang="en")
            second_card = Card(id="pg-rapid-002_en", tcg_card_id="pg-rapid-002", name="Postgres card two", set_id="pg-rapid", number="002", lang="en")
            db.add_all([user, set_row, first_card, second_card])
            db.commit()
            self.user_id = user.id
            self.set_id = set_row.id
            self.card_id = first_card.id
            self.second_card_id = second_card.id
        finally:
            db.close()

    def tearDown(self):
        self.engine.dispose()
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin_engine.dispose()

    def test_two_concurrent_first_inserts_merge_without_duplicate_rows(self):
        start = threading.Barrier(2)
        results = []
        errors = []
        guard = threading.Lock()

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

        # No collection row exists at the start. A row lock alone cannot
        # protect this case, so this must use the transaction-scoped owner lock.
        first = threading.Thread(target=run_session, name="rapid-entry-first")
        second = threading.Thread(target=run_session, name="rapid-entry-second")
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(len(results), 2)
        db = self.Session()
        try:
            self.assertEqual(db.query(CollectionItem).one().quantity, 2)
        finally:
            db.close()

    def test_reversed_multi_card_requests_cannot_deadlock(self):
        """One owner lock gives scan/request ordering a shared boundary."""
        start = threading.Barrier(2)
        errors = []
        guard = threading.Lock()

        def run_session(card_ids):
            db = self.Session()
            try:
                start.wait(timeout=5)
                rapid_set_entry.commit_rapid_set_entry(
                    db,
                    set_id=self.set_id,
                    items=[
                        SimpleNamespace(card_id=card_id, quantity=1, condition="Mint", variant="Normal", lang="en")
                        for card_id in card_ids
                    ],
                    current_user=User(id=self.user_id),
                )
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                db.close()

        first = threading.Thread(
            target=run_session, args=([self.card_id, self.second_card_id],), name="rapid-entry-forward"
        )
        second = threading.Thread(
            target=run_session, args=([self.second_card_id, self.card_id],), name="rapid-entry-reverse"
        )
        first.start()
        second.start()
        first.join(timeout=10)
        second.join(timeout=10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        db = self.Session()
        try:
            quantities = dict(db.query(CollectionItem.card_id, CollectionItem.quantity).all())
            self.assertEqual(quantities, {self.card_id: 2, self.second_card_id: 2})
        finally:
            db.close()


if __name__ == "__main__":
    unittest.main()
