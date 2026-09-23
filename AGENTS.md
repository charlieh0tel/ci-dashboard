# Agent Instructions for ci-dashboard

- `build.py` is the whole tool: one file, standard library only. No new
  dependencies.
- `collect` does all network I/O and writes `status.json`; `render` reads only
  that file. Changing the file's shape means bumping `SCHEMA`.
- Before committing: `uvx ruff check --preview . && uvx ruff format --check .`
  must pass. Fix findings rather than adding `# noqa`.
- No test suite: check a change by running `render` (and `collect` if it
  touches the API) and looking at `_site/index.html`.
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
