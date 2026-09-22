#!/usr/bin/env python3
"""Build a static CI dashboard for one owner's repositories.

Everything is resolved here, at build time, and written into index.html. The
page makes no API calls of its own: a browser fetching the GitHub API would be
capped at 60 requests an hour per visitor, and a page that needs a token is a
page that leaks one.

Reads GITHUB_TOKEN (the Actions token is enough -- every repository listed is
public). Writes index.html and status.json next to itself, or into --out-dir.
"""

import argparse
import datetime as dt
import html
import json
import os
import sys
import time
import urllib.error
import urllib.request

API = "https://api.github.com"

# Repositories outside the owner's account that belong on the board anyway.
EXTRA_REPOS = ["PAARA-org/w6otx"]

# Forks and archives are noise: nobody is watching CI on a fork of direwolf.
# What is left still includes long-dormant repositories, so a repository only
# earns a row if it has CI or an open pull request -- see `interesting()`.


def api(path, token, params=None):
    url = f"{API}{path}"
    if params:
        url += "?" + "&".join(f"{k}={v}" for k, v in params.items())
    req = urllib.request.Request(
        url,
        headers={
            "Accept": "application/vnd.github+json",
            "Authorization": f"Bearer {token}",
            "User-Agent": "ci-dashboard",
            "X-GitHub-Api-Version": "2022-11-28",
        },
    )
    # Roughly 80 requests per build, unattended, twice an hour: a single
    # timeout should not cost the whole board.
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=30) as resp:
                return json.load(resp)
        except urllib.error.HTTPError as e:
            # A missing Actions setup or a repository we cannot see is not
            # fatal; the row just carries less. Anything else should stop the
            # build rather than quietly publish a half-empty board.
            if e.code in (403, 404, 451):
                print(f"  {path}: HTTP {e.code}, skipping", file=sys.stderr)
                return None
            if e.code < 500 or attempt == 2:
                raise
        except (urllib.error.URLError, TimeoutError) as e:
            if attempt == 2:
                raise
            print(f"  {path}: {e}, retrying", file=sys.stderr)
        time.sleep(2 * (attempt + 1))
    return None


def repos_for(owner, token):
    out, page = [], 1
    while True:
        batch = api(
            f"/users/{owner}/repos",
            token,
            {"per_page": 100, "page": page, "type": "owner"},
        )
        if not batch:
            break
        out.extend(batch)
        if len(batch) < 100:
            break
        page += 1
    return [r for r in out if not r["fork"] and not r["archived"] and not r["private"]]


def latest_runs(full_name, branch, token):
    """The most recent run of each workflow on the default branch."""
    data = api(
        f"/repos/{full_name}/actions/runs",
        token,
        {"branch": branch, "per_page": 50, "exclude_pull_requests": "true"},
    )
    if not data:
        return []
    seen, runs = set(), []
    for run in data.get("workflow_runs", []):
        if run["workflow_id"] in seen or run["status"] != "completed":
            continue
        seen.add(run["workflow_id"])
        runs.append(
            {
                "name": run["name"],
                "conclusion": run["conclusion"],
                "url": run["html_url"],
                "finished": run["updated_at"],
            }
        )
    return sorted(runs, key=lambda r: r["name"].lower())


def open_prs(full_name, token):
    data = api(f"/repos/{full_name}/pulls", token, {"state": "open", "per_page": 50})
    if not data:
        return []
    prs = []
    for pr in data:
        prs.append(
            {
                "number": pr["number"],
                "title": pr["title"],
                "url": pr["html_url"],
                "draft": pr["draft"],
                "author": pr["user"]["login"],
                "updated": pr["updated_at"],
                "checks": pr_checks(full_name, pr["head"]["sha"], token),
            }
        )
    return prs


def pr_checks(full_name, sha, token):
    """Roll a pull request's check runs up to one word."""
    data = api(f"/repos/{full_name}/commits/{sha}/check-runs", token, {"per_page": 100})
    if not data:
        return "none"
    runs = data.get("check_runs", [])
    if not runs:
        return "none"
    if any(r["status"] != "completed" for r in runs):
        return "running"
    bad = {"failure", "timed_out", "cancelled", "action_required"}
    if any(r["conclusion"] in bad for r in runs):
        return "failing"
    return "passing"


def interesting(repo):
    return bool(repo["runs"] or repo["prs"])


def health(repo):
    if any(
        r["conclusion"] not in ("success", "skipped", "neutral") for r in repo["runs"]
    ):
        return "failing"
    if repo["runs"]:
        return "passing"
    return "unknown"


def collect(owner, token):
    out = []
    names = [r["full_name"] for r in repos_for(owner, token)] + EXTRA_REPOS
    for full_name in sorted(set(names)):
        print(f"- {full_name}", file=sys.stderr)
        meta = api(f"/repos/{full_name}", token)
        if not meta:
            continue
        repo = {
            "name": full_name,
            "url": meta["html_url"],
            "branch": meta["default_branch"],
            "pushed": meta["pushed_at"],
            "runs": latest_runs(full_name, meta["default_branch"], token),
            "prs": open_prs(full_name, token),
        }
        repo["health"] = health(repo)
        if interesting(repo):
            out.append(repo)
    # Anything red first, then anything awaiting review, then the quiet ones.
    order = {"failing": 0, "unknown": 1, "passing": 2}
    return sorted(
        out, key=lambda r: (order[r["health"]], -len(r["prs"]), r["name"].lower())
    )


def ago(stamp, now):
    when = dt.datetime.fromisoformat(stamp.replace("Z", "+00:00"))
    secs = (now - when).total_seconds()
    for limit, div, unit in (
        (3600, 60, "min"),
        (86400, 3600, "hour"),
        (None, 86400, "day"),
    ):
        if limit is None or secs < limit:
            n = max(1, int(secs // div))
            return f"{n} {unit}{'s' if n != 1 else ''} ago"
    return stamp


CSS = """
:root {
  color-scheme: light dark;
  --bg: #fbfbfa; --card: #ffffff; --line: #e4e2dd; --ink: #1d1c1a;
  --muted: #6b6862; --pass: #2f7d4f; --fail: #c0392b; --warn: #a06a00;
  --accent: #3b5bdb;
}
@media (prefers-color-scheme: dark) {
  :root:not([data-theme="light"]) {
    --bg: #17181a; --card: #1f2023; --line: #303236; --ink: #e9e8e6;
    --muted: #9a9892; --pass: #5bbd7f; --fail: #ef7a6d; --warn: #d9a441;
    --accent: #8ea3f0;
  }
}
* { box-sizing: border-box; }
body {
  margin: 0; padding: 32px 16px 64px; background: var(--bg); color: var(--ink);
  font: 15px/1.5 ui-sans-serif, system-ui, -apple-system, "Segoe UI", sans-serif;
}
.wrap { max-width: 960px; margin: 0 auto; }
h1 { font-size: 22px; margin: 0 0 4px; letter-spacing: -0.01em; }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
.summary { display: flex; gap: 20px; flex-wrap: wrap; margin: 0 0 24px;
  padding: 14px 16px; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; }
.summary div { font-size: 13px; color: var(--muted); }
.summary b { display: block; font-size: 22px; color: var(--ink); font-weight: 600; }
.summary b.fail { color: var(--fail); }
.repo { background: var(--card); border: 1px solid var(--line); border-radius: 10px;
  padding: 14px 16px; margin-bottom: 10px; }
.repo.failing { border-left: 3px solid var(--fail); }
.repo header { display: flex; align-items: baseline; gap: 10px; flex-wrap: wrap; }
.repo h2 { font-size: 15px; margin: 0; font-weight: 600; }
.repo h2 a { color: var(--ink); text-decoration: none; }
.repo h2 a:hover { color: var(--accent); }
.when { color: var(--muted); font-size: 12px; margin-left: auto; }
.chips { display: flex; gap: 6px; flex-wrap: wrap; margin-top: 10px; }
.chip { font-size: 12px; padding: 2px 9px; border-radius: 999px; text-decoration: none;
  border: 1px solid var(--line); color: var(--muted); white-space: nowrap; }
.chip.success { color: var(--pass); border-color: color-mix(in srgb, var(--pass) 35%, transparent); }
.chip.failure { color: var(--fail); border-color: color-mix(in srgb, var(--fail) 45%, transparent);
  font-weight: 600; }
.chip.other { color: var(--warn); border-color: color-mix(in srgb, var(--warn) 40%, transparent); }
.prs { margin: 10px 0 0; padding: 10px 0 0; border-top: 1px dashed var(--line);
  list-style: none; }
.prs li { display: flex; gap: 8px; align-items: baseline; padding: 3px 0; font-size: 13px; }
.prs a { color: var(--accent); text-decoration: none; }
.prs a:hover { text-decoration: underline; }
.num { color: var(--muted); font-variant-numeric: tabular-nums; }
.state { font-size: 11px; padding: 1px 7px; border-radius: 999px; border: 1px solid var(--line);
  color: var(--muted); margin-left: auto; white-space: nowrap; }
.state.passing { color: var(--pass); }
.state.failing { color: var(--fail); font-weight: 600; }
.state.running { color: var(--warn); }
.draft { font-size: 11px; color: var(--muted); border: 1px solid var(--line);
  border-radius: 4px; padding: 0 5px; }
footer { color: var(--muted); font-size: 12px; margin-top: 28px; text-align: center; }
footer a { color: var(--muted); }
@media (max-width: 600px) {
  .when { margin-left: 0; width: 100%; }
  .state { margin-left: 0; }
  .prs li { flex-wrap: wrap; }
}
"""


def render(repos, owner, now):
    e = html.escape
    failing = [r for r in repos if r["health"] == "failing"]
    prs = [(r, p) for r in repos for p in r["prs"]]
    parts = [
        "<!DOCTYPE html>",
        '<html lang="en"><head><meta charset="utf-8">',
        '<meta name="viewport" content="width=device-width, initial-scale=1">',
        f"<title>CI status — {e(owner)}</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
        "<h1>CI status</h1>",
        (
            f"<p class='sub'>Default-branch workflow results and open pull requests "
            f"across {len(repos)} active repositories. "
            f"Rebuilt {e(now.strftime('%Y-%m-%d %H:%M UTC'))}.</p>"
        ),
        "<div class='summary'>",
        f"<div><b class='{'fail' if failing else ''}'>{len(failing)}</b>repos failing</div>",
        f"<div><b>{len(prs)}</b>open pull requests</div>",
        f"<div><b>{len(repos)}</b>repos watched</div>",
        "</div>",
    ]
    for r in repos:
        parts.append(f"<section class='repo {r['health']}'>")
        short = (
            r["name"].split("/", 1)[1]
            if r["name"].startswith(owner + "/")
            else r["name"]
        )
        parts.append(
            f"<header><h2><a href='{e(r['url'])}'>{e(short)}</a></h2>"
            f"<span class='when'>pushed {e(ago(r['pushed'], now))}</span></header>"
        )
        if r["runs"]:
            parts.append("<div class='chips'>")
            for run in r["runs"]:
                cls = (
                    "success"
                    if run["conclusion"] == "success"
                    else "failure"
                    if run["conclusion"] == "failure"
                    else "other"
                )
                parts.append(
                    f"<a class='chip {cls}' href='{e(run['url'])}'>"
                    f"{e(run['name'])} · {e(run['conclusion'] or 'n/a')}</a>"
                )
            parts.append("</div>")
        if r["prs"]:
            parts.append("<ul class='prs'>")
            for p in r["prs"]:
                draft = "<span class='draft'>draft</span>" if p["draft"] else ""
                parts.append(
                    f"<li><span class='num'>#{p['number']}</span>"
                    f"<a href='{e(p['url'])}'>{e(p['title'])}</a>{draft}"
                    f"<span class='state {p['checks']}'>{e(p['checks'])}</span></li>"
                )
            parts.append("</ul>")
        parts.append("</section>")
    parts.append(
        "<footer>Built by <a href='https://github.com/"
        f"{e(owner)}/ci-dashboard'>ci-dashboard</a>. "
        "Public, non-fork repositories with CI or an open pull request.</footer>"
    )
    parts.append("</div></body></html>")
    return "\n".join(parts)


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--owner", default="charlieh0tel")
    ap.add_argument("--out-dir", default=os.path.dirname(os.path.abspath(__file__)))
    args = ap.parse_args()

    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is required (the Actions token is enough)")

    now = dt.datetime.now(dt.timezone.utc)
    repos = collect(args.owner, token)
    os.makedirs(args.out_dir, exist_ok=True)
    with open(os.path.join(args.out_dir, "status.json"), "w") as f:
        json.dump({"generated": now.isoformat(), "repos": repos}, f, indent=2)
    with open(os.path.join(args.out_dir, "index.html"), "w") as f:
        f.write(render(repos, args.owner, now))
    print(f"{len(repos)} repositories written to {args.out_dir}", file=sys.stderr)


if __name__ == "__main__":
    main()
