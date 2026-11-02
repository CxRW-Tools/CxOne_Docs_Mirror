"""Mirror stage: HTTP 429 handling and the failure threshold.

    python -m unittest discover -s tests -v
"""

import asyncio
import sys
import unittest
from pathlib import Path
from types import SimpleNamespace
from unittest import mock

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

import cx_docs_mirror as D  # noqa: E402


class FakeResponse:
    def __init__(self, status, headers=None):
        self.status_code = status
        self.headers = headers or {}


class FakeClient:
    """Returns the queued responses in order, then 200."""

    def __init__(self, *statuses):
        self.queue = [s if isinstance(s, FakeResponse) else FakeResponse(s) for s in statuses]
        self.calls = 0

    async def get(self, url, headers=None):
        self.calls += 1
        return self.queue.pop(0) if self.queue else FakeResponse(200)


def cfg(**kw):
    base = dict(retries=3, rate={"until": 0.0, "extra": 0.0}, max_failure_rate=0.02)
    base.update(kw)
    return SimpleNamespace(**base)


class RateLimit(unittest.TestCase):
    def get(self, client, config):
        sleeps = []

        async def fake_sleep(seconds):
            sleeps.append(seconds)

        with mock.patch.object(D.asyncio, "sleep", fake_sleep):
            r = asyncio.run(D._http_get(client, "https://x.test/a.html", config, {}))
        return r, sleeps

    def test_retry_after_is_honoured_and_pauses_everyone(self):
        config = cfg()
        client = FakeClient(FakeResponse(429, {"retry-after": "42"}))
        r, sleeps = self.get(client, config)
        self.assertEqual(r.status_code, 200)
        self.assertEqual(client.calls, 2)
        self.assertEqual(round(config.rate["extra"], 2), 0.25)          # later requests slow down a little
        self.assertTrue(any(40 <= s <= 42.5 for s in sleeps), sleeps)    # the worker waited out the pause

    def test_without_retry_after_the_backoff_grows(self):
        config = cfg()
        _, sleeps = self.get(FakeClient(429, 429, 429), config)
        waits = [s for s in sleeps if s >= 10]
        self.assertEqual([round(w, -1) for w in waits[:3]], [10, 30, 60])   # ~15s, 30s, 60s (less time already spent)

    def test_gives_up_after_the_rate_limit_budget(self):
        client = FakeClient(*[429] * (D.RATE_LIMIT_RETRIES + 5))
        r, _ = self.get(client, cfg())
        self.assertIsNone(r)
        self.assertEqual(client.calls, D.RATE_LIMIT_RETRIES + 1)

    def test_other_errors_keep_the_short_retry_budget(self):
        client = FakeClient(500, 500, 500, 500)
        r, sleeps = self.get(client, cfg(retries=3))
        self.assertIsNone(r)
        self.assertEqual(client.calls, 3)
        self.assertEqual(sleeps, [2, 4])

    def test_404_is_final(self):
        client = FakeClient(404)
        r, _ = self.get(client, cfg())
        self.assertEqual((r.status_code, client.calls), (404, 1))

    def test_retry_after_parsing(self):
        self.assertEqual(D._retry_after(FakeResponse(429, {"retry-after": "7"})), 7.0)
        self.assertEqual(D._retry_after(FakeResponse(429, {"retry-after": "99999"})), 300.0)
        self.assertIsNone(D._retry_after(FakeResponse(429, {"retry-after": "Wed, 21 Oct 2026 07:28:00 GMT"})))
        self.assertIsNone(D._retry_after(FakeResponse(429)))


class FailureThreshold(unittest.TestCase):
    def test_below_and_above_the_limit(self):
        self.assertIsNone(D._failure_problem(["u"] * 14, 709, cfg()))          # 1.97%
        message = D._failure_problem(["u"] * 25, 709, cfg())                    # 3.5%
        self.assertIn("25 of 709", message)
        self.assertIn("--max-failure-rate", message)

    def test_can_be_disabled(self):
        self.assertIsNone(D._failure_problem(["u"] * 700, 709, cfg(max_failure_rate=1.0)))
        self.assertIsNone(D._failure_problem(["u"], 709, cfg(max_failure_rate=None)))

    def test_nothing_to_check_is_fine(self):
        self.assertIsNone(D._failure_problem([], 0, cfg()))


if __name__ == "__main__":
    unittest.main()
