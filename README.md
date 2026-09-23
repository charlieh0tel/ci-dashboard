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

`build.py` queries the API and writes `index.html` and `status.json`. A workflow
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

Run it locally the same way CI does:

```sh
GITHUB_TOKEN=$(gh auth token) python3 build.py --out-dir _site
```

No dependencies beyond the standard library.

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

It checks two refs, because they answer different questions:

- **main** -- is the problem fixed?
- **published** (the newest release tag) -- does what people can `apt install`
  today still have it?

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

Rows fall into three labelled sections, so the order is visible rather than
inferred:

- **Needs attention** -- a published release carrying an advisory, or a red
  default branch. Sorted by how many advisories, worst first.
- **Open pull requests** -- nothing wrong, something waiting. Sorted by count.
- **Quiet** -- alphabetical.

A vulnerable release outranks a red build: the build is your problem, the
release is everyone else's.

## Private repositories

They are deliberately absent. GitHub Pages is public even when served from a
private repository, so putting them on the board would publish their names,
branches and failure messages to anyone with the URL. If you want them anyway,
set a `DASHBOARD_TOKEN` secret (fine-grained PAT, read-only on contents,
metadata, actions and pull requests), drop the `private` filter in
`repos_for()`, and understand what becomes public.

## Cost and freshness

Four runs an hour, a few seconds each. GitHub delays scheduled workflows under
load and drops them entirely on repositories with no activity for 60 days, so
treat the timestamp on the page as the truth rather than assuming it is current.
`workflow_dispatch` rebuilds it on demand.
