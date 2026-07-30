"""Clone/update against real local git repositories over file:// URLs."""

from __future__ import annotations

from pathlib import Path

import pytest

from draupnir.errors import GitError
from draupnir.gitcmd import GitRunner
from draupnir.models import Repo, Status
from draupnir.sync import RepoSyncer, SyncOptions, _is_transient
from tests.conftest import commit, git, needs_git

pytestmark = needs_git


def repo_for(source: Path, owner: str = "alice", name: str = "tool") -> Repo:
    return Repo(
        owner=owner,
        name=name,
        clone_url=f"file://{source}",
        default_branch="main",
        size_kb=1,
        updated_at="2026-01-01T00:00:00Z",
    )


def syncer(output: Path, **kwargs) -> RepoSyncer:
    options = SyncOptions(output=output, retries=0, **kwargs)
    return RepoSyncer(options, GitRunner(timeout=120))


def test_clone_then_unchanged(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    engine = syncer(out)
    repo = repo_for(origin)

    first = engine.sync(repo)
    assert first.status is Status.CLONED
    assert (out / "alice" / "tool" / "file.txt").is_file()
    assert first.head

    second = engine.sync(repo)
    assert second.status is Status.UNCHANGED
    assert second.head == first.head


def test_update_pulls_new_commits(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    engine = syncer(out)
    repo = repo_for(origin)
    engine.sync(repo)

    commit(origin, "second.txt", "more")
    commit(origin, "third.txt", "more")

    result = engine.sync(repo)
    assert result.status is Status.UPDATED
    assert result.commits_ahead == 2
    assert (out / "alice" / "tool" / "third.txt").is_file()


def test_dirty_worktree_is_never_clobbered(tmp_path: Path, origin: Path) -> None:
    """A mirror must not destroy an operator's local edits."""
    out = tmp_path / "mirror"
    engine = syncer(out)
    repo = repo_for(origin)
    engine.sync(repo)

    local = out / "alice" / "tool" / "file.txt"
    local.write_text("MY LOCAL EDIT", encoding="utf-8")
    commit(origin, "upstream.txt", "new")

    result = engine.sync(repo)
    assert "local changes" in result.detail
    assert local.read_text(encoding="utf-8") == "MY LOCAL EDIT"


def test_force_resets_dirty_worktree(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    engine = syncer(out, force=True)
    repo = repo_for(origin)
    engine.sync(repo)

    local = out / "alice" / "tool" / "file.txt"
    local.write_text("scratch", encoding="utf-8")
    commit(origin, "upstream.txt", "new")

    result = engine.sync(repo)
    assert result.status is Status.UPDATED
    assert local.read_text(encoding="utf-8") == "hello"


def test_occupied_path_reports_conflict(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    squatter = out / "alice" / "tool"
    squatter.mkdir(parents=True)
    (squatter / "important.txt").write_text("do not delete", encoding="utf-8")

    result = syncer(out).sync(repo_for(origin))
    assert result.status is Status.CONFLICT
    assert (squatter / "important.txt").read_text(encoding="utf-8") == "do not delete"


def test_empty_leftover_directory_is_reused(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    (out / "alice" / "tool").mkdir(parents=True)
    result = syncer(out).sync(repo_for(origin))
    assert result.status is Status.CLONED


def test_failed_clone_leaves_no_directory(tmp_path: Path) -> None:
    """Atomic staging: a failure must not leave a half-repo behind."""
    out = tmp_path / "mirror"
    ghost = Repo(owner="a", name="b", clone_url=f"file://{tmp_path}/does-not-exist")
    result = syncer(out).sync(ghost)

    assert result.status is Status.FAILED
    assert not (out / "a" / "b").exists()
    leftovers = list(out.rglob("*.draupnir-tmp-*"))
    assert leftovers == []


def test_mirror_mode_is_bare_and_keeps_all_refs(tmp_path: Path, origin: Path) -> None:
    git("branch", "sidebranch", cwd=origin)
    git("tag", "v1", cwd=origin)
    out = tmp_path / "mirror"

    result = syncer(out, mode="mirror").sync(repo_for(origin))
    assert result.status is Status.CLONED

    target = out / "alice" / "tool"
    assert (target / "HEAD").is_file()
    assert not (target / ".git").exists()
    refs = git("for-each-ref", "--format=%(refname)", cwd=target)
    assert "refs/heads/sidebranch" in refs
    assert "refs/tags/v1" in refs


def test_mirror_mode_detects_upstream_change(tmp_path: Path, origin: Path) -> None:
    """HEAD alone would say 'unchanged' -- the ref snapshot must catch it."""
    out = tmp_path / "mirror"
    engine = syncer(out, mode="mirror")
    repo = repo_for(origin)
    engine.sync(repo)

    commit(origin, "later.txt", "x")
    result = engine.sync(repo)
    assert result.status is Status.UPDATED


def test_worktree_keeps_all_branches(tmp_path: Path, origin: Path) -> None:
    git("branch", "feature", cwd=origin)
    out = tmp_path / "mirror"
    syncer(out).sync(repo_for(origin))
    refs = git("for-each-ref", "--format=%(refname)", cwd=out / "alice" / "tool")
    assert "refs/remotes/origin/feature" in refs


def test_single_branch_limits_refs(tmp_path: Path, origin: Path) -> None:
    git("branch", "feature", cwd=origin)
    out = tmp_path / "mirror"
    syncer(out, single_branch=True).sync(repo_for(origin))
    refs = git("for-each-ref", "--format=%(refname)", cwd=out / "alice" / "tool")
    assert "refs/remotes/origin/feature" not in refs


def test_shallow_depth(tmp_path: Path, origin: Path) -> None:
    commit(origin, "b.txt")
    commit(origin, "c.txt")
    out = tmp_path / "mirror"
    syncer(out, depth=1).sync(repo_for(origin))
    count = git("rev-list", "--count", "HEAD", cwd=out / "alice" / "tool")
    assert count == "1"


def test_empty_repo_skipped(tmp_path: Path) -> None:
    out = tmp_path / "mirror"
    repo = Repo(owner="a", name="b", clone_url="file:///nowhere", empty=True)
    result = syncer(out).sync(repo)
    assert result.status is Status.SKIPPED
    assert "empty" in result.detail


def test_unsafe_name_fails_cleanly(tmp_path: Path, origin: Path) -> None:
    result = syncer(tmp_path / "mirror").sync(repo_for(origin, owner="..", name="evil"))
    assert result.status is Status.FAILED
    assert not (tmp_path / "evil").exists()


def test_flat_layout(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    result = syncer(out, layout="flat").sync(repo_for(origin))
    assert result.status is Status.CLONED
    assert (out / "alice__tool").is_dir()


def test_remote_url_is_realigned(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    engine = syncer(out)
    engine.sync(repo_for(origin))

    moved = origin.parent / "moved"
    origin.rename(moved)
    repo = Repo(owner="alice", name="tool", clone_url=f"file://{moved}", default_branch="main")

    result = engine.sync(repo)
    assert result.status in {Status.UNCHANGED, Status.UPDATED}
    assert git("remote", "get-url", "origin", cwd=out / "alice" / "tool") == f"file://{moved}"


def test_no_credentials_written_to_disk(tmp_path: Path, origin: Path) -> None:
    """The token must never end up in .git/config."""
    out = tmp_path / "mirror"
    options = SyncOptions(output=out, retries=0)
    engine = RepoSyncer(options, GitRunner(timeout=120, token="tok3n-secret-value"))
    engine.sync(repo_for(origin))

    config = (out / "alice" / "tool" / ".git" / "config").read_text(encoding="utf-8")
    assert "tok3n-secret-value" not in config


@pytest.mark.parametrize(
    "message,expected",
    [
        ("fatal: could not resolve host: git.example.org", True),
        ("fatal: the remote end hung up unexpectedly", True),
        ("error: RPC failed; curl 92", True),
        ("fatal: repository 'x' does not exist", False),
        ("fatal: destination path already exists", False),
    ],
)
def test_transient_classification(message: str, expected: bool) -> None:
    assert _is_transient(GitError("clone failed", stderr=message)) is expected


def test_timeout_is_always_transient() -> None:
    assert _is_transient(GitError("timed out", timed_out=True)) is True
