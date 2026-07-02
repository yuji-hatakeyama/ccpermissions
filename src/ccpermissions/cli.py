"""CLI entry point — the PreToolUse hook body.

Reads PreToolUse JSON on stdin, parses the Bash command into individual
positions (`parse.enumerate_commands`), runs `decide()` over each, picks
the strongest action via `aggregate.aggregate`, and writes the resulting
`hookSpecificOutput` JSON to stdout. Any failure along the way becomes an
`ask` decision whose `permissionDecisionReason` carries a human-readable
explanation.
"""

from __future__ import annotations

import json
import sys
from typing import IO, Any

from .aggregate import AggregateResult, aggregate
from .config import LoadResult, load_merged
from .parse import ExtractedCommand, enumerate_commands


class _HookInputError(Exception):
    """Raised when the hook input JSON is missing or malformed.

    The literal prefixes of the messages (e.g. ``hook input: invalid JSON:``)
    are surfaced verbatim to the user via ``permissionDecisionReason`` and
    are effectively part of the user-facing contract — change carefully.
    The interpolated suffixes (parser detail, type names) are diagnostic and
    may change freely.
    """


def _extract_command(payload: Any) -> str:
    """Pull `tool_input.command` out of the PreToolUse JSON payload.

    Args:
        payload: Decoded JSON object from stdin (already verified to be a dict).

    Returns:
        The command string.

    Raises:
        _HookInputError: When the payload shape is wrong or ``command`` is
            missing / not a string.
    """
    tool_input: Any = payload.get("tool_input")
    if not isinstance(tool_input, dict):
        raise _HookInputError("hook input: 'tool_input' missing or not an object")
    command: Any = tool_input.get("command")
    if command is None:
        raise _HookInputError("hook input: 'tool_input.command' is missing")
    if not isinstance(command, str):
        raise _HookInputError(
            "hook input: 'tool_input.command' must be a string, "
            f"got {type(command).__name__}"
        )
    return command


def _format_output(action: str, reason: str | None) -> str:
    """Build the PreToolUse `hookSpecificOutput` JSON string.

    Emits ``permissionDecisionReason`` only when `reason` is not ``None``;
    which outcomes carry a reason is the caller's policy.

    Returns:
        A single-line JSON string with a trailing newline, suitable for
        writing to stdout.
    """
    hso: dict[str, Any] = {
        "hookEventName": "PreToolUse",
        "permissionDecision": action,
    }
    if reason is not None:
        hso["permissionDecisionReason"] = reason
    return json.dumps({"hookSpecificOutput": hso}) + "\n"


def _ask(error: str) -> str:
    """Build an `ask + reason` output for an error path.

    Errors fall back to `ask`, where the reason appears in the user's
    confirmation dialog.
    """
    return _format_output("ask", error)


def _run(stdin: IO[str], stdout: IO[str]) -> None:
    """Main hook body — split out so `main()` can wrap it in a backstop."""
    try:
        payload: Any = json.loads(stdin.read())
    except json.JSONDecodeError as e:
        stdout.write(_ask(f"hook input: invalid JSON: {e}"))
        return

    if not isinstance(payload, dict):
        stdout.write(_ask("hook input is not a JSON object"))
        return

    # The hook is expected to be wired with `"matcher": "Bash"`; if the
    # matcher is missing or mis-configured and we see a non-Bash tool here,
    # stay silent so Claude Code's default permission flow runs instead of
    # ccpermissions opining on tools it doesn't understand.
    if payload.get("tool_name") != "Bash":
        return

    try:
        command: str = _extract_command(payload)
    except _HookInputError as e:
        stdout.write(_ask(str(e)))
        return

    cfg: LoadResult = load_merged()
    if cfg.error is not None:
        # The error LoadResult is pinned to rules=(), default="ask", so the
        # outcome is already determined — skip the parse/aggregate pass.
        stdout.write(_ask(cfg.error))
        return
    commands: list[ExtractedCommand] = enumerate_commands(command)
    result: AggregateResult = aggregate(commands, cfg.rules, cfg.default)
    stdout.write(_format_output(result.action, result.reason))


def main(
    stdin: IO[str] | None = None,
    stdout: IO[str] | None = None,
) -> int:
    """Run the PreToolUse hook body.

    Reads PreToolUse JSON from `stdin`, evaluates it, and writes the
    `hookSpecificOutput` JSON to `stdout`. Returns 0 in every documented
    flow; an outer backstop catches unexpected exceptions and surfaces
    them via `ask + permissionDecisionReason` so the user is never
    silently locked out.

    Args:
        stdin: Stream to read the hook input from. Defaults to `sys.stdin`.
        stdout: Stream to write the hook output to. Defaults to `sys.stdout`.

    Returns:
        Always `0`.
    """
    if stdin is None:
        stdin = sys.stdin
    if stdout is None:
        stdout = sys.stdout

    try:
        _run(stdin, stdout)
    except Exception as e:  # noqa: BLE001 — contract: never crash the hook
        stdout.write(_ask(f"ccpermissions internal error: {e}"))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
