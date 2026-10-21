"""Check a resolved role's host requirements (OS, binaries and versions, env vars) before any LLM call."""
from __future__ import annotations

import os
import platform
import re
import shlex
import shutil
import subprocess
from collections.abc import Mapping

from ..resolver import Resolved

OS_NAMES = {"Darwin": "macos", "Linux": "linux", "Windows": "windows"}
VERSION = re.compile(r"(\d+)(?:\.(\d+))?(?:\.(\d+))?")


class RequirementsError(Exception):
    def __init__(self, problems: list[str]):
        super().__init__("host requirements not met:\n" + "\n".join(f"  - {p}" for p in problems))
        self.problems = problems


def check_requirements(resolved: Resolved, env: Mapping[str, str] | None = None) -> list[str]:
    """Problems found, as readable lines. Empty means ready to run."""
    env = os.environ if env is None else env
    current_os = OS_NAMES.get(platform.system(), platform.system().lower())
    problems: list[str] = []
    for pack_id, req in resolved.requirements.items():
        if req.get("os") and current_os not in req["os"]:
            problems.append(f"{pack_id}: needs {' or '.join(req['os'])}, this machine is {current_os}")
        for binary in req.get("binaries", []):
            problems += _check_binary(pack_id, binary, env)
        for name in req.get("env", []):
            if not env.get(name):
                problems.append(f"{pack_id}: environment variable {name} isn't set")
    return problems


def ensure_requirements(resolved: Resolved) -> None:
    problems = check_requirements(resolved)
    if problems:
        raise RequirementsError(problems)


def _check_binary(pack_id: str, binary: Mapping, env: Mapping[str, str]) -> list[str]:
    name = binary["name"]
    if shutil.which(name, path=env.get("PATH")) is None:
        return [f"{pack_id}: {name} isn't installed or isn't on PATH"]
    low, high = binary.get("min_version"), binary.get("below_version")
    if not (low or high):
        return []
    if not binary.get("version_cmd"):
        return [f"{pack_id}: {name} has a version requirement but no version_cmd"]
    try:
        out = subprocess.run(
            shlex.split(binary["version_cmd"]), capture_output=True, text=True, timeout=30,
            env=dict(env), stdin=subprocess.DEVNULL,
        )
    except (OSError, subprocess.TimeoutExpired) as e:
        return [f"{pack_id}: couldn't run {binary['version_cmd']!r}: {e}"]
    found = parse_version(out.stdout + out.stderr)     # java -version prints to stderr
    if found is None:
        return [f"{pack_id}: couldn't find a version number in the output of {binary['version_cmd']!r}"]
    shown = ".".join(map(str, found))
    if low and found < parse_version(low):
        return [f"{pack_id}: {name} {shown} is too old (need {low} or newer)"]
    if high and found >= parse_version(high):
        return [f"{pack_id}: {name} {shown} is too new (need a version below {high})"]
    return []


def parse_version(text: str) -> tuple[int, int, int] | None:
    """First version-like number in the text, padded to three parts: '3.10' -> (3, 10, 0)."""
    m = VERSION.search(text)
    if not m:
        return None
    return tuple(int(part or 0) for part in m.groups())   # type: ignore[return-value]
