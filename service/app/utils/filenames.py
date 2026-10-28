"""Safe Windows file and folder names for Hebrew text (PRD §4.5)."""

import os
import re
from pathlib import Path

_INVALID_CHARS = re.compile(r'[<>:"/\\|?*\x00-\x1f]')
_WHITESPACE = re.compile(r"\s+")
_REPEATED_UNDERSCORE = re.compile(r"_{2,}")
_RESERVED_NAMES = {"CON", "PRN", "AUX", "NUL"} | {
    f"{p}{i}" for p in ("COM", "LPT") for i in range(1, 10)
}

# Windows MAX_PATH is 260; leave headroom before switching to the extended-length prefix.
_LONG_PATH_THRESHOLD = 240


def safe_name(text: str, max_length: int = 80) -> str:
    """Turn arbitrary text into a single valid path component.

    Hebrew double quotes (as in תמ"א) become gershayim, slashes in plan numbers
    become dashes, whitespace becomes underscores, and the result is shortened
    to max_length characters while keeping any file extension.
    """
    name = text.strip().replace('"', "״").replace("/", "-").replace("\\", "-")
    name = _INVALID_CHARS.sub("_", name)
    name = _WHITESPACE.sub("_", name)
    name = _REPEATED_UNDERSCORE.sub("_", name)
    name = name.strip(" ._")
    name = _shorten(name, max_length).rstrip(" ._")
    if not name:
        name = "_"
    if name.split(".")[0].upper() in _RESERVED_NAMES:
        name = f"_{name}"
    return name


def _shorten(name: str, max_length: int) -> str:
    if len(name) <= max_length:
        return name
    stem, dot, ext = name.rpartition(".")
    if dot and 0 < len(ext) <= 5:
        return stem[: max_length - len(ext) - 1].rstrip(" ._") + "." + ext
    return name[:max_length]


def long_path(path: Path) -> Path:
    """Return a path usable for file operations even when longer than MAX_PATH on Windows."""
    absolute = path.resolve()
    text = str(absolute)
    if os.name != "nt" or len(text) < _LONG_PATH_THRESHOLD or text.startswith("\\\\?\\"):
        return absolute
    if text.startswith("\\\\"):
        return Path("\\\\?\\UNC\\" + text[2:])
    return Path("\\\\?\\" + text)
