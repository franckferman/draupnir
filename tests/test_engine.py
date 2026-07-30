"""Engine: filtering, planning, concurrency, failure isolation, pruning."""

from __future__ import annotations

from pathlib import Path

import pytest

from draupnir.engine import Event, MirrorConfig, MirrorEngine
from draupnir.errors import ConfigError, DiscoveryError
from draupnir.models import Status
from tests.conftest import commit, git, needs_git
from tests.fakeforge import FakeForge, ForgeState, make_repo


def config(url: str, output: Path, **kwargs) -> MirrorConfig:
    base = {"base_url": url, "output": output, "jobs": 2, "http_retries": 0, "use_lock": False}
    base.update(kwargs)
    return MirrorConfig(**base)


# -- validation ----------------------------------------------------------


@pytest.mark.parametrize(
    "kwargs",
    [
        {"mode": "nope"},
        {"layout": "nope"},
        {"jobs": 0},
        {"depth": -1},
        {"timeout": 0},
        {"include": ["(unclosed"]},
    ],
)
def test_invalid_config_rejected(tmp_path: Path, kwargs) -> None:
    with pytest.raises(ConfigError):
        MirrorConfig(base_url="https://h", output=tmp_path, **kwargs).validate()


# -- planning ------------------------------------------------------------


def plan_with(tmp_path: Path, repos: list[dict], **kwargs):
    with FakeForge(ForgeState(repos=repos)) as forge:
        engine = MirrorEngine(config(forge.url, tmp_path, **kwargs))
        return engine.plan()


def test_filters_by_flags(tmp_path: Path) -> None:
    repos = [
        make_repo("a", "normal", rid=1),
        make_repo("a", "forked", rid=2, fork=True),
        make_repo("a", "old", rid=3, archived=True),
        make_repo("a", "mirrored", rid=4, mirror=True),
        make_repo("a", "blank", rid=5, empty=True),
    ]
    plan = plan_with(
        tmp_path, repos, skip_forks=True, skip_archived=True, skip_mirrors=True
    )
    assert [r.name for r in plan.selected] == ["normal"]
    assert set(plan.reasons()) == {"fork", "archived", "mirror", "empty"}


def test_regex_include_exclude(tmp_path: Path) -> None:
    repos = [
        make_repo("alice", "keep", rid=1),
        make_repo("alice", "drop-me", rid=2),
        make_repo("bob", "keep", rid=3),
    ]
    plan = plan_with(tmp_path, repos, include=[r"^alice/"], exclude=[r"drop"])
    assert [r.full_name for r in plan.selected] == ["alice/keep"]


def test_glob_match(tmp_path: Path) -> None:
    repos = [make_repo("a", "tool-x", rid=1), make_repo("a", "lib-y", rid=2)]
    plan = plan_with(tmp_path, repos, match=["*/tool-*"])
    assert [r.name for r in plan.selected] == ["tool-x"]


def test_star_and_size_thresholds(tmp_path: Path) -> None:
    repos = [
        make_repo("a", "popular", rid=1, stars=10, size=5),
        make_repo("a", "ignored", rid=2, stars=0, size=5),
        make_repo("a", "huge", rid=3, stars=10, size=10_000),
    ]
    plan = plan_with(tmp_path, repos, min_stars=5, max_size_kb=1000)
    assert [r.name for r in plan.selected] == ["popular"]


def test_owner_filter(tmp_path: Path) -> None:
    repos = [make_repo("alice", "x", rid=1), make_repo("bob", "y", rid=2)]
    plan = plan_with(tmp_path, repos, owners=("alice",))
    assert [r.owner for r in plan.selected] == ["alice"]


def test_partial_repos_survive_flag_filters(tmp_path: Path) -> None:
    """HTML stubs have every flag False -- they must not be dropped as 'empty'."""
    state = ForgeState(repos=[make_repo("a", "x", rid=1)], api_enabled=False)
    with FakeForge(state) as forge:
        engine = MirrorEngine(config(forge.url, tmp_path, skip_forks=True, skip_archived=True))
        plan = engine.plan()
    assert len(plan.selected) == 1
    assert plan.selected[0].partial is True


def test_limit_caps_and_records_reason(tmp_path: Path) -> None:
    repos = [make_repo("a", f"r{i}", rid=i) for i in range(10)]
    plan = plan_with(tmp_path, repos, limit=3)
    assert len(plan.selected) == 3
    assert plan.reasons()["over --limit"] == 7


def test_largest_first_ordering(tmp_path: Path) -> None:
    repos = [
        make_repo("a", "small", rid=1, size=1),
        make_repo("a", "big", rid=2, size=999),
        make_repo("a", "mid", rid=3, size=50),
    ]
    plan = plan_with(tmp_path, repos)
    assert [r.name for r in plan.selected] == ["big", "mid", "small"]


# -- execution -----------------------------------------------------------


@needs_git
def test_dry_run_touches_nothing(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        report = MirrorEngine(config(forge.url, out, dry_run=True)).run()

    assert report.planned == 1
    assert all(o.status is Status.PLANNED for o in report.outcomes)
    assert not (out / "alice").exists()
    assert not (out / ".draupnir-state.json").exists()


@needs_git
def test_full_run_clones_and_reports(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        report = MirrorEngine(config(forge.url, out)).run()

    assert report.success
    assert report.counts() == {"cloned": 1}
    assert (out / "alice" / "tool" / "file.txt").is_file()
    assert (out / ".draupnir-state.json").is_file()


@needs_git
def test_one_failure_does_not_abort_the_run(tmp_path: Path, origin: Path) -> None:
    """The classic executor.map() bug: a broken repo must not swallow the rest."""
    out = tmp_path / "mirror"
    rows = [
        make_repo("alice", "good", rid=1, clone_url=f"file://{origin}"),
        make_repo("alice", "broken", rid=2, clone_url=f"file://{tmp_path}/nope"),
        make_repo("alice", "good2", rid=3, clone_url=f"file://{origin}"),
    ]
    with FakeForge(ForgeState(repos=rows)) as forge:
        report = MirrorEngine(config(forge.url, out)).run()

    counts = report.counts()
    assert counts["cloned"] == 2
    assert counts["failed"] == 1
    assert not report.success
    assert report.failures[0].repo.name == "broken"


@needs_git
def test_incremental_second_run_skips_everything(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        first = MirrorEngine(config(forge.url, out)).run()
        second = MirrorEngine(config(forge.url, out)).run()

    assert first.counts() == {"cloned": 1}
    assert second.planned == 0
    assert second.excluded.get("up to date") == 1


@needs_git
def test_refresh_all_bypasses_cache(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        MirrorEngine(config(forge.url, out)).run()
        second = MirrorEngine(config(forge.url, out, refresh_all=True)).run()
    assert second.counts() == {"unchanged": 1}


@needs_git
def test_changed_timestamp_triggers_resync(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    state = ForgeState(repos=[row])
    with FakeForge(state) as forge:
        MirrorEngine(config(forge.url, out)).run()
        commit(origin, "new.txt", "x")
        state.repos[0]["updated_at"] = "2026-09-09T00:00:00Z"
        second = MirrorEngine(config(forge.url, out)).run()

    assert second.counts() == {"updated": 1}
    assert second.outcomes[0].commits_ahead == 1


@needs_git
def test_listener_receives_events(tmp_path: Path, origin: Path) -> None:
    seen: list[Event] = []
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        MirrorEngine(config(forge.url, tmp_path / "m"), listener=seen.append).run()

    kinds = [event.kind for event in seen]
    assert "discovered" in kinds and "planned" in kinds and "repo_done" in kinds
    done = next(e for e in seen if e.kind == "repo_done")
    assert done.outcome is not None and done.total == 1


@needs_git
def test_broken_listener_cannot_break_the_run(tmp_path: Path, origin: Path) -> None:
    def explode(_event: Event) -> None:
        raise RuntimeError("ui bug")

    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        report = MirrorEngine(config(forge.url, tmp_path / "m"), listener=explode).run()
    assert report.success


@needs_git
def test_prune_moves_vanished_repos_without_deleting(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    rows = [
        make_repo("alice", "keeper", rid=1, clone_url=f"file://{origin}"),
        make_repo("alice", "goner", rid=2, clone_url=f"file://{origin}"),
    ]
    state = ForgeState(repos=rows)
    with FakeForge(state) as forge:
        MirrorEngine(config(forge.url, out)).run()
        state.repos = [rows[0]]  # forge dropped one
        report = MirrorEngine(config(forge.url, out, prune_deleted=True)).run()

    assert report.pruned == ["alice/goner"]
    assert not (out / "alice" / "goner").exists()
    graveyard = out / ".draupnir-pruned" / "alice__goner"
    assert graveyard.is_dir()  # moved, never deleted
    assert (out / "alice" / "keeper").is_dir()


@needs_git
def test_prune_is_off_by_default(tmp_path: Path, origin: Path) -> None:
    out = tmp_path / "mirror"
    rows = [make_repo("alice", "goner", rid=1, clone_url=f"file://{origin}")]
    state = ForgeState(repos=rows)
    with FakeForge(state) as forge:
        MirrorEngine(config(forge.url, out)).run()
        state.repos = []
        # empty forge -> discovery error, and nothing on disk is touched
        with pytest.raises(DiscoveryError):
            MirrorEngine(config(forge.url, out)).run()
    assert (out / "alice" / "goner").is_dir()


@needs_git
def test_mirror_mode_end_to_end(tmp_path: Path, origin: Path) -> None:
    git("tag", "v1", cwd=origin)
    out = tmp_path / "mirror"
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        report = MirrorEngine(config(forge.url, out, mode="mirror")).run()

    assert report.success
    assert (out / "alice" / "tool" / "HEAD").is_file()


def test_report_json_shape(tmp_path: Path) -> None:
    rows = [make_repo("a", "b", rid=1)]
    with FakeForge(ForgeState(repos=rows)) as forge:
        report = MirrorEngine(config(forge.url, tmp_path / "m", dry_run=True)).run()

    blob = report.to_dict()
    assert blob["forge"]["kind"] == "gitea"
    assert blob["dry_run"] is True
    assert blob["counts"] == {"planned": 1}
    assert blob["repos"][0]["repo"] == "a/b"
    assert "draupnir" in blob
