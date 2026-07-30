"""Run state: incremental sync + a lock against concurrent runs.

The state file lives at the root of the output tree. It records, per repo, the
`updated_at` the forge reported the last time we synced it successfully. On the
next run any repo whose `updated_at` is unchanged -- and whose directory is
still present -- is skipped without spawning git at all. On a 100-repo forge
that turns a multi-minute re-sync into a couple of seconds.

Writes are atomic (temp file + os.replace) so a crash mid-write leaves the
previous state intact rather than a truncated JSON file.
"""

from __future__ import annotations

import json
import os
import tempfile
import threading
import time
from dataclasses import asdict, dataclass, field
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

from .errors import LockError
from .models import Outcome, Repo, Status

__all__ = ["LOCK_FILENAME", "STATE_FILENAME", "OutputLock", "RepoRecord", "StateStore"]

STATE_FILENAME = ".draupnir-state.json"
LOCK_FILENAME = ".draupnir.lock"
STATE_VERSION = 1


def _now() -> str:
    return datetime.now(timezone.utc).replace(microsecond=0).isoformat()


@dataclass
class RepoRecord:
    """What we remember about one repository between runs."""

    path: str = ""
    updated_at: str = ""  # forge-reported mtime at last successful sync
    synced_at: str = ""
    head: str = ""
    status: str = ""
    size_kb: int = 0

    @classmethod
    def from_dict(cls, blob: Any) -> RepoRecord:
        if not isinstance(blob, dict):
            return cls()
        allowed = set(cls.__dataclass_fields__)
        return cls(**{k: v for k, v in blob.items() if k in allowed})


class StateStore:
    """Load/save the state file. All mutations are mutex-guarded."""

    def __init__(self, output: Path, *, filename: str = STATE_FILENAME) -> None:
        self.output = Path(output)
        self.path = self.output / filename
        self.repos: dict[str, RepoRecord] = {}
        self.forge: str = ""
        self.last_run: str = ""
        self._lock = threading.Lock()
        self._dirty = False

    # -- io --------------------------------------------------------------

    def load(self) -> StateStore:
        """Read state. A corrupt or unreadable file degrades to empty state.

        Losing the cache costs time, never correctness -- every repo is simply
        re-checked -- so a broken file must not abort the run.
        """
        try:
            raw = self.path.read_text(encoding="utf-8")
        except (FileNotFoundError, NotADirectoryError):
            return self
        except OSError:
            return self

        try:
            blob = json.loads(raw)
        except json.JSONDecodeError:
            self._quarantine()
            return self

        if not isinstance(blob, dict) or blob.get("version") != STATE_VERSION:
            return self

        self.forge = str(blob.get("forge") or "")
        self.last_run = str(blob.get("last_run") or "")
        repos = blob.get("repos")
        if isinstance(repos, dict):
            self.repos = {
                str(key): RepoRecord.from_dict(value)
                for key, value in repos.items()
                if isinstance(key, str)
            }
        return self

    def save(self) -> None:
        """Atomically persist state. Never raises -- state is a cache."""
        with self._lock:
            if not self._dirty and self.path.exists():
                return
            payload = {
                "version": STATE_VERSION,
                "forge": self.forge,
                "last_run": _now(),
                "repos": {key: asdict(record) for key, record in sorted(self.repos.items())},
            }
            try:
                self.output.mkdir(parents=True, exist_ok=True)
                handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed below
                    "w",
                    encoding="utf-8",
                    dir=str(self.output),
                    prefix=".draupnir-state-",
                    suffix=".tmp",
                    delete=False,
                )
                with handle as stream:
                    json.dump(payload, stream, indent=2, sort_keys=False)
                    stream.write("\n")
                    stream.flush()
                    os.fsync(stream.fileno())
                os.replace(handle.name, str(self.path))
                self._dirty = False
            except OSError:
                _unlink(getattr(locals().get("handle"), "name", ""))

    def _quarantine(self) -> None:
        """Keep a corrupt state file around for forensics instead of deleting."""
        try:
            self.path.replace(self.path.with_suffix(f".corrupt-{int(time.time())}"))
        except OSError:
            pass

    # -- queries ---------------------------------------------------------

    def record_for(self, repo: Repo) -> RepoRecord | None:
        return self.repos.get(repo.full_name)

    def is_current(self, repo: Repo, destination: Path) -> bool:
        """True when a sync would be a no-op, cheaply provable.

        Requires all of: a prior successful sync, an unchanged forge timestamp,
        a timestamp that is actually present (repos discovered via the HTML
        fallback have none), and the directory still on disk.
        """
        record = self.repos.get(repo.full_name)
        if record is None or repo.partial:
            return False
        if not record.updated_at or not repo.updated_at:
            return False
        if record.updated_at != repo.updated_at:
            return False
        if record.status not in {Status.CLONED.value, Status.UPDATED.value, Status.UNCHANGED.value}:
            return False
        return destination.exists()

    def known_paths(self) -> dict[str, str]:
        return {name: record.path for name, record in self.repos.items() if record.path}

    # -- mutations -------------------------------------------------------

    def remember(self, outcome: Outcome) -> None:
        """Persist a successful sync. Failures leave the old record alone."""
        if outcome.status in {Status.FAILED, Status.CONFLICT, Status.PLANNED}:
            return
        with self._lock:
            previous = self.repos.get(outcome.repo.full_name)
            if outcome.status is Status.SKIPPED and previous is not None:
                return
            self.repos[outcome.repo.full_name] = RepoRecord(
                path=outcome.path,
                updated_at=outcome.repo.updated_at,
                synced_at=_now(),
                head=outcome.head or (previous.head if previous else ""),
                status=outcome.status.value,
                size_kb=outcome.repo.size_kb,
            )
            self._dirty = True

    def forget(self, full_name: str) -> None:
        with self._lock:
            if self.repos.pop(full_name, None) is not None:
                self._dirty = True

    def set_forge(self, url: str) -> None:
        with self._lock:
            if self.forge != url:
                self.forge = url
                self._dirty = True


class OutputLock:
    """Advisory lock so two runs cannot fight over one output tree.

    Uses O_EXCL creation rather than fcntl to stay portable, and detects a
    stale lock (holder no longer running) so a killed run does not wedge the
    directory forever.
    """

    def __init__(self, output: Path, *, filename: str = LOCK_FILENAME) -> None:
        self.path = Path(output) / filename
        self.acquired = False

    def acquire(self, *, steal_stale: bool = True) -> OutputLock:
        self.path.parent.mkdir(parents=True, exist_ok=True)
        try:
            self._create()
        except FileExistsError:
            holder = self._read()
            if steal_stale and not _pid_alive(holder.get("pid", 0)):
                _unlink(str(self.path))
                self._create()
            else:
                raise LockError(
                    f"{self.path} is held by pid {holder.get('pid', '?')} "
                    f"since {holder.get('since', '?')}; "
                    "wait for it to finish or remove the file"
                ) from None
        self.acquired = True
        return self

    def release(self) -> None:
        if self.acquired:
            _unlink(str(self.path))
            self.acquired = False

    def _create(self) -> None:
        descriptor = os.open(str(self.path), os.O_CREAT | os.O_EXCL | os.O_WRONLY, 0o644)
        with os.fdopen(descriptor, "w", encoding="utf-8") as stream:
            json.dump({"pid": os.getpid(), "since": _now(), "host": _hostname()}, stream)

    def _read(self) -> dict[str, Any]:
        try:
            blob = json.loads(self.path.read_text(encoding="utf-8"))
            return blob if isinstance(blob, dict) else {}
        except (OSError, json.JSONDecodeError):
            return {}

    def __enter__(self) -> OutputLock:
        return self.acquire()

    def __exit__(self, *_exc: object) -> None:
        self.release()


@dataclass
class _Noop:
    """Stand-in used when locking is disabled."""

    acquired: bool = field(default=False)

    def acquire(self, **_: object) -> _Noop:
        return self

    def release(self) -> None:
        return None

    def __enter__(self) -> _Noop:
        return self

    def __exit__(self, *_exc: object) -> None:
        return None


def no_lock() -> _Noop:
    return _Noop()


def _pid_alive(pid: Any) -> bool:
    try:
        pid = int(pid)
    except (TypeError, ValueError):
        return False
    if pid <= 0:
        return False
    try:
        os.kill(pid, 0)
    except ProcessLookupError:
        return False
    except PermissionError:
        return True  # exists, owned by someone else
    except OSError:
        return True
    return True


def _hostname() -> str:
    try:
        import socket

        return socket.gethostname()
    except Exception:  # pragma: no cover
        return ""


def _unlink(path: str) -> None:
    if not path:
        return
    try:
        os.unlink(path)
    except OSError:
        pass
