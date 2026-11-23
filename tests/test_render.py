"""Render the committed fixture and check the page that comes out.

`render` reads only status.json, so it can be checked without the network or
a token: tests/status-fixture.json is a hand-written file in the shape
`collect` writes, with one repository for each thing the page distinguishes.
The live `collect` run in CI cannot do this job, because what it sees changes
from hour to hour.

Run it the way CI does: python3 -m unittest discover -s tests -v
"""

import json
import os
import subprocess
import sys
import tempfile
import unittest
from html.parser import HTMLParser

HERE = os.path.dirname(os.path.abspath(__file__))
BUILD = os.path.join(os.path.dirname(HERE), "build.py")
FIXTURE = os.path.join(HERE, "status-fixture.json")

# Elements the page uses that never take a closing tag.
VOID = {"meta", "link"}


class Page(HTMLParser):
    """Every element with its text, and every way the nesting went wrong."""

    def __init__(self):
        super().__init__()
        self.open, self.elements, self.problems = [], [], []
        self.doctype, self.count = None, 0

    def handle_decl(self, decl):
        self.doctype = decl

    def handle_starttag(self, tag, attrs):
        element = {"tag": tag, "attrs": dict(attrs), "text": []}
        # Elements are recorded as they close; this puts them back in the
        # order a reader meets them.
        element["order"] = self.count = self.count + 1
        if tag in VOID:
            self.elements.append(element)
        else:
            self.open.append(element)

    def handle_data(self, data):
        for element in self.open:
            element["text"].append(data)

    def handle_endtag(self, tag):
        if not self.open or self.open[-1]["tag"] != tag:
            inside = self.open[-1]["tag"] if self.open else "nothing"
            self.problems.append(f"</{tag}> at {self.getpos()} closes {inside}")
            return
        element = self.open.pop()
        element["text"] = "".join(element["text"])
        self.elements.append(element)

    def find(self, tag, cls=None):
        """Matching elements, in the order they open on the page."""
        found = [
            el
            for el in self.elements
            if el["tag"] == tag
            and (cls is None or cls in (el["attrs"].get("class") or "").split())
        ]
        return sorted(found, key=lambda el: el["order"])

    def texts(self, tag, cls=None):
        return [" ".join(el["text"].split()) for el in self.find(tag, cls)]


def run_render(status_path, out_dir):
    return subprocess.run(
        [sys.executable, BUILD, "render", status_path, "--out-dir", out_dir],
        capture_output=True,
        text=True,
        check=False,
    )


class RenderFixture(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        with tempfile.TemporaryDirectory() as out_dir:
            result = run_render(FIXTURE, out_dir)
            if result.returncode:
                raise AssertionError(f"render failed:\n{result.stderr}")
            with open(os.path.join(out_dir, "index.html"), encoding="utf-8") as f:
                cls.html = f.read()
        cls.page = Page()
        cls.page.feed(cls.html)
        cls.page.close()

    def test_well_formed(self):
        self.assertEqual(self.page.doctype, "DOCTYPE html")
        self.assertEqual(self.page.problems, [])
        self.assertEqual([el["tag"] for el in self.page.open], [])
        self.assertEqual(self.page.texts("title"), ["example's CI Status"])

    def test_rebuilt_time_comes_from_the_file(self):
        # Not the clock: the same status.json must always give the same page.
        self.assertIn("Rebuilt 2026-01-15 12:00 UTC", self.html)
        self.assertIn("pushed 1 day ago", self.page.texts("span", "when"))

    def test_summary_counts(self):
        # Failing, open pull requests, releases with advisories, watched.
        self.assertEqual(self.page.texts("b"), ["1", "2", "1", "5"])
        self.assertIn("5repos watched", self.page.texts("div"))

    def test_sections_and_repositories_in_order(self):
        self.assertEqual(
            self.page.texts("h2", "section"),
            ["Needs attention (2)", "Open pull requests (1)", "Quiet (2)"],
        )
        # The owner's repositories lose their prefix; a guest keeps its own.
        headings = [el["text"] for el in self.page.find("h2") if not el["attrs"]]
        self.assertEqual(
            headings, ["shipping", "broken", "reviewing", "other-org/guest", "quiet"]
        )

    def test_only_the_failing_repository_is_red(self):
        classes = [el["attrs"]["class"] for el in self.page.find("section", "repo")]
        self.assertEqual(classes.count("repo failing"), 1)
        self.assertEqual(classes.count("repo passing"), 4)
        # The favicon follows the builds, so one red repository turns it red.
        self.assertIn("fill='%23d8574a'", self.html)

    def test_run_chips(self):
        chips = {
            el["attrs"]["href"].split("/runs/")[1]: el
            for el in self.page.find("a", "chip")
        }
        self.assertEqual(len(chips), 6)
        self.assertIn("failure", chips["201"]["attrs"]["class"].split())
        self.assertNotIn("stale-chip", chips["201"]["attrs"]["class"].split())
        self.assertIn("stale-chip", chips["202"]["attrs"]["class"].split())
        self.assertTrue(chips["202"]["text"].endswith("(stale)"))
        self.assertEqual(
            chips["202"]["attrs"]["title"], "Ran 3 days ago, before the last push"
        )
        # A run on a tag says which tag.
        self.assertTrue(chips["401"]["text"].endswith("success @v2.0.0"))

    def test_pull_requests(self):
        self.assertEqual(self.page.texts("span", "state"), ["failing", "running"])
        self.assertEqual(self.page.texts("span", "num"), ["#7", "#8"])
        self.assertEqual(self.page.texts("span", "draft"), ["draft"])

    def test_api_text_is_escaped(self):
        with open(FIXTURE, encoding="utf-8") as f:
            title = json.load(f)["repos"][2]["prs"][0]["title"]
        self.assertIn("<b>", title)
        # No <b> element made it onto the page from the title; the summary
        # counts are the only bold text there is.
        self.assertEqual(len(self.page.find("b")), 4)
        link = [el for el in self.page.find("a") if el["attrs"]["href"].endswith("/7")]
        self.assertEqual([el["text"] for el in link], [title])

    def test_published_versions(self):
        self.assertEqual(
            self.page.texts("div", "pub-line"),
            [
                "newest releasev1.2.0",
                "crates.ioshipping1.2.0 source is 1.3.0",
                "newest tagv2.0.0",
            ],
        )

    def test_advisories(self):
        self.assertEqual(
            self.page.texts("div", "adv-line"),
            [
                "on mainclean",
                "at release (v1.2.0)1 advisories",
                "on mainclean +1 unmaintained/yanked",
            ],
        )
        self.assertEqual(len(self.page.find("div", "stale")), 1)
        self.assertIn("Fixed on main", self.page.texts("div", "stale")[0])
        listed = [
            el["attrs"]["href"]
            for el in self.page.find("a")
            if "osv.dev/vulnerability/" in el["attrs"]["href"]
        ]
        self.assertEqual(listed, ["https://osv.dev/vulnerability/RUSTSEC-2025-9001"])


class RenderRefusals(unittest.TestCase):
    def test_unknown_schema_is_refused(self):
        with open(FIXTURE, encoding="utf-8") as f:
            data = json.load(f)
        data["schema"] += 1
        with tempfile.TemporaryDirectory() as out_dir:
            status = os.path.join(out_dir, "status.json")
            with open(status, "w", encoding="utf-8") as f:
                json.dump(data, f)
            result = run_render(status, out_dir)
            self.assertNotEqual(result.returncode, 0)
            self.assertIn("schema", result.stderr)
            self.assertFalse(os.path.exists(os.path.join(out_dir, "index.html")))


if __name__ == "__main__":
    unittest.main()
