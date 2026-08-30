"""The provider call has to outlast inference, and say what really happened.

A live incident: scans reported "The scanner endpoint could not be reached"
and retried repeatedly, while the endpoint answered a plain request from the
same container in under a quarter of a second. One 30 second budget covered
connecting, uploading megabytes of base64 image AND waiting for a vision model
to read a card. The model was thinking, not missing.
"""
import unittest

try:
    import httpx
    from fastapi import HTTPException

    from api.recognize import recognition_timeout
    from services.scan_providers import OPENAI, ScanProvider

    DEPS_AVAILABLE = True
except ModuleNotFoundError:
    DEPS_AVAILABLE = False


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class RecognitionTimeoutTests(unittest.TestCase):
    def test_connecting_stays_short_so_an_unreachable_host_fails_fast(self):
        # The half that must NOT be generous: a host that is genuinely down
        # should be reported quickly rather than after minutes of waiting.
        timeout = recognition_timeout()
        self.assertLessEqual(timeout.connect, 15.0)

    def test_reading_is_generous_because_that_is_the_part_that_thinks(self):
        timeout = recognition_timeout()
        self.assertGreaterEqual(timeout.read, 120.0)
        self.assertGreater(timeout.read, timeout.connect * 5)

    def test_writing_allows_a_large_image_to_be_sent(self):
        # A composite is several megabytes of base64 on a domestic uplink.
        self.assertGreaterEqual(recognition_timeout().write, 60.0)


@unittest.skipUnless(DEPS_AVAILABLE, "Scanner dependencies are not installed")
class TimeoutReportingTests(unittest.IsolatedAsyncioTestCase):
    class _TimingOutClient:
        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            raise httpx.ReadTimeout("timed out")

    class _UnreachableClient:
        def __init__(self):
            self.calls = 0

        async def post(self, *args, **kwargs):
            self.calls += 1
            raise httpx.ConnectError("no route to host")

    async def _detail(self, client):
        provider = ScanProvider(OPENAI)
        with self.assertRaises(HTTPException) as caught:
            await provider.generate_text(client, "key", [{"text": "hi"}])
        return caught.exception.detail

    async def test_a_slow_answer_is_reported_as_slow_not_unreachable(self):
        detail = await self._detail(self._TimingOutClient())
        self.assertIn("too long", detail)
        self.assertNotIn("could not be reached", detail)

    async def test_a_genuinely_unreachable_host_still_says_so(self):
        # The bystander: reporting everything as a timeout would hide a real
        # outage behind a message telling people to wait.
        detail = await self._detail(self._UnreachableClient())
        self.assertIn("could not be reached", detail)
