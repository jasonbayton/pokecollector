"""Regression coverage for atomic filing of confident scan results."""

import datetime
import os
import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

try:
    from fastapi import HTTPException
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool

    from database import Base
    from models import Card, CollectionItem, ScanJob, ScanJobItem, User
    from services import scan_bulk_add

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner bulk-add dependencies are not installed")
class ScanBulkAddTests(unittest.TestCase):
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
        self.user = User(username="confident-bulk-owner", hashed_password="x", is_active=True)
        self.db.add(self.user)
        self.db.commit()
        self.job = ScanJob(
            user_id=self.user.id,
            expires_at=datetime.datetime.utcnow() + datetime.timedelta(days=1),
        )
        self.db.add(self.job)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    def _card(self, card_id, **overrides):
        values = {
            "id": card_id,
            "tcg_card_id": card_id.rsplit("_", 1)[0],
            "name": card_id,
            "lang": "en",
            "variants_normal": True,
        }
        values.update(overrides)
        card = Card(**values)
        self.db.add(card)
        self.db.commit()
        return card

    def _item(self, position, *, card_id="card-1_en", status="done", confident=True,
              suggested=None, resolved=False, image=True, matches=None, recognized=None):
        suggested = card_id if suggested is None else suggested
        relative_path = f"{self.job.id}/bulk-add-{position}.jpg" if image else None
        if relative_path:
            path = Path(self.temp_dir.name) / relative_path
            path.parent.mkdir(parents=True, exist_ok=True)
            path.write_bytes(b"scan")
        item = ScanJobItem(
            job_id=self.job.id,
            user_id=self.user.id,
            position=position,
            image_path=relative_path,
            byte_size=4,
            status=status,
            resolved=resolved,
            matches=matches if matches is not None else [{"id": card_id}],
            recognized=recognized,
            identity_confident=confident,
            suggested_match_id=suggested,
        )
        self.db.add(item)
        self.db.commit()
        return item, Path(self.temp_dir.name) / relative_path if relative_path else None

    def _file(self, cards):
        return scan_bulk_add.add_all_confident_scan_items(
            self.db,
            job_id=self.job.id,
            current_user=self.user,
            prepared_card_ids=set(cards),
        )

    def test_files_every_eligible_item_once_and_preserves_bystanders_and_images(self):
        first = self._card("card-1_en")
        second = self._card("card-2_en")
        eligible_one, first_image = self._item(0, card_id=first.id)
        eligible_two, second_image = self._item(1, card_id=second.id)
        unconfident, unconfident_image = self._item(2, card_id=first.id, confident=False)
        failed, failed_image = self._item(3, card_id=second.id, status="failed")
        legacy, legacy_image = self._item(4, card_id=first.id, confident=None, suggested=None)

        self.assertEqual(self._file({first.id, second.id}), 2)
        # The repeat is the sequential control for the PostgreSQL concurrency
        # test: already-resolved rows are not eligible for a second copy.
        self.assertEqual(self._file({first.id, second.id}), 0)

        rows = self.db.query(CollectionItem).order_by(CollectionItem.card_id).all()
        self.assertEqual([(row.card_id, row.quantity, row.condition, row.variant) for row in rows], [
            (first.id, 1, "Mint", "Normal"),
            (second.id, 1, "Mint", "Normal"),
        ])
        self.assertTrue(self.db.get(ScanJobItem, eligible_one.id).resolved)
        self.assertTrue(self.db.get(ScanJobItem, eligible_two.id).resolved)
        for bystander in (unconfident, failed, legacy):
            self.assertFalse(self.db.get(ScanJobItem, bystander.id).resolved)
        self.assertFalse(first_image.exists())
        self.assertFalse(second_image.exists())
        # Bystanders that must survive: the unsure, failed, and legacy rows
        # stay both unresolved and reviewable with their original photos.
        self.assertTrue(unconfident_image.exists())
        self.assertTrue(failed_image.exists())
        self.assertTrue(legacy_image.exists())

    def test_rejects_a_suggestion_not_present_in_stored_matches(self):
        self._card("card-1_en")
        stale, stale_image = self._item(
            0,
            suggested="different-card_en",
            matches=[{"id": "card-1_en"}],
        )

        with self.assertRaises(HTTPException) as raised:
            self._file(set())

        self.assertEqual(raised.exception.status_code, 422)
        self.assertFalse(self.db.get(ScanJobItem, stale.id).resolved)
        self.assertEqual(self.db.query(CollectionItem).count(), 0)
        self.assertTrue(stale_image.exists())

    def test_already_owned_suggestion_stays_for_explicit_review(self):
        card = self._card("card-1_en")
        item, image = self._item(0, card_id=card.id)
        self.db.add(CollectionItem(
            card_id=card.id,
            user_id=self.user.id,
            quantity=2,
            condition="Mint",
            variant="Normal",
            lang="en",
        ))
        self.db.commit()

        self.assertEqual(self._file({card.id}), 0)
        self.assertEqual(self.db.query(CollectionItem).one().quantity, 2)
        self.assertFalse(self.db.get(ScanJobItem, item.id).resolved)
        self.assertTrue(image.exists())

    def test_uses_an_available_print_when_normal_does_not_exist(self):
        holo_only = self._card("holo-only_en", variants_normal=False, variants_holo=True)
        self._item(0, card_id=holo_only.id)

        self.assertEqual(self._file({holo_only.id}), 1)

        row = self.db.query(CollectionItem).one()
        self.assertEqual(row.variant, "Holo")
        self.assertEqual(row.condition, "Mint")

    def test_files_a_recognized_reverse_holo_when_the_card_offers_it(self):
        card = self._card(
            "normal-and-reverse_en",
            variants_normal=True,
            variants_reverse=True,
        )
        self._item(
            0,
            card_id=card.id,
            recognized={"finish": "foil across the border and card face; artwork is matte"},
        )

        scan_bulk_add._add_collection_copy(
            self.db,
            card=card,
            current_user=self.user,
            recognized_finish="foil across the border and card face; artwork is matte",
        )
        self.db.commit()

        self.assertEqual(self.db.query(CollectionItem).one().variant, "Reverse Holo")

    def test_ignores_a_recognized_finish_the_card_does_not_offer(self):
        card = self._card(
            "reverse-only_en",
            variants_normal=False,
            variants_reverse=True,
            variants_holo=False,
        )

        self.assertEqual(
            scan_bulk_add.variant_for_recognized_finish(
                card,
                "foil in the artwork panel",
            ),
            "Reverse Holo",
        )

    def test_keeps_the_default_variant_when_no_finish_was_read(self):
        card = self._card(
            "normal-and-reverse_en",
            variants_normal=True,
            variants_reverse=True,
        )

        self.assertEqual(scan_bulk_add.variant_for_recognized_finish(card, None), "Normal")
        self.assertEqual(
            scan_bulk_add.variant_for_recognized_finish(card, "unfamiliar metallic pattern"),
            "Normal",
        )
        self.assertEqual(
            scan_bulk_add.variant_for_recognized_finish(card, "first edition"),
            "Normal",
        )

    def test_a_read_finish_is_filed_even_when_it_differs_from_the_default(self):
        # The ordinary case this exists for: a reverse holo pulled from a pack,
        # on a card that also has a normal printing. The default is not a
        # competing observation, it is what is assumed when nothing has been
        # read, so escalating here would halt on exactly the cards the feature
        # is meant to get right.
        card = self._card("card-1_en", variants_normal=True, variants_reverse=True)
        self._item(0, card_id=card.id, recognized={"finish": "face_foil"})

        self.assertEqual(self._file({card.id}), 1)
        row = self.db.query(CollectionItem).filter(
            CollectionItem.card_id == card.id
        ).one()
        self.assertEqual(row.variant, "Reverse Holo")

    def test_a_mid_transaction_failure_rolls_back_cards_and_resolutions(self):
        first = self._card("card-1_en")
        second = self._card("card-2_en")
        bystander_card = self._card("card-3_en")
        first_item, first_image = self._item(0, card_id=first.id)
        second_item, second_image = self._item(1, card_id=second.id)
        existing = CollectionItem(
            card_id=bystander_card.id,
            user_id=self.user.id,
            quantity=7,
            condition="Mint",
            variant="Normal",
            lang="en",
        )
        self.db.add(existing)
        self.db.commit()

        original = scan_bulk_add._add_collection_copy
        calls = 0

        def fail_after_second_copy(*args, **kwargs):
            nonlocal calls
            calls += 1
            original(*args, **kwargs)
            if calls == 2:
                raise RuntimeError("test-only collection failure")

        with patch("services.scan_bulk_add._add_collection_copy", side_effect=fail_after_second_copy):
            with self.assertRaisesRegex(RuntimeError, "test-only collection failure"):
                self._file({first.id, second.id})

        self.db.expire_all()
        restored = self.db.get(CollectionItem, existing.id)
        self.assertEqual(restored.quantity, 7)
        self.assertEqual(self.db.query(CollectionItem).count(), 1)
        self.assertFalse(self.db.get(ScanJobItem, first_item.id).resolved)
        self.assertFalse(self.db.get(ScanJobItem, second_item.id).resolved)
        # The pre-existing collection row is the rollback bystander; both scan
        # photos are additional bystanders and must survive a failed transaction.
        self.assertTrue(first_image.exists())
        self.assertTrue(second_image.exists())


if __name__ == "__main__":
    unittest.main()
