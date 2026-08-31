"""Real PostgreSQL locking coverage for confident scan bulk filing."""

import datetime
import io
import os
import tempfile
import threading
import time
import unittest
from unittest.mock import patch

try:
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from PIL import Image

    from database import Base
    from models import Card, CollectionItem, ScanJob, ScanJobItem, User
    from services import scan_bulk_add
    from services.scan_queue import replace_scan_item_photo
    from services.scan_storage import ScanItemNoLongerReviewable

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(
    DEPS_AVAILABLE and POSTGRES_TEST_URL.startswith("postgresql"),
    "TEST_POSTGRES_URL is required for PostgreSQL bulk-add locking coverage",
)
class ScanBulkAddPostgresTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.engine = create_engine(POSTGRES_TEST_URL, pool_pre_ping=True)
        Base.metadata.drop_all(cls.engine)
        Base.metadata.create_all(cls.engine)
        cls.Session = sessionmaker(bind=cls.engine)

    @classmethod
    def tearDownClass(cls):
        Base.metadata.drop_all(cls.engine)
        cls.engine.dispose()

    def setUp(self):
        db = self.Session()
        try:
            self.user = User(username="pg-confident-bulk-owner", hashed_password="x", is_active=True)
            db.add(self.user)
            db.commit()
            self.user_id = self.user.id
            self.current_user = User(id=self.user_id)
            self.job = ScanJob(
                user_id=self.user.id,
                expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=1),
            )
            db.add(self.job)
            db.add(Card(
                id="pg-card-1_en",
                tcg_card_id="pg-card-1",
                name="Postgres confident card",
                lang="en",
                variants_normal=True,
            ))
            db.flush()
            self.item = ScanJobItem(
                job_id=self.job.id,
                user_id=self.user.id,
                position=0,
                byte_size=0,
                status="done",
                resolved=False,
                matches=[{"id": "pg-card-1_en"}],
                identity_confident=True,
                suggested_match_id="pg-card-1_en",
            )
            db.add(self.item)
            db.commit()
            self.job_id = self.job.id
            self.item_id = self.item.id
        finally:
            db.close()

    def tearDown(self):
        db = self.Session()
        try:
            db.query(CollectionItem).delete()
            db.query(ScanJobItem).delete()
            db.query(Card).delete()
            db.query(ScanJob).delete()
            db.query(User).delete()
            db.commit()
        finally:
            db.close()

    def test_two_concurrent_requests_file_one_copy_and_one_resolution(self):
        """The job row lock is the duplicate-prevention boundary on PostgreSQL."""
        start = threading.Barrier(2)
        entered_copy = threading.Event()
        allow_commit = threading.Event()
        results = []
        errors = []
        lock = threading.Lock()
        copy_entries = 0
        original = scan_bulk_add._add_collection_copy

        def pause_first_copy(*args, **kwargs):
            nonlocal copy_entries
            original(*args, **kwargs)
            with lock:
                copy_entries += 1
                entered_copy.set()
            self.assertTrue(allow_commit.wait(timeout=5), "second request did not block on the job lock")

        def file_once():
            db = self.Session()
            try:
                start.wait(timeout=5)
                result = scan_bulk_add.add_all_confident_scan_items(
                    db,
                    job_id=self.job_id,
                    current_user=self.current_user,
                    prepared_card_ids={"pg-card-1_en"},
                )
                with lock:
                    results.append(result)
            except BaseException as exc:  # Preserve failures raised inside a thread.
                with lock:
                    errors.append(exc)
            finally:
                db.close()

        with patch("services.scan_bulk_add._add_collection_copy", side_effect=pause_first_copy):
            first = threading.Thread(target=file_once, name="branch5-bulk-add-first")
            second = threading.Thread(target=file_once, name="branch5-bulk-add-second")
            first.start()
            second.start()
            self.assertTrue(entered_copy.wait(timeout=5), "no request reached the locked mutation")
            # The bystander request is still waiting on ScanJob FOR UPDATE here.
            time.sleep(0.2)
            self.assertEqual(copy_entries, 1)
            self.assertEqual(results, [])
            allow_commit.set()
            first.join(timeout=10)
            second.join(timeout=10)

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertEqual(sorted(results), [0, 1])

        db = self.Session()
        try:
            self.assertEqual(db.query(CollectionItem).count(), 1)
            self.assertEqual(db.query(CollectionItem).one().quantity, 1)
            self.assertTrue(db.get(ScanJobItem, self.item_id).resolved)
        finally:
            db.close()

    def test_bulk_add_and_retake_complete_without_a_lock_cycle(self):
        """Both paths lock the item before its job on a real PostgreSQL DB."""
        temp_dir = tempfile.TemporaryDirectory()
        source = os.path.join(temp_dir.name, f"{self.job_id}-source.jpg")
        with open(source, "wb") as handle:
            handle.write(b"old scan")
        db = self.Session()
        try:
            item = db.get(ScanJobItem, self.item_id)
            item.image_path = os.path.basename(source)
            db.commit()
        finally:
            db.close()

        entered_copy = threading.Event()
        release_copy = threading.Event()
        results = []
        errors = []
        guard = threading.Lock()
        original = scan_bulk_add._add_collection_copy

        def pause_bulk(*args, **kwargs):
            original(*args, **kwargs)
            entered_copy.set()
            self.assertTrue(release_copy.wait(timeout=5), "re-take did not reach the item lock")

        image = io.BytesIO()
        Image.new("RGB", (400, 560), "#385898").save(image, format="JPEG")

        def bulk_add():
            db = self.Session()
            try:
                results.append(scan_bulk_add.add_all_confident_scan_items(
                    db, job_id=self.job_id, current_user=self.current_user,
                    prepared_card_ids={"pg-card-1_en"},
                ))
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                db.close()

        def retake():
            db = self.Session()
            try:
                item = db.get(ScanJobItem, self.item_id)
                replace_scan_item_photo(db, item, image.getvalue())
                results.append("retaken")
            except ScanItemNoLongerReviewable:
                # Bulk filing won the item lock. That is a safe race outcome;
                # the important property is that both transactions complete.
                results.append("already-handled")
            except BaseException as exc:
                with guard:
                    errors.append(exc)
            finally:
                db.close()

        try:
            with patch.dict(os.environ, {"SCAN_UPLOAD_DIR": temp_dir.name}), \
                    patch("services.scan_bulk_add._add_collection_copy", side_effect=pause_bulk):
                first = threading.Thread(target=bulk_add, name="bulk-add-lock-order")
                first.start()
                self.assertTrue(entered_copy.wait(timeout=5), "bulk add did not acquire its locks")
                second = threading.Thread(target=retake, name="retake-lock-order")
                second.start()
                time.sleep(0.2)
                self.assertTrue(second.is_alive(), "re-take should wait on the bulk item's lock")
                release_copy.set()
                first.join(timeout=10)
                second.join(timeout=10)
        finally:
            temp_dir.cleanup()

        self.assertFalse(first.is_alive())
        self.assertFalse(second.is_alive())
        self.assertEqual(errors, [])
        self.assertIn(1, results)
        self.assertIn("already-handled", results)


if __name__ == "__main__":
    unittest.main()
