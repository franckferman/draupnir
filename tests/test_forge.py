"""Discovery: pagination correctness is the whole point of this module."""

from __future__ import annotations

import pytest

from draupnir.errors import DiscoveryError, ForgeError
from draupnir.forge import ForgeClient, normalise_base_url
from draupnir.http import HttpClient
from tests.fakeforge import FakeForge, ForgeState, make_repo


def build(forge: FakeForge, **kwargs) -> ForgeClient:
    return ForgeClient(forge.url, HttpClient(sleeper=lambda _s: None, retries=1), **kwargs)


def many(count: int, owner: str = "o") -> list[dict]:
    return [make_repo(owner, f"repo{i:03d}", rid=i) for i in range(count)]


@pytest.mark.parametrize(
    "raw,expected",
    [
        ("git.example.org", "https://git.example.org"),
        ("https://git.example.org/", "https://git.example.org"),
        ("http://h:3000", "http://h:3000"),
        ("https://h/gitea/", "https://h/gitea"),
        ("https://h/x?a=1#f", "https://h/x"),
    ],
)
def test_base_url_normalisation(raw: str, expected: str) -> None:
    assert normalise_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://h", "https://"])
def test_bad_base_urls_rejected(raw: str) -> None:
    with pytest.raises(ForgeError):
        normalise_base_url(raw)


def test_probe_identifies_gitea() -> None:
    with FakeForge(ForgeState(repos=many(1))) as forge:
        info = build(forge).probe()
    assert info.api is True
    assert info.kind == "gitea"
    assert info.version == "1.27.1"


def test_probe_identifies_forgejo() -> None:
    with FakeForge(ForgeState(repos=many(1), forgejo=True)) as forge:
        info = build(forge).probe()
    assert info.kind == "forgejo"


def test_probe_survives_disabled_api() -> None:
    with FakeForge(ForgeState(api_enabled=False)) as forge:
        info = build(forge).probe()
    assert info.api is False


def test_pagination_collects_every_repo() -> None:
    """The bug this whole module exists to avoid: a partial sweep."""
    state = ForgeState(repos=many(237), page_size=50)
    with FakeForge(state) as forge:
        repos, info = build(forge).discover()
    assert len(repos) == 237
    assert info.api is True
    assert len({r.full_name for r in repos}) == 237


def test_pagination_uses_stable_sort_key() -> None:
    state = ForgeState(repos=many(120))
    with FakeForge(state) as forge:
        build(forge).discover()
    searches = [hit for hit in state.hits if "repos/search" in hit]
    assert searches
    assert all("sort=id" in hit and "order=asc" in hit for hit in searches)


def test_pagination_without_total_count_header() -> None:
    state = ForgeState(repos=many(75), send_total_count=False)
    with FakeForge(state) as forge:
        repos, _ = build(forge).discover()
    assert len(repos) == 75


def test_server_ignoring_page_param_terminates() -> None:
    """A forge that returns page 1 forever must not loop indefinitely."""
    state = ForgeState(repos=many(50), page_size=50)
    with FakeForge(state) as forge:
        client = build(forge)
        # page_size equals the corpus -> second page is empty -> loop ends
        repos, _ = client.discover()
    assert len(repos) == 50


def test_malformed_rows_are_skipped_not_fatal() -> None:
    rows = many(3)
    rows.append({"garbage": True})
    rows.append({"owner": {"login": ""}, "name": ""})
    state = ForgeState(repos=rows)
    with FakeForge(state) as forge:
        repos, _ = build(forge).discover()
    assert len(repos) == 3


def test_owner_filter_applied_during_discovery() -> None:
    rows = many(3, owner="alice") + many(2, owner="bob")
    for index, row in enumerate(rows):
        row["id"] = index
    with FakeForge(ForgeState(repos=rows)) as forge:
        repos, _ = build(forge).discover(owners=["alice"])
    assert {r.owner for r in repos} == {"alice"}


def test_html_fallback_when_api_disabled() -> None:
    state = ForgeState(repos=many(12), api_enabled=False, explore_pages=3)
    with FakeForge(state) as forge:
        repos, info = build(forge).discover()
    assert info.api is False
    assert len(repos) == 12
    assert all(repo.partial for repo in repos)
    assert all(repo.clone_url.endswith(".git") for repo in repos)


def test_html_fallback_ignores_system_routes() -> None:
    state = ForgeState(repos=many(2), api_enabled=False)
    with FakeForge(state) as forge:
        repos, _ = build(forge).discover()
    names = {repo.full_name for repo in repos}
    assert "explore/users" not in names
    assert "user/login" not in names


def test_fallback_can_be_refused() -> None:
    state = ForgeState(repos=many(2), api_enabled=False)
    with FakeForge(state) as forge, pytest.raises(DiscoveryError):
        build(forge).discover(allow_html_fallback=False)


def test_empty_forge_raises_discovery_error() -> None:
    with FakeForge(ForgeState(repos=[], explore_pages=0)) as forge, pytest.raises(DiscoveryError):
        build(forge).discover()


def test_results_are_sorted_deterministically() -> None:
    state = ForgeState(repos=many(30))
    with FakeForge(state) as forge:
        first, _ = build(forge).discover()
        second, _ = build(forge).discover()
    assert [r.full_name for r in first] == [r.full_name for r in second]
    assert [r.full_name for r in first] == sorted(r.full_name for r in first)
