"""${namespace.name} variable expansion for commands, globs, knowledge locations and prompts."""
from __future__ import annotations

import re
from collections.abc import Mapping
from typing import Any

VAR = re.compile(r"\$\{([a-z_]+)\.([a-z_]+)\}")
NAMESPACES = frozenset({"project", "platform", "pack", "task", "role", "args"})
# ${args.*} is filled in per tool call, after the agent supplies arguments.
DEFERRED = frozenset({"args"})
MAX_DEPTH = 10

Context = Mapping[str, Mapping[str, str]]


class VariableError(ValueError):
    pass


def expand(text: str, ctx: Context, *, defer: frozenset[str] = DEFERRED) -> str:
    """Expand known-namespace variables, repeatedly, so values may reference other variables.

    Text that only looks like a variable (e.g. Mule's ${secure::key} or ${env})
    is left alone because it doesn't match <namespace>.<name>.
    """
    def substitute(match: re.Match) -> str:
        ns, name = match.groups()
        if ns not in NAMESPACES or ns in defer:
            return match.group(0)
        values = ctx.get(ns)
        if values is None:
            raise VariableError(f"${{{ns}.{name}}}: no {ns!r} values in this context")
        if name not in values:
            raise VariableError(f"${{{ns}.{name}}} is not defined")
        return str(values[name])

    for _ in range(MAX_DEPTH):
        expanded = VAR.sub(substitute, text)
        if expanded == text:
            return expanded
        text = expanded
    raise VariableError(f"expansion didn't settle after {MAX_DEPTH} passes (circular reference?): {text}")


def expand_all(value: Any, ctx: Context, *, defer: frozenset[str] = DEFERRED) -> Any:
    """expand() applied to every string inside nested dicts and lists."""
    if isinstance(value, str):
        return expand(value, ctx, defer=defer)
    if isinstance(value, Mapping):
        return {k: expand_all(v, ctx, defer=defer) for k, v in value.items()}
    if isinstance(value, list):
        return [expand_all(v, ctx, defer=defer) for v in value]
    return value
