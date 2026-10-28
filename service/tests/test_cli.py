import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from app.cli import MANUAL_CITY_UNAVAILABLE, MANUAL_MAVAT, run
from app.config import Config, SourcesConfig
from tests.conftest import fixture_bytes, offline_http

PDF = b"%PDF-1.4 fixture"
NOT_FOUND = "לא ניתן להציג את המידע המבוקש"

COMPLOT_PAGES = {
    ("GetGushFile", "443"): "complot_gush_6123_443_parcellated.html",
    ("GetGushFile", "2518"): "complot_gush_6123_2518.html",
    ("GetTabaFile", "3896"): "complot_taba_3896.html",
    ("GetTabaFile", "1201"): "complot_taba_1201.html",
}


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
            query = parse_qs(urlsplit(str(request.url)).query)
            key = (query["prgname"][0], query.get("h", query.get("n"))[0])
            name = COMPLOT_PAGES.get(key)
            if name is None:
                return httpx.Response(200, text=NOT_FOUND)
            return httpx.Response(200, content=fixture_bytes(name))
        if host == "archive.gis-net.co.il":
            return httpx.Response(200, content=PDF)
        raise AssertionError(f"unexpected host {host}")

    return handler


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
    plans = {p["number"]: p for p in manifest["plans"]}
    # 3896 is in the city system and in iplan: merged, with documents.
    assert plans["501-0873794"]["source"] == "bnei_brak+iplan"
    assert plans["501-0873794"]["manual_action"] is None
    # Older local plan only in the city system.
    assert plans["מק/105/ה"]["source"] == "bnei_brak"
    # National plan only in iplan: manual download from mavat.
    assert plans["תמא/ 60"]["manual_action"] == MANUAL_MAVAT

    paths = sorted(f["path"] for f in manifest["files"])
    roof = "02_תבעות/501-0873794_קביעת_הוראות_לחדרי_גג_ודירות_גג"
    assert f"{roof}/הוראות.pdf" in paths
    assert f"{roof}/תשריט.pdf" in paths
    assert f"{roof}/תשריט_2.pdf" in paths
    assert all(len(f["sha256"]) == 64 for f in manifest["files"])
    assert any("פרצלציה" in w for w in manifest["warnings"])
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
    assert len(manifest["plans"]) == 8
    assert all(p["manual_action"] == MANUAL_CITY_UNAVAILABLE for p in manifest["plans"])
    assert manifest["files"] == []
    assert [(p["gush"], p["helka"]) for p in manifest["parcels"]] == [(6123, 443)]
