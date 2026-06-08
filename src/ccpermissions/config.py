"""YAML configuration loading, merging, and validation.

User and project YAML files are loaded, validated, and merged into a single
`LoadResult` consumed by the CLI orchestrator.
"""

from __future__ import annotations

import os
import re
from dataclasses import dataclass
from pathlib import Path
from typing import Final, cast

from .decide import ACTIONS, Action, CompiledRule, Matcher


class _ConfigError(Exception):
    """Raised when a single config file fails schema or regex validation.

    Internal; `load_merged` catches this and surfaces the message via
    `LoadResult.error`. Not part of the public API.
    """


@dataclass(frozen=True)
class _ParsedFile:
    """One config file after successful parsing."""

    rules: tuple[CompiledRule, ...] = ()
    default: Action | None = None


@dataclass(frozen=True)
class LoadResult:
    """Merged configuration handed to the CLI orchestrator.

    Two valid shapes:

    - **Success**: `rules` populated (possibly empty), `default` set,
      `error=None`.
    - **Failure**: `rules=()`, `default="ask"`, `error` non-`None`. The
      orchestrator emits an `ask` decision with `error` as the reason, so
      the user always sees what went wrong without being silently locked
      out.

    Attributes:
        rules: Compiled rules in evaluation order (user rules first, then
            project rules).
        default: The merged default action.
        error: Human-readable description of any load failure, or `None`.
    """

    rules: tuple[CompiledRule, ...] = ()
    default: Action = "ask"
    error: str | None = None

    def __post_init__(self) -> None:
        if self.error is not None and (self.rules or self.default != "ask"):
            raise ValueError("LoadResult with error must have rules=() and default='ask'")


def user_config_path() -> Path:
    """Resolve the user-scoped config path.

    Honors `$CLAUDE_CONFIG_DIR` if set, otherwise falls back to
    `~/.claude/ccpermissions.yaml`.

    Returns:
        Absolute path to the user config file. The file is not required to
        exist.
    """
    config_dir = os.environ.get("CLAUDE_CONFIG_DIR")
    if config_dir:
        return Path(config_dir) / "ccpermissions.yaml"
    return Path.home() / ".claude" / "ccpermissions.yaml"


def project_config_path() -> Path | None:
    """Resolve the project-scoped config path.

    Returns:
        `${CLAUDE_PROJECT_DIR}/.claude/ccpermissions.yaml` when the
        environment variable is set, otherwise `None`.
    """
    project_dir = os.environ.get("CLAUDE_PROJECT_DIR")
    if not project_dir:
        return None
    return Path(project_dir) / ".claude" / "ccpermissions.yaml"


def _expected_actions() -> str:
    """Human-readable list of valid actions for error messages."""
    return ", ".join(ACTIONS)


# Lazily built and cached: importing PyYAML and registering the constructor
# is deferred so a fresh install with no config file never pays for it.
_unique_key_loader: type | None = None


def _build_unique_key_loader():
    """A `yaml.SafeLoader` that rejects duplicate mapping keys.

    PyYAML silently keeps the *last* value for a duplicated key. In a security
    gate that means a typo like two ``regex:`` keys (or two ``action:`` keys)
    would silently drop a rule element and change which commands are gated.
    Rejecting surfaces the mistake instead of degrading it.
    """
    global _unique_key_loader
    if _unique_key_loader is not None:
        return _unique_key_loader

    import yaml

    class _UniqueKeyLoader(yaml.SafeLoader):
        pass

    def _construct_mapping(loader, node, deep=False):
        seen: set = set()
        for key_node, _ in node.value:
            key = loader.construct_object(key_node, deep=deep)
            try:
                duplicate = key in seen
            except TypeError:
                # Unhashable (complex) key, e.g. `? [a, b]`. Defer to the stock
                # constructor, which raises a clean ConstructorError (a
                # YAMLError) instead of letting a bare TypeError escape
                # `_parse`'s documented error contract.
                break
            if duplicate:
                raise yaml.constructor.ConstructorError(
                    "while constructing a mapping",
                    node.start_mark,
                    f"found duplicate key {key!r}",
                    key_node.start_mark,
                )
            seen.add(key)
        return yaml.SafeLoader.construct_mapping(loader, node, deep=deep)

    _UniqueKeyLoader.add_constructor(
        yaml.resolver.BaseResolver.DEFAULT_MAPPING_TAG, _construct_mapping
    )
    _unique_key_loader = _UniqueKeyLoader
    return _unique_key_loader


def _parse(text: str, source: Path) -> _ParsedFile:
    """Validate and parse the YAML text of one config file.

    Args:
        text: Raw file contents.
        source: Path of the file (used only to enrich error messages).

    Returns:
        The parsed rules and (optional) default action.

    Raises:
        _ConfigError: When the YAML is malformed, the schema is violated, or
            any regex fails to compile.
    """
    # Deferred import: PyYAML's import cost is non-trivial, and a fresh
    # install with no config file should not pay it on every PreToolUse.
    import yaml

    try:
        data = yaml.load(text, Loader=_build_unique_key_loader())
    except yaml.YAMLError as e:
        raise _ConfigError(f"{source}: YAML parse error: {e}") from e

    if data is None:
        raise _ConfigError(f"{source}: file is empty; 'version: 1' is required")
    if not isinstance(data, dict):
        raise _ConfigError(f"{source}: top-level must be a mapping")

    if "version" not in data:
        raise _ConfigError(
            f"{source}: missing required key 'version' (expected: version: 1)"
        )
    version = data["version"]
    # `bool` is a subclass of `int`, so `True == 1` is True; `float(1.0)` also
    # compares equal to 1. Require strict, non-bool `int`.
    if not isinstance(version, int) or isinstance(version, bool) or version != 1:
        raise _ConfigError(
            f"{source}: unsupported version {version!r} "
            f"(expected integer 1, got {type(version).__name__})"
        )

    default: Action | None = None
    if "default" in data:
        d = data["default"]
        if d not in ACTIONS:
            raise _ConfigError(
                f"{source}: invalid default {d!r}; expected one of: {_expected_actions()}"
            )
        default = cast(Action, d)

    raw_rules = data.get("rules", [])
    if not isinstance(raw_rules, list):
        raise _ConfigError(f"{source}: 'rules' must be a list")

    return _ParsedFile(
        rules=tuple(_parse_rule(entry, source, i) for i, entry in enumerate(raw_rules)),
        default=default,
    )


_ALLOWED_RULE_KEYS: Final[frozenset[str]] = frozenset({"all", "none", "action"})


def _parse_rule(entry: object, source: Path, i: int) -> CompiledRule:
    """Validate one ``rules[i]`` mapping into a `CompiledRule`.

    Raises:
        _ConfigError: When the entry is not a mapping, carries unknown or
            missing keys, has an invalid action, or any matcher fails to
            validate.
    """
    if not isinstance(entry, dict):
        raise _ConfigError(
            f"{source}: rules[{i}] must be a mapping with keys 'all' and "
            f"'action' (optional 'none'), got {type(entry).__name__}"
        )
    unknown: list[str] = sorted(set(entry) - _ALLOWED_RULE_KEYS)
    if unknown:
        raise _ConfigError(
            f"{source}: rules[{i}] has unknown key(s) {unknown}; "
            f"allowed keys: all, none, action"
        )
    if "all" not in entry:
        raise _ConfigError(f"{source}: rules[{i}] missing required key 'all'")
    if "action" not in entry:
        raise _ConfigError(f"{source}: rules[{i}] missing required key 'action'")

    act: object = entry["action"]
    if act not in ACTIONS:
        raise _ConfigError(
            f"{source}: rules[{i}] action {act!r} must be one of: {_expected_actions()}"
        )

    return CompiledRule(
        all=_compile_matchers(entry["all"], source, i, "all", allow_empty=False),
        none=_compile_matchers(
            entry.get("none", []), source, i, "none", allow_empty=True
        ),
        action=cast(Action, act),
    )


def _compile_matchers(
    value: object, source: Path, i: int, field: str, *, allow_empty: bool
) -> tuple[Matcher, ...]:
    """Compile a rule's ``all`` / ``none`` list into a tuple of `Matcher`."""
    if not isinstance(value, list):
        raise _ConfigError(
            f"{source}: rules[{i}].{field} must be a list, got {type(value).__name__}"
        )
    if not value and not allow_empty:
        raise _ConfigError(f"{source}: rules[{i}].{field} must not be empty")
    return tuple(_compile_matcher(el, source, i, field, j) for j, el in enumerate(value))


def _compile_matcher(el: object, source: Path, i: int, field: str, j: int) -> Matcher:
    """Compile one ``all`` / ``none`` element into a `Matcher`.

    A plain string is a literal token matcher; a ``{regex: '<pattern>'}``
    mapping is a regex matcher. Anything else (including ``true`` / a bare
    number, which YAML would otherwise coerce) is rejected so a config typo
    never silently degrades into a token that can't match.
    """
    if isinstance(el, str):
        return Matcher(raw=el)
    if isinstance(el, dict):
        if set(el) != {"regex"}:
            raise _ConfigError(
                f"{source}: rules[{i}].{field}[{j}] mapping must have exactly "
                f"the key 'regex', got {sorted(el)}"
            )
        pat: object = el["regex"]
        if not isinstance(pat, str):
            raise _ConfigError(
                f"{source}: rules[{i}].{field}[{j}] regex must be a string, "
                f"got {type(pat).__name__}"
            )
        try:
            compiled = re.compile(pat)
        except re.error as e:
            # `re.error` may produce multi-line messages; the permission
            # dialog is single-line so keep only the first.
            raise _ConfigError(
                f"{source}: rules[{i}].{field}[{j}] regex {pat!r} failed to "
                f"compile: {str(e).splitlines()[0]}"
            ) from e
        return Matcher(raw=pat, pattern=compiled)
    raise _ConfigError(
        f"{source}: rules[{i}].{field}[{j}] must be a string or {{regex: '...'}}, "
        f"got {type(el).__name__}"
    )


def _load_one(path: Path) -> _ParsedFile | None:
    """Read and parse a single config file.

    Args:
        path: Config file path. Missing files are not an error.

    Returns:
        The parsed file, or `None` when the file does not exist.

    Raises:
        _ConfigError: On read failure, decoding failure, or any validation
            error inside `_parse`.
    """
    try:
        text = path.read_text(encoding="utf-8")
    except FileNotFoundError:
        return None
    except UnicodeDecodeError as e:
        raise _ConfigError(f"{path}: file is not valid UTF-8: {e}") from e
    except OSError as e:
        raise _ConfigError(f"{path}: read error: {e}") from e
    return _parse(text, path)


@dataclass(frozen=True)
class _SafeLoad:
    """Result of one `_load_one` call: either a parsed file or an error message.

    Both attributes are `None` when no file existed at the resolved path —
    that case means "contribute nothing to the merge" rather than failure.
    """

    parsed: _ParsedFile | None = None
    error: str | None = None


def _safe_load(path: Path | None) -> _SafeLoad:
    """Wrap `_load_one` so callers don't need a try/except per file.

    Catches the documented `_ConfigError`, plus any unexpected exception, so
    a pathological YAML input (recursive anchors, etc.) can never crash the
    hook silently — the message is always surfaced via `LoadResult.error`.
    """
    if path is None:
        return _SafeLoad()
    try:
        return _SafeLoad(parsed=_load_one(path))
    except _ConfigError as e:
        return _SafeLoad(error=str(e))
    except Exception as e:  # noqa: BLE001 — contract: never crash the hook
        return _SafeLoad(error=f"{path}: unexpected load error: {e}")


def _resolve_default(user: _ParsedFile | None, project: _ParsedFile | None) -> Action:
    """Pick the default action: project, then user, then `"ask"`."""
    if project is not None and project.default is not None:
        return project.default
    if user is not None and user.default is not None:
        return user.default
    return "ask"


def load_merged() -> LoadResult:
    """Load user + project configs and merge them.

    Both files are optional. Errors from either file are concatenated and
    returned via `LoadResult.error`; when any error occurs, `rules` is empty
    and `default` is `"ask"`, so the caller can fall back uniformly without
    branching on the error path.

    Returns:
        The merged `LoadResult`. Project rules are placed after user rules
        in evaluation order so that they take precedence under the
        last-match-wins semantics of `decide()`.
    """
    user = _safe_load(user_config_path())
    project = _safe_load(project_config_path())

    errors = [e for e in (user.error, project.error) if e]
    if errors:
        return LoadResult(error=" | ".join(errors))

    user_rules = user.parsed.rules if user.parsed is not None else ()
    project_rules = project.parsed.rules if project.parsed is not None else ()
    return LoadResult(
        rules=user_rules + project_rules,
        default=_resolve_default(user.parsed, project.parsed),
    )
