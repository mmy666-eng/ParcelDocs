"""Domain models. All geometries are WKT in ITM (EPSG:2039)."""

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel, Field


class Address(BaseModel):
    city: str
    street: str
    number: str


class Parcel(BaseModel):
    gush: int
    helka: int
    geometry_wkt: str | None = None
    status: str | None = None
    # Municipality name from the cadastre (LOCALITY_N), e.g. "בני ברק".
    city: str | None = None
    addresses: list[Address] = []


class DocumentKind(StrEnum):
    INSTRUCTIONS = "הוראות"
    MAP = "תשריט"
    APPENDIX = "נספח"
    PERMIT = "היתר"


class PlanScope(StrEnum):
    """Where a plan applies: one area, a whole city, or a national/district plan."""

    SPECIFIC = "ספציפית"
    CITYWIDE = "כלל-עירונית"
    NATIONAL = "ארצית"


class Document(BaseModel):
    title: str
    url: str
    source: str
    kind: DocumentKind | None = None
    subject: str | None = None
    date: dt.date | None = None
    # For a copy from the office library: where the file first came from.
    origin_source: str | None = None
    origin_url: str | None = None


class Plan(BaseModel):
    number: str
    name: str
    status: str
    source: str
    aliases: list[str] = []
    plan_type: str | None = None
    approval_date: dt.date | None = None
    yalkut: str | None = None
    source_id: str | None = None
    mavat_url: str | None = None
    documents: list[Document] = []
    # Where the documents chosen for download come from (library / rmi / bnei_brak / mavat).
    documents_source: str | None = None
    manual_action: str | None = None
    city: str | None = None
    scope: PlanScope | None = None
    # Why the plan has this scope, e.g. "הגבול מכסה 95% משטח הרשות".
    scope_note: str | None = None
    # Committee level in iplan (pl_by_auth_of): 1 national, 2 district, 3 local.
    authority_level: int | None = None
    # The plan's id in every source that reported it, e.g. {"bnei_brak": "3896"}.
    source_ids: dict[str, str] = {}
    # Plan boundary (ITM WKT) from iplan. Large, so kept out of the manifest and the API.
    boundary_wkt: str | None = Field(default=None, exclude=True)


class Permit(BaseModel):
    request_number: str
    permit_number: str | None = None
    date: dt.date | None = None
    request_date: dt.date | None = None
    description: str | None = None
    status: str | None = None
    request_type: str | None = None
    building_file: str | None = None
    address: str | None = None
    gush: int | None = None
    helka: int | None = None
    has_documents: bool = False
    documents: list[Document] = []
    manual_action: str | None = None


class ManifestEntry(BaseModel):
    path: str
    source: str
    url: str
    downloaded_at: dt.datetime
    sha256: str
    from_cache: bool = False
    origin_source: str | None = None
    origin_url: str | None = None
