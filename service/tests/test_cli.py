import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from app.cli import run
from app.config import Config, SourcesConfig
from app.pipeline import MANUAL_CITY_UNAVAILABLE, MANUAL_MAVAT
from tests.conftest import fixture_bytes, offline_http

PDF = b"%PDF-1.4 fixture"
NOT_FOUND = "לא ניתן להציג את המידע המבוקש"

COMPLOT_PAGES = {
    ("GetGushFile", "443"): "complot_gush_6123_443_parcellated.html",
    ("GetGushFile", "2518"): "complot_gush_6123_2518.html",
    ("GetTabaFile", "3896"): "complot_taba_3896.html",
    ("GetTabaFile", "1201"): "complot_taba_1201.html",
    ("GetBakashotByGush", "443"): "complot_requests_gush_6123_443.html",
    ("GetBakashaFile", "195000038"): "complot_request_20177.html",
}
# Israel Land Authority TabaSearch answers, by (gush, chelka) or by plan number.
RMI_BY_PARCEL = {("6123", "443"): "rmi_plans_6123_443.json"}
RMI_BY_NUMBER = {"501-0175646": "rmi_plans_501-0175646.json"}


def _config(tmp_path: Path, disabled: list[str]) -> Config:
    return Config(
        output_dir=tmp_path / "out",
        cache_db=tmp_path / "cache.sqlite",
        user_agent="t",
        disabled_sources=disabled,
        sources=SourcesConfig(
            govmap_wfs="https://open.govmap.gov.il/geoserver/opendata/wfs",
            iplan_plans_layer="https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/MapServer/1",
            complot_base="https://handasi.complot.co.il/magicscripts/mgrqispi.dll",
            complot_site_id=75,
            complot_ws="https://handasi.complot.co.il/wsComplotPublicData/ComplotPublicData.asmx",
        ),
    )


def _handler(hosts: list[str]):
    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        hosts.append(host)
        if host == "open.govmap.gov.il":
            return httpx.Response(200, content=fixture_bytes("govmap_parcel_6123_443.json"))
        if host == "ags.iplan.gov.il":
            return httpx.Response(200, content=fixture_bytes("iplan_query.json"))
        if host == "handasi.complot.co.il":
            if request.url.path.endswith(".asmx"):
                return httpx.Response(200, content=fixture_bytes("complot_streets.xml"))
            query = parse_qs(urlsplit(str(request.url)).query)
            key = (query["prgname"][0], query.get("h", query.get("n", query.get("b")))[0])
            name = COMPLOT_PAGES.get(key)
            if name is None:
                return httpx.Response(200, text=NOT_FOUND)
            return httpx.Response(200, content=fixture_bytes(name))
        if host == "archive.gis-net.co.il":
            return httpx.Response(200, content=PDF)
        if host == "apps.land.gov.il":
            return _rmi_response(request)
        raise AssertionError(f"unexpected host {host}")

    return handler


def _rmi_response(request: httpx.Request) -> httpx.Response:
    path = request.url.path
    if path.endswith("/SerachPlans/GetPlanTypesList"):
        return httpx.Response(200, content=fixture_bytes("rmi_plan_types.json"))
    if path.endswith("/SerachPlans/GetPlans"):
        body = json.loads(request.content)
        assert body["planTypes"], "the search must send the plan type codes"
        name = RMI_BY_NUMBER.get(body["planNumber"]) or RMI_BY_PARCEL.get(
            (body["gush"], body["chelka"]), "rmi_plans_empty.json"
        )
        return httpx.Response(200, content=fixture_bytes(name))
    if path.startswith("/IturTabotData/"):
        assert "\\" not in path and "%5C" not in str(request.url)
        # Distinct content per file, as the library keeps one copy per SHA256.
        return httpx.Response(200, content=PDF + path.encode())
    raise AssertionError(f"unexpected rmi request {request.url}")


def _manifest(tmp_path: Path) -> dict:
    root = tmp_path / "out" / "בדיקה_ג6123_ח443_2026-10-28"
    return json.loads((root / "00_סיכום" / "manifest.json").read_text(encoding="utf-8"))


def test_full_run_with_city_system(tmp_path: Path):
    hosts: list[str] = []
    code = run(
        _config(tmp_path, []),
        6123,
        443,
        "בדיקה",
        http=offline_http(_handler(hosts)),
        today=date(2026, 10, 28),
    )

    assert code == 0
    manifest = _manifest(tmp_path)
    assert [(p["gush"], p["helka"]) for p in manifest["parcels"]] == [(6123, 2518)]
    # Addresses come only from the city system's parcel file.
    addresses = [(a["street"], a["number"]) for a in manifest["parcels"][0]["addresses"]]
    assert addresses == [("הבנים", "3")]
    plans = {p["number"]: p for p in manifest["plans"]}
    # 3896 is in the city system, iplan and rmi: merged; rmi's documents come first.
    assert plans["501-0873794"]["source"] == "bnei_brak+iplan+rmi"
    assert plans["501-0873794"]["documents_source"] == "rmi"
    assert plans["501-0873794"]["manual_action"] is None
    # Older local plan only in the city system: documents from the city archive.
    assert plans["מק/105/ה"]["source"] == "bnei_brak"
    assert plans["מק/105/ה"]["documents_source"] == "bnei_brak"
    # Old plans only rmi knows are added, with their documents.
    assert plans["בב/68"]["source"] == "rmi"
    assert plans["בב/68"]["approval_date"] == "1959-06-04"
    # National plan only in iplan: manual download from mavat.
    assert plans["תמא/ 60"]["manual_action"] == MANUAL_MAVAT
    # Listed by rmi (found by number) but without documents.
    assert plans["501-0175646"]["source"] == "iplan+rmi"
    assert plans["501-0175646"]["manual_action"] == MANUAL_MAVAT

    paths = sorted(f["path"] for f in manifest["files"])
    roof = "02_תבעות/501-0873794_קביעת_הוראות_לחדרי_גג_ודירות_גג"
    assert f"{roof}/הוראות.pdf" in paths
    assert f"{roof}/תשריט.pdf" in paths
    assert f"{roof}/נספחים/בינוי.pdf" in paths
    assert "02_תבעות/מק-105-ה_השלמת_יחידת_דיור_אחת/הוראות.pdf" in paths
    assert "02_תבעות/בב-68_קביעת_אזורים/תשריט.pdf" in paths
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert any("פרצלציה" in w for w in manifest["warnings"])
    assert any("מק/105/ה: חסרים תשריט" in w for w in manifest["warnings"])
    assert [p["permit_number"] for p in manifest["permits"]] == ["9545"]
    assert "01_היתרי_בניה/1989_היתר_9545/היתר.pdf" in paths
    root = tmp_path / "out" / "בדיקה_ג6123_ח443_2026-10-28"
    assert (root / "99_לוג" / "run.log").is_file()


def test_disabled_city_system_is_never_contacted(tmp_path: Path):
    hosts: list[str] = []
    code = run(
        _config(tmp_path, ["bnei_brak"]),
        6123,
        443,
        "בדיקה",
        http=offline_http(_handler(hosts)),
        today=date(2026, 10, 28),
    )

    assert code == 0
    assert "handasi.complot.co.il" not in hosts
    assert "archive.gis-net.co.il" not in hosts
    manifest = _manifest(tmp_path)
    # 8 plans from iplan and 6 older ones from rmi; rmi has documents for 12 of them.
    assert len(manifest["plans"]) == 14
    manual = sorted(p["number"] for p in manifest["plans"] if p["manual_action"])
    assert manual == ["501-0175646", "תמא/ 60"]
    assert all(
        p["manual_action"] == MANUAL_CITY_UNAVAILABLE
        for p in manifest["plans"]
        if p["manual_action"]
    )
    assert {f["source"] for f in manifest["files"]} == {"rmi"}
    assert manifest["permits"] == []
    assert any("היתרי בנייה לא נבדקו" in w for w in manifest["warnings"])
    # The cadastre has no addresses; the warning says why they are missing.
    assert any("כתובות" in w for w in manifest["warnings"])
    assert [(p["gush"], p["helka"]) for p in manifest["parcels"]] == [(6123, 443)]
    root = tmp_path / "out" / "בדיקה_ג6123_ח443_2026-10-28"
    assert "resolve parcel 6123/443" in (root / "99_לוג" / "run.log").read_text(encoding="utf-8")
    dxf = (root / "00_סיכום" / "polygon.dxf").read_text(encoding="ascii")
    assert "PARCELDOCS_PARCELS" in dxf
    assert "6123/443" in dxf


def test_city_and_rmi_disabled_leaves_plans_for_mavat(tmp_path: Path):
    hosts: list[str] = []
    code = run(
        _config(tmp_path, ["bnei_brak", "rmi"]),
        6123,
        443,
        "בדיקה",
        http=offline_http(_handler(hosts)),
        today=date(2026, 10, 28),
    )

    assert code == 0
    assert "apps.land.gov.il" not in hosts
    manifest = _manifest(tmp_path)
    assert len(manifest["plans"]) == 8
    assert all(p["manual_action"] == MANUAL_CITY_UNAVAILABLE for p in manifest["plans"])
    assert manifest["files"] == []


def test_library_is_filled_then_used_before_any_source(tmp_path: Path):
    config = _config(tmp_path, ["bnei_brak"])
    config.plans_library = tmp_path / "library"
    config.plans_library.mkdir()
    first: list[str] = []
    run(config, 6123, 443, "ראשון", http=offline_http(_handler(first)), today=date(2026, 10, 28))

    # The parcel is in Bnei Brak (cadastre LOCALITY_N), so the plan is filed under its city.
    roof = tmp_path / "library" / "בני_ברק" / "501-0873794"
    assert (roof / "הוראות.pdf").is_file()
    assert (roof / "נספחים" / "בינוי.pdf").is_file()
    entry = json.loads((roof / "plan.json").read_text(encoding="utf-8"))
    assert {f["source"] for f in entry["files"]} == {"rmi"}

    second: list[str] = []
    http = offline_http(_handler(second))
    run(config, 6123, 443, "שני", http=http, today=date(2026, 10, 28))

    root = tmp_path / "out" / "שני_ג6123_ח443_2026-10-28"
    manifest = json.loads((root / "00_סיכום" / "manifest.json").read_text(encoding="utf-8"))
    plans = {p["number"]: p for p in manifest["plans"]}
    assert plans["501-0873794"]["documents_source"] == "library"
    library_files = [f for f in manifest["files"] if f["source"] == "library"]
    assert len(library_files) == len(manifest["files"]) == 48
    assert {f["origin_source"] for f in library_files} == {"rmi"}
    # Only the plan search itself went to rmi; no file was fetched again.
    assert http.request_count("apps.land.gov.il") == 3
