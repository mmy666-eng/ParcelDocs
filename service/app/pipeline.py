"""The resolve → download flow shared by the CLI and the FastAPI service (PRD §3).

resolve finds parcels, approved plans and granted permits without downloading;
download fetches the documents of the items the user kept and builds the folder.
Only specific plans are offered for download; city-wide and national plans that apply
to the parcel are listed in the report ("חלות על החלקה – לא הורדו") and not downloaded.

Plan names come from the office library and from iplan, the Israel Land Authority (rmi)
and the city system (PRD §4.3): (א) the library's index is searched for the place, (ב) the
sources are asked only to discover new plans and changes, (ג) only documents the library
does not have are downloaded – the library first, then rmi, then the city system – and
(ד) the library is updated. Plans still without documents are left for the
semi-automatic mavat step (app.mavat_assist).
"""

import logging
import re
import threading
import uuid
from collections import Counter
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date
from pathlib import Path

from pydantic import BaseModel
from shapely import wkt
from shapely.geometry.base import BaseGeometry

from app.config import Config
from app.download.cache import DownloadCache
from app.download.downloader import Downloader, HttpClient, SourceError
from app.geo.cadastre import GovMapCadastre, check_itm, parcel_geometry
from app.models import (
    Address,
    Document,
    DocumentKind,
    ManifestEntry,
    Parcel,
    Permit,
    Plan,
    PlanScope,
)
from app.output.library import (
    LIBRARY_SOURCE,
    OFFICE_SOURCE,
    LibraryError,
    LibraryPlan,
    NewFile,
)
from app.output.library_index import IndexedPlan
from app.output.office_library import (
    MANUAL_NOTE,
    LibraryUpdate,
    OfficeLibrary,
    recently_checked,
)
from app.output.organizer import OutputOrganizer
from app.output.plan_scope import (
    NATIONAL_AUTHORITY,
    ScopeBasis,
    ScopeDecision,
    city_key,
    classify_scope,
)
from app.providers.bnei_brak import (
    CITY_NAME,
    IN_FORCE,
    BneiBrakPermitsProvider,
    BneiBrakPlansProvider,
    ComplotClient,
    GushFile,
    is_granted,
    select_documents,
)
from app.providers.iplan import SOURCE as IPLAN_SOURCE
from app.providers.iplan import IplanPlansProvider
from app.providers.rmi import SOURCE as RMI_SOURCE
from app.providers.rmi import RmiPlansProvider
from app.utils.plan_numbers import is_national_number, local_key, national_numbers_in

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
APPLICABLE_SECTION = "חלות על החלקה – לא הורדו"

_LOCAL_NUMBER = re.compile(r"(?:בב/)?((?:מק/)?\d+(?:/[֐-׿\d]+)+)")


class NotFoundError(ValueError):
    pass


class ResolveResult(BaseModel):
    resolve_id: str
    gush: int
    helka: int
    parcels: list[Parcel]
    # Specific plans: offered for download.
    plans: list[Plan]
    plan_documents: dict[str, list[Document]]
    permits: list[Permit]
    permit_documents: dict[str, list[Document]]
    warnings: list[str]
    polygon_wkt: str | None = None
    # Log lines of the resolve step; the folder (and run.log) exists only at download.
    log: list[str] = []
    # Municipality of the parcels (cadastre), e.g. "בני ברק".
    city: str | None = None
    # Per plan id: what to write to the office library after the download (step ד).
    library_updates: dict[str, LibraryUpdate] = {}
    # City-wide and national plans that apply to the parcel: listed, never downloaded.
    applicable: list[Plan] = []


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
    national = national_numbers_in(f"{plan.number} {plan.name}")
    local = set(_LOCAL_NUMBER.findall(plan.number.replace(" ", "")))
    return national, local


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
        city.source_ids = {**iplan_plan.source_ids, **city.source_ids}
        city.city = city.city or iplan_plan.city
        city.authority_level = iplan_plan.authority_level
        city.boundary_wkt = iplan_plan.boundary_wkt
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
        plan.source_ids = {**rmi_plan.source_ids, **plan.source_ids}
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

    Office copies in the library (kept by hand, named by the office) are added only when
    the plan would otherwise lack instructions or a map.
    """
    office = [d for d in library_documents if d.origin_source == OFFICE_SOURCE]
    documents, source = _choose_documents(
        plan, [d for d in library_documents if d.origin_source != OFFICE_SOURCE]
    )
    present = {d.kind for d in documents}
    missing = [kind for kind in CORE_KINDS if kind not in present]
    extra = [d for d in office if missing and (d.kind in missing or d.kind is None)]
    if extra:
        return [*documents, *extra], f"{source}+{OFFICE_SOURCE}" if source else OFFICE_SOURCE
    return documents, source


def _choose_documents(plan: Plan, library_documents: list[Document]) -> tuple[list[Document], str]:
    """Without office copies.

    A complete library entry (instructions and a map) is used as it is. Otherwise every
    kind the library has comes from it, and only the missing kinds from the first source
    with instructions or a map: rmi, then the city system (approved versions). Then a
    partial library entry, then a source with appendices only.
    """
    if has_core_documents(library_documents):
        return library_documents, LIBRARY_SOURCE
    in_library = {d.kind for d in library_documents}
    candidates = [
        ([d for d in plan.documents if d.source == RMI_SOURCE], RMI_SOURCE),
        (select_documents([d for d in plan.documents if d.source == CITY_SOURCE]), CITY_SOURCE),
    ]
    for documents, source in candidates:
        if any(d.kind in CORE_KINDS for d in documents):
            if not library_documents:
                return documents, source
            missing = [d for d in documents if d.kind not in in_library]
            if missing:
                return [*library_documents, *missing], f"{LIBRARY_SOURCE}+{source}"
            return library_documents, LIBRARY_SOURCE
    if library_documents:
        return library_documents, LIBRARY_SOURCE
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


def make_library(config: Config) -> OfficeLibrary | None:
    if config.plans_library is None:
        return None
    cache = config.cache_db.parent
    return OfficeLibrary(
        config.plans_library,
        config.plans_library_local or cache / "library_local",
        config.library_index or cache / "library_index.sqlite",
    )


def plan_from_library(entry: LibraryPlan) -> Plan:
    """A plan that only the library knows for this place."""
    return Plan(
        number=entry.number,
        name=entry.name,
        status=entry.status or "",
        source=LIBRARY_SOURCE,
        aliases=[a for a in entry.aliases if a != entry.number],
        plan_type=entry.plan_type,
        approval_date=entry.approval_date,
        mavat_url=entry.mavat_url,
        city=entry.city,
        scope=entry.scope,
        source_ids={ref.source: ref.id for ref in entry.sources},
        boundary_wkt=entry.boundary_wkt,
    )


def apply_library_record(plan: Plan, entry: LibraryPlan) -> None:
    """What the library knows and this run's sources did not say, and changes since."""
    for number in entry.numbers():
        if number != plan.number and number not in plan.aliases:
            plan.aliases.append(number)
    for ref in entry.sources:
        plan.source_ids.setdefault(ref.source, ref.id)
    plan.name = plan.name or entry.name
    plan.city = plan.city or entry.city
    plan.plan_type = plan.plan_type or entry.plan_type
    plan.approval_date = entry.approval_date or plan.approval_date
    plan.mavat_url = plan.mavat_url or entry.mavat_url
    plan.boundary_wkt = plan.boundary_wkt or entry.boundary_wkt
    if entry.status and plan.status and entry.status != plan.status:
        log.info(
            "plan %s: status in the library %r, now %r", plan.number, entry.status, plan.status
        )


def known_decision(
    library: OfficeLibrary | None, plan: Plan, entry: LibraryPlan | None
) -> ScopeDecision | None:
    """plan_scope decided before: a manual correction, or a final automatic decision."""
    override = library.override_for(plan) if library else None
    if override is not None:
        return ScopeDecision(scope=override, basis=ScopeBasis.MANUAL, note=MANUAL_NOTE)
    if entry is not None and entry.scope_basis != ScopeBasis.DEFAULT:
        return ScopeDecision(scope=entry.scope, basis=entry.scope_basis, note=entry.scope_note)
    return None


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
        # Cities whose boundary GovMap was asked for in this run.
        self._municipalities_tried: set[str] = set()

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
        library = self.library
        self._municipalities_tried = set()
        if library is not None:
            warnings.extend(library.open())
        city_available = CITY_SOURCE not in self.config.disabled_sources
        status = {(p.gush, p.helka): p.status for p in cadastre_parcels}
        geometries = {(p.gush, p.helka): p.geometry_wkt for p in cadastre_parcels}
        run_city = _most_common_city(cadastre_parcels)
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
                city=run_city,
                geometry_wkt=geometries.get(key),
            )
            for key, g in unique.items()
        ]
        all_parcels = list(dict.fromkeys([*requested, *unique]))

        # (א) The library's index, for this place.
        in_library: list[IndexedPlan] = []
        if library is not None:
            if run_city and iplan_query is not None:
                self._city_boundary(run_city, iplan_query)
            in_library = library.search(iplan_query, run_city, all_parcels)
            log.info("library: %d plans for this place in the index", len(in_library))

        # (ב) The sources, to discover new plans and changes. hits: the parcels each
        # source reported a plan for ("source:id" → parcels).
        reported_by: set[str] = set()
        hits: dict[str, set[tuple[int, int]]] = {}
        iplan_plans: list[Plan] = []
        if iplan_query is not None:
            try:
                iplan_plans = self.iplan.find_by_polygon(iplan_query)
                reported_by.add(IPLAN_SOURCE)
            except SourceError as exc:
                log.error("iplan unavailable: %s", exc)
                warnings.append(f"{IPLAN_UNAVAILABLE}: {exc}")

        city_plans: list[Plan] = []
        skip_details: set[int] = set()
        if city_available:
            for request, resolved in resolved_by_request.items():
                for gush_file in resolved:
                    for ref in gush_file.plans:
                        if ref.status == IN_FORCE:
                            hits.setdefault(f"{CITY_SOURCE}:{ref.complot_id}", set()).update(
                                {(gush_file.gush, gush_file.helka), request}
                            )
            skip_details = self._documents_in_library(list(unique.values()))
            try:
                city_plans = self.city_plans.find_by_parcels(list(unique.values()), skip_details)
                reported_by.add(CITY_SOURCE)
            except SourceError as exc:
                log.error("Complot unavailable: %s", exc)
                warnings.append(f"{CITY_UNAVAILABLE}: {exc}")
                city_available = False
        plans = merge_plans(iplan_plans, city_plans)

        if RMI_SOURCE not in self.config.disabled_sources:
            try:
                plans = merge_rmi(plans, self._rmi_plans(all_parcels, plans, hits))
                reported_by.add(RMI_SOURCE)
            except SourceError as exc:
                log.error("rmi unavailable: %s", exc)
                warnings.append(f"{RMI_UNAVAILABLE}: {exc}")

        for plan in plans:
            if plan.authority_level != NATIONAL_AUTHORITY:
                plan.city = plan.city or run_city
        records: dict[str, IndexedPlan] = {}
        if library is not None:
            plans, records = self._with_library(plans, in_library, reported_by)

        updates: dict[str, LibraryUpdate] = {}
        skipped = {str(i) for i in skip_details}
        for plan in plans:
            record = records.get(plan_id(plan))
            entry = record.plan if record else None
            decision = known_decision(library, plan, entry) or self._classify(plan)
            plan.scope, plan.scope_note = decision.scope, decision.note
            found_for: set[tuple[int, int]] = set()
            for source, source_id in plan.source_ids.items():
                found_for |= hits.get(f"{source}:{source_id}", set())
            sources = plan.source.split("+")
            checked = [RMI_SOURCE] if RMI_SOURCE in sources else []
            if CITY_SOURCE in sources and plan.source_ids.get(CITY_SOURCE) not in skipped:
                checked.append(CITY_SOURCE)
            updates[plan_id(plan)] = LibraryUpdate(
                decision=decision,
                parcels=sorted(found_for),
                checked_sources=checked,
                reported=plan.source != LIBRARY_SOURCE,
            )

        applicable = [p for p in plans if p.scope != PlanScope.SPECIFIC]
        plans = [p for p in plans if p.scope == PlanScope.SPECIFIC]
        if applicable:
            log.info(
                "%s: %s",
                APPLICABLE_SECTION,
                ", ".join(f"{p.number} ({p.scope.value})" for p in applicable if p.scope),
            )
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
            city=run_city,
            library_updates=updates,
            applicable=applicable,
        )

    def _documents_in_library(self, gush_files: list[GushFile]) -> set[int]:
        """Complot ids whose plan file (GetTabaFile) is not needed: city-wide and national
        plans (never downloaded), and plans whose instructions and map are in the library
        or that were checked in the city system recently."""
        library = self.library
        if library is None:
            return set()
        refs = {
            ref.complot_id: ref for g in gush_files for ref in g.plans if ref.status == IN_FORCE
        }
        skip = set()
        for complot_id in {*refs, *self.config.citywide_plans}:
            item = library.find_source(CITY_SOURCE, str(complot_id))
            ref = refs.get(complot_id)
            listed = Plan(
                number=ref.number if ref else "",
                name="",
                status="",
                source=CITY_SOURCE,
                city=CITY_NAME,
                source_ids={CITY_SOURCE: str(complot_id)},
            )
            decision = known_decision(library, listed, item.plan if item else None)
            not_downloaded = decision is not None and decision.scope != PlanScope.SPECIFIC
            in_library = (
                item is not None
                and library.available
                and (
                    item.plan.complete()
                    or recently_checked(item.plan, CITY_SOURCE, self.config.library_recheck_days)
                )
            )
            if not_downloaded or in_library:
                skip.add(complot_id)
        return skip

    def _with_library(
        self, plans: list[Plan], in_library: list[IndexedPlan], reported_by: set[str]
    ) -> tuple[list[Plan], dict[str, IndexedPlan]]:
        """Join this run's plans with their library records, and add the plans that only
        the library knows for this place – unless a source that reported such a plan
        before was asked now and did not report it."""
        assert self.library is not None
        records: dict[str, IndexedPlan] = {}
        matched: set[str] = set()
        for plan in plans:
            record = self.library.find(plan)
            if record is None:
                log.info("library: new plan %s", plan.number)
                continue
            apply_library_record(plan, record.plan)
            records[plan_id(plan)] = record
            matched |= record.plan.match_keys()
        for record in in_library:
            entry = record.plan
            if entry.match_keys() & matched:
                continue
            asked = {ref.source for ref in entry.sources} & reported_by
            kept_citywide = entry.scope == PlanScope.CITYWIDE and entry.scope_basis in (
                ScopeBasis.MANUAL,
                ScopeBasis.CONFIG,
            )
            if asked and not kept_citywide:
                log.info(
                    "library: plan %s not reported by %s this time – left out",
                    entry.number,
                    ", ".join(sorted(asked)),
                )
                continue
            plan = plan_from_library(entry)
            log.info("library: plan %s found only in the library (%s)", plan.number, entry.scope)
            plans.append(plan)
            records[plan_id(plan)] = record
            matched |= entry.match_keys()
        return plans, records

    def _classify(self, plan: Plan) -> ScopeDecision:
        boundary = wkt.loads(plan.boundary_wkt) if plan.boundary_wkt else None
        city_boundary = self._city_boundary(plan.city, boundary) if boundary is not None else None
        return classify_scope(
            plan,
            boundary,
            city_boundary,
            self.config.citywide_min_share,
            {str(i) for i in self.config.citywide_plans},
        )

    def _city_boundary(self, city: str | None, near: BaseGeometry) -> BaseGeometry | None:
        """The municipal boundary of a city, from the library's index or GovMap (once)."""
        library = self.library
        key = city_key(city)
        if library is None or not key:
            return None
        cached = library.city_boundary(city)
        if cached is not None or key in self._municipalities_tried:
            return cached
        self._municipalities_tried.add(key)
        point = near.representative_point()
        try:
            municipality = self.cadastre.municipality_at(point.x, point.y)
        except SourceError as exc:
            log.warning("GovMap municipalities unavailable: %s", exc)
            return None
        if municipality is None:
            return None
        library.put_city(municipality.name, municipality.boundary_wkt)
        if city_key(municipality.name) != key:
            log.info("GovMap: %s is not the municipality of %s", municipality.name, city)
            return None
        return wkt.loads(municipality.boundary_wkt)

    def _rmi_plans(
        self,
        parcels: list[tuple[int, int]],
        known: list[Plan],
        hits: dict[str, set[tuple[int, int]]],
    ) -> list[Plan]:
        """rmi plans by parcel, plus a lookup by number for national-numbered plans that
        other sources found but rmi did not link to these parcels (unless the library
        already has their instructions and map)."""
        found: dict[str, Plan] = {}
        for gush, helka in parcels:
            for plan in self.rmi.find_by_parcel(gush, helka):
                found.setdefault(plan.source_id or plan.number, plan)
                if plan.source_id:
                    hits.setdefault(f"{RMI_SOURCE}:{plan.source_id}", set()).add((gush, helka))
        numbers = {p.number for p in found.values()}
        library = self.library if self.library is not None and self.library.available else None
        for plan in known:
            if not is_national_number(plan.number) or plan.number in numbers:
                continue
            record = library.find(plan) if library else None
            if record is not None and (
                record.plan.complete() or record.plan.scope != PlanScope.SPECIFIC
            ):
                continue
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
                except (LibraryError, OSError) as exc:
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

        self._update_library(result, done, organizer, warnings)
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
            result.applicable,
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

    def _update_library(
        self,
        result: ResolveResult,
        done: dict[str, list[tuple[Document, Path]]],
        organizer: OutputOrganizer,
        warnings: list[str],
    ) -> None:
        """(ד) Every plan found goes into the library, with the files that came from a
        source (not from the library itself) for the plans that were downloaded."""
        if self.library is None:
            return
        for plan in [*result.plans, *result.applicable]:
            update = result.library_updates.get(plan_id(plan))
            if update is None:
                continue
            files = [
                NewFile(
                    local_path=path,
                    path=path.relative_to(organizer.plan_dir(plan)).as_posix(),
                    kind=doc.kind,
                    title=doc.title,
                    source=doc.origin_source or doc.source,
                    url=doc.origin_url or doc.url,
                )
                for doc, path in done.get(plan_id(plan), [])
                if doc.source != LIBRARY_SOURCE
            ]
            try:
                self.library.record(plan, update, files, warnings)
            except (LibraryError, OSError) as exc:
                log.error("library: cannot add plan %s: %s", plan.number, exc)
                warnings.append(f"{LIBRARY_UNAVAILABLE} – תכנית {plan.number} לא נשמרה בה")
                return


def _most_common_city(parcels: list[Parcel]) -> str | None:
    cities = Counter(p.city for p in parcels if p.city)
    return cities.most_common(1)[0][0] if cities else None
