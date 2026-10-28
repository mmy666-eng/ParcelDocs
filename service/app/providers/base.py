"""Provider interfaces (PRD §5.2). Polygons are shapely geometries in ITM (EPSG:2039)."""

from typing import Protocol

from shapely.geometry.base import BaseGeometry

from app.models import Document, Permit, Plan


class PlansProvider(Protocol):
    def find_by_polygon(self, polygon_itm: BaseGeometry) -> list[Plan]: ...

    def get_documents(self, plan: Plan) -> list[Document]: ...


class PermitsProvider(Protocol):
    def find_by_parcel(self, gush: int, helka: int) -> list[Permit]: ...

    def find_by_address(self, street: str, number: str) -> list[Permit]: ...

    def get_documents(self, permit: Permit) -> list[Document]: ...
