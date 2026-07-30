"""Orchestration: discover -> filter -> plan -> execute.

This is the public entry point for embedding draupnir in another tool:

    from draupnir import MirrorConfig, MirrorEngine

    engine = MirrorEngine(MirrorConfig(base_url="https://git.example.org",
                                       output=Path("/srv/mirror")))
    report = engine.run()

The engine emits typed events, so a caller can render its own UI (or none)
without the library ever writing to stdout itself.

Concurrency notes: work is dispatched through a thread pool and collected with
as_completed, so a worker raising does not vanish silently -- the classic
`executor.map()` mistake, where exceptions surface only if the result iterator
is consumed. SIGINT flips a shared Event: in-flight git processes are killed,
queued repos are cancelled, and the partial report is still returned.
"""

from __future__ import annotations

import fnmatch
import re
import signal
import threading
import time
from collections.abc import Iterable, Sequence
from concurrent.futures import Future, ThreadPoolExecutor, as_completed
from dataclasses import dataclass
from datetime import datetime, timezone
from pathlib import Path
from typing import Callable

from .errors import ConfigError, DiscoveryError, Interrupted
from .forge import ForgeClient, normalise_base_url
from .gitcmd import GitRunner, git_version
from .http import DEFAULT_USER_AGENT, HttpClient
from .models import ForgeInfo, Outcome, Plan, Repo, RunReport, Status
from .naming import LAYOUTS, repo_path
from .state import OutputLock, StateStore, no_lock
from .sync import RepoSyncer, SyncOptions

__all__ = ["Event", "MirrorConfig", "MirrorEngine", "compile_patterns"]

MODES = ("worktree", "mirror")


@dataclass(frozen=True)
class Event:
    """Progress notification handed to the caller's listener."""

    kind: str  # probe | discovered | planned | repo_start | repo_done | pruned | done
    message: str = ""
    repo: Repo | None = None
    outcome: Outcome | None = None
    index: int = 0
    total: int = 0
    payload: object = None


Listener = Callable[[Event], None]


@dataclass
class MirrorConfig:
    """Every knob, in one place. Defaults are the safe/quiet ones."""

    base_url: str
    output: Path

    # transport
    token: str = ""
    timeout: float = 30.0
    git_timeout: float = 1800.0
    http_retries: int = 4
    git_retries: int = 2
    verify_tls: bool = True
    user_agent: str = DEFAULT_USER_AGENT
    allow_html_fallback: bool = True

    # selection
    owners: tuple[str, ...] = ()
    include: tuple[str, ...] = ()  # regex on owner/name
    exclude: tuple[str, ...] = ()
    match: tuple[str, ...] = ()  # glob on owner/name
    skip_forks: bool = False
    skip_archived: bool = False
    skip_mirrors: bool = False
    skip_templates: bool = False
    min_stars: int = 0
    max_size_kb: int = 0  # 0 -> unlimited
    limit: int = 0
    include_private: bool = False

    # execution
    jobs: int = 4
    mode: str = "worktree"
    layout: str = "owner"
    depth: int = 0
    include_wiki: bool = False
    clone_empty: bool = False
    lfs: bool = False
    force: bool = False
    single_branch: bool = False
    dry_run: bool = False
    refresh_all: bool = False  # ignore the incremental cache
    use_state: bool = True
    use_lock: bool = True
    prune_deleted: bool = False
    git_binary: str = "git"

    def validate(self) -> MirrorConfig:
        if self.mode not in MODES:
            raise ConfigError(f"mode must be one of {MODES}, got {self.mode!r}")
        if self.layout not in LAYOUTS:
            raise ConfigError(f"layout must be one of {LAYOUTS}, got {self.layout!r}")
        if self.jobs < 1:
            raise ConfigError("jobs must be >= 1")
        if self.depth < 0:
            raise ConfigError("depth must be >= 0")
        if self.timeout <= 0 or self.git_timeout <= 0:
            raise ConfigError("timeouts must be > 0")
        for pattern in (*self.include, *self.exclude):
            try:
                re.compile(pattern)
            except re.error as exc:
                raise ConfigError(f"invalid regex {pattern!r}: {exc}") from exc
        object.__setattr__(self, "base_url", normalise_base_url(self.base_url))
        return self


def compile_patterns(patterns: Sequence[str]) -> list[re.Pattern[str]]:
    return [re.compile(pattern, re.IGNORECASE) for pattern in patterns]


class MirrorEngine:
    """Runs one mirroring pass over a forge."""

    def __init__(self, config: MirrorConfig, *, listener: Listener | None = None) -> None:
        self.config = config.validate()
        self.listener = listener or (lambda event: None)
        self.cancel = threading.Event()
        self.http = HttpClient(
            token=config.token,
            timeout=config.timeout,
            retries=config.http_retries,
            verify_tls=config.verify_tls,
            user_agent=config.user_agent,
        )
        self.forge = ForgeClient(config.base_url, self.http)
        self.git = GitRunner(
            binary=config.git_binary,
            timeout=config.git_timeout,
            token=config.token,
            lfs=config.lfs,
            verify_tls=config.verify_tls,
            cancel=self.cancel,
        )
        self.state = StateStore(config.output)
        self._include = compile_patterns(config.include)
        self._exclude = compile_patterns(config.exclude)

    # -- public ----------------------------------------------------------

    def run(self) -> RunReport:
        """Full pass. Returns a report even when interrupted."""
        config = self.config
        started_at = time.monotonic()
        started = _stamp()

        git_banner = git_version(config.git_binary)  # fail fast if git is absent
        self._emit(Event("probe", message=git_banner))

        output = Path(config.output).expanduser()
        output.mkdir(parents=True, exist_ok=True)

        lock = OutputLock(output) if config.use_lock and not config.dry_run else no_lock()
        report = RunReport(
            forge=ForgeInfo(base_url=config.base_url),
            output=str(output),
            started=started,
            dry_run=config.dry_run,
        )

        with lock:
            if config.use_state:
                self.state.load()
            repos, info = self._discover()
            report.forge = info
            report.discovered = len(repos)
            self.state.set_forge(config.base_url)

            plan = self._plan(repos, output)
            report.planned = len(plan.selected)
            report.excluded = plan.reasons()
            self._emit(
                Event(
                    "planned",
                    message=f"{len(plan.selected)} selected, {len(plan.excluded)} filtered out",
                    payload=plan,
                    total=len(plan.selected),
                )
            )

            with _SignalGuard(self.cancel):
                if config.dry_run:
                    report.outcomes = [
                        Outcome(
                            repo=repo,
                            status=Status.PLANNED,
                            path=str(self._destination(repo, output)),
                            detail="dry run",
                        )
                        for repo in plan.selected
                    ]
                else:
                    report.outcomes = self._execute(plan.selected, output)

            if config.prune_deleted and not config.dry_run and not self.cancel.is_set():
                report.pruned = self._prune(repos, output)

            if config.use_state and not config.dry_run:
                self.state.save()

        report.interrupted = self.cancel.is_set()
        report.finished = _stamp()
        report.duration = time.monotonic() - started_at
        self._emit(Event("done", payload=report))
        return report

    def discover(self) -> tuple[list[Repo], ForgeInfo]:
        """Enumerate without touching the filesystem."""
        return self._discover()

    def plan(self, repos: Iterable[Repo] | None = None) -> Plan:
        output = Path(self.config.output).expanduser()
        if repos is None:
            repos, _ = self._discover()
        if self.config.use_state:
            self.state.load()
        return self._plan(list(repos), output)

    # -- stages ----------------------------------------------------------

    def _discover(self) -> tuple[list[Repo], ForgeInfo]:
        self._emit(Event("probe", message=f"probing {self.config.base_url}"))
        repos, info = self.forge.discover(
            include_private=self.config.include_private,
            allow_html_fallback=self.config.allow_html_fallback,
            owners=self.config.owners,
        )
        source = "api" if info.api else "html"
        self._emit(
            Event(
                "discovered",
                message=f"{len(repos)} repositories via {source}",
                total=len(repos),
                payload=info,
            )
        )
        if not repos:
            raise DiscoveryError(f"no repositories found at {self.config.base_url}")
        return repos, info

    def _plan(self, repos: list[Repo], output: Path) -> Plan:
        selected: list[Repo] = []
        excluded: list[tuple[Repo, str]] = []

        for repo in repos:
            reason = self._reject(repo)
            if reason:
                excluded.append((repo, reason))
                continue
            if not self.config.refresh_all and self.config.use_state:
                destination = self._destination(repo, output)
                if destination and self.state.is_current(repo, destination):
                    excluded.append((repo, "up to date"))
                    continue
            selected.append(repo)

        if self.config.limit > 0 and len(selected) > self.config.limit:
            for repo in selected[self.config.limit :]:
                excluded.append((repo, "over --limit"))
            selected = selected[: self.config.limit]

        # Biggest first: with N workers this keeps the long tail off the end of
        # the run, where it would otherwise idle every other worker.
        selected.sort(key=lambda r: (-r.size_kb, r.full_name.lower()))
        return Plan(selected=tuple(selected), excluded=tuple(excluded), discovered=len(repos))

    def _reject(self, repo: Repo) -> str:
        config = self.config
        name = repo.full_name

        if config.owners and repo.owner.lower() not in {o.lower() for o in config.owners}:
            return "owner not selected"
        if self._include and not any(p.search(name) for p in self._include):
            return "no --include match"
        if any(p.search(name) for p in self._exclude):
            return "--exclude match"
        if config.match and not any(
            fnmatch.fnmatch(name.lower(), pattern.lower()) for pattern in config.match
        ):
            return "no --match match"

        # Flag-based filters are meaningless on HTML-discovered stubs (every
        # flag defaults to False there), so never drop a partial repo on them.
        if not repo.partial:
            if config.skip_forks and repo.fork:
                return "fork"
            if config.skip_archived and repo.archived:
                return "archived"
            if config.skip_mirrors and repo.mirror:
                return "mirror"
            if config.skip_templates and repo.template:
                return "template"
            if config.min_stars and repo.stars < config.min_stars:
                return "below --min-stars"
            if config.max_size_kb and repo.size_kb > config.max_size_kb:
                return "over --max-size"
            if repo.empty and not config.clone_empty:
                return "empty"
        return ""

    def _execute(self, repos: Sequence[Repo], output: Path) -> list[Outcome]:
        if not repos:
            return []

        options = SyncOptions(
            output=output,
            mode=self.config.mode,
            layout=self.config.layout,
            depth=self.config.depth,
            retries=self.config.git_retries,
            include_wiki=self.config.include_wiki,
            clone_empty=self.config.clone_empty,
            force=self.config.force,
            single_branch=self.config.single_branch,
        )
        syncer = RepoSyncer(options, self.git)
        outcomes: list[Outcome] = []
        total = len(repos)
        done = 0
        workers = max(1, min(self.config.jobs, total))

        executor = ThreadPoolExecutor(max_workers=workers, thread_name_prefix="draupnir")
        futures: dict[Future[Outcome], Repo] = {}
        try:
            for repo in repos:
                futures[executor.submit(self._sync_one, syncer, repo)] = repo

            for future in as_completed(futures):
                repo = futures[future]
                done += 1
                try:
                    outcome = future.result()
                except Interrupted:
                    outcome = Outcome(repo=repo, status=Status.SKIPPED, detail="cancelled")
                except Exception as exc:  # a worker must never kill the run
                    outcome = Outcome(
                        repo=repo,
                        status=Status.FAILED,
                        detail=f"{type(exc).__name__}: {exc}"[:300],
                    )
                outcomes.append(outcome)
                if self.config.use_state:
                    self.state.remember(outcome)
                self._emit(
                    Event(
                        "repo_done",
                        repo=repo,
                        outcome=outcome,
                        index=done,
                        total=total,
                    )
                )
                # Checkpoint periodically: a run killed at repo 90/102 keeps
                # its progress instead of re-cloning everything next time.
                if self.config.use_state and done % 25 == 0:
                    self.state.save()
                if self.cancel.is_set():
                    for pending in futures:
                        pending.cancel()
        finally:
            executor.shutdown(wait=not self.cancel.is_set(), cancel_futures=True)
        return outcomes

    def _sync_one(self, syncer: RepoSyncer, repo: Repo) -> Outcome:
        if self.cancel.is_set():
            raise Interrupted(repo.full_name)
        self._emit(Event("repo_start", repo=repo))
        return syncer.sync(repo)

    def _prune(self, remote: Sequence[Repo], output: Path) -> list[str]:
        """Report local repos the forge no longer lists.

        Deliberately conservative: only directories draupnir itself recorded in
        state are considered, and they are moved to `.draupnir-pruned/`, never
        deleted. An operator can inspect and remove them by hand.
        """
        live = {repo.full_name for repo in remote}
        pruned: list[str] = []
        graveyard = output / ".draupnir-pruned"

        for full_name, path in sorted(self.state.known_paths().items()):
            if full_name in live:
                continue
            source = Path(path)
            if not source.exists():
                self.state.forget(full_name)
                continue
            try:
                destination = graveyard / full_name.replace("/", "__")
                destination.parent.mkdir(parents=True, exist_ok=True)
                if destination.exists():
                    destination = destination.with_name(f"{destination.name}.{int(time.time())}")
                source.replace(destination)
            except OSError:
                continue
            self.state.forget(full_name)
            pruned.append(full_name)
            self._emit(Event("pruned", message=full_name))
        return pruned

    # -- helpers ---------------------------------------------------------

    def _destination(self, repo: Repo, output: Path) -> Path | None:
        try:
            return repo_path(output, repo.owner, repo.name, layout=self.config.layout)
        except Exception:
            return None

    def _emit(self, event: Event) -> None:
        try:
            self.listener(event)
        except Exception:  # a broken UI must not abort a mirror
            pass


class _SignalGuard:
    """Turn SIGINT/SIGTERM into a cooperative cancel, twice means abort."""

    def __init__(self, cancel: threading.Event) -> None:
        self.cancel = cancel
        self.previous: dict[int, object] = {}

    def __enter__(self) -> _SignalGuard:
        if threading.current_thread() is not threading.main_thread():
            return self  # signal handlers are main-thread only
        for signum in (signal.SIGINT, signal.SIGTERM):
            try:
                self.previous[signum] = signal.getsignal(signum)
                signal.signal(signum, self._handle)
            except (ValueError, OSError):  # pragma: no cover
                continue
        return self

    def _handle(self, signum: int, frame: object) -> None:
        if self.cancel.is_set():
            # Second hit: restore default handling so the next one really kills.
            handler = self.previous.get(signum, signal.SIG_DFL)
            signal.signal(signum, handler)  # type: ignore[arg-type]
            raise KeyboardInterrupt
        self.cancel.set()

    def __exit__(self, *_exc: object) -> None:
        for signum, handler in self.previous.items():
            try:
                signal.signal(signum, handler)  # type: ignore[arg-type]
            except (ValueError, OSError):  # pragma: no cover
                continue


def _stamp() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()
