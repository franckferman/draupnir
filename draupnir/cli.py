"""Command line interface.

Exit codes are part of the contract -- this tool is meant to run from cron and
systemd timers, where the only thing a supervisor sees is the status:

    0   everything succeeded
    1   the run completed but some repositories failed
    2   configuration/usage error, or the run could not start
    3   local mirror is damaged (verify only)
    130 interrupted by the operator
"""

from __future__ import annotations

import argparse
import os
import sys
from collections.abc import Sequence
from pathlib import Path

from . import __version__
from .audit import scan_local, verify_local
from .engine import MODES, MirrorConfig, MirrorEngine
from .errors import ConfigError, DraupnirError, GitMissing, LockError
from .gitcmd import GitRunner
from .http import DEFAULT_USER_AGENT
from .naming import LAYOUTS
from .report import render_repo_table, repos_as_json, write_json
from .ui import Console, ProgressReporter, human_size, human_time, render_summary

__all__ = ["build_parser", "main"]

EXIT_OK = 0
EXIT_PARTIAL = 1
EXIT_USAGE = 2
EXIT_DAMAGED = 3
EXIT_INTERRUPTED = 130

ENV_TOKEN = "DRAUPNIR_TOKEN"
ENV_OUTPUT = "DRAUPNIR_OUTPUT"

_EPILOG = """\
examples:
  draupnir sync https://git.example.org -o ~/mirror
  draupnir sync https://git.example.org -o ~/mirror --mode mirror --jobs 8
  draupnir sync https://git.example.org -o ~/mirror --exclude '^bot/' --skip-forks
  draupnir list https://git.example.org --json repos.json
  draupnir status -o ~/mirror
  draupnir verify -o ~/mirror

token:
  export DRAUPNIR_TOKEN=... (preferred; --token is visible in ps output)
"""


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        prog="draupnir",
        description="Mirror every repository of a Gitea/Forgejo forge, reliably.",
        epilog=_EPILOG,
        formatter_class=argparse.RawDescriptionHelpFormatter,
    )
    parser.add_argument("--version", action="version", version=f"draupnir {__version__}")
    subparsers = parser.add_subparsers(dest="command", metavar="<command>")

    # -- shared option groups -------------------------------------------
    def add_common(target: argparse.ArgumentParser) -> None:
        group = target.add_argument_group("output")
        group.add_argument("-q", "--quiet", action="store_true", help="errors only")
        group.add_argument("-v", "--verbose", action="store_true", help="per-step detail")
        group.add_argument("--no-color", action="store_true", help="disable ANSI colour")
        group.add_argument("--json", metavar="FILE", help="write a machine-readable report")

    def add_remote(target: argparse.ArgumentParser) -> None:
        group = target.add_argument_group("remote")
        group.add_argument(
            "--token",
            default="",
            help=f"API token (prefer ${ENV_TOKEN}: argv is world-readable)",
        )
        group.add_argument("--token-file", metavar="FILE", help="read the token from a file")
        group.add_argument("--timeout", type=float, default=30.0, help="HTTP timeout (default 30)")
        group.add_argument(
            "--http-retries", type=int, default=4, help="HTTP retry budget (default 4)"
        )
        group.add_argument(
            "--insecure", action="store_true", help="skip TLS verification (never on a public net)"
        )
        group.add_argument("--user-agent", default="", help="override the User-Agent")
        group.add_argument(
            "--no-html-fallback",
            action="store_true",
            help="fail instead of scraping /explore when the API is unavailable",
        )
        group.add_argument(
            "--include-private",
            action="store_true",
            help="also mirror private repos visible to the token",
        )

    def add_filters(target: argparse.ArgumentParser) -> None:
        group = target.add_argument_group("selection")
        group.add_argument(
            "--owner", action="append", default=[], metavar="NAME", help="restrict to owner(s)"
        )
        group.add_argument(
            "--include", action="append", default=[], metavar="REGEX", help="keep matches only"
        )
        group.add_argument(
            "--exclude", action="append", default=[], metavar="REGEX", help="drop matches"
        )
        group.add_argument(
            "--match", action="append", default=[], metavar="GLOB", help="keep glob matches only"
        )
        group.add_argument("--skip-forks", action="store_true")
        group.add_argument("--skip-archived", action="store_true")
        group.add_argument("--skip-mirrors", action="store_true")
        group.add_argument("--skip-templates", action="store_true")
        group.add_argument("--min-stars", type=int, default=0, metavar="N")
        group.add_argument(
            "--max-size", type=int, default=0, metavar="KB", help="skip repos larger than KB"
        )
        group.add_argument("--limit", type=int, default=0, metavar="N", help="cap the run size")

    # -- sync ------------------------------------------------------------
    sync = subparsers.add_parser(
        "sync",
        help="clone new repositories and update existing ones",
        description="Discover every repository on a forge, then clone or update each one.",
        formatter_class=argparse.RawDescriptionHelpFormatter,
        epilog=_EPILOG,
    )
    sync.add_argument("url", help="forge base URL, e.g. https://git.example.org")
    sync.add_argument(
        "-o",
        "--output",
        default="",
        metavar="DIR",
        help=f"destination tree (default: ./<host>, or ${ENV_OUTPUT})",
    )
    add_remote(sync)
    add_filters(sync)
    execution = sync.add_argument_group("execution")
    execution.add_argument("-j", "--jobs", type=int, default=4, help="parallel workers (default 4)")
    execution.add_argument(
        "--mode",
        choices=MODES,
        default="worktree",
        help="worktree: browsable checkouts (default). mirror: bare, every ref, archival",
    )
    execution.add_argument("--layout", choices=LAYOUTS, default="owner", help="on-disk layout")
    execution.add_argument(
        "--depth", type=int, default=0, metavar="N", help="shallow clone depth (0 = full history)"
    )
    execution.add_argument(
        "--single-branch", action="store_true", help="default branch only (worktree mode)"
    )
    execution.add_argument("--include-wiki", action="store_true", help="mirror wikis too")
    execution.add_argument("--clone-empty", action="store_true", help="do not skip empty repos")
    execution.add_argument("--lfs", action="store_true", help="fetch Git LFS objects")
    execution.add_argument(
        "--git-timeout", type=float, default=1800.0, metavar="SEC", help="per-git-command timeout"
    )
    execution.add_argument(
        "--git-retries", type=int, default=2, metavar="N", help="retries per repo on network errors"
    )
    behaviour = sync.add_argument_group("behaviour")
    behaviour.add_argument(
        "-n", "--dry-run", action="store_true", help="show the plan, touch nothing"
    )
    behaviour.add_argument(
        "--force",
        action="store_true",
        help="hard-reset repositories with local changes (destructive)",
    )
    behaviour.add_argument(
        "--refresh-all", action="store_true", help="ignore the incremental cache, check every repo"
    )
    behaviour.add_argument(
        "--no-state", action="store_true", help="do not read/write the state file"
    )
    behaviour.add_argument("--no-lock", action="store_true", help="skip the concurrent-run lock")
    behaviour.add_argument(
        "--prune-deleted",
        action="store_true",
        help="move locally-known repos the forge dropped into .draupnir-pruned/",
    )
    behaviour.add_argument(
        "--show-unchanged", action="store_true", help="print up-to-date repos too"
    )
    add_common(sync)

    # -- list ------------------------------------------------------------
    listing = subparsers.add_parser(
        "list", help="enumerate repositories without cloning anything"
    )
    listing.add_argument("url", help="forge base URL")
    listing.add_argument("--urls", action="store_true", help="print clone URLs under each entry")
    add_remote(listing)
    add_filters(listing)
    add_common(listing)

    # -- status ----------------------------------------------------------
    status = subparsers.add_parser("status", help="inspect a local mirror tree")
    status.add_argument("-o", "--output", default="", metavar="DIR", help="mirror directory")
    status.add_argument("--layout", choices=LAYOUTS, default="owner")
    status.add_argument("--no-size", action="store_true", help="skip disk usage (much faster)")
    add_common(status)

    # -- verify ----------------------------------------------------------
    verify = subparsers.add_parser("verify", help="run git fsck over every mirrored repository")
    verify.add_argument("-o", "--output", default="", metavar="DIR", help="mirror directory")
    verify.add_argument("--layout", choices=LAYOUTS, default="owner")
    verify.add_argument(
        "--deep", action="store_true", help="full object checksum verification (slow)"
    )
    add_common(verify)

    return parser


# -- helpers -------------------------------------------------------------


def _console(args: argparse.Namespace) -> Console:
    quiet = -1 if getattr(args, "quiet", False) else 0
    verbosity = 1 if getattr(args, "verbose", False) else quiet
    color = False if getattr(args, "no_color", False) else None
    return Console(stream=sys.stderr, color=color, verbosity=verbosity)


def _token(args: argparse.Namespace) -> str:
    path = getattr(args, "token_file", "") or ""
    if path:
        try:
            return Path(path).expanduser().read_text(encoding="utf-8").strip()
        except OSError as exc:
            raise ConfigError(f"cannot read token file: {exc}") from exc
    return (getattr(args, "token", "") or os.environ.get(ENV_TOKEN, "")).strip()


def _output_dir(args: argparse.Namespace, url: str = "") -> Path:
    raw = getattr(args, "output", "") or os.environ.get(ENV_OUTPUT, "")
    if raw:
        return Path(raw).expanduser()
    if url:
        from urllib.parse import urlsplit

        from .forge import normalise_base_url

        host = urlsplit(normalise_base_url(url)).netloc.replace(":", "_")
        return Path.cwd() / host
    raise ConfigError("no output directory: pass -o/--output")


def _config(args: argparse.Namespace, console: Console) -> MirrorConfig:
    token = _token(args)
    if getattr(args, "token", ""):
        console.warn("--token is visible to other users via ps; prefer $DRAUPNIR_TOKEN")
    if getattr(args, "insecure", False):
        console.warn("TLS verification disabled")

    return MirrorConfig(
        base_url=args.url,
        output=_output_dir(args, args.url),
        token=token,
        timeout=args.timeout,
        git_timeout=getattr(args, "git_timeout", 1800.0),
        http_retries=args.http_retries,
        git_retries=getattr(args, "git_retries", 2),
        verify_tls=not args.insecure,
        user_agent=args.user_agent or DEFAULT_USER_AGENT,
        allow_html_fallback=not args.no_html_fallback,
        owners=tuple(args.owner),
        include=tuple(args.include),
        exclude=tuple(args.exclude),
        match=tuple(args.match),
        skip_forks=args.skip_forks,
        skip_archived=args.skip_archived,
        skip_mirrors=args.skip_mirrors,
        skip_templates=args.skip_templates,
        min_stars=args.min_stars,
        max_size_kb=args.max_size,
        limit=args.limit,
        include_private=args.include_private,
        jobs=getattr(args, "jobs", 4),
        mode=getattr(args, "mode", "worktree"),
        layout=getattr(args, "layout", "owner"),
        depth=getattr(args, "depth", 0),
        include_wiki=getattr(args, "include_wiki", False),
        clone_empty=getattr(args, "clone_empty", False),
        lfs=getattr(args, "lfs", False),
        force=getattr(args, "force", False),
        single_branch=getattr(args, "single_branch", False),
        dry_run=getattr(args, "dry_run", False),
        refresh_all=getattr(args, "refresh_all", False),
        use_state=not getattr(args, "no_state", False),
        use_lock=not getattr(args, "no_lock", False),
        prune_deleted=getattr(args, "prune_deleted", False),
    )


# -- commands ------------------------------------------------------------


def cmd_sync(args: argparse.Namespace, console: Console) -> int:
    config = _config(args, console)
    console.write()
    console.write(
        f"  {console.paint('draupnir', 'bold')} {__version__} "
        f"{console.paint('->', 'dim')} {config.base_url}"
    )

    reporter = ProgressReporter(console, show_unchanged=getattr(args, "show_unchanged", False))
    engine = MirrorEngine(config, listener=reporter)
    report = engine.run()

    if console.verbosity >= 0:
        render_summary(console, report)
    if args.json:
        write_json(Path(args.json), report.to_dict())
        console.write(console.paint(f"  report: {args.json}", "dim"), level=0)

    if report.interrupted:
        return EXIT_INTERRUPTED
    return EXIT_PARTIAL if report.failures else EXIT_OK


def cmd_list(args: argparse.Namespace, console: Console) -> int:
    args.jobs = 1
    config = _config(args, console)
    engine = MirrorEngine(config)
    repos, info = engine.discover()
    plan = engine.plan(repos)

    console.write()
    console.write(
        f"  {console.paint(config.base_url, 'bold')} "
        f"{console.paint(info.describe(), 'dim')}"
    )
    render_repo_table(console, plan.selected, show_url=args.urls)
    if plan.excluded:
        reasons = ", ".join(f"{count} {name}" for name, count in plan.reasons().items())
        console.write(console.paint(f"  filtered out: {reasons}", "dim"))

    if args.json:
        write_json(
            Path(args.json),
            {
                "forge": {"base_url": config.base_url, "kind": info.kind, "version": info.version},
                "count": len(plan.selected),
                "repos": repos_as_json(plan.selected),
            },
        )
        console.write(console.paint(f"  report: {args.json}", "dim"))
    return EXIT_OK


def cmd_status(args: argparse.Namespace, console: Console) -> int:
    output = _output_dir(args)
    runner = GitRunner(timeout=300)
    report = scan_local(output, runner, layout=args.layout, measure=not args.no_size)

    console.write()
    console.write(f"  {console.paint(str(output), 'bold')}")
    console.write(
        f"  {len(report.repos)} repositories"
        + (f", {human_size(report.total_bytes)} on disk" if not args.no_size else "")
    )
    for group, label, style in (
        (report.dirty, "with local changes", "yellow"),
        (report.broken, "damaged", "red"),
    ):
        if group:
            console.write(console.paint(f"  {len(group)} {label}:", style))
            for repo in group[:15]:
                console.write(f"    {repo.name}" + (f" - {repo.problem}" if repo.problem else ""))
            if len(group) > 15:
                console.write(console.paint(f"    ... and {len(group) - 15} more", "dim"))
    if report.missing:
        console.write(
            console.paint(f"  {len(report.missing)} tracked but absent from disk", "yellow")
        )
    if report.untracked:
        console.write(console.paint(f"  {len(report.untracked)} present but not tracked", "dim"))

    if args.json:
        write_json(Path(args.json), report.to_dict())
        console.write(console.paint(f"  report: {args.json}", "dim"))
    return EXIT_OK


def cmd_verify(args: argparse.Namespace, console: Console) -> int:
    output = _output_dir(args)
    runner = GitRunner(timeout=900)
    report = scan_local(output, runner, layout=args.layout, measure=False)
    console.write()
    console.write(f"  verifying {len(report.repos)} repositories in {output}")
    verify_local(report, runner, deep=args.deep)

    broken = report.broken
    if broken:
        console.write(console.paint(f"  {len(broken)} damaged:", "red", "bold"))
        for repo in broken:
            console.write(f"    {repo.name}: {repo.problem}")
    else:
        console.write(console.paint(f"  all {len(report.repos)} repositories intact", "green"))

    if args.json:
        write_json(Path(args.json), report.to_dict())
        console.write(console.paint(f"  report: {args.json}", "dim"))
    return EXIT_DAMAGED if broken else EXIT_OK


_COMMANDS = {
    "sync": cmd_sync,
    "list": cmd_list,
    "status": cmd_status,
    "verify": cmd_verify,
}


def main(argv: Sequence[str] | None = None) -> int:
    parser = build_parser()
    args = parser.parse_args(argv)
    if not args.command:
        parser.print_help()
        return EXIT_USAGE

    console = _console(args)
    handler = _COMMANDS[args.command]
    started = _monotonic()

    try:
        return handler(args, console)
    except KeyboardInterrupt:
        console.write()
        console.warn("interrupted")
        return EXIT_INTERRUPTED
    except GitMissing as exc:
        console.error(f"{exc} -- install git and retry")
        return EXIT_USAGE
    except LockError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    except ConfigError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    except DraupnirError as exc:
        console.error(str(exc))
        return EXIT_USAGE
    except BrokenPipeError:  # piped into head/less
        try:
            sys.stderr.close()
        except OSError:
            pass
        return EXIT_OK
    finally:
        if console.verbosity >= 1:
            console.write(console.paint(f"  total {human_time(_monotonic() - started)}", "dim"))


def _monotonic() -> float:
    import time

    return time.monotonic()


def entrypoint() -> None:  # pragma: no cover - console_scripts shim
    try:
        sys.exit(main())
    except KeyboardInterrupt:
        sys.exit(EXIT_INTERRUPTED)


if __name__ == "__main__":  # pragma: no cover
    entrypoint()
