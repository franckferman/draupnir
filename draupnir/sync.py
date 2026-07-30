"""Per-repository clone / update.

Reliability rules enforced here:

  * **Atomic clones.** A fresh clone lands in a sibling temp directory and is
    renamed into place only once git succeeded. Ctrl-C or a dead network can
    never leave a half-populated repo that later runs mistake for a good one.
  * **No data loss.** A dirty working tree is reported, never reset. A path
    occupied by something that is not our repository is reported as a conflict
    and left untouched. Nothing is deleted without an explicit flag.
  * **Retries where they help.** Network verbs are retried with backoff; local
    failures (bad path, corrupt repo) are not, because retrying is pointless.
  * **Truthful status.** "updated" means refs actually moved, and the commit
    delta is measured, not guessed.
"""

from __future__ import annotations

import os
import random
import shutil
import time
from dataclasses import dataclass
from pathlib import Path

from .errors import GitError, UnsafeNameError
from .gitcmd import GitResult, GitRunner
from .models import Outcome, Repo, Status
from .naming import repo_path

__all__ = ["RepoSyncer", "SyncOptions"]

_TRANSIENT = (
    "could not resolve host",
    "connection reset",
    "connection timed out",
    "connection refused",
    "operation timed out",
    "early eof",
    "rpc failed",
    "the remote end hung up",
    "unexpected disconnect",
    "http/2 stream",
    "gnutls_handshake",
    "ssl_read",
    "tls packet",
    "500 internal server error",
    "502 bad gateway",
    "503 service unavailable",
    "504 gateway",
    "429 too many requests",
    "failed to connect",
    "temporary failure in name resolution",
)


@dataclass
class SyncOptions:
    """Everything the syncer needs that is not the repo itself."""

    output: Path
    mode: str = "worktree"  # worktree | mirror
    layout: str = "owner"
    depth: int = 0  # 0 -> full history
    retries: int = 2
    retry_backoff: float = 2.0
    include_wiki: bool = False
    clone_empty: bool = False
    force: bool = False  # allow hard reset of a dirty tree
    single_branch: bool = False

    @property
    def bare(self) -> bool:
        return self.mode == "mirror"


class RepoSyncer:
    """Clones or updates one repository at a time. Thread-safe."""

    def __init__(self, options: SyncOptions, runner: GitRunner) -> None:
        self.options = options
        self.git = runner

    # -- entry point -----------------------------------------------------

    def sync(self, repo: Repo) -> Outcome:
        started = time.monotonic()
        try:
            destination = repo_path(
                self.options.output, repo.owner, repo.name, layout=self.options.layout
            )
        except UnsafeNameError as exc:
            return Outcome(repo=repo, status=Status.FAILED, detail=str(exc))

        try:
            outcome = self._sync_to(repo, destination)
        except GitError as exc:
            outcome = Outcome(
                repo=repo,
                status=Status.FAILED,
                detail=_detail(exc),
                path=str(destination),
            )
        except OSError as exc:
            outcome = Outcome(
                repo=repo, status=Status.FAILED, detail=str(exc), path=str(destination)
            )

        wiki_note = ""
        if self.options.include_wiki and repo.has_wiki and outcome.status.ok:
            wiki_note = self._sync_wiki(repo, destination)

        return Outcome(
            repo=outcome.repo,
            status=outcome.status,
            detail=outcome.detail,
            path=outcome.path or str(destination),
            duration=time.monotonic() - started,
            commits_ahead=outcome.commits_ahead,
            head=outcome.head,
            attempts=outcome.attempts,
            wiki=wiki_note,
        )

    # -- dispatch --------------------------------------------------------

    def _sync_to(self, repo: Repo, destination: Path) -> Outcome:
        if self._is_our_repo(destination):
            return self._update(repo, destination)

        if destination.exists():
            if _is_empty_dir(destination):
                destination.rmdir()  # leftover from an aborted run
            else:
                return Outcome(
                    repo=repo,
                    status=Status.CONFLICT,
                    detail="path exists and is not a draupnir repository",
                    path=str(destination),
                )

        if repo.empty and not self.options.clone_empty:
            return Outcome(repo=repo, status=Status.SKIPPED, detail="empty repository")

        return self._clone(repo, destination)

    def _is_our_repo(self, destination: Path) -> bool:
        """True when destination is a git repo of the expected shape."""
        if not destination.is_dir():
            return False
        if self.options.bare:
            return (destination / "HEAD").is_file() and (destination / "objects").is_dir()
        return (destination / ".git").exists()

    # -- clone -----------------------------------------------------------

    def _clone(self, repo: Repo, destination: Path) -> Outcome:
        destination.parent.mkdir(parents=True, exist_ok=True)
        args = ["clone", "--quiet"]
        if self.options.bare:
            args.append("--mirror")
        else:
            args.append("--no-checkout" if repo.empty else "--no-single-branch")
            if self.options.single_branch:
                args = [a for a in args if a != "--no-single-branch"] + ["--single-branch"]
        if self.options.depth > 0:
            args += ["--depth", str(self.options.depth)]
            if not self.options.bare:
                args.append("--no-tags")
        args += ["--", repo.clone_url]

        attempt = 0
        last: GitError | None = None
        while attempt <= self.options.retries:
            attempt += 1
            staging = _staging_dir(destination)
            try:
                self.git.run([*args, str(staging)], authenticated=True)
                # Rename is atomic on the same filesystem -> a repo directory
                # either does not exist or is complete. Never half-cloned.
                os.replace(str(staging), str(destination))
                head = self._head(destination)
                return Outcome(
                    repo=repo,
                    status=Status.CLONED,
                    path=str(destination),
                    head=head,
                    attempts=attempt,
                )
            except GitError as exc:
                _discard(staging)
                last = exc
                if not _is_transient(exc) or attempt > self.options.retries:
                    break
                self._sleep(attempt)
            except OSError as exc:
                _discard(staging)
                raise GitError(f"cannot place clone: {exc}", args=tuple(args)) from exc

        raise last or GitError("clone failed", args=tuple(args))

    # -- update ----------------------------------------------------------

    def _update(self, repo: Repo, destination: Path) -> Outcome:
        self._align_remote(repo, destination)
        before = self._head(destination)
        # Snapshot every ref, not just HEAD: on a bare mirror HEAD almost never
        # moves, so HEAD alone would report "unchanged" after a real fetch.
        before_refs = self._ref_snapshot(destination)

        attempt = 0
        last: GitError | None = None
        while attempt <= self.options.retries:
            attempt += 1
            try:
                if self.options.bare:
                    self.git.run(
                        ["remote", "update", "--prune"], cwd=destination, authenticated=True
                    )
                else:
                    fetch = ["fetch", "--quiet", "--prune", "--prune-tags", "origin"]
                    if self.options.depth > 0:
                        fetch += ["--depth", str(self.options.depth)]
                    else:
                        fetch.append("--tags")
                    self.git.run(fetch, cwd=destination, authenticated=True)
                break
            except GitError as exc:
                last = exc
                if not _is_transient(exc) or attempt > self.options.retries:
                    raise
                self._sleep(attempt)
        else:  # pragma: no cover - loop always breaks or raises
            raise last or GitError("fetch failed")

        detail = ""
        if not self.options.bare:
            detail = self._fast_forward(repo, destination)

        after = self._head(destination)
        after_refs = self._ref_snapshot(destination)

        if after_refs != before_refs or (after and before and after != before):
            ahead = self._count_between(destination, before, after) if before and after else 0
            return Outcome(
                repo=repo,
                status=Status.UPDATED,
                detail=detail,
                path=str(destination),
                commits_ahead=ahead,
                head=after,
                attempts=attempt,
            )
        return Outcome(
            repo=repo,
            status=Status.UNCHANGED,
            detail=detail,
            path=str(destination),
            head=after,
            attempts=attempt,
        )

    def _align_remote(self, repo: Repo, destination: Path) -> None:
        """Keep origin pointing at the current forge URL (host may have moved)."""
        current = self.git.run(
            ["remote", "get-url", "origin"], cwd=destination, check=False
        )
        if not current.ok:
            self.git.run(["remote", "add", "origin", repo.clone_url], cwd=destination, check=False)
            return
        if current.out and current.out != repo.clone_url:
            self.git.run(
                ["remote", "set-url", "origin", repo.clone_url], cwd=destination, check=False
            )

    def _fast_forward(self, repo: Repo, destination: Path) -> str:
        """Advance the checked-out branch when it is safe to do so."""
        if self._is_detached(destination):
            return "detached HEAD, left as-is"

        branch = self._current_branch(destination) or self._remote_default(destination, repo)
        if not branch:
            return "no branch to update"

        upstream = f"origin/{branch}"
        if not self._rev_parse(destination, upstream):
            return f"{upstream} missing, left as-is"

        if self._is_dirty(destination):
            if not self.options.force:
                # Refuse to clobber operator edits. This is a mirror, not a bulldozer.
                return "local changes, skipped merge"
            self.git.run(["reset", "--hard", upstream], cwd=destination)
            self.git.run(["clean", "-fdx"], cwd=destination, check=False)
            return "forced reset over local changes"

        merge = self.git.run(["merge", "--ff-only", upstream], cwd=destination, check=False)
        if merge.ok:
            return ""
        if self.options.force:
            self.git.run(["reset", "--hard", upstream], cwd=destination)
            return "history diverged, forced reset"
        return "history diverged, fast-forward refused"

    # -- wiki ------------------------------------------------------------

    def _sync_wiki(self, repo: Repo, destination: Path) -> str:
        """Mirror the wiki next to the repo. Absent wiki is not a failure."""
        target = destination.parent / f"{destination.name}.wiki"
        wiki_repo = Repo(
            owner=repo.owner,
            name=f"{repo.name}.wiki",
            clone_url=repo.wiki_clone_url,
            default_branch=repo.default_branch,
        )
        try:
            if self._is_our_repo(target):
                self._update(wiki_repo, target)
                return "updated"
            if target.exists():
                return "conflict"
            self._clone(wiki_repo, target)
            return "cloned"
        except GitError as exc:
            text = (exc.stderr or str(exc)).lower()
            if "not found" in text or "404" in text or "repository not found" in text:
                return "absent"
            return f"failed: {_detail(exc)[:80]}"

    # -- git helpers -----------------------------------------------------

    def _head(self, destination: Path) -> str:
        result = self.git.run(["rev-parse", "HEAD"], cwd=destination, check=False)
        return result.out if result.ok else ""

    def _rev_parse(self, destination: Path, ref: str) -> str:
        result = self.git.run(
            ["rev-parse", "--verify", "--quiet", ref], cwd=destination, check=False
        )
        return result.out if result.ok else ""

    def _is_detached(self, destination: Path) -> bool:
        result = self.git.run(
            ["symbolic-ref", "--quiet", "HEAD"], cwd=destination, check=False
        )
        return not result.ok

    def _current_branch(self, destination: Path) -> str:
        result = self.git.run(["symbolic-ref", "--short", "HEAD"], cwd=destination, check=False)
        return result.out if result.ok else ""

    def _remote_default(self, destination: Path, repo: Repo) -> str:
        result = self.git.run(
            ["symbolic-ref", "--short", "refs/remotes/origin/HEAD"], cwd=destination, check=False
        )
        if result.ok and "/" in result.out:
            return result.out.split("/", 1)[1]
        return repo.default_branch

    def _is_dirty(self, destination: Path) -> bool:
        result = self.git.run(["status", "--porcelain"], cwd=destination, check=False)
        return bool(result.out)

    def _count_between(self, destination: Path, old: str, new: str) -> int:
        result = self.git.run(
            ["rev-list", "--count", f"{old}..{new}"], cwd=destination, check=False
        )
        try:
            return int(result.out)
        except (TypeError, ValueError):
            return 0

    def _ref_snapshot(self, destination: Path) -> str:
        """All refs + their object ids -> cheap fingerprint of the repo state."""
        result: GitResult = self.git.run(
            ["for-each-ref", "--format=%(objectname) %(refname)"], cwd=destination, check=False
        )
        return result.out if result.ok else ""

    def _sleep(self, attempt: int) -> None:
        window = self.options.retry_backoff * (2 ** (attempt - 1))
        time.sleep(min(window, 30.0) * (0.5 + random.random() / 2))


# -- module helpers ------------------------------------------------------


def _staging_dir(destination: Path) -> Path:
    """Sibling temp path -> rename into place stays on one filesystem."""
    suffix = f".draupnir-tmp-{os.getpid()}-{random.randrange(1 << 30):08x}"
    return destination.with_name(f"{destination.name}{suffix}")


def _discard(path: Path) -> None:
    shutil.rmtree(path, ignore_errors=True)


def _is_empty_dir(path: Path) -> bool:
    try:
        next(path.iterdir())
    except StopIteration:
        return True
    except OSError:
        return False
    return False


def _is_transient(exc: GitError) -> bool:
    if exc.timed_out:
        return True
    blob = f"{exc} {exc.stderr}".lower()
    return any(marker in blob for marker in _TRANSIENT)


def _detail(exc: GitError) -> str:
    text = str(exc)
    if exc.stderr and exc.stderr.strip() not in text:
        first = exc.stderr.strip().splitlines()
        if first:
            text = f"{text} | {first[-1][:160]}"
    return " ".join(text.split())[:300]
