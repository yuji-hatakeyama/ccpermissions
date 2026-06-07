# Contributing

```bash
git clone https://github.com/yuji-hatakeyama/ccpermissions.git
cd ccpermissions
uv sync                                      # install runtime + dev deps (pytest)
```

## Unit / orchestrator tests

```bash
uv run pytest                                # full suite (~160 tests, <1s)
uv run pytest tests/test_decide.py           # one module
uv run pytest tests/test_decide.py::test_name  # one test
uv run pytest -k "regex and not config"      # by expression
```

Test layout (one file per module so failures localise):

- `tests/test_parse.py`     — `bashlex`-aware command enumeration (text + argv).
- `tests/test_decide.py`    — pure-function unit tests for token matching.
- `tests/test_aggregate.py` — priority and reason composition.
- `tests/test_config.py`    — YAML loading, merging, and error reporting.
- `tests/test_cli.py`       — end-to-end JSON in / JSON out via the
  orchestrator (and one subprocess smoke test against the installed
  `ccpermissions-claude-code` console script).

`conftest.py` autouse-isolates `HOME`, `CLAUDE_CONFIG_DIR`, and
`CLAUDE_PROJECT_DIR` into `tmp_path` for every test, so the suite never
touches your real config.

## Driving the hook by hand

The CLI is JSON-in / JSON-out, so you can exercise a single decision
without involving Claude Code at all:

```bash
echo '{"tool_name":"Bash","tool_input":{"command":"rm -rf /tmp/x"}}' \
  | uv run python -m ccpermissions.cli
```

This honours `CLAUDE_CONFIG_DIR` and `CLAUDE_PROJECT_DIR` the same way the
real hook does, so point them at a scratch directory with a hand-written
`ccpermissions.yaml` to test rule changes in isolation.

## Integration testing against a real Claude Code session

To try the working tree as an actual PreToolUse hook without polluting your
real `~/.claude`, build an isolated `CLAUDE_CONFIG_DIR` sandbox that resolves
`ccpermissions-claude-code` from the local checkout via `uvx --from <path>`:

```bash
# 1. Build a throwaway config dir
export CCPERM_SANDBOX="$(mktemp -d)"
mkdir -p "$CCPERM_SANDBOX"

# 2. Point the hook at THIS checkout (no install needed)
cat > "$CCPERM_SANDBOX/settings.json" <<EOF
{
  "hooks": {
    "PreToolUse": [
      {
        "matcher": "Bash",
        "hooks": [
          {
            "type": "command",
            "command": "uvx --from '$PWD' ccpermissions-claude-code"
          }
        ]
      }
    ]
  }
}
EOF

# 3. Drop in rules to exercise
cat > "$CCPERM_SANDBOX/ccpermissions.yaml" <<'EOF'
version: 1
default: ask
rules:
  - all: ['ls']
    action: allow
  - all: [{regex: '(^|/)rm$'}, '-rf']
    action: deny
EOF

# 4. Launch Claude Code against the sandbox
CLAUDE_CONFIG_DIR="$CCPERM_SANDBOX" claude
```

Because `CLAUDE_CONFIG_DIR` overrides `~/.claude` wholesale, your real
settings, MCP servers, and history are untouched. Edit the working tree
and the next Bash call picks up the change — `uvx --from <path>` rebuilds
on source change. When you're done, `rm -rf "$CCPERM_SANDBOX"`.

## Updating pinned dependencies

`pyyaml` and `bashlex` are pinned to a patch version. To take a security
update:

```bash
uv lock --upgrade-package pyyaml
# bump the version in pyproject.toml, review diff, open a PR
```
