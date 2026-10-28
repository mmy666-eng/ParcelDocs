import json
import os
import time
from datetime import date
from pathlib import Path

import pytest

from app.models import Document, DocumentKind, Plan
from app.output.library import LibraryError, PlansLibrary
from app.utils.filenames import path_from_file_uri


def _plan(number: str = "501-0873794", aliases: list[str] | None = None) -> Plan:
    return Plan(
        number=number,
        name="חדרי יציאה לגג",
        status="אישור",
        source="iplan+rmi",
        aliases=aliases or [],
        approval_date=date(2023, 1, 26),
    )


def _doc(kind: DocumentKind, title: str) -> Document:
    return Document(
        title=title, url=f"https://apps.land.gov.il/{title}.pdf", source="rmi", kind=kind
    )


def _file(tmp_path: Path, name: str, content: bytes) -> Path:
    path = tmp_path / "project" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return path


def test_add_then_documents(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    instructions = _file(tmp_path, "הוראות.pdf", b"%PDF instructions")
    plan_map = _file(tmp_path, "תשריט.pdf", b"%PDF map")

    added = library.add(
        _plan(),
        [
            (_doc(DocumentKind.INSTRUCTIONS, "תקנון סרוק"), instructions, "הוראות.pdf"),
            (_doc(DocumentKind.MAP, "תשריט מצב מוצע"), plan_map, "תשריט.pdf"),
        ],
    )

    assert added == 2
    entry = json.loads((tmp_path / "lib" / "501-0873794" / "plan.json").read_text("utf-8"))
    assert [f["path"] for f in entry["files"]] == ["הוראות.pdf", "תשריט.pdf"]
    assert entry["files"][0]["url"] == "https://apps.land.gov.il/תקנון סרוק.pdf"
    documents = library.documents(_plan())
    assert [(d.kind, d.source, d.origin_source) for d in documents] == [
        (DocumentKind.INSTRUCTIONS, "library", "rmi"),
        (DocumentKind.MAP, "library", "rmi"),
    ]
    assert path_from_file_uri(documents[1].url).read_bytes() == b"%PDF map"
    assert not (tmp_path / "lib" / "501-0873794" / ".lock").exists()


def test_same_file_is_not_added_twice_and_name_clash_gets_suffix(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    first = _file(tmp_path, "a/תשריט.pdf", b"%PDF version 1")
    second = _file(tmp_path, "b/תשריט.pdf", b"%PDF version 2")
    doc = _doc(DocumentKind.MAP, "תשריט")

    assert library.add(_plan(), [(doc, first, "תשריט.pdf")]) == 1
    assert library.add(_plan(), [(doc, first, "תשריט.pdf")]) == 0
    assert library.add(_plan(), [(doc, second, "תשריט.pdf")]) == 1

    folder = tmp_path / "lib" / "501-0873794"
    assert (folder / "תשריט.pdf").read_bytes() == b"%PDF version 1"
    assert (folder / "תשריט_2.pdf").read_bytes() == b"%PDF version 2"


def test_found_by_alias_and_folder_names_are_safe(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    path = _file(tmp_path, "הוראות.pdf", b"%PDF")
    library.add(_plan("בב/105א"), [(_doc(DocumentKind.INSTRUCTIONS, "x"), path, "הוראות.pdf")])

    assert (tmp_path / "lib" / "בב-105א" / "הוראות.pdf").is_file()
    # The same plan under its city number, with the rmi number as an alias.
    assert len(library.documents(_plan("105/א", aliases=["בב/105א"]))) == 1
    assert library.documents(_plan("105/ב")) == []


def test_missing_library_file_is_skipped(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    path = _file(tmp_path, "הוראות.pdf", b"%PDF")
    library.add(_plan(), [(_doc(DocumentKind.INSTRUCTIONS, "x"), path, "הוראות.pdf")])
    (tmp_path / "lib" / "501-0873794" / "הוראות.pdf").unlink()

    assert library.documents(_plan()) == []


def test_lock_held_by_another_user_times_out_and_stale_lock_is_removed(tmp_path: Path):
    folder = tmp_path / "lib" / "501-0873794"
    folder.mkdir(parents=True)
    lock = folder / ".lock"
    lock.write_text("someone else")
    path = _file(tmp_path, "הוראות.pdf", b"%PDF")
    doc = _doc(DocumentKind.INSTRUCTIONS, "x")

    busy = PlansLibrary(tmp_path / "lib", lock_timeout_seconds=0.3)
    with pytest.raises(LibraryError):
        busy.add(_plan(), [(doc, path, "הוראות.pdf")])

    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert PlansLibrary(tmp_path / "lib").add(_plan(), [(doc, path, "הוראות.pdf")]) == 1
    assert not lock.exists()
