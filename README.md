# ccpermissions

A Claude Code [PreToolUse hook](https://code.claude.com/docs/hooks#pretooluse)
that gates Bash tool execution through an **ordered list of token-matching rules**.

For each Bash command, ccpermissions reduces it to its argv (token list) and
walks the merged rules, returning the action (`allow` / `ask` / `deny`) of the
**last** rule whose conditions match. Rules read top-to-bottom but the last
match wins, so you can layer broad defaults followed by specific exceptions —
the opposite of "first match wins" firewall semantics.

## Setup

Wire `ccpermissions-claude-code` into `~/.claude/settings.json` (or the
project's `.claude/settings.json`) as a PreToolUse hook on `Bash`:

```json
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "uvx --from 'ccpermissions>=0.1,<0.2' ccpermissions-claude-code"
          }
        ]
      }
    ]
  }
}
```

`uvx` resolves the package on first use and caches it, keeping the version
pinned without a separate install step.

The package name is `ccpermissions`; the console script is
`ccpermissions-claude-code`. The `-claude-code` suffix names the host the
hook protocol targets, leaving room for sibling scripts (e.g.
`ccpermissions-codex`) without an `--endpoint` flag.

## Configuration

The CLI reads YAML from **two optional locations** and merges them:

| Scope   | Path                                                       |
| ------- | ---------------------------------------------------------- |
| user    | `${CLAUDE_CONFIG_DIR:-$HOME/.claude}/ccpermissions.yaml`   |
| project | `${CLAUDE_PROJECT_DIR}/.claude/ccpermissions.yaml`         |

Merge semantics:

- `rules = user.rules + project.rules` — project rules are evaluated **after**
  user rules, so project rules win on conflict (since "last match wins").
- `default = project.default ?? user.default ?? "ask"`.

If neither file exists, every command falls back to `default = ask`.

### Example

```yaml
version: 1
default: ask
rules:
  - all: [{regex: '(^|/)git$'}]                  # any git command (incl. /usr/bin/git)
    none: ['push']                               # ... except git push (handled below)
    action: allow
  - all: [{regex: '(^|/)git$'}, 'push']          # git push ...
    none: ['--force', '-f', {regex: '^--force='}] # ... but force push falls through to default (ask)
    action: ask
  - all: [{regex: '(^|/)rm$'}, '-rf']            # rm -rf anywhere in argv
    action: deny
  - all: [{regex: '^(npm|pnpm|bun)$'}, 'test']   # npm/pnpm/bun test
    action: allow
```

The `(^|/)<name>$` pattern matches the command both when invoked by basename
(`git`) and by absolute path (`/usr/bin/git`), without spilling onto tokens
like `gitignore`, `git-lfs`, or `gitlab` that merely contain the substring.

Layering note: the `none: ['push']` on the broad allow is what lets the
second rule's `none` actually do something — without it, `git push --force`
would be allow-suppressed by rule 2 but fall back to the broad allow above
and bypass the default `ask`.

- `version: 1` is **required**.
- `default` is `allow`, `ask`, or `deny` (defaults to `ask`).
- `rules` is a list of mappings, each with `all` (required), `none` (optional),
  and `action` (required).

### Rule semantics

A rule matches a command when **every** `all` element matches some argv token
**and no** `none` element matches any token. Both lists are matched against the
command's tokens, order-independent.

- **`all`** — an existence check (AND). Each element must match *some* token,
  in any position. `all: ['git', 'push']` matches `git push`, `git -c x.y push`,
  even `echo git push` — the check is intentionally broad, so prefer it for
  `allow` rules and write more specific `all` lists for `deny` / `ask`.
- **`none`** — an exclusion (OR). If *any* element matches *any* token, the rule
  is skipped. Use it to carve exceptions out of a broad `all`.
- **Element forms**:
  - A **string** matches a whole token by exact equality — `push` never matches
    the token `pushup` or `--push`, only `push`.
  - A **`{regex: '<pattern>'}`** mapping is a Python regex matched with
    `re.search` against each token. **Anchoring is yours**: `{regex: 'npm'}`
    matches any token containing `npm` (e.g. `npm-check`), while
    `{regex: '^npm$'}` matches exactly the token `npm`.
- Because matching is per whole token, `--force=true` is a single token that the
  literal `--force` does **not** match; use `{regex: '^--force'}` to catch the
  `--force=...` form.
- A literal name like `git` does not match the absolute-path form
  `/usr/bin/git`. Use `{regex: '(^|/)git$'}` to match either; the `(^|/)` anchor
  keeps it from also hitting `gitignore` / `git-lfs` / `gitlab`.

## Behavior

| Situation                             | `permissionDecision` | `permissionDecisionReason`                |
| ------------------------------------- | -------------------- | ----------------------------------------- |
| Matched an `allow` rule               | `allow`              | (omitted)                                 |
| Matched an `ask` rule                 | `ask`                | `matched rule: all=[…] -> ask`            |
| Matched a `deny` rule                 | `deny`               | `matched rule: all=[…] -> deny`           |
| Multiple `deny` matches across a pipeline / wrapper | `deny`     | `matched N deny rules:` then a per-source list |
| No rule matched                       | the `default` action | (omitted)                                 |
| Config file failed to load            | `ask`                | error summary (file path + reason)        |
| Both files failed to load             | `ask`                | error summaries joined by ` \| `          |
| `tool_input.command` missing/non-str  | `ask`                | error summary                             |
| `tool_name` is not `"Bash"`           | (no output)          | (Claude Code's default permission flow)   |

A rule's reason label is its `all=[…]` list (with `none=[…]` appended when set);
regex elements are shown with an `re:` prefix, e.g.
`matched rule: all=[re:^(npm|pnpm|bun)$, test] -> allow`.

Why reasons appear only on `ask` and `deny`:

- `ask` reasons are shown in the **user's permission dialog**, so the user
  immediately sees which rule (or which config error) triggered the prompt.
- `deny` reasons are surfaced to **Claude's context**, helping the model adjust
  its next step rather than guessing why a tool failed.
- `allow` and default-fallback reasons would be noise (no decision to explain).

## Shell-aware analysis

The parsing layer walks the [`bashlex`](https://pypi.org/project/bashlex/) AST
and reduces each executable command position to its argv (token list), so a
rule fires on every command that would actually run, not just the literal
`tool_input.command` string. This covers:

- separators and operators — `;`, `&&`, `||`, `|`, `&`, newlines
- subshells and brace groups — `( … )`, `{ …; }`
- control flow — `if`, `for`, `while`, `until`
- command substitution — `$(…)`, backticks
- process substitution — `<(…)`, `>(…)`
- wrapper unwrapping — `sh -c`, `bash -c`, `zsh -c`, `dash -c`, `sudo`,
  `env`, `find -exec … \;` / `… +`, `eval`

When `bashlex` cannot parse the input (malformed shell, `case … esac`), the
parser produces a single **unanalyzable** segment. The aggregator treats it
as a vote of `default`, so the user is never silently allowed past
unparseable input.

The aggregator picks the strongest action across all command positions
(`deny > ask > allow`) and composes a multi-line reason listing only the
contributors to the winning action.

## Composing with other plugins' hooks

Claude Code aggregates `permissionDecision` across all matching `PreToolUse`
hooks with the priority **`deny > defer > ask > allow`** ([docs](https://code.claude.com/docs/hooks#pretooluse)).
If another plugin returns `deny`, ccpermissions returning `allow` will not
override it.

## Library use

Each module exposes a pure function suitable for embedding:

```python
from ccpermissions.parse     import enumerate_commands
from ccpermissions.aggregate import aggregate
from ccpermissions.config    import load_merged

cfg      = load_merged()
commands = enumerate_commands("git status && rm -rf /")
result   = aggregate(commands, cfg.rules, cfg.default)
print(result.action, result.reason)
```

`decide(argv, rules, default)` is the pure core: it takes a single command's
token list and returns the winning `Decision`. All public types are frozen
dataclasses; nothing here touches global state beyond reading environment
variables and YAML files in `config.load_merged`.

## Contributing

See [CONTRIBUTING.md](CONTRIBUTING.md) for the dev setup, running tests,
driving the hook by hand, and a sandboxed integration-test recipe that
exercises the working tree against a real Claude Code session without
touching your real `~/.claude`.

## License

[MIT](LICENSE).
