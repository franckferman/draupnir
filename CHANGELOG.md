# Changelog

All notable changes to this project are documented here.

The format follows [Keep a Changelog](https://keepachangelog.com/en/1.1.0/),
and the project adheres to [Semantic Versioning](https://semver.org/spec/v2.0.0.html).

## [1.0.0] - 2026-07-31

First public release.

### Added

**Discovery**
- Gitea/Forgejo REST API enumeration via `/api/v1/repos/search`, covering user
  *and* organisation repositories in a single sweep.
- Pagination pinned to `sort=id&order=asc` so a forge reordering results
  mid-sweep cannot cause repositories to be silently skipped.
- HTML fallback over `/explore/repos` when the API is disabled or firewalled;
  results are flagged `partial` and excluded from metadata-based filtering.
- Token authentication (`$DRAUPNIR_TOKEN`, `--token-file`, `--token`) with
  optional private-repository inclusion.

**Synchronisation**
- `worktree` mode (browsable checkouts, all branches) and `mirror` mode (bare,
  every ref, archival fidelity).
- Atomic clones: staged in a sibling temp directory and renamed into place, so
  an interrupted run never leaves a half-populated repository.
- Incremental sync — repositories whose forge `updated_at` is unchanged are
  skipped without invoking git.
- Per-repository retries with exponential backoff, restricted to network
  failures that retrying can actually fix.
- Wiki mirroring (`--include-wiki`), shallow clones (`--depth`), Git LFS
  (`--lfs`), and single-branch checkouts.

**Safety**
- Dirty working trees are reported, never reset, unless `--force` is given.
- Occupied paths are reported as conflicts and left untouched.
- `--prune-deleted` moves vanished repositories to `.draupnir-pruned/` rather
  than deleting them.
- Every remote name is validated before it becomes a path (traversal, control
  characters, bidi overrides, reserved device names), and the resolved
  destination is re-checked against the output root.
- Credentials travel via `GIT_CONFIG_*` environment variables — never on argv,
  never persisted to `.git/config` — and are redacted from all output.
- Advisory output-directory lock with stale-holder detection.

**Operations**
- `sync`, `list`, `status`, and `verify` subcommands.
- `verify` runs `git fsck` over every mirrored repository.
- JSON reports (`--json`) and documented exit codes for cron/systemd use.
- Cooperative interrupt handling: SIGINT finishes cleanly and returns a partial
  report; a second SIGINT aborts.

**Library**
- Public API (`MirrorEngine`, `MirrorConfig`, `ForgeClient`, `Repo`, …) with an
  event listener interface, so downstream tools can embed the engine instead of
  shelling out to the CLI.

**Project**
- Zero runtime dependencies: standard library plus the `git` binary.
- 201 tests covering real git repositories over `file://` and a stdlib fake
  forge serving genuine HTTP.
- CI across Python 3.9–3.13 on Linux and macOS.

[1.0.0]: https://github.com/franckferman/draupnir/releases/tag/v1.0.0
