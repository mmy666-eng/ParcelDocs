"""Domain models. All geometries are WKT in ITM (EPSG:2039)."""

import datetime as dt
from enum import StrEnum

from pydantic import BaseModel


class Address(BaseModel):
    city: str
    street: str
    number: str


class Parcel(BaseModel):
    gush: int
    helka: int
    geometry_wkt: str | None = None
    status: str | None = None
    addresses: list[Address] = []


class DocumentKind(StrEnum):
    INSTRUCTIONS = "הוראות"
    MAP = "תשריט"
    APPENDIX = "נספח"
    PERMIT = "היתר"


class Document(BaseModel):
    title: str
    url: str
    source: str
    kind: DocumentKind | None = None
    subject: str | None = None
    date: dt.date | None = None


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
    manual_action: str | None = None


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
