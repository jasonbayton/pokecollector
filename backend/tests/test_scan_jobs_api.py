import io
import datetime
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, patch

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
    from models import Card, CollectionItem, ScanJob, ScanJobItem, User
    from services.scan_storage import MAX_JOB_BYTES, resolve_scan_path
    from services import scan_queue as scan_queue_module

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _jpeg_bytes(*, size=(80, 112), color="#d92828"):
    image = Image.new("RGB", size, color)
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner API dependencies are not installed")
class ScanJobsApiTests(unittest.TestCase):
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
        self.user = User(username="scan-api-owner", hashed_password="x", is_active=True)
        self.other_user = User(username="scan-api-other", hashed_password="x", is_active=True)
        self.db.add_all([self.user, self.other_user])
        self.db.commit()

        app = FastAPI()
        app.include_router(router, prefix="/api/cards")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.app = app
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    def _enqueue(self):
        with patch("api.recognize.get_gemini_key", return_value="secret-key"), \
                patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                "/api/cards/recognize/jobs",
                files={"files": ("private-name.jpg", _jpeg_bytes(), "image/jpeg")},
            )
        self.assertEqual(response.status_code, 200, response.text)
        return response.json()

    def test_enqueue_list_detail_and_authenticated_image(self):
        created = self._enqueue()
        job_id = created["id"]

        listed = self.client.get("/api/cards/recognize/jobs")
        self.assertEqual(listed.status_code, 200)
        self.assertEqual([job["id"] for job in listed.json()["jobs"]], [job_id])
        self.assertEqual(listed.json()["jobs"][0]["active"], 1)
        self.assertEqual(listed.json()["jobs"][0]["attention"], 0)

        detail = self.client.get(f"/api/cards/recognize/jobs/{job_id}")
        self.assertEqual(detail.status_code, 200)
        item = detail.json()["items"][0]
        self.assertTrue(item["has_image"])
        self.assertNotIn("private-name", self.db.get(ScanJobItem, item["id"]).image_path)

        image = self.client.get(
            f"/api/cards/recognize/jobs/{job_id}/items/{item['id']}/image"
        )
        self.assertEqual(image.status_code, 200)
        self.assertEqual(image.content[:2], b"\xff\xd8")

    def test_retry_schedule_is_exposed_in_list_and_detail_payloads(self):
        created = self._enqueue()
        retry_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "retrying"
        item.next_attempt_at = retry_at
        item.retry_reason = "daily_quota"
        self.db.commit()

        listed_job = self.client.get("/api/cards/recognize/jobs").json()["jobs"][0]
        detail_item = self.client.get(
            f"/api/cards/recognize/jobs/{created['id']}"
        ).json()["items"][0]

        self.assertEqual(listed_job["next_retry_at"], retry_at.isoformat())
        self.assertEqual(listed_job["retry_reason"], "daily_quota")
        self.assertEqual(detail_item["next_attempt_at"], retry_at.isoformat())
        self.assertEqual(detail_item["retry_reason"], "daily_quota")

    def test_detail_payload_carries_the_persisted_matcher_verdict(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        item.matches = [
            {"id": "base1-58_en", "tcg_card_id": "base1-58"},
            {"id": "base1-59_en", "tcg_card_id": "base1-59"},
        ]
        item.identity_confident = True
        item.identity_decision = "number_metadata"
        item.suggested_match_id = "base1-59_en"
        self.db.commit()

        payload = self.client.get(
            f"/api/cards/recognize/jobs/{created['id']}"
        ).json()["items"][0]

        self.assertIs(payload["identity_confident"], True)
        self.assertEqual(payload["identity_decision"], "number_metadata")
        self.assertEqual(payload["suggested_match_id"], "base1-59_en")

    def test_detail_payload_reports_an_unjudged_scan_as_unknown_not_as_unsure(self):
        """The negative control: a row from before this feature has no verdict.

        Null and false must stay distinguishable over the wire. Collapsing them
        would let the review UI claim the matcher considered a legacy scan and
        was not convinced, which never happened.
        """
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        item.matches = [{"id": "base1-58_en", "tcg_card_id": "base1-58"}]
        self.db.commit()

        payload = self.client.get(
            f"/api/cards/recognize/jobs/{created['id']}"
        ).json()["items"][0]

        self.assertIsNone(payload["identity_confident"])
        self.assertIsNone(payload["identity_decision"])
        self.assertIsNone(payload["suggested_match_id"])

    def test_other_user_cannot_access_job_or_photo(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        self.app.dependency_overrides[get_current_user] = lambda: self.other_user

        self.assertEqual(
            self.client.get(f"/api/cards/recognize/jobs/{created['id']}").status_code,
            404,
        )
        self.assertEqual(
            self.client.get(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/image"
            ).status_code,
            404,
        )

    def test_enqueue_preserves_order_and_individual_choices(self):
        with patch("api.recognize.get_gemini_key", return_value="secret-key"), \
                patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                "/api/cards/recognize/jobs",
                data={"individual_positions": "[1]"},
                files=[
                    ("files", ("first.jpg", _jpeg_bytes(), "image/jpeg")),
                    ("files", ("second.jpg", _jpeg_bytes(), "image/jpeg")),
                    ("files", ("third.jpg", _jpeg_bytes(), "image/jpeg")),
                ],
            )

        self.assertEqual(response.status_code, 200, response.text)
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual([item.position for item in items], [0, 1, 2])
        self.assertEqual([item.batch_mode for item in items], [True, False, True])

    def test_single_photo_is_always_individual(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        self.assertFalse(item.batch_mode)

    def test_resolve_deletes_photo_and_removes_fully_handled_job_from_inbox(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        stored = resolve_scan_path(item.image_path)
        item.status = "done"
        item.matches = [{"id": "card-1"}]
        self.db.commit()

        response = self.client.post(
            f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/resolve"
        )

        self.assertEqual(response.status_code, 200)
        self.assertTrue(response.json()["resolved"])
        self.assertFalse(stored.exists())
        self.assertEqual(self.client.get("/api/cards/recognize/jobs").json()["jobs"], [])

    def test_add_all_confident_files_only_validated_candidates(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        self.db.add(Card(
            id="card-1_en",
            tcg_card_id="card-1",
            name="Confident card",
            lang="en",
            variants_normal=True,
        ))
        item.status = "done"
        item.matches = [{"id": "card-1_en", "tcg_card_id": "card-1"}]
        item.identity_confident = True
        item.suggested_match_id = "card-1_en"
        self.db.commit()

        detail = self.client.get(f"/api/cards/recognize/jobs/{created['id']}")
        self.assertEqual(detail.status_code, 200, detail.text)
        self.assertEqual(detail.json()["confident_addable"], 1)

        response = self.client.post(f"/api/cards/recognize/jobs/{created['id']}/add-all-confident")

        self.assertEqual(response.status_code, 200, response.text)
        self.assertEqual(response.json(), {"added": 1, "condition": "Mint"})
        self.assertTrue(self.db.get(ScanJobItem, item.id).resolved)
        added = self.db.query(CollectionItem).one()
        self.assertEqual((added.card_id, added.quantity, added.condition, added.variant), (
            "card-1_en", 1, "Mint", "Normal",
        ))

    def test_resolve_labels_a_returned_candidate_as_a_candidate_correction(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        self.db.add(Card(
            id="card-1_en",
            tcg_card_id="card-1",
            name="Candidate card",
            lang="en",
            variants_normal=True,
        ))
        item.status = "done"
        item.matches = [{"id": "card-1_en", "tcg_card_id": "card-1"}]
        self.db.commit()

        with patch("services.scan_trace.record_ground_truth", return_value=1) as label:
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/resolve",
                json={"card_id": "card-1"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        label.assert_called_once_with(
            self.user.id,
            created["id"],
            item.id,
            "card-1",
            source="candidate",
        )

    def test_resolve_accepts_a_catalogue_card_that_was_not_a_candidate(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        manual_card = Card(
            id="different-card_de",
            tcg_card_id="different-card",
            name="Manually identified card",
            lang="de",
            variants_normal=True,
        )
        self.db.add(manual_card)
        item.status = "failed"
        item.matches = []
        self.db.commit()

        with patch("services.scan_trace.record_ground_truth", return_value=1) as label:
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/resolve",
                json={"card_id": "different-card", "lang": "de"},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertTrue(response.json()["resolved"])
        label.assert_called_once_with(
            self.user.id,
            created["id"],
            item.id,
            "different-card",
            source="manual",
        )

    def test_a_manual_correction_keeps_its_photo_and_a_candidate_pick_does_not(self):
        # The photo is the only record of what the recogniser could not read,
        # and the evidence for whether a later change to retrieval would have
        # found the card. Resolution deletes photos so a reviewed batch does
        # not fill the disk; a manually identified card is the exception.
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        stored = resolve_scan_path(item.image_path)
        self.db.add(Card(
            id="different-card_de",
            tcg_card_id="different-card",
            name="Manually identified card",
            lang="de",
            variants_normal=True,
        ))
        item.status = "failed"
        item.matches = []
        self.db.commit()

        response = self.client.post(
            f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/resolve",
            json={"card_id": "different-card", "lang": "de"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        persisted = self.db.get(ScanJobItem, item.id)
        self.assertTrue(persisted.resolved)
        self.assertIsNotNone(persisted.image_path)
        self.assertTrue(stored.exists())

    def test_a_candidate_pick_still_releases_its_photo(self):
        # The bystander: retaining evidence must not stop an ordinary review
        # from freeing its storage.
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        stored = resolve_scan_path(item.image_path)
        self.db.add(Card(
            id="card-1_en",
            tcg_card_id="card-1",
            name="Candidate card",
            lang="en",
            variants_normal=True,
        ))
        item.status = "done"
        item.matches = [{"id": "card-1_en", "tcg_card_id": "card-1"}]
        self.db.commit()

        response = self.client.post(
            f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/resolve",
            json={"card_id": "card-1"},
        )

        self.assertEqual(response.status_code, 200, response.text)
        persisted = self.db.get(ScanJobItem, item.id)
        self.assertTrue(persisted.resolved)
        self.assertIsNone(persisted.image_path)
        self.assertFalse(stored.exists())

    def test_resolve_rejects_a_non_catalogue_manual_card_server_side(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        item.matches = [{"id": "card-1_en", "tcg_card_id": "card-1"}]
        self.db.commit()

        response = self.client.post(
            f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/resolve",
            json={"card_id": "rubbish-card", "lang": "de"},
        )

        self.assertEqual(response.status_code, 404)
        self.assertFalse(self.db.get(ScanJobItem, item.id).resolved)

    def test_failed_photo_is_retained_and_retry_resets_it(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        stored = resolve_scan_path(item.image_path)
        item.status = "failed"
        item.attempts = 3
        item.error = "unreadable"
        self.db.commit()

        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/retry"
            )

        self.assertEqual(response.status_code, 200)
        self.assertEqual(response.json()["status"], "pending")
        self.assertEqual(response.json()["attempts"], 0)
        self.assertTrue(stored.exists())

    def test_retake_refuses_when_the_item_was_resolved_mid_upload(self):
        # The endpoint reads resolved and status before it sanitises the
        # upload, which is long enough for add-all or a dismissal to claim the
        # same row. Without a recheck under lock, the re-take would commit a
        # fresh photo and a pending status over a resolved item: the old card
        # already added, the new photo invisible to review, and a resolved row
        # queued for work.
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        self.db.commit()
        original_path = item.image_path

        real_sanitize = scan_queue_module.sanitize_image_bytes

        def resolve_it_first(raw, **kwargs):
            # Stand-in for the concurrent review action, run at the point the
            # endpoint has already passed its own guard. Keyword arguments are
            # forwarded rather than pinned, so the stub intercepts the call
            # without also asserting the signature of the thing it stands in
            # for.
            self.db.query(ScanJobItem).filter(ScanJobItem.id == item.id).update(
                {"resolved": True}
            )
            self.db.commit()
            return real_sanitize(raw, **kwargs)

        with patch.object(scan_queue_module, "sanitize_image_bytes", side_effect=resolve_it_first), \
                patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(size=(400, 560)), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 409)
        self.db.expire_all()
        persisted = self.db.get(ScanJobItem, item.id)
        self.assertTrue(persisted.resolved)
        self.assertEqual(persisted.image_path, original_path)

    def test_retake_budget_ignores_photos_that_were_already_deleted(self):
        # Resolution and add-all null image_path and delete the file but leave
        # byte_size, so charging the job for every row meant deleted photos
        # could push a valid replacement over the limit.
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        ghost = ScanJobItem(
            job_id=item.job_id,
            user_id=item.user_id,
            position=item.position + 1,
            image_path=None,
            content_type="image/jpeg",
            byte_size=MAX_JOB_BYTES,
            status="done",
            resolved=True,
            attempts=0,
            transient_failures=0,
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
        )
        self.db.add(ghost)
        self.db.commit()

        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(size=(400, 560)), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 200, response.text)

    def test_retake_survives_a_failure_to_unlink_the_replaced_photo(self):
        # The unlink runs after the commit, so by then the re-take has already
        # happened. Letting an OSError out of it reported a failure for work
        # that succeeded, and the caller would reasonably retry it. The row no
        # longer references the old file either way.
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        self.db.commit()
        old_path = item.image_path

        real_delete = scan_queue_module.delete_scan_image

        def fail_on_the_old_photo(path):
            if path == old_path:
                raise OSError("device busy")
            return real_delete(path)

        with patch.object(scan_queue_module, "delete_scan_image", side_effect=fail_on_the_old_photo), \
                patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(size=(400, 560)), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.db.expire_all()
        persisted = self.db.get(ScanJobItem, item.id)
        self.assertNotEqual(persisted.image_path, old_path)
        self.assertEqual(persisted.status, "pending")

    def test_retake_changes_the_image_token_so_the_panel_refetches(self):
        # The review panel fetches each photo into a blob URL once, keyed on
        # what the payload says identifies the image. Keyed on the item id
        # alone it never refetched, so a re-take left the user looking at the
        # photo they had just replaced while the scan ran on the new one.
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        self.db.commit()
        before = self.client.get(
            f"/api/cards/recognize/jobs/{created['id']}"
        ).json()["items"][0]["image_token"]
        self.assertIsNotNone(before)

        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(size=(400, 560), color="#385898"), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.assertNotEqual(response.json()["image_token"], before)

    def test_image_token_survives_a_status_change_that_leaves_the_photo_alone(self):
        # The bystander for the test above. A token that changed on every
        # status transition would also make it pass, while refetching the same
        # image on every poll.
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        self.db.commit()
        before = self.client.get(
            f"/api/cards/recognize/jobs/{created['id']}"
        ).json()["items"][0]["image_token"]

        item.status = "failed"
        item.error = "provider timeout"
        item.updated_at = datetime.datetime.utcnow()
        self.db.commit()

        after = self.client.get(
            f"/api/cards/recognize/jobs/{created['id']}"
        ).json()["items"][0]["image_token"]
        self.assertEqual(after, before)

    def test_a_job_byte_overflow_is_raised_as_its_own_type(self):
        # The re-take endpoint answers 409 for this and 400 for every other
        # upload complaint. That used to be decided by string-matching the
        # message, so rephrasing it would silently have changed the status
        # code. The distinction is now carried by the type.
        import asyncio

        from services.scan_storage import (
            ScanJobBytesExceeded,
            ScanUploadError,
            read_limited_upload,
        )

        self.assertTrue(issubclass(ScanJobBytesExceeded, ScanUploadError))
        # It refuses on the budget before it reads anything, so there is no
        # upload to supply here.
        with self.assertRaises(ScanJobBytesExceeded):
            asyncio.run(read_limited_upload(None, remaining_job_bytes=0))

    def test_retake_rejects_an_item_that_was_already_resolved(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        item.resolved = True
        self.db.commit()

        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(color="#385898"), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "This scan has already been handled.")

    def test_retake_rejects_a_processing_item_without_touching_its_photo(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        old_path = item.image_path
        old_file = resolve_scan_path(old_path)
        old_bytes = old_file.read_bytes()
        item.status = "processing"
        self.db.commit()

        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(color="#385898"), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "This scan is still being processed.")
        self.db.refresh(item)
        self.assertEqual(item.image_path, old_path)
        self.assertEqual(old_file.read_bytes(), old_bytes)

    def test_retake_rejects_a_photo_that_would_exceed_the_job_byte_limit(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        item.status = "done"
        self.db.commit()

        with patch("api.scan_jobs.MAX_JOB_BYTES", item.byte_size), \
                patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(size=(400, 560)), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 409)
        self.assertEqual(response.json()["detail"], "The scan job exceeds the 200 MB upload limit.")

    def test_retake_replaces_the_photo_and_clears_the_previous_scan_result(self):
        created = self._enqueue()
        job = self.db.get(ScanJob, created["id"])
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        old_path = item.image_path
        old_file = resolve_scan_path(old_path)
        item.status = "done"
        item.attempts = 3
        item.transient_failures = 2
        item.batch_mode = True
        item.recognized = {"name": "Wrong"}
        item.matches = [{"id": "wrong-card"}]
        item.identity_confident = True
        item.identity_decision = "number_unique"
        item.suggested_match_id = "wrong-card"
        item.error = "old error"
        job.status = "done"
        job.finished_at = datetime.datetime.utcnow()
        job.error_message = "old job error"
        self.db.commit()

        with patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            response = self.client.post(
                f"/api/cards/recognize/jobs/{created['id']}/items/{item.id}/photo",
                files={"file": ("retake.jpg", _jpeg_bytes(size=(400, 560), color="#385898"), "image/jpeg")},
            )

        self.assertEqual(response.status_code, 200, response.text)
        self.db.refresh(item)
        self.db.refresh(job)
        self.assertNotEqual(item.image_path, old_path)
        self.assertEqual(item.content_type, "image/jpeg")
        self.assertGreater(item.byte_size, 0)
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.transient_failures, 0)
        self.assertIsNone(item.recognized)
        self.assertIsNone(item.matches)
        self.assertIsNone(item.identity_confident)
        self.assertIsNone(item.identity_decision)
        self.assertIsNone(item.suggested_match_id)
        self.assertIsNone(item.error)
        self.assertFalse(item.batch_mode)
        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.finished_at)
        self.assertIsNone(job.error_message)
        self.assertFalse(old_file.exists())
        self.assertTrue(resolve_scan_path(item.image_path).is_file())

    def test_deleting_job_removes_database_rows_and_photo_directory(self):
        created = self._enqueue()
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == created["id"]).one()
        job_dir = resolve_scan_path(item.image_path).parent

        response = self.client.delete(f"/api/cards/recognize/jobs/{created['id']}")

        self.assertEqual(response.status_code, 200)
        self.assertIsNone(self.db.get(ScanJob, created["id"]))
        self.assertFalse(job_dir.exists())


if __name__ == "__main__":
    unittest.main()
