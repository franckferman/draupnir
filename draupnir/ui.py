"""Terminal rendering.

Kept apart from the engine so the library stays silent when embedded: a caller
supplies its own listener (or none) and nothing is written to stdout.

Honours NO_COLOR, a non-tty stdout, and dumb terminals.
"""

from __future__ import annotations

import os
import shutil
import sys
import threading
from dataclasses import dataclass
from typing import TextIO

from .engine import Event
from .models import ForgeInfo, Outcome, Plan, RunReport, Status

__all__ = ["Console", "ProgressReporter", "human_size", "human_time"]

_COLORS = {
    "reset": "\033[0m",
    "dim": "\033[2m",
    "bold": "\033[1m",
    "red": "\033[31m",
    "green": "\033[32m",
    "yellow": "\033[33m",
    "blue": "\033[34m",
    "magenta": "\033[35m",
    "cyan": "\033[36m",
}

_STATUS_COLOR = {
    Status.CLONED: "green",
    Status.UPDATED: "cyan",
    Status.UNCHANGED: "dim",
    Status.SKIPPED: "dim",
    Status.PLANNED: "blue",
    Status.FAILED: "red",
    Status.CONFLICT: "yellow",
}


def human_size(num_bytes: float) -> str:
    value = float(num_bytes)
    for unit in ("B", "KB", "MB", "GB", "TB"):
        if abs(value) < 1024.0 or unit == "TB":
            return f"{value:.0f} {unit}" if unit in {"B", "KB"} else f"{value:.1f} {unit}"
        value /= 1024.0
    return f"{value:.1f} TB"  # pragma: no cover


def human_time(seconds: float) -> str:
    seconds = max(0.0, float(seconds))
    if seconds < 1:
        return f"{seconds * 1000:.0f}ms"
    if seconds < 60:
        return f"{seconds:.1f}s"
    minutes, rest = divmod(int(seconds), 60)
    if minutes < 60:
        return f"{minutes}m{rest:02d}s"
    hours, minutes = divmod(minutes, 60)
    return f"{hours}h{minutes:02d}m"


@dataclass
class Console:
    """Thread-safe writer with optional ANSI colour."""

    stream: TextIO = sys.stderr
    color: bool | None = None  # None -> autodetect
    verbosity: int = 0  # -1 quiet, 0 normal, 1 verbose

    def __post_init__(self) -> None:
        if self.color is None:
            self.color = self._supports_color()
        self._lock = threading.Lock()

    def _supports_color(self) -> bool:
        if os.environ.get("NO_COLOR") is not None:
            return False
        if os.environ.get("FORCE_COLOR"):
            return True
        if os.environ.get("TERM", "") == "dumb":
            return False
        return bool(getattr(self.stream, "isatty", lambda: False)())

    def paint(self, text: str, *styles: str) -> str:
        if not self.color or not styles:
            return text
        prefix = "".join(_COLORS.get(style, "") for style in styles)
        return f"{prefix}{text}{_COLORS['reset']}" if prefix else text

    def write(self, text: str = "", *, level: int = 0) -> None:
        if level > self.verbosity:
            return
        with self._lock:
            self.stream.write(f"{text}\n")
            self.stream.flush()

    def error(self, text: str) -> None:
        with self._lock:
            self.stream.write(f"{self.paint('error', 'red', 'bold')}: {text}\n")
            self.stream.flush()

    def warn(self, text: str) -> None:
        if self.verbosity < 0:
            return
        with self._lock:
            self.stream.write(f"{self.paint('warning', 'yellow')}: {text}\n")
            self.stream.flush()

    @property
    def width(self) -> int:
        try:
            return max(60, min(shutil.get_terminal_size((100, 24)).columns, 160))
        except OSError:  # pragma: no cover
            return 100


class ProgressReporter:
    """Turns engine events into human output."""

    def __init__(self, console: Console, *, show_unchanged: bool = False) -> None:
        self.console = console
        self.show_unchanged = show_unchanged
        self.total = 0

    def __call__(self, event: Event) -> None:
        handler = getattr(self, f"_on_{event.kind}", None)
        if handler is not None:
            handler(event)

    # -- event handlers --------------------------------------------------

    def _on_probe(self, event: Event) -> None:
        self.console.write(self.console.paint(f"  {event.message}", "dim"), level=1)

    def _on_discovered(self, event: Event) -> None:
        info = event.payload
        source = ""
        if isinstance(info, ForgeInfo):
            source = f" [{info.describe()}]" if info.api else " [HTML fallback: API unavailable]"
        self.console.write(
            f"  {self.console.paint(str(event.total), 'bold')} repositories discovered"
            f"{self.console.paint(source, 'dim')}"
        )

    def _on_planned(self, event: Event) -> None:
        plan = event.payload
        self.total = event.total
        if not isinstance(plan, Plan):
            return
        skipped = len(plan.excluded)
        line = f"  {self.console.paint(str(event.total), 'bold')} to sync"
        if skipped:
            reasons = ", ".join(f"{count} {name}" for name, count in plan.reasons().items())
            line += self.console.paint(f"  ({skipped} filtered: {reasons})", "dim")
        self.console.write(line)
        self.console.write()

    def _on_repo_done(self, event: Event) -> None:
        outcome = event.outcome
        if outcome is None:
            return
        hidden = outcome.status is Status.UNCHANGED and not self.show_unchanged
        if hidden and self.console.verbosity < 1:
            return
        self.console.write(self._line(outcome, event.index, event.total))

    def _on_pruned(self, event: Event) -> None:
        self.console.write(
            f"  {self.console.paint('pruned', 'yellow')}   {event.message} "
            f"{self.console.paint('-> .draupnir-pruned/', 'dim')}"
        )

    # -- formatting ------------------------------------------------------

    def _line(self, outcome: Outcome, index: int, total: int) -> str:
        width = len(str(total)) if total else 3
        counter = self.console.paint(f"[{index:>{width}}/{total}]", "dim")
        status = outcome.status.value
        painted = self.console.paint(f"{status:<9}", _STATUS_COLOR.get(outcome.status, ""))
        name = outcome.repo.full_name

        extra: list[str] = []
        if outcome.commits_ahead:
            plural = "s" if outcome.commits_ahead > 1 else ""
            extra.append(f"+{outcome.commits_ahead} commit{plural}")
        elif outcome.status is Status.CLONED and outcome.repo.size_kb:
            extra.append(human_size(outcome.repo.size_kb * 1024))
        if outcome.detail:
            extra.append(outcome.detail)
        if outcome.wiki and outcome.wiki not in {"absent"}:
            extra.append(f"wiki: {outcome.wiki}")
        if outcome.attempts > 1:
            extra.append(f"{outcome.attempts} attempts")

        suffix = ""
        if extra:
            joined = "  ".join(extra)
            style = "red" if outcome.status is Status.FAILED else "dim"
            suffix = "  " + self.console.paint(f"({joined})", style)
        return f"  {counter} {painted} {name}{suffix}"


def render_summary(console: Console, report: RunReport) -> None:
    """Final block: counts, failures, totals."""
    counts = report.counts()
    console.write()

    if report.interrupted:
        console.write(console.paint("  interrupted -- partial run", "yellow", "bold"))

    order = [
        (Status.CLONED, "green"),
        (Status.UPDATED, "cyan"),
        (Status.UNCHANGED, "dim"),
        (Status.PLANNED, "blue"),
        (Status.SKIPPED, "dim"),
        (Status.CONFLICT, "yellow"),
        (Status.FAILED, "red"),
    ]
    parts = [
        console.paint(f"{counts[status.value]} {status.value}", style)
        for status, style in order
        if counts.get(status.value)
    ]
    headline = "  " + console.paint("summary", "bold") + "  " + "  ".join(parts)
    console.write(headline)

    if report.bytes_synced:
        console.write(
            console.paint(
                f"          {human_size(report.bytes_synced)} of repository data touched", "dim"
            )
        )
    console.write(
        console.paint(
            f"          {report.discovered} discovered  "
            f"{report.planned} planned  in {human_time(report.duration)}",
            "dim",
        )
    )

    failures = report.failures
    if failures:
        console.write()
        console.write(console.paint(f"  {len(failures)} problem(s):", "red", "bold"))
        for outcome in failures[:20]:
            console.write(
                f"    {console.paint(outcome.status.value, 'red')} "
                f"{outcome.repo.full_name}: {outcome.detail}"
            )
        if len(failures) > 20:
            console.write(console.paint(f"    ... and {len(failures) - 20} more", "dim"))
    if report.output:
        console.write()
        console.write(console.paint(f"  mirror: {report.output}", "dim"))
