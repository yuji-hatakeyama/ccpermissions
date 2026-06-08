# Contributing

```bash
uv sync           # install runtime + dev deps (pytest)
uv run pytest     # full suite (~160 tests, <1s)
```

Tests are organised one file per module (`tests/test_<module>.py`).
`conftest.py` autouse-isolates `HOME`, `CLAUDE_CONFIG_DIR`, and
`CLAUDE_PROJECT_DIR` into `tmp_path`, so the suite never touches your real
config.

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

`pyyaml` and `bashlex` are pinned to a patch version. Bump via
`uv lock --upgrade-package <name>` plus the matching `pyproject.toml` edit.
