from pathlib import Path

from fastapi.testclient import TestClient

from app.main import create_app
from tests.conftest import offline_http
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
