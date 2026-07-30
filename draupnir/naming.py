"""Remote names -> filesystem paths.

Owner and repo names are attacker-controlled on a public forge (anyone can
register). Every component is validated before it touches a Path, and the final
location is re-checked against the root so no clone can land outside it.
"""

from __future__ import annotations

import os
import re
import unicodedata
from pathlib import Path, PurePosixPath

from .errors import UnsafeNameError

__all__ = ["LAYOUTS", "relative_repo_path", "repo_path", "safe_component"]

LAYOUTS = ("owner", "flat")

_MAX_COMPONENT = 96
# Reserved device names on Windows -- harmless on Linux, fatal on a shared or
# exported mirror, so refuse them everywhere for portable output trees.
_WINDOWS_RESERVED = frozenset(
    {"con", "prn", "aux", "nul", "clock$"}
    | {f"com{n}" for n in range(1, 10)}
    | {f"lpt{n}" for n in range(1, 10)}
)
_ILLEGAL = re.compile(r'[\x00-\x1f\x7f<>:"|?*\\/]')


def safe_component(raw: str, *, kind: str = "name") -> str:
    """Validate one path component. Returns it unchanged or raises.

    Deliberately rejects rather than rewrites: a silently mangled name would
    make the local tree disagree with the forge, and two different remote repos
    could collapse onto one directory.
    """
    if not isinstance(raw, str):
        raise UnsafeNameError(f"{kind} is not a string: {raw!r}")

    value = raw.strip()
    if not value:
        raise UnsafeNameError(f"empty {kind}")
    if value in {".", ".."}:
        raise UnsafeNameError(f"{kind} is a path traversal component: {raw!r}")
    if _ILLEGAL.search(value):
        raise UnsafeNameError(f"{kind} contains an illegal character: {raw!r}")
    if value != raw:
        raise UnsafeNameError(f"{kind} has leading/trailing whitespace: {raw!r}")
    if value.endswith("."):
        raise UnsafeNameError(f"{kind} ends with a dot: {raw!r}")
    if value.lower() in _WINDOWS_RESERVED:
        raise UnsafeNameError(f"{kind} is a reserved device name: {raw!r}")
    if len(value.encode("utf-8")) > _MAX_COMPONENT:
        raise UnsafeNameError(f"{kind} is too long ({len(value)} chars): {raw!r}")
    # Unicode direction overrides can make a name render as something else.
    if any(unicodedata.category(char) == "Cf" for char in value):
        raise UnsafeNameError(f"{kind} contains bidi/format control characters: {raw!r}")
    return value


def relative_repo_path(owner: str, name: str, *, layout: str = "owner") -> PurePosixPath:
    """owner/name under the chosen layout, still relative."""
    if layout not in LAYOUTS:
        raise UnsafeNameError(f"unknown layout: {layout!r}")
    safe_owner = safe_component(owner, kind="owner")
    safe_name = safe_component(name, kind="repository")
    if layout == "flat":
        return PurePosixPath(f"{safe_owner}__{safe_name}")
    return PurePosixPath(safe_owner) / safe_name


def repo_path(root: Path, owner: str, name: str, *, layout: str = "owner") -> Path:
    """Absolute destination for a repo, proven to sit inside root."""
    root = Path(root).expanduser()
    target = root / relative_repo_path(owner, name, layout=layout)

    # Compare without resolve() on the target: it may not exist yet, and on a
    # symlinked root resolve() would rewrite the prefix too. Normalising both
    # sides the same way keeps the check honest.
    root_norm = Path(os.path.normpath(str(root.absolute())))
    target_norm = Path(os.path.normpath(str(target.absolute())))
    if root_norm != target_norm and root_norm not in target_norm.parents:
        raise UnsafeNameError(f"{owner}/{name} escapes the output directory")
    return target_norm
