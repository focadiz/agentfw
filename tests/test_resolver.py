import copy

import pytest

from agentfw.config import load_workspace
from agentfw.resolver import ResolutionError, resolve


@pytest.fixture(scope="module")
def project(ws):
    return ws.projects["ridexpress"].data


def task(ws, task_id):
    return copy.deepcopy(ws.project_handoffs("ridexpress")[task_id].data)


def test_developer_on_ios_task(ws, project, tmp_path):
    r = resolve(ws, "ridexpress", "developer", task(ws, "task-002"), work_root=tmp_path)
    mobile = f"{project['root']}/mobile-app"

    assert r.packs == ("ios",)
    assert r.command_cwd == mobile
    assert "Platform: iOS" in r.instructions                    # pack prompt appended
    assert r.instructions.startswith("# Role: Developer")

    tools = {t.id: t for t in r.pack_tools}
    assert "-scheme RideXpress" in tools["ios.build"].command
    assert "${args.only_testing}" in tools["ios.test"].command   # left for call time
    assert "\n" not in tools["ios.build"].command
    assert {c.id for c in r.verification} == {"build", "unit-tests", "lint", "no-hardcoded-secrets"}

    p = r.permissions
    assert p.can_write(f"{mobile}/Sources/Features/SignUp/SignUpView.swift")
    assert not p.can_write(f"{mobile}/README.md")               # outside the pack's paths
    assert not p.can_write(f"{project['root']}/mulesoft/pom.xml")
    assert not p.can_write(f"{mobile}/Sources/RideXpress.xcodeproj/project.pbxproj")
    assert p.can_run("xcodebuild") and not p.can_run("mvn")     # narrowed to the iOS pack
    assert p.git["can_merge"] is False


def test_developer_on_mulesoft_task(ws, project, tmp_path):
    r = resolve(ws, "ridexpress", "developer", task(ws, "task-001"), work_root=tmp_path)
    repo = f"{project['root']}/mulesoft"

    assert r.packs == ("mulesoft",)
    assert r.command_cwd == repo
    munit = next(c for c in r.verification if c.id == "munit")
    assert munit.command == "mvn -f user-sign-up-process-api/pom.xml test"
    assert r.permissions.can_write(f"{repo}/user-sign-up-process-api/src/main/mule/users.xml")
    assert not r.permissions.can_write(f"{repo}/okta-system-api/src/main/mule/okta.xml")


def test_qa_runs_in_a_clean_checkout(ws, tmp_path):
    r = resolve(ws, "ridexpress", "qa", task(ws, "task-001"), work_root=tmp_path)
    checkout = tmp_path / "ridexpress" / "task-001" / "checkout"

    assert r.command_cwd == str(checkout)
    assert "Platform: MuleSoft" not in r.instructions            # QA doesn't load pack instructions
    assert {c.id for c in r.verification} >= {"build", "munit"}
    assert r.permissions.git["can_push"] is False


def test_architect_gets_knowledge_from_every_platform_but_no_tools(ws, project):
    r = resolve(ws, "ridexpress", "architect")
    assert set(r.packs) == {"mulesoft", "ios"}
    assert r.pack_tools == () and r.verification == () and r.command_cwd is None
    origins = {k.origin for k in r.knowledge}
    assert origins == {"role", "mulesoft", "ios"}
    specs = next(k for k in r.knowledge if k.id == "api-specs")
    assert specs.location.startswith(f"{project['root']}/mulesoft/")
    assert "mulesoft, ios" in r.instructions                    # ${project.platforms} expanded


def test_pm_has_no_packs(ws):
    r = resolve(ws, "ridexpress", "pm")
    assert r.packs == () and r.command_cwd is None
    assert not r.permissions.can_write("/tmp/anything")


def test_role_that_needs_a_task_fails_without_one(ws):
    with pytest.raises(ResolutionError, match="a task is required"):
        resolve(ws, "ridexpress", "developer")


def test_disabled_role_needs_explicit_override(ws):
    with pytest.raises(ResolutionError, match="disabled"):
        resolve(ws, "ridexpress", "devops", task(ws, "task-001"))


def test_devops_gets_the_deploy_pack(ws):
    t = task(ws, "task-001")
    t["body"]["pack_variables"]["app"] = "user-sign-up-process-api"
    r = resolve(ws, "ridexpress", "devops", t, allow_disabled=True)
    assert r.packs == ("mulesoft-cloudhub",)
    health = next(c for c in r.verification if c.id == "health")
    assert "ridexpress-develop-user-sign-up-process-api" in health.command   # nested ${pack.app}


def test_missing_task_variable_is_reported(ws):
    t = task(ws, "task-001")
    del t["body"]["pack_variables"]
    with pytest.raises(ResolutionError, match=r"pack\.api_dir"):
        resolve(ws, "ridexpress", "developer", t)


def test_non_task_handoff_is_rejected(ws):
    spec = ws.project_handoffs("ridexpress")["spec-001"].data
    with pytest.raises(ResolutionError, match="not a task"):
        resolve(ws, "ridexpress", "developer", spec)


def test_project_model_override(ws_copy):
    root, edit = ws_copy
    edit("projects/ridexpress.yaml", "  - id: pm\n", "  - id: pm\n    model_id: cheap/model\n")
    r = resolve(load_workspace(root), "ridexpress", "pm")
    assert r.model["id"] == "cheap/model"


def test_project_paths_may_be_relative_home_or_env(monkeypatch, tmp_path):
    from pathlib import Path

    from agentfw.config import _resolve_project_paths

    monkeypatch.setenv("RX", str(tmp_path / "rx"))
    data = {"root": "../RideXpress", "docs_root": "$RX/docs", "handoffs_dir": "~/handoffs"}
    _resolve_project_paths(data, tmp_path / "framework")
    assert data == {"root": str((tmp_path / "RideXpress").resolve()), "docs_root": str(tmp_path / "rx" / "docs"),
                    "handoffs_dir": str(Path.home() / "handoffs")}
