import datetime
import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

DEPS_AVAILABLE = True
try:
    from fastapi import FastAPI
    from fastapi.testclient import TestClient
    from PIL import Image
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from api.scan_jobs import router
    from api.auth import get_current_user
    from database import Base, get_db
    from models import ScanJob, ScanJobItem, User
    from services.scan_storage import (
        COMPOSITE_GROUP_SIZE,
        COMPOSITE_LINGER_SECONDS,
        MAX_PENDING_ITEMS,
    )
    from services.scan_queue import claim_next_scan_item
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _jpeg_bytes(*, size=(80, 112), color="#3355cc"):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner API dependencies are not installed")
class RollingScanQueueTests(unittest.TestCase):
    """Photographing is submitting: there is no staging tray and no send."""

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SCAN_UPLOAD_DIR": self.temp_dir.name})
        self.env.start()
        self.engine = create_engine(
            "sqlite://",
            connect_args={"check_same_thread": False},
            poolclass=StaticPool,
        )
        Base.metadata.create_all(self.engine)
        self.Session = sessionmaker(bind=self.engine)
        self.db = self.Session()
        self.user = User(username="rolling-owner", hashed_password="x", is_active=True)
        self.other_user = User(username="rolling-other", hashed_password="x", is_active=True)
        self.db.add_all([self.user, self.other_user])
        self.db.commit()

        app = FastAPI()
        app.include_router(router, prefix="/api/cards")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

        # Enqueue refuses without a usable provider credential, which is
        # correct and is tested elsewhere. It is not what any of this is about.
        provider = type("StubProvider", (), {
            "requires_credential": lambda self: False,
            "credential": lambda self, db, user_id: "stub",
            "missing_credential_message": lambda self: "",
        })()
        self.provider_patch = patch("services.scan_providers.get_provider", return_value=provider)
        self.provider_patch.start()
        self.addCleanup(self.provider_patch.stop)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    def _open_rolling_job(self):
        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                "/api/cards/recognize/jobs",
                files={"files": ("first.jpg", _jpeg_bytes(), "image/jpeg")},
                data={"rolling": "true"},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()["id"]

    def _append(self, job_id, *, count=1, expect=200):
        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{job_id}/items",
                files=[
                    ("files", (f"more-{index}.jpg", _jpeg_bytes(), "image/jpeg"))
                    for index in range(count)
                ],
            )
        self.assertEqual(response.status_code, expect, response.text)
        return response

    def _items(self, job_id):
        return (
            self.db.query(ScanJobItem)
            .filter(ScanJobItem.job_id == job_id)
            .order_by(ScanJobItem.position.asc())
            .all()
        )

    def test_a_photo_taken_now_is_held_so_it_can_share_a_request(self):
        # The cost trap this whole design exists to avoid. The composite
        # processor tiles two to four cards into ONE vision request, so a photo
        # that became claimable the instant it arrived would always be scanned
        # alone and cost roughly four times as much.
        job_id = self._open_rolling_job()
        item = self._items(job_id)[0]

        self.assertTrue(item.batch_mode)
        self.assertGreater(item.next_attempt_at, datetime.datetime.utcnow())

        # Nothing to claim yet, precisely because it is still gathering.
        self.assertIsNone(claim_next_scan_item(self.db))

    def test_the_wait_ends_on_its_own(self):
        job_id = self._open_rolling_job()
        item = self._items(job_id)[0]

        later = datetime.datetime.utcnow() + datetime.timedelta(seconds=COMPOSITE_LINGER_SECONDS + 1)
        claim = claim_next_scan_item(self.db, now=later)

        self.assertIsNotNone(claim, "the photo never became claimable")
        self.assertIn(item.id, claim.all_item_ids)

    def test_a_full_group_does_not_wait_at_all(self):
        # Someone photographing quickly should never pay the linger: once
        # enough have gathered there is nothing left to gather.
        job_id = self._open_rolling_job()
        self._append(job_id, count=COMPOSITE_GROUP_SIZE - 1)

        released = [item for item in self._items(job_id) if item.next_attempt_at <= datetime.datetime.utcnow()]

        self.assertEqual(len(released), COMPOSITE_GROUP_SIZE)
        self.assertIsNotNone(claim_next_scan_item(self.db), "a full group was still waiting")

    def test_appended_photos_continue_the_job_rather_than_starting_one(self):
        job_id = self._open_rolling_job()
        self._append(job_id, count=2)

        items = self._items(job_id)
        self.assertEqual([item.position for item in items], [0, 1, 2])
        self.assertEqual({item.job_id for item in items}, {job_id})
        self.assertTrue(all(item.batch_mode for item in items))

    def test_the_ceiling_counts_work_in_flight_across_every_job(self):
        # MAX_FILES_PER_JOB bounds one job's stored photos. This is a different
        # limit: how much recognition may be outstanding at once.
        job_id = self._open_rolling_job()
        filler = [
            ScanJobItem(
                job_id=job_id,
                user_id=self.user.id,
                position=100 + index,
                image_path=None,
                content_type="image/jpeg",
                byte_size=10,
                batch_mode=False,
                status="pending",
                resolved=False,
                attempts=0,
                transient_failures=0,
                created_at=datetime.datetime.utcnow(),
                updated_at=datetime.datetime.utcnow(),
            )
            for index in range(MAX_PENDING_ITEMS)
        ]
        self.db.add_all(filler)
        self.db.commit()

        response = self._append(job_id, expect=429)
        self.assertIn(str(MAX_PENDING_ITEMS), response.json()["detail"])

    def test_resolved_photos_stop_counting_towards_the_ceiling(self):
        # Their file has already been deleted, so holding their place would
        # make a long session stall for no reason.
        job_id = self._open_rolling_job()
        # In a different job, deliberately. Putting them in this one would hit
        # MAX_FILES_PER_JOB instead, which is a different limit and is what the
        # first version of this test actually measured.
        older = ScanJob(
            user_id=self.user.id,
            status="done",
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=1),
        )
        self.db.add(older)
        self.db.commit()
        for index in range(MAX_PENDING_ITEMS):
            self.db.add(ScanJobItem(
                job_id=older.id,
                user_id=self.user.id,
                position=200 + index,
                image_path=None,
                content_type="image/jpeg",
                byte_size=10,
                batch_mode=False,
                status="done",
                resolved=True,
                attempts=0,
                transient_failures=0,
                created_at=datetime.datetime.utcnow(),
                updated_at=datetime.datetime.utcnow(),
            ))
        self.db.commit()

        self._append(job_id, expect=200)

    def test_another_user_cannot_append_to_this_job(self):
        job_id = self._open_rolling_job()
        job = self.db.get(ScanJob, job_id)
        job.user_id = self.other_user.id
        self.db.commit()

        self._append(job_id, expect=404)

    def test_a_submitted_batch_is_unchanged_by_any_of_this(self):
        # The bystander. Rolling is opt in, so the existing submit path must
        # still enqueue photos that are claimable straight away.
        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                "/api/cards/recognize/jobs",
                files=[
                    ("files", ("a.jpg", _jpeg_bytes(), "image/jpeg")),
                    ("files", ("b.jpg", _jpeg_bytes(), "image/jpeg")),
                ],
            )
        self.assertEqual(response.status_code, 200, response.text)
        items = self._items(response.json()["id"])
        self.assertTrue(all(item.next_attempt_at <= datetime.datetime.utcnow() for item in items))
        self.assertIsNotNone(claim_next_scan_item(self.db))


if __name__ == "__main__":
    unittest.main()
