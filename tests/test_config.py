"""Tests for `config.load_merged()` — path resolution, merging, validation."""

from __future__ import annotations

import textwrap

from ccpermissions.config import load_merged
from ccpermissions.decide import CompiledRule


def labels(rules: tuple[CompiledRule, ...]) -> list[str]:
    """The single-line label of each compiled rule, for compact assertions."""
    return [r.label for r in rules]


# --- presence / merging ---------------------------------------------------


def test_both_absent_returns_empty_ask(tmp_path, monkeypatch):
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(tmp_path / "no-such-dir"))
    result = load_merged()
    assert result.rules == ()
    assert result.default == "ask"
    assert result.error is None


def test_user_only(write_user_config):
    write_user_config("""
        version: 1
        default: allow
        rules:
          - all: ['git']
            action: allow
          - all: ['rm', '-rf']
            action: deny
    """)
    result = load_merged()
    assert result.error is None
    assert result.default == "allow"
    assert labels(result.rules) == ["all=[git]", "all=[rm, -rf]"]
    assert [r.action for r in result.rules] == ["allow", "deny"]


def test_project_only(write_project_config):
    write_project_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
    """)
    result = load_merged()
    assert result.error is None
    assert result.default == "ask"
    assert labels(result.rules) == ["all=[ls]"]


def test_both_user_first_then_project(write_user_config, write_project_config):
    write_user_config("""
        version: 1
        default: deny
        rules:
          - all: ['git']
            action: allow
    """)
    write_project_config("""
        version: 1
        default: ask
        rules:
          - all: ['git', 'push']
            action: deny
    """)
    result = load_merged()
    assert result.error is None
    assert labels(result.rules) == ["all=[git]", "all=[git, push]"]
    assert result.default == "ask"


def test_user_default_used_when_project_default_absent(
    write_user_config, write_project_config
):
    write_user_config("""
        version: 1
        default: deny
        rules: []
    """)
    write_project_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
    """)
    result = load_merged()
    assert result.error is None
    assert result.default == "deny"


def test_home_fallback_when_claude_config_dir_unset(tmp_path):
    """Without CLAUDE_CONFIG_DIR, the user config is read from $HOME/.claude."""
    home = tmp_path / "fake-home"
    (home / ".claude").mkdir(parents=True, exist_ok=True)
    (home / ".claude" / "ccpermissions.yaml").write_text(
        textwrap.dedent("""
        version: 1
        rules:
          - all: ['echo']
            action: allow
    """)
    )
    result = load_merged()
    assert result.error is None
    assert labels(result.rules) == ["all=[echo]"]


# --- rule shapes that succeed ---------------------------------------------


def test_none_and_regex_elements_compile(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['git', 'push']
            none: ['--force', '-f']
            action: allow
          - all:
              - regex: '^(npm|pnpm|bun)$'
              - 'test'
            action: allow
    """)
    result = load_merged()
    assert result.error is None
    assert labels(result.rules) == [
        "all=[git, push] none=[--force, -f]",
        "all=[re:^(npm|pnpm|bun)$, test]",
    ]


def test_rules_key_missing_loads_with_no_rules(write_user_config):
    """`rules:` is optional; a file with only `version: 1` loads cleanly."""
    write_user_config("version: 1\n")
    result = load_merged()
    assert result.error is None
    assert result.rules == ()
    assert result.default == "ask"


# --- version validation (independent of the rule schema) ------------------


def test_missing_version_is_error(write_user_config):
    write_user_config("""
        rules:
          - all: ['ls']
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "version" in result.error
    assert result.default == "ask"
    assert result.rules == ()


def test_unknown_version_is_error(write_user_config):
    write_user_config("""
        version: 99
        rules: []
    """)
    result = load_merged()
    assert result.error is not None
    assert "99" in result.error or "version" in result.error
    assert result.default == "ask"


def test_quoted_version_is_error(write_user_config):
    """`version: "1"` is a string, not the integer the schema requires."""
    write_user_config("""
        version: "1"
        rules: []
    """)
    result = load_merged()
    assert result.error is not None
    assert "str" in result.error


def test_bool_version_is_error(write_user_config):
    """`version: true` parses as Python's `True`, which equals 1; reject it explicitly."""
    write_user_config("""
        version: true
        rules: []
    """)
    result = load_merged()
    assert result.error is not None
    assert "bool" in result.error


def test_float_version_is_error(write_user_config):
    """`version: 1.0` equals 1 by `==` in Python; require strict int."""
    write_user_config("""
        version: 1.0
        rules: []
    """)
    result = load_merged()
    assert result.error is not None
    assert "float" in result.error


# --- file-level errors ----------------------------------------------------


def test_invalid_yaml_is_error(write_user_config):
    write_user_config("version: 1\nrules: [unclosed\n")
    result = load_merged()
    assert result.error is not None
    assert result.default == "ask"


def test_non_utf8_file_is_error(tmp_path, monkeypatch):
    """A non-UTF-8 config file must not crash the hook."""
    d = tmp_path / "user-config"
    d.mkdir()
    (d / "ccpermissions.yaml").write_bytes(b"\xff\xfe\x00garbage")
    monkeypatch.setenv("CLAUDE_CONFIG_DIR", str(d))
    result = load_merged()
    assert result.error is not None
    assert "UTF-8" in result.error


def test_empty_yaml_file_is_error(write_user_config):
    write_user_config("")
    result = load_merged()
    assert result.error is not None


def test_errors_from_both_files_combined(write_user_config, write_project_config):
    write_user_config("""
        rules: []
    """)  # missing version
    write_project_config("""
        version: 99
        rules: []
    """)  # wrong version
    result = load_merged()
    assert result.error is not None
    assert "user-config" in result.error
    assert "project" in result.error


# --- rule-schema validation -----------------------------------------------


def test_rule_entry_that_is_not_a_mapping_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - just-a-string
    """)
    result = load_merged()
    assert result.error is not None
    assert "rules[0]" in result.error
    assert "mapping" in result.error


def test_rule_missing_all_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "rules[0]" in result.error
    assert "all" in result.error


def test_rule_missing_action_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
    """)
    result = load_merged()
    assert result.error is not None
    assert "action" in result.error


def test_empty_all_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: []
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "all" in result.error
    assert "empty" in result.error


def test_unknown_rule_key_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
            action: allow
            extra: 1
    """)
    result = load_merged()
    assert result.error is not None
    assert "unknown" in result.error
    assert "extra" in result.error


def test_all_not_a_list_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: 'git'
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "all" in result.error
    assert "list" in result.error


def test_invalid_action_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: ['ls']
            action: permit
    """)
    result = load_merged()
    assert result.error is not None
    assert "permit" in result.error or "action" in result.error


def test_invalid_default_is_error(write_user_config):
    write_user_config("""
        version: 1
        default: maybe
        rules: []
    """)
    result = load_merged()
    assert result.error is not None
    assert "maybe" in result.error or "default" in result.error


# --- matcher-element validation -------------------------------------------


def test_non_string_non_mapping_element_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all: [123]
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "rules[0].all[0]" in result.error
    assert "string" in result.error


def test_regex_mapping_wrong_key_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all:
              - regexp: '^x'
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "regex" in result.error


def test_regex_value_not_string_is_error(write_user_config):
    write_user_config("""
        version: 1
        rules:
          - all:
              - regex: 123
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "regex" in result.error
    assert "string" in result.error


def test_invalid_regex_is_error(write_user_config):
    write_user_config(r"""
        version: 1
        rules:
          - all:
              - regex: '[unclosed'
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "compile" in result.error
    assert result.default == "ask"
    assert result.rules == ()


def test_duplicate_regex_key_is_error(write_user_config):
    """A duplicated `regex:` key would silently drop one pattern in a security
    gate; the loader rejects it instead of keeping the last value."""
    write_user_config("""
        version: 1
        rules:
          - all:
              - regex: '^rm$'
                regex: '^ls$'
            action: deny
    """)
    result = load_merged()
    assert result.error is not None
    assert "duplicate" in result.error
    assert result.rules == ()


def test_duplicate_rule_key_is_error(write_user_config):
    """Duplicate top-level rule keys (e.g. two `action:`) are rejected too."""
    write_user_config("""
        version: 1
        rules:
          - all: ['rm']
            action: deny
            action: allow
    """)
    result = load_merged()
    assert result.error is not None
    assert "duplicate" in result.error


def test_complex_yaml_key_surfaces_clean_parse_error(write_user_config):
    """A complex (unhashable) mapping key must surface as a clean parse error,
    not a bare TypeError escaping the loader's contract."""
    write_user_config("version: 1\n? [a, b]\n: 1\n")
    result = load_merged()
    assert result.error is not None
    assert "YAML parse error" in result.error
    assert "unexpected load error" not in result.error
    assert result.rules == ()
