# ci-dashboard

One page showing, for every active repository in this account: the result of
each workflow on its default branch, and every open pull request with its check
status.

**<https://charlieh0tel.github.io/ci-dashboard/>**

GitHub has no cross-repository view of CI. It does have one for pull requests --
<https://github.com/pulls?q=is%3Aopen+is%3Apr+author%3A%40me>, or
`gh search prs --owner charlieh0tel --state open` -- so the pull request half of
this page is a convenience; the CI half is the part you cannot get anywhere else.

## How it works

Two halves with a file between them. `build.py collect` asks the API what is
true and writes `status.json`; `build.py render` turns that file into
`index.html` and touches no network. Run with no subcommand it does both,
which is what CI does in two steps.

```sh
GITHUB_TOKEN=$(gh auth token) python3 build.py collect --out-dir _site
python3 build.py render --out-dir _site      # no token, no network, instant
```

That split is worth having because rendering is where the iterating happens: a
layout change costs a 38ms re-render rather than a two-minute crawl, and the
same file can feed anything else that wants the data. `status.json` carries a
`schema` field, and `render` refuses a file it does not recognise. A workflow
runs it every 15 minutes and deploys the result to Pages. The page is completely
static: it makes no API calls of its own, because a browser hitting the GitHub
API is capped at 60 requests an hour per visitor, and a page that needs a token
is a page that leaks one. `status.json` is published alongside it if you want to
script against the same data.

Rebuild on demand rather than waiting for the half hour: the **rebuild now**
link in the page footer goes to the workflow, where *Run workflow* fires it, or

```sh
gh workflow run build.yml -R charlieh0tel/ci-dashboard
```

The page has no button of its own on purpose. Firing a workflow needs a token
with `actions: write`, and this page is public -- a button here would mean
shipping that token to every visitor. A fresh build still lands behind Pages'
ten-minute CDN cache.

A `lint` job checks `build.py` with ruff on every push and every quarter hour,
against current ruff with preview rules. It runs beside the build rather than
before it: a style finding should not stop the board from telling you what is
red.

Check it locally the same way CI does -- `uvx ruff`, unpinned, so you see what
CI will see:

```sh
uvx ruff check --preview . && uvx ruff format --check .
```

Pull requests get their own workflow, `pr.yml`, which does everything short of
deploying: the same ruff commands, `actionlint` over the workflows, a render of
a committed fixture, and a live `collect` and `render` into a scratch
directory. The fixture is what makes the render check repeatable -- the live
data changes hourly, `tests/status-fixture.json` does not -- and a second
test file drives `collect` against a network that refuses everything:

```sh
python3 -m unittest discover -s tests
```

The live run on a pull request collects four repositories, not the account,
using `collect --repo OWNER/NAME` (repeatable): the Actions token's allowance
is shared with the build that deploys, and a check has no use for the other
fifty-odd. The job fails if any of its four repositories is missing or comes
back unchecked, and, through `collect --max-requests`, if collecting them took
more GitHub API requests than it should -- see Cost and freshness.

Run it locally the same way CI does:

```sh
GITHUB_TOKEN=$(gh auth token) python3 build.py --out-dir _site
```

No dependencies beyond the standard library, and Python 3.10 or newer.
`ruff.toml` pins ruff's target to 3.10 so it never suggests something newer.

## Published versions

Each repository shows the newest tag it carries, then what is actually
installable per channel: crates.io, npm, and the APT repository. The tag row
says whether it is a GitHub release or a bare tag, so a version on a registry
can be read against the tag that produced it.

Registry rows are annotated with the manifest: `= source` when they agree,
`source is X` when the branch has moved on, and `not published` for a package
the manifest declares that no registry serves.

`not published` is a signal, not noise -- it is what would have shown
usbrelay-rs sitting at 0.1.1 unreleased for months. A crate that is never
going to a registry says so in its own manifest with `publish = false`, which
also makes `cargo publish` refuse, and it drops off the board. npm's `private:
true` does the same for a package. The package names come from the manifests, never from the
repository name, and a registry's answer counts only if its own `repository`
field points back at that repository.

That check is not theoretical. `weather-rs` publishes a crate called `weather`,
and crates.io has a `weather` crate belonging to somebody else since 2016.
Matching on name alone would report a stranger's release as yours.

The APT column reads apt-repo's own `packages.tsv` for the mapping and its
published `Packages` index for the versions, so it reports what an `apt
install` would fetch today. One repository can ship several packages --
renogymon ships four -- so the mapping is matched as a prefix. Debian revisions
(`-1`, `+ci...~git...`) are not upstream versions and are shown as they are.

The GitHub release is deliberately not the source of truth here: usbrelay-rs
carried a `v0.1.1` release for months while crates.io still served 0.1.0.

## Advisories

For every repository with a lockfile, the board checks the dependencies against
[OSV](https://osv.dev). Three are read, each against its own ecosystem:

| lockfile | ecosystem | what covers it otherwise |
|---|---|---|
| `Cargo.lock` | crates.io | `cargo audit` in CI, Dependabot |
| `uv.lock` | PyPI | `pip-audit` in CI only |
| `package-lock.json` | npm | Dependabot |

`uv.lock` is the reason this exists in its current form. GitHub does not list
uv as a supported ecosystem, so Dependabot will not open a fix PR for those
projects; outside their own CI, this board is where their advisories appear.

It checks two git refs, because they answer different questions:

- **on main** -- is the problem fixed?
- **at release** (the newest GitHub release) -- does the code that release was
  cut from still have it?

A repository that publishes without cutting GitHub releases -- nec2-js ships
by pushing `<package>@<version>` tags -- has no release to compare against, so
it simply has no second row. What it publishes is in the section above.

Those diverge exactly when it matters. A merged fix does nothing for anyone
until a tag ships it, so a repository whose branch is clean and whose release is
not gets called out, and sorts to the top of the page: a red build is your
problem, a vulnerable release is everyone else's.

Expect this to find *more* than the `cargo audit` job in a repo's CI, not fewer.
cargo audit reads RustSec alone; OSV carries RustSec plus GitHub's own database,
and some crates.io advisories were only ever filed as a GHSA -- the tract-onnx
arbitrary-file-read and the tar PAX issue currently showing here are both
invisible to cargo audit. An id beginning `RUSTSEC-` is one CI would also flag;
a `GHSA-` id is one only this board sees. The RustSec advisory and its GHSA twin
are the same finding and are counted once.

Yanked versions are the one thing this board cannot see. A yank is registry
state, not an advisory, so OSV has no record of it and only `cargo audit` --
which reads crates.io directly -- will tell you. The gap runs both ways: the
board sees GHSA-only advisories that cargo audit misses, and cargo audit sees
yanks the board misses. Neither on its own is the whole picture.

Unmaintained crates are counted separately from vulnerabilities.
`cargo audit` treats them as warnings and so does this, because a crate nobody
maintains is worth knowing about but is not the same as an advisory.

Dependabot security updates cover the other half -- it opens the bump PR when a
fix exists. This board is what shows the ones it cannot fix yet, and what is
still out in a release.

## What lands on the board

Public, non-fork, non-archived repositories, plus anything in `EXTRA_REPOS` --
which is how `PAARA-org/w6otx` gets on. A repository still has to have either CI
or an open pull request to earn a row, or the board fills up with dormant
repositories that will never show anything.

Each repository shows the newest run of every workflow it has, wherever that
run happened. Filtering to the default branch would hide a workflow that only
runs on tags: nec2-js releases by pushing `<package>@<version>` tags, so its
Release workflow never touches main, and the only chip it ever showed was a
stray `workflow_dispatch` from August that failed. A chip from another ref says
which one, as `Release - success @necpp-wasm@0.2.3`.

A run older than the last push is dimmed and marked stale: it describes code
that is no longer here. That applies to default-branch runs only -- a tag run
describes its tag and stays true however far main moves afterwards.

Rows fall into three labelled sections, so the order is visible rather than
inferred:

- **Needs attention** -- a published release carrying an advisory, or a red
  default branch. Sorted by how many advisories, worst first.
- **Open pull requests** -- nothing wrong, something waiting. Sorted by count.
- **Quiet** -- alphabetical.

A vulnerable release outranks a red build: the build is your problem, the
release is everyone else's.

## When something cannot be checked

"Does not exist" and "could not check" are different answers and the board
keeps them apart. A 404 is an answer: no such lockfile, no release, no such
package on the registry. A rate limit (a 403 or 429 from GitHub, primary or
secondary), any other refusal, a server error that outlasts three attempts and
a network failure are not answers, and `collect` used to treat the 403 among
them exactly like a 404 -- so a build that ran out of API allowance published
a board with rows and details quietly missing.

Now the build still deploys, and says what it could not find out:

- A repository with any unanswered question gets a row in a **Could not
  check** section at the top, carrying the reason and nothing else. Half a row
  would show "no advisories" for the half that was never asked.
- A warning above the summary says how many repositories could not be checked
  and why, and the same for the two things that are not a repository: the
  repository list and the APT index.
- The favicon goes amber when nothing is known to be failing but something
  could not be checked.

In `status.json` each repository carries `unchecked` (the reason, or `null`),
and the top level carries an `unchecked` list for the rest. That changed the
file's shape, so `schema` is 2.

A 401, or any other 4xx that means the request itself was wrong, still stops
the build: that is this repository's own fault and every request would fail
the same way.

## Private repositories

They are deliberately absent. GitHub Pages is public even when served from a
private repository, so putting them on the board would publish their names,
branches and failure messages to anyone with the URL. If you want them anyway,
set a `DASHBOARD_TOKEN` secret (fine-grained PAT, read-only on contents,
metadata, actions and pull requests), drop the `private` filter in
`repos_for()`, and understand what becomes public.

## Cost and freshness

Four runs an hour, under two minutes each. GitHub delays scheduled workflows
under load and drops them entirely on repositories with no activity for 60
days, so treat the timestamp on the page as the truth rather than assuming it
is current. `workflow_dispatch` rebuilds it on demand.

The number that matters is API requests. The Actions token gets this
repository 1,000 REST requests an hour and, counted separately, 1,000 GraphQL
points, across all of its workflows. A full crawl of 62 repositories used to
be about 720 REST requests, so the schedule only fitted because GitHub was
dropping most of it. It is now about 100:

| what | requests |
|---|---|
| one GraphQL query per ten repositories: branch, pull requests and their checks, lockfiles and manifests at the branch, the release and its lockfiles, the newest tag | 7 (62 points) |
| workflow runs, which GraphQL does not have: one page per repository, and the default branch's page only when the first did not hold every run | 85 |
| the repository list, the APT map, an npm workspace's members | 6 |

Four of those an hour is about 370 REST requests and 250 points, which leaves
room for push builds and pull request checks. `collect` prints the tally as
its last line, and the pull request check fails if its four repositories take
more than 25 requests (14 today), so a change that goes back to asking per
file or per pull request is caught before it merges. OSV and the package
registries are not GitHub and are not counted against any of this.
