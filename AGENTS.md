# Agent Instructions for ci-dashboard

- `build.py` is the whole tool: one file, standard library only. No new
  dependencies.
- `collect` does all network I/O and writes `status.json`; `render` reads only
  that file. Changing the file's shape means bumping `SCHEMA`.
- Before committing: `uvx ruff check --preview . && uvx ruff format --check .`
  must pass. Fix findings rather than adding `# noqa`.
- Tests: `python3 -m unittest discover -s tests` renders
  `tests/status-fixture.json` and checks the page, and checks how `collect`
  reports requests that were refused. Keep the fixture in step with `SCHEMA`.
  For anything else, run `render` (and `collect` if the change touches the
  API) and look at `_site/index.html`.
- GitHub API requests are budgeted: see Cost and freshness in the README. Add
  fields to the `SNAPSHOT` query before adding a REST call per repository,
  and check the tally `collect` prints.
- All network I/O goes through `fetch()`: None means a 404, and anything else
  that is not an answer raises `CouldNotCheck`. Never catch that to return an
  empty result.
- Be Pythonic: comprehensions, `with`, unpacking, `dict.get`, `any`/`all`,
  and the stdlib before a hand-rolled helper.
- DRY: reuse or extend existing helpers (`api()`, `dedupe()`, `ago()`, ...)
  instead of near-copies. Don't abstract something with one caller.
- Match the file: plain functions and dicts, no type hints, f-strings,
  `os.path`.
- Catch narrow exceptions only, and log anything skipped to stderr. "Could not
  check" must never render the same as "nothing found".
- `html.escape()` every API value that reaches the page. The page stays
  static, with no token and no private repositories.
- Comments explain why, not what. Update comments and README with the code.
- Pin every `uses:` to a full SHA with the tag in a comment. Never pipe a
  remote script into a shell.
- Commit subjects are plain imperative with no prefix; the body says what was
  wrong and why. One change per commit.
- Do not add Claude attribution to commit or PR bodies.
