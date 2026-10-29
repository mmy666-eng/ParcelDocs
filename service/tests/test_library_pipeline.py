"""Runs with the office library: (א) search, (ב) discovery, (ג) missing files, (ד) update."""

import json
from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from app.cli import run
from app.config import Config
from app.output.office_library import SHARED_UNAVAILABLE
from tests.conftest import fixture_bytes, offline_http
from tests.test_cli import COMPLOT_PAGES, NOT_FOUND, PDF, _config, _rmi_response

DAY = date(2026, 10, 29)


def _handler(seen: list[str]):
    """Like test_cli's, with iplan boundaries and the municipal layer. seen gets one
    label per request: "muni", "iplan", "complot:<prgname>:<id>", or the host."""

    def handler(request: httpx.Request) -> httpx.Response:
        host = request.url.host
        query = parse_qs(urlsplit(str(request.url)).query)
        if host == "open.govmap.gov.il":
            if query["typeNames"][0] == "opendata:muni_il":
                seen.append("muni")
                return httpx.Response(200, content=fixture_bytes("govmap_muni_bnei_brak.json"))
            seen.append("parcels")
            return httpx.Response(200, content=fixture_bytes("govmap_parcel_6123_443.json"))
        if host == "ags.iplan.gov.il":
            seen.append("iplan")
            return httpx.Response(200, content=fixture_bytes("iplan_query_6123_443_geometry.json"))
        if host == "handasi.complot.co.il":
            if request.url.path.endswith(".asmx"):
                seen.append("complot:streets")
                return httpx.Response(200, content=fixture_bytes("complot_streets.xml"))
            key = (query["prgname"][0], query.get("h", query.get("n", query.get("b")))[0])
            seen.append(f"complot:{key[0]}:{key[1]}")
            name = COMPLOT_PAGES.get(key)
            if name is None:
                return httpx.Response(200, text=NOT_FOUND)
            return httpx.Response(200, content=fixture_bytes(name))
        seen.append(host)
        if host == "archive.gis-net.co.il":
            return httpx.Response(200, content=PDF + request.url.path.encode())
        if host == "apps.land.gov.il":
            return _rmi_response(request)
        raise AssertionError(f"unexpected host {host}")

    return handler


def _config_with_library(tmp_path: Path, disabled: list[str]) -> Config:
    config = _config(tmp_path, disabled)
    config.plans_library = tmp_path / "share"
    config.plans_library.mkdir(exist_ok=True)
    config.plans_library_local = tmp_path / "local"
    config.library_index = tmp_path / "index.sqlite"
    return config


def _run(config: Config, project: str) -> tuple[list[str], dict]:
    seen: list[str] = []
    assert run(config, 6123, 443, project, http=offline_http(_handler(seen)), today=DAY) == 0
    root = config.output_dir / f"{project}_ג6123_ח443_2026-10-29"
    manifest = json.loads((root / "00_סיכום" / "manifest.json").read_text(encoding="utf-8"))
    return seen, manifest


def _entry(tmp_path: Path, folder: str) -> dict:
    return json.loads((tmp_path / "share" / folder / "plan.json").read_text("utf-8"))


def test_library_fills_up_and_later_runs_ask_only_what_is_missing(tmp_path: Path):
    config = _config_with_library(tmp_path, [])

    seen, manifest = _run(config, "ראשון")

    assert seen.count("muni") == 1
    # The parcel file lists 14 plans; the fixtures have plan files for 3896 and 1201.
    assert sum(s.startswith("complot:GetTabaFile:") for s in seen) == 14
    plans = {p["number"]: p for p in manifest["plans"]}
    assert plans["501-0873794"]["scope"] == "כלל-עירונית"
    assert plans["תמא/ 60"]["scope"] == "ארצית"
    assert plans["מק/105/ה"]["scope"] == "ספציפית"
    roof = _entry(tmp_path, "בני_ברק/501-0873794")
    assert (roof["scope_basis"], roof["scope_note"]) == ("area", "הגבול מכסה 95% משטח הרשות")
    assert roof["boundary_wkt"].startswith("POLYGON")
    assert {(s["source"], s["id"]) for s in roof["sources"]} >= {("bnei_brak", "3896")}
    assert roof["parcels"] == []
    metro = _entry(tmp_path, "ארציות/תמא-_60")
    assert metro["city"] is None and metro["files"] == []
    city_only = _entry(tmp_path, "בני_ברק/מק-105-ה")
    assert city_only["scope_basis"] == "default"
    # Reported for the municipal child parcel 2518 of the requested parcel 443.
    assert city_only["parcels"] == [[6123, 443], [6123, 2518]]
    assert city_only["last_checked"] is not None

    seen, manifest = _run(config, "שני")

    # The city system's plan files are not needed again: complete in the library, or
    # listed there less than library_recheck_days ago. GovMap is not asked for the city.
    assert "complot:GetTabaFile:3896" not in seen and "complot:GetTabaFile:1201" not in seen
    assert sum(s.startswith("complot:GetTabaFile:") for s in seen) == 12
    assert "muni" not in seen
    assert "archive.gis-net.co.il" not in seen
    plans = {p["number"]: p for p in manifest["plans"]}
    assert plans["501-0873794"]["documents_source"] == "library"
    assert plans["מק/105/ה"]["documents_source"] == "library"
    plan_files = [f for f in manifest["files"] if f["path"].startswith("02_תבעות/")]
    assert {f["source"] for f in plan_files} == {"library"}


def test_plans_only_the_library_knows_are_kept_when_the_city_system_is_off(tmp_path: Path):
    _run(_config_with_library(tmp_path, []), "ראשון")

    seen, manifest = _run(_config_with_library(tmp_path, ["bnei_brak"]), "בלי_עירייה")

    assert not any(s.startswith("complot") for s in seen)
    plans = {p["number"]: p for p in manifest["plans"]}
    # Found through the parcel it was reported for; documents from the library.
    assert plans["מק/105/ה"]["source"] == "library"
    assert plans["מק/105/ה"]["documents_source"] == "library"
    assert plans["מק/105/ה"]["manual_action"] is None


def test_unreachable_share_warns_and_keeps_new_files_locally(tmp_path: Path):
    config = _config_with_library(tmp_path, ["bnei_brak"])
    config.plans_library = tmp_path / "offline-share"

    _, manifest = _run(config, "בלי_רשת")

    assert SHARED_UNAVAILABLE in manifest["warnings"]
    assert (tmp_path / "local" / "בני_ברק" / "501-0873794" / "הוראות.pdf").is_file()
    log = config.output_dir / "בלי_רשת_ג6123_ח443_2026-10-29" / "99_לוג" / "run.log"
    assert SHARED_UNAVAILABLE in log.read_text("utf-8")

    config.plans_library.mkdir()
    _run(config, "חזרה")
    assert (tmp_path / "offline-share" / "בני_ברק" / "501-0873794" / "הוראות.pdf").is_file()
    assert not (tmp_path / "local" / "בני_ברק" / "501-0873794").exists()
