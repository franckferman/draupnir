<div id="top" align="center">

[![CI][ci-shield]](https://github.com/franckferman/draupnir/actions/workflows/ci.yml)
[![Python][python-shield]](https://www.python.org/)
[![Dependencies][deps-shield]](#why-no-dependencies)
[![Stars][stars-shield]](https://github.com/franckferman/draupnir/stargazers)
[![Issues][issues-shield]](https://github.com/franckferman/draupnir/issues)
[![License][license-shield]](https://github.com/franckferman/draupnir/blob/stable/LICENSE)

<h2 align="center">Draupnir</h2>

<p align="center">
  <strong>Mirror every repository of a Gitea/Forgejo forge — reliably, incrementally, unattended.</strong><br>
  Zero dependencies. Atomic clones. Resumable. Built to run from a systemd timer for years.
</p>

<p align="center">
  <a href="#about">About</a> ·
  <a href="#install">Install</a> ·
  <a href="#quick-start">Quick Start</a> ·
  <a href="#commands">Commands</a> ·
  <a href="#cli-reference">CLI Reference</a> ·
  <a href="#automation">Automation</a> ·
  <a href="#library-api">Library API</a> ·
  <a href="#design-notes">Design Notes</a>
</p>

</div>

---

## About

**Draupnir** — the Norse ring forged by the dwarves Brokkr and Sindri, which every ninth night
drips eight new rings identical to itself — mirrors a whole Git forge onto your disk.

Point it at a Gitea or Forgejo instance and it will find every repository on it,
clone what is missing, update what changed, skip what did not, and tell you
exactly what happened. Then run it again tomorrow, and the day after, from a
timer, without watching it.

```console
$ draupnir sync https://git.example.org -o ~/mirror

  draupnir 1.0.0 -> https://git.example.org
  102 repositories discovered [gitea 1.27.1]
  102 to sync

  [  1/102] cloned    Nightmare_Eclipse/BlueHammer      467 KB
  [  2/102] updated   ek0mssavi0r/rootkit-poc           +3 commits
  [  3/102] skipped   someone/scratch                   (empty repository)
  ...

  summary  87 cloned  14 updated  1 skipped
          443.0 MB of repository data touched
          102 discovered  102 planned  in 3m12s
```

### What makes it different

Mirroring a forge looks like a twenty-line script until you actually depend on it.
Draupnir is the version that survives contact with reality:

| | Naive approach | Draupnir |
|---|---|---|
| **Discovery** | Scrape `/explore/users`, then scrape each profile | REST API `/repos/search` — users **and** organisations, with metadata |
| **Completeness** | Misses repos past page 1 of a profile, misses orgs | Every repository, verified against `X-Total-Count` |
| **Pagination** | Default ordering shifts mid-sweep → silent gaps | Pinned to `sort=id&order=asc` — a stable key |
| **History** | `--depth 1`, one branch | Full history, every branch and tag (or `--mirror` for bare archival) |
| **Interruption** | Half-cloned directory, mistaken for good next run | Staged in a temp dir, renamed only on success |
| **Re-runs** | Re-fetches everything, every time | Skips untouched repos via forge timestamps |
| **Failures** | `executor.map()` swallows the exception | Every failure captured, reported, and retried when retryable |
| **Hangs** | git blocks forever on a credential prompt | Prompts disabled, timeouts everywhere, process groups killed |
| **Your edits** | `git pull` fails or clobbers | Dirty trees reported, never reset without `--force` |
| **Credentials** | Token baked into the remote URL on disk | Passed via env, never written to `.git/config`, redacted from output |
| **Deleted repos** | Silently accumulate forever | `--prune-deleted` moves them aside (never deletes) |
| **Verification** | None | `draupnir verify` runs `git fsck` across the mirror |

### Why no dependencies

A backup tool is only useful if it still runs in five years. Draupnir imports
nothing but the Python standard library and calls the `git` binary. There is no
`requests`, no `beautifulsoup4`, no transitive dependency that can break your
restore path on the day you actually need it.

---

## Install

Requires **Python 3.9+** and **git**.

```bash
pip install git+https://github.com/franckferman/draupnir.git
```

Or clone and install locally:

```bash
git clone https://github.com/franckferman/draupnir.git
cd draupnir
pip install -e .
```

Or just run it — there is nothing to install:

```bash
python -m draupnir sync https://git.example.org -o ~/mirror
```

---

## Quick Start

```bash
# Mirror an entire forge into ~/mirror
draupnir sync https://git.example.org -o ~/mirror

# See what would happen, touch nothing
draupnir sync https://git.example.org -o ~/mirror --dry-run

# Bare archival mirror, 8 workers, wikis included
draupnir sync https://git.example.org -o /srv/archive --mode mirror -j 8 --include-wiki

# Only one owner, skipping forks
draupnir sync https://git.example.org -o ~/mirror --owner alice --skip-forks

# Private repositories too
export DRAUPNIR_TOKEN=<your-token>
draupnir sync https://git.example.org -o ~/mirror --include-private
```

Repositories land as `<output>/<owner>/<repo>`:

```
~/mirror/
├── alice/
│   ├── tool/
│   └── library/
├── bob/
│   └── notes/
└── .draupnir-state.json
```

---

## Commands

### `sync` — clone and update

The main verb. Discovers, filters, then clones or updates each repository in
parallel. Safe to interrupt, safe to re-run, safe to schedule.

### `list` — enumerate without cloning

```console
$ draupnir list https://git.example.org

  https://git.example.org gitea 1.27.1

  REPOSITORY                      SIZE     ★  FLAGS
  ----------------------------------------------------
  alice/hack-house            221.7 MB     3
  bob/noPROXY                  52.7 MB     -  fork
  carol/nightshift             13.0 MB     -  mirror

  102 repositories, 443.0 MB total
```

Accepts every selection flag `sync` does, so you can rehearse a filter before
committing disk to it. `--json` writes the full metadata for scripting.

### `status` — inspect a local mirror

Reports what is on disk, what has local modifications, what the state file
tracks but cannot find, and what exists but is untracked.

### `verify` — prove the mirror is intact

Runs `git fsck` over every repository. `--deep` re-hashes every object (slow,
thorough). Exits `3` if anything is damaged, so a timer can page you.

---

## CLI Reference

### Selection

| Flag | Description |
|---|---|
| `--owner NAME` | Restrict to owner(s). Repeatable. |
| `--include REGEX` | Keep only `owner/name` matches. Repeatable. |
| `--exclude REGEX` | Drop `owner/name` matches. Repeatable. |
| `--match GLOB` | Keep only glob matches, e.g. `*/tool-*`. Repeatable. |
| `--skip-forks` | Skip forks. |
| `--skip-archived` | Skip archived repositories. |
| `--skip-mirrors` | Skip repositories that are themselves mirrors. |
| `--skip-templates` | Skip template repositories. |
| `--min-stars N` | Only repositories with at least N stars. |
| `--max-size KB` | Skip repositories larger than KB. |
| `--limit N` | Cap the run — with the incremental cache, each run advances N further. |
| `--include-private` | Include private repositories the token can see. |

### Execution

| Flag | Default | Description |
|---|---|---|
| `-j, --jobs N` | `4` | Parallel workers. |
| `--mode {worktree,mirror}` | `worktree` | Browsable checkouts, or bare archival mirrors. |
| `--layout {owner,flat}` | `owner` | `owner/repo` tree, or `owner__repo` flat. |
| `--depth N` | `0` | Shallow clone depth. `0` keeps full history. |
| `--single-branch` | off | Default branch only. |
| `--include-wiki` | off | Mirror wikis alongside repositories. |
| `--clone-empty` | off | Do not skip empty repositories. |
| `--lfs` | off | Fetch Git LFS objects. |
| `--git-timeout SEC` | `1800` | Per-git-command timeout. |
| `--git-retries N` | `2` | Retries per repository on network errors. |

### Behaviour

| Flag | Description |
|---|---|
| `-n, --dry-run` | Print the plan, touch nothing. |
| `--force` | Hard-reset repositories with local changes. **Destructive.** |
| `--refresh-all` | Ignore the incremental cache; check every repository. |
| `--prune-deleted` | Move locally-known repos the forge dropped into `.draupnir-pruned/`. |
| `--no-state` | Do not read or write the state file. |
| `--no-lock` | Skip the concurrent-run lock. |
| `--show-unchanged` | Print up-to-date repositories too. |

### Remote

| Flag | Description |
|---|---|
| `--token TOKEN` | API token. Prefer `$DRAUPNIR_TOKEN` — argv is world-readable. |
| `--token-file FILE` | Read the token from a file. |
| `--timeout SEC` | HTTP timeout (default 30). |
| `--http-retries N` | HTTP retry budget (default 4). |
| `--insecure` | Skip TLS verification. Never on a public network. |
| `--no-html-fallback` | Fail instead of scraping `/explore` when the API is unavailable. |

### Environment

| Variable | Purpose |
|---|---|
| `DRAUPNIR_TOKEN` | API token. Preferred over `--token`. |
| `DRAUPNIR_OUTPUT` | Default output directory. |
| `NO_COLOR` | Disable ANSI colour. |

### Exit codes

| Code | Meaning |
|---|---|
| `0` | Everything succeeded. |
| `1` | Run completed, some repositories failed. |
| `2` | Configuration or usage error; the run could not start. |
| `3` | Local mirror is damaged (`verify` only). |
| `130` | Interrupted by the operator. |

---

## Automation

Draupnir is built to be scheduled. It locks its output directory, exits with
meaningful codes, and writes a JSON report you can alert on.

**`/etc/systemd/system/draupnir.service`**

```ini
[Unit]
Description=Mirror git.example.org
After=network-online.target
Wants=network-online.target

[Service]
Type=oneshot
User=mirror
Environment=DRAUPNIR_TOKEN_FILE=/etc/draupnir/token
ExecStart=/usr/local/bin/draupnir sync https://git.example.org \
    --output /srv/mirror \
    --token-file /etc/draupnir/token \
    --mode mirror \
    --jobs 8 \
    --json /var/log/draupnir/last-run.json \
    --quiet

# Hardening
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ReadWritePaths=/srv/mirror /var/log/draupnir
```

**`/etc/systemd/system/draupnir.timer`**

```ini
[Unit]
Description=Mirror git.example.org nightly

[Timer]
OnCalendar=daily
RandomizedDelaySec=1h
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl enable --now draupnir.timer
```

A weekly integrity check pairs well with it:

```bash
draupnir verify -o /srv/mirror --json /var/log/draupnir/verify.json || notify-my-pager
```

---

## Library API

Everything the CLI does is available as a library, so other tools can embed the
engine rather than shell out to it.

```python
from pathlib import Path
from draupnir import MirrorConfig, MirrorEngine

config = MirrorConfig(
    base_url="https://git.example.org",
    output=Path("/srv/mirror"),
    mode="mirror",
    jobs=8,
    skip_forks=True,
)

report = MirrorEngine(config).run()

print(report.counts())          # {'cloned': 87, 'updated': 14, 'skipped': 1}
print(report.duration)          # 192.4
for failure in report.failures:
    print(failure.repo.full_name, failure.detail)
```

Discovery alone, without touching the disk:

```python
from draupnir import ForgeClient, HttpClient

client = ForgeClient("https://git.example.org", HttpClient())
repos, info = client.discover()

print(info.describe())                       # 'gitea 1.27.1'
print(sum(r.size_kb for r in repos))         # 453725
print([r.full_name for r in repos if r.fork])
```

Progress events, for your own UI:

```python
def on_event(event):
    if event.kind == "repo_done":
        print(event.outcome.status.value, event.outcome.repo.full_name)

MirrorEngine(config, listener=on_event).run()
```

The engine never writes to stdout itself — rendering is entirely the caller's.

---

## Design Notes

A few decisions worth explaining, because they are the difference between a
script and something you can leave running.

**Pagination is pinned to a stable key.** Gitea's default result ordering is
"recently updated". Fetch page 1, and if any repository is pushed to before you
fetch page 2, the ordering shifts underneath you and something falls through the
gap — a mirror that is quietly, unreproducibly incomplete. Sorting by `id`
ascending makes the sweep deterministic.

**Clones are atomic.** git clones into `<repo>.draupnir-tmp-<pid>-<rand>`, and
only a successful clone is `os.replace()`d into its final name. A repository
directory therefore either does not exist or is complete. Without this, a
Ctrl-C mid-clone leaves a directory that the next run happily treats as an
existing repository and tries to `fetch` from.

**Incremental by default.** The forge reports `updated_at` per repository. If it
matches what we recorded at the last successful sync and the directory is still
there, git is never invoked. Re-mirroring 102 repositories takes about two
seconds instead of several minutes.

**Nothing is destroyed.** A dirty working tree is reported and left alone. An
occupied path is a conflict, not an opportunity. `--prune-deleted` moves
directories to `.draupnir-pruned/`. `--force` exists, is opt-in, and is
documented as destructive.

**git cannot hang.** `GIT_TERMINAL_PROMPT=0`, empty `GIT_ASKPASS`, a timeout on
every invocation, and `start_new_session=True` so a timed-out clone can be killed
as a process group — git spawns `git-remote-https` children that survive a plain
`kill()`.

**Tokens stay off disk and out of `ps`.** Credentials are injected through
`GIT_CONFIG_COUNT`/`GIT_CONFIG_KEY_n` environment variables as an
`http.extraHeader`. They never appear on a command line, and never persist into
`.git/config` the way a `https://token@host/...` remote would.

**Remote names are untrusted input.** On a public forge anyone can register an
account and name a repository. Every path component is validated — traversal,
separators, control characters, bidi overrides, reserved device names — and the
resolved destination is checked to be inside the output root before any write.

---

## Testing

```bash
pip install -e ".[dev]"
pytest tests -q
```

201 tests. The git layer runs against real repositories over `file://` URLs, and
the HTTP layer against a stdlib fake forge serving genuine HTTP with real
pagination headers — no mocks standing in for the units under test. Failure
paths are exercised for real: killed clones, corrupted packfiles, stale locks,
dirty trees, path traversal, rate limits.

---

## Compatibility

Tested against **Gitea 1.27** and Forgejo. Any instance exposing
`/api/v1/repos/search` should work; if the API is unavailable, the HTML fallback
covers public repositories.

Python 3.9 through 3.13, Linux and macOS.

---

## Prior art

The idea came from
[clone-all-church-repos](https://git.churchofmalware.org/Trilltechnician/clone-all-church-repos)
by Trilltechnician, a compact script that scrapes a Gitea instance and clones
what it finds. Draupnir is an independent implementation — different
architecture, API-first discovery, no shared code — built to generalise the idea
to any forge and to hold up as unattended infrastructure.

---

## License

[AGPL-3.0-or-later](LICENSE) © franckferman

<!-- MARKDOWN LINKS & IMAGES -->
[ci-shield]: https://img.shields.io/github/actions/workflow/status/franckferman/draupnir/ci.yml?branch=stable&style=for-the-badge&label=CI
[python-shield]: https://img.shields.io/badge/python-3.9%2B-blue?style=for-the-badge
[deps-shield]: https://img.shields.io/badge/dependencies-none-brightgreen?style=for-the-badge
[stars-shield]: https://img.shields.io/github/stars/franckferman/draupnir?style=for-the-badge
[issues-shield]: https://img.shields.io/github/issues/franckferman/draupnir?style=for-the-badge
[license-shield]: https://img.shields.io/github/license/franckferman/draupnir?style=for-the-badge
