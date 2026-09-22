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
import base64
import datetime as dt
import html
import json
import os
import re
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
        # Dependabot's own update jobs land here as `dynamic` runs, one per
        # dependency it examines. They are not this repository's CI, and they
        # fail routinely when an advisory has no fix Dependabot can apply --
        # which would paint a repo red for the one thing it cannot do anything
        # about.
        if run.get("event") == "dynamic":
            continue
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


OSV_API = "https://api.osv.dev/v1"
# OSV carries the RustSec database, so the advisories here are the ones
# `cargo audit` would report -- without needing a Rust toolchain in this build.
OSV_BATCH = 500


def post_json(url, payload):
    req = urllib.request.Request(
        url,
        data=json.dumps(payload).encode(),
        headers={"Content-Type": "application/json", "User-Agent": "ci-dashboard"},
        method="POST",
    )
    for attempt in range(3):
        try:
            with urllib.request.urlopen(req, timeout=60) as resp:
                return json.load(resp)
        except (urllib.error.URLError, TimeoutError, urllib.error.HTTPError) as e:
            if attempt == 2:
                raise
            print(f"  OSV: {e}, retrying", file=sys.stderr)
            time.sleep(2 * (attempt + 1))
    return None


def lockfile(full_name, ref, token):
    """A repository's Cargo.lock at one ref, or None if it has none."""
    data = api(f"/repos/{full_name}/contents/Cargo.lock", token, {"ref": ref})
    if not data:
        return None
    if data.get("content"):
        return base64.b64decode(data["content"]).decode("utf-8", "replace")
    # Over a megabyte: the contents API stops inlining and hands back a URL.
    url = data.get("download_url")
    if not url:
        return None
    with urllib.request.urlopen(url, timeout=30) as resp:
        return resp.read().decode("utf-8", "replace")


def packages(lock_text):
    """(name, version) for every crate pinned in a Cargo.lock."""
    if not lock_text:
        return []
    out = []
    for block in lock_text.split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.MULTILINE)
        version = re.search(r'^version = "([^"]+)"', block, re.MULTILINE)
        if name and version:
            out.append((name.group(1), version.group(1)))
    return out


def osv_lookup(pkgs, cache):
    """Advisory ids per (name, version), asking OSV only about what is new."""
    unknown = sorted({p for p in pkgs if p not in cache})
    for i in range(0, len(unknown), OSV_BATCH):
        chunk = unknown[i : i + OSV_BATCH]
        body = {
            "queries": [
                {"package": {"name": n, "ecosystem": "crates.io"}, "version": v}
                for n, v in chunk
            ]
        }
        results = post_json(f"{OSV_API}/querybatch", body).get("results", [])
        for pkg, result in zip(chunk, results):
            cache[pkg] = [v["id"] for v in (result or {}).get("vulns", [])]
    found = {}
    for pkg in pkgs:
        for vid in cache.get(pkg, []):
            found.setdefault(vid, set()).add(f"{pkg[0]} {pkg[1]}")
    return found


def osv_detail(vid, cache):
    if vid not in cache:
        data = api_get_json(f"{OSV_API}/vulns/{vid}") or {}
        summary = data.get("summary") or data.get("details", "")
        # RustSec's "unmaintained" and "unsound" advisories live per-affected
        # package, not at the top level. cargo audit prints these as warnings
        # rather than vulnerabilities, and so does this.
        informational = any(
            (a.get("database_specific") or {}).get("informational")
            for a in data.get("affected", [])
        )
        cache[vid] = {
            "id": vid,
            "summary": summary.strip().split("\n")[0][:110],
            "informational": informational,
            "aliases": data.get("aliases") or [],
        }
    return cache[vid]


def dedupe(items):
    """One entry per finding.

    OSV returns the RustSec advisory and its GHSA/CVE twin as separate hits for
    the same problem, which triples the count for anything in rustls. Group by
    alias and keep the RUSTSEC id, so the page says what cargo audit would.
    """
    kept, index = [], {}
    for a in sorted(items, key=lambda a: (not a["id"].startswith("RUSTSEC"), a["id"])):
        ids = {a["id"], *a["aliases"]}
        hit = next((index[i] for i in ids if i in index), None)
        if hit:
            hit["crates"] = sorted(set(hit["crates"]) | set(a["crates"]))
            continue
        kept.append(a)
        for i in ids:
            index[i] = a
    return kept


def api_get_json(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ci-dashboard"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  {url}: {e}", file=sys.stderr)
        return None


def advisories(full_name, branch, token, pkg_cache, vuln_cache):
    """Advisories on the default branch and in the newest release, compared.

    They answer different questions: the branch says whether the problem is
    fixed, the release says whether what people can install still has it.
    """
    out = {"main": [], "published": [], "tag": None, "has_lock": False}
    main_lock = lockfile(full_name, branch, token)
    if main_lock is None:
        return out
    out["has_lock"] = True
    for vid, crates in osv_lookup(packages(main_lock), pkg_cache).items():
        out["main"].append({**osv_detail(vid, vuln_cache), "crates": sorted(crates)})

    release = api(f"/repos/{full_name}/releases/latest", token)
    if release:
        out["tag"] = release["tag_name"]
        rel_lock = lockfile(full_name, release["tag_name"], token)
        for vid, crates in osv_lookup(packages(rel_lock), pkg_cache).items():
            out["published"].append(
                {**osv_detail(vid, vuln_cache), "crates": sorted(crates)}
            )
    for key in ("main", "published"):
        out[key] = dedupe(out[key])
        out[key].sort(key=lambda a: (a["informational"], a["id"]))
    return out


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
    # Shared across repositories: the same crate at the same version resolves
    # to the same answer, and these repos overlap heavily.
    pkg_cache, vuln_cache = {}, {}
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
            "advisories": advisories(
                full_name, meta["default_branch"], token, pkg_cache, vuln_cache
            ),
        }
        repo["health"] = health(repo)
        if interesting(repo):
            out.append(repo)
    # A release people can install with a known advisory in it outranks a red
    # build: the build is a problem for you, the release is a problem for them.
    order = {"failing": 0, "unknown": 1, "passing": 2}
    return sorted(
        out,
        key=lambda r: (
            not real(r["advisories"]["published"]),
            order[r["health"]],
            -len(r["prs"]),
            r["name"].lower(),
        ),
    )


def real(items):
    """Advisories proper, dropping the unmaintained/yanked notices."""
    return [a for a in items if not a["informational"]]


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
.adv { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line);
  font-size: 13px; }
.adv-line { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.adv-label { color: var(--muted); font-size: 12px; min-width: 76px; }
.adv-clean { color: var(--pass); }
.adv-count { color: var(--fail); font-weight: 600; }
.adv-info { color: var(--warn); }
.adv-list { margin: 6px 0 0; padding-left: 0; list-style: none; color: var(--muted);
  font-size: 12px; }
.adv-list li { padding: 1px 0; }
.adv-list a { color: var(--fail); text-decoration: none; }
.adv-list a:hover { text-decoration: underline; }
.adv-list .crate { color: var(--ink); }
.stale { margin-top: 6px; font-size: 12px; color: var(--warn); }
footer { color: var(--muted); font-size: 12px; margin-top: 28px; text-align: center; }
footer a { color: var(--muted); }
@media (max-width: 600px) {
  .when { margin-left: 0; width: 100%; }
  .state { margin-left: 0; }
  .prs li { flex-wrap: wrap; }
}
"""


def advisory_line(label, items, tag=None):
    vulns, info = real(items), [a for a in items if a["informational"]]
    suffix = f" ({tag})" if tag else ""
    if vulns:
        body = f"<span class='adv-count'>{len(vulns)} advisories</span>"
    else:
        body = "<span class='adv-clean'>clean</span>"
    if info:
        body += f" <span class='adv-info'>+{len(info)} unmaintained/yanked</span>"
    return f"<div class='adv-line'><span class='adv-label'>{label}{suffix}</span>{body}</div>"


def advisory_html(repo, e):
    adv = repo["advisories"]
    if not adv["has_lock"]:
        return []
    parts = ["<div class='adv'>"]
    parts.append(advisory_line("main", adv["main"]))
    if adv["tag"]:
        parts.append(advisory_line("published", adv["published"], e(adv["tag"])))
    else:
        parts.append(
            "<div class='adv-line'><span class='adv-label'>published</span>"
            "<span class='adv-clean'>no release</span></div>"
        )
    # The gap is the actionable part: fixed on the branch, still out there in
    # the last release, and only a new tag closes it.
    fixed = {a["id"] for a in real(adv["main"])}
    out_there = real(adv["published"])
    if out_there and not [a for a in out_there if a["id"] in fixed]:
        parts.append(
            "<div class='stale'>Fixed on the branch but not in the release — "
            "cutting a tag ships the fix.</div>"
        )
    shown = real(adv["published"]) or real(adv["main"])
    if shown:
        parts.append("<ul class='adv-list'>")
        for a in shown[:6]:
            url = f"https://osv.dev/vulnerability/{a['id']}"
            crates = ", ".join(a["crates"][:3])
            parts.append(
                f"<li><a href='{e(url)}'>{e(a['id'])}</a> "
                f"<span class='crate'>{e(crates)}</span> — {e(a['summary'])}</li>"
            )
        parts.append("</ul>")
    parts.append("</div>")
    return parts


def render(repos, owner, now):
    e = html.escape
    failing = [r for r in repos if r["health"] == "failing"]
    prs = [(r, p) for r in repos for p in r["prs"]]
    shipping = [r for r in repos if real(r["advisories"]["published"])]
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
        (
            f"<div><b class='{'fail' if shipping else ''}'>{len(shipping)}</b>"
            "releases with advisories</div>"
        ),
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
        parts.extend(advisory_html(r, e))
        parts.append("</section>")
    parts.append(
        "<footer>Built by <a href='https://github.com/"
        f"{e(owner)}/ci-dashboard'>ci-dashboard</a>. "
        "Public, non-fork repositories with CI or an open pull request. "
        "Advisories from <a href='https://osv.dev'>OSV</a>, which carries RustSec "
        "and GitHub's database: a GHSA- id is one <code>cargo audit</code> does "
        "not see. Yanked versions are registry state rather than advisories, so "
        "they appear only in <code>cargo audit</code>.</footer>"
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
