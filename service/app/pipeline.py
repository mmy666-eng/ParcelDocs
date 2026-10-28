"""The resolve → download flow shared by the CLI and the FastAPI service (PRD §3).

resolve finds parcels, approved plans and granted permits without downloading;
download fetches the documents of the items the user kept and builds the folder.

Plan names come from iplan, the Israel Land Authority (rmi) and the city system.
Plan documents are taken from the first of: the office library (when it has both the
instructions and a map), rmi, the city system, a partial library entry. Plans with
none of these are left for the semi-automatic mavat step (app.mavat_assist).
"""

import logging
import re
import threading
import uuid
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from pydantic import BaseModel
from shapely.geometry.base import BaseGeometry

from app.config import Config
from app.download.cache import DownloadCache
from app.download.downloader import Downloader, HttpClient, SourceError
from app.geo.cadastre import GovMapCadastre, check_itm, parcel_geometry
from app.models import Address, Document, DocumentKind, ManifestEntry, Parcel, Permit, Plan
from app.output.library import LIBRARY_SOURCE, LibraryError, PlansLibrary
from app.output.organizer import OutputOrganizer
from app.providers.bnei_brak import (
    BneiBrakPermitsProvider,
    BneiBrakPlansProvider,
    ComplotClient,
    GushFile,
    is_granted,
    select_documents,
)
from app.providers.iplan import IplanPlansProvider
from app.providers.rmi import SOURCE as RMI_SOURCE
from app.providers.rmi import RmiPlansProvider

log = logging.getLogger("parceldocs")

MANUAL_MAVAT = 'אין עותק בספרייה, ברמ"י או במערכת העירונית – להוריד ממבא"ת (עוזר מבא"ת)'
MANUAL_NO_DOCUMENTS = "לא נמצאו הוראות או תשריט סרוקים – לפנות לוועדה"
MANUAL_CITY_UNAVAILABLE = 'המערכת העירונית לא זמינה – להוריד ממבא"ת (עוזר מבא"ת) או מאתר העירייה'
MANUAL_NOT_SCANNED = "לא סרוק – לפנות לארכיון"

CITY_SOURCE = "bnei_brak"
# The cadastre has no addresses; they come only from the city system.
CITY_DISABLED = (
    'המערכת העירונית (Complot) מושבתת ב-config – כתובות, תב"עות עירוניות ומסמכים לא נבדקו'
)
CITY_UNAVAILABLE = 'המערכת העירונית (Complot) לא זמינה – כתובות, תב"עות עירוניות ומסמכים לא נבדקו'
PERMITS_NOT_CHECKED = "היתרי בנייה לא נבדקו – המערכת העירונית לא זמינה"
IPLAN_UNAVAILABLE = "מנהל התכנון (iplan) לא זמין – תכניות לפי מיקום לא נבדקו"
RMI_UNAVAILABLE = 'רמ"י (איתור תוכניות) לא זמין – תכניות ומסמכים ממנו לא נבדקו'
LIBRARY_UNAVAILABLE = "הספרייה המשרדית לא זמינה"

CORE_KINDS = (DocumentKind.INSTRUCTIONS, DocumentKind.MAP)

_NATIONAL_NUMBER = re.compile(r"\d{3}-\d{7}")
_LOCAL_NUMBER = re.compile(r"(?:בב/)?((?:מק/)?\d+(?:/[֐-׿\d]+)+)")
_CITY_PREFIX = re.compile(r"^בב\s*/\s*")
_NOT_KEY_CHARS = re.compile(r"[\W_]+")


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
    polygon_wkt: str | None = None
    # Log lines of the resolve step; the folder (and run.log) exists only at download.
    log: list[str] = []


class DownloadResult(BaseModel):
    folder: Path
    files: list[ManifestEntry]
    warnings: list[str]
    plans: int
    permits: int
    # Selected plans still without instructions or a map: candidates for mavat.
    mavat_pending: list[str] = []


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


def is_national_number(number: str) -> bool:
    return _NATIONAL_NUMBER.fullmatch(number.strip()) is not None


def local_key(number: str) -> str | None:
    """Comparable form of a local plan number: rmi "בב/105א" and Complot "105/א" → "105א"."""
    if is_national_number(number):
        return None
    return _NOT_KEY_CHARS.sub("", _CITY_PREFIX.sub("", number.strip())) or None


def merge_plans(iplan_plans: list[Plan], city_plans: list[Plan]) -> list[Plan]:
    """Join iplan and the city system. City plans carry documents; iplan adds type and mavat.

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
            merged.append(iplan_plan.model_copy(deep=True))
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


def merge_rmi(plans: list[Plan], rmi_plans: list[Plan]) -> list[Plan]:
    """Add the Israel Land Authority's plans: their documents join a matching plan, and
    plans no other source knows (often old local plans) are added as they are."""
    merged = [plan.model_copy(deep=True) for plan in plans]
    for rmi_plan in rmi_plans:
        index = _match_rmi(merged, rmi_plan)
        if index is None:
            merged.append(rmi_plan.model_copy(deep=True))
            continue
        plan = merged[index]
        if RMI_SOURCE in plan.source.split("+"):
            continue
        if rmi_plan.number != plan.number and rmi_plan.number not in plan.aliases:
            plan.aliases = [*plan.aliases, rmi_plan.number]
        plan.source = f"{plan.source}+{RMI_SOURCE}"
        plan.approval_date = plan.approval_date or rmi_plan.approval_date
        plan.name = plan.name or rmi_plan.name
        plan.documents = [*plan.documents, *rmi_plan.documents]
    return merged


def _match_rmi(plans: list[Plan], rmi_plan: Plan) -> int | None:
    number = rmi_plan.number
    if is_national_number(number):
        for index, plan in enumerate(plans):
            if number == plan.number or number in plan.aliases:
                return index
        # A city plan that only mentions the national number in its name.
        for index, plan in enumerate(plans):
            if not is_national_number(plan.number) and number in plan_keys(plan)[0]:
                return index
        return None
    key = local_key(number)
    for index, plan in enumerate(plans):
        if key and key in {local_key(n) for n in [plan.number, *plan.aliases]}:
            return index
    return None


def has_core_documents(documents: list[Document]) -> bool:
    kinds = {d.kind for d in documents}
    return all(kind in kinds for kind in CORE_KINDS)


def choose_documents(plan: Plan, library_documents: list[Document]) -> tuple[list[Document], str]:
    """The documents to download and where they come from; ([], "") when there are none.

    Order: a complete library entry, rmi, the city system (approved versions), and a
    partial library entry. A source with instructions or a map beats one with only
    appendices.
    """
    candidates = [
        (library_documents if has_core_documents(library_documents) else [], LIBRARY_SOURCE),
        ([d for d in plan.documents if d.source == RMI_SOURCE], RMI_SOURCE),
        (select_documents([d for d in plan.documents if d.source == CITY_SOURCE]), CITY_SOURCE),
        (library_documents, LIBRARY_SOURCE),
    ]
    for documents, source in candidates:
        if any(d.kind in CORE_KINDS for d in documents):
            return documents, source
    for documents, source in candidates:
        if documents:
            return documents, source
    return [], ""


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


class _RunLog(logging.Handler):
    """Collects this thread's parceldocs log lines for 99_לוג/run.log."""

    def __init__(self) -> None:
        super().__init__(logging.INFO)
        self.lines: list[str] = []
        self._thread = threading.get_ident()
        self.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))

    def emit(self, record: logging.LogRecord) -> None:
        if record.thread == self._thread:
            self.lines.append(self.format(record))


@contextmanager
def capture_log() -> Iterator[_RunLog]:
    handler = _RunLog()
    previous = log.level
    log.addHandler(handler)
    if log.getEffectiveLevel() > logging.INFO:
        log.setLevel(logging.INFO)
    try:
        yield handler
    finally:
        log.removeHandler(handler)
        log.setLevel(previous)


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


def make_library(config: Config) -> PlansLibrary | None:
    return PlansLibrary(config.plans_library) if config.plans_library else None


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
        self.rmi = RmiPlansProvider(self.http, sources.rmi_api, sources.rmi_files)
        self.library = make_library(config)

    def resolve_parcel(self, gush: int, helka: int) -> ResolveResult:
        self.http.start_run()
        with capture_log() as run_log:
            log.info("resolve parcel %d/%d", gush, helka)
            parcel = self.cadastre.get_parcel(gush, helka)
            geometry = parcel_geometry(parcel) if parcel else None
            query = geometry.buffer(self.config.parcel_buffer_m) if geometry else None
            warnings = []
            if geometry is None:
                warnings.append(
                    f'החלקה {gush}/{helka} לא קיימת בקדסטר של מפ"י – חיפוש מרחבי ב-iplan דולג'
                )
            result = self._resolve([(gush, helka)], [parcel] if parcel else [], query, warnings)
        return result.model_copy(
            update={"polygon_wkt": geometry.wkt if geometry else None, "log": run_log.lines}
        )

    def resolve_polygon(self, polygon: BaseGeometry) -> ResolveResult:
        check_itm(polygon)
        self.http.start_run()
        with capture_log() as run_log:
            log.info("resolve polygon with %d vertices", len(polygon.exterior.coords) - 1)
            found = self.cadastre.find_by_polygon(polygon, self.config.parcel_buffer_m)
            if not found:
                raise NotFoundError('לא נמצאו חלקות של מפ"י בתוך הפוליגון')
            requested = sorted((p.gush, p.helka) for p in found)
            query = polygon.buffer(self.config.parcel_buffer_m)
            result = self._resolve(requested, found, query, [])
        return result.model_copy(update={"polygon_wkt": polygon.wkt, "log": run_log.lines})

    def _resolve(
        self,
        requested: list[tuple[int, int]],
        cadastre_parcels: list[Parcel],
        iplan_query: BaseGeometry | None,
        warnings: list[str],
    ) -> ResolveResult:
        city_available = CITY_SOURCE not in self.config.disabled_sources
        status = {(p.gush, p.helka): p.status for p in cadastre_parcels}
        geometries = {(p.gush, p.helka): p.geometry_wkt for p in cadastre_parcels}
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
            Parcel(
                gush=g.gush,
                helka=g.helka,
                addresses=g.addresses,
                status=status.get(key),
                geometry_wkt=geometries.get(key),
            )
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
        plans = merge_plans(iplan_plans, city_plans)

        if RMI_SOURCE not in self.config.disabled_sources:
            try:
                rmi_parcels = list(dict.fromkeys([*requested, *unique]))
                plans = merge_rmi(plans, self._rmi_plans(rmi_parcels, plans))
            except SourceError as exc:
                log.error("rmi unavailable: %s", exc)
                warnings.append(f"{RMI_UNAVAILABLE}: {exc}")

        plan_documents = self._plan_documents(plans, city_available, warnings)

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

    def _rmi_plans(self, parcels: list[tuple[int, int]], known: list[Plan]) -> list[Plan]:
        """rmi plans by parcel, plus a lookup by number for national-numbered plans that
        other sources found but rmi did not link to these parcels."""
        found: dict[str, Plan] = {}
        for gush, helka in parcels:
            for plan in self.rmi.find_by_parcel(gush, helka):
                found.setdefault(plan.source_id or plan.number, plan)
        numbers = {p.number for p in found.values()}
        for plan in known:
            if is_national_number(plan.number) and plan.number not in numbers:
                for extra in self.rmi.find_by_number(plan.number):
                    found.setdefault(extra.source_id or extra.number, extra)
        return list(found.values())

    def _plan_documents(
        self, plans: list[Plan], city_available: bool, warnings: list[str]
    ) -> dict[str, list[Document]]:
        library = self.library
        plan_documents: dict[str, list[Document]] = {}
        for plan in plans:
            library_documents: list[Document] = []
            if library is not None:
                try:
                    library_documents = library.documents(plan)
                except LibraryError as exc:
                    log.error("library unavailable: %s", exc)
                    warnings.append(f"{LIBRARY_UNAVAILABLE}: {exc}")
                    library = None
            documents, source = choose_documents(plan, library_documents)
            plan.documents_source = source or None
            if not documents:
                if not city_available:
                    plan.manual_action = MANUAL_CITY_UNAVAILABLE
                elif plan.mavat_url:
                    plan.manual_action = MANUAL_MAVAT
                else:
                    plan.manual_action = MANUAL_NO_DOCUMENTS
                continue
            plan_documents[plan_id(plan)] = documents
            missing = [k.value for k in CORE_KINDS if all(d.kind != k for d in documents)]
            if missing:
                warnings.append(f'תכנית {plan.number}: חסרים {", ".join(missing)} – להשלים ממבא"ת')
            log.info("plan %s: %d documents from %s", plan.number, len(documents), source)
        return plan_documents

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
        with capture_log() as run_log:
            log.info("download: project=%s selected=%s", project, sorted(selected or []) or "all")
            downloaded = self._download(result, organizer, project, selected)
        organizer.write_log([*result.log, *run_log.lines])
        return downloaded

    def _download(
        self,
        result: ResolveResult,
        organizer: OutputOrganizer,
        project: str,
        selected: set[str] | None,
    ) -> DownloadResult:
        plans = [p for p in result.plans if selected is None or plan_id(p) in selected]
        permits = [p for p in result.permits if selected is None or permit_id(p) in selected]
        warnings = list(result.warnings)

        downloads: list[tuple[str, Plan | None, Document, Path]] = []
        for plan in plans:
            documents = result.plan_documents.get(plan_id(plan), [])
            paths = organizer.document_paths(plan, documents)
            downloads.extend(
                (plan.number, plan, doc, path) for doc, path in zip(documents, paths, strict=True)
            )
        for permit in permits:
            documents = result.permit_documents.get(permit_id(permit), [])
            paths = organizer.permit_document_paths(permit, documents)
            label = f"היתר {permit.permit_number or permit.request_number}"
            downloads.extend(
                (label, None, doc, path) for doc, path in zip(documents, paths, strict=True)
            )

        files: list[ManifestEntry] = []
        done: dict[str, list[tuple[Document, Path]]] = {}
        cache = DownloadCache(self.config.cache_db)
        downloader = Downloader(self.http, cache)
        try:
            for label, plan, doc, path in downloads:
                try:
                    entry = downloader.download(doc.url, path, doc.source, organizer.root)
                except SourceError as exc:
                    log.error("download failed: %s", exc)
                    warnings.append(f"הורדה נכשלה: {label} – {doc.title}")
                    continue
                entry.origin_source = doc.origin_source
                entry.origin_url = doc.origin_url
                files.append(entry)
                if plan is not None:
                    done.setdefault(plan_id(plan), []).append((doc, path))
        finally:
            cache.close()

        self._add_to_library(plans, done, organizer, warnings)
        pending = [
            plan_id(p)
            for p in plans
            if not has_core_documents([doc for doc, _ in done.get(plan_id(p), [])])
        ]
        # The manifest lists, per plan, the documents chosen for it (not every candidate).
        manifest_plans = [
            p.model_copy(update={"documents": result.plan_documents.get(plan_id(p), [])})
            for p in plans
        ]
        organizer.write_manifest(
            project,
            result.gush,
            result.helka,
            result.parcels,
            manifest_plans,
            files,
            warnings,
            permits,
        )
        if result.polygon_wkt:
            organizer.write_polygon_dxf(result.polygon_wkt, result.parcels)
        log.info("downloaded %d files to %s", len(files), organizer.root)
        return DownloadResult(
            folder=organizer.root,
            files=files,
            warnings=warnings,
            plans=len(plans),
            permits=len(permits),
            mavat_pending=pending,
        )

    def _add_to_library(
        self,
        plans: list[Plan],
        done: dict[str, list[tuple[Document, Path]]],
        organizer: OutputOrganizer,
        warnings: list[str],
    ) -> None:
        """Files that came from a source (not from the library itself) go into the library."""
        if self.library is None:
            return
        for plan in plans:
            new = [
                (doc, path, path.relative_to(organizer.plan_dir(plan)).as_posix())
                for doc, path in done.get(plan_id(plan), [])
                if doc.source != LIBRARY_SOURCE
            ]
            if not new:
                continue
            try:
                self.library.add(plan, new)
            except (LibraryError, OSError) as exc:
                log.error("library: cannot add plan %s: %s", plan.number, exc)
                warnings.append(f"{LIBRARY_UNAVAILABLE} – תכנית {plan.number} לא נשמרה בה")
                return
