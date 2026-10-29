"""Approved plans by location from the Planning Administration ArcGIS layer (docs/sources.md §3א).

Plan documents live on mavat, which requires reCAPTCHA (§3ד), so this provider
returns no documents; plans found only here are marked for manual download. It does
return each plan's boundary, city and committee level, for the office library.
"""

import json
import logging
from datetime import UTC, datetime

from shapely.geometry import MultiPolygon, Polygon
from shapely.geometry.base import BaseGeometry
from shapely.ops import unary_union

from app.download.downloader import HttpClient, SourceError
from app.models import Document, Plan

log = logging.getLogger(__name__)

SOURCE = "iplan"
APPROVED_STATUS = "אישור"
_FIELDS = (
    "pl_number,pl_name,station_desc,entity_subtype_desc,pl_date_8,pl_url,mp_id,"
    "plan_county_name,jurstiction_area_name,pl_by_auth_of"
)


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
                "returnGeometry": "true",
                "outSR": "2039",
                "geometryPrecision": "2",
                "f": "json",
            },
        )
        payload = response.json()
        if "error" in payload:
            raise SourceError(f"iplan: {payload['error']}")
        features = payload.get("features", [])
        plans = [
            _to_plan(f["attributes"], f.get("geometry"))
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


def boundary_from_rings(geometry: dict | None) -> BaseGeometry | None:
    """An ArcGIS polygon (outer rings clockwise, holes counterclockwise) as a shapely shape."""
    rings = (geometry or {}).get("rings") or []
    outers: list[Polygon] = []
    holes: list[Polygon] = []
    for ring in rings:
        if len(ring) < 4:
            continue
        polygon = Polygon(ring)
        # Shapely's exterior.is_ccw is False for clockwise rings, which ArcGIS uses for outers.
        (holes if polygon.exterior.is_ccw else outers).append(polygon.buffer(0))
    if not outers:
        return None
    shape = unary_union(outers)
    if holes:
        shape = shape.difference(unary_union(holes))
    return shape if not shape.is_empty else None


def _to_plan(attrs: dict, geometry: dict | None = None) -> Plan:
    published = attrs.get("pl_date_8")
    boundary = boundary_from_rings(geometry)
    level = attrs.get("pl_by_auth_of")
    # A national plan has no city of its own (its jurisdiction field names one of many).
    city = "" if level == 1 else attrs.get("plan_county_name") or attrs.get("jurstiction_area_name")
    source_id = str(int(attrs["mp_id"])) if attrs.get("mp_id") else None
    return Plan(
        number=attrs["pl_number"].strip(),
        name=(attrs.get("pl_name") or "").strip(),
        status=attrs["station_desc"],
        source=SOURCE,
        plan_type=attrs.get("entity_subtype_desc"),
        approval_date=datetime.fromtimestamp(published / 1000, UTC).date() if published else None,
        source_id=source_id,
        source_ids={SOURCE: source_id} if source_id else {},
        mavat_url=attrs.get("pl_url"),
        city=(city or "").strip() or None,
        authority_level=int(level) if level else None,
        boundary_wkt=boundary.wkt if boundary is not None else None,
    )
