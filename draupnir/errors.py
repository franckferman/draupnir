"""Exception hierarchy.

Everything raised on purpose by draupnir derives from DraupnirError, so callers
embedding the library can catch a single base class.
"""

from __future__ import annotations


class DraupnirError(Exception):
    """Base class for every deliberate draupnir failure."""


class ConfigError(DraupnirError):
    """Invalid configuration / bad user input."""


class HttpError(DraupnirError):
    """HTTP layer failure (transport, status, or decoding)."""

    def __init__(self, message: str, *, url: str = "", status: int = 0) -> None:
        super().__init__(message)
        self.url = url
        self.status = status


class RateLimited(HttpError):
    """429 or explicit rate-limit response that outlived our retry budget."""


class ForgeError(DraupnirError):
    """The remote forge answered, but not with something we can use."""


class DiscoveryError(ForgeError):
    """Repository enumeration failed on every available strategy."""


class UnsafeNameError(DraupnirError):
    """Owner/repo name cannot be mapped to a filesystem path safely."""


class GitError(DraupnirError):
    """A git subprocess failed, timed out, or could not be started."""

    def __init__(
        self,
        message: str,
        *,
        args: tuple[str, ...] = (),
        returncode: int | None = None,
        stderr: str = "",
        timed_out: bool = False,
    ) -> None:
        super().__init__(message)
        self.args_ = args
        self.returncode = returncode
        self.stderr = stderr
        self.timed_out = timed_out


class GitMissing(GitError):
    """No usable git binary on PATH."""


class LockError(DraupnirError):
    """Another draupnir run holds the output directory."""


class StateError(DraupnirError):
    """State file is unreadable or corrupt beyond recovery."""


class Interrupted(DraupnirError):
    """Run cancelled by the operator (SIGINT/SIGTERM)."""
