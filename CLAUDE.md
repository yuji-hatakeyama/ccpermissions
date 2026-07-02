# CLAUDE.md

This file provides guidance to Claude Code (claude.ai/code) when working with code in this repository.

## Commands

```bash
uv sync                          # install deps (incl. pytest)
uv run pytest                    # full test suite
uv run pytest tests/test_decide.py::test_name   # one test
uv run python -m ccpermissions.cli < payload.json   # exercise the hook locally
```

There is no lint / type-check step configured; tests are the only gate.

## Architecture

This package ships `ccpermissions-claude-code` (a console script →
`cli:main`) which the user wires into Claude Code's `settings.json` as a
**PreToolUse** hook: PreToolUse JSON in on stdin, `hookSpecificOutput` JSON
out on stdout. The hook never raises — every failure path becomes
`ask + permissionDecisionReason` so the user is never silently locked out.

The pipeline, in execution order, with one module per stage:

1. **`config.load_merged`** — reads two optional YAML files
   (`${CLAUDE_CONFIG_DIR:-~/.claude}/ccpermissions.yaml` and
   `${CLAUDE_PROJECT_DIR}/.claude/ccpermissions.yaml`), validates them, and
   returns `rules = user.rules + project.rules` (project rules evaluated
   **last** — see decide step) and `default = project.default ?? user.default
   ?? "ask"`. Both files optional; errors collected into `LoadResult.error`
   rather than raised. A custom `SafeLoader` rejects duplicate mapping keys so
   a typo can't silently drop a rule element.
2. **`parse.enumerate_commands`** — walks the `bashlex` AST and yields one
   `ExtractedCommand` per command position that would actually execute. Covers
   operators (`;`, `&&`, `|`, …), control flow, command/process substitution,
   and re-parses common wrappers (`sh|bash|zsh|dash -c`, `sudo`, `env`,
   `find -exec`, `eval`) so rules fire on the inner command too. Unparseable
   input (incl. `case … esac`, which `bashlex` flags via `NotImplementedError`)
   becomes a single `unanalyzable=True` entry.
3. **`decide.decide`** — the pure core. For each command's argv, walks rules in
   order and returns the **last** matching rule (last-match-wins, the opposite
   of firewall "first match wins"). A rule matches when **every** `all`
   matcher hits *some* token (AND, order-independent) and **no** `none`
   matcher hits any token. Matchers are either literal whole-token equality or
   `re.search`-against-each-token regex (anchoring is the author's job).
4. **`aggregate.aggregate`** — picks the strongest action across all command
   positions with priority `deny > ask > allow`, then composes a multi-line
   `permissionDecisionReason` listing only the contributors to the winning
   action. `unanalyzable` segments vote `default` (not `allow`). Pure
   default-fallback `ask` wins emit no reason; pure-default `deny` and
   `none`-suppressed fallbacks explain themselves.

### Invariants worth knowing before editing

- **`tool_name != "Bash"`** → produce no output (let Claude Code's default
  permission flow run); this is intentional.
- **Last-match-wins** is load-bearing for layering broad defaults followed by
  specific exceptions. Project rules go **after** user rules so they win.
- **Whole-token matching**: literal `--force` does not match the token
  `--force=true`. Use `{regex: '^--force'}` to catch the `=`-suffixed form.
- **Error path**: a `LoadResult` carrying an `error` must have `rules=()` and
  `default="ask"` (enforced in `__post_init__`). The CLI surfaces the error
  string verbatim as `permissionDecisionReason`.
- **Reason omission**: `allow` and pure-default `ask` winners emit no reason
  on purpose — `ask` reasons appear in the user's permission dialog, `deny`
  reasons go into Claude's context, but `allow`/default-`ask` reasons would be
  noise. Pure-default `deny` and `none`-suppressed fallbacks *do* emit one.
- **No I/O outside `config.load_merged` and `cli`**. `parse`, `decide`,
  `aggregate` are pure — keep them that way; they're the embeddable surface
  documented in the README.
- **PyYAML import is deferred** in `config` so a no-config install pays nothing
  for it on each PreToolUse.

### Test layout

One file per module: `test_parse.py`, `test_decide.py`, `test_aggregate.py`,
`test_config.py`, `test_cli.py`. `conftest.py` autouse-isolates `HOME`,
`CLAUDE_CONFIG_DIR`, `CLAUDE_PROJECT_DIR` into `tmp_path` for every test.
`test_cli.py` also runs one subprocess smoke test against the installed
console script.

## Project-specific conventions

- Single-line commit messages by default (see `memory/feedback_commit_messages.md`).
- The `-claude-code` suffix on the console script names the host the hook
  protocol targets — future hosts get their own script
  (e.g. `ccpermissions-codex`) rather than an `--endpoint` flag.
- `pyyaml` and `bashlex` are intentionally **pinned to a patch version**; bump
  via `uv lock --upgrade-package <name>` + manual `pyproject.toml` edit.
