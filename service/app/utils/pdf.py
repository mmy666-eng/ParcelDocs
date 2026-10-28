"""What the mavat step needs to know about a downloaded PDF: first-page text and page size.

Plan PDFs from the planning systems are encrypted with an owner (permissions) password
only, so they open with an empty user password. In maps, pypdf returns some two-byte
strings as Latin-1 pairs ("\\x05Ý" for ם); those lines are decoded again as UTF-16BE.
Hebrew in maps is often stored in visual order, so callers also look for reversed words.
"""

import logging
from pathlib import Path

from pydantic import BaseModel
from pypdf import PdfReader

from app.utils.filenames import long_path

log = logging.getLogger(__name__)

# A4 is 595 x 842 pt and A3 842 x 1191 pt; plan maps are larger.
LARGE_PAGE_PT = 1190.0


class PdfInfo(BaseModel):
    pages: int
    text: str
    max_side_pt: float


def read_pdf(path: Path, pages: int = 2) -> PdfInfo | None:
    """Text of the first pages and the largest page side, or None when unreadable."""
    try:
        reader = PdfReader(str(long_path(path)))
        if reader.is_encrypted:
            reader.decrypt("")
        texts = []
        max_side = 0.0
        for page in reader.pages[:pages]:
            box = page.mediabox
            max_side = max(max_side, float(box.width), float(box.height))
            texts.append(repair_text(page.extract_text() or ""))
        return PdfInfo(pages=len(reader.pages), text="\n".join(texts), max_side_pt=max_side)
    except Exception as exc:  # pypdf raises many types on damaged or unusual files
        log.warning("cannot read PDF %s: %s", path.name, exc)
        return None


def repair_text(text: str) -> str:
    return "\n".join(_repair_line(line) for line in text.split("\n"))


def _repair_line(line: str) -> str:
    if len(line) < 2 or any(ord(c) > 0xFF for c in line):
        return line
    high_bytes = line[0::2]
    if sum(c in "\x00\x05" for c in high_bytes) < 0.8 * len(high_bytes):
        return line
    raw = line.encode("latin-1")
    if len(raw) % 2:
        raw = raw[:-1]
    return raw.decode("utf-16-be", errors="replace")


def contains(text: str, phrase: str) -> bool:
    """phrase in logical or visual (reversed) order."""
    return phrase in text or phrase[::-1] in text
