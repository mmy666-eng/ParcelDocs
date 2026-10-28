"""The resolve → download flow shared by the CLI and the FastAPI service (PRD §3).

resolve finds parcels, approved plans and granted permits without downloading;
download fetches the documents of the items the user kept and builds the folder.
"""

import logging
import re
import uuid
from datetime import date
from pathlib import Path

from pydantic import BaseModel
from shapely.geometry.base import BaseGeometry

from app.config import Config
from app.download.cache import DownloadCache
from app.download.downloader import Downloader, HttpClient, SourceError
from app.geo.cadastre import GovMapCadastre, check_itm, parcel_geometry
from app.models import Address, Document, ManifestEntry, Parcel, Permit, Plan
from app.output.organizer import OutputOrganizer
from app.providers.bnei_brak import (
    BneiBrakPermitsProvider,
    BneiBrakPlansProvider,
    ComplotClient,
    GushFile,
    is_granted,
)
from app.providers.iplan import IplanPlansProvider

log = logging.getLogger("parceldocs")

MANUAL_MAVAT = 'אין עותק במערכת העירונית – להוריד ידנית ממבא"ת'
MANUAL_NO_DOCUMENTS = "לא נמצאו הוראות או תשריט סרוקים – לפנות לוועדה"
MANUAL_CITY_UNAVAILABLE = 'המערכת העירונית לא זמינה – להוריד ידנית ממבא"ת או מאתר העירייה'
MANUAL_NOT_SCANNED = "לא סרוק – לפנות לארכיון"

CITY_SOURCE = "bnei_brak"
CITY_DISABLED = 'המערכת העירונית (Complot) מושבתת ב-config – תב"עות עירוניות ומסמכים לא נבדקו'
CITY_UNAVAILABLE = 'המערכת העירונית (Complot) לא זמינה – תב"עות עירוניות ומסמכים לא נבדקו'
PERMITS_NOT_CHECKED = "היתרי בנייה לא נבדקו – המערכת העירונית לא זמינה"
IPLAN_UNAVAILABLE = "מנהל התכנון (iplan) לא זמין – תכניות לפי מיקום לא נבדקו"

_NATIONAL_NUMBER = re.compile(r"\d{3}-\d{7}")
_LOCAL_NUMBER = re.compile(r"(?:בב/)?((?:מק/)?\d+(?:/[֐-׿\d]+)+)")


class NotFoundError(ValueError):
    pass


class ResolveResult(BaseModel):
    resolve_id: str
    gush: int
    helka: int
    parcels: list[Parcel]
    plans: list[Plan]
    plan_documents: dict[str, list[Document]]
    permits: list[Permit]
    permit_documents: dict[str, list[Document]]
    warnings: list[str]


class DownloadResult(BaseModel):
    folder: Path
    files: list[ManifestEntry]
    warnings: list[str]
    plans: int
    permits: int


def plan_id(plan: Plan) -> str:
    return f"plan:{plan.number}"


def permit_id(permit: Permit) -> str:
    return f"permit:{permit.permit_number or permit.request_number}"


def plan_keys(plan: Plan) -> tuple[set[str], set[str]]:
    """National (501-xxxxxxx) and local (מק/105/ה) plan numbers mentioned by a plan."""
    text = f"{plan.number} {plan.name}"
    national = set(_NATIONAL_NUMBER.findall(text))
    local = set(_LOCAL_NUMBER.findall(plan.number.replace(" ", "")))
    return national, local


def merge_plans(
    iplan_plans: list[Plan], city_plans: list[Plan], unmatched_action: str = MANUAL_MAVAT
) -> list[Plan]:
    """Join the two sources. City plans carry the documents; iplan adds type and mavat link.

    An iplan plan matches a city plan when its national number appears in the city
    plan's number or name, or when the city plan's local number appears in its name.
    """
    merged = [plan.model_copy(deep=True) for plan in city_plans]
    city_keys = [plan_keys(plan) for plan in city_plans]
    for iplan_plan in iplan_plans:
        match = None
        for index, (national, _) in enumerate(city_keys):
            if iplan_plan.number in national:
                match = index
                break
        if match is None:
            iplan_locals = set(_LOCAL_NUMBER.findall(iplan_plan.name))
            for index, (_, local) in enumerate(city_keys):
                if local & iplan_locals:
                    match = index
                    break
        if match is None:
            merged.append(iplan_plan.model_copy(update={"manual_action": unmatched_action}))
            continue
        city = merged[match]
        if city.number != iplan_plan.number:
            city.aliases = [*city.aliases, city.number]
            city.number = iplan_plan.number
        city.source = f"{city.source}+{iplan_plan.source}"
        city.plan_type = city.plan_type or iplan_plan.plan_type
        city.approval_date = city.approval_date or iplan_plan.approval_date
        city.mavat_url = iplan_plan.mavat_url
    return merged


def collect_permits(
    provider: BneiBrakPermitsProvider,
    parcels: list[tuple[int, int]],
    addresses: list[Address],
) -> list[Permit]:
    """Granted permits by parcel and by address (PRD §4.4), one entry per permit number.

    Old permits are often filed under the parcel number from before parcellation, so
    callers pass both the queried parcel and its municipal child parcels.
    """
    requests: dict[str, Permit] = {}
    for gush, helka in dict.fromkeys(parcels):
        for permit in provider.find_by_parcel(gush, helka):
            requests.setdefault(permit.request_number, permit)
    for address in addresses:
        if not address.number:
            continue
        for permit in provider.find_by_address(address.street, address.number):
            requests.setdefault(permit.request_number, permit)

    permits: dict[str, Permit] = {}
    for listed in requests.values():
        permit = provider.details(listed)
        if not is_granted(permit):
            log.info("request %s has no permit, skipped", permit.request_number)
            continue
        key = permit.permit_number or f"request:{permit.request_number}"
        if key in permits:
            log.info(
                "permit %s appears in requests %s and %s",
                key,
                permits[key].request_number,
                permit.request_number,
            )
            continue
        if not provider.get_documents(permit):
            permit.manual_action = MANUAL_NOT_SCANNED
        permits[key] = permit
    log.info("Complot: %d requests, %d granted permits", len(requests), len(permits))
    return list(permits.values())


def make_http(config: Config) -> HttpClient:
    return HttpClient(
        config.user_agent,
        config.requests_per_second,
        config.timeout_seconds,
        config.retries,
        legacy_tls_hosts=config.legacy_tls_hosts,
        host_min_interval_seconds=config.host_min_interval_seconds,
        rate_limited_wait_seconds=config.rate_limited_wait_seconds,
        host_max_requests=config.host_max_requests_per_run,
        fail_fast_429_hosts=config.fail_fast_429_hosts,
    )


class Pipeline:
    def __init__(self, config: Config, http: HttpClient | None = None) -> None:
        self.config = config
        self.http = http or make_http(config)
        sources = config.sources
        self.cadastre = GovMapCadastre(self.http, sources.govmap_wfs)
        complot = ComplotClient(
            self.http, sources.complot_base, sources.complot_site_id, sources.complot_ws
        )
        self.city_plans = BneiBrakPlansProvider(complot, self.cadastre, config.citywide_plans)
        self.city_permits = BneiBrakPermitsProvider(complot)
        self.iplan = IplanPlansProvider(self.http, sources.iplan_plans_layer)

    def resolve_parcel(self, gush: int, helka: int) -> ResolveResult:
        self.http.start_run()
        parcel = self.cadastre.get_parcel(gush, helka)
        geometry = parcel_geometry(parcel) if parcel else None
        query = geometry.buffer(self.config.parcel_buffer_m) if geometry else None
        warnings = []
        if geometry is None:
            warnings.append(
                f'החלקה {gush}/{helka} לא קיימת בקדסטר של מפ"י – חיפוש מרחבי ב-iplan דולג'
            )
        return self._resolve([(gush, helka)], [parcel] if parcel else [], query, warnings)

    def resolve_polygon(self, polygon: BaseGeometry) -> ResolveResult:
        check_itm(polygon)
        self.http.start_run()
        found = self.cadastre.find_by_polygon(polygon, self.config.parcel_buffer_m)
        if not found:
            raise NotFoundError('לא נמצאו חלקות של מפ"י בתוך הפוליגון')
        requested = sorted((p.gush, p.helka) for p in found)
        query = polygon.buffer(self.config.parcel_buffer_m)
        return self._resolve(requested, found, query, [])

    def _resolve(
        self,
        requested: list[tuple[int, int]],
        cadastre_parcels: list[Parcel],
        iplan_query: BaseGeometry | None,
        warnings: list[str],
    ) -> ResolveResult:
        city_available = CITY_SOURCE not in self.config.disabled_sources
        status = {(p.gush, p.helka): p.status for p in cadastre_parcels}
        resolved_by_request: dict[tuple[int, int], list[GushFile]] = {}
        if not city_available:
            warnings.append(CITY_DISABLED)
        else:
            try:
                for gush, helka in requested:
                    resolved = self.city_plans.resolve_parcels(gush, helka)
                    if [(g.gush, g.helka) for g in resolved] not in ([(gush, helka)], []):
                        children = ", ".join(f"{g.gush}/{g.helka}" for g in resolved)
                        warnings.append(
                            f"החלקה {gush}/{helka} עברה פרצלציה במערכת העירונית: {children}"
                        )
                    resolved_by_request[(gush, helka)] = resolved
            except SourceError as exc:
                log.error("Complot unavailable: %s", exc)
                warnings.append(f"{CITY_UNAVAILABLE}: {exc}")
                city_available = False

        # A parcel the city system does not know (or could not be asked about) is kept
        # with the cadastre's data only.
        gush_files: list[GushFile] = []
        for key in requested:
            resolved = resolved_by_request.get(key) or []
            if not resolved and key in status:
                resolved = [GushFile(gush=key[0], helka=key[1])]
            gush_files.extend(resolved)
        if not gush_files:
            first = requested[0]
            raise NotFoundError(
                f'גוש {first[0]} חלקה {first[1]} לא נמצאו לא במפ"י ולא במערכת העירונית'
            )
        unique = {(g.gush, g.helka): g for g in gush_files}
        parcels = [
            Parcel(gush=g.gush, helka=g.helka, addresses=g.addresses, status=status.get(key))
            for key, g in unique.items()
        ]

        iplan_plans: list[Plan] = []
        if iplan_query is not None:
            try:
                iplan_plans = self.iplan.find_by_polygon(iplan_query)
            except SourceError as exc:
                log.error("iplan unavailable: %s", exc)
                warnings.append(f"{IPLAN_UNAVAILABLE}: {exc}")

        city_plans: list[Plan] = []
        if city_available:
            try:
                city_plans = self.city_plans.find_by_parcels(list(unique.values()))
            except SourceError as exc:
                log.error("Complot unavailable: %s", exc)
                warnings.append(f"{CITY_UNAVAILABLE}: {exc}")
                city_available = False
        plans = merge_plans(
            iplan_plans, city_plans, MANUAL_MAVAT if city_available else MANUAL_CITY_UNAVAILABLE
        )
        plan_documents: dict[str, list[Document]] = {}
        for plan in plans:
            if plan.manual_action:
                continue
            selected = self.city_plans.get_documents(plan)
            if not selected:
                plan.manual_action = MANUAL_NO_DOCUMENTS
            plan_documents[plan_id(plan)] = selected

        permits: list[Permit] = []
        if city_available:
            addresses = [a for p in parcels for a in p.addresses]
            query_parcels = [*requested, *unique]
            try:
                permits = collect_permits(self.city_permits, query_parcels, addresses)
            except SourceError as exc:
                log.error("Complot unavailable: %s", exc)
                warnings.append(f"{PERMITS_NOT_CHECKED}: {exc}")
        else:
            warnings.append(PERMITS_NOT_CHECKED)
        permit_documents = {permit_id(p): self.city_permits.get_documents(p) for p in permits}

        return ResolveResult(
            resolve_id=uuid.uuid4().hex,
            gush=requested[0][0],
            helka=requested[0][1],
            parcels=parcels,
            plans=plans,
            plan_documents=plan_documents,
            permits=permits,
            permit_documents=permit_documents,
            warnings=warnings,
        )

    def organizer(self, result: ResolveResult, project: str, today: date | None) -> OutputOrganizer:
        return OutputOrganizer(
            self.config.output_dir, project, result.gush, result.helka, today or date.today()
        )

    def download(
        self,
        result: ResolveResult,
        project: str,
        selected: set[str] | None = None,
        today: date | None = None,
    ) -> DownloadResult:
        """Download the documents of the selected item ids (all items when None)."""
        organizer = self.organizer(result, project, today)
        organizer.create()
        plans = [p for p in result.plans if selected is None or plan_id(p) in selected]
        permits = [p for p in result.permits if selected is None or permit_id(p) in selected]
        warnings = list(result.warnings)

        downloads: list[tuple[str, Document, Path]] = []
        for plan in plans:
            documents = result.plan_documents.get(plan_id(plan), [])
            paths = organizer.document_paths(plan, documents)
            downloads.extend(
                (plan.number, doc, path) for doc, path in zip(documents, paths, strict=True)
            )
        for permit in permits:
            documents = result.permit_documents.get(permit_id(permit), [])
            paths = organizer.permit_document_paths(permit, documents)
            label = f"היתר {permit.permit_number or permit.request_number}"
            downloads.extend((label, doc, path) for doc, path in zip(documents, paths, strict=True))

        files: list[ManifestEntry] = []
        cache = DownloadCache(self.config.cache_db)
        downloader = Downloader(self.http, cache)
        try:
            for label, doc, path in downloads:
                try:
                    files.append(downloader.download(doc.url, path, doc.source, organizer.root))
                except SourceError as exc:
                    log.error("download failed: %s", exc)
                    warnings.append(f"הורדה נכשלה: {label} – {doc.title}")
        finally:
            cache.close()
        organizer.write_manifest(
            project, result.gush, result.helka, result.parcels, plans, files, warnings, permits
        )
        return DownloadResult(
            folder=organizer.root,
            files=files,
            warnings=warnings,
            plans=len(plans),
            permits=len(permits),
        )
