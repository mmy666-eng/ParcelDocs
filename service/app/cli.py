"""Command line run without CAD (PRD §3): approved plans for a gush/helka (phase 1)."""

import argparse
import logging
import re
import sys
from datetime import date
from pathlib import Path

from app.config import Config, load_config
from app.download.cache import DownloadCache
from app.download.downloader import Downloader, HttpClient, SourceError
from app.geo.cadastre import GovMapCadastre, parcel_geometry
from app.models import Address, ManifestEntry, Parcel, Plan
from app.output.organizer import OutputOrganizer
from app.providers.bnei_brak import BneiBrakPlansProvider, ComplotClient, GushFile
from app.providers.iplan import IplanPlansProvider

log = logging.getLogger("parceldocs")

MANUAL_MAVAT = 'אין עותק במערכת העירונית – להוריד ידנית ממבא"ת'
MANUAL_NO_DOCUMENTS = "לא נמצאו הוראות או תשריט סרוקים – לפנות לוועדה"
MANUAL_CITY_UNAVAILABLE = 'המערכת העירונית לא זמינה – להוריד ידנית ממבא"ת או מאתר העירייה'

CITY_SOURCE = "bnei_brak"
CITY_DISABLED = 'המערכת העירונית (Complot) מושבתת ב-config – תב"עות עירוניות ומסמכים לא נבדקו'
CITY_UNAVAILABLE = 'המערכת העירונית (Complot) לא זמינה – תב"עות עירוניות ומסמכים לא נבדקו'

_NATIONAL_NUMBER = re.compile(r"\d{3}-\d{7}")
_LOCAL_NUMBER = re.compile(r"(?:בב/)?((?:מק/)?\d+(?:/[֐-׿\d]+)+)")


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


def run(
    config: Config,
    gush: int,
    helka: int,
    project: str,
    dry_run: bool = False,
    http: HttpClient | None = None,
    today: date | None = None,
) -> int:
    http = http or HttpClient(
        config.user_agent,
        config.requests_per_second,
        config.timeout_seconds,
        config.retries,
        legacy_tls_hosts=config.legacy_tls_hosts,
        host_min_interval_seconds=config.host_min_interval_seconds,
        rate_limited_wait_seconds=config.rate_limited_wait_seconds,
    )
    organizer = OutputOrganizer(config.output_dir, project, gush, helka, today or date.today())
    handler = None
    if not dry_run:
        organizer.create()
        handler = _log_to_file(organizer.log_file)
    try:
        return _run(config, http, organizer, gush, helka, project, dry_run)
    finally:
        if handler is not None:
            logging.getLogger().removeHandler(handler)
            handler.close()


def _run(
    config: Config,
    http: HttpClient,
    organizer: OutputOrganizer,
    gush: int,
    helka: int,
    project: str,
    dry_run: bool,
) -> int:
    log.info("run: project=%s gush=%d helka=%d dry_run=%s", project, gush, helka, dry_run)
    warnings: list[str] = []
    cadastre = GovMapCadastre(http, config.sources.govmap_wfs)
    complot = ComplotClient(http, config.sources.complot_base, config.sources.complot_site_id)
    city = BneiBrakPlansProvider(complot, cadastre, config.citywide_plans)
    iplan = IplanPlansProvider(http, config.sources.iplan_plans_layer)

    parcel = cadastre.get_parcel(gush, helka)
    city_available = CITY_SOURCE not in config.disabled_sources
    gush_files: list[GushFile] = []
    if not city_available:
        warnings.append(CITY_DISABLED)
    else:
        try:
            gush_files = city.resolve_parcels(gush, helka)
        except SourceError as exc:
            log.error("Complot unavailable: %s", exc)
            warnings.append(f"{CITY_UNAVAILABLE}: {exc}")
            city_available = False
    if not gush_files and parcel is None:
        print(f'גוש {gush} חלקה {helka} לא נמצאו לא במפ"י ולא במערכת העירונית.', file=sys.stderr)
        return 2
    if not gush_files and parcel is not None:
        gush_files = [GushFile(gush=parcel.gush, helka=parcel.helka)]

    parcels = [
        Parcel(
            gush=gf.gush,
            helka=gf.helka,
            addresses=gf.addresses,
            status=parcel.status
            if parcel and (parcel.gush, parcel.helka) == (gf.gush, gf.helka)
            else None,
        )
        for gf in gush_files
    ]
    if [(gf.gush, gf.helka) for gf in gush_files] != [(gush, helka)]:
        children = ", ".join(f"{p.gush}/{p.helka}" for p in parcels)
        warnings.append(f"החלקה {gush}/{helka} עברה פרצלציה במערכת העירונית: {children}")

    iplan_plans: list[Plan] = []
    geometry = parcel_geometry(parcel) if parcel else None
    if geometry is None:
        warnings.append(f'החלקה {gush}/{helka} לא קיימת בקדסטר של מפ"י – חיפוש מרחבי ב-iplan דולג')
    else:
        iplan_plans = iplan.find_by_polygon(geometry.buffer(config.parcel_buffer_m))

    city_plans: list[Plan] = []
    if city_available:
        try:
            city_plans = city.find_by_parcels(gush_files)
        except SourceError as exc:
            log.error("Complot unavailable: %s", exc)
            warnings.append(f"{CITY_UNAVAILABLE}: {exc}")
            city_available = False
    plans = merge_plans(
        iplan_plans, city_plans, MANUAL_MAVAT if city_available else MANUAL_CITY_UNAVAILABLE
    )
    selections = {}
    for plan in plans:
        if plan.manual_action:
            continue
        selected = city.get_documents(plan)
        if not selected:
            plan.manual_action = MANUAL_NO_DOCUMENTS
        selections[id(plan)] = selected

    files: list[ManifestEntry] = []
    if not dry_run:
        cache = DownloadCache(config.cache_db)
        downloader = Downloader(http, cache)
        try:
            for plan in plans:
                documents = selections.get(id(plan), [])
                for doc, path in zip(
                    documents, organizer.document_paths(plan, documents), strict=True
                ):
                    try:
                        files.append(downloader.download(doc.url, path, doc.source, organizer.root))
                    except SourceError as exc:
                        log.error("download failed: %s", exc)
                        warnings.append(f"הורדה נכשלה: {plan.number} – {doc.title}")
        finally:
            cache.close()
        organizer.write_manifest(project, gush, helka, parcels, plans, files, warnings)

    _print_summary(parcels, plans, selections, files, warnings, organizer, dry_run)
    return 0


def _print_summary(
    parcels: list[Parcel],
    plans: list[Plan],
    selections: dict[int, list],
    files: list[ManifestEntry],
    warnings: list[str],
    organizer: OutputOrganizer,
    dry_run: bool,
) -> None:
    print("חלקות:")
    for p in parcels:
        print(f"  {p.gush}/{p.helka}  {_addresses(p.addresses)}")
    print(f'\nתב"עות מאושרות ({len(plans)}):')
    for plan in plans:
        when = plan.approval_date.isoformat() if plan.approval_date else "-"
        docs = len(selections.get(id(plan), []))
        note = f"  ⚠ {plan.manual_action}" if plan.manual_action else f"  {docs} מסמכים"
        print(f"  {plan.number:<16} {when}  {plan.name[:50]}{note}")
    for warning in warnings:
        print(f"⚠ {warning}")
    if dry_run:
        print("\n(dry run – לא הורדו קבצים)")
    else:
        cached = sum(1 for f in files if f.from_cache)
        print(f"\nהורדו {len(files) - cached} קבצים, {cached} מה-cache. תיקייה: {organizer.root}")


def _addresses(addresses: list[Address]) -> str:
    return ", ".join(f"{a.street} {a.number}" for a in addresses) or "-"


def _log_to_file(path: Path) -> logging.Handler:
    handler = logging.FileHandler(path, encoding="utf-8")
    handler.setFormatter(logging.Formatter("%(asctime)s %(levelname)s %(name)s: %(message)s"))
    handler.setLevel(logging.DEBUG)
    root = logging.getLogger()
    root.addHandler(handler)
    if root.level > logging.INFO:
        root.setLevel(logging.INFO)
    return handler


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="parceldocs", description='ParcelDocs – תב"עות לחלקה')
    parser.add_argument("--gush", type=int, required=True)
    parser.add_argument("--helka", type=int, required=True)
    parser.add_argument("--project", default="פרויקט", help="שם הפרויקט לתיקיית הפלט")
    parser.add_argument("--config", type=Path, help="נתיב ל-config.yaml")
    parser.add_argument("--dry-run", action="store_true", help="רק להציג, בלי להוריד")
    parser.add_argument("-v", "--verbose", action="store_true")
    args = parser.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s %(name)s: %(message)s",
    )
    logging.getLogger("httpx").setLevel(logging.WARNING)
    try:
        return run(load_config(args.config), args.gush, args.helka, args.project, args.dry_run)
    except SourceError as exc:
        print(f"שגיאה במקור נתונים: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
