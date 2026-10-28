import httpx
import pytest
from shapely.geometry import box

from app.geo.cadastre import CoordinatesOutOfRangeError, GovMapCadastre, parcel_geometry
from tests.conftest import fixture_bytes, offline_http

WFS = "https://open.govmap.gov.il/geoserver/opendata/wfs"


def _cadastre(fixture: str, seen: list[httpx.Request]) -> GovMapCadastre:
    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=fixture_bytes(fixture))

    return GovMapCadastre(offline_http(handler), WFS)


def test_get_parcel_with_geometry():
    seen: list[httpx.Request] = []
    parcel = _cadastre("govmap_parcel_6123_443.json", seen).get_parcel(6123, 443)

    assert parcel is not None
    assert (parcel.gush, parcel.helka, parcel.status) == (6123, 443, "מוסדר")
    geometry = parcel_geometry(parcel)
    assert geometry is not None
    assert 480 < geometry.area < 520
    params = seen[0].url.params
    assert params["cql_filter"] == "GUSH_NUM=6123 AND PARCEL=443"
    assert params["srsName"] == "EPSG:2039"


def test_missing_parcel_returns_none():
    assert _cadastre("govmap_parcel_empty.json", []).get_parcel(6123, 2518) is None


def test_polygon_query_uses_negative_buffer():
    seen: list[httpx.Request] = []
    _cadastre("govmap_parcel_6123_443.json", seen).find_by_polygon(
        box(183600, 665800, 183700, 665900), buffer_m=-0.5
    )

    cql = seen[0].url.params["cql_filter"]
    assert cql.startswith("INTERSECTS(the_geom, SRID=2039;POLYGON")
    assert "183600.5" in cql


def test_wgs84_coordinates_rejected(no_network):
    cadastre = GovMapCadastre(offline_http(no_network), WFS)
    with pytest.raises(CoordinatesOutOfRangeError):
        cadastre.find_by_polygon(box(34.83, 32.08, 34.84, 32.09))
