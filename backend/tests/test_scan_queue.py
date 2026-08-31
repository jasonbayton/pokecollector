import datetime
import hashlib
import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import ScanJob, ScanJobItem, ScanQueueUserState, User
    from services import scan_queue, scan_storage
    from services.scan_queue import (
        ClaimedScanItem,
        claim_next_scan_item,
        complete_claim,
        fail_claim,
        purge_expired_scan_jobs,
        recover_expired_leases,
        resolve_scan_item,
        retry_scan_item,
        replace_scan_item_photo,
        job_progress,
        complete_claim_group,
        CATALOGUE_UNREACHABLE_RETRY_REASON,
        MAX_CATALOGUE_UNREACHABLE_ATTEMPTS,
    )

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class ScanQueueTests(unittest.TestCase):
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
        self.users = [
            User(username="queue-a", hashed_password="x"),
            User(username="queue-b", hashed_password="x"),
        ]
        self.db.add_all(self.users)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    def _job(self, user, *, positions=(0,), created_at=None, expires_at=None):
        now = created_at or datetime.datetime.utcnow()
        job = ScanJob(
            user_id=user.id,
            status="pending",
            created_at=now,
            updated_at=now,
            expires_at=expires_at or now + datetime.timedelta(days=14),
        )
        self.db.add(job)
        self.db.flush()
        if self.db.get(ScanQueueUserState, user.id) is None:
            self.db.add(ScanQueueUserState(user_id=user.id))
        for position in positions:
            self.db.add(
                ScanJobItem(
                    job_id=job.id,
                    user_id=user.id,
                    position=position,
                    image_path=f"{job.id}/{position}.jpg",
                    content_type="image/jpeg",
                    byte_size=4,
                    status="pending",
                    resolved=False,
                    attempts=0,
                    transient_failures=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                )
            )
        self.db.commit()
        return job

    def test_dispatch_rotates_between_users(self):
        self._job(self.users[0], positions=(0, 1))
        self._job(self.users[1], positions=(0,))

        first = claim_next_scan_item(self.db)
        first_item = self.db.get(ScanJobItem, first.item_id)
        self.assertEqual(first_item.user_id, self.users[0].id)
        complete_claim(self.db, first, {"recognized": {"name": "A"}, "matches": []})

        second = claim_next_scan_item(self.db)
        second_item = self.db.get(ScanJobItem, second.item_id)
        self.assertEqual(second_item.user_id, self.users[1].id)

    def _matcher_result(self, *, confident, decision=None, suggested=None, matches=None):
        """A result shaped exactly like api.recognize.match_card_info returns."""
        return {
            "recognized": {"name": "Pikachu", "number": "58"},
            "matches": matches if matches is not None else [
                {"id": "base1-58_en", "tcg_card_id": "base1-58", "name": "Pikachu"},
                {"id": "base1-59_en", "tcg_card_id": "base1-59", "name": "Pikachu"},
            ],
            "_number_match_count": 1,
            "_identity_confident": confident,
            "_identity_decision": decision,
            "_identity_suggested_match_id": suggested,
        }

    def test_confident_individual_scan_persists_the_matchers_verdict(self):
        self._job(self.users[0])
        claim = claim_next_scan_item(self.db)

        self.assertTrue(complete_claim(
            self.db,
            claim,
            # Deliberately not the first-ranked candidate: a suggestion read
            # from rank order instead of from the matcher would record
            # base1-58 here and the assertion below would catch it.
            self._matcher_result(
                confident=True, decision="number_unique", suggested="base1-59_en"
            ),
        ))

        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "done")
        self.assertIs(item.identity_confident, True)
        self.assertEqual(item.identity_decision, "number_unique")
        self.assertEqual(item.suggested_match_id, "base1-59_en")

    def test_inconclusive_individual_scan_persists_no_suggestion(self):
        """The negative control: unsure must be recorded, not left unknown."""
        self._job(self.users[0])
        claim = claim_next_scan_item(self.db)

        self.assertTrue(complete_claim(
            self.db,
            claim,
            self._matcher_result(confident=False),
        ))

        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "done")
        self.assertIs(item.identity_confident, False)
        self.assertIsNone(item.identity_decision)
        self.assertIsNone(item.suggested_match_id)
        # The candidates still arrive; only the claim about them is withheld.
        self.assertEqual(len(item.matches), 2)

    def test_an_unconfident_result_cannot_smuggle_a_suggested_id_through(self):
        """A stray id without confidence must never reach the Suggested badge."""
        self._job(self.users[0])
        claim = claim_next_scan_item(self.db)

        complete_claim(
            self.db,
            claim,
            self._matcher_result(confident=False, suggested="base1-59_en"),
        )

        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertIs(item.identity_confident, False)
        self.assertIsNone(item.suggested_match_id)

    def test_composite_siblings_persist_and_clear_their_own_verdicts(self):
        self._job(self.users[0], positions=(0, 1))
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        for item in items:
            item.batch_mode = True
            # A verdict left over from an earlier pass over the same photo. If
            # the fallback branch does not clear it, position 1 goes back to
            # pending still advertising a match it no longer has.
            item.identity_confident = True
            item.identity_decision = "stale_decision"
            item.suggested_match_id = "stale-card"
        self.db.commit()
        claim = claim_next_scan_item(self.db)
        self.assertTrue(claim.composite)

        self.assertTrue(complete_claim_group(
            self.db,
            claim,
            [
                self._matcher_result(
                    confident=True, decision="phash", suggested="base1-59_en"
                ),
                None,
            ],
        ))

        self.db.expire_all()
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual(items[0].status, "done")
        self.assertIs(items[0].identity_confident, True)
        self.assertEqual(items[0].identity_decision, "phash")
        self.assertEqual(items[0].suggested_match_id, "base1-59_en")
        # The negative control, in the same run: the unclear sibling.
        self.assertEqual(items[1].status, "pending")
        self.assertIsNone(items[1].identity_confident)
        self.assertIsNone(items[1].identity_decision)
        self.assertIsNone(items[1].suggested_match_id)

    def test_retry_clears_the_previous_verdict_and_leaves_siblings_alone(self):
        job = self._job(self.users[0], positions=(0, 1))
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir()
        for item in items:
            (job_dir / f"{item.position}.jpg").write_bytes(b"jpeg")
            item.status = "done"
            item.recognized = {"name": "Wrong"}
            item.matches = [{"id": "base1-58_en", "tcg_card_id": "base1-58"}]
            item.identity_confident = True
            item.identity_decision = "number_unique"
            item.suggested_match_id = "base1-58_en"
        self.db.commit()

        retry_scan_item(self.db, items[0])

        self.db.expire_all()
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual(items[0].status, "pending")
        self.assertIsNone(items[0].identity_confident)
        self.assertIsNone(items[0].identity_decision)
        self.assertIsNone(items[0].suggested_match_id)
        # The negative control: retrying one photo must not disturb the other.
        self.assertEqual(items[1].status, "done")
        self.assertIs(items[1].identity_confident, True)
        self.assertEqual(items[1].identity_decision, "number_unique")
        self.assertEqual(items[1].suggested_match_id, "base1-58_en")

    def test_batch_claim_groups_four_photos_and_keeps_forced_single_out(self):
        job = self._job(self.users[0], positions=(0, 1, 2, 3, 4))
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        for item in items:
            item.batch_mode = True
        items[1].batch_mode = False
        self.db.commit()

        claim = claim_next_scan_item(self.db)

        self.assertTrue(claim.composite)
        self.assertEqual(claim.all_item_ids, (items[0].id, items[2].id, items[3].id, items[4].id))
        self.assertEqual(items[1].status, "pending")
        self.assertTrue(complete_claim_group(
            self.db,
            claim,
            [{"recognized": {"name": str(index)}, "matches": []} for index in range(4)],
        ))
        self.assertEqual(items[1].status, "pending")

    def test_unclear_composite_position_retries_without_confident_siblings(self):
        self._job(self.users[0], positions=(0, 1, 2, 3))
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        for item in items:
            item.batch_mode = True
        self.db.commit()

        composite_claim = claim_next_scan_item(self.db)
        def confident(name):
            return {
                "recognized": {"name": name, "number": "25"},
                "matches": [{"id": f"card-{name}"}],
            }
        self.assertTrue(complete_claim_group(
            self.db,
            composite_claim,
            [confident("A"), None, confident("C"), confident("D")],
        ))
        self.db.expire_all()
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual([item.status for item in items], ["done", "pending", "done", "done"])
        self.assertFalse(items[1].batch_mode)

        fallback_claim = claim_next_scan_item(self.db)
        self.assertEqual(fallback_claim.all_item_ids, (items[1].id,))
        fail_claim(self.db, fallback_claim, "429", transient=True)
        self.db.expire_all()
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual([item.status for item in items], ["done", "retrying", "done", "done"])
        self.assertEqual([item.transient_failures for item in items], [0, 1, 0, 0])

    def test_an_unclear_position_starts_its_individual_scan_with_a_full_allowance(self):
        # The composite met the outages, and every member of the group was
        # charged for each one. A position that then falls out for an
        # individual scan is starting again, so it must not inherit a budget
        # that is already nearly spent and die on its first outage.
        self._job(self.users[0], positions=(0, 1))
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        for item in items:
            item.batch_mode = True
        self.db.commit()

        composite_claim = claim_next_scan_item(self.db)
        fail_claim(
            self.db,
            composite_claim,
            "catalogue down",
            transient=True,
            retry_reason=CATALOGUE_UNREACHABLE_RETRY_REASON,
        )
        self.db.expire_all()
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual([item.catalogue_failures for item in items], [1, 1])
        for item in items:
            item.next_attempt_at = datetime.datetime.utcnow()
        self.db.commit()

        composite_claim = claim_next_scan_item(self.db)
        self.assertTrue(complete_claim_group(
            self.db,
            composite_claim,
            [{"recognized": {"name": "A"}, "matches": [{"id": "card-A"}]}, None],
        ))
        self.db.expire_all()
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        self.assertEqual(items[1].status, "pending")
        self.assertEqual(items[1].catalogue_failures, 0)

    def test_a_fresh_install_and_an_upgraded_one_agree_on_the_new_column(self):
        # create_all runs before the ALTER migrations, so the ALTER is a no-op
        # on a fresh install. An ORM-side default alone would leave the fresh
        # schema without the DEFAULT the upgraded schema gets, and a statement
        # that does not name the column would fail on one and not the other.
        job = self._job(self.users[0])
        self.db.execute(
            text(
                "INSERT INTO scan_job_items "
                "(job_id, user_id, position, image_path, content_type, byte_size, "
                " batch_mode, status, resolved, attempts, transient_failures, created_at, updated_at) "
                "VALUES (:job, :user, 500, 'x.jpg', 'image/jpeg', 4, 0, 'pending', 0, 0, 0, :now, :now)"
            ),
            {"job": job.id, "user": self.users[0].id, "now": datetime.datetime.utcnow()},
        )
        self.db.commit()
        stored = self.db.execute(
            text("SELECT catalogue_failures FROM scan_job_items WHERE position = 500")
        ).scalar()
        self.assertEqual(stored, 0)

    def test_stale_lease_cannot_complete_an_item(self):
        self._job(self.users[0])
        claim = claim_next_scan_item(self.db)

        self.assertFalse(
            complete_claim(
                self.db,
                ClaimedScanItem(item_id=claim.item_id, lease_token="wrong"),
                {"recognized": {}, "matches": []},
            )
        )
        self.assertTrue(complete_claim(self.db, claim, {"recognized": {}, "matches": []}))

    def test_expired_processing_lease_is_recovered(self):
        self._job(self.users[0])
        claim = claim_next_scan_item(self.db)
        item = self.db.get(ScanJobItem, claim.item_id)
        item.lease_expires_at = datetime.datetime.utcnow() - datetime.timedelta(seconds=1)
        self.db.commit()

        self.assertEqual(recover_expired_leases(self.db), 1)
        self.db.refresh(item)
        self.assertEqual(item.status, "retrying")
        self.assertIsNone(item.lease_token)

    def test_transient_failure_does_not_consume_recognition_attempts(self):
        self._job(self.users[0])
        claim = claim_next_scan_item(self.db)

        fail_claim(self.db, claim, "429", transient=True)
        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.status, "retrying")
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.transient_failures, 1)

    def test_an_unreachable_catalogue_stops_retrying_after_a_few_attempts(self):
        # Every retry of this failure re-runs the vision extraction before it
        # can reach the catalogue at all, so an outage lasting days would
        # charge for the same photo dozens of times and never succeed. The item
        # fails with the reason still on it, and the user's retry resets it.
        job = self._job(self.users[0])
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir()
        (job_dir / "0.jpg").write_bytes(b"jpeg")
        covered = datetime.timedelta()
        for attempt in range(1, MAX_CATALOGUE_UNREACHABLE_ATTEMPTS + 1):
            claim = claim_next_scan_item(self.db)
            self.assertIsNotNone(claim, f"attempt {attempt} should have been claimable")
            before = datetime.datetime.utcnow()
            fail_claim(
                self.db,
                claim,
                "catalogue down",
                transient=True,
                retry_reason=CATALOGUE_UNREACHABLE_RETRY_REASON,
            )
            item = self.db.get(ScanJobItem, claim.item_id)
            last = attempt == MAX_CATALOGUE_UNREACHABLE_ATTEMPTS
            self.assertEqual(item.status, "failed" if last else "retrying", f"attempt {attempt}")
            if not last:
                covered += item.next_attempt_at - before
                item.next_attempt_at = datetime.datetime.utcnow()
                self.db.commit()

        # The window the attempts actually cover, which is the number that
        # decides whether a brief outage survives. Asserted rather than
        # described, because it is the backoff schedule that sets it and a
        # change there would otherwise silently shrink it.
        self.assertGreaterEqual(covered, datetime.timedelta(minutes=12))
        self.assertIsNone(item.next_attempt_at)
        # The review screen shows item.error. Once there is no next attempt it
        # must stop saying one is coming, which is what the message said while
        # the retries were still running.
        self.assertNotIn("automatically", item.error)
        self.assertIn("catalogue", item.error.lower())
        self.assertIn("try it again", item.error.lower())
        self.assertEqual(item.retry_reason, CATALOGUE_UNREACHABLE_RETRY_REASON)
        self.assertEqual(item.attempts, 0, "a catalogue outage is not a recognition failure")

        # The bystander that matters: the user can still retry it themselves
        # once the catalogue is back, and that clears the count.
        retry_scan_item(self.db, item)
        self.assertEqual(item.status, "pending")
        self.assertEqual(item.transient_failures, 0)
        self.assertEqual(item.catalogue_failures, 0, "a user retry must restore the full allowance")

    def test_unrelated_transient_failures_do_not_spend_the_catalogue_budget(self):
        # The two counts are separate on purpose. An item that has already been
        # throttled by the provider a few times has met no outage at all, and
        # must still get its full allowance when it meets one.
        self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        item.transient_failures = MAX_CATALOGUE_UNREACHABLE_ATTEMPTS + 2
        self.db.commit()
        claim = claim_next_scan_item(self.db)

        fail_claim(
            self.db,
            claim,
            "catalogue down",
            transient=True,
            retry_reason=CATALOGUE_UNREACHABLE_RETRY_REASON,
        )

        self.db.refresh(item)
        self.assertEqual(item.status, "retrying")
        self.assertEqual(item.catalogue_failures, 1)

    def test_other_transient_failures_are_still_retried_without_a_bound(self):
        # The bound is for the catalogue reason alone. A provider rate limit
        # must keep its existing unbounded backoff, because waiting genuinely
        # does fix it and a retry costs nothing until the limit clears.
        self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        item.transient_failures = MAX_CATALOGUE_UNREACHABLE_ATTEMPTS + 5
        self.db.commit()
        claim = claim_next_scan_item(self.db)

        fail_claim(self.db, claim, "429", transient=True, retry_reason="daily_quota")

        self.db.refresh(item)
        self.assertEqual(item.status, "retrying")
        self.assertIsNotNone(item.next_attempt_at)
        self.assertEqual(item.catalogue_failures, 0, "only the catalogue reason spends it")

    def test_provider_retry_delay_and_reason_are_persisted(self):
        self._job(self.users[0])
        claim = claim_next_scan_item(self.db)
        before = datetime.datetime.utcnow()

        fail_claim(
            self.db,
            claim,
            "daily quota",
            transient=True,
            retry_after_seconds=3600,
            retry_reason="daily_quota",
        )

        item = self.db.get(ScanJobItem, claim.item_id)
        self.assertEqual(item.retry_reason, "daily_quota")
        self.assertGreaterEqual(
            item.next_attempt_at,
            before + datetime.timedelta(seconds=3599),
        )

    def test_provider_retry_delay_overrides_generic_backoff_exactly(self):
        self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        item.transient_failures = 4
        self.db.commit()
        claim = claim_next_scan_item(self.db)
        before = datetime.datetime.utcnow()

        fail_claim(
            self.db,
            claim,
            "daily quota",
            transient=True,
            retry_after_seconds=21,
            retry_reason="daily_quota",
        )

        self.db.refresh(item)
        scheduled_delay = (item.next_attempt_at - before).total_seconds()
        self.assertGreaterEqual(scheduled_delay, 20.9)
        self.assertLess(scheduled_delay, 22)

    def test_recognition_failure_stops_after_three_attempts(self):
        job = self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir()
        image = job_dir / "0.jpg"
        image.write_bytes(b"jpeg")
        for expected in (1, 2, 3):
            item.status = "pending"
            item.next_attempt_at = datetime.datetime.utcnow()
            self.db.commit()
            claim = claim_next_scan_item(self.db)
            fail_claim(self.db, claim, "unreadable", transient=False)
            item = self.db.get(ScanJobItem, item.id)
            self.assertEqual(item.attempts, expected)
        self.assertEqual(item.status, "failed")
        self.assertTrue(image.exists())
        self.assertEqual(item.image_path, f"{job.id}/0.jpg")

    def test_failed_item_can_be_retried_as_a_fresh_recognition_cycle(self):
        job = self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir()
        (job_dir / "0.jpg").write_bytes(b"jpeg")
        item.status = "failed"
        item.attempts = 3
        item.transient_failures = 2
        item.error = "unreadable"
        item.recognized = {"name": "Wrong"}
        item.matches = []
        job.status = "failed"
        job.finished_at = datetime.datetime.utcnow()
        self.db.commit()

        retry_scan_item(self.db, item)

        self.assertEqual(item.status, "pending")
        self.assertEqual(item.attempts, 0)
        self.assertEqual(item.transient_failures, 0)
        self.assertIsNone(item.error)
        self.assertIsNone(item.recognized)
        self.assertIsNone(item.matches)
        self.assertFalse(item.batch_mode)
        self.assertEqual(job.status, "pending")
        self.assertIsNone(job.finished_at)

    def test_retake_commit_failure_preserves_the_old_photo_and_path(self):
        from PIL import Image

        job = self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        old_file = scan_storage.resolve_scan_path(item.image_path)
        old_file.parent.mkdir(exist_ok=True)
        old_file.write_bytes(b"old photo")
        item.status = "done"
        self.db.commit()

        output = io.BytesIO()
        Image.new("RGB", (400, 560), "#385898").save(output, format="JPEG")
        with patch.object(self.db, "commit", side_effect=RuntimeError("commit failed")):
            with self.assertRaisesRegex(RuntimeError, "commit failed"):
                replace_scan_item_photo(self.db, item, output.getvalue())

        self.db.expire_all()
        persisted = self.db.get(ScanJobItem, item.id)
        self.assertEqual(persisted.image_path, f"{job.id}/0.jpg")
        self.assertTrue(old_file.is_file())
        self.assertEqual(old_file.read_bytes(), b"old photo")
        # The other half of the claim: the replacement that no row now
        # references must not be left behind either. Counted rather than named,
        # because the new path is a uuid generated inside the call. Without
        # this, deleting the orphan cleanup left the suite green.
        self.assertEqual(
            sorted(entry.name for entry in old_file.parent.iterdir()),
            [old_file.name],
        )

    def test_retake_recomputes_recent_duplicate_link(self):
        from PIL import Image

        job = self._job(self.users[0], positions=(0, 1))
        first, second = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        first.status = second.status = "done"
        first_path = scan_storage.resolve_scan_path(first.image_path)
        second_path = scan_storage.resolve_scan_path(second.image_path)
        first_path.parent.mkdir(exist_ok=True)
        first_path.write_bytes(b"old")
        second_path.write_bytes(b"other")
        output = io.BytesIO()
        Image.new("RGB", (400, 560), "#385898").save(output, format="JPEG")
        sanitized = scan_storage.sanitize_image_bytes(output.getvalue())
        first.image_hash = hashlib.sha256(sanitized.data).hexdigest()
        first.image_stored_at = datetime.datetime.utcnow()
        self.db.commit()

        replace_scan_item_photo(self.db, second, output.getvalue())

        self.assertEqual(second.duplicate_of_item_id, first.id)
        self.assertIsNotNone(second.image_stored_at)

    def test_retake_clears_a_previous_duplicate_link_when_photo_changes(self):
        from PIL import Image

        job = self._job(self.users[0], positions=(0, 1))
        first, second = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        first.status = second.status = "done"
        for item in (first, second):
            path = scan_storage.resolve_scan_path(item.image_path)
            path.parent.mkdir(exist_ok=True)
            path.write_bytes(b"old")
        second.duplicate_of_item_id = first.id
        self.db.commit()

        output = io.BytesIO()
        Image.new("RGB", (400, 560), "#743c98").save(output, format="JPEG")
        replace_scan_item_photo(self.db, second, output.getvalue())

        self.assertIsNone(second.duplicate_of_item_id)

    def test_retake_persists_the_profile_that_sanitised_the_replacement(self):
        from PIL import Image

        job = self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        old_file = scan_storage.resolve_scan_path(item.image_path)
        old_file.parent.mkdir(exist_ok=True)
        old_file.write_bytes(b"old photo")
        item.status = "done"
        item.upload_resolution_profile = "low"
        self.db.commit()

        output = io.BytesIO()
        Image.new("RGB", (400, 560), "#385898").save(output, format="JPEG")
        with patch("services.scan_providers.high_resolution_samples_enabled", return_value=True):
            replace_scan_item_photo(self.db, item, output.getvalue())

        self.assertEqual(item.upload_resolution_profile, "high")

    def test_progress_counts_only_reviewable_items_as_attention(self):
        job = self._job(self.users[0], positions=(0, 1, 2, 3))
        items = self.db.query(ScanJobItem).order_by(ScanJobItem.position).all()
        items[0].status = "done"
        items[1].status = "failed"
        items[2].status = "retrying"
        retry_at = datetime.datetime.utcnow() + datetime.timedelta(minutes=30)
        items[2].next_attempt_at = retry_at
        items[2].retry_reason = "daily_quota"
        items[3].status = "done"
        items[3].resolved = True
        self.db.commit()

        progress = job_progress(self.db, job)

        self.assertEqual(progress["processed"], 3)
        self.assertEqual(progress["active"], 1)
        self.assertEqual(progress["attention"], 2)
        self.assertEqual(progress["failed_attention"], 1)
        self.assertEqual(progress["unresolved"], 2)
        self.assertEqual(progress["next_retry_at"], retry_at.isoformat())
        self.assertEqual(progress["retry_reason"], "daily_quota")

    def test_resolve_removes_the_review_photo_immediately(self):
        job = self._job(self.users[0])
        item = self.db.query(ScanJobItem).one()
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir()
        image = job_dir / "0.jpg"
        image.write_bytes(b"jpeg")
        item.image_path = f"{job.id}/0.jpg"
        item.status = "done"
        self.db.commit()

        resolve_scan_item(self.db, item)

        self.assertFalse(image.exists())
        self.assertTrue(item.resolved)
        self.assertIsNone(item.image_path)

    def test_expiry_removes_active_jobs_and_every_photo_after_fourteen_days(self):
        old = datetime.datetime.utcnow() - datetime.timedelta(days=15)
        job = self._job(self.users[0], created_at=old, expires_at=old + datetime.timedelta(days=14))
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir()
        (job_dir / "0.jpg").write_bytes(b"jpeg")

        self.assertEqual(purge_expired_scan_jobs(self.db), 1)
        self.assertIsNone(self.db.get(ScanJob, job.id))
        self.assertFalse(job_dir.exists())


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class ScanQueueDrainTests(unittest.IsolatedAsyncioTestCase):
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
        db = self.Session()
        user = User(username="drain-user", hashed_password="x")
        db.add(user)
        db.commit()
        job = ScanJob(
            user_id=user.id,
            status="pending",
            created_at=datetime.datetime.utcnow(),
            updated_at=datetime.datetime.utcnow(),
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=14),
        )
        db.add(job)
        db.flush()
        db.add(ScanQueueUserState(user_id=user.id))
        job_dir = scan_storage.scan_upload_root() / str(job.id)
        job_dir.mkdir()
        (job_dir / "scan.jpg").write_bytes(b"safe-jpeg")
        db.add(
            ScanJobItem(
                job_id=job.id,
                user_id=user.id,
                position=0,
                image_path=f"{job.id}/scan.jpg",
                content_type="image/jpeg",
                byte_size=9,
                status="pending",
                resolved=False,
                attempts=0,
                transient_failures=0,
                next_attempt_at=datetime.datetime.utcnow(),
                created_at=datetime.datetime.utcnow(),
                updated_at=datetime.datetime.utcnow(),
            )
        )
        db.commit()
        self.item_id = db.query(ScanJobItem.id).scalar()
        db.close()

    def tearDown(self):
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    async def test_drain_uses_processor_and_persists_result(self):
        async def processor(db, user_id, image_bytes, content_type):
            self.assertEqual(image_bytes, b"safe-jpeg")
            return {"recognized": {"name": "Snorlax"}, "matches": [{"id": "card-63"}]}

        with patch("database.SessionLocal", self.Session):
            processed = await scan_queue.drain_scan_queue(max_items=1, processor=processor)

        db = self.Session()
        try:
            item = db.get(ScanJobItem, self.item_id)
            self.assertEqual(processed, 1)
            self.assertEqual(item.status, "done")
            self.assertEqual(item.recognized["name"], "Snorlax")
        finally:
            db.close()


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CompositeProcessorTests(unittest.IsolatedAsyncioTestCase):
    async def test_only_confident_metadata_matches_are_accepted_from_composite(self):
        from PIL import Image
        import io

        def image_bytes(color):
            output = io.BytesIO()
            Image.new("RGB", (100, 140), color).save(output, format="JPEG")
            return output.getvalue()

        db = MagicMock()
        db.get.return_value = User(id=1, username="composite-owner", hashed_password="x", is_active=True)
        composite_info = {
            0: {"name": "Pikachu", "number_local": "25", "language": "en"},
            1: {"name": None, "number_local": "4", "language": "en"},
            2: {"name": "Eevee", "number_local": "133", "language": "en"},
            3: {"name": "Jigglypuff", "artist": "Kagemaru Himeno", "hp": "60", "language": "ja"},
        }
        matched = [
            {
                "recognized": composite_info[0],
                "matches": [{"id": "card-25"}],
                "_number_match_count": 1,
                "_identity_confident": True,
                "_identity_decision": "number_unique",
                "_identity_suggested_match_id": "card-25",
            },
            {
                "recognized": composite_info[2],
                "matches": [{"id": "wrong-number"}],
                "_number_match_count": 0,
                "_identity_confident": False,
            },
            {
                "recognized": composite_info[3],
                "matches": [{"id": "card-jigglypuff"}],
                "_number_match_count": 0,
                "_identity_confident": True,
            },
        ]

        matcher = AsyncMock(side_effect=matched)
        source_images = [
            image_bytes("red"),
            image_bytes("blue"),
            image_bytes("green"),
            image_bytes("yellow"),
        ]
        with patch("api.recognize.get_gemini_key", return_value="secret-key"), \
                patch("api.recognize.recognize_composite_card_info", new=AsyncMock(return_value=composite_info)), \
                patch("api.recognize.match_composite_card_info", new=matcher):
            results = await scan_queue.default_composite_processor(
                db,
                1,
                source_images,
                ["image/jpeg"] * 4,
            )

        self.assertEqual(results[0]["matches"][0]["id"], "card-25")
        # The verdict must survive the processor intact: complete_claim_group
        # persists it straight off this dict.
        self.assertEqual(results[0]["_identity_decision"], "number_unique")
        self.assertEqual(results[0]["_identity_suggested_match_id"], "card-25")
        self.assertEqual(results[1:3], [None, None])
        self.assertEqual(results[3]["matches"][0]["id"], "card-jigglypuff")
        self.assertEqual(matcher.await_count, 3)
        self.assertEqual(
            [call.kwargs["photo_bytes"] for call in matcher.await_args_list],
            [source_images[0], source_images[2], source_images[3]],
        )


if __name__ == "__main__":
    unittest.main()
