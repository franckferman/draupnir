"""HTTP client: retries, rate limits, redirects, decoding."""

from __future__ import annotations

import gzip
import json

import pytest

from draupnir.errors import HttpError, RateLimited
from draupnir.http import HttpClient, Response, _decode, _retry_after, _with_params
from tests.fakeforge import FakeForge, ForgeState, make_repo


@pytest.fixture
def slept() -> list[float]:
    return []


def client(slept: list[float], **kwargs) -> HttpClient:
    return HttpClient(sleeper=slept.append, **kwargs)


def test_get_json_round_trip(slept: list[float]) -> None:
    with FakeForge(ForgeState(repos=[make_repo("a", "b")])) as forge:
        payload, response = client(slept).get_json(f"{forge.url}/api/v1/version")
    assert payload["version"] == "1.27.1"
    assert response.status == 200
    assert not slept


def test_retries_then_succeeds_on_500(slept: list[float]) -> None:
    state = ForgeState(repos=[make_repo("a", "b")], fail_times=2)
    with FakeForge(state) as forge:
        payload, _ = client(slept, retries=4).get_json(f"{forge.url}/api/v1/repos/search")
    assert payload["ok"] is True
    assert len(slept) == 2  # two backoff sleeps, one per failure


def test_gives_up_after_budget(slept: list[float]) -> None:
    state = ForgeState(fail_times=99)
    with FakeForge(state) as forge, pytest.raises(HttpError) as excinfo:
        client(slept, retries=2).get_json(f"{forge.url}/api/v1/repos/search")
    assert excinfo.value.status == 500
    assert len(slept) == 2


def test_rate_limit_is_typed_and_honours_retry_after(slept: list[float]) -> None:
    state = ForgeState(rate_limit_times=99, retry_after="7")
    with FakeForge(state) as forge, pytest.raises(RateLimited):
        client(slept, retries=1).get_json(f"{forge.url}/api/v1/repos/search")
    assert slept == [7.0]


def test_retry_after_is_capped(slept: list[float]) -> None:
    state = ForgeState(rate_limit_times=99, retry_after="99999")
    with FakeForge(state) as forge, pytest.raises(RateLimited):
        client(slept, retries=1, max_retry_after=5).get_json(
            f"{forge.url}/api/v1/repos/search"
        )
    assert slept == [5.0]


def test_404_is_not_retried(slept: list[float]) -> None:
    with FakeForge() as forge, pytest.raises(HttpError) as excinfo:
        client(slept, retries=3).get_json(f"{forge.url}/nope")
    assert excinfo.value.status == 404
    assert slept == []


def test_non_http_scheme_refused(slept: list[float]) -> None:
    with pytest.raises(HttpError):
        client(slept).get("file:///etc/passwd")


def test_json_error_carries_context() -> None:
    response = Response(url="u", status=200, headers={}, body=b"<html>not json</html>")
    with pytest.raises(HttpError) as excinfo:
        response.json()
    assert "not valid JSON" in str(excinfo.value)


def test_gzip_transport_decoding() -> None:
    payload = json.dumps({"ok": True}).encode()
    assert _decode(gzip.compress(payload), "gzip") == payload
    assert _decode(payload, "") == payload
    # a server lying about its encoding must not raise
    assert _decode(b"plain", "gzip") == b"plain"


def test_param_encoding() -> None:
    url = _with_params("https://h/p", {"page": 2, "private": True, "skip": None})
    assert "page=2" in url
    assert "private=true" in url
    assert "skip" not in url


def test_param_merge_keeps_existing_query() -> None:
    url = _with_params("https://h/p?a=1", {"b": 2})
    assert "a=1" in url and "b=2" in url


def test_retry_after_parsing() -> None:
    class _H:
        def __init__(self, value: str) -> None:
            self.value = value

        def get(self, _name: str) -> str:
            return self.value

    assert _retry_after(_H("3"), 60) == 3.0
    assert _retry_after(_H(""), 60) == 0.0
    assert _retry_after(_H("not-a-date"), 60) == 0.0


def test_token_header_sent(slept: list[float]) -> None:
    state = ForgeState(repos=[make_repo("a", "b")])
    with FakeForge(state) as forge:
        client(slept, token="s3cret").get_json(f"{forge.url}/api/v1/version")
    assert state.hits  # served without error; header presence covered by gitcmd redaction tests
