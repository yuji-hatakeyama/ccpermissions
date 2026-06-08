"""Tests for the per-command aggregator (`aggregate.aggregate`).

The aggregator runs `decide` over every `ExtractedCommand`, picks the
strongest action (``deny > ask > allow``), and composes a
``permissionDecisionReason`` from the contributors to the winning action only.
"""

from __future__ import annotations

from helpers import lit, rule

from ccpermissions.aggregate import AggregateResult, aggregate
from ccpermissions.parse import ExtractedCommand


def cmd(
    text: str,
    *,
    argv: tuple[str, ...] | None = None,
    origin: str | None = None,
    unanalyzable: bool = False,
) -> ExtractedCommand:
    """Compact `ExtractedCommand` constructor.

    By default ``argv`` is derived from ``text.split()`` for convenience;
    ``unanalyzable`` entries keep an empty argv (they never reach `decide`).
    """
    if argv is None:
        argv = () if unanalyzable else tuple(text.split())
    return ExtractedCommand(
        text=text, argv=argv, origin=origin, unanalyzable=unanalyzable
    )


# --- priority -------------------------------------------------------------


def test_empty_command_list_returns_default():
    assert aggregate([], [], "ask") == AggregateResult(action="ask", reason=None)


def test_allow_action_emits_no_reason():
    rules = [rule(lit("ls"), "allow")]
    result = aggregate([cmd("ls")], rules, "ask")
    assert result == AggregateResult(action="allow", reason=None)


def test_single_deny_match_reason():
    rules = [rule(lit("rm", "-rf"), "deny")]
    result = aggregate([cmd("rm -rf /")], rules, "ask")
    assert result.action == "deny"
    assert result.reason == "matched rule: all=[rm, -rf] -> deny"


def test_deny_beats_ask_and_allow():
    rules = [
        rule(lit("git"), "allow"),
        rule(lit("rm", "-rf"), "deny"),
        rule(lit("curl"), "ask"),
    ]
    cmds = [cmd("git status"), cmd("rm -rf /"), cmd("curl http://x")]
    assert aggregate(cmds, rules, "ask").action == "deny"


def test_ask_beats_allow():
    rules = [rule(lit("git"), "allow"), rule(lit("git", "push"), "ask")]
    cmds = [cmd("git status"), cmd("git push origin")]
    assert aggregate(cmds, rules, "allow").action == "ask"


# --- multi-match composition ---------------------------------------------


def test_two_deny_matches_listed_with_from_clause():
    rules = [rule(lit("rm", "-rf"), "deny"), rule(lit("curl"), "deny")]
    cmds = [cmd("rm -rf /"), cmd("curl evil.example.com")]
    result = aggregate(cmds, rules, "ask")
    assert result.action == "deny"
    assert result.reason is not None
    assert "matched 2 deny rules:" in result.reason
    assert "all=[rm, -rf]" in result.reason and "all=[curl]" in result.reason
    # (from: ...) distinguishes the two contributors
    assert "(from: rm -rf /)" in result.reason
    assert "(from: curl evil.example.com)" in result.reason


def test_three_deny_matches_all_listed():
    """Three contributors → the count label must read ``3 deny rules``."""
    rules = [rule(lit("a"), "deny"), rule(lit("b"), "deny"), rule(lit("c"), "deny")]
    cmds = [cmd("a"), cmd("b"), cmd("c")]
    result = aggregate(cmds, rules, "ask")
    assert result.reason is not None
    assert result.reason.startswith("matched 3 deny rules:")
    assert result.reason.count("  - ") == 3


def test_multi_match_distinguishes_top_level_from_wrapped():
    """One top-level and one wrapped contributor — both ``from:`` clauses appear."""
    rules = [rule(lit("rm", "-rf"), "deny")]
    cmds = [
        cmd("rm -rf /"),
        cmd("rm -rf /etc", origin="sh -c rm -rf /etc"),
    ]
    result = aggregate(cmds, rules, "ask")
    assert result.reason is not None
    assert "(from: rm -rf /)" in result.reason
    assert "(from: sh -c rm -rf /etc)" in result.reason


def test_winner_only_listed_not_losers():
    """When deny wins, ask/allow matches do not appear in the reason."""
    rules = [
        rule(lit("git"), "allow"),
        rule(lit("git", "push"), "ask"),
        rule(lit("rm", "-rf"), "deny"),
    ]
    cmds = [cmd("git status"), cmd("git push origin"), cmd("rm -rf /")]
    result = aggregate(cmds, rules, "ask")
    assert result.action == "deny"
    assert result.reason is not None
    assert "all=[rm, -rf]" in result.reason
    assert "git" not in result.reason


# --- (from: ...) annotation ----------------------------------------------


def test_origin_shown_when_command_came_from_wrapper():
    """A single nested match shows the outer wrapper via ``(from: ...)``."""
    rules = [rule(lit("rm", "-rf"), "deny")]
    result = aggregate([cmd("rm -rf /", origin="sh -c rm -rf /")], rules, "ask")
    assert result.reason == "matched rule: all=[rm, -rf] -> deny (from: sh -c rm -rf /)"


def test_top_level_single_match_omits_from_clause():
    """A single top-level match needs no ``(from: ...)`` — it would be redundant."""
    rules = [rule(lit("rm", "-rf"), "deny")]
    result = aggregate([cmd("rm -rf /")], rules, "ask")
    assert "(from:" not in (result.reason or "")


# --- unanalyzable contributions ------------------------------------------


def test_unanalyzable_contributes_via_default():
    """``unanalyzable + default=ask`` → ask wins, reason names ``(unanalyzable)``."""
    result = aggregate([cmd(")))broken", unanalyzable=True)], [], "ask")
    assert result.action == "ask"
    assert "(unanalyzable)" in (result.reason or "")
    assert "(from: )))broken)" in (result.reason or "")


def test_unanalyzable_does_not_override_deny():
    """A deny match still wins over ``unanalyzable + default=ask``."""
    rules = [rule(lit("rm"), "deny")]
    cmds = [cmd("rm /tmp/x"), cmd("???", unanalyzable=True)]
    result = aggregate(cmds, rules, "ask")
    assert result.action == "deny"
    assert "(unanalyzable)" not in (result.reason or "")


def test_unanalyzable_and_matched_rule_both_contribute():
    """With ``default=deny``, a deny match and an unanalyzable segment both
    contribute and are listed together — mixing a rule label and ``(unanalyzable)``."""
    rules = [rule(lit("rm"), "deny")]
    cmds = [cmd("rm /x"), cmd(")))", unanalyzable=True)]
    result = aggregate(cmds, rules, "deny")
    assert result.action == "deny"
    assert result.reason is not None
    assert "matched 2 deny rules:" in result.reason
    assert "all=[rm] (from: rm /x)" in result.reason
    assert "(unanalyzable) (from: )))" in result.reason


# --- default fallback -----------------------------------------------------


def test_no_match_no_unanalyzable_returns_default_with_no_reason():
    """Pure default fallback: no rule matched, no unanalyzable → no reason."""
    rules = [rule(lit("never"), "deny")]
    result = aggregate([cmd("foo"), cmd("bar")], rules, "ask")
    assert result == AggregateResult(action="ask", reason=None)


# --- suppressed-by-none surfaced to the user ----------------------------


def test_default_fallback_surfaces_single_suppressed_rule():
    """Without this, an `ask` triggered purely by a `none` exclusion gives the
    user no clue why — the rule was matched-but-suppressed and silently fell
    through to default."""
    rules = [rule(lit("git", "push"), "allow", none=lit("--force", "-f"))]
    result = aggregate([cmd("git push --force origin")], rules, "ask")
    assert result.action == "ask"
    assert result.reason is not None
    assert "suppressed by none" in result.reason
    assert "all=[git, push] none=[--force, -f]" in result.reason


def test_default_fallback_surfaces_multiple_suppressed_rules():
    """Two `none`-suppressed allows across one command — both listed."""
    rules = [
        rule(lit("git", "push"), "allow", none=lit("--force")),
        rule(lit("git"), "allow", none=lit("--force")),
    ]
    result = aggregate([cmd("git push --force")], rules, "ask")
    assert result.action == "ask"
    assert result.reason is not None
    assert "2 rules suppressed by none" in result.reason
    assert "all=[git, push] none=[--force]" in result.reason
    assert "all=[git] none=[--force]" in result.reason


def test_winning_rule_hides_suppressed_info():
    """A matching deny wins; suppressed allow is noise we don't show."""
    rules = [
        rule(lit("git"), "deny"),
        rule(lit("git", "push"), "allow", none=lit("--force")),
    ]
    result = aggregate([cmd("git push --force")], rules, "ask")
    assert result.action == "deny"
    assert "suppressed" not in (result.reason or "")
    # the deny still shows its own label
    assert "all=[git]" in (result.reason or "")


def test_unanalyzable_winner_hides_suppressed_info():
    """An unanalyzable contributor wins the default-fallback slot; don't double up
    on suppressed text in that case — the unanalyzable reason already exists."""
    rules = [rule(lit("git", "push"), "allow", none=lit("--force"))]
    cmds = [
        cmd("git push --force"),
        cmd(")))broken", unanalyzable=True),
    ]
    result = aggregate(cmds, rules, "ask")
    assert result.action == "ask"
    assert "(unanalyzable)" in (result.reason or "")
    assert "suppressed" not in (result.reason or "")


def test_suppressed_reason_includes_from_clause_for_wrapped():
    """When the suppressed rule came from a wrapped command position, the
    `(from: outer)` clause traces it back to the wrapper the user can see."""
    rules = [rule(lit("git", "push"), "allow", none=lit("--force"))]
    result = aggregate(
        [cmd("git push --force", origin="sh -c git push --force")],
        rules,
        "ask",
    )
    assert result.action == "ask"
    assert result.reason is not None
    assert "(from: sh -c git push --force)" in result.reason


def test_no_suppression_no_match_keeps_silent():
    """Pure default fallback with neither a match nor a suppression: no reason
    (this is the existing contract — must not regress)."""
    rules = [rule(lit("never"), "allow", none=lit("--never"))]
    result = aggregate([cmd("ls")], rules, "ask")
    assert result == AggregateResult(action="ask", reason=None)


# --- wrapper-parent contributor dedup -----------------------------------


def test_wrapped_parent_collapsed_when_child_matches_same_rule():
    """A wrapper like ``sudo rm`` and its unwrapped inner ``rm`` both fire
    the same ``all=[rm]`` rule; without dedup the reason reads
    ``matched 2 deny rules`` for what is one real match. Drop the parent."""
    rules = [rule(lit("rm"), "deny")]
    cmds = [
        cmd("sudo rm"),
        cmd("rm", origin="sudo rm"),
    ]
    result = aggregate(cmds, rules, "ask")
    assert result.action == "deny"
    assert result.reason == "matched rule: all=[rm] -> deny (from: sudo rm)"
    assert "matched 2" not in result.reason


def test_wrapped_parent_kept_when_rules_differ():
    """Parent and child match DIFFERENT rules (last-match-wins ensures their
    `matched_rule` is distinct) — both add unique info, keep both."""
    rules = [
        rule(lit("rm"), "deny"),
        rule(lit("sudo", "rm"), "deny"),
    ]
    cmds = [
        cmd("sudo rm"),
        cmd("rm", origin="sudo rm"),
    ]
    result = aggregate(cmds, rules, "ask")
    assert result.action == "deny"
    assert result.reason is not None
    # Parent's last match = all=[sudo, rm], child's only match = all=[rm].
    # Both contributors must be listed because their rule identities differ.
    assert "matched 2 deny rules:" in result.reason
    assert "all=[sudo, rm]" in result.reason
    assert "all=[rm]" in result.reason


def test_dedup_does_not_drop_unrelated_topelevel_matches():
    """Two top-level (no-origin) matches stay as two contributors — they're
    distinct command positions, not a wrapper/child pair."""
    rules = [rule(lit("rm"), "deny")]
    cmds = [cmd("rm /a"), cmd("rm /b")]
    result = aggregate(cmds, rules, "ask")
    assert result.action == "deny"
    assert result.reason is not None
    assert "matched 2 deny rules:" in result.reason


def test_dedup_keeps_suppressed_unchanged():
    """A suppressed allow on the parent must not be dropped just because a
    child also exists — they record different facts."""
    rules = [rule(lit("sudo", "rm"), "allow", none=lit("--no"))]
    cmds = [
        cmd("sudo rm --no"),
        cmd("rm", origin="sudo rm"),
    ]
    # Parent's allow is suppressed; child has no rule.
    # Action: default fallback (ask). Suppressed surfaces from parent entry.
    result = aggregate(cmds, rules, "ask")
    assert result.action == "ask"
    assert result.reason is not None
    assert "suppressed by none" in result.reason


# --- default-deny silent fallback gets a reason -------------------------


def test_default_deny_no_match_surfaces_reason():
    """Pure default-deny with no rule matched — Claude needs the explanation
    or it will retry blindly. The user-facing dialog (or context) gets a clear
    'no rule matched; default is deny'."""
    rules = [rule(lit("ls"), "allow")]
    result = aggregate([cmd("cat /etc/hosts")], rules, "deny")
    assert result.action == "deny"
    assert result.reason is not None
    assert "no rule matched" in result.reason
    assert "default" in result.reason


def test_default_ask_no_match_stays_silent():
    """Same situation under default ask — silence is fine because the user is
    being asked anyway. Don't regress the existing behavior."""
    rules = [rule(lit("ls"), "allow")]
    result = aggregate([cmd("cat /etc/hosts")], rules, "ask")
    assert result == AggregateResult(action="ask", reason=None)


def test_default_deny_with_suppressed_prefers_suppressed_reason():
    """Suppressed is more informative than 'no rule matched'. Keep it.
    They never both surface."""
    rules = [rule(lit("git", "push"), "allow", none=lit("--force"))]
    result = aggregate([cmd("git push --force")], rules, "deny")
    assert result.action == "deny"
    assert result.reason is not None
    assert "suppressed by none" in result.reason
    assert "no rule matched" not in result.reason


def test_default_deny_with_matched_deny_uses_matched_reason():
    """A rule explicitly denied → reason is the rule label, not the fallback note."""
    rules = [rule(lit("rm"), "deny")]
    result = aggregate([cmd("rm /tmp/x")], rules, "deny")
    assert result.action == "deny"
    assert "no rule matched" not in (result.reason or "")
    assert "all=[rm]" in (result.reason or "")


def test_default_deny_pure_with_no_rules_at_all():
    """Empty ruleset with default deny — fall back to the 'no rule matched' note."""
    result = aggregate([cmd("cat /etc/hosts")], [], "deny")
    assert result.action == "deny"
    assert result.reason is not None
    assert "no rule matched" in result.reason
