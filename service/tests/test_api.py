from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import make_pdf, offline_http
from tests.test_cli import _config, _handler

PARCEL_443 = [
    [183609.5, 665815.8],
    [183635.1, 665817.8],
    [183633.5, 665837.5],
    [183608.2, 665835.3],
]


def _client(tmp_path: Path, disabled: list[str]) -> tuple[TestClient, list[str]]:
    hosts: list[str] = []
    app = create_app(_config(tmp_path, disabled), offline_http(_handler(hosts)))
    return TestClient(app), hosts


def test_health(tmp_path: Path):
    client, _ = _client(tmp_path, ["bnei_brak"])
    body = client.get("/health").json()
    assert body["status"] == "ok"
    assert body["sources"]["bnei_brak"] == "disabled"
    assert body["library"] == "off"


def test_health_reports_the_library(tmp_path: Path):
    config = _config(tmp_path, ["bnei_brak"])
    config.plans_library = tmp_path / "share"
    client = TestClient(create_app(config, offline_http(_handler([]))))
    library = client.get("/health").json()["library"]
    assert library["available"] is False and library["pending_local"] == 0
    config.plans_library.mkdir()
    library = client.get("/health").json()["library"]
    assert library["available"] is True and library["path"] == str(tmp_path / "share")


def test_resolve_then_download_selected_items(tmp_path: Path):
    client, hosts = _client(tmp_path, [])

    resolved = client.post("/resolve", json={"vertices": PARCEL_443, "crs": "EPSG:2039"})

    assert resolved.status_code == 200
    body = resolved.json()
    assert [(p["gush"], p["helka"]) for p in body["parcels"]] == [(6123, 2518)]
    plans = {p["number"]: p for p in body["plans"]}
    assert plans["501-0873794"]["documents"] == 3
    assert [p["permit_number"] for p in body["permits"]] == ["9545"]
    assert "archive.gis-net.co.il" not in hosts  # nothing downloaded yet

    selected = [plans["501-0873794"]["id"], body["permits"][0]["id"]]
    downloaded = client.post(
        "/download",
        json={"resolve_id": body["resolve_id"], "project": "בדיקה", "selected": selected},
    )

    assert downloaded.status_code == 200
    result = downloaded.json()
    assert (result["plans"], result["permits"], result["files"]) == (1, 1, 4)
    folder = Path(result["folder"])
    assert (folder / "00_סיכום" / "manifest.json").is_file()
    assert (folder / "01_היתרי_בניה" / "1989_היתר_9545" / "היתר.pdf").is_file()
    # PRD §4.5: the run log covers resolve and download; the DXF has the user's polygon.
    run_log = (folder / "99_לוג" / "run.log").read_text(encoding="utf-8")
    assert "resolve polygon with 4 vertices" in run_log
    assert "download: project=בדיקה" in run_log
    dxf = (folder / "00_סיכום" / "polygon.dxf").read_text(encoding="ascii")
    assert "PARCELDOCS_POLYGON" in dxf
    assert "183609.500" in dxf and "665815.800" in dxf
    assert dxf.rstrip().endswith("EOF")


def test_resolve_rejects_wgs84(tmp_path: Path):
    client, hosts = _client(tmp_path, [])
    wgs84 = [[34.83, 32.08], [34.84, 32.08], [34.84, 32.09]]

    response = client.post("/resolve", json={"vertices": wgs84})

    assert response.status_code == 422
    assert "ITM" in response.json()["detail"]
    assert hosts == []


def test_resolve_rejects_other_crs_and_degenerate_polygon(tmp_path: Path):
    client, _ = _client(tmp_path, [])
    assert (
        client.post("/resolve", json={"vertices": PARCEL_443, "crs": "EPSG:4326"}).status_code
        == 422
    )
    line = [[183600, 665800], [183700, 665800], [183650, 665800]]
    assert client.post("/resolve", json={"vertices": line}).status_code == 400


def test_download_unknown_result_or_item(tmp_path: Path):
    client, _ = _client(tmp_path, ["bnei_brak"])
    missing = client.post("/download", json={"resolve_id": "x", "project": "p", "selected": []})
    assert missing.status_code == 404

    body = client.post("/resolve", json={"vertices": PARCEL_443}).json()
    bad = client.post(
        "/download",
        json={"resolve_id": body["resolve_id"], "project": "p", "selected": ["plan:nope"]},
    )
    assert bad.status_code == 400


def test_mavat_step_files_manual_downloads(tmp_path: Path):
    config = _config(tmp_path, ["bnei_brak"])
    config.downloads_dir = tmp_path / "Downloads"
    config.downloads_dir.mkdir()
    config.plans_library = tmp_path / "library"
    config.plans_library.mkdir()
    opened: list[str] = []
    client = TestClient(create_app(config, offline_http(_handler([])), opener=opened.append))
    body = client.post("/resolve", json={"vertices": PARCEL_443}).json()
    plans = {p["number"]: p for p in body["plans"]}
    assert plans["501-0873794"]["documents_source"] == "rmi"
    assert plans["501-0175646"]["documents"] == 0
    selected = [plans["501-0873794"]["id"], plans["501-0175646"]["id"]]
    downloaded = client.post(
        "/download",
        json={"resolve_id": body["resolve_id"], "project": "בדיקה", "selected": selected},
    ).json()
    assert downloaded["mavat_pending"] == 1

    status = client.post("/mavat/start", json={"folder": downloaded["folder"]}).json()
    assert {p["number"]: p["done"] for p in status["plans"]} == {
        "501-0873794": True,
        "501-0175646": False,
    }
    client.post("/mavat/open", json={"plan_id": plans["501-0175646"]["id"]})
    assert opened == [plans["501-0175646"]["mavat_url"]]
    (config.downloads_dir / "תקנון 501-0175646.pdf").write_bytes(make_pdf("a"))
    (config.downloads_dir / "1234.pdf").write_bytes(make_pdf("Plan 501-0175646", 3370, 2384))
    client.get("/mavat/status")
    status = client.get("/mavat/status").json()

    assert sorted(f["kind"] for f in status["filed"]) == ["הוראות", "תשריט"]
    assert all(p["done"] for p in status["plans"])
    assert client.post("/mavat/stop").status_code == 200
    assert client.get("/mavat/status").status_code == 404
    assert (tmp_path / "library" / "בני_ברק" / "501-0175646" / "הוראות.pdf").is_file()


def test_mavat_start_needs_a_project_folder(tmp_path: Path):
    client, _ = _client(tmp_path, ["bnei_brak"])
    assert client.post("/mavat/start", json={"folder": str(tmp_path)}).status_code == 404
