import json
import zipfile
from pathlib import Path

import pytest

from app.mavat_assist import (
    FILED_DIR,
    REASON_ACTIVE,
    REASON_CONTENT,
    REASON_MANUAL,
    REASON_NAME,
    MavatAssist,
    classify,
    national_numbers,
)
from app.models import DocumentKind
from app.output.library import PlansLibrary
from app.utils.pdf import PdfInfo, read_pdf, repair_text
from tests.conftest import make_pdf

A0 = (3370, 2384)
PLAN_A = {
    "number": "501-0175646",
    "name": "שינוי בינוי ביח״ד קיימות",
    "status": "אישור",
    "source": "iplan+rmi",
    "mavat_url": "https://mavat.iplan.gov.il/SV4/1/5000101/310",
    "manual_action": "להוריד ממבא״ת",
}
PLAN_B = {
    "number": "תמא/ 60",
    "name": "מטרו",
    "status": "אישור",
    "source": "iplan",
    "mavat_url": "https://mavat.iplan.gov.il/SV4/1/6000202/310",
    "manual_action": "להוריד ממבא״ת",
}
DIR_A = "02_תבעות/501-0175646_שינוי_בינוי_ביח״ד_קיימות"
DIR_B = "02_תבעות/תמא-_60_מטרו"


@pytest.fixture
def project(tmp_path: Path) -> Path:
    root = tmp_path / "פרויקט_ג6123_ח443_2026-10-28"
    (root / "00_סיכום").mkdir(parents=True)
    (root / "99_לוג").mkdir()
    manifest = {"project": "פרויקט", "plans": [PLAN_A, PLAN_B], "files": [], "warnings": []}
    (root / "00_סיכום" / "manifest.json").write_text(
        json.dumps(manifest, ensure_ascii=False), encoding="utf-8"
    )
    return root


@pytest.fixture
def downloads(tmp_path: Path) -> Path:
    folder = tmp_path / "Downloads"
    folder.mkdir()
    (folder / "old statement.pdf").write_bytes(make_pdf("already here"))
    return folder


def _assist(project: Path, downloads: Path, tmp_path: Path, opened: list[str] | None = None):
    return MavatAssist(
        project,
        downloads,
        PlansLibrary(tmp_path / "library"),
        opener=(opened.append if opened is not None else lambda url: None),
    )


def _poll(assist: MavatAssist):
    # A download counts only once it is unchanged across two polls.
    assert assist.poll() == []
    return assist.poll()


def test_files_by_number_in_name_and_in_content(project: Path, downloads: Path, tmp_path: Path):
    assist = _assist(project, downloads, tmp_path)
    (downloads / "הוראות התכנית 501-0175646.pdf").write_bytes(make_pdf("instructions"))
    (downloads / "document (3).pdf").write_bytes(make_pdf("Plan 501-0175646 sheet 1", *A0))

    filed = _poll(assist)

    by_kind = {f.kind: f for f in filed}
    assert by_kind[DocumentKind.INSTRUCTIONS].path == f"{DIR_A}/הוראות.pdf"
    assert by_kind[DocumentKind.INSTRUCTIONS].reason == REASON_NAME
    assert by_kind[DocumentKind.MAP].path == f"{DIR_A}/תשריט.pdf"
    assert by_kind[DocumentKind.MAP].reason == REASON_CONTENT
    assert all(f.in_library for f in filed)
    # Originals moved out of the way; the file that was there before is untouched.
    assert sorted(p.name for p in (downloads / FILED_DIR).iterdir()) == [
        "document (3).pdf",
        "הוראות התכנית 501-0175646.pdf",
    ]
    assert (downloads / "old statement.pdf").is_file()

    status = assist.status()
    plan_a = next(p for p in status.plans if p.number == "501-0175646")
    assert plan_a.done
    manifest = json.loads((project / "00_סיכום" / "manifest.json").read_text("utf-8"))
    entry = next(p for p in manifest["plans"] if p["number"] == "501-0175646")
    assert entry["manual_action"] is None
    assert entry["documents_source"] == "mavat"
    assert {f["source"] for f in manifest["files"]} == {"mavat"}
    library_entry = json.loads(
        (tmp_path / "library" / "501-0175646" / "plan.json").read_text("utf-8")
    )
    assert sorted((f["path"], f["source"]) for f in library_entry["files"]) == [
        ("הוראות.pdf", "mavat"),
        ("תשריט.pdf", "mavat"),
    ]
    log = (project / "99_לוג" / "run.log").read_text("utf-8")
    assert "mavat: הוראות התכנית 501-0175646.pdf" in log


def test_opened_plan_takes_unnamed_plan_documents_only(
    project: Path, downloads: Path, tmp_path: Path
):
    opened: list[str] = []
    assist = _assist(project, downloads, tmp_path, opened)
    assist.open("plan:תמא/ 60")
    assert opened == [PLAN_B["mavat_url"]]

    (downloads / "scan0001.pdf").write_bytes(make_pdf("", *A0))
    (downloads / "invoice.pdf").write_bytes(make_pdf("Invoice 12345"))
    filed = _poll(assist)

    assert [(f.name, f.kind, f.reason, f.path) for f in filed] == [
        ("scan0001.pdf", DocumentKind.MAP, REASON_ACTIVE, f"{DIR_B}/תשריט.pdf")
    ]
    # Not a planning document: left in the downloads folder until the user decides.
    assert [u.name for u in assist.status().unidentified] == ["invoice.pdf"]
    assert (downloads / "invoice.pdf").is_file()

    assigned = assist.assign("invoice.pdf", "plan:תמא/ 60")
    assert [(f.kind, f.reason, f.path) for f in assigned] == [
        (DocumentKind.APPENDIX, REASON_MANUAL, f"{DIR_B}/נספחים/invoice.pdf")
    ]
    assert assist.status().unidentified == []


def test_without_an_open_plan_unnamed_files_wait(project: Path, downloads: Path, tmp_path: Path):
    assist = _assist(project, downloads, tmp_path)
    (downloads / "scan0002.pdf").write_bytes(make_pdf("", *A0))

    assert _poll(assist) == []
    assert [u.name for u in assist.status().unidentified] == ["scan0002.pdf"]


def test_zip_named_after_the_plan(project: Path, downloads: Path, tmp_path: Path):
    assist = _assist(project, downloads, tmp_path)
    with zipfile.ZipFile(downloads / "501-0175646.zip", "w") as archive:
        archive.writestr("הוראות התכנית.pdf", make_pdf("a"))
        archive.writestr("תשריט מצב מוצע.pdf", make_pdf("b", *A0))
        archive.writestr("נספח בינוי.pdf", make_pdf("c", *A0))
        archive.writestr("readme.txt", "not a pdf")

    filed = _poll(assist)

    assert sorted(f.path for f in filed) == [
        f"{DIR_A}/הוראות.pdf",
        f"{DIR_A}/נספחים/נספח_בינוי.pdf",
        f"{DIR_A}/תשריט.pdf",
    ]
    assert (downloads / FILED_DIR / "501-0175646.zip").is_file()


def test_same_file_twice_is_filed_once_and_second_map_gets_number(
    project: Path, downloads: Path, tmp_path: Path
):
    assist = _assist(project, downloads, tmp_path)
    (downloads / "תשריט 501-0175646.pdf").write_bytes(make_pdf("one", *A0))
    _poll(assist)
    (downloads / "תשריט 501-0175646 (1).pdf").write_bytes(make_pdf("one", *A0))
    (downloads / "תשריט קיים 501-0175646.pdf").write_bytes(make_pdf("two", *A0))

    filed = {f.name: f for f in _poll(assist)}

    assert filed["תשריט 501-0175646 (1).pdf"].duplicate
    assert filed["תשריט 501-0175646 (1).pdf"].path == f"{DIR_A}/תשריט.pdf"
    assert filed["תשריט קיים 501-0175646.pdf"].path == f"{DIR_A}/תשריט_2.pdf"
    assert sorted(p.name for p in (project / DIR_A).iterdir()) == ["תשריט.pdf", "תשריט_2.pdf"]


def test_unfinished_and_unrelated_downloads_are_ignored(
    project: Path, downloads: Path, tmp_path: Path
):
    assist = _assist(project, downloads, tmp_path)
    (downloads / "הוראות 501-0175646.pdf.crdownload").write_bytes(b"%PDF partial")
    (downloads / "photo.jpg").write_bytes(b"\xff\xd8")
    (downloads / "growing 501-0175646.pdf").write_bytes(make_pdf("x"))
    assert assist.poll() == []
    (downloads / "growing 501-0175646.pdf").write_bytes(make_pdf("x, now longer"))

    assert assist.poll() == []  # changed since the last poll: still downloading
    assert len(assist.poll()) == 1


def test_classify_and_numbers():
    instructions = PdfInfo(pages=14, text="הוראות התכנית\nתכנית מס' 501-0873794", max_side_pt=842)
    reversed_map = PdfInfo(pages=1, text="501-0873794 'סמ\nטירשתה סיפדת", max_side_pt=842)
    big = PdfInfo(pages=1, text="", max_side_pt=3368)

    assert classify("1.pdf", instructions) == DocumentKind.INSTRUCTIONS
    assert classify("1.pdf", reversed_map) == DocumentKind.MAP
    assert classify("1.pdf", big) == DocumentKind.MAP
    assert classify("נספח בינוי.pdf", big) == DocumentKind.APPENDIX
    assert classify("תקנון.pdf", None) == DocumentKind.INSTRUCTIONS
    assert classify("1.pdf", None) == DocumentKind.APPENDIX
    assert national_numbers("x 5010873794 y 501-0873794 z 15010873794") == [
        "501-0873794",
        "501-0873794",
    ]


def test_read_pdf_and_two_byte_text_repair(tmp_path: Path):
    path = tmp_path / "a.pdf"
    path.write_bytes(make_pdf("Plan 501-0873794", *A0))

    info = read_pdf(path)

    assert info is not None and "501-0873794" in info.text and info.max_side_pt == 3370
    assert repair_text("\x05Ý\x05Ù\x05è\x05Ø\x05Þ") == "םירטמ"
    assert repair_text("\x001\x008\x002") == "182"
    assert read_pdf(tmp_path / "missing.pdf") is None
