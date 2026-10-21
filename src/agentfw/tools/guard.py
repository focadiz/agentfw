"""Permission checks applied before any tool touches files, runs a command or uses the network."""
from __future__ import annotations

import os
from pathlib import Path

from ..permissions import normalize
from ..resolver import Resolved


class PermissionDenied(Exception):
    """Raised when a call is outside the role's effective permissions. Shown to the agent as 'Denied: ...'."""


class ToolInputError(Exception):
    """Raised for bad arguments. Shown to the agent as 'Error: ...' so it can correct the call."""


class Guard:
    def __init__(self, resolved: Resolved):
        self.resolved = resolved
        self.permissions = resolved.permissions
        # Relative paths are taken relative to where the role works.
        self.base_dir = Path(resolved.command_cwd or resolved.project_root)

    def path(self, raw: str, *, write: bool = False) -> Path:
        """Resolve an agent-supplied path and check it. Returns the real (symlink-free) path.

        Both the path as written and its real target must be allowed, so a
        symlink inside an allowed directory can't reach a file outside it.
        """
        if not raw:
            raise ToolInputError("path is empty")
        p = Path(raw).expanduser()
        if not p.is_absolute():
            p = self.base_dir / p
        literal = normalize(p)
        real = os.path.realpath(literal)
        action = "write" if write else "read"
        for candidate in dict.fromkeys((literal, real)):
            reason = self.permissions.why_denied(candidate, write=write)
            if reason:
                via = f" (symlink to {real})" if candidate == real != literal else ""
                raise PermissionDenied(f"{action} access to {literal}{via} isn't allowed for role {self.resolved.role}: {reason}")
        return Path(real)

    def command(self, executable: str) -> None:
        if not self.permissions.can_run(executable):
            allowed = ", ".join(sorted(self.permissions.commands)) or "none"
            raise PermissionDenied(f"command {Path(executable).name!r} isn't allowed for role {self.resolved.role} (allowed: {allowed})")

    def host(self, host: str) -> None:
        if not self.permissions.can_reach(host):
            raise PermissionDenied(f"network access to {host!r} isn't allowed for role {self.resolved.role}")
