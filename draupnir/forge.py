"""Repository discovery against a Gitea/Forgejo instance.

Primary strategy is the REST API (`/api/v1/repos/search`), which returns every
repository on the instance -- users *and* organisations -- in one paginated
sweep, with the metadata needed for filtering and incremental sync.

Fallback is the public `/explore/repos` listing, parsed with the stdlib HTML
parser. It is lossy (no size, no fork/archived flags) so repos found that way
are marked `partial` and filters treat them conservatively.

Pagination is pinned to `sort=id&order=asc`. The default ordering is
"recently updated", which reshuffles between two page requests during a long
sweep -- that silently skips repositories. A stable key is what makes a full
mirror actually full.
"""

from __future__ import annotations

import re
from collections.abc import Iterator, Sequence
from dataclasses import dataclass
from html.parser import HTMLParser
from urllib.parse import urlsplit, urlunsplit

from .errors import DiscoveryError, ForgeError, HttpError
from .http import HttpClient
from .models import ForgeInfo, Repo, dedupe

__all__ = ["ForgeClient", "normalise_base_url"]

# Instance-level routes that are not user namespaces.
_RESERVED_ROUTES = frozenset(
    {
        "explore", "api", "user", "users", "org", "orgs", "repo", "admin", "assets",
        "attachments", "avatars", "avatar", "help", "about", "issues", "pulls",
        "milestones", "notifications", "login", "sign_up", "sign_in", "signout",
        "swagger", "metrics", "manifest.json", "robots.txt", "favicon.ico", "vendor",
        "img", "css", "js", "static", "search", "graphs", "devtest", "ghost",
        "packages", "projects", "actions", "settings", "dashboard", "milestone",
        "stopwatches", "runner", "-",
    }
)
_PAGE_LIMIT = 50  # Gitea's default API cap; asking for more is silently clamped
_MAX_PAGES = 2000  # runaway guard for a server that ignores pagination
_HREF_REPO = re.compile(r"^/([^/?#]+)/([^/?#]+)/?$")


def normalise_base_url(raw: str) -> str:
    """Accept 'git.example.org', a full URL, or one with a trailing path."""
    value = (raw or "").strip()
    if not value:
        raise ForgeError("empty forge URL")
    if "://" not in value:
        value = f"https://{value}"
    split = urlsplit(value)
    if split.scheme not in {"http", "https"}:
        raise ForgeError(f"unsupported scheme in forge URL: {split.scheme!r}")
    if not split.netloc:
        raise ForgeError(f"forge URL has no host: {raw!r}")
    path = split.path.rstrip("/")
    return urlunsplit((split.scheme, split.netloc, path, "", ""))


@dataclass
class ForgeClient:
    """Read-only client for one forge instance."""

    base_url: str
    http: HttpClient
    page_limit: int = _PAGE_LIMIT

    def __post_init__(self) -> None:
        self.base_url = normalise_base_url(self.base_url)

    @property
    def api_root(self) -> str:
        return f"{self.base_url}/api/v1"

    # -- probe -----------------------------------------------------------

    def probe(self) -> ForgeInfo:
        """Identify the remote and whether its API is usable.

        Never raises for an unusable API -- discovery falls back to HTML.
        """
        try:
            payload, response = self.http.get_json(f"{self.api_root}/version")
        except HttpError:
            return ForgeInfo(base_url=self.base_url, kind="unknown", version="", api=False)

        version = ""
        if isinstance(payload, dict):
            version = str(payload.get("version") or "")
        kind = "gitea"
        # Forgejo advertises itself in Server / X-Forgejo headers, and its
        # version string carries a +gitea-x.y.z suffix.
        server = f"{response.header('server')} {version}".lower()
        if "forgejo" in server or any(k.startswith("x-forgejo") for k in response.headers):
            kind = "forgejo"
        return ForgeInfo(base_url=self.base_url, kind=kind, version=version, api=True)

    # -- discovery -------------------------------------------------------

    def discover(
        self,
        *,
        include_private: bool = False,
        allow_html_fallback: bool = True,
        owners: Sequence[str] = (),
    ) -> tuple[list[Repo], ForgeInfo]:
        """Enumerate repositories. API first, HTML only if the API is unusable."""
        info = self.probe()
        errors: list[str] = []

        if info.api:
            try:
                repos = dedupe(self.iter_api_repos(include_private=include_private, owners=owners))
                if repos:
                    return sorted(repos, key=lambda r: r.full_name.lower()), info
                errors.append("API returned zero repositories")
            except HttpError as exc:
                errors.append(f"API search failed: {exc}")

        if not allow_html_fallback:
            raise DiscoveryError("; ".join(errors) or "no repositories found via API")

        try:
            repos = dedupe(self.iter_html_repos())
        except HttpError as exc:
            errors.append(f"HTML fallback failed: {exc}")
            raise DiscoveryError("; ".join(errors)) from exc

        if not repos:
            errors.append("HTML fallback returned zero repositories")
            raise DiscoveryError("; ".join(errors))
        if owners:
            wanted = {o.lower() for o in owners}
            repos = [r for r in repos if r.owner.lower() in wanted]
        return sorted(repos, key=lambda r: r.full_name.lower()), info

    def iter_api_repos(
        self, *, include_private: bool = False, owners: Sequence[str] = ()
    ) -> Iterator[Repo]:
        """Walk /repos/search. Yields every repository the token can see."""
        seen_ids: set[int] = set()
        seen_names: set[str] = set()
        page = 1
        expected: int | None = None

        while page <= _MAX_PAGES:
            params = {
                "page": page,
                "limit": self.page_limit,
                "sort": "id",
                "order": "asc",
            }
            if include_private:
                params["private"] = True
            payload, response = self.http.get_json(f"{self.api_root}/repos/search", params)

            if expected is None:
                expected = _total_count(response.header("x-total-count"))

            rows = _rows(payload)
            if not rows:
                break

            fresh = 0
            for row in rows:
                try:
                    repo = Repo.from_api(row, base_url=self.base_url)
                except ValueError:
                    continue  # unusable row -> skip it, keep the sweep going
                key = repo.full_name.lower()
                if repo.remote_id and repo.remote_id in seen_ids:
                    continue
                if key in seen_names:
                    continue
                if repo.remote_id:
                    seen_ids.add(repo.remote_id)
                seen_names.add(key)
                fresh += 1
                if owners and repo.owner.lower() not in {o.lower() for o in owners}:
                    continue
                yield repo

            # A server that ignores ?page returns the same rows forever.
            if fresh == 0:
                break
            if len(rows) < self.page_limit:
                break
            if expected is not None and len(seen_names) >= expected:
                break
            page += 1

    def iter_html_repos(self) -> Iterator[Repo]:
        """Scrape /explore/repos as a last resort (API disabled or firewalled)."""
        seen: set[str] = set()
        page = 1

        while page <= _MAX_PAGES:
            response = self.http.request(
                "GET",
                f"{self.base_url}/explore/repos",
                params={"page": page, "sort": "alphabetically"},
                accept="text/html",
            )
            hrefs = _extract_hrefs(response.text())
            fresh = 0
            for owner, name in hrefs:
                key = f"{owner}/{name}".lower()
                if key in seen:
                    continue
                seen.add(key)
                fresh += 1
                yield Repo(
                    owner=owner,
                    name=name,
                    clone_url=f"{self.base_url}/{owner}/{name}.git",
                    html_url=f"{self.base_url}/{owner}/{name}",
                    partial=True,
                )
            if fresh == 0:
                break
            page += 1


class _RepoLinkParser(HTMLParser):
    """Collect /<owner>/<repo> hrefs from an explore listing."""

    def __init__(self) -> None:
        super().__init__(convert_charrefs=True)
        self.found: list[tuple[str, str]] = []

    def handle_starttag(self, tag: str, attrs: list[tuple[str, str | None]]) -> None:
        if tag != "a":
            return
        href = dict(attrs).get("href") or ""
        match = _HREF_REPO.match(href)
        if not match:
            return
        owner, name = match.group(1), match.group(2)
        if owner.lower() in _RESERVED_ROUTES or name.lower() in _RESERVED_ROUTES:
            return
        if name.endswith(".git"):
            name = name[: -len(".git")]
        self.found.append((owner, name))


def _extract_hrefs(html: str) -> list[tuple[str, str]]:
    parser = _RepoLinkParser()
    try:
        parser.feed(html)
        parser.close()
    except Exception:  # malformed markup -> keep whatever we parsed
        pass
    return parser.found


def _rows(payload: object) -> list[dict]:
    """Gitea wraps results in {ok, data}; some forks return a bare list."""
    if isinstance(payload, dict):
        data = payload.get("data")
        if isinstance(data, list):
            return [row for row in data if isinstance(row, dict)]
        return []
    if isinstance(payload, list):
        return [row for row in payload if isinstance(row, dict)]
    return []


def _total_count(raw: str) -> int | None:
    try:
        value = int(raw)
    except (TypeError, ValueError):
        return None
    return value if value >= 0 else None
