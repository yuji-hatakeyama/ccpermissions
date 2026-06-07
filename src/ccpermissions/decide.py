"""Pure decision logic for ccpermissions.

Permission resolution is a pure function: no I/O, no global state. A command
is reduced to its argv (token list) by the parsing layer; a rule fires when
**every** ``all`` matcher hits some token (AND, order-independent) and **no**
``none`` matcher hits any token (OR negation).
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from typing import Final, Iterable, Literal, Optional, Sequence, get_args

Action = Literal["allow", "ask", "deny"]
ACTIONS: Final[tuple[Action, ...]] = get_args(Action)


@dataclass(frozen=True)
class Matcher:
    """One token matcher: literal equality, or a regex searched per token.

    A literal matches a single argv token by exact string equality, so it can
    never spill across token boundaries (``push`` never matches ``pushup``).
    A regex is matched with ``pattern.search`` against each token individually;
    anchoring is the author's responsibility (write ``^npm$`` for an exact
    token, ``npm`` to match any token containing it).

    Attributes:
        raw: The original string from YAML — the literal token for string
            entries, or the pattern source for ``{regex: ...}`` entries.
            Preserved for echoing back in ``permissionDecisionReason``.
        pattern: Compiled regex for ``{regex: ...}`` entries; ``None`` for
            literal entries.
    """

    raw: str
    pattern: Optional[re.Pattern[str]] = None

    def matches(self, token: str) -> bool:
        """True when `token` satisfies this matcher (exact eq, or regex search)."""
        if self.pattern is None:
            return token == self.raw
        return self.pattern.search(token) is not None

    @property
    def label(self) -> str:
        """Display form: the literal token, or ``re:<pattern>`` for regexes."""
        return self.raw if self.pattern is None else f"re:{self.raw}"


@dataclass(frozen=True)
class CompiledRule:
    """A single rule from the merged config, ready for evaluation.

    Attributes:
        all: Matchers that must *all* be satisfied (each by some token).
            Non-empty by construction (`config` rejects an empty ``all``).
        none: Matchers of which *none* may be satisfied. May be empty.
        action: One of ``"allow"``, ``"ask"``, ``"deny"``.
    """

    all: tuple[Matcher, ...]
    none: tuple[Matcher, ...]
    action: Action

    @property
    def label(self) -> str:
        """Single-line ``all=[...] none=[...]`` form used in reason text."""
        text: str = "all=[" + ", ".join(m.label for m in self.all) + "]"
        if self.none:
            text += " none=[" + ", ".join(m.label for m in self.none) + "]"
        return text

    def matches(self, argv: Sequence[str]) -> bool:
        """True when this rule fires for `argv` (every ``all``, no ``none``)."""
        if not all(any(m.matches(tok) for tok in argv) for m in self.all):
            return False
        if any(m.matches(tok) for m in self.none for tok in argv):
            return False
        return True


@dataclass(frozen=True)
class Decision:
    """The outcome of evaluating a command against the rules.

    Attributes:
        action: The selected `Action`.
        matched_rule: The `CompiledRule` that won, or ``None`` when no rule
            matched and `action` came from the default. The presentation layer
            renders it (via `CompiledRule.label`) only when a reason is needed,
            so decision-making never pays for label formatting.
        suppressed: Rules whose ``all`` matched but were excluded by ``none``.
            Kept so the aggregator can tell the user "this allow exception
            *almost* fired"; otherwise a `none`-driven fall-through to the
            default action would be indistinguishable from "no rule matched
            at all".
    """

    action: Action
    matched_rule: Optional[CompiledRule]
    suppressed: tuple[CompiledRule, ...] = ()


def decide(
    argv: Sequence[str], rules: Iterable[CompiledRule], default: Action
) -> Decision:
    """Pick a permission action for `argv` using last-match-wins semantics.

    Every rule is tested in order. The last rule that matches determines the
    result. If no rule matches, `default` is used and `matched_rule` is
    ``None``. Rules whose ``all`` matched but were excluded by ``none`` land in
    `Decision.suppressed` so the aggregator can surface why an `ask`/`deny`
    default-fallback fired despite an allow exception being "almost" satisfied.

    Args:
        argv: The command's token list for one execution position, as produced
            by the parsing layer.
        rules: Rules in evaluation order. For the standard project/user merge
            this is ``user.rules + project.rules`` so that project rules —
            evaluated last — override user rules on conflict.
        default: Action returned when no rule matches.

    Returns:
        The `Decision` describing the chosen action, the matching rule (if
        any), and any `none`-suppressed rules encountered along the way.
    """
    last_match: Optional[CompiledRule] = None
    suppressed: list[CompiledRule] = []
    for rule in rules:
        if not _all_satisfied(rule, argv):
            continue
        if _none_excluded(rule, argv):
            suppressed.append(rule)
            continue
        last_match = rule
    if last_match is None:
        return Decision(
            action=default, matched_rule=None, suppressed=tuple(suppressed)
        )
    return Decision(
        action=last_match.action,
        matched_rule=last_match,
        suppressed=tuple(suppressed),
    )


def _all_satisfied(rule: CompiledRule, argv: Sequence[str]) -> bool:
    return all(any(m.matches(tok) for tok in argv) for m in rule.all)


def _none_excluded(rule: CompiledRule, argv: Sequence[str]) -> bool:
    return any(m.matches(tok) for m in rule.none for tok in argv)
