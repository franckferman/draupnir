"""CLI surface: parsing, exit codes, token handling."""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from draupnir import cli
from draupnir.cli import (
    EXIT_DAMAGED,
    EXIT_INTERRUPTED,
    EXIT_OK,
    EXIT_PARTIAL,
    EXIT_USAGE,
    build_parser,
    main,
)
from tests.conftest import needs_git
from tests.fakeforge import FakeForge, ForgeState, make_repo


def test_no_command_prints_help() -> None:
    assert main([]) == EXIT_USAGE


def test_version_flag() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["--version"])
    assert excinfo.value.code == 0


def test_unknown_command_is_usage_error() -> None:
    with pytest.raises(SystemExit) as excinfo:
        build_parser().parse_args(["nonsense"])
    assert excinfo.value.code == EXIT_USAGE


def test_bad_mode_rejected() -> None:
    with pytest.raises(SystemExit):
        build_parser().parse_args(["sync", "https://h", "--mode", "banana"])


def test_invalid_regex_is_config_error(tmp_path: Path) -> None:
    code = main(["sync", "https://h", "-o", str(tmp_path), "--include", "(unclosed", "-q"])
    assert code == EXIT_USAGE


def test_missing_output_for_status_is_usage_error(monkeypatch) -> None:
    monkeypatch.delenv("DRAUPNIR_OUTPUT", raising=False)
    assert main(["status", "-q"]) == EXIT_USAGE


def test_token_from_env(monkeypatch) -> None:
    monkeypatch.setenv("DRAUPNIR_TOKEN", "from-env")
    args = build_parser().parse_args(["sync", "https://h"])
    assert cli._token(args) == "from-env"


def test_token_file_wins_over_env(tmp_path: Path, monkeypatch) -> None:
    monkeypatch.setenv("DRAUPNIR_TOKEN", "from-env")
    path = tmp_path / "tok"
    path.write_text("from-file\n", encoding="utf-8")
    args = build_parser().parse_args(["sync", "https://h", "--token-file", str(path)])
    assert cli._token(args) == "from-file"


def test_unreadable_token_file_is_config_error(tmp_path: Path) -> None:
    args = build_parser().parse_args(
        ["sync", "https://h", "--token-file", str(tmp_path / "nope")]
    )
    with pytest.raises(cli.ConfigError):
        cli._token(args)


def test_output_defaults_to_host_directory(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.delenv("DRAUPNIR_OUTPUT", raising=False)
    monkeypatch.chdir(tmp_path)
    args = build_parser().parse_args(["sync", "https://git.example.org"])
    assert cli._output_dir(args, args.url).name == "git.example.org"


def test_output_env_var(monkeypatch, tmp_path: Path) -> None:
    monkeypatch.setenv("DRAUPNIR_OUTPUT", str(tmp_path / "from-env"))
    args = build_parser().parse_args(["sync", "https://h"])
    assert cli._output_dir(args, args.url).name == "from-env"


def test_repeatable_filters_parse() -> None:
    args = build_parser().parse_args(
        ["sync", "https://h", "--exclude", "a", "--exclude", "b", "--owner", "x"]
    )
    assert args.exclude == ["a", "b"]
    assert args.owner == ["x"]


# -- end to end ----------------------------------------------------------


@needs_git
def test_sync_success_exit_code(tmp_path: Path, origin: Path) -> None:
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        code = main(["sync", forge.url, "-o", str(tmp_path / "m"), "-q", "--no-lock"])
    assert code == EXIT_OK


@needs_git
def test_sync_partial_failure_exit_code(tmp_path: Path, origin: Path) -> None:
    rows = [
        make_repo("alice", "good", rid=1, clone_url=f"file://{origin}"),
        make_repo("alice", "bad", rid=2, clone_url=f"file://{tmp_path}/missing"),
    ]
    with FakeForge(ForgeState(repos=rows)) as forge:
        code = main(["sync", forge.url, "-o", str(tmp_path / "m"), "-q", "--no-lock"])
    assert code == EXIT_PARTIAL


@needs_git
def test_sync_writes_json_report(tmp_path: Path, origin: Path) -> None:
    report_path = tmp_path / "report.json"
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    with FakeForge(ForgeState(repos=[row])) as forge:
        main(
            [
                "sync", forge.url, "-o", str(tmp_path / "m"),
                "--json", str(report_path), "-q", "--no-lock",
            ]
        )
    blob = json.loads(report_path.read_text(encoding="utf-8"))
    assert blob["counts"] == {"cloned": 1}
    assert blob["repos"][0]["repo"] == "alice/tool"


def test_unreachable_forge_is_usage_error(tmp_path: Path) -> None:
    code = main(
        ["sync", "http://127.0.0.1:1", "-o", str(tmp_path / "m"), "-q",
         "--http-retries", "0", "--timeout", "2", "--no-lock"]
    )
    assert code == EXIT_USAGE


def test_list_command(tmp_path: Path, capsys) -> None:
    rows = [make_repo("a", "b", rid=1), make_repo("a", "c", rid=2)]
    out = tmp_path / "repos.json"
    with FakeForge(ForgeState(repos=rows)) as forge:
        code = main(["list", forge.url, "--json", str(out)])
    assert code == EXIT_OK
    blob = json.loads(out.read_text(encoding="utf-8"))
    assert blob["count"] == 2


@needs_git
def test_status_and_verify_commands(tmp_path: Path, origin: Path) -> None:
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    mirror = tmp_path / "m"
    with FakeForge(ForgeState(repos=[row])) as forge:
        main(["sync", forge.url, "-o", str(mirror), "-q", "--no-lock"])

    assert main(["status", "-o", str(mirror), "-q"]) == EXIT_OK
    assert main(["verify", "-o", str(mirror), "-q"]) == EXIT_OK


@needs_git
def test_verify_reports_damage(tmp_path: Path, origin: Path) -> None:
    from tests.conftest import corrupt_objects

    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    mirror = tmp_path / "m"
    with FakeForge(ForgeState(repos=[row])) as forge:
        main(["sync", forge.url, "-o", str(mirror), "-q", "--no-lock"])

    assert corrupt_objects(mirror / "alice" / "tool") > 0
    assert main(["verify", "-o", str(mirror), "-q"]) == EXIT_DAMAGED


@needs_git
def test_dry_run_creates_nothing(tmp_path: Path, origin: Path) -> None:
    row = make_repo("alice", "tool", rid=1, clone_url=f"file://{origin}")
    mirror = tmp_path / "m"
    with FakeForge(ForgeState(repos=[row])) as forge:
        assert main(["sync", forge.url, "-o", str(mirror), "-n", "-q"]) == EXIT_OK
    assert not (mirror / "alice").exists()


def test_keyboard_interrupt_exit_code(tmp_path: Path, monkeypatch) -> None:
    def boom(*_args, **_kwargs):
        raise KeyboardInterrupt

    monkeypatch.setattr(cli, "cmd_list", boom)
    monkeypatch.setitem(cli._COMMANDS, "list", boom)
    assert main(["list", "https://h", "-q"]) == EXIT_INTERRUPTED
