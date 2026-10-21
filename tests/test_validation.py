import pytest

from agentfw.cli import main
from agentfw.config import load_workspace
from agentfw.validation import validate


def test_shipped_configs_are_valid(ws):
    report = validate(ws)
    assert report.errors == []
    assert report.warnings == ["WARN  roles/developer.yaml: allowed pack 'salesforce' doesn't exist yet"]


# Each case breaks one thing in a copy of the framework and expects one specific error.
BREAKAGES = [
    pytest.param(
        "examples/ridexpress-uc1/design-001.yaml", "    REQ-5: [CMP-2, CMP-5]\n", "",
        "should requirement REQ-5 isn't covered", id="uncovered-requirement"),
    pytest.param(
        "examples/ridexpress-uc1/test_report-001.yaml", "  verdict: fail", "  verdict: pass",
        "verdict 'pass' is inconsistent", id="inconsistent-verdict"),
    pytest.param(
        "examples/ridexpress-uc1/task-001.yaml", "[build, munit, no-plaintext-secrets]", "[build, munit, e2e]",
        "required check 'e2e' isn't in pack mulesoft", id="unknown-check"),
    pytest.param(
        "examples/ridexpress-uc1/task-001.yaml", "  pack_variables:\n    api_dir: user-sign-up-process-api\n", "",
        "pack mulesoft needs task variable 'api_dir'", id="missing-task-variable"),
    pytest.param(
        "skill-packs/ios.yaml", "commands_allow: [git, xcodebuild, xcrun, swift, swiftlint]",
        "commands_allow: [git, xcodebuild, xcrun, swift, swiftlint, curl]",
        "requests commands ['curl'] outside role 'developer' ceiling", id="pack-exceeds-ceiling"),
    pytest.param(
        "skill-packs/ios.yaml", 'command: "swiftlint lint --strict"', 'command: "bash -c lint"',
        "runs 'bash', which the pack's commands_allow doesn't include", id="pack-runs-unlisted-command"),
    pytest.param(
        "skill-packs/mulesoft.yaml", "mvn -f ${pack.api_dir}/pom.xml test\"\n    timeout",
        "mvn -f ${pack.app_dir}/pom.xml test\"\n    timeout",
        "${pack.app_dir} is not declared", id="undeclared-pack-variable"),
    pytest.param(
        "roles/pm.yaml", "  - human.ask\n", "  - shell.run\n",
        "tool 'shell.run' is not in the tool registry", id="unknown-tool"),
    pytest.param(
        "roles/qa.yaml", "produces: [test_report]", "produces: [pull_request]",
        "consumes 'test_report', which no enabled role produces", id="broken-pipeline"),
    pytest.param(
        "roles/pm.yaml", "  - when: before_handoff", "  - on: before_handoff",
        "'when' is a required property", id="yaml-on-key"),
    pytest.param(
        "projects/ridexpress.yaml", "      scheme: RideXpress\n", "",
        "pack ios needs variable 'scheme'", id="missing-project-variable"),
]


@pytest.mark.parametrize("relative, old, new, expected", BREAKAGES)
def test_breakage_is_caught(ws_copy, relative, old, new, expected):
    root, edit = ws_copy
    edit(relative, old, new)
    report = validate(load_workspace(root))
    assert any(expected in e for e in report.errors), report.format()


def test_resolution_errors_surface_in_validation(ws_copy):
    root, edit = ws_copy
    edit("roles/prompts/developer.md", "# Role: Developer", "# Role: Developer for ${project.owner}")
    report = validate(load_workspace(root))
    assert any("resolving role developer" in e and "project.owner" in e for e in report.errors)


def test_cli_validate_exit_codes(ws_copy, capsys):
    root, edit = ws_copy
    assert main(["--root", str(root), "validate"]) == 0
    edit("roles/pm.yaml", "  - human.ask\n", "  - shell.run\n")
    assert main(["--root", str(root), "validate"]) == 1
    assert "shell.run" in capsys.readouterr().out


def test_cli_resolve(ws_copy, capsys):
    root, _ = ws_copy
    assert main(["--root", str(root), "resolve", "ridexpress", "developer", "--task", "task-002"]) == 0
    out = capsys.readouterr().out
    assert "packs:        ios" in out and "commands run in:" in out
    assert main(["--root", str(root), "resolve", "ridexpress", "developer", "--task", "nope"]) == 2
