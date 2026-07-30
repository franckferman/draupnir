"""Shared fixtures. Real git, real sockets -- no mocking of the units under test."""

from __future__ import annotations

import shutil
import subprocess
import sys
from pathlib import Path

import pytest

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from draupnir.gitcmd import GitRunner
from tests.fakeforge import FakeForge, ForgeState, make_repo

GIT = shutil.which("git")
needs_git = pytest.mark.skipif(GIT is None, reason="git binary not available")

_GIT_ENV = {
    "GIT_AUTHOR_NAME": "test",
    "GIT_AUTHOR_EMAIL": "test@example.invalid",
    "GIT_COMMITTER_NAME": "test",
    "GIT_COMMITTER_EMAIL": "test@example.invalid",
    "GIT_CONFIG_GLOBAL": "/dev/null",
    "GIT_CONFIG_SYSTEM": "/dev/null",
}


def git(*args: str, cwd: Path) -> str:
    """Run git in a hermetic environment (ignores the operator's ~/.gitconfig)."""
    import os

    env = {**os.environ, **_GIT_ENV}
    proc = subprocess.run(
        [GIT, *args], cwd=str(cwd), env=env, capture_output=True, text=True, check=True
    )
    return proc.stdout.strip()


def commit(repo: Path, filename: str = "file.txt", content: str = "hello") -> str:
    (repo / filename).write_text(content, encoding="utf-8")
    git("add", "-A", cwd=repo)
    git("commit", "-m", f"add {filename}", cwd=repo)
    return git("rev-parse", "HEAD", cwd=repo)


def corrupt_objects(worktree: Path, *, bare: bool = False) -> int:
    """Repack, then trash the packfile. git leaves packs mode 0444."""
    import os

    git("repack", "-A", "-d", cwd=worktree)
    objects = worktree / ("objects" if bare else ".git/objects")
    damaged = 0
    for pack in objects.glob("pack/*.pack"):
        os.chmod(pack, 0o644)
        pack.write_bytes(b"CORRUPTED-BY-TEST")
        damaged += 1
    return damaged


@pytest.fixture
def runner() -> GitRunner:
    return GitRunner(timeout=120)


@pytest.fixture
def origin(tmp_path: Path) -> Path:
    """A real non-bare source repo with one commit, usable as a file:// remote."""
    path = tmp_path / "origin"
    path.mkdir()
    git("init", "-q", "-b", "main", cwd=path)
    commit(path)
    return path


@pytest.fixture
def bare_origin(tmp_path: Path, origin: Path) -> Path:
    """Bare clone of `origin` -- pushable, so tests can simulate upstream commits."""
    path = tmp_path / "origin.git"
    git("clone", "-q", "--bare", str(origin), str(path), cwd=tmp_path)
    return path


@pytest.fixture
def forge():
    with FakeForge() as server:
        yield server


__all__ = [
    "GIT",
    "FakeForge",
    "ForgeState",
    "commit",
    "git",
    "make_repo",
    "needs_git",
]
