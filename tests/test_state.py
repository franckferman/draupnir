"""State cache and the concurrent-run lock."""

from __future__ import annotations

import json
import os
from pathlib import Path

import pytest

from draupnir.errors import LockError
from draupnir.models import Outcome, Repo, Status
from draupnir.state import OutputLock, StateStore


def repo(name: str = "a/b", updated: str = "2026-01-01T00:00:00Z") -> Repo:
    owner, _, short = name.partition("/")
    return Repo(owner=owner, name=short, clone_url="u", updated_at=updated)


def outcome(target: Repo, status: Status = Status.CLONED, path: str = "/p") -> Outcome:
    return Outcome(repo=target, status=status, path=path, head="deadbeef")


def test_round_trip(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.remember(outcome(repo()))
    store.save()

    reloaded = StateStore(tmp_path).load()
    assert "a/b" in reloaded.repos
    assert reloaded.repos["a/b"].head == "deadbeef"
    assert reloaded.repos["a/b"].status == "cloned"


def test_missing_file_is_empty_state(tmp_path: Path) -> None:
    assert StateStore(tmp_path).load().repos == {}


def test_corrupt_file_degrades_and_is_quarantined(tmp_path: Path) -> None:
    (tmp_path / ".draupnir-state.json").write_text("{not json", encoding="utf-8")
    store = StateStore(tmp_path).load()
    assert store.repos == {}
    assert list(tmp_path.glob(".draupnir-state.corrupt-*"))


def test_future_version_ignored(tmp_path: Path) -> None:
    (tmp_path / ".draupnir-state.json").write_text(
        json.dumps({"version": 99, "repos": {"a/b": {}}}), encoding="utf-8"
    )
    assert StateStore(tmp_path).load().repos == {}


def test_save_is_atomic_and_leaves_no_temp(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.remember(outcome(repo()))
    store.save()
    assert not list(tmp_path.glob(".draupnir-state-*.tmp"))
    json.loads((tmp_path / ".draupnir-state.json").read_text(encoding="utf-8"))


def test_is_current_requires_matching_timestamp(tmp_path: Path) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    store = StateStore(tmp_path)
    store.remember(outcome(repo(), path=str(target)))

    assert store.is_current(repo(), target) is True
    assert store.is_current(repo(updated="2026-06-06T00:00:00Z"), target) is False


def test_is_current_false_when_directory_gone(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.remember(outcome(repo()))
    assert store.is_current(repo(), tmp_path / "absent") is False


def test_is_current_false_without_timestamp(tmp_path: Path) -> None:
    """HTML-discovered repos have no updated_at -> never trust the cache."""
    target = tmp_path / "repo"
    target.mkdir()
    store = StateStore(tmp_path)
    store.remember(outcome(repo(updated="")))
    assert store.is_current(repo(updated=""), target) is False


def test_partial_repos_are_never_cached(tmp_path: Path) -> None:
    target = tmp_path / "repo"
    target.mkdir()
    store = StateStore(tmp_path)
    store.remember(outcome(repo()))
    stub = Repo(owner="a", name="b", clone_url="u", updated_at="2026-01-01T00:00:00Z", partial=True)
    assert store.is_current(stub, target) is False


def test_failures_do_not_overwrite_good_records(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.remember(outcome(repo()))
    store.remember(Outcome(repo=repo(), status=Status.FAILED, detail="network"))
    assert store.repos["a/b"].status == "cloned"


def test_conflict_not_recorded(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.remember(Outcome(repo=repo(), status=Status.CONFLICT))
    assert store.repos == {}


def test_forget(tmp_path: Path) -> None:
    store = StateStore(tmp_path)
    store.remember(outcome(repo()))
    store.forget("a/b")
    assert store.repos == {}


# -- lock ----------------------------------------------------------------


def test_lock_is_exclusive(tmp_path: Path) -> None:
    with OutputLock(tmp_path), pytest.raises(LockError):
        OutputLock(tmp_path).acquire(steal_stale=False)


def test_lock_released_on_exit(tmp_path: Path) -> None:
    with OutputLock(tmp_path):
        pass
    with OutputLock(tmp_path):  # would raise if the first was not released
        pass


def test_stale_lock_is_stolen(tmp_path: Path) -> None:
    """A killed run must not wedge the directory forever."""
    stale = tmp_path / ".draupnir.lock"
    stale.write_text(json.dumps({"pid": 999_999_999, "since": "2020-01-01"}), encoding="utf-8")
    with OutputLock(tmp_path) as lock:
        assert lock.acquired
    assert not stale.exists()


def test_live_lock_is_not_stolen(tmp_path: Path) -> None:
    live = tmp_path / ".draupnir.lock"
    live.write_text(json.dumps({"pid": os.getpid(), "since": "now"}), encoding="utf-8")
    with pytest.raises(LockError) as excinfo:
        OutputLock(tmp_path).acquire()
    assert str(os.getpid()) in str(excinfo.value)


def test_unreadable_lock_content_is_survivable(tmp_path: Path) -> None:
    (tmp_path / ".draupnir.lock").write_text("garbage", encoding="utf-8")
    with OutputLock(tmp_path) as lock:  # no pid -> treated as stale
        assert lock.acquired
