"""Check that collect tells "does not exist" from "could not check".

No network: urlopen is replaced by something that answers every request the
same way. What render does with the result is test_render.py's business.
"""

import contextlib
import email.message
import io
import os
import sys
import unittest
import urllib.error
from unittest import mock

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import build


def refusing(code, headers=None, body=b"{}"):
    """A urlopen that answers everything with one HTTP error, and counts."""
    message = email.message.Message()
    for key, value in (headers or {}).items():
        message[key] = value

    def urlopen(req, timeout=None):
        urlopen.calls += 1
        raise urllib.error.HTTPError(
            req.full_url, code, "refused", message, io.BytesIO(body)
        )

    urlopen.calls = 0
    return urlopen


@contextlib.contextmanager
def offline(urlopen):
    """build.py with no network, no waiting between retries, and no chatter."""
    with (
        mock.patch.object(build.urllib.request, "urlopen", urlopen),
        mock.patch.object(build, "time"),
        mock.patch.object(build.sys, "stderr"),
    ):
        yield


class Fetch(unittest.TestCase):
    def ask(self, code, headers=None, body=b"{}"):
        """(what api() returned, how many requests it made)."""
        urlopen = refusing(code, headers, body)
        with offline(urlopen):
            return build.api("/repos/example/thing", "token"), urlopen.calls

    def why(self, *args, **kwargs):
        with self.assertRaises(build.CouldNotCheck) as caught:
            self.ask(*args, **kwargs)
        return str(caught.exception)

    def test_missing_is_an_answer(self):
        self.assertEqual(self.ask(404), (None, 1))

    def test_rate_limits(self):
        self.assertEqual(self.why(429), "rate limited")
        self.assertEqual(self.why(403, {"X-RateLimit-Remaining": "0"}), "rate limited")
        self.assertEqual(self.why(403, {"Retry-After": "60"}), "rate limited")
        body = b'{"message": "You have exceeded a secondary rate limit."}'
        self.assertEqual(self.why(403, body=body), "rate limited")

    def test_other_refusals(self):
        self.assertEqual(self.why(403), "refused (HTTP 403)")
        self.assertEqual(self.why(451), "refused (HTTP 451)")

    def test_server_errors_are_retried_then_reported(self):
        urlopen = refusing(502)
        with offline(urlopen), self.assertRaises(build.CouldNotCheck) as caught:
            build.api("/repos/example/thing", "token")
        self.assertEqual(str(caught.exception), "server error (HTTP 502)")
        self.assertEqual(urlopen.calls, 3)

    def test_network_failure(self):
        def urlopen(req, timeout=None):
            raise urllib.error.URLError("no route to host")

        with offline(urlopen), self.assertRaises(build.CouldNotCheck) as caught:
            build.api("/repos/example/thing", "token")
        self.assertEqual(str(caught.exception), "network error")

    def test_a_bad_token_still_stops_the_build(self):
        with self.assertRaises(urllib.error.HTTPError) as caught:
            self.ask(401)
        caught.exception.close()


class Collect(unittest.TestCase):
    def collect(self, code, headers=None, only=None):
        with offline(refusing(code, headers)):
            return build.collect("example", "token", only)

    def test_rate_limited_repositories_get_a_row_that_says_so(self):
        names = ["example/one", "example/two"]
        repos, unchecked = self.collect(403, {"X-RateLimit-Remaining": "0"}, names)
        self.assertEqual([r["name"] for r in repos], names)
        self.assertEqual({r["unchecked"] for r in repos}, {"rate limited"})
        self.assertEqual({r["health"] for r in repos}, {"unknown"})
        self.assertEqual(unchecked, [{"what": "APT repository", "why": "rate limited"}])

    def test_an_unreadable_listing_is_reported(self):
        repos, unchecked = self.collect(429)
        self.assertIn({"what": "repository list", "why": "rate limited"}, unchecked)
        # Only the repositories named in the file are left to ask about.
        self.assertEqual([r["name"] for r in repos], sorted(build.EXTRA_REPOS))
        self.assertTrue(all(r["unchecked"] for r in repos))

    def test_missing_repositories_get_no_row(self):
        repos, unchecked = self.collect(404, only=["example/gone"])
        self.assertEqual((repos, unchecked), ([], []))


if __name__ == "__main__":
    unittest.main()
