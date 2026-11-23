"""Check that collect tells "does not exist" from "could not check".

No network: urlopen is replaced by something that answers every request the
same way. What render does with the result is test_render.py's business.
"""

import base64
import contextlib
import email.message
import io
import json
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


def serving(routes):
    """A urlopen that answers by URL substring, 404 for the rest, and counts."""

    def urlopen(req, timeout=None):
        urlopen.calls.append(req.full_url)
        for part, reply in routes.items():
            if part in req.full_url:
                return io.BytesIO(json.dumps(reply).encode())
        raise urllib.error.HTTPError(
            req.full_url, 404, "not found", email.message.Message(), io.BytesIO()
        )

    urlopen.calls = []
    return urlopen


def blob(text):
    return {"text": text, "isTruncated": False}


def run(**fields):
    return {
        "workflow_id": 1,
        "name": "CI",
        "status": "completed",
        "conclusion": "success",
        "html_url": "https://github.com/example/app/actions/runs/1",
        "created_at": "2026-01-02T00:00:00Z",
        "updated_at": "2026-01-02T00:05:00Z",
        "event": "push",
        "head_branch": "main",
        **fields,
    }


LOCK = '[[package]]\nname = "framer"\nversion = "0.4.1"\n'

# What GraphQL says about example/app: one pull request from a bot with a
# finished check run and a commit status, a Cargo project, one release.
APP = {
    "url": "https://github.com/example/app",
    "pushedAt": "2026-01-01T00:00:00Z",
    "defaultBranchRef": {"name": "main"},
    "pullRequests": {
        "nodes": [
            {
                "number": 3,
                "title": "Bump framer",
                "url": "https://github.com/example/app/pull/3",
                "isDraft": False,
                "updatedAt": "2026-01-02T00:00:00Z",
                "author": {"__typename": "Bot", "login": "dependabot"},
                "commits": {
                    "nodes": [
                        {
                            "commit": {
                                "statusCheckRollup": {
                                    "contexts": {
                                        "nodes": [
                                            {
                                                "__typename": "CheckRun",
                                                "status": "COMPLETED",
                                                "conclusion": "SUCCESS",
                                            },
                                            {"__typename": "StatusContext"},
                                        ]
                                    }
                                }
                            }
                        }
                    ]
                },
            }
        ]
    },
    "latestRelease": {
        "tagName": "v1.0.0",
        "tagCommit": {"f0": {"object": blob(LOCK)}, "f1": None, "f2": None},
    },
    "refs": {"nodes": [{"name": "v1.0.0"}]},
    "f0": blob(LOCK),
    "f1": None,
    "f2": None,
    "f3": blob('[package]\nname = "app"\nversion = "1.1.0"\n'),
    "f4": None,
}


def world(runs, total=None, **graphql):
    """Routes for a GitHub with example/app in it and no example/gone."""
    reply = {
        "data": {"rateLimit": {"cost": 2}, "r0": APP, "r1": None},
        "errors": [{"type": "NOT_FOUND", "path": ["r1"], "message": "no such"}],
        **graphql,
    }
    return {
        "/graphql": reply,
        "/actions/runs": {
            "total_count": len(runs) if total is None else total,
            "workflow_runs": runs,
        },
        "api.osv.dev/v1/querybatch": {"results": [{}]},
    }


class Collect(unittest.TestCase):
    NAMES = ("example/app", "example/gone")

    def setUp(self):
        build.SPENT.clear()

    def collect(self, urlopen, only=NAMES):
        with offline(urlopen):
            return build.collect("example", "token", only and list(only))

    def test_one_query_and_one_page_of_runs(self):
        dependabot = run(workflow_id=2, event="dynamic")
        urlopen = serving(world([run(), dependabot]))
        repos, unchecked = self.collect(urlopen)
        self.assertEqual(unchecked, [])
        # example/gone does not exist, which is an answer: no row.
        self.assertEqual([r["name"] for r in repos], ["example/app"])
        app = repos[0]
        self.assertIsNone(app["unchecked"])
        self.assertEqual(app["health"], "passing")
        self.assertEqual(
            [(r["name"], r["ref"], r["stale"]) for r in app["runs"]],
            [("CI", None, False)],
        )
        self.assertEqual(
            [(p["number"], p["author"], p["checks"]) for p in app["prs"]],
            [(3, "dependabot[bot]", "passing")],
        )
        self.assertEqual(app["advisories"]["locks"], ["Cargo.lock"])
        self.assertEqual(app["advisories"]["tag"], "v1.0.0")
        self.assertEqual(
            [
                (p["channel"], p["package"], p["published"], p["source"])
                for p in app["published"]
            ],
            [
                ("newest release", "v1.0.0", "", None),
                ("crates.io", "app", "not published", "1.1.0"),
            ],
        )
        # The budget. One query for both repositories and one page of runs,
        # which held every run so the default branch was not asked for again.
        # Raising these numbers means a full crawl got more expensive.
        self.assertEqual(build.SPENT["graphql"], 1)
        self.assertEqual(build.SPENT["rest"], 1)
        self.assertEqual(build.SPENT["points"], 2)
        # The APT index, OSV once for both refs, crates.io.
        self.assertEqual(build.SPENT["elsewhere"], 3)

    def test_more_runs_than_a_page_asks_for_the_branch(self):
        elsewhere = run(head_branch="feature", created_at="2026-01-03T00:00:00Z")
        urlopen = serving(world([elsewhere], total=build.RUNS_PAGE + 1))
        repos, _ = self.collect(urlopen)
        self.assertEqual(build.SPENT["rest"], 2)
        self.assertTrue(any("branch=main" in url for url in urlopen.calls))
        self.assertEqual(repos[0]["runs"][0]["ref"], "feature")

    def test_a_truncated_file_is_fetched_whole(self):
        routes = world([run()])
        routes["/graphql"]["data"]["r0"] = {
            **APP,
            "f0": {"text": LOCK[:10], "isTruncated": True},
        }
        whole = base64.b64encode(LOCK.encode()).decode()
        routes["/contents/Cargo.lock?ref=main"] = {"content": whole}
        repos, _ = self.collect(serving(routes))
        self.assertTrue(repos[0]["advisories"]["has_lock"])
        # Only that file: the other four came whole, or do not exist.
        self.assertEqual(build.SPENT["rest"], 2)

    def test_check_rollup(self):
        def checks(*pairs):
            return build.pr_checks(
                [
                    {
                        "__typename": "CheckRun",
                        "status": status,
                        "conclusion": conclusion,
                    }
                    for status, conclusion in pairs
                ]
            )

        self.assertEqual(checks(), "none")
        self.assertEqual(build.pr_checks([{"__typename": "StatusContext"}]), "none")
        self.assertEqual(
            checks(("COMPLETED", "SUCCESS"), ("IN_PROGRESS", None)), "running"
        )
        self.assertEqual(
            checks(("COMPLETED", "SUCCESS"), ("COMPLETED", "FAILURE")), "failing"
        )
        self.assertEqual(checks(("COMPLETED", "CANCELLED")), "failing")
        self.assertEqual(
            checks(("COMPLETED", "SKIPPED"), ("COMPLETED", "NEUTRAL")), "passing"
        )

    def test_a_graphql_error_spoils_the_batch(self):
        errors = [{"type": "RATE_LIMITED", "message": "API rate limit exceeded"}]
        repos, _ = self.collect(serving(world([run()], errors=errors)))
        self.assertEqual([r["name"] for r in repos], list(self.NAMES))
        self.assertEqual({r["unchecked"] for r in repos}, {"rate limited"})
        errors = [{"type": "FORBIDDEN", "path": ["r0", "refs"], "message": "no"}]
        repos, _ = self.collect(serving(world([run()], errors=errors)))
        self.assertEqual({r["unchecked"] for r in repos}, {"GraphQL error (FORBIDDEN)"})

    def test_rate_limited_repositories_get_a_row_that_says_so(self):
        urlopen = refusing(403, {"X-RateLimit-Remaining": "0"})
        repos, unchecked = self.collect(urlopen)
        self.assertEqual([r["name"] for r in repos], list(self.NAMES))
        self.assertEqual({r["unchecked"] for r in repos}, {"rate limited"})
        self.assertEqual({r["health"] for r in repos}, {"unknown"})
        self.assertEqual(unchecked, [{"what": "APT repository", "why": "rate limited"}])

    def test_an_unreadable_listing_is_reported(self):
        repos, unchecked = self.collect(refusing(429), only=None)
        self.assertIn({"what": "repository list", "why": "rate limited"}, unchecked)
        # Only the repositories named in the file are left to ask about.
        self.assertEqual([r["name"] for r in repos], sorted(build.EXTRA_REPOS))
        self.assertTrue(all(r["unchecked"] for r in repos))


if __name__ == "__main__":
    unittest.main()
