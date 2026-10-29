import json
import os
import time
from datetime import date, datetime, timedelta
from pathlib import Path

import pytest

from app.models import DocumentKind, PlanScope
from app.output.library import (
    LibraryError,
    LibraryPlan,
    NewFile,
    PlansLibrary,
    SourceRef,
    merge_plan,
    plan_folder,
)
from app.output.plan_scope import ScopeBasis

FOLDER = "בני_ברק/501-0873794"


def _plan(**fields) -> LibraryPlan:
    values = {"number": "501-0873794", "name": "חדרי יציאה לגג", "city": "בני ברק"}
    return LibraryPlan(**(values | fields))


def _file(tmp_path: Path, name: str, content: bytes, kind=DocumentKind.INSTRUCTIONS) -> NewFile:
    path = tmp_path / "project" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return NewFile(
        local_path=path,
        path=name,
        kind=kind,
        title=Path(name).stem,
        source="rmi",
        url=f"https://apps.land.gov.il/{name}",
    )


def test_update_writes_plan_json_and_files_under_the_city(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    files = [
        _file(tmp_path, "הוראות.pdf", b"%PDF instructions"),
        _file(tmp_path, "תשריט.pdf", b"%PDF map", DocumentKind.MAP),
    ]

    result = library.update(FOLDER, _plan(), files)

    assert result.added == 2 and result.written
    assert result.plan.complete()
    folder = tmp_path / "lib" / "בני_ברק" / "501-0873794"
    entry = json.loads((folder / "plan.json").read_text("utf-8"))
    assert entry["version"] == 2
    assert entry["city"] == "בני ברק"
    assert [f["path"] for f in entry["files"]] == ["הוראות.pdf", "תשריט.pdf"]
    assert entry["files"][0]["url"] == "https://apps.land.gov.il/הוראות.pdf"
    assert entry["first_seen"] is not None
    assert (folder / "תשריט.pdf").read_bytes() == b"%PDF map"
    assert not (folder / ".lock").exists()
    assert library.load(FOLDER) == result.plan


def test_same_file_is_not_added_twice_and_name_clash_gets_suffix(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    first = _file(tmp_path, "a/תשריט.pdf", b"%PDF version 1", DocumentKind.MAP)
    second = _file(tmp_path, "b/תשריט.pdf", b"%PDF version 2", DocumentKind.MAP)
    first.path = second.path = "תשריט.pdf"

    assert library.update(FOLDER, _plan(), [first]).added == 1
    again = library.update(FOLDER, _plan(), [first])
    assert again.added == 0
    # Nothing new on the same day: plan.json is not rewritten.
    assert not again.written
    assert library.update(FOLDER, _plan(), [second]).added == 1

    folder = tmp_path / "lib" / "בני_ברק" / "501-0873794"
    assert (folder / "תשריט.pdf").read_bytes() == b"%PDF version 1"
    assert (folder / "תשריט_2.pdf").read_bytes() == b"%PDF version 2"


def test_merge_keeps_the_record_and_adds_what_is_new():
    seen = datetime(2026, 10, 1).astimezone()
    old = _plan(
        number="מק/105/ה",
        approval_date=date(2002, 5, 1),
        sources=[SourceRef(source="bnei_brak", id="1201", checked_at=seen)],
        parcels=[(6123, 2518)],
        first_seen=seen,
        last_checked=seen,
    )
    later = seen + timedelta(days=20)
    new = _plan(
        number="501-0001111",
        aliases=["מק/105/ה"],
        national_number="501-0001111",
        approval_date=date(2002, 6, 1),
        status="בתוקף",
        boundary_wkt="POLYGON ((0 0, 1 0, 1 1, 0 0))",
        scope=PlanScope.SPECIFIC,
        scope_basis=ScopeBasis.AREA,
        sources=[
            SourceRef(source="bnei_brak", id="1201", checked_at=later),
            SourceRef(source="iplan", id="77"),
        ],
        parcels=[(6123, 443)],
        last_checked=later,
    )

    merged = merge_plan(old, new)

    # The folder's number stays; the national number joins as an alias.
    assert merged.number == "מק/105/ה"
    assert merged.national_number == "501-0001111"
    assert merged.aliases == ["501-0001111"]
    assert merged.approval_date == date(2002, 5, 1)
    assert merged.status == "בתוקף"
    assert merged.boundary_wkt
    # A default decision gives way to one made from a boundary.
    assert merged.scope_basis == ScopeBasis.AREA
    assert {(r.source, r.id, r.checked_at) for r in merged.sources} == {
        ("bnei_brak", "1201", later),
        ("iplan", "77", None),
    }
    assert merged.parcels == [(6123, 443), (6123, 2518)]
    assert (merged.first_seen, merged.last_checked) == (seen, later)

    # A final decision is not replaced by an automatic one, only by a manual one.
    assert merge_plan(merged, _plan(scope_basis=ScopeBasis.DEFAULT)).scope_basis == "area"
    manual = _plan(scope=PlanScope.CITYWIDE, scope_basis=ScopeBasis.MANUAL)
    corrected = merge_plan(merged, manual)
    assert corrected.scope == PlanScope.CITYWIDE
    # City-wide plans apply to every parcel in the city; their parcels are not kept.
    assert corrected.parcels == []


def test_the_same_file_from_the_same_source_can_correct_its_kind(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    first = _file(tmp_path, "819.pdf", b"%PDF sheet", DocumentKind.INSTRUCTIONS)
    library.update(FOLDER, _plan(), [first])

    again = library.update(FOLDER, _plan(), [first.model_copy(update={"kind": DocumentKind.MAP})])

    assert again.written and again.added == 0
    assert [f.kind for f in library.load(FOLDER).files] == [DocumentKind.MAP]
    other = first.model_copy(update={"kind": DocumentKind.APPENDIX, "source": "office"})
    assert not library.update(FOLDER, _plan(), [other]).written


def test_folder_of_a_national_plan_and_plan_json_of_the_old_layout(tmp_path: Path):
    assert plan_folder(_plan()) == FOLDER
    national = _plan(number="תמא/ 60", scope=PlanScope.NATIONAL, city=None)
    assert plan_folder(national) == "ארציות/תמא-_60"
    assert plan_folder(_plan(city=None)) == "ללא_עיר/501-0873794"

    old_layout = {
        "number": "בב/105א",
        "aliases": [],
        "name": "x",
        "approval_date": None,
        "files": [],
    }
    loaded = LibraryPlan.model_validate(old_layout)
    assert loaded.scope_basis == ScopeBasis.DEFAULT and loaded.city is None


def test_scan_lists_plan_folders_and_their_time_changes_on_update(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    library.update(FOLDER, _plan(), [_file(tmp_path, "הוראות.pdf", b"%PDF 1")])
    library.update("ארציות/תמא-_60", _plan(number="תמא/ 60", scope=PlanScope.NATIONAL))
    (tmp_path / "lib" / "overrides.yaml").write_text("{}", encoding="utf-8")
    (tmp_path / "lib" / "_tmp" / "x").mkdir(parents=True)

    before = library.scan()
    assert sorted(before) == ["ארציות/תמא-_60", FOLDER]
    time.sleep(0.05)
    library.update(FOLDER, _plan(), [_file(tmp_path, "תשריט.pdf", b"%PDF 2", DocumentKind.MAP)])
    after = library.scan()
    assert after[FOLDER] != before[FOLDER]
    assert after["ארציות/תמא-_60"] == before["ארציות/תמא-_60"]


def test_move_keeps_files_and_leaves_no_lock(tmp_path: Path):
    library = PlansLibrary(tmp_path / "lib")
    library.update(FOLDER, _plan(), [_file(tmp_path, "הוראות.pdf", b"%PDF")])

    library.move(FOLDER, "ארציות/501-0873794")

    moved = tmp_path / "lib" / "ארציות" / "501-0873794"
    assert (moved / "הוראות.pdf").is_file() and not (moved / ".lock").exists()
    assert library.load(FOLDER) is None
    # The target is taken: the folder stays where it is.
    library.update(FOLDER, _plan())
    with pytest.raises(LibraryError):
        library.move(FOLDER, "ארציות/501-0873794")
    assert library.load(FOLDER) is not None


def test_lock_held_by_another_user_times_out_and_stale_lock_is_removed(tmp_path: Path):
    folder = tmp_path / "lib" / "בני_ברק" / "501-0873794"
    folder.mkdir(parents=True)
    lock = folder / ".lock"
    lock.write_text("someone else")
    file = _file(tmp_path, "הוראות.pdf", b"%PDF")

    busy = PlansLibrary(tmp_path / "lib", lock_timeout_seconds=0.3)
    with pytest.raises(LibraryError):
        busy.update(FOLDER, _plan(), [file])

    old = time.time() - 3600
    os.utime(lock, (old, old))
    assert PlansLibrary(tmp_path / "lib").update(FOLDER, _plan(), [file]).added == 1
    assert not lock.exists()
