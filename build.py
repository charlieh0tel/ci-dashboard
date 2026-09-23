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
    # Roughly 80 requests per build, unattended, four times an hour: a single
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
    """Every repository of the owner's this token can see.

    /user/repos is asked first because it is the only listing that includes
    private repositories, and it only works when the token belongs to the
    owner -- the Actions token does not, so CI needs DASHBOARD_TOKEN set to a
    PAT with read access. Without it this falls back to the public listing and
    the board is simply public-only, rather than failing.
    """
    out = []
    for path, params in (
        ("/user/repos", {"affiliation": "owner"}),
        (f"/users/{owner}/repos", {"type": "owner"}),
    ):
        page = 1
        while True:
            batch = api(path, token, {"per_page": 100, "page": page, **params})
            if not batch:
                break
            out.extend(batch)
            if len(batch) < 100:
                break
            page += 1
        if out:
            break

    seen, repos = set(), []
    for r in out:
        if r["owner"]["login"].lower() != owner.lower():
            continue
        if r["fork"] or r["archived"] or r["full_name"] in seen:
            continue
        seen.add(r["full_name"])
        repos.append(r)
    return repos


def latest_runs(full_name, branch, token):
    """The most recent run of each workflow, wherever it ran.

    Filtering to the default branch hid whole workflows: nec2-js releases by
    pushing <package>@<version> tags, so its Release workflow never runs on
    main and the only chip it ever showed was a stray workflow_dispatch from
    August that failed. Tag runs count, and the chip says which ref it was.
    """
    runs_on_branch = _runs(full_name, token, {"branch": branch})
    newest = {r["workflow_id"]: r for r in reversed(runs_on_branch)}
    for run in _runs(full_name, token, {}):
        current = newest.get(run["workflow_id"])
        if not current or run["updated_at"] > current["updated_at"]:
            newest[run["workflow_id"]] = run

    runs = []
    for run in newest.values():
        runs.append(
            {
                "name": run["name"],
                "conclusion": run["conclusion"],
                "url": run["html_url"],
                "finished": run["updated_at"],
                "event": run["event"],
                # Named only when it is not the default branch, so a release
                # chip says which tag produced it.
                "ref": None if run["head_branch"] == branch else run["head_branch"],
            }
        )
    return sorted(runs, key=lambda r: r["name"].lower())


def _runs(full_name, token, extra):
    """Completed runs, newest first, minus the ones that are not this repo's CI."""
    params = {"per_page": 50, "exclude_pull_requests": "true", **extra}
    data = api(f"/repos/{full_name}/actions/runs", token, params)
    return [
        run
        for run in (data or {}).get("workflow_runs", [])
        # Dependabot's own update jobs arrive as `dynamic` runs, one per
        # dependency examined, and fail routinely on advisories it cannot fix.
        if run.get("event") != "dynamic" and run["status"] == "completed"
    ]


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


# Which lockfiles to read, and the OSV ecosystem each one's contents belong to.
# uv.lock earns its place: GitHub does not list uv as a supported ecosystem, so
# Dependabot will not open a fix PR for those projects, and this is the only
# place their advisories show up outside their own CI.
LOCKFILES = (
    ("Cargo.lock", "crates.io"),
    ("uv.lock", "PyPI"),
    ("package-lock.json", "npm"),
)


def lockfile(full_name, ref, token, name="Cargo.lock"):
    """One lockfile at one ref, or None if the repository has no such file."""
    data = api(f"/repos/{full_name}/contents/{name}", token, {"ref": ref})
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


def packages(lock_text, lock_name):
    """(name, version) for everything pinned in a lockfile.

    Cargo.lock and uv.lock are both TOML with the same [[package]] shape, so
    they parse the same way. package-lock.json is JSON, keyed by install path.
    """
    if not lock_text:
        return []
    if lock_name.endswith(".json"):
        try:
            data = json.loads(lock_text)
        except json.JSONDecodeError:
            return []
        out = []
        for path, spec in (data.get("packages") or {}).items():
            # "" is the project itself; the rest are node_modules/<name>, which
            # nests for transitive copies -- the last segment is the package.
            if not path or not isinstance(spec, dict) or "version" not in spec:
                continue
            out.append((path.split("node_modules/")[-1], spec["version"]))
        return out
    out = []
    for block in lock_text.split("[[package]]")[1:]:
        name = re.search(r'^name = "([^"]+)"', block, re.MULTILINE)
        version = re.search(r'^version = "([^"]+)"', block, re.MULTILINE)
        if name and version:
            out.append((name.group(1), version.group(1)))
    return out


def osv_lookup(pkgs, cache):
    """Advisory ids per (ecosystem, name, version), asking OSV only what is new."""
    unknown = sorted({p for p in pkgs if p not in cache})
    for i in range(0, len(unknown), OSV_BATCH):
        chunk = unknown[i : i + OSV_BATCH]
        body = {
            "queries": [
                {"package": {"name": n, "ecosystem": eco}, "version": v}
                for eco, n, v in chunk
            ]
        }
        results = post_json(f"{OSV_API}/querybatch", body).get("results", [])
        for pkg, result in zip(chunk, results):
            cache[pkg] = [v["id"] for v in (result or {}).get("vulns", [])]
    found = {}
    for pkg in pkgs:
        for vid in cache.get(pkg, []):
            found.setdefault(vid, set()).add(f"{pkg[1]} {pkg[2]}")
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
    out = {"main": [], "published": [], "tag": None, "has_lock": False, "locks": []}

    def pinned(ref):
        """Everything pinned at a ref, across every lockfile the repo has."""
        found = []
        for name, ecosystem in LOCKFILES:
            text = lockfile(full_name, ref, token, name)
            if text is None:
                continue
            if name not in out["locks"]:
                out["locks"].append(name)
            found += [(ecosystem, n, v) for n, v in packages(text, name)]
        return found

    on_branch = pinned(branch)
    if not on_branch:
        return out
    out["has_lock"] = True
    for vid, pkgs in osv_lookup(on_branch, pkg_cache).items():
        out["main"].append({**osv_detail(vid, vuln_cache), "crates": sorted(pkgs)})

    release = api(f"/repos/{full_name}/releases/latest", token)
    if release:
        out["tag"] = release["tag_name"]
        for vid, pkgs in osv_lookup(pinned(release["tag_name"]), pkg_cache).items():
            out["published"].append(
                {**osv_detail(vid, vuln_cache), "crates": sorted(pkgs)}
            )
    for key in ("main", "published"):
        out[key] = dedupe(out[key])
        out[key].sort(key=lambda a: (a["informational"], a["id"]))
    return out


REGISTRIES = {
    "crates.io": "https://crates.io/api/v1/crates/{name}",
    "npm": "https://registry.npmjs.org/{name}/latest",
}


def manifests(full_name, ref, token):
    """(registry, package, version) for everything this repo could publish.

    Read from the manifests rather than guessed from the repository name: the
    two are often different, and a name collision on a public registry belongs
    to whoever registered it first.
    """
    out = []
    cargo = lockfile(full_name, ref, token, "Cargo.toml")
    if cargo and "[package]" in cargo:
        head = cargo.split("[package]", 1)[1].split("\n[", 1)[0]
        name = re.search(r'^name = "([^"]+)"', head, re.MULTILINE)
        version = re.search(r'^version = "([^"]+)"', head, re.MULTILINE)
        # publish = false says this crate is not for a registry. Cargo refuses
        # to publish it, and a board reporting "not published" for it would be
        # reporting an intention nobody has.
        wanted = not re.search(r"^publish = false", head, re.MULTILINE)
        if name and version and wanted:
            out.append(("crates.io", name.group(1), version.group(1)))

    root = lockfile(full_name, ref, token, "package.json")
    if root:
        try:
            spec = json.loads(root)
        except json.JSONDecodeError:
            spec = {}
        if spec.get("workspaces"):
            # A workspace root is not itself published; its members are.
            listing = api(f"/repos/{full_name}/contents/packages", token, {"ref": ref})
            for entry in listing or []:
                member = lockfile(
                    full_name, ref, token, f"packages/{entry['name']}/package.json"
                )
                try:
                    m = json.loads(member or "{}")
                except json.JSONDecodeError:
                    continue
                if m.get("name") and m.get("version") and not m.get("private"):
                    out.append(("npm", m["name"], m["version"]))
        elif spec.get("name") and spec.get("version") and not spec.get("private"):
            out.append(("npm", spec["name"], spec["version"]))
    return out


def registry_version(registry, package, full_name, cache):
    """What the registry serves, but only if it agrees this repo owns it.

    crates.io has a `weather` crate belonging to somebody else entirely.
    Matching on name alone would report a stranger's releases as yours, so the
    registry's own repository field has to point back here.
    """
    key = (registry, package)
    if key in cache:
        return cache[key]
    data = api_get_json(REGISTRIES[registry].format(name=package))
    version = repo_url = None
    if data and registry == "crates.io" and data.get("crate"):
        version = data["crate"].get("max_version")
        repo_url = data["crate"].get("repository") or ""
    elif data and registry == "npm":
        version = data.get("version")
        repo_url = ((data.get("repository") or {}) or {}).get("url") or ""
    if version and full_name.lower() not in (repo_url or "").lower():
        version = None  # same name, different project
    cache[key] = version
    return version


APT_PACKAGES = (
    "https://charlieh0tel.github.io/apt-repo/dists/bookworm/main/binary-amd64/Packages"
)
APT_MAP = "/repos/charlieh0tel/apt-repo/contents/packages.tsv"


def apt_repository(token):
    """What the APT repository actually serves, by source repository.

    Two fetches, once per build: apt-repo's packages.tsv says which repository
    a package comes from, and the published Packages index says which version
    is installable today. A repository can ship several packages -- renogymon
    ships four -- so the tsv name is matched as a prefix.
    """
    index = api_get_text(APT_PACKAGES)
    if not index:
        return {}
    serving, name = {}, None
    for line in index.splitlines():
        if line.startswith("Package: "):
            name = line.split(": ", 1)[1].strip()
        elif line.startswith("Version: ") and name:
            serving[name] = line.split(": ", 1)[1].strip()
            name = None

    data = api(APT_MAP, token)
    if not data or not data.get("content"):
        return {}
    out = {}
    for row in base64.b64decode(data["content"]).decode().splitlines():
        if row.startswith("#") or not row.strip():
            continue
        parts = row.split("\t")
        if len(parts) < 2:
            continue
        repo, pkg = parts[0].strip(), parts[1].strip()
        found = {
            n: v for n, v in serving.items() if n == pkg or n.startswith(pkg + "-")
        }
        if found:
            out[repo] = found
    return out


def api_get_text(url):
    req = urllib.request.Request(url, headers={"User-Agent": "ci-dashboard"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return resp.read().decode("utf-8", "replace")
    except (urllib.error.URLError, urllib.error.HTTPError, TimeoutError) as e:
        print(f"  {url}: {e}", file=sys.stderr)
        return None


def newest_tag(full_name, token, release_tag):
    """The tag a reader would call the latest one.

    The release tag when there is a release. Otherwise the newest tag by commit
    date: nec2-js publishes by pushing <package>@<version> tags and cuts no
    releases, and the order /tags returns is not documented as chronological,
    so the dates are asked for rather than assumed. Capped at five tags, which
    finds the newest without paying for a full listing.
    """
    if release_tag:
        return release_tag, "release"
    best, best_when = None, ""
    for tag in api(f"/repos/{full_name}/tags", token, {"per_page": 5}) or []:
        commit = api(f"/repos/{full_name}/commits/{tag['commit']['sha']}", token)
        when = (((commit or {}).get("commit") or {}).get("committer") or {}).get(
            "date", ""
        )
        if when > best_when:
            best, best_when = tag["name"], when
    return best, "tag"


def upstream(repo, full_name, token, reg_cache, apt_serving):
    """Where this repository's code has actually got to, per channel.

    The GitHub release is not the answer: usbrelay-rs had a v0.1.1 release for
    months while crates.io sat on 0.1.0. Each registry is asked directly.
    """
    rows = []
    tag, kind = newest_tag(full_name, token, repo["advisories"].get("tag"))
    if tag:
        rows.append(
            {
                "channel": "newest " + kind,
                "package": tag,
                "published": "",
                "source": None,
                "behind": False,
            }
        )
    for registry, package, version in manifests(full_name, repo["branch"], token):
        live = registry_version(registry, package, full_name, reg_cache)
        rows.append(
            {
                "channel": registry,
                "package": package,
                # A package the manifest declares but the registry does not
                # serve is worth saying out loud, not omitting.
                "published": live or "not published",
                "source": version,
                "behind": bool(live) and live != version,
            }
        )
    for package, version in sorted(apt_serving.get(full_name, {}).items()):
        rows.append(
            {
                "channel": "apt",
                "package": package,
                "published": version,
                # The Debian revision and any CI suffix are not upstream versions.
                "source": None,
                "behind": False,
            }
        )
    return rows


def interesting(repo):
    return bool(repo["runs"] or repo["prs"])


def health(repo):
    """Red only for failures that still describe the current code.

    The newest run of a workflow can be months old -- a release triggered by
    hand, a workflow that only fires on tags -- and a failure from before the
    last push refers to code that is no longer there. Counting those paints a
    repository red forever over something already superseded, which is the
    fastest way to teach someone to ignore this page.
    """
    live = [r for r in repo["runs"] if not r["stale"]]
    if any(r["conclusion"] not in ("success", "skipped", "neutral") for r in live):
        return "failing"
    if live:
        return "passing"
    return "unknown"


def collect(owner, token):
    out = []
    # Shared across repositories: the same crate at the same version resolves
    # to the same answer, and these repos overlap heavily.
    pkg_cache, vuln_cache, reg_cache = {}, {}, {}
    apt_serving = apt_repository(token)
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
        for run in repo["runs"]:
            # Only default-branch runs go stale. A tag run describes the tag,
            # and stays true however far main moves afterwards -- marking a
            # successful release stale because someone merged a README fix
            # since would be telling people to distrust a result that is fine.
            run["stale"] = not run["ref"] and run["finished"] < meta["pushed_at"]
        repo["published"] = upstream(repo, full_name, token, reg_cache, apt_serving)
        repo["health"] = health(repo)
        if interesting(repo):
            out.append(repo)
    return sorted(out, key=rank)


def short(name):
    return name.split("/", 1)[1] if "/" in name else name


def group(repo):
    """Which section a repository belongs in, and why it is there.

    A release people can install with a known advisory in it outranks a red
    build: the build is a problem for you, the release is a problem for them.
    """
    if real(repo["advisories"]["published"]) or repo["health"] == "failing":
        return 0
    if repo["prs"]:
        return 1
    return 2


def rank(repo):
    # Count, not presence. Seven advisories outranks one, which the previous
    # key got backwards by asking only whether there were any.
    published = len(real(repo["advisories"]["published"]))
    on_main = len(real(repo["advisories"]["main"]))
    return (
        group(repo),
        -published,
        0 if repo["health"] == "failing" else 1,
        -on_main,
        -len(repo["prs"]),
        # The displayed name. Sorting on owner/name puts the one repository
        # from another organisation at the end, looking misfiled.
        short(repo["name"]).lower(),
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
.top { display: flex; align-items: center; gap: 16px; margin-bottom: 4px; }
h1 { font-size: 22px; margin: 0; letter-spacing: -0.01em; }
.rebuild { margin-left: auto; font-size: 13px; text-decoration: none;
  color: var(--accent); border: 1px solid var(--line); border-radius: 6px;
  padding: 5px 11px; white-space: nowrap; }
.rebuild:hover { border-color: var(--accent); }
.sub { color: var(--muted); font-size: 13px; margin: 0 0 24px; }
.summary { display: flex; gap: 20px; flex-wrap: wrap; margin: 0 0 24px;
  padding: 14px 16px; background: var(--card); border: 1px solid var(--line);
  border-radius: 10px; }
.summary div { font-size: 13px; color: var(--muted); }
.summary b { display: block; font-size: 22px; color: var(--ink); font-weight: 600; }
.summary b.fail { color: var(--fail); }
h2.section { font-size: 12px; text-transform: uppercase; letter-spacing: 0.06em;
  color: var(--muted); font-weight: 600; margin: 26px 0 8px; }
h2.section:first-of-type { margin-top: 0; }
h2.section .count { font-weight: 400; text-transform: none; letter-spacing: 0; }
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
.pub { margin-top: 10px; padding-top: 10px; border-top: 1px dashed var(--line);
  font-size: 13px; }
.pub-line { display: flex; gap: 8px; align-items: baseline; flex-wrap: wrap; }
.chan { color: var(--muted); font-size: 12px; min-width: 76px; }
.pkg { color: var(--ink); }
.ver { font-variant-numeric: tabular-nums; color: var(--muted); }
.behind { color: var(--warn); font-size: 12px; }
.same { color: var(--pass); font-size: 12px; }
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
.chip.stale-chip { opacity: 0.45; font-weight: 400; }
#age.old { color: var(--warn); font-weight: 600; }
footer { color: var(--muted); font-size: 12px; margin-top: 28px; text-align: center; }
footer a { color: var(--muted); }
@media (max-width: 600px) {
  .when { margin-left: 0; width: 100%; }
  .state { margin-left: 0; }
  .prs li { flex-wrap: wrap; }
}
"""


# Reload only when the data behind the page has actually changed, and say how
# old it is meanwhile. The build runs every 15 minutes and Pages caches for ten
# minutes, so polling faster than this buys nothing; and a board that quietly
# stops updating -- a delayed cron, or GitHub disabling the schedule after 60
# days of no activity -- should say so rather than look equally authoritative
# at one minute old and at three days.
REFRESH_JS = """<script>
(function () {
  var built = new Date("__GENERATED__");
  var el = document.getElementById("age");
  function human(ms) {
    var m = Math.round(ms / 60000);
    if (m < 1) return "just now";
    if (m < 60) return m + " min ago";
    var h = Math.round(m / 60);
    if (h < 24) return h + (h === 1 ? " hour ago" : " hours ago");
    var d = Math.round(h / 24);
    return d + (d === 1 ? " day ago" : " days ago");
  }
  function tick() {
    if (!el) return;
    var age = Date.now() - built.getTime();
    el.textContent = "updated " + human(age);
    // Two builds missed at a 15-minute cadence: suspect the schedule.
    el.className = age > 40 * 60000 ? "old" : "";
  }
  tick();
  setInterval(tick, 30000);
  setInterval(function () {
    fetch("status.json?t=" + Date.now(), { cache: "no-store" })
      .then(function (r) { return r.ok ? r.json() : null; })
      .then(function (d) {
        if (d && d.generated && d.generated !== "__GENERATED__") location.reload();
      })
      .catch(function () { /* offline or a bad deploy: keep showing the age */ });
  }, 120000);
})();
</script>"""


def published_html(repo, e):
    """What each registry serves, and whether the branch has moved past it."""
    rows = repo.get("published") or []
    if not rows:
        return []
    parts = ["<div class='pub'>"]
    for row in rows:
        if row["behind"]:
            note = f" <span class='behind'>source is {e(row['source'])}</span>"
        elif row["source"] and row["published"] not in ("", "not published"):
            note = " <span class='same'>= source</span>"
        elif row["source"]:
            # Declared by the manifest, absent from the registry: saying it
            # matches the source would be nonsense.
            note = f" <span class='behind'>source is {e(row['source'])}</span>"
        else:
            note = ""
        behind = note
        parts.append(
            f"<div class='pub-line'><span class='chan'>{e(row['channel'])}</span>"
            f"<span class='pkg'>{e(row['package'])}</span>"
            f"<span class='ver'>{e(row['published'])}</span>{behind}</div>"
        )
    parts.append("</div>")
    return parts


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
    parts.append(advisory_line("on main", adv["main"]))
    # Only when there is a release to compare against. Five repositories here
    # never cut one -- the uv projects do not release, nec2-js ships by tag --
    # so the row said the same non-fact about them on every build. What they
    # publish is in the block above.
    if adv["tag"]:
        parts.append(advisory_line("at release", adv["published"], e(adv["tag"])))
    # The gap is the actionable part: fixed on the branch, still out there in
    # the last release, and only a new tag closes it.
    fixed = {a["id"] for a in real(adv["main"])}
    out_there = real(adv["published"])
    if out_there and not [a for a in out_there if a["id"] in fixed]:
        parts.append(
            "<div class='stale'>Fixed on main but not in the newest GitHub "
            "release — cutting a tag ships the fix.</div>"
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
        f"<title>{e(owner)}'s CI Status</title>",
        f"<style>{CSS}</style></head><body><div class='wrap'>",
        (
            f"<div class='top'><h1>{e(owner)}'s CI Status</h1>"
            # Links rather than fires: triggering a workflow needs a token with
            # actions:write, and this page is public, so the only way to make
            # the button real is to put a credential in the browser.
            "<a class='rebuild' target='_blank' rel='noopener' "
            "href='https://github.com/"
            f"{e(owner)}/ci-dashboard/actions/workflows/build.yml'>Rebuild now "
            "&rarr;</a></div>"
        ),
        (
            f"<p class='sub'>Vulnerable releases and red builds first, then "
            f"anything awaiting review. Latest run of each workflow, wherever it "
            f"ran, and open pull requests across {len(repos)} active repositories. "
            f"Rebuilt {e(now.strftime('%Y-%m-%d %H:%M UTC'))} "
            "(<span id='age'></span>).</p>"
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
    titles = {
        0: "Needs attention",
        1: "Open pull requests",
        2: "Quiet",
    }
    seen_groups = set()
    for r in repos:
        g = group(r)
        if g not in seen_groups:
            seen_groups.add(g)
            n = sum(1 for x in repos if group(x) == g)
            parts.append(
                f"<h2 class='section'>{e(titles[g])} "
                f"<span class='count'>({n})</span></h2>"
            )
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
                if run["stale"]:
                    cls += " stale-chip"
                title = (
                    f" title='Ran {ago(run['finished'], now)}, before the last push'"
                    if run["stale"]
                    else ""
                )
                ref = f" @{e(run['ref'])}" if run.get("ref") else ""
                parts.append(
                    f"<a class='chip {cls}' href='{e(run['url'])}'{title}>"
                    f"{e(run['name'])} · {e(run['conclusion'] or 'n/a')}{ref}"
                    f"{' (stale)' if run['stale'] else ''}</a>"
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
        parts.extend(published_html(r, e))
        parts.extend(advisory_html(r, e))
        parts.append("</section>")
    footer = (
        "<footer>Built by <a href='https://github.com/"
        f"{e(owner)}/ci-dashboard'>ci-dashboard</a>. "
        "Public, non-fork repositories with CI or an open pull request. "
        "Advisories from <a href='https://osv.dev'>OSV</a>, which carries RustSec "
        "and GitHub's database: a GHSA- id is one <code>cargo audit</code> does "
        "not see. Yanked versions are registry state rather than advisories, so "
        "they appear only in <code>cargo audit</code>.</footer>"
    )
    parts.extend(
        (
            footer,
            REFRESH_JS.replace("__GENERATED__", now.isoformat()),
            "</div></body></html>",
        )
    )
    return "\n".join(parts)


# status.json is the contract between the two halves, not a by-product of
# rendering. Bump this when its shape changes in a way a reader would notice.
SCHEMA = 1


def do_collect(owner, out_dir):
    """Ask the world what is true, and write it down. Network, no HTML."""
    token = os.environ.get("GITHUB_TOKEN")
    if not token:
        sys.exit("GITHUB_TOKEN is required (the Actions token is enough)")
    now = dt.datetime.now(dt.timezone.utc)
    repos = collect(owner, token)
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "status.json")
    with open(path, "w") as f:
        json.dump(
            {
                "schema": SCHEMA,
                "generated": now.isoformat(),
                "owner": owner,
                "repos": repos,
            },
            f,
            indent=2,
        )
    print(f"{len(repos)} repositories written to {path}", file=sys.stderr)
    return path


def do_render(status_path, out_dir):
    """Turn what was written down into a page. No network."""
    with open(status_path) as f:
        data = json.load(f)
    if data.get("schema") != SCHEMA:
        sys.exit(
            f"{status_path} is schema {data.get('schema')}, this build.py reads {SCHEMA}"
        )
    now = dt.datetime.fromisoformat(data["generated"])
    os.makedirs(out_dir, exist_ok=True)
    path = os.path.join(out_dir, "index.html")
    with open(path, "w") as f:
        f.write(render(data["repos"], data.get("owner", "charlieh0tel"), now))
    print(f"{len(data['repos'])} repositories rendered to {path}", file=sys.stderr)


def main():
    # Declared once and attached to the top level and to each subcommand, so
    # `render --out-dir X` works as readily as `--out-dir X render`.
    # SUPPRESS rather than a real default: a subparser re-declaring an option
    # overwrites whatever the top level parsed, so `--out-dir X render` would
    # quietly write to the default directory instead of X.
    common = argparse.ArgumentParser(add_help=False)
    common.add_argument("--owner", default=argparse.SUPPRESS)
    common.add_argument("--out-dir", default=argparse.SUPPRESS)
    ap = argparse.ArgumentParser(
        parents=[common],
        description="Collect the fleet's status, and render it. With no "
        "subcommand it does both, which is what CI runs.",
    )
    sub = ap.add_subparsers(dest="command")
    sub.add_parser("collect", parents=[common], help="write status.json (network)")
    render_cmd = sub.add_parser(
        "render", parents=[common], help="write index.html from status.json"
    )
    render_cmd.add_argument("status", nargs="?")
    args = ap.parse_args()
    owner = getattr(args, "owner", "charlieh0tel")
    out_dir = getattr(args, "out_dir", os.path.dirname(os.path.abspath(__file__)))

    if args.command == "collect":
        do_collect(owner, out_dir)
    elif args.command == "render":
        do_render(args.status or os.path.join(out_dir, "status.json"), out_dir)
    else:
        do_render(do_collect(owner, out_dir), out_dir)


if __name__ == "__main__":
    main()
