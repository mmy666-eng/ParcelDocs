"""Command line run without CAD (PRD §3): approved plans and building permits for a gush/helka."""

import argparse
import logging
import sys
from datetime import date
from pathlib import Path

from app.config import Config, load_config
from app.download.downloader import HttpClient, SourceError
from app.models import Address
from app.pipeline import NotFoundError, Pipeline, ResolveResult, permit_id, plan_id

log = logging.getLogger("parceldocs")


def run(
    config: Config,
    gush: int,
    helka: int,
    project: str,
    dry_run: bool = False,
    http: HttpClient | None = None,
    today: date | None = None,
) -> int:
    pipeline = Pipeline(config, http)
    log.info("run: project=%s gush=%d helka=%d dry_run=%s", project, gush, helka, dry_run)
    try:
        result = pipeline.resolve_parcel(gush, helka)
    except NotFoundError as exc:
        print(f"{exc}.", file=sys.stderr)
        return 2
    folder = None
    files = cached = 0
    pending = 0
    warnings = result.warnings
    if not dry_run:
        # The pipeline writes 99_לוג/run.log, covering both resolve and download.
        downloaded = pipeline.download(result, project, today=today)
        folder, warnings = downloaded.folder, downloaded.warnings
        files = len(downloaded.files)
        cached = sum(1 for f in downloaded.files if f.from_cache)
        pending = len(downloaded.mavat_pending)
    _print_summary(result, warnings, folder, files, cached)
    if pending:
        print(f'\n{pending} תכניות בלי הוראות או תשריט. להשלמה ממבא"ת (הורדה ידנית בדפדפן):')
        print(f'  .venv\\Scripts\\python -m app.mavat_assist --folder "{folder}"')
    return 0


SOURCE_LABELS = {"library": "ספרייה", "rmi": 'רמ"י', "bnei_brak": "עירייה", "mavat": 'מבא"ת'}


def _print_summary(
    result: ResolveResult, warnings: list[str], folder: Path | None, files: int, cached: int
) -> None:
    print("חלקות:")
    for p in result.parcels:
        print(f"  {p.gush}/{p.helka}  {_addresses(p.addresses)}")
    print(f'\nתב"עות מאושרות ({len(result.plans)}):')
    for plan in result.plans:
        when = plan.approval_date.isoformat() if plan.approval_date else "-"
        docs = len(result.plan_documents.get(plan_id(plan), []))
        source = SOURCE_LABELS.get(plan.documents_source or "", plan.documents_source)
        note = f"  ⚠ {plan.manual_action}" if plan.manual_action else f"  {docs} מסמכים ({source})"
        print(f"  {plan.number:<16} {when}  {plan.name[:50]}{note}")
    print(f"\nהיתרי בנייה ({len(result.permits)}):")
    for permit in result.permits:
        when = str(permit.date or permit.request_date or "-")
        docs = len(result.permit_documents.get(permit_id(permit), []))
        note = f"  ⚠ {permit.manual_action}" if permit.manual_action else f"  {docs} מסמכים"
        description = (permit.description or "")[:40]
        print(f"  היתר {permit.permit_number or '-':<8} {when}  {description}{note}")
    for warning in warnings:
        print(f"⚠ {warning}")
    if folder is None:
        print("\n(dry run – לא הורדו קבצים)")
    else:
        print(f"\nהורדו {files - cached} קבצים, {cached} מה-cache. תיקייה: {folder}")


def _addresses(addresses: list[Address]) -> str:
    return ", ".join(f"{a.street} {a.number}" for a in addresses) or "-"


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="parceldocs", description='ParcelDocs – תב"עות והיתרים לחלקה'
    )
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
