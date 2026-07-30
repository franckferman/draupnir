"""Local mirror inspection: `draupnir status` and `draupnir verify`.

Answers two questions the sync path cannot:
  * what is actually on disk, versus what state claims?
  * is each repository structurally intact (not truncated by a killed clone)?
"""

from __future__ import annotations

import os
from collections.abc import Iterator
from dataclasses import dataclass, field
from pathlib import Path
from typing import Any

from .gitcmd import GitRunner
from .naming import LAYOUTS
from .state import StateStore

__all__ = ["AuditReport", "LocalRepo", "scan_local", "verify_local"]

_SKIP_DIRS = {".draupnir-pruned", ".git"}


@dataclass
class LocalRepo:
    path: Path
    name: str  # owner/repo as derived from the tree
    bare: bool = False
    head: str = ""
    branch: str = ""
    dirty: bool = False
    refs: int = 0
    size_bytes: int = 0
    intact: bool | None = None  # None -> not verified
    problem: str = ""
    tracked: bool = False  # present in the state file

    def to_dict(self) -> dict[str, Any]:
        blob = {
            "repo": self.name,
            "path": str(self.path),
            "bare": self.bare,
            "head": self.head,
            "branch": self.branch,
            "dirty": self.dirty,
            "refs": self.refs,
            "size_bytes": self.size_bytes,
            "tracked": self.tracked,
        }
        if self.intact is not None:
            blob["intact"] = self.intact
        if self.problem:
            blob["problem"] = self.problem
        return blob


@dataclass
class AuditReport:
    output: str
    repos: list[LocalRepo] = field(default_factory=list)
    untracked: list[str] = field(default_factory=list)  # on disk, not in state
    missing: list[str] = field(default_factory=list)  # in state, not on disk
    verified: bool = False

    @property
    def broken(self) -> list[LocalRepo]:
        return [repo for repo in self.repos if repo.intact is False or repo.problem]

    @property
    def dirty(self) -> list[LocalRepo]:
        return [repo for repo in self.repos if repo.dirty]

    @property
    def total_bytes(self) -> int:
        return sum(repo.size_bytes for repo in self.repos)

    def to_dict(self) -> dict[str, Any]:
        return {
            "output": self.output,
            "verified": self.verified,
            "count": len(self.repos),
            "total_bytes": self.total_bytes,
            "broken": [r.name for r in self.broken],
            "dirty": [r.name for r in self.dirty],
            "untracked": self.untracked,
            "missing": self.missing,
            "repos": [repo.to_dict() for repo in self.repos],
        }


def _looks_like_repo(path: Path) -> tuple[bool, bool]:
    """(is_repo, is_bare)."""
    if (path / ".git").exists():
        return True, False
    if (path / "HEAD").is_file() and (path / "objects").is_dir() and (path / "refs").is_dir():
        return True, True
    return False, False


def _walk(root: Path, *, max_depth: int = 3) -> Iterator[Path]:
    """Find repo directories without descending into their internals."""
    stack: list[tuple[Path, int]] = [(root, 0)]
    while stack:
        current, depth = stack.pop()
        try:
            entries = sorted(p for p in current.iterdir() if p.is_dir())
        except OSError:
            continue
        for entry in entries:
            # Staging dirs are named "<repo>.draupnir-tmp-<pid>-<hex>", so the
            # marker sits mid-name: a SIGKILLed clone must not be inventoried
            # as a real repository.
            if entry.name in _SKIP_DIRS or ".draupnir-tmp-" in entry.name:
                continue
            is_repo, _ = _looks_like_repo(entry)
            if is_repo:
                yield entry
            elif depth + 1 < max_depth:
                stack.append((entry, depth + 1))


def _dir_size(path: Path) -> int:
    total = 0
    for current, dirs, files in os.walk(path, onerror=lambda _e: None):
        dirs[:] = [d for d in dirs if not d.startswith(".draupnir-tmp-")]
        for name in files:
            try:
                total += os.lstat(os.path.join(current, name)).st_size
            except OSError:
                continue
    return total


def scan_local(
    output: Path,
    runner: GitRunner,
    *,
    layout: str = "owner",
    measure: bool = True,
) -> AuditReport:
    """Inventory the mirror tree and cross-check it against the state file."""
    output = Path(output).expanduser()
    report = AuditReport(output=str(output))
    if layout not in LAYOUTS:  # defensive: caller validated, but keep it honest
        layout = "owner"

    state = StateStore(output).load()
    tracked = state.known_paths()
    tracked_by_path = {str(Path(p)): name for name, p in tracked.items()}

    if not output.exists():
        report.missing = sorted(tracked)
        return report

    for path in _walk(output):
        _, bare = _looks_like_repo(path)
        try:
            relative = path.relative_to(output)
        except ValueError:  # pragma: no cover
            relative = Path(path.name)
        name = "/".join(relative.parts) if layout == "owner" else path.name.replace("__", "/", 1)

        local = LocalRepo(path=path, name=name, bare=bare)
        local.tracked = str(path) in tracked_by_path or name in tracked
        local.head = _git_out(runner, path, ["rev-parse", "HEAD"])
        local.branch = _git_out(runner, path, ["symbolic-ref", "--short", "HEAD"])
        refs = _git_out(runner, path, ["for-each-ref", "--format=%(refname)"])
        local.refs = len(refs.splitlines()) if refs else 0
        if not bare:
            local.dirty = bool(_git_out(runner, path, ["status", "--porcelain"]))
        if measure:
            local.size_bytes = _dir_size(path)
        if not local.head and local.refs == 0:
            local.problem = "no refs (empty or interrupted clone)"
        report.repos.append(local)

    report.repos.sort(key=lambda r: r.name.lower())
    on_disk = {repo.name for repo in report.repos}
    report.untracked = sorted(name for name in on_disk if name not in tracked)
    report.missing = sorted(name for name in tracked if name not in on_disk)
    return report


def verify_local(report: AuditReport, runner: GitRunner, *, deep: bool = False) -> AuditReport:
    """Run git's own integrity check over every repo in the report."""
    for repo in report.repos:
        args = ["fsck", "--no-progress", "--no-dangling"]
        if not deep:
            # Connectivity-only skips object re-hashing: seconds instead of
            # minutes per repo, and still catches a truncated/missing object.
            args.append("--connectivity-only")
        result = runner.run(args, cwd=repo.path, check=False, timeout=900)
        repo.intact = result.returncode == 0
        if not repo.intact:
            first = [line for line in result.stderr.splitlines() if line.strip()]
            repo.problem = (first[0] if first else f"fsck exit {result.returncode}")[:200]
    report.verified = True
    return report


def _git_out(runner: GitRunner, cwd: Path, args: list[str]) -> str:
    result = runner.run(args, cwd=cwd, check=False, timeout=120)
    return result.out if result.ok else ""
