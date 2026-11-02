import pytest

from agentfw.permissions import Permissions

ROLE = {
    "fs_read": ["/repo/**"],
    "fs_write": ["/repo/**"],
    "fs_deny": ["**/.env*", "**/*.p12"],
    "commands_allow": ["git", "mvn", "xcodebuild"],
    "network": "allowlist",
    "network_allowlist": ["github.com", "repo.maven.apache.org", "*.cloudhub.io"],
    "git": {"branch_prefix": "agent/dev/", "can_merge": False},
}


@pytest.fixture
def role() -> Permissions:
    return Permissions.from_role(ROLE)


def test_role_ceiling_alone(role):
    assert role.can_write("/repo/app/main.swift")
    assert not role.can_write("/elsewhere/file")
    assert not role.can_write("/repo/.env.local")          # deny wins
    assert role.can_run("git") and not role.can_run("curl")


def test_pack_narrows_write_paths(role):
    p = role.narrow({"fs_write": ["/repo/Sources/**"]})
    assert p.can_write("/repo/Sources/App.swift")
    assert not p.can_write("/repo/README.md")              # allowed by role, not by pack


def test_pack_cannot_widen_write_paths(role):
    p = role.narrow({"fs_write": ["/etc/**", "/repo/Sources/**"]})
    assert not p.can_write("/etc/hosts")                   # requested by pack, outside role ceiling


def test_deny_lists_are_combined(role):
    p = role.narrow({"fs_deny": ["**/*.pbxproj"]})
    assert not p.can_write("/repo/App.xcodeproj/project.pbxproj")
    assert not p.can_write("/repo/.env")                   # role deny still applies


def test_dotdot_cannot_escape_a_glob(role):
    p = role.narrow({"fs_write": ["/repo/Sources/**"]})
    assert not p.can_write("/repo/Sources/../../etc/passwd")


def test_relative_paths_are_rejected(role):
    with pytest.raises(ValueError, match="absolute"):
        role.can_read("repo/file")


def test_commands_are_intersected(role):
    p = role.narrow({"commands_allow": ["git", "xcodebuild", "curl"]})
    assert p.commands == {"git", "xcodebuild"}             # curl not in role ceiling, mvn not requested
    assert p.can_run("/usr/bin/xcodebuild")


def test_fields_a_pack_omits_are_not_narrowed(role):
    p = role.narrow({"fs_write": ["/repo/Sources/**"]})
    assert p.commands == role.commands
    assert p.network_allowlist == role.network_allowlist


def test_network_hosts_are_intersected(role):
    p = role.narrow({"network": "allowlist", "network_allowlist": ["github.com", "evil.example.com"]})
    assert p.can_reach("github.com")
    assert not p.can_reach("evil.example.com")
    assert not p.can_reach("repo.maven.apache.org")        # not requested by pack


def test_network_wildcards_match_on_either_side(role):
    p = role.narrow({"network": "allowlist", "network_allowlist": ["orders.cloudhub.io"]})
    assert p.can_reach("orders.cloudhub.io")
    assert not p.can_reach("other.cloudhub.io")


def test_network_takes_the_lower_level(role):
    assert role.narrow({"network": "none"}).network == "none"
    assert role.narrow({"network": "any"}).network == "allowlist"
    open_role = Permissions.from_role({**ROLE, "network": "any"})
    p = open_role.narrow({"network": "allowlist", "network_allowlist": ["github.com"]})
    assert p.network == "allowlist" and p.network_allowlist == ("github.com",)


def test_git_settings_come_from_role_only(role):
    assert role.narrow({"fs_write": ["/repo/**"]}).git["can_merge"] is False


def test_role_without_fs_write_cannot_write():
    p = Permissions.from_role({"fs_read": ["/repo/**"]})
    assert not p.can_write("/repo/anything")


def test_allowed_directory_itself_is_readable_but_not_writable(role):
    assert role.can_read("/repo")
    assert not role.can_write("/repo")


def test_directories_inside_a_file_type_glob_can_be_listed_not_read():
    p = Permissions.from_role({"fs_read": ["/repo/*.md", "/repo/**/*.md"], "fs_deny": ["**/.env*"]})
    assert p.can_read("/repo/README.md") and p.can_read("/repo/docs/a/b.md")
    assert not p.can_read("/repo/src/app.xml")
    assert p.can_read("/repo/src", directory=True)                 # list/search it; files filtered
    assert not p.can_read("/repo/src")                              # not as a file path
    assert not p.can_read("/", directory=True)                      # ancestors stay closed
    assert not p.can_read("/other", directory=True)
    assert not p.can_read("/repo/.env.d", directory=True)           # deny still wins


def test_a_glob_without_wildcards_opens_no_directory():
    p = Permissions.from_role({"fs_read": ["/repo/README.md"]})
    assert not p.can_read("/repo", directory=True)


def test_symlinked_prefix_gets_a_resolved_variant(tmp_path):
    real = tmp_path / "real"
    (real / "src").mkdir(parents=True)
    link = tmp_path / "link"
    link.symlink_to(real)
    p = Permissions.from_role({"fs_read": [f"{link}/src/**"]})
    assert p.can_read(f"{link}/src/a.txt")
    assert p.can_read(f"{real.resolve()}/src/a.txt")          # same file, resolved path
    assert not p.can_read(f"{real.resolve()}/other.txt")
