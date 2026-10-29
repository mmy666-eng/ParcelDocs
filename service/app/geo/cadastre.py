"""Gush/helka from the Survey of Israel cadastre via the GovMap open WFS (docs/sources.md §1).

Also the municipal boundaries (muni_il), used to tell city-wide plans apart (§1.4).
"""

import logging

from pydantic import BaseModel
from shapely import wkt
from shapely.geometry import shape
from shapely.geometry.base import BaseGeometry

from app.download.downloader import HttpClient, SourceError
from app.models import Parcel

log = logging.getLogger(__name__)

PARCELS_LAYER = "opendata:Parcels_ITM"
MUNICIPALITIES_LAYER = "opendata:muni_il"
_FIELDS = "the_geom,GUSH_NUM,PARCEL,STATUS_TEX,LOCALITY_N"
_MUNICIPALITY_FIELDS = "the_geom,Muni_Heb"

# Generous ITM envelope around Israel (EPSG:2039), used to reject wrong coordinate systems.
ITM_BOUNDS = (100_000.0, 350_000.0, 300_000.0, 800_000.0)


class CoordinatesOutOfRangeError(ValueError):
    pass


def check_itm(geometry: BaseGeometry) -> None:
    min_x, min_y, max_x, max_y = geometry.bounds
    lo_x, lo_y, hi_x, hi_y = ITM_BOUNDS
    if min_x < lo_x or min_y < lo_y or max_x > hi_x or max_y > hi_y:
        raise CoordinatesOutOfRangeError(
            f"הקואורדינטות ({min_x:.0f},{min_y:.0f})–({max_x:.0f},{max_y:.0f}) "
            "אינן בטווח רשת ישראל החדשה (ITM, EPSG:2039)"
        )


class Municipality(BaseModel):
    name: str
    boundary_wkt: str


class GovMapCadastre:
    def __init__(self, http: HttpClient, wfs_url: str) -> None:
        self._http = http
        self._wfs_url = wfs_url

    def get_parcel(self, gush: int, helka: int) -> Parcel | None:
        parcels = self._query(f"GUSH_NUM={int(gush)} AND PARCEL={int(helka)}")
        return parcels[0] if parcels else None

    def find_by_polygon(self, polygon: BaseGeometry, buffer_m: float = -0.5) -> list[Parcel]:
        check_itm(polygon)
        query = polygon.buffer(buffer_m) if buffer_m else polygon
        if query.is_empty:
            raise ValueError("הפוליגון קטן מדי אחרי הבאפר השלילי")
        return self._query(f"INTERSECTS(the_geom, SRID=2039;{query.wkt})")

    def municipality_at(self, x: float, y: float) -> Municipality | None:
        """The municipality whose boundary contains an ITM point."""
        features = self._features(
            MUNICIPALITIES_LAYER,
            _MUNICIPALITY_FIELDS,
            f"INTERSECTS(the_geom, SRID=2039;POINT({x:.2f} {y:.2f}))",
        )
        for feature in features:
            name = (feature["properties"].get("Muni_Heb") or "").strip()
            if name and feature.get("geometry"):
                log.info("GovMap: municipality %s at %.0f,%.0f", name, x, y)
                return Municipality(name=name, boundary_wkt=shape(feature["geometry"]).wkt)
        return None

    def _query(self, cql_filter: str) -> list[Parcel]:
        features = self._features(PARCELS_LAYER, _FIELDS, cql_filter)
        parcels = [_to_parcel(feature) for feature in features]
        log.info("GovMap: %d parcels for %s", len(parcels), cql_filter[:80])
        return parcels

    def _features(self, layer: str, fields: str, cql_filter: str) -> list[dict]:
        response = self._http.get(
            self._wfs_url,
            params={
                "service": "WFS",
                "version": "2.0.0",
                "request": "GetFeature",
                "typeNames": layer,
                "outputFormat": "application/json",
                "srsName": "EPSG:2039",
                "propertyName": fields,
                "cql_filter": cql_filter,
            },
        )
        try:
            return response.json()["features"]
        except (ValueError, KeyError) as exc:
            raise SourceError(f"GovMap WFS: unexpected response: {response.text[:200]}") from exc


def _to_parcel(feature: dict) -> Parcel:
    props = feature["properties"]
    geometry = feature.get("geometry")
    return Parcel(
        gush=int(props["GUSH_NUM"]),
        helka=int(props["PARCEL"]),
        status=props.get("STATUS_TEX"),
        city=(props.get("LOCALITY_N") or "").strip() or None,
        geometry_wkt=shape(geometry).wkt if geometry else None,
    )


def parcel_geometry(parcel: Parcel) -> BaseGeometry | None:
    return wkt.loads(parcel.geometry_wkt) if parcel.geometry_wkt else None
