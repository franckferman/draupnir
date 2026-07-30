"""git subprocess layer: timeouts, hang-proofing, secret hygiene."""

from __future__ import annotations

import time
from pathlib import Path

import pytest

from draupnir.errors import GitError, GitMissing
from draupnir.gitcmd import GitRunner, git_version, redact
from tests.conftest import needs_git


@needs_git
def test_version_probe() -> None:
    assert "git version" in git_version()


def test_missing_binary_is_typed() -> None:
    with pytest.raises(GitMissing):
        git_version("definitely-not-a-real-git-binary")


@needs_git
def test_failing_command_raises_with_stderr(tmp_path: Path) -> None:
    runner = GitRunner(timeout=30)
    with pytest.raises(GitError) as excinfo:
        runner.run(["rev-parse", "HEAD"], cwd=tmp_path)
    assert excinfo.value.returncode != 0
    assert excinfo.value.stderr


@needs_git
def test_check_false_returns_result(tmp_path: Path) -> None:
    result = GitRunner().run(["rev-parse", "HEAD"], cwd=tmp_path, check=False)
    assert not result.ok
    assert result.returncode != 0


@needs_git
def test_timeout_kills_and_is_flagged(tmp_path: Path) -> None:
    """A hung git must die on schedule -- the original failure mode we fix."""
    runner = GitRunner(timeout=1.0)
    started = time.monotonic()
    with pytest.raises(GitError) as excinfo:
        # 203.0.113.0/24 is TEST-NET-3: guaranteed unroutable, so this hangs.
        runner.run(["clone", "https://203.0.113.7/x.git", str(tmp_path / "out")], timeout=1.0)
    elapsed = time.monotonic() - started
    assert excinfo.value.timed_out is True
    assert elapsed < 20  # killed, not waited out


@needs_git
def test_terminal_prompt_disabled(tmp_path: Path) -> None:
    env = GitRunner()._env(authenticated=False)
    assert env["GIT_TERMINAL_PROMPT"] == "0"
    assert env["GIT_ASKPASS"] == ""
    assert env["LC_ALL"] == "C"


def test_token_travels_via_env_not_argv() -> None:
    env = GitRunner(token="s3cr3t-token")._env(authenticated=True)
    keys = [key for key in env if key.startswith("GIT_CONFIG_KEY_")]
    values = [env[key.replace("KEY", "VALUE")] for key in keys]
    assert any(env[key] == "http.extraHeader" for key in keys)
    assert any("s3cr3t-token" in value for value in values)
    assert env["GIT_CONFIG_COUNT"] == str(len(keys))


def test_token_absent_when_unauthenticated() -> None:
    env = GitRunner(token="s3cr3t")._env(authenticated=False)
    assert not any("s3cr3t" in value for value in env.values())


def test_extra_config_appended() -> None:
    env = GitRunner(extra_config=("core.autocrlf=false",))._env(authenticated=False)
    assert env["GIT_CONFIG_KEY_0"] == "core.autocrlf"
    assert env["GIT_CONFIG_VALUE_0"] == "false"


def test_lfs_skipped_by_default() -> None:
    assert GitRunner()._env(authenticated=False)["GIT_LFS_SKIP_SMUDGE"] == "1"
    assert "GIT_LFS_SKIP_SMUDGE" not in GitRunner(lfs=True)._env(authenticated=False)


def test_insecure_sets_ssl_verify_off() -> None:
    env = GitRunner(verify_tls=False)._env(authenticated=False)
    pairs = {
        env[f"GIT_CONFIG_KEY_{i}"]: env[f"GIT_CONFIG_VALUE_{i}"]
        for i in range(int(env["GIT_CONFIG_COUNT"]))
    }
    assert pairs["http.sslVerify"] == "false"


@pytest.mark.parametrize(
    "raw,forbidden",
    [
        ("https://user:hunter2@host/x.git", "hunter2"),
        ("Authorization: token abcdef123456", "abcdef123456"),
    ],
)
def test_redaction(raw: str, forbidden: str) -> None:
    assert forbidden not in redact(raw)


def test_redaction_of_explicit_secret() -> None:
    assert "supersecret" not in redact("leaked supersecret here", "supersecret")


def test_short_secrets_are_not_over_redacted() -> None:
    # too short to be a credential; redacting it would mangle ordinary output
    assert redact("abc def", "abc") == "abc def"


@needs_git
def test_cancel_event_blocks_new_commands(tmp_path: Path) -> None:
    import threading

    cancel = threading.Event()
    cancel.set()
    runner = GitRunner(cancel=cancel)
    with pytest.raises(GitError):
        runner.run(["--version"])
