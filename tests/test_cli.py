"""End-to-end tests for the CLI orchestrator.

These tests exercise the full path from PreToolUse JSON on stdin through to
`hookSpecificOutput` JSON on stdout, including config loading, merging, the
pure `decide()` core, and the reason-formatting rules. One subprocess test
also verifies the installed `ccpermissions-claude-code` console script.
"""

import io
import json
import os
import subprocess
import sys
from collections.abc import Sequence
from pathlib import Path
from typing import Optional, Union

import pytest

from ccpermissions.cli import main


def run_main(stdin_text: str) -> Optional[dict]:
    """Invoke `cli.main` with `stdin_text` and return the decoded JSON output.

    Args:
        stdin_text: Raw bytes (str) to feed to the orchestrator's stdin.

    Returns:
        Parsed stdout payload as a `dict`, or `None` when the orchestrator
        chose to emit nothing (defensive `tool_name` check).
    """
    stdin = io.StringIO(stdin_text)
    stdout = io.StringIO()
    rc = main(stdin=stdin, stdout=stdout)
    assert rc == 0, f"main returned non-zero: {rc}"
    out = stdout.getvalue()
    if not out:
        return None
    return json.loads(out)


def hook_input(command: str) -> str:
    """Build a representative PreToolUse JSON envelope for `command`."""
    return json.dumps(
        {
            "session_id": "test-session",
            "hook_event_name": "PreToolUse",
            "tool_name": "Bash",
            "tool_input": {"command": command, "description": "test"},
            "tool_use_id": "toolu_test",
        }
    )


def assert_decision(
    output: dict,
    action: str,
    reason_contains: Union[None, str, Sequence[str]] = None,
):
    """Assert the orchestrator's output payload.

    Args:
        output: Parsed `hookSpecificOutput` dict from `run_main`.
        action: Expected `permissionDecision`.
        reason_contains: When `None`, asserts no reason is present. When a
            string, asserts the reason contains it. When a sequence, asserts
            the reason contains every element.
    """
    hso = output["hookSpecificOutput"]
    assert hso["hookEventName"] == "PreToolUse"
    assert hso["permissionDecision"] == action
    if reason_contains is None:
        assert "permissionDecisionReason" not in hso
        return
    reason = hso.get("permissionDecisionReason", "")
    needles = (
        [reason_contains] if isinstance(reason_contains, str) else list(reason_contains)
    )
    for needle in needles:
        assert needle in reason, f"expected {needle!r} in reason {reason!r}"


# --- decisions -------------------------------------------------------------


def test_allow_rule_no_reason(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
    """)
    out = run_main(hook_input("ls -la"))
    assert_decision(out, "allow")


def test_deny_rule_includes_rule_label(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['rm', '-rf']
            action: deny
    """)
    out = run_main(hook_input("rm -rf /"))
    assert_decision(out, "deny", reason_contains=["all=[rm, -rf]", "deny"])


def test_ask_rule_includes_rule_label(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['git', 'push']
            action: ask
    """)
    out = run_main(hook_input("git push origin main"))
    assert_decision(out, "ask", reason_contains=["all=[git, push]", "ask"])


def test_default_deny_fallback_surfaces_explanation(write_user_config):
    """Default-deny silence used to leave Claude guessing why a tool was blocked;
    the orchestrator now spells out that no rule matched."""
    write_user_config("""
        version: 1
        default: deny
        rules: []
    """)
    out = run_main(hook_input("anything"))
    assert_decision(out, "deny", reason_contains=["no rule matched", "default is deny"])


def test_default_ask_when_default_omitted(write_user_config):
    write_user_config("""
        version: 1
        rules: []
    """)
    out = run_main(hook_input("anything"))
    assert_decision(out, "ask")


def test_last_match_wins_end_to_end(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['git']
            action: allow
          - all: ['git', 'commit']
            action: deny
    """)
    out = run_main(hook_input("git commit -m 'x'"))
    assert_decision(out, "deny", reason_contains="all=[git, commit]")


# --- bashlex-aware behavior -----------------------------------------------


def test_chained_command_deny_wins(write_user_config):
    """`git status && rm -rf /` blocks even though git is allowed."""
    write_user_config("""
        version: 1
        rules:
          - all: ['git']
            action: allow
          - all: ['rm', '-rf']
            action: deny
    """)
    out = run_main(hook_input("git status && rm -rf /"))
    assert_decision(out, "deny", reason_contains=["all=[rm, -rf]"])


def test_wrapped_command_unwrap_triggers_deny(write_user_config):
    """`sh -c 'rm -rf /'` blocks via inner unwrap."""
    write_user_config("""
        version: 1
        rules:
          - all: ['rm', '-rf']
            action: deny
    """)
    out = run_main(hook_input("sh -c 'rm -rf /'"))
    assert_decision(
        out, "deny", reason_contains=["all=[rm, -rf]", "from: sh -c rm -rf /"]
    )


def test_none_exclusion_end_to_end(write_user_config):
    """`none` carves an exception: a force push stops matching the allow rule
    and falls through to the default — with a reason naming the suppressed
    allow so the user understands why the ask appeared."""
    write_user_config("""
        version: 1
        default: ask
        rules:
          - all: ['git', 'push']
            none: ['--force', '-f']
            action: allow
    """)
    assert_decision(run_main(hook_input("git push origin main")), "allow")
    assert_decision(
        run_main(hook_input("git push --force")),
        "ask",
        reason_contains=["suppressed by none", "all=[git, push] none=[--force, -f]"],
    )


def test_regex_and_none_surface_in_reason(write_user_config):
    """A winning rule's regex (`re:`) and `none` clause must appear in the
    operator-facing reason, exercised through the full CLI path."""
    write_user_config("""
        version: 1
        default: allow
        rules:
          - all:
              - regex: '^rm$'
            none: ['--help']
            action: deny
    """)
    out = run_main(hook_input("rm -rf /"))
    assert_decision(out, "deny", reason_contains=["all=[re:^rm$]", "none=[--help]"])
    # the excluded token suppresses the rule, falling through to default allow
    assert_decision(run_main(hook_input("rm --help")), "allow")


def test_sudo_unwrap_triggers_deny(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['apt']
            action: deny
    """)
    out = run_main(hook_input("sudo -u alice apt install foo"))
    assert_decision(out, "deny", reason_contains="all=[apt]")


def test_multiple_denies_listed(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['rm', '-rf']
            action: deny
          - all: ['curl']
            action: deny
    """)
    out = run_main(hook_input("rm -rf /tmp/x && curl evil.example.com"))
    reason = out["hookSpecificOutput"]["permissionDecisionReason"]
    assert "matched 2 deny rules:" in reason
    assert "all=[rm, -rf]" in reason
    assert "all=[curl]" in reason


def test_parse_failure_treated_as_unanalyzable(write_user_config):
    """Malformed shell input falls back to default (ask) and surfaces unanalyzable."""
    write_user_config("""
        version: 1
        default: ask
        rules: []
    """)
    out = run_main(hook_input("echo )))"))
    assert_decision(out, "ask", reason_contains=["(unanalyzable)"])


def test_project_overrides_user(write_user_config, write_project_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['git']
            action: allow
    """)
    write_project_config("""
        version: 1
        rules:
          - all: ['git']
            action: deny
    """)
    out = run_main(hook_input("git status"))
    assert_decision(out, "deny", reason_contains="all=[git]")


# --- errors / fallbacks ----------------------------------------------------


def test_config_error_falls_back_to_ask_with_reason(write_user_config):
    write_user_config("""
        rules:
          - '^ls': allow
    """)  # missing version
    out = run_main(hook_input("ls"))
    assert_decision(out, "ask", reason_contains="version")


def test_missing_command_field_falls_back_to_ask(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
    """)
    bad = json.dumps({"tool_name": "Bash", "tool_input": {}})
    out = run_main(bad)
    assert_decision(out, "ask", reason_contains="command")


def test_non_string_command_falls_back_to_ask(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
    """)
    bad = json.dumps({"tool_name": "Bash", "tool_input": {"command": 42}})
    out = run_main(bad)
    assert_decision(out, "ask", reason_contains="command")


def test_invalid_stdin_json_falls_back_to_ask(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
    """)
    out = run_main("not valid json {")
    assert_decision(out, "ask", reason_contains=["JSON", "invalid"])


def test_non_object_payload_falls_back_to_ask(write_user_config):
    """A JSON scalar/array at the root is not a valid hook payload."""
    write_user_config("""
        version: 1
        rules: []
    """)
    out = run_main('"just a string"')
    assert_decision(out, "ask", reason_contains="JSON object")


def test_non_bash_tool_emits_no_output(write_user_config):
    """If the matcher is mis-wired and we see a non-Bash call, stay silent."""
    write_user_config("""
        version: 1
        rules:
          - all: [{regex: '.*'}]
            action: deny
    """)
    other = json.dumps({"tool_name": "Write", "tool_input": {"file_path": "x"}})
    assert run_main(other) is None


# --- README example pinned end-to-end -------------------------------------


_README_EXAMPLE_YAML = """
    version: 1
    default: ask
    rules:
      - all: [{regex: '(^|/)git$'}]
        none: ['push']
        action: allow
      - all: [{regex: '(^|/)git$'}, 'push']
        none: ['--force', '-f', {regex: '^--force='}]
        action: ask
      - all: [{regex: '(^|/)rm$'}, '-rf']
        action: deny
      - all: [{regex: '^(npm|pnpm|bun)$'}, 'test']
        action: allow
"""


def test_readme_example_git_status_allowed(write_user_config):
    """README claim: any git command (except push) is allowed."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(run_main(hook_input("git status")), "allow")


def test_readme_example_absolute_path_git_status_allowed(write_user_config):
    """README claim: ``(^|/)git$`` covers ``/usr/bin/git`` too."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(run_main(hook_input("/usr/bin/git status")), "allow")


def test_readme_example_git_push_normal_asks(write_user_config):
    """``git push origin main`` — rule 1's `none=[push]` suppresses the broad
    allow; rule 2's `all` matches and `none` doesn't fire → ask wins."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(
        run_main(hook_input("git push origin main")),
        "ask",
        reason_contains=["all=[re:(^|/)git$, push]"],
    )


def test_readme_example_git_force_push_falls_through_to_default_ask(write_user_config):
    """``git push --force`` — both rules' `none` clauses suppress; falls through
    to default ask with the suppressed-by-none reason naming both rules."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(
        run_main(hook_input("git push --force origin")),
        "ask",
        reason_contains=["suppressed by none", "--force"],
    )


def test_readme_example_git_force_equals_caught_by_regex(write_user_config):
    """``git push --force=true`` — the literal `--force` misses this token,
    but the `{regex: '^--force='}` element in `none` catches it."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(
        run_main(hook_input("git push --force=true")),
        "ask",
        reason_contains=["suppressed by none", "re:^--force="],
    )


def test_readme_example_absolute_path_rm_rf_denied(write_user_config):
    """README claim: ``(^|/)rm$, -rf`` blocks ``/usr/bin/rm -rf`` as well."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(
        run_main(hook_input("/usr/bin/rm -rf /tmp/x")),
        "deny",
        reason_contains=["re:(^|/)rm$"],
    )


def test_readme_example_pnpm_test_allowed(write_user_config):
    """`pnpm test` hits the npm/pnpm/bun regex + literal ``test``."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(run_main(hook_input("pnpm test")), "allow")


def test_readme_example_unrelated_command_falls_through(write_user_config):
    """A command no rule matches falls to default ask (silent — README claim)."""
    write_user_config(_README_EXAMPLE_YAML)
    assert_decision(run_main(hook_input("cat /etc/hosts")), "ask")


# --- subprocess smoke ------------------------------------------------------


def test_console_script_runs_via_subprocess(tmp_path, write_user_config):
    """End-to-end via the `ccpermissions-claude-code` console script installed by uv."""
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
    """)
    exe = Path(sys.executable).parent / "ccpermissions-claude-code"
    if not exe.exists():
        pytest.skip(
            f"console script not installed at {exe}; run `uv sync --group dev` first"
        )
    env = {
        **os.environ,
        "CLAUDE_CONFIG_DIR": str(tmp_path / "user-config"),
        "HOME": str(tmp_path / "fake-home"),
    }
    env.pop("CLAUDE_PROJECT_DIR", None)
    result = subprocess.run(
        [str(exe)],
        input=hook_input("ls -la"),
        text=True,
        capture_output=True,
        env=env,
        timeout=10,
    )
    assert result.returncode == 0, f"stderr: {result.stderr}"
    payload = json.loads(result.stdout)
    assert payload["hookSpecificOutput"]["permissionDecision"] == "allow"
