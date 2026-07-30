"""git subprocess layer.

Three things the naive `subprocess.run(["git", "clone", ...])` gets wrong and
that this module fixes:

  * **Hangs.** git will happily block forever on a credential prompt or a dead
    TCP connection. Every invocation gets a timeout, terminal prompts are
    disabled, and a timed-out git is killed as a *process group* (git spawns
    git-remote-https children that survive a bare kill).
  * **Token leakage.** Credentials are passed through `GIT_CONFIG_*` env vars,
    not on argv (visible in `ps`) and not baked into the remote URL (which
    would persist in `.git/config` on disk).
  * **Silence.** stdout/stderr are captured and surfaced in the exception, with
    secrets redacted, instead of being dumped to DEVNULL.
"""

from __future__ import annotations

import os
import re
import shutil
import signal
import subprocess
import threading
from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from pathlib import Path

from .errors import GitError, GitMissing

__all__ = ["GitResult", "GitRunner", "git_version", "redact"]

_SECRET = re.compile(r"(?i)(://[^/\s:@]+:)[^@/\s]+(@)")
_TOKEN_LINE = re.compile(r"(?i)(authorization:\s*token\s+)\S+")


def redact(text: str, *extra: str) -> str:
    """Strip credentials from anything we are about to print or store."""
    cleaned = _SECRET.sub(r"\1***\2", text or "")
    cleaned = _TOKEN_LINE.sub(r"\1***", cleaned)
    for secret in extra:
        if secret and len(secret) >= 6:
            cleaned = cleaned.replace(secret, "***")
    return cleaned


@dataclass(frozen=True)
class GitResult:
    args: tuple[str, ...]
    returncode: int
    stdout: str
    stderr: str

    @property
    def ok(self) -> bool:
        return self.returncode == 0

    @property
    def out(self) -> str:
        return self.stdout.strip()


def git_version(binary: str = "git") -> str:
    """Return the git version string, or raise GitMissing."""
    path = shutil.which(binary)
    if not path:
        raise GitMissing(f"{binary!r} not found on PATH")
    try:
        proc = subprocess.run(
            [path, "--version"],
            capture_output=True,
            text=True,
            timeout=15,
            check=False,
        )
    except (OSError, subprocess.SubprocessError) as exc:
        raise GitMissing(f"cannot execute {path}: {exc}") from exc
    if proc.returncode != 0:
        raise GitMissing(f"{path} --version exited {proc.returncode}")
    return proc.stdout.strip()


@dataclass
class GitRunner:
    """Runs git with a hardened environment. Safe to share across threads."""

    binary: str = "git"
    timeout: float = 1800.0
    token: str = ""
    lfs: bool = False
    verify_tls: bool = True
    extra_config: tuple[str, ...] = ()  # "key=value" pairs
    cancel: threading.Event = field(default_factory=threading.Event)

    def run(
        self,
        args: Sequence[str],
        *,
        cwd: Path | str | None = None,
        timeout: float | None = None,
        check: bool = True,
        authenticated: bool = False,
    ) -> GitResult:
        """Execute one git command.

        `authenticated` injects the token as an HTTP header for network
        commands; leave it off for purely local plumbing.
        """
        if self.cancel.is_set():
            raise GitError("cancelled before start", args=tuple(args))

        binary = shutil.which(self.binary)
        if not binary:
            raise GitMissing(f"{self.binary!r} not found on PATH")

        argv = [binary, *args]
        limit = self.timeout if timeout is None else timeout
        env = self._env(authenticated=authenticated)

        try:
            proc = subprocess.Popen(
                argv,
                cwd=str(cwd) if cwd else None,
                env=env,
                stdin=subprocess.DEVNULL,
                stdout=subprocess.PIPE,
                stderr=subprocess.PIPE,
                text=True,
                errors="replace",
                start_new_session=True,  # own process group -> killable as a unit
            )
        except OSError as exc:
            raise GitError(f"cannot start git: {exc}", args=tuple(args)) from exc

        try:
            stdout, stderr = proc.communicate(timeout=limit)
        except subprocess.TimeoutExpired as exc:
            _kill_group(proc)
            stdout, stderr = _drain(proc)
            raise GitError(
                f"git {' '.join(args[:2])} timed out after {limit:.0f}s",
                args=tuple(args),
                stderr=redact(stderr, self.token),
                timed_out=True,
            ) from exc
        except BaseException:  # KeyboardInterrupt and friends must not orphan git
            _kill_group(proc)
            raise

        result = GitResult(
            args=tuple(args),
            returncode=proc.returncode,
            stdout=stdout or "",
            stderr=redact(stderr or "", self.token),
        )
        if check and not result.ok:
            summary = _first_useful_line(result.stderr) or f"exit {result.returncode}"
            raise GitError(
                f"git {' '.join(args[:2])} failed: {summary}",
                args=result.args,
                returncode=result.returncode,
                stderr=result.stderr,
            )
        return result

    # -- environment -----------------------------------------------------

    def _env(self, *, authenticated: bool) -> dict[str, str]:
        env = dict(os.environ)
        # Never block on a prompt: no controlling terminal, no askpass helper,
        # no credential manager UI.
        env["GIT_TERMINAL_PROMPT"] = "0"
        env["GIT_ASKPASS"] = ""
        env["SSH_ASKPASS"] = ""
        env["GCM_INTERACTIVE"] = "never"
        env["GIT_FLUSH"] = "1"
        # Deterministic, parseable output regardless of the operator's locale.
        env["LC_ALL"] = "C"
        env["LANG"] = "C"
        env.pop("GIT_DIR", None)
        env.pop("GIT_WORK_TREE", None)
        if not self.lfs:
            env["GIT_LFS_SKIP_SMUDGE"] = "1"

        configs: list[tuple[str, str]] = list(_split_config(self.extra_config))
        if authenticated and self.token:
            configs.append(("http.extraHeader", f"Authorization: token {self.token}"))
        if not self.verify_tls:
            configs.append(("http.sslVerify", "false"))

        if configs:
            # GIT_CONFIG_COUNT keeps secrets out of argv and out of .git/config.
            existing = _as_int(env.get("GIT_CONFIG_COUNT"))
            for offset, (key, value) in enumerate(configs):
                index = existing + offset
                env[f"GIT_CONFIG_KEY_{index}"] = key
                env[f"GIT_CONFIG_VALUE_{index}"] = value
            env["GIT_CONFIG_COUNT"] = str(existing + len(configs))
        return env


def _split_config(pairs: Sequence[str]) -> list[tuple[str, str]]:
    out: list[tuple[str, str]] = []
    for item in pairs:
        key, sep, value = item.partition("=")
        if sep and key.strip():
            out.append((key.strip(), value))
    return out


def _as_int(raw: str | None) -> int:
    try:
        return max(0, int(raw or 0))
    except ValueError:
        return 0


def _kill_group(proc: subprocess.Popen) -> None:
    """SIGTERM the whole group, then SIGKILL what is left."""
    for sig, grace in ((signal.SIGTERM, 5.0), (signal.SIGKILL, 2.0)):
        if proc.poll() is not None:
            return
        try:
            os.killpg(os.getpgid(proc.pid), sig)
        except (ProcessLookupError, PermissionError, OSError):
            try:
                proc.kill()
            except OSError:
                return
        try:
            proc.wait(timeout=grace)
            return
        except subprocess.TimeoutExpired:
            continue


def _drain(proc: subprocess.Popen) -> tuple[str, str]:
    try:
        return proc.communicate(timeout=5)
    except (subprocess.TimeoutExpired, ValueError, OSError):  # pragma: no cover
        return "", ""


def _first_useful_line(stderr: str) -> str:
    noise = ("cloning into", "warning:", "remote:", "receiving objects", "resolving deltas")
    for line in (stderr or "").splitlines():
        text = line.strip()
        if text and not text.lower().startswith(noise):
            return text[:300]
    return (stderr or "").strip().splitlines()[-1][:300] if stderr.strip() else ""


def env_for(mapping: Mapping[str, str]) -> dict[str, str]:  # pragma: no cover - helper
    env = dict(os.environ)
    env.update(mapping)
    return env
