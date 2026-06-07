"""Shared constructors for building rules/matchers in tests.

Kept out of ``conftest.py`` because these are plain helpers imported by name,
not pytest fixtures.
"""

from __future__ import annotations

import re

from ccpermissions.decide import Action, CompiledRule, Matcher


def lit(*tokens: str) -> tuple[Matcher, ...]:
    """Literal matchers for each token (exact argv-token equality)."""
    return tuple(Matcher(raw=t) for t in tokens)


def rx(pattern: str) -> Matcher:
    """A regex matcher compiled from `pattern`."""
    return Matcher(raw=pattern, pattern=re.compile(pattern))


def rule(all_, action: Action, none=()) -> CompiledRule:
    """Compact `CompiledRule` constructor used across the test suite."""
    return CompiledRule(all=tuple(all_), none=tuple(none), action=action)
