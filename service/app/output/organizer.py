"""Output folder layout (PRD §4.5). Every name goes through utils.filenames."""

import json
from datetime import date, datetime
from pathlib import Path

from shapely import wkt
from shapely.geometry.base import BaseGeometry

from app.models import Document, DocumentKind, ManifestEntry, Parcel, Permit, Plan
from app.utils.filenames import long_path, safe_name

SUMMARY_DIR = "00_סיכום"
PERMITS_DIR = "01_היתרי_בניה"
PLANS_DIR = "02_תבעות"
LOG_DIR = "99_לוג"
APPENDICES_DIR = "נספחים"

_PLAN_NAME_LENGTH = 40


def project_folder_name(project: str, gush: int, helka: int, day: date) -> str:
    return safe_name(f"{project}_ג{gush}_ח{helka}_{day:%Y-%m-%d}", max_length=120)


class OutputOrganizer:
    def __init__(self, base_dir: Path, project: str, gush: int, helka: int, day: date) -> None:
        self._set_root(base_dir / project_folder_name(project, gush, helka, day))

    @classmethod
    def from_root(cls, root: Path) -> "OutputOrganizer":
        """An existing project folder (as when filing mavat downloads into it later)."""
        organizer = cls.__new__(cls)
        organizer._set_root(root)
        return organizer

    def _set_root(self, root: Path) -> None:
        self.root = root
        self.summary_dir = self.root / SUMMARY_DIR
        self.permits_dir = self.root / PERMITS_DIR
        self.plans_dir = self.root / PLANS_DIR
        self.log_dir = self.root / LOG_DIR

    @property
    def manifest_file(self) -> Path:
        return self.summary_dir / "manifest.json"

    def create(self) -> None:
        for folder in (self.summary_dir, self.permits_dir, self.plans_dir, self.log_dir):
            long_path(folder).mkdir(parents=True, exist_ok=True)

    @property
    def log_file(self) -> Path:
        return self.log_dir / "run.log"

    def plan_dir(self, plan: Plan) -> Path:
        short_name = safe_name(plan.name, max_length=_PLAN_NAME_LENGTH)
        return self.plans_dir / safe_name(f"{plan.number}_{short_name}", max_length=90)

    def document_paths(self, plan: Plan, documents: list[Document]) -> list[Path]:
        """Target path per document: הוראות.pdf, תשריט.pdf, תשריט_2.pdf, נספחים/<title>.pdf."""
        folder = self.plan_dir(plan)
        counts: dict[DocumentKind, int] = {}
        totals = {kind: sum(1 for d in documents if d.kind == kind) for kind in DocumentKind}
        used: set[str] = set()
        paths = []
        for doc in documents:
            kind = doc.kind or DocumentKind.APPENDIX
            counts[kind] = counts.get(kind, 0) + 1
            if kind == DocumentKind.APPENDIX:
                name = safe_name(f"{doc.title}.pdf", max_length=80)
                stem = name.removesuffix(".pdf")
                suffix = 2
                while name in used:
                    name = f"{stem}_{suffix}.pdf"
                    suffix += 1
                used.add(name)
                paths.append(folder / APPENDICES_DIR / name)
            elif totals[kind] == 1 or counts[kind] == 1:
                paths.append(folder / f"{kind.value}.pdf")
            else:
                paths.append(folder / f"{kind.value}_{counts[kind]}.pdf")
        return paths

    def permit_dir(self, permit: Permit) -> Path:
        """<שנה>_היתר_<מספר>, or <שנה>_בקשה_<מספר> when the permit number is unknown."""
        when = permit.date or permit.request_date
        year = f"{when.year}" if when else "ללא_תאריך"
        if permit.permit_number:
            name = f"{year}_היתר_{permit.permit_number}"
        else:
            name = f"{year}_בקשה_{permit.request_number}"
        return self.permits_dir / safe_name(name)

    def permit_document_paths(self, permit: Permit, documents: list[Document]) -> list[Path]:
        """היתר.pdf (היתר_2.pdf, ...) and תשריט_<n>.pdf, as in PRD §4.5."""
        folder = self.permit_dir(permit)
        permits = maps = 0
        paths = []
        for doc in documents:
            if doc.kind == DocumentKind.MAP:
                maps += 1
                paths.append(folder / f"תשריט_{maps}.pdf")
            else:
                permits += 1
                paths.append(folder / ("היתר.pdf" if permits == 1 else f"היתר_{permits}.pdf"))
        return paths

    def write_manifest(
        self,
        project: str,
        gush: int,
        helka: int,
        parcels: list[Parcel],
        plans: list[Plan],
        files: list[ManifestEntry],
        warnings: list[str],
        permits: list[Permit] | None = None,
        applicable: list[Plan] | None = None,
    ) -> Path:
        manifest = {
            "project": project,
            "query": {"gush": gush, "helka": helka},
            "created_at": datetime.now().astimezone().isoformat(),
            "parcels": [p.model_dump(mode="json", exclude={"geometry_wkt"}) for p in parcels],
            "plans": [
                p.model_dump(mode="json", exclude={"documents"})
                | {"documents": [d.model_dump(mode="json") for d in p.documents]}
                for p in plans
            ],
            "permits": [p.model_dump(mode="json") for p in permits or []],
            # The report's "חלות על החלקה – לא הורדו": city-wide and national plans.
            "applicable_plans": [
                p.model_dump(
                    mode="json", exclude={"documents", "documents_source", "manual_action"}
                )
                for p in applicable or []
            ],
            "files": [f.model_dump(mode="json") for f in files],
            "warnings": warnings,
        }
        path = self.manifest_file
        long_path(path).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path

    def write_log(self, lines: list[str]) -> Path:
        """Append one run's log lines; reruns into the same folder keep earlier runs."""
        with long_path(self.log_file).open("a", encoding="utf-8") as handle:
            handle.writelines(f"{line}\n" for line in lines)
        return self.log_file

    def write_polygon_dxf(self, polygon_wkt: str, parcels: list[Parcel]) -> Path:
        """The query polygon and the cadastre parcels in ITM, as DXF R12 (ASCII only)."""
        entities: list[str] = []
        for ring in _rings(wkt.loads(polygon_wkt)):
            entities.extend(_dxf_polyline(ring, POLYGON_LAYER))
        for parcel in parcels:
            if not parcel.geometry_wkt:
                continue
            geometry = wkt.loads(parcel.geometry_wkt)
            for ring in _rings(geometry):
                entities.extend(_dxf_polyline(ring, PARCELS_LAYER))
            point = geometry.representative_point()
            entities.extend(
                _dxf_text(point.x, point.y, f"{parcel.gush}/{parcel.helka}", PARCELS_LAYER)
            )
        lines = ["0", "SECTION", "2", "HEADER", "9", "$ACADVER", "1", "AC1009", "0", "ENDSEC"]
        lines += ["0", "SECTION", "2", "ENTITIES", *entities, "0", "ENDSEC", "0", "EOF"]
        path = self.summary_dir / "polygon.dxf"
        long_path(path).write_text("\n".join(lines) + "\n", encoding="ascii")
        return path


POLYGON_LAYER = "PARCELDOCS_POLYGON"
PARCELS_LAYER = "PARCELDOCS_PARCELS"


def _rings(geometry: BaseGeometry) -> list[list[tuple[float, float]]]:
    polygons = getattr(geometry, "geoms", [geometry])
    return [
        [(x, y) for x, y, *_ in ring.coords[:-1]]
        for polygon in polygons
        for ring in (polygon.exterior, *polygon.interiors)
    ]


def _dxf_polyline(points: list[tuple[float, float]], layer: str) -> list[str]:
    lines = ["0", "POLYLINE", "8", layer, "66", "1", "70", "1"]
    for x, y in points:
        lines += ["0", "VERTEX", "8", layer, "10", f"{x:.3f}", "20", f"{y:.3f}", "30", "0.0"]
    return [*lines, "0", "SEQEND", "8", layer]


def _dxf_text(x: float, y: float, text: str, layer: str) -> list[str]:
    return ["0", "TEXT", "8", layer, "10", f"{x:.3f}", "20", f"{y:.3f}", "30", "0.0",
            "40", "1.0", "1", text]  # fmt: skip
