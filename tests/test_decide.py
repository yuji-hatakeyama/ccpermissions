"""Unit tests for the pure `decide` function and rule/token matching."""

from __future__ import annotations

from helpers import lit, rule, rx

from ccpermissions.decide import Decision, Matcher, decide

# --- baseline -------------------------------------------------------------


def test_empty_rules_returns_default():
    assert decide(["git", "status"], [], "ask") == Decision(
        action="ask", matched_rule=None
    )
    assert decide(["anything"], [], "allow") == Decision(
        action="allow", matched_rule=None
    )


def test_no_match_returns_default():
    rules = [rule(lit("git"), "allow")]
    assert decide(["ls"], rules, "ask") == Decision(action="ask", matched_rule=None)


def test_single_match_returns_action_and_rule():
    rules = [rule(lit("git"), "allow")]
    d = decide(["git", "status"], rules, "ask")
    assert d.action == "allow"
    assert d.matched_rule is rules[0]


def test_default_fallback_has_no_matched_rule():
    rules = [rule(lit("never-matches"), "allow")]
    d = decide(["something", "else"], rules, "deny")
    assert d.action == "deny"
    assert d.matched_rule is None


# --- `all`: order-independent existence (AND) -----------------------------


def test_all_tokens_must_all_be_present_order_independent():
    r = [rule(lit("git", "push"), "allow")]
    assert decide(["git", "push"], r, "ask").action == "allow"
    # extra tokens between them still hit (`git -c /path/dir push`)
    assert decide(["git", "-c", "/path/dir", "push"], r, "ask").action == "allow"
    # order does not matter
    assert decide(["push", "git"], r, "ask").action == "allow"
    # one token missing → no match
    assert decide(["git", "status"], r, "ask").action == "ask"


def test_echo_git_push_also_hits():
    """The existence check is intentionally broad: `echo git push` argv hits too."""
    r = [rule(lit("git", "push"), "allow")]
    assert decide(["echo", "git", "push"], r, "ask").action == "allow"


def test_literal_matches_whole_token_not_substring():
    """A literal matches a whole token, never a substring (`push` != `pushup`)."""
    r = [rule(lit("push"), "deny")]
    assert decide(["git", "pushup"], r, "allow").action == "allow"
    assert decide(["git", "push"], r, "allow").action == "deny"


# --- `none`: OR negation --------------------------------------------------


def test_none_excludes_when_any_matcher_hits():
    r = [rule(lit("git", "push"), "allow", none=lit("--force", "-f"))]
    assert decide(["git", "push", "origin"], r, "ask").action == "allow"
    assert decide(["git", "push", "--force"], r, "ask").action == "ask"
    assert decide(["git", "push", "-f"], r, "ask").action == "ask"


def test_force_equals_is_a_distinct_token_not_excluded():
    """`--force=true` is one token; the literal `--force` does not match it."""
    r = [rule(lit("git", "push"), "allow", none=lit("--force"))]
    assert decide(["git", "push", "--force=true"], r, "ask").action == "allow"


def test_regex_in_none_catches_the_equals_form():
    """A regex `none` element catches `--force=true` that a literal cannot."""
    r = [rule(lit("git", "push"), "allow", none=[rx(r"^--force")])]
    assert decide(["git", "push", "--force=true"], r, "ask").action == "ask"
    assert decide(["git", "push", "origin"], r, "ask").action == "allow"


def test_none_flips_the_winning_rule():
    """`none` must suppress a would-be last match so an earlier rule wins."""
    rules = [
        rule(lit("git"), "deny"),
        rule(lit("git", "push"), "allow", none=lit("--force")),
    ]
    # no --force: the allow rule is the last match and wins
    assert decide(["git", "push", "origin"], rules, "ask").action == "allow"
    # --force present: allow rule is suppressed, the earlier deny wins
    d = decide(["git", "push", "--force"], rules, "ask")
    assert d.action == "deny"
    assert d.matched_rule is rules[0]


# --- regex elements -------------------------------------------------------


def test_regex_element_searched_per_token():
    """A regex element is searched against each token; `test` is a literal."""
    r = [rule([rx(r"^(npm|pnpm|bun)$"), Matcher(raw="test")], "allow")]
    assert decide(["npm", "test"], r, "ask").action == "allow"
    assert decide(["pnpm", "test"], r, "ask").action == "allow"
    assert decide(["bun", "test"], r, "ask").action == "allow"
    # `npm run test` carries a literal `test` token → hits
    assert decide(["npm", "run", "test"], r, "ask").action == "allow"
    # `npm run test:watch` has no exact `test` token → misses
    assert decide(["npm", "run", "test:watch"], r, "ask").action == "ask"


def test_regex_anchoring_is_the_authors_responsibility():
    unanchored = [rule([rx(r"(npm|pnpm|bun)")], "deny")]
    anchored = [rule([rx(r"^(npm|pnpm|bun)$")], "deny")]
    # a token merely *containing* npm
    assert decide(["npm-check"], unanchored, "allow").action == "deny"
    assert decide(["npm-check"], anchored, "allow").action == "allow"


def test_regex_element_position_does_not_matter():
    """`all` is order-independent regardless of where the regex element sits."""
    r = [rule([Matcher(raw="sudo"), rx(r"^rm$"), Matcher(raw="-rf")], "deny")]
    assert decide(["sudo", "rm", "-rf", "/"], r, "allow").action == "deny"
    assert decide(["rm", "-rf", "sudo"], r, "allow").action == "deny"
    assert decide(["sudo", "-rf"], r, "allow").action == "allow"  # missing rm


def test_empty_argv_matches_nothing():
    """An empty argv (defensive) satisfies no non-empty `all`, so default wins."""
    rules = [rule(lit("git"), "deny"), rule([rx(r".*")], "deny")]
    assert decide([], rules, "ask") == Decision(action="ask", matched_rule=None)


# --- precedence and labels ------------------------------------------------


def test_last_match_wins():
    """Project rules go after user rules, so the last match expresses precedence."""
    rules = [
        rule(lit("git"), "allow"),
        rule(lit("git", "push"), "ask"),
    ]
    assert decide(["git", "status"], rules, "ask").action == "allow"
    d = decide(["git", "push", "origin"], rules, "ask")
    assert d.action == "ask"
    assert d.matched_rule is rules[1]


def test_rule_label_renders_regex_and_none():
    """The rule's own label shows literals plainly, regexes with `re:`, and `none`."""
    r = rule([rx(r"^rm$"), Matcher(raw="-rf")], "deny", none=lit("--dry-run"))
    assert r.label == "all=[re:^rm$, -rf] none=[--dry-run]"


# --- suppressed-by-none tracking ----------------------------------------


def test_suppressed_rule_recorded_when_none_excludes_it():
    """A rule whose `all` matched but was suppressed by `none` lands in `suppressed`.

    The user-facing pain is that an `ask`/`deny` from default-fallback gives no
    hint that an `allow` exception almost fired; `Decision.suppressed` is the
    data the aggregator uses to surface that.
    """
    r = rule(lit("git", "push"), "allow", none=lit("--force"))
    d = decide(["git", "push", "--force"], [r], "ask")
    assert d.action == "ask"
    assert d.matched_rule is None
    assert d.suppressed == (r,)


def test_suppressed_empty_when_no_rule_matches_all():
    """A rule whose `all` did not even match doesn't count as suppressed."""
    r = rule(lit("git", "push"), "allow", none=lit("--force"))
    d = decide(["ls"], [r], "ask")
    assert d.action == "ask"
    assert d.matched_rule is None
    assert d.suppressed == ()


def test_suppressed_recorded_alongside_winning_rule():
    """A suppressed allow plus a matching deny: deny wins, but suppressed is preserved
    so the aggregator can choose whether to surface it."""
    rules = [
        rule(lit("git"), "deny"),
        rule(lit("git", "push"), "allow", none=lit("--force")),
    ]
    d = decide(["git", "push", "--force"], rules, "ask")
    assert d.action == "deny"
    assert d.matched_rule is rules[0]
    assert d.suppressed == (rules[1],)


def test_multiple_suppressed_rules_all_recorded():
    """If several rules would have matched and were all suppressed, all are recorded."""
    rules = [
        rule(lit("git", "push"), "allow", none=lit("--force")),
        rule(lit("git"), "allow", none=lit("--force")),
    ]
    d = decide(["git", "push", "--force"], rules, "ask")
    assert d.action == "ask"
    assert d.matched_rule is None
    assert d.suppressed == (rules[0], rules[1])


# --- README-canonical regex patterns -------------------------------------
#
# These tests pin the matching semantics of the regex forms the README
# presents as canonical. If the README's recommended patterns ever silently
# drift in behaviour, these tests should be what catches it.


def test_path_anchored_regex_matches_basename_and_absolute_path():
    """``(^|/)git$`` is the README's recommended way to match a command by name
    regardless of whether it was invoked by basename or absolute path."""
    r = [rule([rx(r"(^|/)git$")], "deny")]
    assert decide(["git", "status"], r, "allow").action == "deny"
    assert decide(["/usr/bin/git", "status"], r, "allow").action == "deny"
    assert decide(["/opt/homebrew/bin/git", "status"], r, "allow").action == "deny"


def test_path_anchored_regex_does_not_match_substring_neighbours():
    """The ``(^|/)`` left-anchor and ``$`` right-anchor must keep the pattern
    from leaking onto tokens that merely contain ``git`` as a substring."""
    r = [rule([rx(r"(^|/)git$")], "deny")]
    for argv in (
        ["gitignore"],
        ["mygit"],
        ["git-lfs"],
        ["gitlab"],
        ["/usr/bin/gitlab"],
        ["/usr/local/bin/git-lfs"],
    ):
        assert decide(argv, r, "allow").action == "allow", argv


def test_force_equals_regex_catches_equals_form_not_other_force_flags():
    """``^--force=`` matches ``--force=true`` (the literal `--force` misses)
    but stays out of the way of plain ``--force`` / ``--force-with-lease``."""
    r = [rule(lit("git", "push"), "allow", none=[rx(r"^--force=")])]
    # equals form: rule suppressed → default
    assert decide(["git", "push", "--force=true"], r, "ask").action == "ask"
    # plain --force: ^--force= does NOT match, allow fires
    assert decide(["git", "push", "--force"], r, "ask").action == "allow"
    # --force-with-lease: also not matched by ^--force=, allow fires
    assert decide(["git", "push", "--force-with-lease"], r, "ask").action == "allow"
