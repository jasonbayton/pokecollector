import datetime
import io
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

    from api.auth import get_current_user
    from api.products import router as products_router
    from api.scan_jobs import router as scan_jobs_router
    from database import Base, get_db
    from models import ProductPurchase, ScanJob, User

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


def _jpeg_bytes():
    image = Image.new("RGB", (80, 112), "#d92828")
    output = io.BytesIO()
    image.save(output, format="JPEG")
    return output.getvalue()


@unittest.skipUnless(DEPS_AVAILABLE, "Product scan dependencies are not installed")
class ProductScanSessionApiTests(unittest.TestCase):
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
        self.user = User(username="product-scan-owner", hashed_password="x", is_active=True)
        self.other_user = User(username="product-scan-other", hashed_password="x", is_active=True)
        self.db.add_all([self.user, self.other_user])
        self.db.commit()
        self.product = ProductPurchase(
            user_id=self.user.id,
            product_name="Session box",
            purchase_price=25,
            purchase_date=datetime.date.today(),
            lifecycle_status="sealed",
        )
        self.other_product = ProductPurchase(
            user_id=self.other_user.id,
            product_name="Other box",
            purchase_price=25,
            purchase_date=datetime.date.today(),
            lifecycle_status="sealed",
        )
        self.db.add_all([self.product, self.other_product])
        self.db.commit()

        app = FastAPI()
        app.include_router(products_router, prefix="/api/products")
        app.include_router(scan_jobs_router, prefix="/api/cards")

        def override_db():
            yield self.db

        app.dependency_overrides[get_db] = override_db
        app.dependency_overrides[get_current_user] = lambda: self.user
        self.client = TestClient(app)

    def tearDown(self):
        self.client.close()
        self.db.close()
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    def _enqueue(self, data):
        with patch("api.recognize.get_gemini_key", return_value="secret-key"), \
                patch("api.scan_jobs.drain_scan_queue", new=AsyncMock(return_value=0)):
            return self.client.post(
                "/api/cards/recognize/jobs",
                data=data,
                files={"files": ("scan.jpg", _jpeg_bytes(), "image/jpeg")},
            )

    def _open_scan(self, product_id=None, **payload):
        body = {"condition": "NM", "lang": "de"}
        body.update(payload)
        return self.client.post(
            f"/api/products/{product_id or self.product.id}/open-scan", json=body
        )

    def test_open_scan_refuses_a_condition_or_language_it_does_not_support(self):
        # The batch defaults are written to the job and applied to every card
        # filed from it, so an unchecked value would be stamped across a whole
        # session rather than one row.
        self.assertEqual(self._open_scan(condition="Pristine").status_code, 422)
        self.assertEqual(self._open_scan(lang="kl").status_code, 422)
        self.assertEqual(
            self.db.get(ProductPurchase, self.product.id).lifecycle_status,
            "sealed",
            "a rejected request still marked the product opened",
        )

    def test_a_sold_product_cannot_be_opened_for_scanning(self):
        # A completed whole-product sale is defined by explicit proceeds.
        self.product.sold_price = 60
        self.db.commit()

        self.assertEqual(self._open_scan().status_code, 409)
        self.assertEqual(
            self.db.get(ProductPurchase, self.product.id).lifecycle_status,
            "sealed",
        )

    def test_a_product_sold_after_opening_cannot_take_a_new_scan_job(self):
        # The sale can land between opening and uploading, so enqueue has to
        # check it again rather than trust the earlier open-scan call.
        self.assertEqual(self._open_scan().status_code, 200)
        self.product.sold_price = 60
        self.db.commit()

        response = self._enqueue({
            "product_id": str(self.product.id),
            "default_condition": "NM",
            "default_lang": "de",
        })

        self.assertEqual(response.status_code, 409)
        self.assertEqual(self.db.query(ScanJob).count(), 0)

    def test_opening_then_enqueueing_persists_session_fields_and_payload(self):
        opened = self.client.post(
            f"/api/products/{self.product.id}/open-scan",
            json={"condition": "NM", "lang": "de"},
        )
        self.assertEqual(opened.status_code, 200, opened.text)
        self.assertEqual(opened.json()["condition"], "NM")
        self.assertEqual(self.db.get(ProductPurchase, self.product.id).lifecycle_status, "opened")

        response = self._enqueue({
            "product_id": str(self.product.id),
            "default_condition": "NM",
            "default_lang": "de",
        })
        self.assertEqual(response.status_code, 200, response.text)
        job = self.db.query(ScanJob).one()
        self.assertEqual((job.product_id, job.default_condition, job.default_lang), (self.product.id, "NM", "de"))
        self.assertEqual(response.json()["product_name"], "Session box")

    def test_unlinked_scanner_remains_unlinked_with_compatibility_defaults(self):
        response = self._enqueue({})
        self.assertEqual(response.status_code, 200, response.text)
        job = self.db.query(ScanJob).one()
        self.assertIsNone(job.product_id)
        self.assertEqual((job.default_condition, job.default_lang), ("Mint", "en"))

    def test_enqueue_rejects_unopened_other_owned_and_invalid_session_values(self):
        unopened = self._enqueue({
            "product_id": str(self.product.id),
            "default_condition": "Mint",
            "default_lang": "en",
        })
        self.assertEqual(unopened.status_code, 409)

        other = self._enqueue({
            "product_id": str(self.other_product.id),
            "default_condition": "Mint",
            "default_lang": "en",
        })
        self.assertEqual(other.status_code, 404)

        opened = self.client.post(f"/api/products/{self.product.id}/open-scan", json={})
        self.assertEqual(opened.status_code, 200)
        invalid_condition = self._enqueue({
            "product_id": str(self.product.id),
            "default_condition": "Pristine",
            "default_lang": "en",
        })
        self.assertEqual(invalid_condition.status_code, 422)
        invalid_language = self._enqueue({
            "product_id": str(self.product.id),
            "default_condition": "Mint",
            "default_lang": "xx",
        })
        self.assertEqual(invalid_language.status_code, 422)


if __name__ == "__main__":
    unittest.main()
