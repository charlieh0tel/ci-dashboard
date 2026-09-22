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
runs it twice an hour and deploys the result to Pages. The page is completely
static: it makes no API calls of its own, because a browser hitting the GitHub
API is capped at 60 requests an hour per visitor, and a page that needs a token
is a page that leaks one. `status.json` is published alongside it if you want to
script against the same data.

Run it locally the same way CI does:

```sh
GITHUB_TOKEN=$(gh auth token) python3 build.py --out-dir _site
```

No dependencies beyond the standard library.

## Advisories

For every repository with a `Cargo.lock`, the board checks the dependencies
against [OSV](https://osv.dev), which carries the RustSec database -- the same
advisories `cargo audit` reports, without needing a Rust toolchain here.

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

Rows sort worst-first: failing default branch, then unknown, then by number of
open pull requests.

## Private repositories

They are deliberately absent. GitHub Pages is public even when served from a
private repository, so putting them on the board would publish their names,
branches and failure messages to anyone with the URL. If you want them anyway,
set a `DASHBOARD_TOKEN` secret (fine-grained PAT, read-only on contents,
metadata, actions and pull requests), drop the `private` filter in
`repos_for()`, and understand what becomes public.

## Cost and freshness

Two runs an hour, a few seconds each. GitHub delays scheduled workflows under
load and drops them entirely on repositories with no activity for 60 days, so
treat the timestamp on the page as the truth rather than assuming it is current.
`workflow_dispatch` rebuilds it on demand.
