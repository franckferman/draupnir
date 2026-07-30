"""draupnir -- mirror every repository of a Gitea/Forgejo forge.

Public API, stable across 1.x:

    from draupnir import MirrorConfig, MirrorEngine, ForgeClient, HttpClient

    engine = MirrorEngine(MirrorConfig(base_url="https://git.example.org",
                                       output=Path("/srv/mirror")))
    report = engine.run()
    print(report.counts())

Everything the CLI does is reachable from this API, so downstream tools can
build on the engine instead of shelling out to it.
"""

from __future__ import annotations

__version__ = "1.0.0"
__author__ = "franckferman"
__license__ = "AGPL-3.0-or-later"

from .audit import AuditReport, LocalRepo, scan_local, verify_local
from .engine import Event, MirrorConfig, MirrorEngine
from .errors import (
    ConfigError,
    DiscoveryError,
    DraupnirError,
    ForgeError,
    GitError,
    GitMissing,
    HttpError,
    LockError,
    RateLimited,
    UnsafeNameError,
)
from .forge import ForgeClient, normalise_base_url
from .gitcmd import GitRunner, git_version
from .http import HttpClient
from .models import ForgeInfo, Outcome, Plan, Repo, RunReport, Status
from .naming import repo_path, safe_component
from .state import StateStore
from .sync import RepoSyncer, SyncOptions

__all__ = [
    "__version__",
    # engine
    "MirrorConfig",
    "MirrorEngine",
    "Event",
    # discovery
    "ForgeClient",
    "HttpClient",
    "normalise_base_url",
    # git
    "GitRunner",
    "RepoSyncer",
    "SyncOptions",
    "git_version",
    # models
    "Repo",
    "Outcome",
    "Plan",
    "RunReport",
    "Status",
    "ForgeInfo",
    # local inspection
    "AuditReport",
    "LocalRepo",
    "scan_local",
    "verify_local",
    "StateStore",
    # paths
    "repo_path",
    "safe_component",
    # errors
    "DraupnirError",
    "ConfigError",
    "HttpError",
    "RateLimited",
    "ForgeError",
    "DiscoveryError",
    "GitError",
    "GitMissing",
    "LockError",
    "UnsafeNameError",
]
