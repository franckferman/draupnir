"""Machine-readable output and tabular renderers."""

from __future__ import annotations

import json
import os
import tempfile
from collections.abc import Iterable, Sequence
from pathlib import Path
from typing import Any

from .models import Repo, RunReport
from .ui import Console, human_size

__all__ = ["render_repo_table", "repos_as_json", "write_json"]


def write_json(path: Path, payload: Any) -> None:
    """Atomically write JSON so a reader never sees a truncated file."""
    path = Path(path).expanduser()
    path.parent.mkdir(parents=True, exist_ok=True)
    handle = tempfile.NamedTemporaryFile(  # noqa: SIM115 - closed by the with-block below
        "w",
        encoding="utf-8",
        dir=str(path.parent),
        prefix=f".{path.name}.",
        suffix=".tmp",
        delete=False,
    )
    try:
        with handle as stream:
            json.dump(payload, stream, indent=2, sort_keys=False)
            stream.write("\n")
            stream.flush()
            os.fsync(stream.fileno())
        os.replace(handle.name, str(path))
    except BaseException:
        try:
            os.unlink(handle.name)
        except OSError:
            pass
        raise


def repos_as_json(repos: Iterable[Repo]) -> list[dict[str, Any]]:
    return [repo.to_dict() for repo in repos]


def render_repo_table(console: Console, repos: Sequence[Repo], *, show_url: bool = False) -> None:
    """Aligned listing for `draupnir list`."""
    if not repos:
        console.write("  no repositories")
        return

    name_width = min(max((len(r.full_name) for r in repos), default=10) + 2, 52)
    console.write()
    header = f"  {'REPOSITORY':<{name_width}} {'SIZE':>9}  {'★':>4}  FLAGS"
    console.write(console.paint(header, "bold"))
    console.write(console.paint("  " + "-" * (name_width + 26), "dim"))

    for repo in repos:
        flags = []
        if repo.private:
            flags.append("private")
        if repo.fork:
            flags.append("fork")
        if repo.mirror:
            flags.append("mirror")
        if repo.archived:
            flags.append("archived")
        if repo.template:
            flags.append("template")
        if repo.empty:
            flags.append("empty")
        if repo.partial:
            flags.append("partial-metadata")

        size = human_size(repo.size_kb * 1024) if repo.size_kb else "-"
        name = repo.full_name
        if len(name) > name_width - 1:
            name = name[: name_width - 2] + "…"
        line = f"  {name:<{name_width}} {size:>9}  {repo.stars or '-':>4}  "
        line += console.paint(",".join(flags), "dim") if flags else ""
        console.write(line.rstrip())
        if show_url:
            console.write(console.paint(f"    {repo.clone_url}", "dim"))

    total_kb = sum(repo.size_kb for repo in repos)
    console.write()
    console.write(
        console.paint(
            f"  {len(repos)} repositories, {human_size(total_kb * 1024)} total", "bold"
        )
    )


def summarise_for_json(report: RunReport) -> dict[str, Any]:
    return report.to_dict()
