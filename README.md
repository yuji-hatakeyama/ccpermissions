# ccpermissions

A Claude Code [PreToolUse hook](https://code.claude.com/docs/hooks#pretooluse)
that gates Bash tool execution through an **ordered list of token-matching rules**.

For each Bash command, ccpermissions reduces it to its argv (token list) and
walks the merged rules, returning the action (`allow` / `ask` / `deny`) of the
**last** rule whose conditions match. Rules read top-to-bottom but the last
match wins, so you can layer broad defaults followed by specific exceptions.

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
    none: ['push']                               # ... except git push (falls to default ask)
    action: allow
  - all: [{regex: '(^|/)git$'}, 'push', {regex: '^(--force|-f$)'}]
    action: deny                                 # block force push (incl. --force-with-lease, --force=...)
  - all: [{regex: '(^|/)rm$'}, '-rf']            # rm -rf anywhere in argv
    action: deny
  - all: [{regex: '^(npm|pnpm|bun)$'}, 'test']   # npm/pnpm/bun test
    action: allow
```

The `(^|/)<name>$` pattern matches the command both when invoked by basename
(`git`) and by absolute path (`/usr/bin/git`), without spilling onto tokens
like `gitignore`, `git-lfs`, or `gitlab` that merely contain the substring.

Layering note: `none: ['push']` on the broad allow keeps plain `git push`
from being silently allowed — it falls through to the default `ask` so you
confirm before pushing, while the deny rule catches force-push variants
outright.

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

## Shell-aware analysis

The parser walks the [`bashlex`](https://pypi.org/project/bashlex/) AST and
yields every command position that would actually execute, so rules fire on
the inner command, not just the literal `tool_input.command` string. Covered:

- separators and operators — `;`, `&&`, `||`, `|`, `&`, newlines
- subshells and brace groups — `( … )`, `{ …; }`
- control flow — `if`, `for`, `while`, `until`
- command substitution — `$(…)`, backticks
- process substitution — `<(…)`, `>(…)`
- wrapper unwrapping — `sh -c`, `bash -c`, `zsh -c`, `dash -c`, `sudo`,
  `env`, `find -exec … \;` / `… +`, `eval`

The aggregator picks the strongest action across all positions
(`deny > ask > allow`). Input `bashlex` cannot parse (malformed shell,
`case … esac`) votes `default` rather than `allow`, so unparseable input is
never silently let through.

## Composing with other plugins' hooks

Claude Code aggregates `permissionDecision` across all matching `PreToolUse`
hooks with the priority **`deny > defer > ask > allow`** ([docs](https://code.claude.com/docs/hooks#pretooluse)).
If another plugin returns `deny`, ccpermissions returning `allow` will not
override it.

