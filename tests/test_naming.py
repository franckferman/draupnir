"""Path mapping must never let a remote name escape the output tree."""

from __future__ import annotations

from pathlib import Path

import pytest

from draupnir.errors import UnsafeNameError
from draupnir.naming import relative_repo_path, repo_path, safe_component


@pytest.mark.parametrize(
    "value",
    [
        "..",
        ".",
        "../etc",
        "a/b",
        "a\\b",
        "with\x00null",
        "tab\there",
        "new\nline",
        "",
        "   ",
        " leading",
        "trailing ",
        "ends.",
        "con",
        "COM1",
        "nul",
        "a" * 200,
        "we‮wolf",  # bidi override -> renders as something else
        'quo"te',
        "pipe|",
        "star*",
        "quest?",
        "colon:",
    ],
)
def test_rejects_dangerous_components(value: str) -> None:
    with pytest.raises(UnsafeNameError):
        safe_component(value)


@pytest.mark.parametrize(
    "value",
    ["repo", "Repo-Name", "under_score", "dot.name", "a.b.c", "ünicode", "日本語", "x", "-dash"],
)
def test_accepts_ordinary_components(value: str) -> None:
    assert safe_component(value) == value


def test_traversal_cannot_escape_root(tmp_path: Path) -> None:
    for owner, name in (("..", "evil"), ("ok", ".."), ("../..", "x"), ("/abs", "y")):
        with pytest.raises(UnsafeNameError):
            repo_path(tmp_path, owner, name)


def test_owner_layout(tmp_path: Path) -> None:
    result = repo_path(tmp_path, "alice", "tool")
    assert result == (tmp_path / "alice" / "tool").absolute()
    assert result.is_relative_to(tmp_path.absolute())


def test_flat_layout_collapses_to_one_level(tmp_path: Path) -> None:
    result = repo_path(tmp_path, "alice", "tool", layout="flat")
    assert result.name == "alice__tool"
    assert result.parent == tmp_path.absolute()


def test_unknown_layout_rejected(tmp_path: Path) -> None:
    with pytest.raises(UnsafeNameError):
        repo_path(tmp_path, "alice", "tool", layout="nope")


def test_relative_path_is_posix() -> None:
    assert str(relative_repo_path("a", "b")) == "a/b"
    assert str(relative_repo_path("a", "b", layout="flat")) == "a__b"


def test_result_is_absolute_and_normalised(tmp_path: Path) -> None:
    messy = tmp_path / "sub" / ".." / "root"
    (tmp_path / "root").mkdir(parents=True)
    result = repo_path(messy, "o", "n")
    assert ".." not in str(result)
    assert result.is_absolute()
