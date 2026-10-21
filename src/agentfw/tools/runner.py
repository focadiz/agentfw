"""Run pack tool commands and verification checks.

Safety properties:
- No shell. The template is split into arguments first, then each ${args.x}
  fills exactly one argument, so a value can't add flags or chain commands.
- Argument values must match their declared type and pattern.
- The executable must be in the role's effective commands_allow.
- A minimal environment is passed (no inherited secrets beyond what the pack declares).
- On timeout the whole process group is killed (Maven and Xcode spawn children).

Not enforced here: network access by the command itself (e.g. Maven downloads).
The network allowlist applies to framework tools; sandboxing subprocesses is future work.
"""
from __future__ import annotations

import glob
import os
import re
import shlex
import shutil
import signal
import subprocess
import time
from collections.abc import Mapping
from dataclasses import dataclass
from pathlib import Path
from typing import Any

from ..resolver import Check, PackTool, Resolved
from .audit import AuditLog
from .guard import Guard, ToolInputError
from .requirements import check_requirements

ARG = re.compile(r"\$\{args\.([a-z_]+)\}")
DEFAULT_TIMEOUT = 600
OUTPUT_HEAD_CHARS = 4_000      # build tools often print the real cause first...
OUTPUT_TAIL_CHARS = 8_000      # ...and the summary last
SUMMARY_ERROR_LINES = 10
SUMMARY_TAIL_LINES = 5
ERROR_LINE = re.compile(r"\b(ERROR|FAILED|FAILURE|Exception|error:|fatal:)|\*\* BUILD FAILED \*\*", re.IGNORECASE)
ENV_PASSTHROUGH = (
    "PATH", "HOME", "USER", "LOGNAME", "SHELL", "LANG", "LC_ALL", "TMPDIR",
    "JAVA_HOME", "JENV_VERSION", "MAVEN_OPTS", "DEVELOPER_DIR",
)


@dataclass(frozen=True)
class CommandResult:
    name: str
    argv: tuple[str, ...]
    cwd: str
    exit_code: int | None
    duration_seconds: float
    timed_out: bool
    output: str
    log_file: str

    @property
    def ok(self) -> bool:
        return self.exit_code == 0 and not self.timed_out

    def excerpt(self, head: int = OUTPUT_HEAD_CHARS, tail: int = OUTPUT_TAIL_CHARS) -> str:
        """Start and end of the output; the middle is in the log file."""
        if len(self.output) <= head + tail:
            return self.output
        omitted = len(self.output) - head - tail
        return f"{self.output[:head]}\n... [{omitted} chars omitted; full output in {self.log_file}] ...\n{self.output[-tail:]}"

    def format(self) -> str:
        """What the agent sees."""
        status = "timed out" if self.timed_out else f"exit code {self.exit_code}"
        return (
            f"$ {shlex.join(self.argv)}\n(in {self.cwd})\n{status} after {self.duration_seconds:.1f}s\n"
            f"--- output ---\n{self.excerpt()}"
        )


@dataclass(frozen=True)
class CheckResult:
    check_id: str
    passed: bool
    blocking: bool
    duration_seconds: float
    summary: str
    artifacts: tuple[str, ...]
    log_file: str

    def to_handoff(self) -> dict[str, Any]:
        """The check_result shape used in pull_request and test_report handoffs."""
        return {
            "check_id": self.check_id,
            "passed": self.passed,
            "blocking": self.blocking,
            "duration_seconds": round(self.duration_seconds, 1),
            "summary": self.summary,
            "artifacts": list(self.artifacts),
        }

    def format(self) -> str:
        verdict = "PASSED" if self.passed else "FAILED"
        kind = "blocking" if self.blocking else "non-blocking"
        return f"check {self.check_id} ({kind}): {verdict} in {self.duration_seconds:.1f}s\n{self.summary}\nfull log: {self.log_file}"


class CommandRunner:
    def __init__(self, resolved: Resolved, guard: Guard, audit: AuditLog, *, enforce_requirements: bool = True):
        self.resolved = resolved
        self.guard = guard
        self.audit = audit
        self.enforce_requirements = enforce_requirements
        self._requirement_problems: list[str] | None = None
        self.env_names = ENV_PASSTHROUGH + tuple(
            name for req in resolved.requirements.values() for name in req.get("env", [])
        )

    # ---- Public ---------------------------------------------------------------

    def run_tool(self, tool: PackTool, args: Mapping[str, Any]) -> CommandResult:
        argv = build_argv(tool.command, tool.args, args)
        return self.run(tool.id, argv, tool.timeout_seconds)

    def run_check(self, check: Check) -> CheckResult:
        result = self.run(f"check-{check.id}", build_argv(check.command, {}, {}), check.timeout_seconds)
        expected = check.success.get("exit_code", 0)
        passed = not result.timed_out and result.exit_code == expected
        if result.timed_out:
            summary = f"timed out after {result.duration_seconds:.0f}s"
        elif not passed:
            summary = f"exit code {result.exit_code} (expected {expected})\n" + failure_excerpt(result.output)
        else:
            summary = f"exit code {result.exit_code}"
        forbidden = check.success.get("output_must_not_match")
        if passed and forbidden:
            match = re.search(forbidden, result.output)
            if match:
                passed = False
                summary = f"output matched a forbidden pattern: {match.group(0)[:120]!r}"
        artifacts = _find_artifacts(check.artifacts, result.cwd)
        self.audit.record("check", check=check.id, passed=passed, blocking=check.blocking, log_file=result.log_file)
        return CheckResult(check.id, passed, check.blocking, result.duration_seconds, summary, artifacts, result.log_file)

    def run(self, name: str, argv: list[str], timeout: int | None) -> CommandResult:
        if not argv:
            raise ToolInputError("empty command")
        self.guard.command(argv[0])
        self._check_requirements()
        cwd = self.resolved.command_cwd
        if not cwd or not Path(cwd).is_dir():
            hint = " (QA: run git.checkout first)" if cwd and cwd.endswith("/checkout") else ""
            raise ToolInputError(f"working directory {cwd!r} doesn't exist{hint}")
        env = {k: os.environ[k] for k in self.env_names if k in os.environ}
        executable = shutil.which(argv[0], path=env.get("PATH"))
        if executable is None:
            raise ToolInputError(f"{argv[0]!r} isn't installed or isn't on PATH")
        self._ensure_output_dirs(argv)

        log_file = self.audit.output_file(name)
        timeout = timeout or DEFAULT_TIMEOUT
        start = time.monotonic()
        timed_out = False
        # Own process group, so a timeout can stop the command's children too.
        proc = subprocess.Popen(
            [executable, *argv[1:]], cwd=cwd, env=env, stdin=subprocess.DEVNULL,
            stdout=subprocess.PIPE, stderr=subprocess.STDOUT, text=True, errors="replace",
            start_new_session=True,
        )
        try:
            output, _ = proc.communicate(timeout=timeout)
        except subprocess.TimeoutExpired:
            timed_out = True
            _kill_group(proc)
            output, _ = proc.communicate()
        duration = time.monotonic() - start
        log_file.write_text(f"$ {shlex.join(argv)}\n(in {cwd})\n\n{output}")

        result = CommandResult(name, tuple(argv), cwd, None if timed_out else proc.returncode, duration, timed_out, output, str(log_file))
        self.audit.record(
            "command", name=name, argv=list(argv), cwd=cwd, exit_code=result.exit_code,
            duration_seconds=round(duration, 2), timed_out=timed_out, log_file=str(log_file),
        )
        return result

    def _check_requirements(self) -> None:
        if not self.enforce_requirements or not self.resolved.requirements:
            return
        if self._requirement_problems is None:
            self._requirement_problems = check_requirements(self.resolved)
        if self._requirement_problems:
            raise ToolInputError(
                "host requirements not met, so pack commands can't run here. Ask the human to fix:\n"
                + "\n".join(f"  - {p}" for p in self._requirement_problems)
            )

    def _ensure_output_dirs(self, argv: list[str]) -> None:
        """Create parent dirs for output paths inside the task's scratch dir (e.g. screenshots/)."""
        if not self.resolved.workdir:
            return
        workdir = Path(self.resolved.workdir)
        for token in argv[1:]:
            p = Path(token)
            if p.is_absolute() and Path(os.path.normpath(p)).is_relative_to(workdir):
                p.parent.mkdir(parents=True, exist_ok=True)


def build_argv(template: str, specs: Mapping[str, Mapping[str, Any]], args: Mapping[str, Any]) -> list[str]:
    """Split the template, then substitute validated argument values one argument at a time."""
    unknown = set(args) - set(specs)
    if unknown:
        raise ToolInputError(f"unknown argument(s): {', '.join(sorted(unknown))}")
    values = {name: _validate_arg(name, spec, args.get(name)) for name, spec in specs.items()}
    try:
        tokens = shlex.split(template)
    except ValueError as e:
        raise ToolInputError(f"command template can't be parsed: {e}") from None

    argv = []
    for token in tokens:
        whole = ARG.fullmatch(token)
        if whole:
            value = values.get(whole.group(1))
            if value is None:
                raise ToolInputError(f"template uses undeclared argument {whole.group(1)!r}")
            if value != "":          # an omitted optional argument drops out entirely
                argv.append(value)
            continue
        try:
            argv.append(ARG.sub(lambda m: values[m.group(1)], token))
        except KeyError as e:
            raise ToolInputError(f"template uses undeclared argument {e.args[0]!r}") from None
    return argv


def _validate_arg(name: str, spec: Mapping[str, Any], value: Any) -> str:
    kind = spec["type"]
    if value is None or value == "":
        if spec.get("required"):
            raise ToolInputError(f"argument {name} is required")
        return ""                    # omitted optional argument: no pattern check, drops out of argv
    if kind == "string" and not isinstance(value, str):
        raise ToolInputError(f"argument {name} must be a string")
    elif kind == "integer":
        if isinstance(value, bool) or not isinstance(value, int):
            raise ToolInputError(f"argument {name} must be an integer")
        value = str(value)
    elif kind == "boolean":
        if not isinstance(value, bool):
            raise ToolInputError(f"argument {name} must be true or false")
        value = "true" if value else "false"
    pattern = spec.get("pattern")
    if pattern and not re.fullmatch(pattern, value):
        raise ToolInputError(f"argument {name}={value!r} doesn't match the required pattern {pattern}")
    if "\x00" in value:
        raise ToolInputError(f"argument {name} contains a NUL byte")
    return value


def _find_artifacts(patterns: tuple[str, ...], cwd: str, limit: int = 50) -> tuple[str, ...]:
    found: list[str] = []
    for pattern in patterns:
        full = pattern if os.path.isabs(pattern) else os.path.join(cwd, pattern)
        found += sorted(glob.glob(full, recursive=True))
    return tuple(dict.fromkeys(found))[:limit]


def _kill_group(proc: subprocess.Popen) -> None:
    try:
        os.killpg(proc.pid, signal.SIGTERM)
        proc.wait(timeout=5)
    except subprocess.TimeoutExpired:
        os.killpg(proc.pid, signal.SIGKILL)
    except ProcessLookupError:
        pass


def failure_excerpt(text: str) -> str:
    """The first error-looking lines plus the last few lines: usually the cause and the summary."""
    lines = [line.rstrip() for line in text.splitlines() if line.strip()]
    errors = [line[:800] for line in lines if ERROR_LINE.search(line)][:SUMMARY_ERROR_LINES]
    tail = [line[:300] for line in lines[-SUMMARY_TAIL_LINES:]]
    picked = list(dict.fromkeys(errors + (["..."] if errors else []) + tail))
    return "\n".join(picked)
