"""Real PostgreSQL coverage for the collection identity migration."""

import os
import unittest
import uuid

try:
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from database import Base, install_collection_merge_uniqueness
    from models import CollectionItem

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


@unittest.skipUnless(
    DEPS_AVAILABLE and POSTGRES_TEST_URL.startswith("postgresql"),
    "TEST_POSTGRES_URL is required for PostgreSQL collection-migration coverage",
)
class CollectionMergeMigrationPostgresTests(unittest.TestCase):
    def setUp(self):
        self.schema = f"collection_merge_test_{uuid.uuid4().hex}"
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

    def tearDown(self):
        self.engine.dispose()
        with self.admin_engine.connect() as conn:
            conn.execute(text(f'DROP SCHEMA "{self.schema}" CASCADE'))
        self.admin_engine.dispose()

    def test_consolidates_duplicates_retargets_references_and_installs_backstop(self):
        """The one-off migration retains quantities, allocations and history."""
        with self.engine.begin() as conn:
            user_id = conn.execute(text(
                "INSERT INTO users (username, hashed_password) VALUES ('merge-owner', 'x') RETURNING id"
            )).scalar_one()
            conn.execute(text(
                "INSERT INTO sets (id, tcg_set_id, name, lang) VALUES ('merge_en', 'merge', 'Merge', 'en')"
            ))
            conn.execute(text(
                "INSERT INTO cards (id, tcg_card_id, name, set_id, number, lang) "
                "VALUES ('merge-001_en', 'merge-001', 'Migration card', 'merge', '001', 'en')"
            ))
            keeper_id = conn.execute(text(
                "INSERT INTO collection (user_id, card_id, quantity, condition, variant, lang) "
                "VALUES (:user_id, 'merge-001_en', 2, 'Mint', 'Normal', 'en') RETURNING id"
            ), {"user_id": user_id}).scalar_one()
            loser_id = conn.execute(text(
                "INSERT INTO collection (user_id, card_id, quantity, condition, variant, lang) "
                "VALUES (:user_id, 'merge-001_en', 3, 'Mint', 'Normal', 'en') RETURNING id"
            ), {"user_id": user_id}).scalar_one()
            binder_id = conn.execute(text(
                "INSERT INTO binders (name, user_id, grid_rows, grid_columns) "
                "VALUES ('Migration binder', :user_id, 3, 3) RETURNING id"
            ), {"user_id": user_id}).scalar_one()
            keeper_binder_id = conn.execute(text(
                "INSERT INTO binder_cards (binder_id, card_id, collection_item_id, required_quantity) "
                "VALUES (:binder_id, 'merge-001_en', :item_id, 1) RETURNING id"
            ), {"binder_id": binder_id, "item_id": keeper_id}).scalar_one()
            loser_binder_id = conn.execute(text(
                "INSERT INTO binder_cards (binder_id, card_id, collection_item_id, required_quantity) "
                "VALUES (:binder_id, 'merge-001_en', :item_id, 2) RETURNING id"
            ), {"binder_id": binder_id, "item_id": loser_id}).scalar_one()
            conn.execute(text(
                "INSERT INTO binder_slots (binder_card_id, binder_id, page, pocket) "
                "VALUES (:entry, :binder, 1, 1), (:loser, :binder, 1, 2)"
            ), {"entry": keeper_binder_id, "loser": loser_binder_id, "binder": binder_id})
            product_id = conn.execute(text(
                "INSERT INTO product_purchases (product_name, user_id, purchase_price, purchase_date) "
                "VALUES ('Migration product', :user_id, 1, CURRENT_DATE) RETURNING id"
            ), {"user_id": user_id}).scalar_one()
            conn.execute(text(
                "INSERT INTO product_cards (product_id, user_id, card_id, collection_item_id) "
                "VALUES (:product, :user_id, 'merge-001_en', :item_id)"
            ), {"product": product_id, "user_id": user_id, "item_id": loser_id})
            conn.execute(text(
                "INSERT INTO product_ledger_entries "
                "(product_id, user_id, entry_type, card_id, original_collection_item_id, quantity, amount, event_date) "
                "VALUES (:product, :user_id, 'card_sale', 'merge-001_en', :item_id, 1, 0, CURRENT_DATE)"
            ), {"product": product_id, "user_id": user_id, "item_id": loser_id})
            trade_id = conn.execute(text(
                "INSERT INTO trades (user_id, trade_date) VALUES (:user_id, CURRENT_DATE) RETURNING id"
            ), {"user_id": user_id}).scalar_one()
            conn.execute(text(
                "INSERT INTO trade_items "
                "(trade_id, user_id, direction, card_id, original_collection_item_id, created_collection_item_id, quantity, value_per_card, value_total) "
                "VALUES (:trade, :user_id, 'incoming', 'merge-001_en', :item_id, :item_id, 1, 0, 0)"
            ), {"trade": trade_id, "user_id": user_id, "item_id": loser_id})
            conn.execute(text(
                "INSERT INTO deleted_collection_items "
                "(original_collection_item_id, user_id, card_id, quantity, variant) "
                "VALUES (:item_id, :user_id, 'merge-001_en', 1, 'Normal')"
            ), {"item_id": loser_id, "user_id": user_id})

        with self.engine.connect() as conn:
            self.assertTrue(install_collection_merge_uniqueness(conn))

        with self.engine.connect() as conn:
            self.assertEqual(conn.execute(text("SELECT quantity FROM collection")).scalar_one(), 5)
            self.assertEqual(conn.execute(text(
                "SELECT collection_item_id, required_quantity FROM binder_cards"
            )).one(), (keeper_id, 3))
            self.assertEqual(conn.execute(text(
                "SELECT count(*) FROM binder_slots WHERE binder_card_id = :entry"
            ), {"entry": keeper_binder_id}).scalar_one(), 2)
            for table, column in (
                ("product_cards", "collection_item_id"),
                ("product_ledger_entries", "original_collection_item_id"),
                ("trade_items", "original_collection_item_id"),
                ("trade_items", "created_collection_item_id"),
                ("deleted_collection_items", "original_collection_item_id"),
            ):
                self.assertEqual(
                    conn.execute(text(f"SELECT {column} FROM {table} LIMIT 1")).scalar_one(),
                    keeper_id,
                    f"{table}.{column} was not retargeted",
                )
            self.assertIsNotNone(conn.execute(text(
                "SELECT to_regclass('uq_collection_merge_identity')"
            )).scalar_one())

        db = self.Session()
        try:
            db.add(CollectionItem(
                user_id=user_id, card_id="merge-001_en", quantity=1,
                condition="Mint", variant="Normal", lang="en",
            ))
            with self.assertRaises(IntegrityError):
                db.commit()
        finally:
            db.rollback()
            db.close()


if __name__ == "__main__":
    unittest.main()
