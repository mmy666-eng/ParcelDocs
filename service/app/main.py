"""FastAPI service on localhost:8765 for the CAD plugin (PRD §5.3).

POST /resolve finds parcels, approved plans and granted permits for a polygon without
downloading. POST /download fetches the items the user kept and builds the folder.
"""

import logging
import threading
from collections import OrderedDict
from collections.abc import Callable
from datetime import date
from pathlib import Path
from typing import Literal

import uvicorn
from fastapi import FastAPI, HTTPException
from pydantic import BaseModel, Field
from shapely.geometry import Polygon

from app.config import Config, downloads_dir, load_config
from app.download.downloader import HttpClient, SourceError
from app.geo.cadastre import CoordinatesOutOfRangeError
from app.mavat_assist import AssistStatus, MavatAssist
from app.models import Address
from app.pipeline import (
    CITY_SOURCE,
    RMI_SOURCE,
    NotFoundError,
    Pipeline,
    ResolveResult,
    permit_id,
    plan_id,
)

log = logging.getLogger("parceldocs")

HOST = "127.0.0.1"
PORT = 8765
_KEEP_RESULTS = 20


class ResolveRequest(BaseModel):
    vertices: list[tuple[float, float]] = Field(min_length=3)
    crs: Literal["EPSG:2039"] = "EPSG:2039"


class ParcelOut(BaseModel):
    gush: int
    helka: int
    status: str | None
    addresses: list[Address]


class PlanOut(BaseModel):
    id: str
    number: str
    name: str
    plan_type: str | None
    approval_date: date | None
    yalkut: str | None
    documents: int
    documents_source: str | None
    manual_action: str | None
    mavat_url: str | None


class PermitOut(BaseModel):
    id: str
    permit_number: str | None
    request_number: str
    date: date | None
    description: str | None
    address: str | None
    documents: int
    manual_action: str | None


class ResolveResponse(BaseModel):
    resolve_id: str
    parcels: list[ParcelOut]
    plans: list[PlanOut]
    permits: list[PermitOut]
    warnings: list[str]


class DownloadRequest(BaseModel):
    resolve_id: str
    project: str = Field(min_length=1, max_length=80)
    selected: list[str]


class DownloadResponse(BaseModel):
    folder: str
    files: int
    from_cache: int
    plans: int
    permits: int
    warnings: list[str]
    # Downloaded plans still without instructions or a map (see /mavat/*).
    mavat_pending: int


class MavatStartRequest(BaseModel):
    folder: str


class MavatOpenRequest(BaseModel):
    plan_id: str


class MavatAssignRequest(BaseModel):
    name: str
    plan_id: str


def create_app(
    config: Config | None = None,
    http: HttpClient | None = None,
    opener: Callable[[str], None] | None = None,
) -> FastAPI:
    config = config or load_config()
    pipeline = Pipeline(config, http)
    results: OrderedDict[str, ResolveResult] = OrderedDict()
    # One run at a time: the per-host rate limits and request budgets are per process.
    lock = threading.Lock()
    # The mavat step: one project at a time. Each /mavat/status call also polls the
    # downloads folder, so the plugin's refresh timer drives it.
    assist: dict[str, MavatAssist] = {}
    app = FastAPI(title="ParcelDocs", version="0.1.0")

    @app.get("/health")
    def health() -> dict[str, object]:
        disabled = set(config.disabled_sources)
        library = pipeline.library
        return {
            "status": "ok",
            "output_dir": str(config.output_dir),
            # path, whether the shared folder answers, plans in the index, plans waiting
            # in the local copy for the share to come back.
            "library": library.status(timeout_seconds=2.0) if library else "off",
            "sources": {
                "govmap": "enabled",
                "iplan": "enabled",
                RMI_SOURCE: "disabled" if RMI_SOURCE in disabled else "enabled",
                CITY_SOURCE: "disabled" if CITY_SOURCE in disabled else "enabled",
                "library": str(config.plans_library) if config.plans_library else "off",
            },
        }

    @app.post("/resolve")
    def resolve(request: ResolveRequest) -> ResolveResponse:
        polygon = Polygon(request.vertices)
        if not polygon.is_valid or polygon.area <= 0:
            raise HTTPException(400, "הפוליליין אינו פוליגון סגור ותקין")
        with lock:
            try:
                result = pipeline.resolve_polygon(polygon)
            except CoordinatesOutOfRangeError as exc:
                raise HTTPException(422, str(exc)) from exc
            except NotFoundError as exc:
                raise HTTPException(404, str(exc)) from exc
            except SourceError as exc:
                raise HTTPException(502, f"שגיאה במקור נתונים: {exc}") from exc
        results[result.resolve_id] = result
        while len(results) > _KEEP_RESULTS:
            results.popitem(last=False)
        return _resolve_response(result)

    @app.post("/download")
    def download(request: DownloadRequest) -> DownloadResponse:
        result = results.get(request.resolve_id)
        if result is None:
            raise HTTPException(404, "תוצאת החיפוש לא נמצאה – יש להריץ שוב את PARCELDOCS")
        known = {plan_id(p) for p in result.plans} | {permit_id(p) for p in result.permits}
        unknown = set(request.selected) - known
        if unknown:
            raise HTTPException(400, f"פריטים לא מוכרים: {', '.join(sorted(unknown))}")
        with lock:
            downloaded = pipeline.download(result, request.project, set(request.selected))
        return DownloadResponse(
            folder=str(downloaded.folder),
            files=len(downloaded.files),
            from_cache=sum(1 for f in downloaded.files if f.from_cache),
            plans=downloaded.plans,
            permits=downloaded.permits,
            warnings=downloaded.warnings,
            mavat_pending=len(downloaded.mavat_pending),
        )

    @app.post("/mavat/start")
    def mavat_start(request: MavatStartRequest) -> AssistStatus:
        try:
            assist["current"] = MavatAssist(
                Path(request.folder),
                downloads_dir(config),
                pipeline.library,
                opener=opener,
                move_filed=config.move_filed_downloads,
            )
        except FileNotFoundError as exc:
            raise HTTPException(404, "בתיקייה אין 00_סיכום/manifest.json של ParcelDocs") from exc
        return assist["current"].status()

    @app.get("/mavat/status")
    def mavat_status() -> AssistStatus:
        current = _current(assist)
        current.poll()
        return current.status()

    @app.post("/mavat/open")
    def mavat_open(request: MavatOpenRequest) -> AssistStatus:
        current = _current(assist)
        try:
            current.open(request.plan_id)
        except KeyError as exc:
            raise HTTPException(404, f"תכנית לא מוכרת: {request.plan_id}") from exc
        except (ValueError, OSError) as exc:
            raise HTTPException(400, str(exc)) from exc
        return current.status()

    @app.post("/mavat/assign")
    def mavat_assign(request: MavatAssignRequest) -> AssistStatus:
        current = _current(assist)
        try:
            current.assign(request.name, request.plan_id)
        except (KeyError, FileNotFoundError) as exc:
            raise HTTPException(404, f"לא נמצא: {exc}") from exc
        return current.status()

    @app.post("/mavat/stop")
    def mavat_stop() -> AssistStatus:
        current = _current(assist)
        current.poll()
        del assist["current"]
        return current.status()

    return app


def _current(assist: dict[str, MavatAssist]) -> MavatAssist:
    current = assist.get("current")
    if current is None:
        raise HTTPException(404, 'עוזר מבא"ת לא פעיל – יש להתחיל עם /mavat/start')
    return current


def _resolve_response(result: ResolveResult) -> ResolveResponse:
    return ResolveResponse(
        resolve_id=result.resolve_id,
        parcels=[
            ParcelOut(gush=p.gush, helka=p.helka, status=p.status, addresses=p.addresses)
            for p in result.parcels
        ],
        plans=[
            PlanOut(
                id=plan_id(p),
                number=p.number,
                name=p.name,
                plan_type=p.plan_type,
                approval_date=p.approval_date,
                yalkut=p.yalkut,
                documents=len(result.plan_documents.get(plan_id(p), [])),
                documents_source=p.documents_source,
                manual_action=p.manual_action,
                mavat_url=p.mavat_url,
            )
            for p in result.plans
        ],
        permits=[
            PermitOut(
                id=permit_id(p),
                permit_number=p.permit_number,
                request_number=p.request_number,
                date=p.date or p.request_date,
                description=p.description,
                address=p.address,
                documents=len(result.permit_documents.get(permit_id(p), [])),
                manual_action=p.manual_action,
            )
            for p in result.permits
        ],
        warnings=result.warnings,
    )


def main() -> None:
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")
    logging.getLogger("httpx").setLevel(logging.WARNING)
    uvicorn.run(create_app(), host=HOST, port=PORT)


if __name__ == "__main__":
    main()
