import datetime
import io
import os
import tempfile
import random
import unittest
from unittest.mock import patch

try:
    from fastapi import UploadFile
    from PIL import Image
    from sqlalchemy import create_engine
    from sqlalchemy.orm import sessionmaker
    from sqlalchemy.pool import StaticPool
    from starlette.datastructures import Headers

    from database import Base
    from models import ScanJobItem, User
    from services import scan_storage
    from services.scan_storage import ScanUploadError, create_scan_job, sanitize_image_bytes

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _image_bytes(image_format="JPEG", *, size=(800, 1100), exif=False):
    image = Image.new("RGB", size, "#d92828")
    output = io.BytesIO()
    kwargs = {}
    if exif:
        metadata = Image.Exif()
        metadata[274] = 6
        metadata[270] = "private scan metadata"
        kwargs["exif"] = metadata
    image.save(output, format=image_format, **kwargs)
    return output.getvalue()


def _upload(data, content_type="image/jpeg"):
    return UploadFile(
        file=io.BytesIO(data),
        filename="private-original-name.jpg",
        headers=Headers({"content-type": content_type}),
    )


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner image dependencies are not installed")
class ScanSanitizationTests(unittest.TestCase):
    def test_reencodes_orients_resizes_and_strips_metadata(self):
        sanitized = sanitize_image_bytes(_image_bytes(size=(3000, 4000), exif=True))

        with Image.open(io.BytesIO(sanitized.data)) as result:
            self.assertEqual(result.format, "JPEG")
            self.assertLessEqual(max(result.size), 2048)
            self.assertFalse(result.getexif())
        self.assertEqual(sanitized.content_type, "image/jpeg")

    def test_high_resolution_setting_changes_only_the_upload_edge_cap(self):
        raw = _image_bytes(size=(6000, 4000))

        low_resolution = sanitize_image_bytes(raw, high_resolution=False)
        high_resolution = sanitize_image_bytes(raw, high_resolution=True)

        with Image.open(io.BytesIO(low_resolution.data)) as result:
            self.assertEqual(max(result.size), 2048)
        with Image.open(io.BytesIO(high_resolution.data)) as result:
            self.assertEqual(max(result.size), 4096)

    def test_accepts_png_and_webp_but_always_outputs_safe_jpeg(self):
        for image_format in ("PNG", "WEBP"):
            with self.subTest(image_format=image_format):
                sanitized = sanitize_image_bytes(_image_bytes(image_format))
                with Image.open(io.BytesIO(sanitized.data)) as result:
                    self.assertEqual(result.format, "JPEG")

    def test_rejects_non_images_and_unsupported_formats(self):
        with self.assertRaises(ScanUploadError):
            sanitize_image_bytes(b"not an image")

        animated = io.BytesIO()
        frames = [Image.new("RGB", (8, 8), color) for color in ("red", "blue")]
        frames[0].save(animated, format="GIF", save_all=True, append_images=frames[1:])
        with self.assertRaises(ScanUploadError):
            sanitize_image_bytes(animated.getvalue())

    @unittest.skipUnless(getattr(scan_storage, "HEIC_AVAILABLE", False), "pillow-heif is unavailable")
    def test_accepts_heic_and_normalizes_it_to_jpeg(self):
        raw = _image_bytes("HEIF")
        sanitized = sanitize_image_bytes(raw)
        with Image.open(io.BytesIO(sanitized.data)) as result:
            self.assertEqual(result.format, "JPEG")


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner image dependencies are not installed")
class ScanJobStorageTests(unittest.IsolatedAsyncioTestCase):
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
        self.user = User(username="scan-owner", hashed_password="x")
        self.db.add(self.user)
        self.db.commit()

    def tearDown(self):
        self.db.close()
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    async def test_job_stores_only_sanitized_randomly_named_files(self):
        before = datetime.datetime.utcnow()
        job = await create_scan_job(self.db, self.user.id, [_upload(_image_bytes())])
        item = self.db.query(ScanJobItem).filter(ScanJobItem.job_id == job.id).one()

        self.assertNotIn("private-original-name", item.image_path)
        stored = scan_storage.resolve_scan_path(item.image_path)
        self.assertTrue(stored.is_file())
        self.assertEqual(stored.read_bytes()[:2], b"\xff\xd8")
        self.assertEqual(item.content_type, "image/jpeg")
        self.assertGreaterEqual(job.expires_at, before + datetime.timedelta(days=13, hours=23))

    async def test_rejects_more_than_fifty_photos_before_writing(self):
        uploads = [_upload(_image_bytes(size=(8, 8))) for _ in range(51)]
        with self.assertRaises(ScanUploadError):
            await create_scan_job(self.db, self.user.id, uploads)
        self.assertEqual(list(scan_storage.scan_upload_root().iterdir()), [])

    async def test_rejects_per_file_and_total_limits_and_cleans_partial_job(self):
        small = _image_bytes(size=(8, 8))
        with patch.object(scan_storage, "MAX_FILE_BYTES", len(small) - 1):
            with self.assertRaises(ScanUploadError):
                await create_scan_job(self.db, self.user.id, [_upload(small)])

        with patch.object(scan_storage, "MAX_JOB_BYTES", len(small) + 10):
            with self.assertRaises(ScanUploadError):
                await create_scan_job(
                    self.db,
                    self.user.id,
                    [_upload(small), _upload(small)],
                )
        self.assertEqual(list(scan_storage.scan_upload_root().iterdir()), [])

    async def test_a_large_upload_that_shrinks_when_stored_is_accepted(self):
        # The ordinary case, and the one the first fix got wrong. Photographs
        # usually get SMALLER when sanitised. Charging the raw upload against
        # the remaining stored budget refused a big photograph that would have
        # left plenty of room once encoded.
        small = _image_bytes(size=(8, 8))
        real_sanitize = scan_storage.sanitize_image_bytes

        def shrink(raw, *, high_resolution=False):
            sanitized = real_sanitize(raw, high_resolution=high_resolution)
            return sanitized.__class__(data=b"\xff\xd8\x00", content_type=sanitized.content_type)

        # A budget smaller than the upload, but larger than what it stores.
        with patch.object(scan_storage, "MAX_JOB_BYTES", len(small) - 1), \
                patch.object(scan_storage, "sanitize_image_bytes", shrink):
            job = await create_scan_job(self.db, self.user.id, [_upload(small)])

        self.assertIsNotNone(job)
        self.assertEqual(
            self.db.query(ScanJobItem).filter(ScanJobItem.job_id == job.id).count(), 1
        )

    async def test_the_budget_is_exact_at_its_limit_and_one_byte_over(self):
        small = _image_bytes(size=(8, 8))
        real_sanitize = scan_storage.sanitize_image_bytes
        stored_size = 64

        def fixed(raw, *, high_resolution=False):
            sanitized = real_sanitize(raw, high_resolution=high_resolution)
            return sanitized.__class__(
                data=b"\xff\xd8" + b"\x00" * (stored_size - 2),
                content_type=sanitized.content_type,
            )

        with patch.object(scan_storage, "sanitize_image_bytes", fixed):
            # Exactly at the limit is allowed.
            with patch.object(scan_storage, "MAX_JOB_BYTES", stored_size * 2):
                job = await create_scan_job(
                    self.db, self.user.id, [_upload(small), _upload(small)],
                )
                self.assertIsNotNone(job)

            # One byte less of budget is not.
            with patch.object(scan_storage, "MAX_JOB_BYTES", stored_size * 2 - 1):
                with self.assertRaises(ScanUploadError):
                    await create_scan_job(
                        self.db, self.user.id, [_upload(small), _upload(small)],
                    )

    async def test_the_job_budget_counts_stored_bytes_not_uploaded_ones(self):
        # Sanitising re-encodes, and the result can be far larger than its
        # input. Charging the upload let a job pass its limit on arrival and
        # then write many times that to disk. The budget is a disk budget.
        small = _image_bytes(size=(8, 8))
        big_when_stored = len(small) * 20

        def inflate(raw, *, high_resolution=False):
            sanitized = real_sanitize(raw, high_resolution=high_resolution)
            return sanitized.__class__(
                data=b"\xff\xd8" + b"\x00" * big_when_stored,
                content_type=sanitized.content_type,
            )

        real_sanitize = scan_storage.sanitize_image_bytes
        with patch.object(scan_storage, "MAX_JOB_BYTES", big_when_stored + 8), \
                patch.object(scan_storage, "sanitize_image_bytes", inflate):
            with self.assertRaises(ScanUploadError):
                await create_scan_job(
                    self.db, self.user.id, [_upload(small), _upload(small)],
                )

        # Nothing is left behind, including the oversized file that tipped it.
        self.assertEqual(list(scan_storage.scan_upload_root().iterdir()), [])


@unittest.skipUnless(DEPS_AVAILABLE, "Scan storage dependencies are not installed")
class StoredBytesBudgetTests(unittest.TestCase):
    """The job budget has to count what lands on disk, not what arrived."""

    def test_re_encoding_can_exceed_its_input_by_an_order_of_magnitude(self):
        # The premise of the budget defect, asserted rather than assumed: a
        # compact PNG of noise re-encodes to a far larger JPEG, so charging the
        # upload would let a job write many times the bytes it was allowed.
        import io
        from PIL import Image
        from services.scan_storage import sanitize_image_bytes

        rng = random.Random(11)
        image = Image.new("RGB", (4096, 4096))
        image.putdata([
            (rng.randint(0, 255), rng.randint(0, 255), rng.randint(0, 255))
            for _ in range(64 * 64)
        ] * (4096 * 4096 // (64 * 64)))
        source = io.BytesIO()
        image.save(source, format="PNG", optimize=True)
        raw = source.getvalue()

        stored = sanitize_image_bytes(raw, high_resolution=True)

        self.assertGreater(
            len(stored.data),
            len(raw),
            "this fixture no longer demonstrates re-encoding growth",
        )


if __name__ == "__main__":
    unittest.main()
