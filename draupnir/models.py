"""Value objects shared by every layer.

All remote payloads are funnelled through Repo.from_api() so the rest of the
codebase never touches a raw dict -> one place to harden against a forge that
renames, drops, or nulls a field.
"""

from __future__ import annotations

import re
from collections.abc import Iterable, Mapping
from dataclasses import dataclass, field, replace
from datetime import datetime, timezone
from enum import Enum
from typing import Any

__all__ = [
    "ForgeInfo",
    "Outcome",
    "Plan",
    "Repo",
    "RunReport",
    "Status",
    "parse_timestamp",
]

# Gitea/Forgejo hand back RFC3339. Accept a trailing Z, an offset, or neither.
_ZULU = re.compile(r"Z$", re.IGNORECASE)
# Gitea uses these as "never" sentinels instead of null.
_NULL_TIMES = frozenset(
    {"", "0001-01-01T00:00:00Z", "1970-01-01T00:00:00Z", "0001-01-01T00:00:00+00:00"}
)


def parse_timestamp(raw: Any) -> datetime | None:
    """RFC3339 -> aware datetime. Sentinels and junk collapse to None."""
    if not isinstance(raw, str) or raw.strip() in _NULL_TIMES:
        return None
    text = _ZULU.sub("+00:00", raw.strip())
    try:
        parsed = datetime.fromisoformat(text)
    except ValueError:
        return None
    if parsed.tzinfo is None:
        parsed = parsed.replace(tzinfo=timezone.utc)
    return parsed.astimezone(timezone.utc)


def _as_bool(value: Any, default: bool = False) -> bool:
    if isinstance(value, bool):
        return value
    if isinstance(value, str):
        return value.strip().lower() in {"1", "true", "yes", "on"}
    if isinstance(value, (int, float)):
        return bool(value)
    return default


def _as_int(value: Any, default: int = 0) -> int:
    try:
        return int(value)
    except (TypeError, ValueError):
        return default


def _as_str(value: Any, default: str = "") -> str:
    return value.strip() if isinstance(value, str) else default


class Status(str, Enum):
    """Terminal state of one repository within a run."""

    CLONED = "cloned"
    UPDATED = "updated"
    UNCHANGED = "unchanged"
    SKIPPED = "skipped"
    FAILED = "failed"
    CONFLICT = "conflict"  # path taken by something that is not our repo
    PLANNED = "planned"  # dry-run only

    @property
    def ok(self) -> bool:
        return self in _OK_STATUSES

    @property
    def touched_disk(self) -> bool:
        return self in {Status.CLONED, Status.UPDATED}


_OK_STATUSES = frozenset(
    {Status.CLONED, Status.UPDATED, Status.UNCHANGED, Status.SKIPPED, Status.PLANNED}
)


@dataclass(frozen=True)
class ForgeInfo:
    """What we managed to learn about the remote before enumerating."""

    base_url: str
    kind: str = "unknown"  # gitea | forgejo | unknown
    version: str = ""
    api: bool = False  # REST API reachable -> HTML fallback not needed

    def describe(self) -> str:
        if not self.version:
            return f"{self.kind} (version unknown)"
        return f"{self.kind} {self.version}"


@dataclass(frozen=True)
class Repo:
    """One remote repository, normalised."""

    owner: str
    name: str
    clone_url: str
    default_branch: str = ""
    description: str = ""
    html_url: str = ""
    size_kb: int = 0
    stars: int = 0
    empty: bool = False
    fork: bool = False
    archived: bool = False
    mirror: bool = False
    private: bool = False
    template: bool = False
    has_wiki: bool = False
    updated_at: str = ""
    remote_id: int = 0
    # Set when the repo came from the HTML fallback: flags above are guesses,
    # so filters that depend on them must not silently drop it.
    partial: bool = False

    @property
    def full_name(self) -> str:
        return f"{self.owner}/{self.name}"

    @property
    def updated(self) -> datetime | None:
        return parse_timestamp(self.updated_at)

    @property
    def wiki_clone_url(self) -> str:
        """Gitea serves wikis at <repo>.wiki.git."""
        base = self.clone_url
        if base.endswith(".git"):
            base = base[: -len(".git")]
        return f"{base}.wiki.git"

    @classmethod
    def from_api(cls, payload: Mapping[str, Any], *, base_url: str = "") -> Repo:
        """Build from a Gitea/Forgejo repository object.

        Tolerates missing keys; raises ValueError only when owner/name -- the two
        fields we genuinely cannot invent -- are unusable.
        """
        owner_blob = payload.get("owner")
        owner = ""
        if isinstance(owner_blob, Mapping):
            owner = _as_str(owner_blob.get("login")) or _as_str(owner_blob.get("username"))
        name = _as_str(payload.get("name"))

        # Fall back to splitting full_name when the nested owner object is absent.
        full = _as_str(payload.get("full_name"))
        if (not owner or not name) and "/" in full:
            head, _, tail = full.partition("/")
            owner = owner or head.strip()
            name = name or tail.strip()

        if not owner or not name:
            raise ValueError(f"repository payload without owner/name: {full or payload!r}")

        clone_url = _as_str(payload.get("clone_url"))
        if not clone_url and base_url:
            clone_url = f"{base_url.rstrip('/')}/{owner}/{name}.git"
        if not clone_url:
            raise ValueError(f"no clone url for {owner}/{name}")

        return cls(
            owner=owner,
            name=name,
            clone_url=clone_url,
            default_branch=_as_str(payload.get("default_branch")),
            description=_as_str(payload.get("description")),
            html_url=_as_str(payload.get("html_url")),
            size_kb=_as_int(payload.get("size")),
            stars=_as_int(payload.get("stars_count")),
            empty=_as_bool(payload.get("empty")),
            fork=_as_bool(payload.get("fork")),
            archived=_as_bool(payload.get("archived")),
            mirror=_as_bool(payload.get("mirror")),
            private=_as_bool(payload.get("private")),
            template=_as_bool(payload.get("template")),
            has_wiki=_as_bool(payload.get("has_wiki")),
            updated_at=_as_str(payload.get("updated_at")),
            remote_id=_as_int(payload.get("id")),
        )

    def to_dict(self) -> dict[str, Any]:
        return {
            "full_name": self.full_name,
            "owner": self.owner,
            "name": self.name,
            "clone_url": self.clone_url,
            "default_branch": self.default_branch,
            "description": self.description,
            "html_url": self.html_url,
            "size_kb": self.size_kb,
            "stars": self.stars,
            "empty": self.empty,
            "fork": self.fork,
            "archived": self.archived,
            "mirror": self.mirror,
            "private": self.private,
            "template": self.template,
            "has_wiki": self.has_wiki,
            "updated_at": self.updated_at,
            "partial": self.partial,
        }


@dataclass(frozen=True)
class Outcome:
    """Result of syncing one repository."""

    repo: Repo
    status: Status
    detail: str = ""
    path: str = ""
    duration: float = 0.0
    commits_ahead: int = 0
    head: str = ""
    attempts: int = 1
    wiki: str = ""  # wiki sub-result, when --include-wiki is on

    @property
    def ok(self) -> bool:
        return self.status.ok

    def to_dict(self) -> dict[str, Any]:
        blob: dict[str, Any] = {
            "repo": self.repo.full_name,
            "status": self.status.value,
            "path": self.path,
            "duration": round(self.duration, 3),
            "attempts": self.attempts,
        }
        if self.detail:
            blob["detail"] = self.detail
        if self.commits_ahead:
            blob["commits_ahead"] = self.commits_ahead
        if self.head:
            blob["head"] = self.head
        if self.wiki:
            blob["wiki"] = self.wiki
        return blob


@dataclass(frozen=True)
class Plan:
    """Discovery output split into what we will and will not touch."""

    selected: tuple[Repo, ...] = ()
    excluded: tuple[tuple[Repo, str], ...] = ()  # (repo, reason)
    discovered: int = 0

    def reasons(self) -> dict[str, int]:
        tally: dict[str, int] = {}
        for _, reason in self.excluded:
            tally[reason] = tally.get(reason, 0) + 1
        return dict(sorted(tally.items()))


@dataclass
class RunReport:
    """Aggregate result of a whole sync."""

    forge: ForgeInfo
    output: str = ""
    started: str = ""
    finished: str = ""
    duration: float = 0.0
    discovered: int = 0
    planned: int = 0
    outcomes: list[Outcome] = field(default_factory=list)
    excluded: dict[str, int] = field(default_factory=dict)
    pruned: list[str] = field(default_factory=list)
    interrupted: bool = False
    dry_run: bool = False

    def counts(self) -> dict[str, int]:
        tally = {status.value: 0 for status in Status}
        for outcome in self.outcomes:
            tally[outcome.status.value] += 1
        return {key: value for key, value in tally.items() if value}

    @property
    def failures(self) -> list[Outcome]:
        return [o for o in self.outcomes if not o.ok]

    @property
    def bytes_synced(self) -> int:
        return sum(o.repo.size_kb for o in self.outcomes if o.status.touched_disk) * 1024

    @property
    def success(self) -> bool:
        return not self.failures and not self.interrupted

    def to_dict(self) -> dict[str, Any]:
        return {
            "draupnir": _version(),
            "forge": {
                "base_url": self.forge.base_url,
                "kind": self.forge.kind,
                "version": self.forge.version,
                "api": self.forge.api,
            },
            "output": self.output,
            "started": self.started,
            "finished": self.finished,
            "duration": round(self.duration, 3),
            "dry_run": self.dry_run,
            "interrupted": self.interrupted,
            "discovered": self.discovered,
            "planned": self.planned,
            "excluded": self.excluded,
            "counts": self.counts(),
            "pruned": self.pruned,
            "repos": [o.to_dict() for o in self.outcomes],
        }


def _version() -> str:
    from draupnir import __version__

    return __version__


def dedupe(repos: Iterable[Repo]) -> list[Repo]:
    """Drop duplicates by full_name, keeping the richest record.

    Paginated endpoints can repeat entries when the remote reorders between
    requests; HTML fallback rows are also thinner than API rows.
    """
    best: dict[str, Repo] = {}
    for repo in repos:
        key = repo.full_name.lower()
        current = best.get(key)
        if current is None or (current.partial and not repo.partial):
            best[key] = repo
    return list(best.values())


def merge_partial(repo: Repo, known: Repo) -> Repo:
    """Overlay API metadata onto an HTML-discovered stub."""
    if not repo.partial:
        return repo
    return replace(known, partial=False)
