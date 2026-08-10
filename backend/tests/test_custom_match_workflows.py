import datetime
import os
import unittest
from unittest.mock import MagicMock, patch

try:
    import httpx
    from fastapi import BackgroundTasks, HTTPException
    from sqlalchemy import create_engine, text
    from sqlalchemy.exc import IntegrityError
    from sqlalchemy.orm import sessionmaker

    from api.cards import create_custom_card, get_custom_matches, migrate_custom_card
    from database import Base, install_custom_match_uniqueness
    from models import Card, CollectionItem, CustomCardMatch, User
    from schemas import CardCustomCreate
    from services.sync_service import (
        check_custom_card_matches,
        match_custom_card_in_background,
        record_custom_card_match,
    )

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    HTTPException = Exception
    DEPS_AVAILABLE = False


skip_without_deps = unittest.skipUnless(
    DEPS_AVAILABLE,
    "FastAPI/SQLAlchemy are not installed in this lightweight test environment",
)

POSTGRES_TEST_URL = os.getenv("TEST_POSTGRES_URL", "")


def _memory_db():
    engine = create_engine("sqlite:///:memory:")
    Base.metadata.create_all(engine)
    return sessionmaker(bind=engine)()


def _owner(db, username="jason"):
    """Manual cards are owned now, so these paths need a real user.

    Reused per database: minting a new user on every call would leave a card
    owned by one user and queried by another, which the ownership filters then
    correctly hide.
    """
    existing = db.query(User).filter(User.username == username).first()
    if existing:
        return existing
    user = User(username=username, hashed_password="x", role="admin", is_active=True)
    db.add(user)
    db.commit()
    db.refresh(user)
    return user


@skip_without_deps
class MigrationSafetyTests(unittest.TestCase):
    def test_migration_rejects_a_fetched_card_whose_identity_changed(self):
        db = _memory_db()
        try:
            owner = _owner(db)
            custom = Card(
                id="custom-identity",
                name="Pikachu",
                set_id="base1",
                number="58",
                lang="en",
                is_custom=True,
                custom_owner_id=owner.id,
            )
            match = CustomCardMatch(
                custom_card_id=custom.id,
                api_card_id="base1-58",
                status="pending",
            )
            db.add_all([custom, match])
            db.commit()
            db.refresh(match)

            fetched = {
                "id": "base1-58",
                "name": "Growlithe",
                "localId": "28",
            }
            with patch("api.cards.pokemon_api.get_card", return_value=fetched), \
                 patch("api.cards.pokemon_api.parse_card_for_db") as parse_card:
                with self.assertRaises(HTTPException) as ctx:
                    migrate_custom_card(match.id, db=db, current_user=_owner(db))

            self.assertEqual(ctx.exception.status_code, 409)
            parse_card.assert_not_called()
            self.assertEqual(db.get(CustomCardMatch, match.id).status, "pending")
            self.assertIsNotNone(db.get(Card, custom.id))
        finally:
            db.close()

    def test_match_preview_falls_back_to_catalogue_when_target_is_not_local(self):
        db = _memory_db()
        try:
            owner = _owner(db)
            custom = Card(
                id="custom-preview",
                name="Charmeleon",
                set_id="me02",
                number="12",
                lang="en",
                is_custom=True,
                custom_owner_id=owner.id,
            )
            match = CustomCardMatch(
                custom_card_id=custom.id,
                api_card_id="me02-012",
                status="pending",
                matched_at=datetime.datetime(2026, 1, 2, 3, 4, 5),
            )
            db.add_all([custom, match])
            db.commit()

            remote = {
                "id": "me02-012",
                "name": "Charmeleon",
                "localId": "012",
                "rarity": "Uncommon",
                "image": "https://img.example/me02-012",
                "set": {"id": "me02"},
            }
            with patch("api.cards.pokemon_api.get_card", return_value=remote) as get_card:
                result = get_custom_matches(db=db, current_user=_owner(db))

            get_card.assert_called_once_with("me02-012", lang="en")
            self.assertEqual(len(result), 1)
            self.assertEqual(
                result[0]["api_card"],
                {
                    "id": "me02-012",
                    "name": "Charmeleon",
                    "images_small": "https://img.example/me02-012/low.webp",
                    "images_large": "https://img.example/me02-012/high.png",
                    "rarity": "Uncommon",
                    "number": "012",
                    "set_id": "me02",
                },
            )
        finally:
            db.close()

    def test_match_preview_returns_none_when_local_and_remote_lookups_miss(self):
        db = _memory_db()
        try:
            owner = _owner(db)
            custom = Card(
                id="custom-no-preview",
                name="Missingno",
                set_id="unknown",
                number="0",
                lang="en",
                is_custom=True,
                custom_owner_id=owner.id,
            )
            match = CustomCardMatch(
                custom_card_id=custom.id,
                api_card_id="unknown-0",
                status="pending",
            )
            db.add_all([custom, match])
            db.commit()

            with patch("api.cards.pokemon_api.get_card", return_value=None) as get_card:
                result = get_custom_matches(db=db, current_user=_owner(db))

            get_card.assert_called_once_with("unknown-0", lang="en")
            self.assertEqual(len(result), 1)
            self.assertIsNone(result[0]["api_card"])
        finally:
            db.close()


@skip_without_deps
class CreationTimeMatchingTests(unittest.TestCase):
    def test_creation_succeeds_without_running_a_failing_matcher_inline(self):
        db = _memory_db()
        try:
            tasks = BackgroundTasks()
            data = CardCustomCreate(
                name="Pikachu",
                set_id="base1",
                number="58",
                lang="en",
            )
            with patch(
                "api.cards.match_custom_card_in_background",
                side_effect=RuntimeError("TCGdex unreachable"),
            ) as matcher:
                result = create_custom_card(data, tasks, db=db, current_user=_owner(db))

            # The id was once derived from set and number. Manual cards are
            # per-user now, so two users may both own a "base1 58" and the id
            # is opaque instead. What this test is about is that creation
            # succeeded and persisted despite the matcher being broken.
            self.assertTrue(result["id"].startswith("custom-"))
            stored = db.get(Card, result["id"])
            self.assertIsNotNone(stored)
            self.assertEqual((stored.set_id, stored.number), ("base1", "58"))
            matcher.assert_not_called()
            self.assertEqual(len(tasks.tasks), 1)
        finally:
            db.close()

    def test_creation_queues_catalogue_work_instead_of_blocking_on_it(self):
        db = _memory_db()
        try:
            tasks = MagicMock()
            data = CardCustomCreate(name="Eevee", number="51", lang="en")

            with patch("api.cards.match_custom_card_in_background") as matcher:
                result = create_custom_card(data, tasks, db=db, current_user=_owner(db))

            self.assertTrue(result["is_custom"])
            matcher.assert_not_called()
            tasks.add_task.assert_called_once_with(matcher, result["id"])
        finally:
            db.close()

    def test_background_worker_owns_and_closes_its_session_on_match_failure(self):
        db = MagicMock()
        card = Card(
            id="custom-worker",
            name="Pikachu",
            set_id="base1",
            number="58",
            lang="en",
            is_custom=True,
        )
        db.query.return_value.filter.return_value.first.return_value = card

        with patch("database.SessionLocal", return_value=db) as session_factory, \
             patch(
                 "services.sync_service.record_custom_card_match",
                 side_effect=RuntimeError("TCGdex unreachable"),
             ) as matcher, \
             patch("services.sync_service.logger.warning") as warning:
            match_custom_card_in_background(card.id)

        session_factory.assert_called_once_with()
        matcher.assert_called_once_with(db, card)
        warning.assert_called_once()
        db.close.assert_called_once_with()


@skip_without_deps
class MatchRecordingTests(unittest.TestCase):
    def test_catalogue_http_errors_are_treated_as_no_match(self):
        request = httpx.Request("GET", "https://catalogue.example/cards/base1-A")
        unavailable_errors = [
            httpx.TimeoutException("catalogue timed out", request=request),
            httpx.HTTPStatusError(
                "catalogue unavailable",
                request=request,
                response=httpx.Response(503, request=request),
            ),
        ]

        for index, unavailable_error in enumerate(unavailable_errors):
            with self.subTest(error_type=type(unavailable_error).__name__):
                db = _memory_db()
                try:
                    card = Card(
                        id=f"custom-http-{index}",
                        name="Pikachu",
                        set_id="base1",
                        number="A",
                        lang="en",
                        is_custom=True,
                    )
                    db.add(card)
                    db.commit()

                    with patch(
                        "services.sync_service.pokemon_api.get_card",
                        side_effect=unavailable_error,
                    ):
                        result = record_custom_card_match(db, card, notify=False)

                    self.assertIsNone(result)
                    self.assertEqual(db.query(CustomCardMatch).count(), 0)
                finally:
                    db.close()

    def test_integrity_error_is_swallowed_after_rollback(self):
        db = MagicMock()
        card = Card(
            id="custom-race",
            name="Pikachu",
            set_id="base1",
            number="58",
            lang="en",
            is_custom=True,
        )
        db.commit.side_effect = IntegrityError("duplicate", {}, RuntimeError("race"))

        with patch("services.sync_service.find_api_match", return_value="base1-58"):
            result = record_custom_card_match(db, card, notify=False)

        self.assertIsNone(result)
        db.add.assert_called_once()
        db.rollback.assert_called_once_with()

    def test_non_integrity_persistence_error_rolls_back_and_propagates(self):
        db = MagicMock()
        card = Card(
            id="custom-db-error",
            name="Pikachu",
            set_id="base1",
            number="58",
            lang="en",
            is_custom=True,
        )
        db.commit.side_effect = RuntimeError("database unavailable")

        with patch("services.sync_service.find_api_match", return_value="base1-58"):
            with self.assertRaisesRegex(RuntimeError, "database unavailable"):
                record_custom_card_match(db, card, notify=False)

        db.rollback.assert_called_once_with()

    def test_full_check_logs_each_failure_and_the_failure_count(self):
        db = _memory_db()
        try:
            owner = _owner(db)
            cards = [
                Card(
                    id=f"custom-{suffix.lower()}",
                    name=suffix,
                    set_id="test-set",
                    number=suffix,
                    lang="en",
                    is_custom=True,
                    custom_owner_id=owner.id,
                )
                for suffix in ("A", "B", "C")
            ]
            db.add_all(cards)
            db.commit()

            with patch(
                "services.sync_service.pokemon_api.get_card",
                side_effect=[RuntimeError("first"), None, ValueError("third")],
            ) as get_card, patch("services.sync_service.logger.error") as error:
                check_custom_card_matches(db)

            self.assertEqual(get_card.call_count, 3)
            error.assert_any_call("Could not record match for custom card custom-a: first")
            error.assert_any_call("Could not record match for custom card custom-c: third")
            error.assert_any_call("%s custom card(s) could not be checked for matches", 2)
        finally:
            db.close()


@skip_without_deps
class CustomMatchUniquenessTests(unittest.TestCase):
    @unittest.skipUnless(
        POSTGRES_TEST_URL.startswith("postgresql"),
        "TEST_POSTGRES_URL is required to exercise PostgreSQL DELETE USING cleanup SQL",
    )
    def test_duplicate_pending_rows_are_removed_before_index_creation(self):
        engine = create_engine(POSTGRES_TEST_URL)
        try:
            try:
                conn = engine.connect()
            except Exception as exc:
                self.skipTest(
                    "PostgreSQL is unavailable at TEST_POSTGRES_URL "
                    f"({type(exc).__name__})"
                )

            with conn:
                # A temporary table and indexes exercise the production SQL
                # without touching application data in the configured test DB.
                conn.execute(text("""
                    CREATE TEMPORARY TABLE custom_card_matches (
                        id INTEGER PRIMARY KEY,
                        custom_card_id VARCHAR NOT NULL,
                        api_card_id VARCHAR NOT NULL,
                        matched_at TIMESTAMP NULL,
                        status VARCHAR NOT NULL
                    ) ON COMMIT PRESERVE ROWS
                """))
                conn.execute(text(
                    "CREATE INDEX ux_custom_card_match_open "
                    "ON custom_card_matches (custom_card_id)"
                ))
                conn.execute(text("""
                    INSERT INTO custom_card_matches
                        (id, custom_card_id, api_card_id, matched_at, status)
                    VALUES
                        (1, 'custom-a', 'api-1', NULL, 'pending'),
                        (2, 'custom-a', 'api-2', NULL, 'pending'),
                        (3, 'custom-a', 'api-3', TIMESTAMP '2026-01-01', 'pending'),
                        (4, 'custom-a', 'api-4', NULL, 'dismissed')
                """))
                conn.commit()

                self.assertTrue(install_custom_match_uniqueness(conn))
                remaining = [
                    tuple(row)
                    for row in conn.execute(text(
                        "SELECT id, status FROM custom_card_matches ORDER BY id"
                    )).all()
                ]
                self.assertEqual(remaining, [(3, "pending"), (4, "dismissed")])
                self.assertIsNone(
                    conn.execute(text(
                        "SELECT to_regclass('ux_custom_card_match_open')"
                    )).scalar_one()
                )
        finally:
            engine.dispose()

    def test_legacy_index_drop_failure_is_reported_by_strict_installer(self):
        class FailingConnection:
            def __init__(self):
                self.statements = []
                self.rolled_back = False

            def execute(self, statement):
                sql = " ".join(str(statement).split())
                self.statements.append(sql)
                if sql == "DROP INDEX IF EXISTS ux_custom_card_match_open":
                    raise RuntimeError("permission denied")

            def commit(self):
                raise AssertionError("failed installation must not commit")

            def rollback(self):
                self.rolled_back = True

        conn = FailingConnection()
        with patch("database.logger.error") as error:
            result = install_custom_match_uniqueness(conn)

        self.assertFalse(result)
        self.assertTrue(conn.rolled_back)
        self.assertEqual(
            conn.statements,
            ["DROP INDEX IF EXISTS ux_custom_card_match_open"],
        )
        error.assert_called_once()

    def test_two_custom_cards_can_both_migrate_to_one_api_card(self):
        db = _memory_db()
        try:
            class SqliteInstallConnection:
                def execute(self, statement):
                    sql = str(statement)
                    if sql.lstrip().startswith("DELETE FROM custom_card_matches"):
                        return None  # PostgreSQL DELETE USING; the new fixture has no duplicates.
                    return db.execute(text(sql))

                def commit(self):
                    db.commit()

                def rollback(self):
                    db.rollback()

            self.assertTrue(install_custom_match_uniqueness(SqliteInstallConnection()))
            owner = _owner(db)
            first = Card(
                id="custom-first",
                name="Pikachu",
                set_id="base1",
                number="58",
                lang="en",
                is_custom=True,
                custom_owner_id=owner.id,
            )
            second = Card(
                id="custom-second",
                name="Pikachu",
                set_id="base1",
                number="58",
                lang="en",
                is_custom=True,
                custom_owner_id=owner.id,
            )
            first_match = CustomCardMatch(
                custom_card_id=first.id,
                api_card_id="base1-58",
                status="pending",
            )
            second_match = CustomCardMatch(
                custom_card_id=second.id,
                api_card_id="base1-58",
                status="pending",
            )
            db.add_all([first, second, first_match, second_match])
            db.commit()
            db.refresh(first_match)
            db.refresh(second_match)

            fetched = {"id": "base1-58", "name": "Pikachu", "localId": "58"}
            parsed = {
                "id": "base1-58_en",
                "tcg_card_id": "base1-58",
                "name": "Pikachu",
                "set_id": None,
                "number": "58",
                "lang": "en",
            }
            with patch("api.cards.pokemon_api.get_card", return_value=fetched), \
                 patch(
                     "api.cards.pokemon_api.parse_card_for_db",
                     side_effect=lambda *args, **kwargs: dict(parsed),
                 ), \
                 patch(
                     "api.cards.apply_cross_language_fallbacks",
                     side_effect=lambda _db, value: value,
                 ):
                migrate_custom_card(first_match.id, db=db, current_user=_owner(db))
                migrate_custom_card(second_match.id, db=db, current_user=_owner(db))

            migrated = db.query(CustomCardMatch).order_by(CustomCardMatch.id).all()
            self.assertEqual([row.status for row in migrated], ["migrated", "migrated"])
            self.assertEqual(
                [row.custom_card_id for row in migrated],
                ["base1-58_en", "base1-58_en"],
            )
        finally:
            db.close()

    def test_promoting_a_shared_template_leaves_every_clone_alone(self):
        """One logical card is several physical cards after the ownership backfill.

        The backfill gives the first admin the original as a shared template and
        every other user a private clone. Promotion is scoped to the caller's own
        card, so this checks that the admin promoting the template moves only the
        admin's rows: the clone, its owner's collection row and its own pending
        match all have to survive untouched, and the clone's owner has to still be
        able to promote afterwards.
        """
        db = _memory_db()
        try:
            admin = _owner(db, username="admin")
            trainer = _owner(db, username="mika")

            template = Card(
                id="custom-legacy", name="Bergmite", set_id="me04", number="24",
                lang="en", is_custom=True,
                custom_owner_id=admin.id, is_shared_template=True,
            )
            clone = Card(
                id="custom-clone", name="Bergmite", set_id="me04", number="24",
                lang="en", is_custom=True,
                custom_owner_id=trainer.id, is_shared_template=False,
                custom_source_card_id="custom-legacy",
            )
            db.add_all([template, clone])
            db.commit()

            for card, user in ((template, admin), (clone, trainer)):
                db.add(CollectionItem(
                    card_id=card.id, user_id=user.id, quantity=1,
                    condition="NM", variant="Normal", lang="en",
                ))
            db.add_all([
                CustomCardMatch(custom_card_id="custom-legacy", api_card_id="me04-024", status="pending"),
                CustomCardMatch(custom_card_id="custom-clone", api_card_id="me04-024", status="pending"),
            ])
            db.commit()

            template_match = db.query(CustomCardMatch).filter(
                CustomCardMatch.custom_card_id == "custom-legacy"
            ).one()
            clone_match = db.query(CustomCardMatch).filter(
                CustomCardMatch.custom_card_id == "custom-clone"
            ).one()

            fetched = {"id": "me04-024", "name": "Bergmite", "localId": "24"}
            parsed = {
                "id": "me04-024_en", "tcg_card_id": "me04-024", "name": "Bergmite",
                "set_id": None, "number": "24", "lang": "en",
            }
            promote = lambda match_id, user: migrate_custom_card(match_id, db=db, current_user=user)
            with patch("api.cards.pokemon_api.get_card", return_value=fetched), \
                 patch("api.cards.pokemon_api.parse_card_for_db",
                       side_effect=lambda *args, **kwargs: dict(parsed)), \
                 patch("api.cards.apply_cross_language_fallbacks",
                       side_effect=lambda _db, value: value):
                promote(template_match.id, admin)

                # The admin's own side moved.
                self.assertIsNone(db.query(Card).filter(Card.id == "custom-legacy").first())
                admin_item = db.query(CollectionItem).filter(
                    CollectionItem.user_id == admin.id
                ).one()
                self.assertEqual(admin_item.card_id, "me04-024_en")

                # The clone did not.
                surviving = db.query(Card).filter(Card.id == "custom-clone").one()
                self.assertTrue(surviving.is_custom)
                self.assertEqual(surviving.custom_owner_id, trainer.id)
                trainer_item = db.query(CollectionItem).filter(
                    CollectionItem.user_id == trainer.id
                ).one()
                self.assertEqual(trainer_item.card_id, "custom-clone")
                db.refresh(clone_match)
                self.assertEqual(clone_match.status, "pending")

                # The pointer back to the template now dangles. Nothing reads it
                # at runtime and there is no foreign key, so this is recorded as
                # the real state rather than asserted to be tidy.
                self.assertEqual(surviving.custom_source_card_id, "custom-legacy")

                # The divergence is real until the clone's owner acts, and the
                # promotion still available to them is what closes it.
                promote(clone_match.id, trainer)

            self.assertIsNone(db.query(Card).filter(Card.id == "custom-clone").first())
            self.assertEqual(
                {item.card_id for item in db.query(CollectionItem).all()},
                {"me04-024_en"},
            )
        finally:
            db.close()

    def test_a_clone_owner_cannot_promote_someone_elses_match(self):
        db = _memory_db()
        try:
            admin = _owner(db, username="admin")
            trainer = _owner(db, username="mika")
            db.add(Card(
                id="custom-legacy", name="Bergmite", set_id="me04", number="24",
                lang="en", is_custom=True,
                custom_owner_id=admin.id, is_shared_template=True,
            ))
            db.commit()
            db.add(CustomCardMatch(
                custom_card_id="custom-legacy", api_card_id="me04-024", status="pending",
            ))
            db.commit()
            match = db.query(CustomCardMatch).one()

            with self.assertRaises(HTTPException) as caught:
                migrate_custom_card(match.id, db=db, current_user=trainer)
            self.assertEqual(caught.exception.status_code, 404)
            self.assertIsNotNone(db.query(Card).filter(Card.id == "custom-legacy").first())
            self.assertEqual(db.query(CustomCardMatch).one().status, "pending")
        finally:
            db.close()

    def test_index_creation_failure_is_logged_and_returns_false(self):
        class FailingConnection:
            def __init__(self):
                self.calls = 0
                self.rolled_back = False

            def execute(self, statement):
                self.calls += 1
                if str(statement).lstrip().startswith("CREATE UNIQUE INDEX"):
                    raise RuntimeError("permission denied")

            def commit(self):
                raise AssertionError("failed installation must not commit")

            def rollback(self):
                self.rolled_back = True

        conn = FailingConnection()
        with patch("database.logger.error") as error:
            result = install_custom_match_uniqueness(conn)

        self.assertFalse(result)
        self.assertTrue(conn.rolled_back)
        error.assert_called_once()
        self.assertIn("ux_custom_card_match_pending", error.call_args.args[0])
        self.assertIsInstance(error.call_args.args[-1], RuntimeError)


if __name__ == "__main__":
    unittest.main()
