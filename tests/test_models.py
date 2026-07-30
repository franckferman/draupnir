"""Payload normalisation: a forge that omits or nulls fields must not crash us."""

from __future__ import annotations

import pytest

from draupnir.models import Repo, Status, dedupe, parse_timestamp


def test_from_api_full_payload() -> None:
    repo = Repo.from_api(
        {
            "id": 7,
            "owner": {"login": "alice"},
            "name": "tool",
            "clone_url": "https://f/alice/tool.git",
            "size": 42,
            "stars_count": 3,
            "fork": True,
            "empty": False,
            "updated_at": "2026-01-02T03:04:05Z",
            "default_branch": "main",
        }
    )
    assert repo.full_name == "alice/tool"
    assert repo.size_kb == 42
    assert repo.stars == 3
    assert repo.fork is True
    assert repo.remote_id == 7
    assert repo.updated.year == 2026


def test_from_api_minimal_payload_derives_clone_url() -> None:
    repo = Repo.from_api({"owner": {"login": "bob"}, "name": "x"}, base_url="https://f")
    assert repo.clone_url == "https://f/bob/x.git"


def test_from_api_falls_back_to_full_name() -> None:
    repo = Repo.from_api({"full_name": "carol/thing"}, base_url="https://f")
    assert (repo.owner, repo.name) == ("carol", "thing")


def test_from_api_rejects_unusable_rows() -> None:
    with pytest.raises(ValueError):
        Repo.from_api({"name": "orphan"}, base_url="https://f")
    with pytest.raises(ValueError):
        Repo.from_api({"owner": {"login": "a"}, "name": "b"})  # no clone url, no base


def test_from_api_tolerates_wrong_types() -> None:
    repo = Repo.from_api(
        {
            "owner": {"login": "a"},
            "name": "b",
            "clone_url": "https://f/a/b.git",
            "size": "not-a-number",
            "stars_count": None,
            "fork": "yes",
            "archived": 1,
            "description": None,
        }
    )
    assert repo.size_kb == 0
    assert repo.stars == 0
    assert repo.fork is True
    assert repo.archived is True
    assert repo.description == ""


@pytest.mark.parametrize(
    "raw",
    ["", None, "0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z", "garbage", 42],
)
def test_null_timestamps_collapse_to_none(raw: object) -> None:
    assert parse_timestamp(raw) is None


def test_timestamp_offsets_normalise_to_utc() -> None:
    zulu = parse_timestamp("2026-01-01T12:00:00Z")
    offset = parse_timestamp("2026-01-01T13:00:00+01:00")
    assert zulu == offset


def test_wiki_url_derivation() -> None:
    repo = Repo(owner="a", name="b", clone_url="https://f/a/b.git")
    assert repo.wiki_clone_url == "https://f/a/b.wiki.git"
    bare = Repo(owner="a", name="b", clone_url="https://f/a/b")
    assert bare.wiki_clone_url == "https://f/a/b.wiki.git"


def test_dedupe_prefers_rich_records() -> None:
    stub = Repo(owner="a", name="b", clone_url="u", partial=True)
    full = Repo(owner="A", name="B", clone_url="u", size_kb=9)
    result = dedupe([stub, full])
    assert len(result) == 1
    assert result[0].partial is False
    assert result[0].size_kb == 9


def test_status_ok_classification() -> None:
    assert Status.CLONED.ok and Status.UNCHANGED.ok and Status.SKIPPED.ok
    assert not Status.FAILED.ok
    assert not Status.CONFLICT.ok
    assert Status.CLONED.touched_disk and not Status.UNCHANGED.touched_disk
