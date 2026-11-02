"""Guard, command runner, requirements, fs tools and the tool factory."""
import copy
import json
import os
import sys
import time
from dataclasses import replace
from pathlib import Path

import pytest

from agentfw.permissions import Permissions
from agentfw.resolver import Check, PackTool, resolve
from agentfw.tools import (
    Guard, PermissionDenied, ToolInputError, build_argv, build_toolset, check_requirements,
)
from agentfw.tools.requirements import parse_version


@pytest.fixture(autouse=True)
def isolated_home(tmp_path, monkeypatch):
    """Keep audit logs and scratch dirs out of the real ~/.agentfw."""
    monkeypatch.setenv("AGENTFW_HOME", str(tmp_path / "agentfw-home"))


@pytest.fixture
def bin_dir(tmp_path, monkeypatch):
    """A PATH dir for fake executables. `pyrun` is this Python, so tests can run small scripts."""
    d = tmp_path / "bin"
    d.mkdir()
    (d / "pyrun").symlink_to(sys.executable)
    monkeypatch.setenv("PATH", f"{d}{os.pathsep}{os.environ['PATH']}")

    def fake(name: str, output: str, *, stderr: bool = False) -> None:
        stream = "sys.stderr" if stderr else "sys.stdout"
        script = d / name
        script.write_text(f"#!{sys.executable}\nimport sys\nprint({output!r}, file={stream})\n")
        script.chmod(0o755)

    return fake


@pytest.fixture
def sandbox(ws, tmp_path):
    """A real resolved Developer role, re-pointed at a temp tree with simple permissions."""
    root = tmp_path / "proj"
    (root / "src").mkdir(parents=True)
    (root / "docs").mkdir()
    (root / "src" / "app.txt").write_text("hello\nTODO: fix\n")
    (root / "docs" / "notes.md").write_text("TODO in docs\n")
    (root / ".env").write_text("SECRET=TODO-leak\n")
    (tmp_path / "outside.txt").write_text("outside TODO\n")

    task = copy.deepcopy(ws.project_handoffs("ridexpress")["task-001"].data)
    base = resolve(ws, "ridexpress", "developer", task, work_root=tmp_path / "work")
    perms = Permissions.from_role({
        "fs_read": [f"{root}/**"], "fs_write": [f"{root}/**"], "fs_deny": ["**/.env*"],
        "commands_allow": ["pyrun", "git"], "network": "none",
    }).narrow({"fs_write": [f"{root}/src/**"]})
    return replace(base, project_root=str(root), command_cwd=str(root), permissions=perms,
                   requirements={}, workdir=str(tmp_path / "work" / "task-001")), root


# ---- Guard -----------------------------------------------------------------

def test_guard_resolves_relative_paths_against_the_worktree(sandbox):
    r, root = sandbox
    assert Guard(r).path("src/app.txt") == (root / "src" / "app.txt").resolve()


def test_guard_explains_denials(sandbox):
    r, root = sandbox
    g = Guard(r)
    with pytest.raises(PermissionDenied, match="deny rule '\\*\\*/.env\\*'"):
        g.path(".env")
    with pytest.raises(PermissionDenied, match="outside the allowed write paths"):
        g.path("docs/notes.md", write=True)
    with pytest.raises(PermissionDenied, match="command 'curl' isn't allowed"):
        g.command("curl")


def test_guard_blocks_symlink_escape(sandbox, tmp_path):
    r, root = sandbox
    (root / "src" / "link.txt").symlink_to(tmp_path / "outside.txt")
    with pytest.raises(PermissionDenied, match="symlink to"):
        Guard(r).path("src/link.txt")


def test_guard_blocks_dotdot_escape(sandbox):
    r, _ = sandbox
    with pytest.raises(PermissionDenied):
        Guard(r).path("src/../../outside.txt")


# ---- argv building ---------------------------------------------------------

SPECS = {
    "name": {"type": "string", "description": "", "pattern": "^[a-z0-9-]+$"},
    "free": {"type": "string", "description": ""},
    "count": {"type": "integer", "description": ""},
}


def test_args_fill_exactly_one_argument():
    argv = build_argv("tool --label ${args.free} out/${args.name}.png", SPECS, {"free": "a b; rm -rf /", "name": "shot-1"})
    assert argv == ["tool", "--label", "a b; rm -rf /", "out/shot-1.png"]


def test_omitted_optional_arg_drops_out():
    assert build_argv("tool test ${args.free}", SPECS, {}) == ["tool", "test"]


def test_required_args_must_be_present():
    with pytest.raises(ToolInputError, match="argument name is required"):
        build_argv("tool ${args.name}", {**SPECS, "name": {**SPECS["name"], "required": True}}, {})


def test_pattern_and_type_are_enforced():
    with pytest.raises(ToolInputError, match="doesn't match"):
        build_argv("tool ${args.name}", SPECS, {"name": "../../etc"})
    with pytest.raises(ToolInputError, match="must be an integer"):
        build_argv("tool ${args.count}", SPECS, {"count": "3"})
    with pytest.raises(ToolInputError, match="unknown argument"):
        build_argv("tool", SPECS, {"extra": "x"})


def test_quoted_template_parts_stay_together():
    argv = build_argv("xcodebuild -destination 'platform=iOS Simulator,name=iPhone 16' build", {}, {})
    assert argv == ["xcodebuild", "-destination", "platform=iOS Simulator,name=iPhone 16", "build"]


# ---- Command runner ------------------------------------------------------------

def run_tool(r, command, args=None, specs=None, timeout=None):
    ts = build_toolset(r, check_requirements=False)
    return ts.context.runner.run_tool(PackTool("test", "t.run", "", command, specs or {}, timeout), args or {}), ts


def test_runner_runs_in_the_worktree_and_logs(sandbox, bin_dir):
    r, root = sandbox
    result, ts = run_tool(r, "pyrun -c 'import os; print(os.getcwd())'")
    assert result.ok and result.output.strip() == str(root.resolve())
    assert Path(result.log_file).read_text().endswith(result.output)
    events = [json.loads(line) for line in ts.context.audit.path.read_text().splitlines()]
    assert events[-1]["event"] == "command" and events[-1]["exit_code"] == 0


def test_runner_does_not_leak_environment(sandbox, bin_dir, monkeypatch):
    r, _ = sandbox
    monkeypatch.setenv("SUPER_SECRET_TOKEN", "abc")
    result, _ = run_tool(r, "pyrun -c 'import os; print(os.environ.get(\"SUPER_SECRET_TOKEN\"))'")
    assert result.output.strip() == "None"


def test_runner_refuses_commands_outside_the_allowlist(sandbox):
    r, _ = sandbox
    with pytest.raises(PermissionDenied, match="'bash' isn't allowed"):
        run_tool(r, "bash -c 'echo hi'")


def test_runner_kills_the_process_group_on_timeout(sandbox, bin_dir):
    r, _ = sandbox
    start = time.monotonic()
    result, _ = run_tool(r, "pyrun -c 'import subprocess, time; subprocess.Popen([\"sleep\", \"30\"]); time.sleep(30)'", timeout=1)
    assert result.timed_out and result.exit_code is None
    assert time.monotonic() - start < 10


def test_missing_worktree_is_reported(sandbox, tmp_path):
    r, _ = sandbox
    r = replace(r, command_cwd=str(tmp_path / "work" / "task-001" / "checkout"))
    with pytest.raises(ToolInputError, match="run git.checkout first"):
        run_tool(r, "pyrun -c 'print(1)'")


def check(command, **kw):
    return Check("test", kw.pop("id", "c"), "", command, kw.pop("blocking", True), kw.pop("success", {}), kw.pop("artifacts", ()), None)


def test_checks_pass_fail_and_forbidden_output(sandbox, bin_dir):
    r, root = sandbox
    runner = build_toolset(r, check_requirements=False).context.runner
    assert runner.run_check(check("pyrun -c 'print(\"ok\")'")).passed

    failed = runner.run_check(check("pyrun -c 'import sys; print(\"boom happened\"); sys.exit(2)'"))
    assert not failed.passed and "exit code 2" in failed.summary and "boom happened" in failed.summary

    leaked = runner.run_check(check("pyrun -c 'print(\"api_key = \\\"AAAAAAAAAAAAAAAAAAAA\\\"\")'",
                                    success={"output_must_not_match": r"api_key\s*=\s*\"[A-Z]{16,}"}))
    assert not leaked.passed and "forbidden pattern" in leaked.summary

    (root / "src" / "report.xml").write_text("<r/>")
    with_artifacts = runner.run_check(check("pyrun -c 'print(1)'", artifacts=("src/*.xml",)))
    assert with_artifacts.artifacts == (str(root / "src" / "report.xml"),)
    assert with_artifacts.to_handoff()["check_id"] == "c"


# ---- Requirements ------------------------------------------------------------

def test_version_parsing():
    assert parse_version('openjdk version "27" 2026-09-15') == (27, 0, 0)
    assert parse_version("Apache Maven 3.10.0 (c43a36b)") == (3, 10, 0)
    assert parse_version("Xcode 16.4\nBuild version 16F6") == (16, 4, 0)
    assert parse_version("no digits here") is None


def test_requirements_catch_versions_binaries_and_env(sandbox, bin_dir):
    r, _ = sandbox
    bin_dir("fakemvn", "Apache Maven 3.10.0")
    bin_dir("fakejava", 'openjdk version "17.0.12"', stderr=True)      # java prints to stderr
    r = replace(r, requirements={"p": {
        "binaries": [
            {"name": "fakemvn", "version_cmd": "fakemvn -v", "min_version": "3.9.0", "below_version": "3.10"},
            {"name": "fakejava", "version_cmd": "fakejava -version", "min_version": "17", "below_version": "18"},
            {"name": "not-installed-anywhere"},
        ],
        "env": ["AGENTFW_TEST_UNSET_VAR"],
    }})
    problems = check_requirements(r)
    assert "p: fakemvn 3.10.0 is too new (need a version below 3.10)" in problems
    assert not any("fakejava" in p for p in problems)
    assert "p: not-installed-anywhere isn't installed or isn't on PATH" in problems
    assert "p: environment variable AGENTFW_TEST_UNSET_VAR isn't set" in problems
    r = replace(r, pack_tools=(PackTool("p", "p.run", "", "pyrun -c 'print(1)'", {}, None),))
    ts = build_toolset(r)
    assert ts.get("fs.read").invoke({"path": "src/app.txt"}).startswith("/")      # non-pack tools still work
    blocked = ts.get("p.run").invoke({})
    assert blocked.startswith("Error: host requirements not met") and "fakemvn 3.10.0 is too new" in blocked
    assert build_toolset(r, check_requirements=False).get("p.run").invoke({}).startswith("$ pyrun")


# ---- fs tools via the toolset --------------------------------------------------

@pytest.fixture
def tools(sandbox):
    r, root = sandbox
    ts = build_toolset(r, check_requirements=False)
    return ts, root


def test_fs_read_and_write(tools):
    ts, root = tools
    assert "TODO: fix" in ts.get("fs.read").invoke({"path": "src/app.txt"})
    assert ts.get("fs.write").invoke({"path": "src/new/deep/file.txt", "content": "x"}).startswith("Created")
    assert (root / "src" / "new" / "deep" / "file.txt").read_text() == "x"


def test_fs_read_stops_at_a_whole_line_and_says_where_to_resume(tools, monkeypatch):
    ts, root = tools
    monkeypatch.setattr("agentfw.tools.fs.MAX_READ_CHARS", 100)
    (root / "src" / "long.txt").write_text("".join(f"line {n}\n" for n in range(1, 51)))
    out = ts.get("fs.read").invoke({"path": "src/long.txt"})
    header, *rows, note = out.splitlines()
    last = int(rows[-1].split()[0])
    assert header.endswith(f"(lines 1-{last} of 50)") and last < 50
    assert rows[-1].endswith(f"line {last}")                                   # not cut mid-line
    assert f"start_line={last + 1}" in note
    resumed = ts.get("fs.read").invoke({"path": "src/long.txt", "start_line": last + 1})
    assert f"(lines {last + 1}-" in resumed.splitlines()[0]


def test_fs_read_returns_one_line_even_if_it_is_too_long(tools, monkeypatch):
    ts, root = tools
    monkeypatch.setattr("agentfw.tools.fs.MAX_READ_CHARS", 50)
    (root / "src" / "wide.txt").write_text("x" * 500 + "\nshort\n")
    out = ts.get("fs.read").invoke({"path": "src/wide.txt"}).splitlines()
    assert "(lines 1-1 of 2)" in out[0] and len(out[1]) == 50 and "start_line=2" in out[2]


def test_denied_write_is_returned_and_audited(tools):
    ts, root = tools
    result = ts.get("fs.write").invoke({"path": "docs/notes.md", "content": "overwrite"})
    assert result.startswith("Denied:") and "outside the allowed write paths" in result
    assert (root / "docs" / "notes.md").read_text() == "TODO in docs\n"          # unchanged
    denial = ts.context.audit.events[-1]
    assert denial["outcome"] == "denied" and denial["tool"] == "fs.write"
    assert denial["args"]["content"] == '<11 chars>'                             # content not logged


def test_search_and_list_skip_unreadable_files(tools):
    ts, _ = tools
    found = ts.get("fs.search").invoke({"pattern": "TODO"})
    assert "src/app.txt:2" in found and "docs/notes.md:1" in found
    assert "leak" not in found                                                    # .env content never shown
    listing = ts.get("fs.list").invoke({"path": ".", "glob": "*"})
    assert ".env" not in listing and "1 entries hidden" in listing


def test_markdown_only_role_can_search_folders_but_sees_only_markdown(sandbox):
    r, root = sandbox
    perms = Permissions.from_role({"fs_read": [f"{root}/*.md", f"{root}/**/*.md"], "fs_deny": ["**/.env*"]})
    ts = build_toolset(replace(r, permissions=perms), check_requirements=False)
    found = ts.get("fs.search").invoke({"pattern": "TODO", "path": str(root)})
    assert found == "docs/notes.md:1: TODO in docs"                              # app.txt skipped, no denial
    assert ts.get("fs.list").invoke({"path": str(root / "src")}).endswith("(1 entries hidden: not readable by this role)")
    assert ts.get("fs.read").invoke({"path": "src/app.txt"}).startswith("Denied:")
    listing = ts.get("fs.list").invoke({"path": str(root)})
    assert "docs/" in listing and "src/" not in listing                          # nothing readable in src/


def test_invalid_arguments_come_back_as_errors(tools):
    ts, _ = tools
    assert ts.get("fs.read").invoke({"path": 5}).startswith("Error: invalid arguments")
    assert ts.context.audit.events[-1]["outcome"] == "error"


# ---- Factory on the real configs -------------------------------------------------

def test_factory_on_real_developer_role(ws):
    task = ws.project_handoffs("ridexpress")["task-002"].data
    ts = build_toolset(resolve(ws, "ridexpress", "developer", task), check_requirements=False)
    names = {t.name for t in ts.tools}
    assert {"fs_read", "fs_write", "ios_build", "ios_test", "ios_screenshot", "run_check"} <= names
    assert all("." not in n for n in names)                                      # API-safe names
    assert "github.pr.create" in ts.unavailable and "MCP" in ts.unavailable["github.pr.create"]
    run_check = ts.get("run_check")
    assert run_check.args_schema["properties"]["check_id"]["enum"] == ["build", "unit-tests", "lint", "no-hardcoded-secrets"]
    assert ts.get("ios.screenshot").args_schema["properties"]["name"]["pattern"] == "^[a-z0-9-]+$"


def test_pack_tool_argument_injection_is_rejected(ws):
    task = ws.project_handoffs("ridexpress")["task-002"].data
    ts = build_toolset(resolve(ws, "ridexpress", "developer", task), check_requirements=False)
    result = ts.get("ios.screenshot").invoke({"name": "x; rm -rf ~"})
    assert result.startswith("Error: invalid arguments") and "does not match" in result


def test_cli_call_denied_path(ws, capsys):
    from agentfw.cli import main
    code = main(["resolve", "ridexpress", "developer", "--task", "task-001"])
    assert code == 0
    capsys.readouterr()
    code = main(["call", "ridexpress", "developer", "--task", "task-001", "fs.write",
                 "--arg", "path=okta-system-api/src/main/mule/x.xml", "--arg", "content=<x/>"])
    out = capsys.readouterr().out
    assert code == 1 and out.startswith("Denied:") and "outside the allowed write paths" in out


def test_long_output_keeps_head_and_tail(sandbox, bin_dir):
    r, _ = sandbox
    script = "print('ROOT CAUSE: NoSuchMethodError'); [print('classpath', i) for i in range(5000)]; print('BUILD FAILURE')"
    result, _ = run_tool(r, f'pyrun -c "{script}"')
    shown = result.format()
    assert "ROOT CAUSE" in shown and "BUILD FAILURE" in shown and "chars omitted" in shown
    assert len(shown) < 13_000


def test_failure_summary_prefers_error_lines():
    from agentfw.tools.runner import failure_excerpt
    output = "\n".join(["[INFO] start", "[ERROR] Failed to execute goal: NoSuchMethodError"]
                       + [f"[ERROR] urls[{i}] = file:/x.jar" for i in range(60)] + ["[ERROR] -> [Help 1]"])
    summary = failure_excerpt(output)
    assert summary.splitlines()[0] == "[ERROR] Failed to execute goal: NoSuchMethodError"
    assert summary.splitlines()[-1] == "[ERROR] -> [Help 1]"
    assert len(summary.splitlines()) <= 16
