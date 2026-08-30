"""Starting up against a database that predates the current models.

This is the case the other schema tests missed. They exercised create_all on a
fresh database and the migration list on a prepared one, separately. Startup
does create_all FIRST and the migrations afterwards, and create_all skips tables
that already exist - so an established database keeps its old constraints while
create_all still tries to build new tables that reference them.

binder_slots has a composite foreign key onto (binder_cards.id, binder_id).
PostgreSQL rejects a foreign key onto columns with no matching unique
constraint, so a real upgrade died at startup while every fresh-install test
passed.
"""
import os
import unittest

try:
    from sqlalchemy import create_engine, text
    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class StartupUpgradeTests(unittest.TestCase):
    """Runs the real init_db against a deliberately outdated schema."""

    def setUp(self):
        database = POSTGRES_TEST_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in database.lower():
            raise unittest.SkipTest(f"refusing to drop tables in database {database!r}")
        import database

        # database.engine is bound at import from DATABASE_URL, so setting the
        # variable here would be too late: whichever test imported the module
        # first decides where init_db points. Rebind it for the duration.
        self.engine = create_engine(POSTGRES_TEST_URL)
        self._original_engine = database.engine
        database.engine = self.engine

    def tearDown(self):
        import database

        database.engine = self._original_engine
        self.engine.dispose()

    def _build_pre_feature_schema(self):
        """Everything the app had before binder layouts existed."""
        import database
        from database import Base
        import models  # noqa: F401 - registers the tables

        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("DROP TABLE IF EXISTS binder_slots"))
            conn.execute(text("ALTER TABLE binders DROP CONSTRAINT IF EXISTS ck_binder_grid_dimensions"))
            conn.execute(text("ALTER TABLE binders DROP COLUMN IF EXISTS grid_rows"))
            conn.execute(text("ALTER TABLE binders DROP COLUMN IF EXISTS grid_columns"))
            # The constraint the new table depends on did not exist before.
            conn.execute(text(
                "ALTER TABLE binder_cards DROP CONSTRAINT IF EXISTS uq_binder_cards_id_binder"
            ))
        return database

    def test_startup_upgrades_an_existing_database(self):
        database = self._build_pre_feature_schema()

        # The real entry point, in the real order.
        database.init_db()

        with self.engine.connect() as conn:
            self.assertIsNotNone(
                conn.execute(text("SELECT to_regclass('binder_slots')")).scalar(),
                "binder_slots was not created during startup",
            )
            grid = [row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='binders' AND column_name LIKE 'grid%' ORDER BY 1"
            ))]
            self.assertEqual(grid, ["grid_columns", "grid_rows"])
            for name in ("uq_binder_cards_id_binder", "fk_binder_slots_entry"):
                self.assertEqual(
                    conn.execute(text(
                        "SELECT count(*) FROM pg_constraint WHERE conname = :name"
                    ), {"name": name}).scalar(), 1, f"{name} missing after startup")

    def test_startup_is_repeatable(self):
        """It runs on every boot, so a second pass must be a no-op."""
        database = self._build_pre_feature_schema()
        database.init_db()
        database.init_db()

        with self.engine.connect() as conn:
            self.assertEqual(
                conn.execute(text(
                    "SELECT count(*) FROM pg_constraint "
                    "WHERE conname = 'uq_binder_cards_id_binder'"
                )).scalar(), 1)

    def _build_pre_confidence_scan_schema(self):
        """A database whose scan_job_items predates the persisted verdict.

        create_all skips scan_job_items because it already exists, so the only
        thing that can add these columns to an established install is the
        migration list. A fresh-schema test would pass without a migration at
        all and prove nothing.
        """
        import datetime

        from sqlalchemy.orm import Session

        import database
        from database import Base
        from models import ScanJob, ScanJobItem, User

        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

        # Seed a reviewed scan through the ORM so every unrelated NOT NULL
        # column gets its real default, then take the columns away. That leaves
        # exactly what an install upgraded from the previous release holds: a
        # complete row with no verdict recorded anywhere.
        now = datetime.datetime.utcnow()
        with Session(self.engine) as session:
            user = User(username="legacy-scan-owner", hashed_password="x")
            session.add(user)
            session.flush()
            job = ScanJob(
                user_id=user.id,
                status="done",
                created_at=now,
                updated_at=now,
                expires_at=now + datetime.timedelta(days=14),
            )
            session.add(job)
            session.flush()
            session.add(ScanJobItem(
                job_id=job.id,
                user_id=user.id,
                position=0,
                image_path=f"{job.id}/0.jpg",
                content_type="image/jpeg",
                byte_size=4,
                status="done",
                resolved=False,
                attempts=1,
                transient_failures=0,
                recognized={"name": "Pikachu"},
                matches=[{"tcg_card_id": "base1-58"}],
                created_at=now,
                updated_at=now,
            ))
            session.commit()

        with self.engine.begin() as conn:
            for column in ("identity_confident", "identity_decision", "suggested_match_id"):
                conn.execute(text(
                    f"ALTER TABLE scan_job_items DROP COLUMN IF EXISTS {column}"
                ))
        return database

    def _scan_item_columns(self, conn):
        return {row[0]: row[1] for row in conn.execute(text(
            "SELECT column_name, is_nullable FROM information_schema.columns "
            "WHERE table_name = 'scan_job_items'"
        ))}

    def _existing_scan_values(self, conn):
        """The columns the row already had, readable before and after the boot."""
        return conn.execute(text(
            "SELECT recognized::text, matches::text, status, resolved, attempts "
            "FROM scan_job_items ORDER BY id"
        )).one()

    def _scan_verdict(self, conn):
        return conn.execute(text(
            "SELECT identity_confident, identity_decision, suggested_match_id "
            "FROM scan_job_items ORDER BY id"
        )).one()

    def test_startup_adds_the_scan_verdict_columns_to_an_existing_database(self):
        database = self._build_pre_confidence_scan_schema()
        with self.engine.connect() as conn:
            self.assertNotIn("identity_confident", self._scan_item_columns(conn))
            before = self._existing_scan_values(conn)

        database.init_db()

        with self.engine.connect() as conn:
            columns = self._scan_item_columns(conn)
            for column in ("identity_confident", "identity_decision", "suggested_match_id"):
                self.assertIn(column, columns, f"{column} missing after startup")
                # NOT NULL would need a backfill, and any backfilled value would
                # be a claim about a scan nobody judged.
                self.assertEqual(columns[column], "YES", f"{column} must stay nullable")

            # The negative control: the migration must add columns and disturb
            # nothing the row already held.
            after = self._existing_scan_values(conn)
            self.assertEqual(after, before)
            self.assertEqual(after[0], '{"name": "Pikachu"}')
            self.assertEqual(after[1], '[{"tcg_card_id": "base1-58"}]')
            self.assertEqual(after[2], "done")
            self.assertIs(after[3], False)
            self.assertEqual(after[4], 1)
            # A row nobody judged reads as unknown, not as unconvinced.
            self.assertEqual(self._scan_verdict(conn), (None, None, None))

    def test_scan_verdict_columns_survive_a_second_startup(self):
        """It runs on every boot, and a stored verdict must outlive a restart."""
        database = self._build_pre_confidence_scan_schema()
        database.init_db()

        with self.engine.begin() as conn:
            conn.execute(text(
                "UPDATE scan_job_items SET identity_confident = TRUE, "
                "identity_decision = 'number_unique', suggested_match_id = 'base1-58'"
            ))

        database.init_db()

        with self.engine.connect() as conn:
            self.assertEqual(
                self._scan_verdict(conn),
                (True, "number_unique", "base1-58"),
            )
            self.assertEqual(
                conn.execute(text(
                    "SELECT count(*) FROM information_schema.columns "
                    "WHERE table_name = 'scan_job_items' "
                    "AND column_name = 'identity_confident'"
                )).scalar(), 1)

    def test_startup_still_works_on_a_fresh_database(self):
        from database import Base
        import database
        import models  # noqa: F401

        Base.metadata.drop_all(self.engine)
        database.init_db()

        with self.engine.connect() as conn:
            self.assertIsNotNone(
                conn.execute(text("SELECT to_regclass('binder_slots')")).scalar())


if __name__ == "__main__":
    unittest.main()


@unittest.skipUnless(DEPS_AVAILABLE and POSTGRES_TEST_URL, "TEST_POSTGRES_URL is not set")
class ProductScanSessionMigrationTests(unittest.TestCase):
    """The product-session columns on scan_jobs, on both schema paths.

    A scan job predates this feature on every established install, so the
    columns arrive by migration there and by create_all on a fresh one. Those
    two paths produce the defaults differently: create_all needs server_default
    on the model, and only the migration's DEFAULT clause reaches an existing
    table. Testing one path proves nothing about the other.
    """

    def setUp(self):
        name = POSTGRES_TEST_URL.rsplit("/", 1)[-1].split("?")[0]
        if "test" not in name.lower():
            raise unittest.SkipTest(f"refusing to drop tables in database {name!r}")
        import database

        self.engine = create_engine(POSTGRES_TEST_URL)
        self._original_engine = database.engine
        database.engine = self.engine

    def tearDown(self):
        import database

        database.engine = self._original_engine
        self.engine.dispose()

    def _pre_feature_schema(self):
        """The schema as it stood before a scan job could belong to a product."""
        import database
        from database import Base
        import models  # noqa: F401 - registers the tables

        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)
        with self.engine.begin() as conn:
            conn.execute(text("DROP INDEX IF EXISTS ix_scan_jobs_product_id"))
            for column in ("product_id", "default_condition", "default_lang"):
                conn.execute(text(f"ALTER TABLE scan_jobs DROP COLUMN IF EXISTS {column}"))
        return database

    @staticmethod
    def _column_default(conn, column):
        return conn.execute(text(
            "SELECT column_default FROM information_schema.columns "
            "WHERE table_name='scan_jobs' AND column_name=:column"
        ), {"column": column}).scalar()

    def _seed_job(self, *, with_product):
        """Owner and product through the ORM; the scan job through raw SQL.

        The job insert has to omit default_condition and default_lang, because
        the whole point is that the database supplies them rather than the ORM.
        The surrounding rows carry unrelated not-null columns and are simpler
        to build with the models.
        """
        import datetime
        from sqlalchemy.orm import sessionmaker
        from models import ProductPurchase, User

        session = sessionmaker(bind=self.engine)()
        try:
            user = User(username="migration-owner", hashed_password="x")
            session.add(user)
            session.flush()
            product_id = None
            if with_product:
                product = ProductPurchase(
                    user_id=user.id,
                    product_name="Elite Trainer Box",
                    purchase_price=40.0,
                    purchase_date=datetime.date(2026, 1, 1),
                    lifecycle_status="opened",
                )
                session.add(product)
                session.flush()
                product_id = product.id
            job_id = session.execute(text(
                # status and the timestamps are not-null with ORM-only
                # defaults, so a raw insert must supply them. Only
                # default_condition and default_lang are left out, which is
                # what this is testing.
                "INSERT INTO scan_jobs "
                "(user_id, product_id, status, created_at, updated_at, expires_at) "
                "VALUES (:user, :product, 'pending', NOW(), NOW(), "
                "NOW() + INTERVAL '1 day') RETURNING id"
            ), {"user": user.id, "product": product_id}).scalar()
            session.commit()
            return job_id, product_id
        finally:
            session.close()


    def test_an_established_database_gains_the_columns_at_startup(self):
        database = self._pre_feature_schema()

        database.init_db()

        with self.engine.connect() as conn:
            columns = [row[0] for row in conn.execute(text(
                "SELECT column_name FROM information_schema.columns "
                "WHERE table_name='scan_jobs' AND column_name IN "
                "('product_id','default_condition','default_lang') ORDER BY 1"
            ))]
            self.assertEqual(columns, ["default_condition", "default_lang", "product_id"])
            self.assertIn("Mint", self._column_default(conn, "default_condition") or "")
            self.assertIn("en", self._column_default(conn, "default_lang") or "")
            self.assertIsNotNone(
                conn.execute(text("SELECT to_regclass('ix_scan_jobs_product_id')")).scalar(),
                "the product index was not created at startup",
            )

    def test_a_second_startup_leaves_the_upgrade_alone(self):
        # Migrations run on every boot. A change undone by the next restart
        # passes its own test and breaks the install.
        database = self._pre_feature_schema()
        database.init_db()
        database.init_db()

        with self.engine.connect() as conn:
            self.assertIn("Mint", self._column_default(conn, "default_condition") or "")
            self.assertEqual(
                conn.execute(text(
                    "SELECT count(*) FROM pg_indexes "
                    "WHERE indexname = 'ix_scan_jobs_product_id'"
                )).scalar(), 1)

    def test_a_fresh_install_supplies_the_defaults_from_the_model(self):
        import database
        from database import Base
        import models  # noqa: F401

        Base.metadata.drop_all(self.engine)
        database.init_db()

        job_id, _ = self._seed_job(with_product=False)
        with self.engine.connect() as conn:
            row = conn.execute(text(
                "SELECT default_condition, default_lang, product_id "
                "FROM scan_jobs WHERE id = :id"
            ), {"id": job_id}).one()
        self.assertEqual((row[0], row[1], row[2]), ("Mint", "en", None))

    def test_the_upgraded_delete_action_also_keeps_the_scan_job(self):
        # The sibling test below recreates the schema from the model, so it
        # exercises the FRESH INSTALL foreign key. Changing only the migration
        # SQL from SET NULL to CASCADE left it passing. The two paths build the
        # constraint independently and both have to be checked.
        database = self._pre_feature_schema()
        database.init_db()

        job_id, product_id = self._seed_job(with_product=True)
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM product_purchases WHERE id = :id"), {"id": product_id})
            survived = conn.execute(text(
                "SELECT product_id FROM scan_jobs WHERE id = :id"
            ), {"id": job_id}).one_or_none()

        self.assertIsNotNone(survived, "the migrated schema deleted the job with its product")
        self.assertIsNone(survived[0], "the migrated schema left a dangling reference")

    def test_deleting_a_product_keeps_the_unfinished_scan_job(self):
        # The reason product_id is a real foreign key with SET NULL: an
        # abandoned opening must not take the user's queued review work with
        # it, and must not leave a dangling product reference either.
        import database
        from database import Base
        import models  # noqa: F401

        Base.metadata.drop_all(self.engine)
        database.init_db()

        job_id, product_id = self._seed_job(with_product=True)
        with self.engine.begin() as conn:
            conn.execute(text("DELETE FROM product_purchases WHERE id = :id"), {"id": product_id})
            survived = conn.execute(text(
                "SELECT product_id FROM scan_jobs WHERE id = :id"
            ), {"id": job_id}).one_or_none()

        self.assertIsNotNone(survived, "the scan job was deleted with its product")
        self.assertIsNone(survived[0], "the deleted product left a dangling reference")
