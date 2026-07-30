"""Local mirror inspection: status + verify."""

from __future__ import annotations

import shutil
from pathlib import Path

from draupnir.audit import scan_local, verify_local
from draupnir.gitcmd import GitRunner
from draupnir.models import Outcome, Repo, Status
from draupnir.state import StateStore
from tests.conftest import corrupt_objects, git, needs_git

pytestmark = needs_git


def build_mirror(tmp_path: Path, origin: Path, name: str = "alice/tool") -> Path:
    out = tmp_path / "mirror"
    owner, _, repo = name.partition("/")
    (out / owner).mkdir(parents=True, exist_ok=True)
    git("clone", "-q", str(origin), str(out / owner / repo), cwd=tmp_path)
    return out


def test_scan_finds_repositories(tmp_path: Path, origin: Path) -> None:
    out = build_mirror(tmp_path, origin)
    report = scan_local(out, GitRunner(timeout=60))

    assert [r.name for r in report.repos] == ["alice/tool"]
    entry = report.repos[0]
    assert entry.head
    assert entry.branch == "main"
    assert entry.dirty is False
    assert entry.refs > 0
    assert entry.size_bytes > 0


def test_scan_detects_dirty_worktree(tmp_path: Path, origin: Path) -> None:
    out = build_mirror(tmp_path, origin)
    (out / "alice" / "tool" / "file.txt").write_text("edited", encoding="utf-8")
    report = scan_local(out, GitRunner(timeout=60))
    assert report.dirty and report.dirty[0].name == "alice/tool"


def test_scan_cross_checks_state(tmp_path: Path, origin: Path) -> None:
    out = build_mirror(tmp_path, origin)
    store = StateStore(out)
    store.remember(
        Outcome(
            repo=Repo(owner="ghost", name="gone", clone_url="u", updated_at="2026-01-01T00:00:00Z"),
            status=Status.CLONED,
            path=str(out / "ghost" / "gone"),
        )
    )
    store.save()

    report = scan_local(out, GitRunner(timeout=60))
    assert "ghost/gone" in report.missing
    assert "alice/tool" in report.untracked


def test_scan_ignores_pruned_and_temp_dirs(tmp_path: Path, origin: Path) -> None:
    out = build_mirror(tmp_path, origin)
    shutil.copytree(out / "alice" / "tool", out / ".draupnir-pruned" / "old")
    shutil.copytree(out / "alice" / "tool", out / "alice" / "tool.draupnir-tmp-1-abc")
    report = scan_local(out, GitRunner(timeout=60))
    assert [r.name for r in report.repos] == ["alice/tool"]


def test_scan_missing_directory(tmp_path: Path) -> None:
    report = scan_local(tmp_path / "absent", GitRunner(timeout=60))
    assert report.repos == []


def test_verify_passes_on_healthy_repo(tmp_path: Path, origin: Path) -> None:
    out = build_mirror(tmp_path, origin)
    report = verify_local(scan_local(out, GitRunner(timeout=60)), GitRunner(timeout=120))
    assert report.verified is True
    assert report.broken == []
    assert report.repos[0].intact is True


def test_verify_detects_corruption(tmp_path: Path, origin: Path) -> None:
    """Trash the packfile -> fsck must notice."""
    out = build_mirror(tmp_path, origin)
    assert corrupt_objects(out / "alice" / "tool") > 0

    report = verify_local(scan_local(out, GitRunner(timeout=60)), GitRunner(timeout=120))
    assert report.broken
    assert report.repos[0].intact is False
    assert report.repos[0].problem


def test_bare_mirror_is_recognised(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    (out / "alice").mkdir(parents=True)
    git("clone", "-q", "--mirror", str(origin), str(out / "alice" / "tool.git"), cwd=tmp_path)
    report = scan_local(out, GitRunner(timeout=60))
    assert report.repos[0].bare is True
    assert report.repos[0].refs > 0


def test_interrupted_clone_flagged_as_problem(tmp_path: Path) -> None:
    out = tmp_path / "mirror" / "alice" / "broken"
    out.mkdir(parents=True)
    git("init", "-q", cwd=out)  # a repo with zero commits and zero refs
    report = scan_local(tmp_path / "mirror", GitRunner(timeout=60))
    assert report.repos[0].problem
