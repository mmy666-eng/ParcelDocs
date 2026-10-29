import hashlib
import json
from datetime import datetime
from pathlib import Path

from app.library import migrate
from app.models import Document, DocumentKind, Plan, PlanScope
from app.office_import import import_office_files
from app.output.library import NewFile
from app.output.office_library import LibraryUpdate, OfficeLibrary
from app.output.plan_scope import ScopeBasis, ScopeDecision
from app.pipeline import choose_documents
from tests.conftest import make_pdf

A0 = (3370, 2384)


def _library(tmp_path: Path) -> OfficeLibrary:
    (tmp_path / "share").mkdir(exist_ok=True)
    library = OfficeLibrary(tmp_path / "share", tmp_path / "local", tmp_path / "index.sqlite")
    library.open()
    return library


def _write(path: Path, data: bytes) -> Path:
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(data)
    return path


def _tree(root: Path) -> dict[str, str]:
    return {
        p.relative_to(root).as_posix(): hashlib.sha256(p.read_bytes()).hexdigest()
        for p in sorted(root.rglob("*"))
        if p.is_file()
    }


def _sources(tmp_path: Path) -> list[Path]:
    first, second = tmp_path / "jobs" / "00-תב''ע", tmp_path / "jobs" / "00תבע"
    _write(first / "105.pdf", make_pdf("", *A0))
    _write(first / "107א.pdf", make_pdf("scan 107a", *A0))
    _write(second / "107.pdf", make_pdf("scan 107a", *A0))  # the same file, another name
    _write(first / "185.pdf", make_pdf("scan 185", *A0))
    _write(second / "בב 185.pdf", make_pdf("scan 185", *A0))
    _write(first / "הוראות בב 93.pdf", make_pdf("93"))
    _write(first / "181.pdf", make_pdf("scan 181", *A0))
    _write(first / "1 3.pdf", make_pdf("?"))
    # A map sheet that names its national number; the file name has the local one.
    _write(first / "819.pdf", make_pdf("tochnit 501-0246629 bb/819", *A0))
    _write(first / "notes.dwg", b"AC1027")
    _write(first / "הירקון 501-0282152" / "5005134876.pdf", make_pdf("plan 501-0282152"))
    _write(second / "ערד" / "שכונת טללים" / "תקנון (1).PDF", make_pdf("arad"))
    return [first, second]


def test_office_files_are_copied_by_plan_number_and_the_rest_is_reported(tmp_path: Path):
    sources = _sources(tmp_path)
    before = {source: _tree(source) for source in sources}
    library = _library(tmp_path)
    instructions = _write(tmp_path / "p" / "הוראות.pdf", b"%PDF rmi")
    library.record(
        Plan(number="181", name="", status="", source="rmi", city="בני ברק"),
        LibraryUpdate(
            decision=ScopeDecision(scope=PlanScope.SPECIFIC, basis=ScopeBasis.DEFAULT, note="")
        ),  # fmt: skip
        [
            NewFile(
                local_path=instructions,
                path="הוראות.pdf",
                kind=DocumentKind.INSTRUCTIONS,
                title="תקנון",
                source="rmi",
                url="https://apps.land.gov.il/x",
            )
        ],  # fmt: skip
        [],
    )

    assert import_office_files(library, sources, "בני ברק", dry_run=True).imported
    assert not (tmp_path / "share" / "בני_ברק" / "105").exists()
    report = import_office_files(library, sources, "בני ברק")

    # The source folders are only read.
    assert {source: _tree(source) for source in sources} == before
    imported = {item.folder: item for item in report.imported}
    assert sorted(imported) == [
        "בני_ברק/105",
        "בני_ברק/181",
        "בני_ברק/185",
        "בני_ברק/501-0246629",
        "בני_ברק/501-0282152",
        "בני_ברק/93",
    ]
    assert imported["בני_ברק/501-0246629"].kind == DocumentKind.MAP
    by_local = library.find(Plan(number="819", name="", status="", source="bnei_brak",
                                 city="בני ברק"))  # fmt: skip
    assert by_local is not None and by_local.folder == "בני_ברק/501-0246629"
    assert imported["בני_ברק/185"].files == ["00-תב''ע/185.pdf", "00תבע/בב 185.pdf"]
    assert imported["בני_ברק/105"].kind == DocumentKind.MAP
    assert imported["בני_ברק/93"].kind == DocumentKind.INSTRUCTIONS
    reasons = {item.shown: item.reason for item in report.skipped}
    assert reasons["00-תב''ע/107א.pdf"].startswith("השמות של אותו קובץ סותרים")
    assert reasons["00תבע/107.pdf"] == reasons["00-תב''ע/107א.pdf"]
    assert reasons["00-תב''ע/1 3.pdf"] == "לא זוהה מספר תכנית"
    assert reasons["00תבע/ערד/שכונת טללים/תקנון (1).PDF"] == "לא זוהה מספר תכנית"
    assert reasons["00-תב''ע/notes.dwg"] == "לא PDF (.dwg)"

    # An existing plan gets the office copy beside its documents from a source.
    entry = json.loads((tmp_path / "share" / "בני_ברק" / "181" / "plan.json").read_text("utf-8"))
    assert sorted((f["path"], f["source"]) for f in entry["files"]) == [
        ("הוראות.pdf", "rmi"),
        ("משרד/181.pdf", "office"),
    ]
    assert entry["files"][1]["url"].startswith("file:")
    assert not library.find(Plan(number="181", name="", status="", source="x",
                                 city="בני ברק")).plan.complete()  # fmt: skip
    # Running it again adds nothing.
    library_before = _tree(tmp_path / "share" / "בני_ברק")
    again = import_office_files(library, sources, "בני ברק")
    assert {item.note for item in again.imported} == {"כבר יובא"}
    assert _tree(tmp_path / "share" / "בני_ברק") == library_before
    assert "יובאו: 6 קבצים שונים (7 עותקים) ל-6 תכניות." in report.text(sources, False)


def test_office_copies_only_fill_missing_kinds():
    def doc(kind, origin: str, source: str = "library") -> Document:
        return Document(title=origin, url="file:///x", source=source, kind=kind,
                        origin_source=origin)  # fmt: skip

    office_map = doc(DocumentKind.MAP, "office")
    rmi = Plan(number="1", name="", status="", source="rmi", documents=[
        Document(title="t", url="https://x", source="rmi", kind=DocumentKind.INSTRUCTIONS),
        Document(title="m", url="https://x", source="rmi", kind=DocumentKind.MAP),
    ])  # fmt: skip
    documents, source = choose_documents(rmi, [office_map])
    assert source == "rmi" and office_map not in documents
    alone = rmi.model_copy(update={"documents": []})
    assert choose_documents(alone, [office_map]) == ([office_map], "office")
    library_instructions = doc(DocumentKind.INSTRUCTIONS, "rmi")
    assert choose_documents(alone, [library_instructions, office_map]) == (
        [library_instructions, office_map],
        "library+office",
    )


def test_migrate_moves_the_old_layout_into_cities(tmp_path: Path):
    old = tmp_path / "old"
    _write(old / "בב-105א" / "הוראות.pdf", b"%PDF 105a")
    added = datetime(2026, 10, 29, 9, 40).astimezone().isoformat()
    (old / "בב-105א" / "plan.json").write_text(json.dumps({
        "number": "בב/105א", "aliases": ["105/א"], "name": "", "approval_date": None,
        "files": [{"path": "הוראות.pdf", "kind": "הוראות", "title": "תקנון", "size": 9,
                   "sha256": hashlib.sha256(b"%PDF 105a").hexdigest(), "source": "rmi",
                   "url": "https://apps.land.gov.il/a", "added_at": added}],
    }), encoding="utf-8")  # fmt: skip
    (old / "loose").mkdir()
    library = _library(tmp_path)

    assert migrate(library, old, "בני ברק", delete_source=True) == 0

    folder = tmp_path / "share" / "בני_ברק" / "בב-105א"
    entry = json.loads((folder / "plan.json").read_text(encoding="utf-8"))
    assert entry["city"] == "בני ברק" and entry["version"] == 2
    assert entry["files"][0]["added_at"].startswith("2026-10-29T09:40")
    assert not (old / "בב-105א").exists() and (old / "loose").exists()
    found = library.find(Plan(number="105/א", name="", status="", source="bnei_brak",
                              city="בני ברק"))  # fmt: skip
    assert found is not None and found.folder == "בני_ברק/בב-105א"
