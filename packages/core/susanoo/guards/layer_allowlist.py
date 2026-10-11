"""Layer path allow-list. [PRD › Improvement loop and guardrails] [PRD › Repository structure]

The Fixer may only edit files that belong to the layer the Diagnostician blamed:

    code          -> backend/, web/
    tool          -> agent/tools.py
    prompt_policy -> agent/prompt.md, agent/policy.yaml
    test          -> scenarios/train/   (the graph also pauses for human approval)

`scenarios/heldout/` is a hard wall: no layer and no operation may touch it, and the
Diagnostician and the Fixer may not even read it. All paths are repo-relative POSIX
strings. Entries ending in "/" are folders, the rest are exact files.
"""

from __future__ import annotations

import unicodedata
from collections.abc import Iterable
from pathlib import PurePosixPath

from susanoo.schemas import Layer

ALLOWED: dict[Layer, tuple[str, ...]] = {
    "code": ("backend/", "web/"),
    "tool": ("agent/tools.py",),
    "prompt_policy": ("agent/prompt.md", "agent/policy.yaml"),
    "test": ("scenarios/train/",),
}

HELDOUT_PARTS = ("scenarios", "heldout")
# Heads that must never read the held-out set (it would let them tune to the exam).
HELDOUT_BLIND_HEADS = frozenset({"diagnostician", "fixer"})


class OutsideLayer(Exception):
    """A path is not allowed for the diagnosed layer (or is off limits altogether)."""

    def __init__(self, layer: str, path: str, message: str) -> None:
        self.layer = layer
        self.path = path
        super().__init__(message)


def allowed_paths(layer: Layer) -> list[str]:
    """The allow-list for `layer`, in the form `coding_worker.run_change(allowed_paths=...)` takes."""
    try:
        return list(ALLOWED[layer])
    except KeyError:
        raise ValueError(f"Unknown layer {layer!r}; expected one of {sorted(ALLOWED)}") from None


def _fold(part: str) -> str:
    # Case-insensitive file systems (macOS, Windows) also ignore trailing dots and spaces.
    return unicodedata.normalize("NFKC", part).casefold().rstrip(" .")


def is_heldout(path: str) -> bool:
    """True if `path` points into `scenarios/heldout/` (any case, any nesting, any slash style).

    Deliberately lenient about odd input: it is the check that must never be fooled.
    """
    raw = [p for p in path.replace("\\", "/").split("/") if p not in ("", ".")]
    parts = [p if p == ".." else _fold(p) for p in raw]
    resolved: list[str] = []  # the same path with ".." applied, in case the caller resolves it later
    for part in parts:
        if part == "..":
            if resolved:
                resolved.pop()
        else:
            resolved.append(part)
    return _has_heldout(parts) or _has_heldout(resolved)


def _has_heldout(parts: list[str]) -> bool:
    n = len(HELDOUT_PARTS)
    return any(tuple(parts[i : i + n]) == HELDOUT_PARTS for i in range(len(parts) - n + 1))


def _clean(path: str) -> str:
    """Normalise a repo-relative path, or raise ValueError saying what is wrong."""
    if not isinstance(path, str) or path.strip() == "":
        raise ValueError("the path is empty")
    if any(ord(c) < 32 or ord(c) == 127 for c in path):
        raise ValueError("the path contains control characters")
    if "\\" in path:
        raise ValueError("the path contains a backslash")
    posix = PurePosixPath(path)
    if posix.is_absolute():
        raise ValueError("the path is absolute, it must be relative to the repo")
    if ".." in posix.parts:
        raise ValueError("the path climbs out of its folder with '..'")
    if str(posix) == ".":
        raise ValueError("the path is empty")
    return str(posix)


def _matches(path: str, entry: str) -> bool:
    if entry.endswith("/"):
        return path.startswith(entry)
    return path == entry


def check_paths(layer: Layer, paths: Iterable[str]) -> None:
    """Raise OutsideLayer for the first path the `layer` is not allowed to touch.

    Applies to every operation (read, edit, create, delete, rename, both sides of a rename).
    """
    allowed = allowed_paths(layer)  # raises ValueError for an unknown layer
    for raw in paths:
        shown = raw if isinstance(raw, str) else repr(raw)
        if isinstance(raw, str) and is_heldout(raw):
            raise OutsideLayer(layer, shown, f"'{shown}' is in the held-out test set, which no fix may touch.")
        try:
            path = _clean(raw)
        except ValueError as exc:
            raise OutsideLayer(layer, shown, f"'{shown}' is not a valid repo path: {exc}.") from None
        if is_heldout(path):  # defence in depth after normalisation
            raise OutsideLayer(layer, shown, f"'{shown}' is in the held-out test set, which no fix may touch.")
        if any(part.casefold() == ".git" for part in path.split("/")):
            raise OutsideLayer(layer, shown, f"'{shown}' is inside a git folder, which no fix may touch.")
        if not any(_matches(path, entry) for entry in allowed):
            raise OutsideLayer(
                layer,
                shown,
                f"'{shown}' is outside the files a '{layer}' fix may change (allowed: {', '.join(allowed)}).",
            )


def is_readable_by(head: str, path: str) -> bool:
    """May `head` read `path`? The Diagnostician and the Fixer may never read the held-out set.

    A path they cannot be shown to be clean (absolute, '..', backslash, control characters)
    is treated as unreadable for those two heads.
    """
    if head.strip().casefold() not in HELDOUT_BLIND_HEADS:
        return True
    if is_heldout(path):
        return False
    try:
        _clean(path)
    except ValueError:
        return False
    return True
