import time
from pathlib import Path

import yaml
from shapely.geometry import box

from app.models import DocumentKind, Plan, PlanScope
from app.output.library import NewFile
from app.output.library_index import LOCAL, SHARED
from app.output.office_library import SHARED_UNAVAILABLE, LibraryUpdate, OfficeLibrary
from app.output.plan_scope import ScopeBasis, ScopeDecision
from app.utils.filenames import path_from_file_uri

SPECIFIC = ScopeDecision(scope=PlanScope.SPECIFIC, basis=ScopeBasis.DEFAULT, note="ברירת מחדל")
CITYWIDE = ScopeDecision(scope=PlanScope.CITYWIDE, basis=ScopeBasis.AREA, note="95%")
PARCEL = box(183600, 665800, 183620, 665830)


def _computer(tmp_path: Path, name: str, shared: Path | None = None) -> OfficeLibrary:
    shared = shared or tmp_path / "share"
    return OfficeLibrary(
        shared, tmp_path / name / "local", tmp_path / name / "index.sqlite", timeout_seconds=2
    )


def _plan(number: str = "501-0873794", **fields) -> Plan:
    values = {"name": "חדרי יציאה לגג", "status": "אישור", "source": "iplan", "city": "בני ברק"}
    return Plan(number=number, **(values | fields))


def _file(tmp_path: Path, name: str, content: bytes, kind=DocumentKind.INSTRUCTIONS) -> NewFile:
    path = tmp_path / "project" / name
    path.parent.mkdir(parents=True, exist_ok=True)
    path.write_bytes(content)
    return NewFile(local_path=path, path=name, kind=kind, title=name, source="rmi", url="u")


def test_two_computers_share_one_folder_through_their_own_indexes(tmp_path: Path):
    (tmp_path / "share").mkdir()
    first, second = _computer(tmp_path, "a"), _computer(tmp_path, "b")
    assert first.open() == [] and second.open() == []

    plan = _plan(aliases=["מק/105/ג/4"], source_ids={"iplan": "5"})
    first.record(plan, LibraryUpdate(decision=SPECIFIC, parcels=[(6123, 443)]),
                 [_file(tmp_path, "הוראות.pdf", b"%PDF 1")], [])  # fmt: skip

    second.open()
    # Found by its local number from the city system, in the same city.
    found = second.find(_plan("מק/105/ג/4", source="bnei_brak"))
    assert found is not None and found.folder == "בני_ברק/501-0873794"
    assert second.find(_plan("מק/105/ג/4", city="ערד")) is None
    documents = second.documents(_plan())
    assert [d.kind for d in documents] == [DocumentKind.INSTRUCTIONS]
    assert path_from_file_uri(documents[0].url).read_bytes() == b"%PDF 1"

    time.sleep(0.05)
    second.record(_plan(), LibraryUpdate(decision=SPECIFIC),
                  [_file(tmp_path, "תשריט.pdf", b"%PDF 2", DocumentKind.MAP)], [])  # fmt: skip
    first.open()
    assert first.find(_plan()).plan.complete()
    assert first.index.count(SHARED) == 1


def test_search_by_boundary_by_parcel_and_city_wide_plans(tmp_path: Path):
    (tmp_path / "share").mkdir()
    library = _computer(tmp_path, "a")
    library.open()
    near = box(183500, 665700, 183700, 665900).wkt
    far = box(190000, 670000, 190100, 670100).wkt
    library.record(_plan("501-1", boundary_wkt=near), LibraryUpdate(decision=SPECIFIC), [], [])
    library.record(_plan("501-2", boundary_wkt=far), LibraryUpdate(decision=SPECIFIC), [], [])
    library.record(_plan("501-3", boundary_wkt=far), LibraryUpdate(decision=CITYWIDE), [], [])
    library.record(_plan("מק/105/ה", source="bnei_brak"),
                   LibraryUpdate(decision=SPECIFIC, parcels=[(6123, 2518)]), [], [])  # fmt: skip
    library.record(_plan("501-4", city="ערד", boundary_wkt=far),
                   LibraryUpdate(decision=CITYWIDE), [], [])  # fmt: skip

    found = library.search(PARCEL, "בני ברק", [(6123, 2518)])

    assert sorted(item.plan.number for item in found) == ["501-1", "501-3", "מק/105/ה"]
    assert library.search(None, None, [(6123, 2518)])[0].plan.number == "מק/105/ה"


def test_unreachable_share_uses_the_local_copy_and_syncs_when_it_returns(tmp_path: Path):
    library = _computer(tmp_path, "a")
    assert library.open() == [SHARED_UNAVAILABLE]
    warnings: list[str] = []

    record = library.record(_plan(), LibraryUpdate(decision=SPECIFIC),
                            [_file(tmp_path, "הוראות.pdf", b"%PDF 1")], warnings)  # fmt: skip

    assert record.root == LOCAL
    assert (tmp_path / "a" / "local" / "בני_ברק" / "501-0873794" / "הוראות.pdf").is_file()
    assert len(library.documents(_plan())) == 1
    assert library.status()["pending_local"] == 1

    (tmp_path / "share").mkdir()
    assert library.open() == []
    assert (tmp_path / "share" / "בני_ברק" / "501-0873794" / "הוראות.pdf").is_file()
    assert not (tmp_path / "a" / "local" / "בני_ברק" / "501-0873794").exists()
    assert (library.index.count(SHARED), library.index.count(LOCAL)) == (1, 0)
    assert library.find(_plan()).plan.files[0].source == "rmi"


def test_manual_scope_applies_to_the_city_and_moves_national_plans(tmp_path: Path):
    (tmp_path / "share").mkdir()
    library = _computer(tmp_path, "a")
    library.open()
    library.record(_plan("מק/מעליות", source="bnei_brak"), LibraryUpdate(decision=SPECIFIC),
                   [_file(tmp_path, "הוראות.pdf", b"%PDF")], [])  # fmt: skip

    item = library.set_scope("בני ברק", "מק/מעליות", PlanScope.CITYWIDE)

    assert item.plan.scope == PlanScope.CITYWIDE and item.plan.scope_basis == ScopeBasis.MANUAL
    saved = yaml.safe_load((tmp_path / "share" / "overrides.yaml").read_text("utf-8"))
    assert saved == {"בני ברק": {"מק/מעליות": "כלל-עירונית"}}
    # Every run in the city now finds it, whatever the parcel.
    assert [i.plan.number for i in library.search(None, "בני ברק", [])] == ["מק/מעליות"]

    # A hand edit of the file, read by another computer at the start of its run.
    (tmp_path / "share" / "overrides.yaml").write_text(
        "בני ברק:\n  מק/מעליות: ארצית\n", encoding="utf-8"
    )
    other = _computer(tmp_path, "b")
    other.open()
    moved = other.find(_plan("מק/מעליות"))
    assert moved.folder == "ארציות/מק-מעליות"
    assert (tmp_path / "share" / "ארציות" / "מק-מעליות" / "הוראות.pdf").is_file()
    assert other.override_for(_plan("מק/מעליות")) == PlanScope.NATIONAL


def test_several_corrections_and_a_series_rule_at_once(tmp_path: Path):
    (tmp_path / "share").mkdir()
    library = _computer(tmp_path, "a")
    library.open()
    for number in ["105/ש", "בב/105א", "1050", "מק/מעליות"]:
        library.record(_plan(number, source="bnei_brak"), LibraryUpdate(decision=SPECIFIC), [], [])

    covered = library.set_scopes("בני ברק", ["מק/מעליות", "105*"], PlanScope.CITYWIDE)

    assert [i.plan.number for i in covered["מק/מעליות"]] == ["מק/מעליות"]
    assert sorted(i.plan.number for i in covered["105*"]) == ["105/ש", "בב/105א"]
    saved = yaml.safe_load((tmp_path / "share" / "overrides.yaml").read_text("utf-8"))
    assert saved == {"בני ברק": {"105*": "כלל-עירונית", "מק/מעליות": "כלל-עירונית"}}
    assert library.find(_plan("1050")).plan.scope == PlanScope.SPECIFIC
    # A plan of the series found later gets the rule too.
    assert library.override_for(_plan("105/ת/7")) == PlanScope.CITYWIDE


def test_index_is_rebuilt_from_plan_json(tmp_path: Path):
    (tmp_path / "share").mkdir()
    library = _computer(tmp_path, "a")
    library.open()
    library.record(_plan(), LibraryUpdate(decision=CITYWIDE), [], [])
    (tmp_path / "a" / "index.sqlite").unlink()

    fresh = _computer(tmp_path, "a")
    fresh.open()
    assert fresh.find(_plan()).plan.scope == PlanScope.CITYWIDE


def test_share_that_does_not_answer_in_time(tmp_path: Path, monkeypatch):
    (tmp_path / "share").mkdir()
    library = OfficeLibrary(tmp_path / "share", tmp_path / "l", tmp_path / "i.sqlite", 0.2)

    def slow(path) -> bool:
        time.sleep(1)
        return True

    monkeypatch.setattr("app.output.office_library.os.path.isdir", slow)
    started = time.monotonic()
    assert library.open() == [SHARED_UNAVAILABLE]
    assert time.monotonic() - started < 0.9
