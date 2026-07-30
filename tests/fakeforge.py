"""A throwaway Gitea-shaped HTTP server used by the tests.

Real sockets, real HTTP, real pagination headers -- the client code under test
runs unmodified. Knobs let a test force 429s, 500s, truncated pages, a disabled
API (to exercise the HTML fallback), or an unstable sort order.
"""

from __future__ import annotations

import json
import threading
from dataclasses import dataclass, field
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from urllib.parse import parse_qs, urlsplit


@dataclass
class ForgeState:
    repos: list[dict] = field(default_factory=list)
    version: str = "1.27.1"
    api_enabled: bool = True
    page_size: int = 50
    fail_times: int = 0  # first N API calls return 500
    rate_limit_times: int = 0  # first N API calls return 429
    retry_after: str = "0"
    send_total_count: bool = True
    forgejo: bool = False
    hits: list[str] = field(default_factory=list)
    explore_pages: int = 1


def make_repo(
    owner: str,
    name: str,
    *,
    rid: int = 0,
    size: int = 10,
    empty: bool = False,
    fork: bool = False,
    archived: bool = False,
    mirror: bool = False,
    private: bool = False,
    template: bool = False,
    stars: int = 0,
    updated: str = "2026-01-01T00:00:00Z",
    clone_url: str = "",
    has_wiki: bool = False,
) -> dict:
    return {
        "id": rid or abs(hash(f"{owner}/{name}")) % 100000,
        "owner": {"login": owner, "username": owner},
        "name": name,
        "full_name": f"{owner}/{name}",
        "description": "",
        "empty": empty,
        "private": private,
        "fork": fork,
        "template": template,
        "mirror": mirror,
        "archived": archived,
        "size": size,
        "stars_count": stars,
        "has_wiki": has_wiki,
        "default_branch": "main",
        "updated_at": updated,
        "clone_url": clone_url or f"https://example.invalid/{owner}/{name}.git",
        "html_url": f"https://example.invalid/{owner}/{name}",
    }


class _Handler(BaseHTTPRequestHandler):
    state: ForgeState

    def log_message(self, *_args) -> None:  # silence
        return

    # -- helpers ---------------------------------------------------------

    def _send(self, code: int, body: bytes, headers: dict[str, str] | None = None) -> None:
        self.send_response(code)
        self.send_header("Content-Type", (headers or {}).pop("Content-Type", "application/json"))
        self.send_header("Content-Length", str(len(body)))
        for key, value in (headers or {}).items():
            self.send_header(key, value)
        self.end_headers()
        self.wfile.write(body)

    def _json(self, code: int, payload, headers: dict[str, str] | None = None) -> None:
        self._send(code, json.dumps(payload).encode(), headers)

    # -- routes ----------------------------------------------------------

    def do_GET(self) -> None:
        split = urlsplit(self.path)
        path, query = split.path, parse_qs(split.query)
        self.state.hits.append(self.path)

        if path == "/api/v1/version":
            if not self.state.api_enabled:
                self._json(404, {"message": "Not Found"})
                return
            headers = {"Server": "forgejo" if self.state.forgejo else "gitea"}
            self._json(200, {"version": self.state.version}, headers)
            return

        if path == "/api/v1/repos/search":
            self._search(query)
            return

        if path == "/explore/repos":
            self._explore(query)
            return

        self._json(404, {"message": "Not Found"})

    def _search(self, query: dict[str, list[str]]) -> None:
        state = self.state
        if not state.api_enabled:
            self._json(404, {"message": "Not Found"})
            return
        if state.rate_limit_times > 0:
            state.rate_limit_times -= 1
            self._json(429, {"message": "slow down"}, {"Retry-After": state.retry_after})
            return
        if state.fail_times > 0:
            state.fail_times -= 1
            self._json(500, {"message": "boom"})
            return

        page = int(query.get("page", ["1"])[0])
        limit = min(int(query.get("limit", [str(state.page_size)])[0]), state.page_size)
        ordered = sorted(state.repos, key=lambda r: r.get("id", 0))
        start = (page - 1) * limit
        window = ordered[start : start + limit]

        headers = {}
        if state.send_total_count:
            headers["X-Total-Count"] = str(len(ordered))
        self._json(200, {"ok": True, "data": window}, headers)

    def _explore(self, query: dict[str, list[str]]) -> None:
        page = int(query.get("page", ["1"])[0])
        if page > self.state.explore_pages:
            self._send(200, b"<html><body>no more</body></html>", {"Content-Type": "text/html"})
            return
        usable = [r for r in self.state.repos if r.get("owner", {}).get("login") and r.get("name")]
        per = max(1, len(usable) // self.state.explore_pages or 1)
        window = usable[(page - 1) * per : page * per]
        rows = "".join(
            f'<a href="/{r["owner"]["login"]}/{r["name"]}">{r["full_name"]}</a>' for r in window
        )
        noise = '<a href="/explore/users">users</a><a href="/user/login">login</a><a href="/">home</a>'
        html = f"<html><body>{noise}{rows}</body></html>".encode()
        self._send(200, html, {"Content-Type": "text/html; charset=utf-8"})


class FakeForge:
    """Context manager exposing `.url` and a mutable `.state`."""

    def __init__(self, state: ForgeState | None = None) -> None:
        self.state = state or ForgeState()
        handler = type("Handler", (_Handler,), {"state": self.state})
        self.server = ThreadingHTTPServer(("127.0.0.1", 0), handler)
        self.thread = threading.Thread(target=self.server.serve_forever, daemon=True)

    @property
    def url(self) -> str:
        host, port = self.server.server_address[:2]
        return f"http://{host}:{port}"

    def __enter__(self) -> FakeForge:
        self.thread.start()
        return self

    def __exit__(self, *_exc: object) -> None:
        self.server.shutdown()
        self.server.server_close()
        self.thread.join(timeout=5)
