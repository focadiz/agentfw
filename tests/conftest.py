import shutil
from pathlib import Path

import pytest

from agentfw.config import Workspace, load_workspace

FRAMEWORK_ROOT = Path(__file__).resolve().parents[1]
COPY_IGNORE = shutil.ignore_patterns(".venv", ".work", "__pycache__", ".pytest_cache", "*.egg-info", "tests")


@pytest.fixture(scope="session")
def ws() -> Workspace:
    return load_workspace(FRAMEWORK_ROOT)


@pytest.fixture
def ws_copy(tmp_path):
    """A writable copy of the framework, for tests that break configs on purpose."""
    root = tmp_path / "framework"
    shutil.copytree(FRAMEWORK_ROOT, root, ignore=COPY_IGNORE)

    def edit(relative: str, old: str, new: str) -> None:
        path = root / relative
        text = path.read_text()
        assert old in text, f"{old!r} not in {relative}"
        path.write_text(text.replace(old, new, 1))

    return root, edit
