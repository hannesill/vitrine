"""Find the line of agent code that is making a card."""

from __future__ import annotations

import hashlib
import linecache
import os
import sys
from dataclasses import dataclass
from pathlib import Path
from types import FrameType

from vitrine._types import CodeOrigin

# Scripts larger than this are referenced by path and line only.
_MAX_SOURCE_BYTES = 1_000_000

_PACKAGE_PREFIXES = tuple(
    {
        str(Path(__file__).parent) + os.sep,
        str(Path(__file__).resolve().parent) + os.sep,
    }
)


@dataclass
class CallingCode:
    """The code that called into Vitrine, and its source when readable."""

    origin: CodeOrigin
    source: bytes | None
    suffix: str


def calling_code(project_root: Path | None) -> CallingCode | None:
    """Describe the first frame outside Vitrine on the current call stack.

    ``show()`` reached through ``confirm()``, ``ask()`` or ``progress()``
    reports the agent's line, not Vitrine's. The source is read from the
    file, or from ``linecache`` for code without one (a notebook cell);
    code run from stdin or ``python -c`` has neither, so it gets a path and
    line but no hash.
    """
    frame: FrameType | None = sys._getframe(1)
    while frame is not None and frame.f_code.co_filename.startswith(_PACKAGE_PREFIXES):
        frame = frame.f_back
    if frame is None:
        return None
    filename = frame.f_code.co_filename
    source = _read_source(filename, frame.f_globals)
    origin = CodeOrigin(
        path=_display_path(filename, project_root),
        line=frame.f_lineno,
        sha256=hashlib.sha256(source).hexdigest() if source is not None else None,
    )
    return CallingCode(
        origin=origin, source=source, suffix=Path(filename).suffix or ".txt"
    )


def _read_source(filename: str, module_globals: dict) -> bytes | None:
    path = Path(filename)
    try:
        if path.is_file():
            if path.stat().st_size > _MAX_SOURCE_BYTES:
                return None
            return path.read_bytes()
    except OSError:
        return None
    lines = linecache.getlines(filename, module_globals)
    if not lines:
        return None
    source = "".join(lines).encode()
    return source if len(source) <= _MAX_SOURCE_BYTES else None


def _display_path(filename: str, project_root: Path | None) -> str:
    if filename.startswith("<"):
        return filename
    path = Path(filename)
    if project_root is not None:
        try:
            return path.resolve().relative_to(project_root.resolve()).as_posix()
        except (OSError, ValueError):
            pass
    return str(path.resolve())
