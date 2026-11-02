"""Effective permissions: a role's ceiling narrowed by skill pack requests.

Rules:
- fs_read / fs_write: a path must match the role's globs AND each narrowing
  pack's globs (kept as layers, since glob sets can't be intersected directly).
- fs_deny: union of every layer; a deny match always wins.
- commands_allow: set intersection.
- network: the lower of the two levels; allowlists intersected by pattern.
- git: role only.
A field a pack leaves out doesn't narrow anything.
"""
from __future__ import annotations

import os
from collections.abc import Mapping
from dataclasses import dataclass, field, replace
from fnmatch import fnmatchcase
from pathlib import Path
from types import MappingProxyType
from typing import Any

LEVELS = {"none": 0, "allowlist": 1, "any": 2}


@dataclass(frozen=True)
class Permissions:
    fs_read: tuple[tuple[str, ...], ...]
    fs_write: tuple[tuple[str, ...], ...]
    fs_deny: tuple[str, ...]
    commands: frozenset[str]
    network: str
    network_allowlist: tuple[str, ...]
    git: Mapping[str, Any] = field(default_factory=lambda: MappingProxyType({}))

    @classmethod
    def from_role(cls, p: Mapping[str, Any]) -> Permissions:
        network = p.get("network", "none")
        return cls(
            fs_read=(_with_real_variants(p.get("fs_read", ())),),
            fs_write=(_with_real_variants(p.get("fs_write", ())),),
            fs_deny=tuple(p.get("fs_deny", ())),
            commands=frozenset(p.get("commands_allow", ())),
            network=network,
            network_allowlist=tuple(p.get("network_allowlist", ())) if network == "allowlist" else (),
            git=MappingProxyType(dict(p.get("git", {}))),
        )

    def narrow(self, p: Mapping[str, Any]) -> Permissions:
        """Apply a pack's permission request. The result never exceeds self."""
        pack_level = p.get("network") or ("allowlist" if "network_allowlist" in p else None)
        network, hosts = _intersect_network(
            self.network, self.network_allowlist, pack_level, tuple(p.get("network_allowlist", ()))
        )
        return replace(
            self,
            fs_read=self.fs_read + ((_with_real_variants(p["fs_read"]),) if "fs_read" in p else ()),
            fs_write=self.fs_write + ((_with_real_variants(p["fs_write"]),) if "fs_write" in p else ()),
            fs_deny=_dedupe(self.fs_deny + tuple(p.get("fs_deny", ()))),
            commands=self.commands & frozenset(p["commands_allow"]) if "commands_allow" in p else self.commands,
            network=network,
            network_allowlist=hosts,
        )

    # ---- Checks -----------------------------------------------------------

    def can_read(self, path: str | Path, *, directory: bool = False) -> bool:
        return self.why_denied(path, write=False, directory=directory) is None

    def can_write(self, path: str | Path) -> bool:
        return self.why_denied(path, write=True) is None

    def why_denied(self, path: str | Path, *, write: bool, directory: bool = False) -> str | None:
        """None if allowed, otherwise a reason an agent can act on."""
        p = normalize(path)
        for glob in self.fs_deny:
            if fnmatchcase(p, glob):
                return f"matches deny rule {glob!r}"
        # For reads, a directory whose contents match ('/repo/**') is itself listable.
        forms = (p,) if write else (p, p.rstrip("/") + "/")
        for layer in self.fs_write if write else self.fs_read:
            # A directory inside a glob's fixed root ('/repo/' of '/repo/**/*.md') can be
            # listed and searched; its files are still checked one by one.
            if directory and not write and any(_inside_root(p, glob) for glob in layer):
                continue
            if not any(fnmatchcase(f, glob) for glob in layer for f in forms):
                if not layer:
                    return f"this role has no {'write' if write else 'read'} access"
                return f"outside the allowed {'write' if write else 'read'} paths {list(layer)}"
        return None

    def can_run(self, executable: str) -> bool:
        return Path(executable).name in self.commands

    def can_reach(self, host: str) -> bool:
        if self.network == "any":
            return True
        if self.network == "none":
            return False
        return any(fnmatchcase(host.lower(), pattern.lower()) for pattern in self.network_allowlist)

    def to_dict(self) -> dict[str, Any]:
        return {
            "fs_read": [list(layer) for layer in self.fs_read],
            "fs_write": [list(layer) for layer in self.fs_write],
            "fs_deny": list(self.fs_deny),
            "commands_allow": sorted(self.commands),
            "network": self.network,
            "network_allowlist": list(self.network_allowlist),
            "git": dict(self.git),
        }


def normalize(path: str | Path) -> str:
    """Absolute, with '.' and '..' collapsed, so '../' can't escape a glob.

    Symlinks aren't resolved here; the tool guard does that against the real
    filesystem before checking.
    """
    p = os.fspath(path)
    if not os.path.isabs(p):
        raise ValueError(f"permission checks need an absolute path, got {p!r}")
    return os.path.normpath(p)


def _intersect_network(
    role_level: str, role_hosts: tuple[str, ...], pack_level: str | None, pack_hosts: tuple[str, ...]
) -> tuple[str, tuple[str, ...]]:
    if pack_level is None:
        return role_level, role_hosts
    level = min(role_level, pack_level, key=LEVELS.__getitem__)
    if level != "allowlist":
        return level, ()
    if role_level == "any":
        return level, pack_hosts
    if pack_level == "any":
        return level, role_hosts
    # Keep each pattern that the other side also allows.
    kept = [h for h in pack_hosts if any(fnmatchcase(h, r) for r in role_hosts)]
    kept += [r for r in role_hosts if any(fnmatchcase(r, h) for h in pack_hosts)]
    return level, _dedupe(tuple(kept))


def _with_real_variants(globs) -> tuple[str, ...]:
    """Each glob plus a copy whose fixed directory prefix has symlinks resolved.

    The guard checks a path both as written and after resolving symlinks. On
    macOS /var is a symlink to /private/var, so '/var/x/**' must also be
    present as '/private/var/x/**' for the resolved path to match. A symlink
    pointing outside the allowed tree still matches neither variant.
    """
    out = []
    for glob in globs:
        out.append(glob)
        cut = min((i for i, c in enumerate(glob) if c in "*?["), default=len(glob))
        prefix = glob[:cut].rsplit("/", 1)[0] if "/" in glob[:cut] else ""
        if prefix and os.path.isabs(prefix) and os.path.exists(prefix):
            real = os.path.realpath(prefix)
            if real != prefix:
                out.append(real + glob[len(prefix):])
    return _dedupe(tuple(out))


def _dedupe(items: tuple[str, ...]) -> tuple[str, ...]:
    return tuple(dict.fromkeys(items))


def _inside_root(directory: str, glob: str) -> bool:
    """True if `directory` is at or below the fixed part of `glob`, up to its last '/'
    before the first wildcard. '/repo/**/*.md' has root '/repo/'; a glob with no
    wildcard is a single path and has no root to list."""
    cut = min((i for i in (glob.find(c) for c in "*?[") if i >= 0), default=-1)
    if cut < 0:
        return False
    root = glob[: glob.rfind("/", 0, cut) + 1]
    return bool(root) and (directory.rstrip("/") + "/").startswith(root)
