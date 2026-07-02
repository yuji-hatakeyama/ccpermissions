"""Aggregate per-command decisions into a single PreToolUse outcome.

The shell-text parsing layer (`parse.enumerate_commands`) returns one
`ExtractedCommand` per command position. `aggregate` runs `decide` over each,
picks the strongest action — ``deny > ask > allow`` — and composes a
``permissionDecisionReason`` from the entries that contributed to the winner.
"""

from __future__ import annotations

from collections.abc import Sequence
from dataclasses import dataclass
from typing import Final

from .decide import Action, CompiledRule, Decision, decide
from .parse import ExtractedCommand

_PRIORITY: Final[dict[Action, int]] = {"deny": 3, "ask": 2, "allow": 1}


@dataclass(frozen=True)
class AggregateResult:
    """The combined decision plus the optional reason text.

    The CLI error paths also construct this shape directly (``ask`` with the
    error string as `reason`) so the output formatter has a single input type.

    Attributes:
        action: The winning action across all command positions.
        reason: Composed reason text, or ``None`` when the winner is ``allow``
            or every contributor to the winning action was a pure
            default-fallback (no matched rule, not unanalyzable).
    """

    action: Action
    reason: str | None


def aggregate(
    commands: Sequence[ExtractedCommand],
    rules: Sequence[CompiledRule],
    default: Action,
) -> AggregateResult:
    """Pick the strongest action across all command positions.

    Args:
        commands: Output of `enumerate_commands`. Empty input falls back to
            ``default`` with no reason.
        rules: Compiled rules in evaluation order.
        default: Action used when no rule matches a command, and as the action
            for ``unanalyzable`` entries.

    Returns:
        An `AggregateResult`. ``reason`` is populated when at least one
        contributor to the winning action either matched a rule or was an
        unanalyzable segment; otherwise it is ``None`` so pure default-fallback
        wins emit no reason.
    """
    if not commands:
        return AggregateResult(action=default, reason=None)

    entries: list[_Entry] = [_decide_one(c, rules, default) for c in commands]
    entries = _dedupe_wrapper_parents(entries)
    winner_action: Action = max(
        entries, key=lambda e: _PRIORITY[e.decision.action]
    ).decision.action
    if winner_action == "allow":
        return AggregateResult(action=winner_action, reason=None)

    contributors: list[_Entry] = [
        e
        for e in entries
        if e.decision.action == winner_action
        and (e.decision.matched_rule is not None or e.extracted.unanalyzable)
    ]

    if contributors:
        return AggregateResult(
            action=winner_action,
            reason=_compose_reason(contributors, winner_action),
        )
    # No contributor for the winning action: the action came purely from the
    # default fallback. If any rule was matched-but-suppressed by `none`,
    # surface it so the user understands why an allow exception did not fire.
    suppressed = _collect_suppressed(entries, winner_action)
    if suppressed:
        return AggregateResult(
            action=winner_action,
            reason=_compose_suppressed_reason(suppressed, winner_action),
        )
    # Pure-default `deny` is otherwise silent and Claude has to guess why a
    # tool was blocked. Spell it out so the next attempt is informed.
    if winner_action == "deny":
        return AggregateResult(action="deny", reason="no rule matched; default is deny")
    return AggregateResult(action=winner_action, reason=None)


# --- internals ------------------------------------------------------------


@dataclass(frozen=True)
class _Entry:
    """A per-command result tied back to the source that produced it."""

    extracted: ExtractedCommand
    decision: Decision


def _decide_one(
    extracted: ExtractedCommand,
    rules: Sequence[CompiledRule],
    default: Action,
) -> _Entry:
    """Run `decide` for one command, or synthesize a default vote when unanalyzable."""
    if extracted.unanalyzable:
        return _Entry(extracted, Decision(action=default, matched_rule=None))
    return _Entry(extracted, decide(extracted.argv, rules, default))


def _compose_reason(entries: list[_Entry], action: Action) -> str:
    """Build the reason text from the contributors to the winning action.

    Args:
        entries: Contributing entries, in source order. Must be non-empty.
        action: The winning action (``deny`` or ``ask``).

    Returns:
        A single-line string for one contributor, or a multi-line list when
        two or more contributors share the winning action.
    """
    if len(entries) == 1:
        e: _Entry = entries[0]
        return (
            f"matched rule: {_rule_label(e)} -> {action}{_from_clause(e, always=False)}"
        )
    lines: list[str] = [f"matched {len(entries)} {action} rules:"]
    for e in entries:
        lines.append(f"  - {_rule_label(e)}{_from_clause(e, always=True)}")
    return "\n".join(lines)


def _rule_label(e: _Entry) -> str:
    """``(unanalyzable)`` for unanalyzable entries, the rule's label otherwise."""
    if e.extracted.unanalyzable:
        return "(unanalyzable)"
    return e.decision.matched_rule.label if e.decision.matched_rule else ""


def _dedupe_wrapper_parents(entries: list[_Entry]) -> list[_Entry]:
    """Drop wrapper-parent entries fully covered by an unwrapped child match.

    When ``sudo rm`` and the unwrapped ``rm`` (origin=``sudo rm``) both fire
    the same rule, the parent's contribution is redundant — same rule, same
    action, just a coarser argv position. Without this step the reason reads
    ``matched 2 deny rules`` for what is one real match.

    Parents matching a *different* rule than the child stay (their rule is
    unique information); entries without an `origin` (top-level positions)
    are never dropped.
    """
    child_origins_by_rule: dict[int, set[str]] = {}
    for e in entries:
        rule = e.decision.matched_rule
        if rule is None or e.extracted.origin is None:
            continue
        child_origins_by_rule.setdefault(id(rule), set()).add(e.extracted.origin)

    kept: list[_Entry] = []
    for e in entries:
        rule = e.decision.matched_rule
        if rule is not None and e.extracted.text in child_origins_by_rule.get(
            id(rule), set()
        ):
            continue
        kept.append(e)
    return kept


def _collect_suppressed(entries: list[_Entry], winner_action: Action) -> list[_Entry]:
    """Pick the entries whose `none`-suppression is worth reporting.

    Only entries that fell through to the default (and therefore have no
    matched rule) carry suppressed rules the user might want to know about —
    a contributor entry already has its own reason and adding suppressed text
    would be noise.
    """
    return [
        e
        for e in entries
        if not e.extracted.unanalyzable
        and e.decision.matched_rule is None
        and e.decision.action == winner_action
        and e.decision.suppressed
    ]


def _compose_suppressed_reason(entries: list[_Entry], action: Action) -> str:
    """Render the `(rule, source)` pairs the user would have expected to fire."""
    items: list[tuple[CompiledRule, _Entry]] = [
        (r, e) for e in entries for r in e.decision.suppressed
    ]
    if len(items) == 1:
        rule, e = items[0]
        return (
            f"default {action}; suppressed by none: "
            f"{rule.label}{_from_clause(e, always=False)}"
        )
    lines: list[str] = [f"default {action}; {len(items)} rules suppressed by none:"]
    for rule, e in items:
        lines.append(f"  - {rule.label}{_from_clause(e, always=True)}")
    return "\n".join(lines)


def _from_clause(e: _Entry, *, always: bool) -> str:
    """Render the ``(from: <source>)`` suffix tracing a contributor to its source.

    The source is the outer wrapper (`origin`) when the command came from one,
    else the command text itself. In a single-contributor reason it is omitted
    for plain top-level matches (`always=False`) since it would just repeat the
    command; in a multi-contributor list it is always shown (`always=True`) so
    each line maps back to a distinct sub-command.
    """
    if not always and e.extracted.origin is None and not e.extracted.unanalyzable:
        return ""
    src: str = e.extracted.origin if e.extracted.origin is not None else e.extracted.text
    return f" (from: {src})"
