"""Composite matching runs positions concurrently without extra DB connections.

The first attempt at this gave every concurrent matcher its own SQLAlchemy
Session. That turned one pooled connection per claim into three, and the two
extra checkouts were synchronous blocking waits made from inside the event loop
while the coordinator already held a connection of its own. These tests pin the
properties that failure violated, so it cannot come back quietly.

By default the engine here is a file-backed SQLite database driven by a real
QueuePool, which exercises SQLAlchemy's pool accounting but proves nothing about
PostgreSQL's own behaviour. Set SCAN_QUEUE_POSTGRES_TEST=1 with a PostgreSQL
DATABASE_URL to run the same assertions against the database production uses.
"""

import asyncio
import datetime
import io
import os
import tempfile
import unittest
from unittest.mock import AsyncMock, MagicMock, patch

try:
    from PIL import Image
    from sqlalchemy import create_engine, event
    from sqlalchemy.orm import Session, sessionmaker
    from sqlalchemy.pool import QueuePool

    from database import Base
    from fastapi import HTTPException
    from models import Card, ScanJob, ScanJobItem, ScanQueueUserState, Set, User
    from services import scan_queue, scan_storage
    from services.scan_queue import claim_next_scan_item, process_claimed_scan_item

    DEPS_AVAILABLE = True
except ModuleNotFoundError:  # pragma: no cover - mirrors the sibling suites
    DEPS_AVAILABLE = False


POSTGRES_TEST_ENABLED = (
    DEPS_AVAILABLE
    and os.environ.get("SCAN_QUEUE_POSTGRES_TEST") == "1"
    and os.environ.get("DATABASE_URL", "").startswith("postgresql")
)


def _jpeg(colour: str, size: tuple[int, int] = (100, 140)) -> bytes:
    output = io.BytesIO()
    Image.new("RGB", size, colour).save(output, format="JPEG")
    return output.getvalue()


def _real_matcher():
    """The real matcher, captured before the module attribute is patched."""
    from api.recognize import match_composite_card_info

    return match_composite_card_info


def _mock_db(user):
    """A session stub whose reads all come back empty, for the fake-network tests."""
    db = MagicMock()
    db.get.return_value = user
    db.query.return_value.filter.return_value.all.return_value = []
    db.query.return_value.filter.return_value.first.return_value = None
    return db


class _PoolProbe:
    """Record every pooled connection checkout and checkin for one engine."""

    def __init__(self, engine):
        self.engine = engine
        self.total_checkouts = 0
        self.live = 0
        self.peak = 0
        self.connections: set[int] = set()
        self.armed = False
        self.checkouts_while_armed = 0

    def __enter__(self):
        event.listen(self.engine, "checkout", self._checkout)
        event.listen(self.engine, "checkin", self._checkin)
        return self

    def __exit__(self, *exc_info):
        event.remove(self.engine, "checkout", self._checkout)
        event.remove(self.engine, "checkin", self._checkin)
        return False

    def arm(self) -> None:
        """Start counting checkouts, from the moment matching begins."""
        self.armed = True

    def _checkout(self, dbapi_connection, connection_record, connection_proxy):
        self.total_checkouts += 1
        self.live += 1
        self.peak = max(self.peak, self.live)
        self.connections.add(id(dbapi_connection))
        if self.armed:
            self.checkouts_while_armed += 1

    def _checkin(self, dbapi_connection, connection_record):
        self.live -= 1


class _FakeStreamResponse:
    def __init__(self, payload: bytes, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers = {"content-length": str(len(payload))}

    async def aiter_bytes(self):
        yield self._payload


class _FakeResponse:
    def __init__(self, payload, status_code: int = 200):
        self._payload = payload
        self.status_code = status_code
        self.headers: dict[str, str] = {}

    def json(self):
        return self._payload


class _FakeTcgdex:
    """Stand-in for every outbound TCGdex request the matcher makes.

    Records the peak number of requests in flight at once, which is how the
    per-claim request gate is measured.
    """

    def __init__(self, *, cards: list[dict], hold: float = 0.0):
        self.cards = cards
        self.hold = hold
        self.in_flight = 0
        self.peak_in_flight = 0
        self.search_calls = 0
        self.detail_calls = 0
        self.image_calls = 0

    def client_factory(self):
        fake = self

        class _FakeAsyncClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, *exc_info):
                return False

            async def get(self, url, **kwargs):
                if "/cards/" in url:
                    fake.detail_calls += 1
                    async with fake._flight():
                        return _FakeResponse({
                            "illustrator": "Mitsuhiro Arita",
                            "hp": "60",
                            "regulationMark": "F",
                            "set": {"cardCount": {"official": 102}},
                        })
                fake.search_calls += 1
                async with fake._flight():
                    return _FakeResponse(fake.cards)

            def stream(self, method, url, **kwargs):
                fake.image_calls += 1
                return fake._image_stream()

        return _FakeAsyncClient

    def _flight(self):
        fake = self

        class _Flight:
            async def __aenter__(self):
                fake.in_flight += 1
                fake.peak_in_flight = max(fake.peak_in_flight, fake.in_flight)
                await asyncio.sleep(fake.hold)
                return self

            async def __aexit__(self, *exc_info):
                fake.in_flight -= 1
                return False

        return _Flight()

    def _image_stream(self):
        fake = self

        class _Stream:
            async def __aenter__(self):
                fake.in_flight += 1
                fake.peak_in_flight = max(fake.peak_in_flight, fake.in_flight)
                await asyncio.sleep(fake.hold)
                return _FakeStreamResponse(_jpeg("purple", (60, 84)))

            async def __aexit__(self, *exc_info):
                fake.in_flight -= 1
                return False

        return _Stream()


def _tcgdex_cards(count: int) -> list[dict]:
    return [
        {
            "id": f"base1-{index + 1}",
            "name": "Pikachu",
            "set": {"name": "Base"},
            "localId": str(index + 1),
            "image": f"https://assets.tcgdex.net/en/base/base1/{index + 1}",
            "rarity": "Common",
        }
        for index in range(count)
    ]


class _CompositeClaimHarness(unittest.IsolatedAsyncioTestCase):
    """A real engine, real schema and a real composite claim to process."""

    pool_size = 1
    max_overflow = 4
    pool_timeout = 5

    def setUp(self):
        self.temp_dir = tempfile.TemporaryDirectory()
        self.env = patch.dict(os.environ, {"SCAN_UPLOAD_DIR": self.temp_dir.name})
        self.env.start()
        # Diagnostics write files and read a UserSetting row; neither belongs in
        # a connection count, so keep them off exactly as a default install has.
        os.environ.pop("SCAN_TRACE_DIR", None)
        os.environ.pop("SCAN_TRACE_STORAGE_DIR", None)

        if POSTGRES_TEST_ENABLED:
            self.engine = create_engine(
                os.environ["DATABASE_URL"],
                poolclass=QueuePool,
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_timeout=self.pool_timeout,
            )
        else:
            self.engine = create_engine(
                "sqlite:///" + os.path.join(self.temp_dir.name, "pool.sqlite"),
                connect_args={"check_same_thread": False},
                poolclass=QueuePool,
                pool_size=self.pool_size,
                max_overflow=self.max_overflow,
                pool_timeout=self.pool_timeout,
            )
        self.Session = sessionmaker(bind=self.engine)
        self._reset_schema()

    def tearDown(self):
        Base.metadata.drop_all(self.engine)
        self.engine.dispose()
        self.env.stop()
        self.temp_dir.cleanup()

    def _reset_schema(self):
        Base.metadata.drop_all(self.engine)
        Base.metadata.create_all(self.engine)

    def _seed(self, *, positions: int, batch_mode: bool = True) -> None:
        db = self.Session()
        try:
            user = User(username="composite-owner", hashed_password="x")
            db.add(user)
            db.flush()
            now = datetime.datetime.utcnow()
            job = ScanJob(
                user_id=user.id,
                status="pending",
                created_at=now,
                updated_at=now,
                expires_at=now + datetime.timedelta(days=14),
            )
            db.add(job)
            db.flush()
            db.add(ScanQueueUserState(user_id=user.id))
            job_dir = scan_storage.scan_upload_root() / str(job.id)
            job_dir.mkdir(parents=True, exist_ok=True)
            for position in range(positions):
                photo = _jpeg(["red", "blue", "green", "yellow"][position % 4])
                (job_dir / f"{position}.jpg").write_bytes(photo)
                db.add(ScanJobItem(
                    job_id=job.id,
                    user_id=user.id,
                    position=position,
                    image_path=f"{job.id}/{position}.jpg",
                    content_type="image/jpeg",
                    byte_size=len(photo),
                    status="pending",
                    batch_mode=batch_mode,
                    resolved=False,
                    attempts=0,
                    transient_failures=0,
                    next_attempt_at=now,
                    created_at=now,
                    updated_at=now,
                ))
            # Local rows so the matcher's two synchronous reads hit real data
            # rather than returning empty and skipping the work under test.
            db.add(Set(
                id="base1_en",
                tcg_set_id="base1",
                name="Base Set",
                lang="en",
                printed_total=102,
            ))
            for index in range(10):
                db.add(Card(
                    id=f"base1-{index + 1}_en",
                    tcg_card_id=f"base1-{index + 1}",
                    name="Pikachu",
                    set_id="base1",
                    number=str(index + 1),
                ))
            db.commit()
            self.user_id = user.id
        finally:
            db.close()

    def _claim(self):
        db = self.Session()
        try:
            claim = claim_next_scan_item(db)
        finally:
            db.close()
        self.assertIsNotNone(claim)
        return claim

    def _item_statuses(self) -> list[str]:
        db = self.Session()
        try:
            return [
                row[0]
                for row in db.query(ScanJobItem.status)
                .order_by(ScanJobItem.position)
                .all()
            ]
        finally:
            db.close()

    def _recognized(self, positions: int) -> dict[int, dict]:
        return {
            position: {
                "name": "Pikachu",
                "number_local": "25",
                "language": "en",
                "artist": "Mitsuhiro Arita",
            }
            for position in range(positions)
        }

    async def _process_claim(self, *, positions, probe=None, matcher_session_factory=None):
        """Process one composite claim through the real matcher.

        matcher_session_factory reintroduces the rejected design: when supplied,
        each matcher gets its own Session instead of the coordinator's.
        """
        self._reset_schema()
        self._seed(positions=positions)
        claim = self._claim()
        self.assertTrue(claim.composite)

        fake = _FakeTcgdex(cards=_tcgdex_cards(10))
        real = _real_matcher()

        async def matcher(db, card_info, **kwargs):
            if probe is not None:
                probe.arm()
            if matcher_session_factory is None:
                return await real(db, card_info, **kwargs)
            private = matcher_session_factory(db)
            try:
                return await real(private, card_info, **kwargs)
            finally:
                private.close()

        with patch("database.SessionLocal", self.Session), \
                patch("api.recognize.httpx.AsyncClient", fake.client_factory()), \
                patch("api.recognize.get_gemini_key", return_value="test-key"), \
                patch(
                    "api.recognize.recognize_composite_card_info",
                    new=AsyncMock(return_value=self._recognized(positions)),
                ), \
                patch("api.recognize.match_composite_card_info", new=matcher):
            await process_claimed_scan_item(claim)
        return fake


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CompositeClaimConnectionTests(_CompositeClaimHarness):
    """Acceptance 1 and 2: connection usage, and no checkout inside the loop."""

    async def test_composite_claim_uses_one_pooled_connection_at_a_time(self):
        with _PoolProbe(self.engine) as probe:
            fake = await self._process_claim(positions=4, probe=probe)

        print(
            "COMPOSITE_POOL_METRICS positions=4 "
            f"total={probe.total_checkouts} peak={probe.peak} "
            f"distinct={len(probe.connections)} during_match={probe.checkouts_while_armed}"
        )
        self.assertEqual(fake.search_calls, 4, "the real matcher did not run for every position")
        self.assertNotIn("retrying", self._item_statuses())
        self.assertEqual(probe.peak, 1, "more than one connection was held at once")
        self.assertEqual(len(probe.connections), 1, "the claim used more than one connection")
        self.assertEqual(
            probe.checkouts_while_armed,
            0,
            "a connection was checked out from inside the event loop while matching",
        )

    async def test_connection_usage_does_not_grow_with_matched_positions(self):
        with _PoolProbe(self.engine) as two:
            await self._process_claim(positions=2, probe=two)
        with _PoolProbe(self.engine) as four:
            await self._process_claim(positions=4, probe=four)

        print(
            f"COMPOSITE_POOL_METRICS two_positions={two.total_checkouts} "
            f"four_positions={four.total_checkouts}"
        )
        self.assertGreater(two.total_checkouts, 0)
        self.assertEqual(
            two.total_checkouts,
            four.total_checkouts,
            "connection usage scaled with the number of concurrently matched positions",
        )

    async def test_negative_control_a_private_matcher_session_costs_more_connections(self):
        with _PoolProbe(self.engine) as probe:
            await self._process_claim(
                positions=4,
                probe=probe,
                matcher_session_factory=lambda db: Session(bind=db.get_bind(), autoflush=False),
            )

        print(
            "COMPOSITE_POOL_METRICS negative_control "
            f"total={probe.total_checkouts} peak={probe.peak} "
            f"distinct={len(probe.connections)} during_match={probe.checkouts_while_armed}"
        )
        self.assertGreater(
            probe.peak,
            1,
            "the probe cannot see extra connections, so the positive test proves nothing",
        )
        self.assertGreater(probe.checkouts_while_armed, 0)


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CompositeClaimSingleConnectionPoolTests(_CompositeClaimHarness):
    """The claim must still succeed when the pool has exactly one connection."""

    pool_size = 1
    max_overflow = 0
    pool_timeout = 2

    async def test_claim_completes_on_a_pool_of_exactly_one_connection(self):
        fake = await self._process_claim(positions=4)

        statuses = self._item_statuses()
        print(f"COMPOSITE_SINGLE_CONNECTION_POOL statuses={statuses}")
        self.assertEqual(fake.search_calls, 4, "the real matcher did not run for every position")
        self.assertNotIn("retrying", statuses)
        self.assertNotIn("failed", statuses)

    async def test_negative_control_private_sessions_exhaust_the_pool(self):
        # The rejected design, on the same pool: each matcher waits, from inside
        # the event loop, for a connection the coordinator is still holding.
        await self._process_claim(
            positions=4,
            matcher_session_factory=lambda db: Session(bind=db.get_bind(), autoflush=False),
        )

        statuses = self._item_statuses()
        print(f"COMPOSITE_SINGLE_CONNECTION_POOL negative_control statuses={statuses}")
        self.assertTrue(
            any(status in {"retrying", "failed"} for status in statuses),
            "a private matcher session did not exhaust a one-connection pool, "
            "so the positive test proves nothing",
        )


class _OverlapTracker:
    def __init__(self):
        self.in_flight = 0
        self.peak = 0
        self.entry_order: list[int] = []

    async def matcher(self, db, card_info, *, photo_bytes=None, trace=None, **kwargs):
        position = int(card_info["name"].removeprefix("Card"))
        self.entry_order.append(position)
        self.in_flight += 1
        self.peak = max(self.peak, self.in_flight)
        try:
            # Two hops, so a third matcher has a real chance to slip in if the
            # bound is not doing its job.
            await asyncio.sleep(0.02)
            await asyncio.sleep(0)
            return {
                "_identity_confident": True,
                "recognized": card_info,
                "matches": [{"id": f"card-{position}"}],
            }
        finally:
            self.in_flight -= 1


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CompositeMatchOverlapTests(unittest.IsolatedAsyncioTestCase):
    """Acceptance 3, 4 and 5: overlap, ordering and deterministic failure."""

    def setUp(self):
        self.db = _mock_db(
            User(id=1, username="composite-owner", hashed_password="x", is_active=True)
        )
        self.photos = [_jpeg(colour) for colour in ("red", "blue", "green", "yellow")]

    def _recognized(self, count=4):
        return {
            position: {"name": f"Card{position}", "number_local": "25", "language": "en"}
            for position in range(count)
        }

    async def _process(self, matcher, recognized, images=None):
        images = images or self.photos
        with patch("api.recognize.get_gemini_key", return_value="test-key"), \
                patch(
                    "api.recognize.recognize_composite_card_info",
                    new=AsyncMock(return_value=recognized),
                ), \
                patch("api.recognize.match_composite_card_info", new=matcher):
            return await scan_queue.default_composite_processor(
                self.db,
                1,
                images,
                ["image/jpeg"] * len(images),
            )

    async def test_two_positions_overlap_and_a_third_waits(self):
        tracker = _OverlapTracker()
        results = await self._process(tracker.matcher, self._recognized(4))

        print(
            f"COMPOSITE_OVERLAP peak_in_flight={tracker.peak} "
            f"order={tracker.entry_order} bound={scan_queue.COMPOSITE_MATCH_CONCURRENCY}"
        )
        self.assertEqual(len(results), 4)
        self.assertEqual(tracker.peak, 2, "positions did not overlap exactly two at a time")
        self.assertEqual(scan_queue.COMPOSITE_MATCH_CONCURRENCY, 2)
        self.assertEqual(tracker.entry_order, [0, 1, 2, 3])

    async def test_negative_control_bounding_at_one_removes_the_overlap(self):
        tracker = _OverlapTracker()
        with patch.object(scan_queue, "COMPOSITE_MATCH_CONCURRENCY", 1):
            await self._process(tracker.matcher, self._recognized(4))

        print(f"COMPOSITE_OVERLAP negative_control peak_in_flight={tracker.peak}")
        self.assertEqual(
            tracker.peak,
            1,
            "the tracker reports overlap the bound should have prevented",
        )

    async def test_results_land_by_source_position_not_completion_order(self):
        # Position 0 holds its slot far longer than the rest, so it completes
        # last while the positions behind it stream through ahead of it.
        delays = {0: 0.08, 1: 0.0, 2: 0.0, 3: 0.0}
        completion: list[int] = []

        async def matcher(db, card_info, *, photo_bytes=None, trace=None, **kwargs):
            position = int(card_info["name"].removeprefix("Card"))
            await asyncio.sleep(delays[position])
            completion.append(position)
            return {
                "_identity_confident": True,
                "recognized": card_info,
                "matches": [{"id": f"card-{position}"}],
            }

        results = await self._process(matcher, self._recognized(4))

        print(f"COMPOSITE_ORDERING completion={completion}")
        self.assertNotEqual(completion, [0, 1, 2, 3], "completion order was not shuffled")
        self.assertEqual(
            [result["matches"][0]["id"] for result in results],
            ["card-0", "card-1", "card-2", "card-3"],
        )

    async def test_negative_control_reshuffling_completion_keeps_the_same_mapping(self):
        for delays in (
            {0: 0.0, 1: 0.04, 2: 0.01, 3: 0.03},
            {0: 0.03, 1: 0.0, 2: 0.04, 3: 0.01},
        ):
            completion: list[int] = []

            async def matcher(db, card_info, *, photo_bytes=None, trace=None, _d=delays, **kwargs):
                position = int(card_info["name"].removeprefix("Card"))
                await asyncio.sleep(_d[position])
                completion.append(position)
                return {
                    "_identity_confident": True,
                    "recognized": card_info,
                    "matches": [{"id": f"card-{position}"}],
                }

            results = await self._process(matcher, self._recognized(4))
            print(f"COMPOSITE_ORDERING negative_control completion={completion}")
            self.assertEqual(
                [result["matches"][0]["id"] for result in results],
                ["card-0", "card-1", "card-2", "card-3"],
            )

    def _raising_matcher(self, *, first_to_fail: int):
        """Position 1 raises a 400, position 2 a 429; first_to_fail lands first."""
        errors = {
            1: HTTPException(status_code=400, detail="permanent"),
            2: HTTPException(status_code=429, detail="transient"),
        }
        second = 2 if first_to_fail == 1 else 1
        delays = {first_to_fail: 0.0, second: 0.05}
        raised: list[int] = []

        async def matcher(db, card_info, *, photo_bytes=None, trace=None, **kwargs):
            position = int(card_info["name"].removeprefix("Card"))
            if position in errors:
                await asyncio.sleep(delays[position])
                raised.append(position)
                raise errors[position]
            await asyncio.sleep(0)
            return {
                "_identity_confident": True,
                "recognized": card_info,
                "matches": [{"id": f"card-{position}"}],
            }

        return matcher, raised

    async def test_the_lowest_positions_exception_decides_the_outcome(self):
        for first_to_fail in (1, 2):
            matcher, raised = self._raising_matcher(first_to_fail=first_to_fail)
            with self.assertRaises(HTTPException) as caught:
                await self._process(matcher, self._recognized(4))

            print(
                f"COMPOSITE_ERROR first_to_fail={first_to_fail} raised_order={raised} "
                f"propagated={caught.exception.status_code}"
            )
            self.assertEqual(raised[0], first_to_fail, "the intended failure did not land first")
            self.assertEqual(
                caught.exception.status_code,
                400,
                "the propagated failure was not the lowest position's",
            )


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class IndividualClaimTests(_CompositeClaimHarness):
    """Negative control for the overlap: one photo still means one matcher."""

    async def test_an_individual_claim_runs_exactly_one_matcher(self):
        self._seed(positions=1, batch_mode=False)
        claim = self._claim()
        self.assertFalse(claim.composite)

        calls: list[dict] = []

        async def matcher(db, card_info, **kwargs):
            calls.append(card_info)
            await asyncio.sleep(0)
            return {
                "recognized": card_info,
                "matches": [{"id": "card-25"}],
                "_number_match_count": 1,
                "_identity_confident": True,
            }

        generate = AsyncMock(return_value=('{"name": "Pikachu", "number": "25"}', None))
        composite = AsyncMock(side_effect=AssertionError("the composite path was used"))
        with patch("database.SessionLocal", self.Session), \
                patch("api.recognize.get_gemini_key", return_value="test-key"), \
                patch("services.scan_providers.ScanProvider.generate_text", new=generate), \
                patch("api.recognize.recognize_composite_card_info", new=composite), \
                patch("api.recognize.match_card_info", new=matcher):
            await process_claimed_scan_item(claim)

        statuses = self._item_statuses()
        print(f"INDIVIDUAL_CLAIM matchers={len(calls)} vision={generate.await_count} statuses={statuses}")
        self.assertEqual(len(calls), 1)
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(composite.await_count, 0)
        self.assertEqual(statuses, ["done"])


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CompositeFailureStatusTests(_CompositeClaimHarness):
    """The item's terminal status must not depend on which failure lands first."""

    async def _run(self, *, first_to_fail: int) -> list[str]:
        self._reset_schema()
        self._seed(positions=4)
        claim = self._claim()

        errors = {
            1: HTTPException(status_code=400, detail="permanent"),
            2: HTTPException(status_code=429, detail="transient"),
        }
        second = 2 if first_to_fail == 1 else 1
        delays = {first_to_fail: 0.0, second: 0.05}
        raised: list[int] = []

        async def matcher(db, card_info, **kwargs):
            position = int(card_info["name"].removeprefix("Card"))
            if position in errors:
                await asyncio.sleep(delays[position])
                raised.append(position)
                raise errors[position]
            await asyncio.sleep(0)
            return {
                "_identity_confident": True,
                "recognized": card_info,
                "matches": [{"id": f"card-{position}"}],
            }

        recognized = {
            position: {"name": f"Card{position}", "number_local": "25", "language": "en"}
            for position in range(4)
        }
        with patch("database.SessionLocal", self.Session), \
                patch("api.recognize.get_gemini_key", return_value="test-key"), \
                patch(
                    "api.recognize.recognize_composite_card_info",
                    new=AsyncMock(return_value=recognized),
                ), \
                patch("api.recognize.match_composite_card_info", new=matcher):
            await process_claimed_scan_item(claim)

        statuses = self._item_statuses()
        print(
            f"COMPOSITE_FAILURE_STATUS first_to_fail={first_to_fail} "
            f"raised_order={raised} statuses={statuses}"
        )
        self.assertEqual(raised[0], first_to_fail, "the intended failure did not land first")
        return statuses

    async def test_swapping_which_failure_lands_first_does_not_change_the_status(self):
        # A 400 is permanent and a 429 transient. Position 1 carries the 400, so
        # every item must end "failed" whichever exception arrives first.
        lowest_first = await self._run(first_to_fail=1)
        highest_first = await self._run(first_to_fail=2)

        self.assertEqual(lowest_first, ["failed"] * 4)
        self.assertEqual(highest_first, ["failed"] * 4)


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CompositeVisionCallTests(unittest.IsolatedAsyncioTestCase):
    """Acceptance 6: exactly one paid extraction per composite claim."""

    def setUp(self):
        self.db = _mock_db(
            User(id=1, username="composite-owner", hashed_password="x", is_active=True)
        )

    async def _run(self, recognized_rows: str, positions: int):
        generate = AsyncMock(return_value=(recognized_rows, None))
        fake = _FakeTcgdex(cards=[])
        photos = [_jpeg(colour) for colour in ("red", "blue", "green", "yellow")][:positions]
        with patch("api.recognize.get_gemini_key", return_value="test-key"), \
                patch("api.recognize.httpx.AsyncClient", fake.client_factory()), \
                patch("services.scan_providers.ScanProvider.generate_text", new=generate):
            results = await scan_queue.default_composite_processor(
                self.db,
                1,
                photos,
                ["image/jpeg"] * positions,
            )
        return generate, results, fake

    async def test_one_vision_call_covers_every_position(self):
        rows = (
            '[{"index": 1, "name": "Pikachu"}, {"index": 2, "name": "Eevee"},'
            ' {"index": 3, "name": "Snorlax"}, {"index": 4, "name": "Mew"}]'
        )
        generate, results, fake = await self._run(rows, 4)

        print(
            f"COMPOSITE_VISION calls={generate.await_count} "
            f"searches={fake.search_calls} results={[bool(r) for r in results]}"
        )
        self.assertEqual(generate.await_count, 1)
        self.assertEqual(fake.search_calls, 4, "a named position was not matched")

    async def test_negative_control_an_unnamed_position_falls_back_without_extra_work(self):
        rows = (
            '[{"index": 1, "name": "Pikachu"}, {"index": 2, "name": ""},'
            ' {"index": 3, "name": "Snorlax"}, {"index": 4, "name": "Mew"}]'
        )
        generate, results, fake = await self._run(rows, 4)

        print(
            f"COMPOSITE_VISION negative_control calls={generate.await_count} "
            f"searches={fake.search_calls} results={[bool(r) for r in results]}"
        )
        self.assertEqual(generate.await_count, 1, "the fallback added a paid call")
        self.assertEqual(fake.search_calls, 3, "the unnamed position was matched anyway")
        self.assertIsNone(results[1])

    async def test_code_and_number_without_a_name_reaches_the_composite_matcher(self):
        rows = (
            '[{"index": 1, "name": "", "set_code": "SVI", "number_local": "25"},'
            ' {"index": 2, "name": "Pikachu"}]'
        )
        generate = AsyncMock(return_value=(rows, None))
        matched = []

        async def matcher(_db, card_info, **_kwargs):
            matched.append(card_info)
            return {
                "recognized": card_info,
                "matches": [{"id": "sv1-025_en"}],
                "_identity_confident": True,
            }

        with patch("api.recognize.get_gemini_key", return_value="test-key"), \
                patch("services.scan_providers.ScanProvider.generate_text", new=generate), \
                patch("api.recognize.match_composite_card_info", new=matcher):
            results = await scan_queue.default_composite_processor(
                self.db,
                1,
                [_jpeg("red"), _jpeg("blue")],
                ["image/jpeg", "image/jpeg"],
            )

        self.assertEqual(matched[0], {"index": 1, "name": "", "set_code": "SVI", "number_local": "25", "number": "25", "number_total": None})
        self.assertEqual(results[0]["matches"][0]["id"], "sv1-025_en")


@unittest.skipUnless(DEPS_AVAILABLE, "SQLAlchemy is not installed")
class CompositeRequestGateTests(unittest.IsolatedAsyncioTestCase):
    """Regression coverage for shared request-gate propagation with a fake client."""

    def setUp(self):
        self.db = _mock_db(
            User(id=1, username="composite-owner", hashed_password="x", is_active=True)
        )
        self.photos = [_jpeg(colour) for colour in ("red", "blue")]

    async def _run(self, *, gated: bool):
        recognized = {
            position: {
                "name": f"Card{position}",
                "number_local": "25",
                "language": "en",
                "artist": "Mitsuhiro Arita",
            }
            for position in range(2)
        }
        fake = _FakeTcgdex(cards=_tcgdex_cards(10), hold=0.02)
        real = _real_matcher()

        async def matcher(db, card_info, **kwargs):
            if not gated:
                kwargs.pop("request_gate", None)
            return await real(db, card_info, **kwargs)

        with patch("api.recognize.get_gemini_key", return_value="test-key"), \
                patch("api.recognize.httpx.AsyncClient", fake.client_factory()), \
                patch(
                    "api.recognize.recognize_composite_card_info",
                    new=AsyncMock(return_value=recognized),
                ), \
                patch("api.recognize.match_composite_card_info", new=matcher):
            await scan_queue.default_composite_processor(
                self.db,
                1,
                self.photos,
                ["image/jpeg"] * 2,
            )
        return fake

    async def test_two_matchers_never_exceed_one_matchers_burst(self):
        from api.recognize import TCGDEX_REQUEST_BURST

        fake = await self._run(gated=True)

        print(
            f"COMPOSITE_GATE peak_in_flight={fake.peak_in_flight} "
            f"burst={TCGDEX_REQUEST_BURST} details={fake.detail_calls} images={fake.image_calls}"
        )
        self.assertGreater(fake.detail_calls, 0)
        self.assertLessEqual(fake.peak_in_flight, TCGDEX_REQUEST_BURST)

    async def test_negative_control_without_the_gate_the_burst_doubles(self):
        from api.recognize import TCGDEX_REQUEST_BURST

        fake = await self._run(gated=False)

        print(f"COMPOSITE_GATE negative_control peak_in_flight={fake.peak_in_flight}")
        self.assertGreater(
            fake.peak_in_flight,
            TCGDEX_REQUEST_BURST,
            "the fake cannot observe an ungated burst, so the positive test proves nothing",
        )

if __name__ == "__main__":
    unittest.main()
