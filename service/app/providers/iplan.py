"""Approved plans by location from the Planning Administration ArcGIS layer (docs/sources.md §3א).

Plan documents live on mavat, which requires reCAPTCHA (§3ד), so this provider
returns no documents; plans found only here are marked for manual download.
"""

import json
import logging
from datetime import UTC, datetime

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry

from app.download.downloader import HttpClient, SourceError
from app.models import Document, Plan

log = logging.getLogger(__name__)

SOURCE = "iplan"
APPROVED_STATUS = "אישור"
_FIELDS = "pl_number,pl_name,station_desc,entity_subtype_desc,pl_date_8,pl_url,mp_id"


class IplanPlansProvider:
    def __init__(self, http: HttpClient, layer_url: str) -> None:
        self._http = http
        self._query_url = layer_url.rstrip("/") + "/query"

    def find_by_polygon(self, polygon_itm: BaseGeometry) -> list[Plan]:
        # POST: polygon geometries make the URL too long for GET. Filtering on station_desc
        # is done here because a Hebrew where clause returns nothing on this server.
        response = self._http.post(
            self._query_url,
            data={
                "geometry": json.dumps(_esri_polygon(polygon_itm)),
                "geometryType": "esriGeometryPolygon",
                "inSR": "2039",
                "spatialRel": "esriSpatialRelIntersects",
                "outFields": _FIELDS,
                "returnGeometry": "false",
                "f": "json",
            },
        )
        payload = response.json()
        if "error" in payload:
            raise SourceError(f"iplan: {payload['error']}")
        features = payload.get("features", [])
        plans = [
            _to_plan(f["attributes"])
            for f in features
            if f["attributes"].get("station_desc") == APPROVED_STATUS
        ]
        log.info("iplan: %d plans intersect, %d approved", len(features), len(plans))
        return plans

    def get_documents(self, plan: Plan) -> list[Document]:
        return []


def _esri_polygon(geometry: BaseGeometry) -> dict:
    if isinstance(geometry, Polygon):
        polygons = [geometry]
    elif isinstance(geometry, MultiPolygon):
        polygons = list(geometry.geoms)
    else:
        raise ValueError(f"expected a polygon, got {geometry.geom_type}")
    rings = []
    for polygon in polygons:
        rings.append([list(xy) for xy in polygon.exterior.coords])
        rings.extend([list(xy) for xy in interior.coords] for interior in polygon.interiors)
    return {"rings": rings, "spatialReference": {"wkid": 2039}}


def _to_plan(attrs: dict) -> Plan:
    published = attrs.get("pl_date_8")
    return Plan(
        number=attrs["pl_number"].strip(),
        name=(attrs.get("pl_name") or "").strip(),
        status=attrs["station_desc"],
        source=SOURCE,
        plan_type=attrs.get("entity_subtype_desc"),
        approval_date=datetime.fromtimestamp(published / 1000, UTC).date() if published else None,
        source_id=str(int(attrs["mp_id"])) if attrs.get("mp_id") else None,
        mavat_url=attrs.get("pl_url"),
    )
