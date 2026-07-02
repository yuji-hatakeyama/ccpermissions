"""Shell-text parsing layer.

`enumerate_commands(text)` walks the bashlex AST and yields one
`ExtractedCommand` per command position that would execute. Common wrappers
(``sh -c``, ``sudo``, ``env``, ``find -exec``, ``eval``) are re-parsed so that
rules fire on the inner command as well as the wrapper.

Failures of ``bashlex.parse`` — malformed shell, ``case`` statements (which
bashlex flags via ``NotImplementedError``), and matching errors — surface as a
single `ExtractedCommand` with ``unanalyzable=True``; the orchestrator treats
those as a ``default`` vote so the user is never silently allowed past
unparseable input.
"""

from __future__ import annotations

import shlex
from collections.abc import Iterable, Sequence
from dataclasses import dataclass
from typing import Any, Final

import bashlex
import bashlex.errors

_TRUNCATE: Final[int] = 80

# Exceptions that mean "we cannot statically analyze this input."
# `ParsingError` covers tokenizer-side matched-pair errors via subclassing.
# `NotImplementedError` is what bashlex raises for `case ... esac`.
_UNPARSEABLE: Final[tuple[type, ...]] = (
    bashlex.errors.ParsingError,
    NotImplementedError,
)

# bashlex's AST nodes are loosely-typed Python objects without published
# stubs. Aliasing to `Any` documents intent at the function-signature level
# without pretending we have real type safety on the parser internals.
_BashNode = Any


@dataclass(frozen=True)
class ExtractedCommand:
    """One command position extracted from a shell input.

    Attributes:
        text: The joined argv string. The display value in
            `permissionDecisionReason` (and, for ``unanalyzable`` entries, the
            only available form of the segment).
        argv: The command's token list — the match target for `decide()`.
            Empty for ``unanalyzable`` entries, which are never run through
            `decide()`.
        origin: Outer wrapper text when the command was extracted from inside
            something the user cannot immediately see (a wrapper such as
            ``sh -c``, a command substitution). ``None`` for top-level
            positions.
        unanalyzable: ``True`` iff this entry was synthesized because the
            corresponding shell text could not be parsed. The orchestrator
            should skip `decide()` and treat it as a ``default`` vote.
    """

    text: str
    argv: tuple[str, ...] = ()
    origin: str | None = None
    unanalyzable: bool = False


def enumerate_commands(text: str) -> list[ExtractedCommand]:
    """Walk the shell input and return one entry per executable command position.

    Args:
        text: Raw shell text from ``tool_input.command``.

    Returns:
        Commands in execution order. Empty / whitespace-only input returns an
        empty list. A parse failure of the whole input returns a single
        ``unanalyzable=True`` entry.
    """
    if not text.strip():
        return []
    try:
        trees = bashlex.parse(text)
    except _UNPARSEABLE:
        return [ExtractedCommand(text=_truncate(text), unanalyzable=True)]
    out: list[ExtractedCommand] = []
    for tree in trees:
        _walk(tree, origin=None, out=out)
    return out


# --- AST walking ----------------------------------------------------------


def _walk(node: _BashNode, *, origin: str | None, out: list[ExtractedCommand]) -> None:
    """Recurse over an AST node, emitting one `ExtractedCommand` per command."""
    if getattr(node, "kind", None) == "command":
        _emit_command(node, origin=origin, out=out)
        return
    for child in _children(node):
        _walk(child, origin=origin, out=out)


def _children(node: _BashNode) -> Iterable[_BashNode]:
    """Yield every child AST node bashlex hangs off this one.

    bashlex uses a small set of attribute names to hold children
    (``parts``, ``list``, and ``command`` for substitutions). Iterating
    them all is enough to descend through compound / list / pipeline /
    control-flow nodes without enumerating each kind.
    """
    for attr in ("parts", "list", "command"):
        child = getattr(node, attr, None)
        if child is None:
            continue
        if isinstance(child, list):
            yield from child
        else:
            yield child


def _emit_command(
    cmd_node: _BashNode, *, origin: str | None, out: list[ExtractedCommand]
) -> None:
    """Emit one entry for ``cmd_node``, plus any embedded / wrapped commands."""
    argv: list[str] = _argv_words(cmd_node)
    if not argv:
        return
    text: str = " ".join(argv)
    out.append(ExtractedCommand(text=text, argv=tuple(argv), origin=origin))
    _emit_substitutions(cmd_node, parent_text=text, out=out)
    inner: str | None = _try_unwrap(argv)
    if inner is not None:
        _walk_string(inner, origin=text, out=out)
    if argv[0] in _SHELLS:
        heredoc: str | None = _heredoc_body(cmd_node)
        if heredoc is not None:
            _walk_string(heredoc, origin=text, out=out)


def _emit_substitutions(
    cmd_node: _BashNode, *, parent_text: str, out: list[ExtractedCommand]
) -> None:
    """Walk ``$(...)`` and ``<(...)`` inside the command's word parts.

    Substitutions are nested commands the shell will actually execute; their
    inner AST is exposed by bashlex as ``.command`` on the substitution node.
    """
    for part in cmd_node.parts:
        for sub in _children(part):
            kind: str | None = getattr(sub, "kind", None)
            if kind in ("commandsubstitution", "processsubstitution"):
                _walk(sub.command, origin=parent_text, out=out)


def _argv_words(cmd_node: _BashNode) -> list[str]:
    """Return the word values of a command, ignoring assignments / redirects."""
    return [p.word for p in cmd_node.parts if getattr(p, "kind", None) == "word"]


def _walk_string(s: str, *, origin: str, out: list[ExtractedCommand]) -> None:
    """Re-parse a string (the body of a wrapper) and walk its AST."""
    if not s.strip():
        return
    try:
        trees = bashlex.parse(s)
    except _UNPARSEABLE:
        out.append(ExtractedCommand(text=_truncate(s), origin=origin, unanalyzable=True))
        return
    for t in trees:
        _walk(t, origin=origin, out=out)


# --- Wrapper unwrap table -------------------------------------------------
#
# Each unwrapper takes the wrapper's argv and returns the inner shell text
# (already shell-quoted via shlex.join so bashlex can re-parse it without
# losing word boundaries), or ``None`` when there is nothing to unwrap.
# Kept intentionally minimal; unknown wrappers stay opaque.


_SHELLS: Final[frozenset[str]] = frozenset({"sh", "bash", "zsh", "dash"})


def _try_unwrap(argv: Sequence[str]) -> str | None:
    """Dispatch on ``argv[0]`` to the matching unwrapper, or ``None``."""
    head: str = argv[0]
    if head in _SHELLS:
        return _unwrap_dash_c(argv)
    if head == "sudo":
        return _unwrap_sudo(argv)
    if head == "env":
        return _unwrap_env(argv)
    if head == "find":
        return _unwrap_find_exec(argv)
    if head == "eval":
        return _unwrap_eval(argv)
    return None


def _unwrap_dash_c(argv: Sequence[str]) -> str | None:
    """``<shell> -c '<inner>'`` → return ``<inner>`` (already a single word)."""
    for i in range(1, len(argv) - 1):
        if argv[i] == "-c":
            return argv[i + 1]
    return None


_SUDO_VALUE_FLAGS: Final[frozenset[str]] = frozenset(
    {"-u", "-g", "-p", "-D", "-h", "-r", "-t", "-T", "-C", "-A"}
)


def _unwrap_sudo(argv: Sequence[str]) -> str | None:
    """Drop sudo's own flags and ``VAR=val`` preamble; return the rest."""
    rest: list[str] = _strip_flags_and_assignments(argv[1:], _SUDO_VALUE_FLAGS)
    return shlex.join(rest) if rest else None


_ENV_VALUE_FLAGS: Final[frozenset[str]] = frozenset({"-u", "-C", "-S"})


def _unwrap_env(argv: Sequence[str]) -> str | None:
    """``env [VAR=val ...] [flags] cmd args`` → drop env's preamble."""
    rest: list[str] = _strip_flags_and_assignments(argv[1:], _ENV_VALUE_FLAGS)
    return shlex.join(rest) if rest else None


def _unwrap_find_exec(argv: Sequence[str]) -> str | None:
    """``find ... -exec cmd args \\;`` or ``... -exec cmd args +``."""
    try:
        start: int = argv.index("-exec")
    except ValueError:
        return None
    end: int = len(argv)
    for j in range(start + 1, len(argv)):
        if argv[j] in (";", "+"):
            end = j
            break
    inner: list[str] = list(argv[start + 1 : end])
    return shlex.join(inner) if inner else None


def _unwrap_eval(argv: Sequence[str]) -> str | None:
    """``eval <args...>`` runs the joined args as a shell command per POSIX.

    Treating eval as a wrapper is symmetric with ``sh -c``: both feed a string
    to the shell to execute. ``ssh``/``docker exec``/``kubectl exec`` look
    similar but have wrapper-specific argv layouts that vary by version, so
    they stay opaque on purpose.

    Args are joined with a plain space (NOT shlex.join) because POSIX eval
    concatenates its arguments and re-scans the result; re-quoting via
    shlex.join would collapse ``eval 'rm -rf /'`` to a single token under
    bashlex's re-parse and the per-token matchers would miss everything.
    """
    if len(argv) < 2:
        return None
    return " ".join(argv[1:])


def _heredoc_body(cmd_node: _BashNode) -> str | None:
    """Return the heredoc body attached to a shell command, or ``None``.

    Only called for commands whose head is one of `_SHELLS`. A ``bash <<EOF``
    redirect carries its body in ``redirect.heredoc.value`` (terminator
    included); we strip the trailing ``\\n<terminator>`` so the body re-parses
    as a standalone script.
    """
    for part in getattr(cmd_node, "parts", ()):
        if getattr(part, "kind", None) != "redirect":
            continue
        if getattr(part, "type", None) not in ("<<", "<<-"):
            continue
        heredoc = getattr(part, "heredoc", None)
        if heredoc is None:
            continue
        body: str = heredoc.value
        terminator: str | None = getattr(getattr(part, "output", None), "word", None)
        if terminator is not None:
            suffix: str = "\n" + terminator
            if body.endswith(suffix):
                body = body[: -len(suffix)]
            elif body == terminator:
                body = ""
        return body
    return None


def _strip_flags_and_assignments(
    rest: Sequence[str], value_flags: frozenset[str]
) -> list[str]:
    """Drop leading ``-flag`` tokens and ``VAR=val`` assignments.

    Consumes an extra argument for known value-bearing flags (e.g. ``-u alice``).
    Stops at ``--`` (consumed) or at the first non-flag, non-assignment token.

    Args:
        rest: Argv tail starting just after the wrapper command name.
        value_flags: Flags that take a separate value as the next token.

    Returns:
        The remaining argv as a list (may be empty).
    """
    out: list[str] = list(rest)
    while out:
        tok: str = out[0]
        if tok == "--":
            return out[1:]
        if tok in value_flags and len(out) > 1:
            out = out[2:]
            continue
        if tok.startswith("-") and tok != "-":
            out = out[1:]
            continue
        if "=" in tok and not tok.startswith("="):
            out = out[1:]
            continue
        break
    return out


# --- helpers --------------------------------------------------------------


def _truncate(s: str, limit: int = _TRUNCATE) -> str:
    """Cap ``s`` at ``limit`` characters with an ellipsis marker."""
    return s if len(s) <= limit else s[: limit - 1] + "…"
