"""Office library commands (PRD §4.3): list plans and their plan_scope, correct a scope,
sync the local copy, rebuild this computer's index, and move a library of the old flat
layout (<library>/<plan>/) into the city layout.

    python -m app.library list [--city "בני ברק"] [--scope כלל-עירונית] [--basis default]
    python -m app.library scope "מק/מעליות" "105*" כלל-עירונית --city "בני ברק"
    python -m app.library sync
    python -m app.library rebuild
    python -m app.library migrate --source C:/ParcelDocs_Library [--delete-source]
    python -m app.library import-office --source "<folder>" [--source …] [--dry-run]
"""

import argparse
import hashlib
import logging
import shutil
import sys
from datetime import datetime
from pathlib import Path

from app.config import Config, load_config
from app.models import Plan
from app.office_import import import_office_files
from app.output.library import (
    PLAN_FILE,
    LibraryError,
    LibraryPlan,
    NewFile,
    national_number_of,
    plan_folder,
)
from app.output.library_index import LOCAL, SHARED
from app.output.office_library import OfficeLibrary
from app.output.plan_scope import ScopeBasis, city_key, classify_scope, parse_scope
from app.pipeline import make_library
from app.providers.bnei_brak import CITY_NAME
from app.utils.filenames import long_path

log = logging.getLogger("parceldocs")


def list_plans(
    library: OfficeLibrary,
    city: str | None = None,
    scope: str | None = None,
    basis: str | None = None,
) -> int:
    wanted_scope = parse_scope(scope) if scope else None
    rows = [
        item
        for item in library.index.plans()
        if (city is None or city_key(item.plan.city) == city_key(city))
        and (wanted_scope is None or item.plan.scope == wanted_scope)
        and (basis is None or item.plan.scope_basis.value == basis)
    ]
    for item in rows:
        plan = item.plan
        kinds = ",".join(sorted(k.value for k in plan.kinds())) or "-"
        checked = plan.last_checked.date().isoformat() if plan.last_checked else "-"
        pending = "  (עותק מקומי, ממתין לסנכרון)" if item.root == LOCAL else ""
        print(
            f"{item.folder:<32} {plan.national_number or '-':<12} {plan.scope.value:<12} "
            f"{plan.scope_basis.value:<9} {len(plan.files):>3} קבצים [{kinds}] "
            f"נבדקה {checked}  {plan.scope_note}{pending}"
        )
    print(f"\n{len(rows)} תכניות")
    return 0


def set_scope(library: OfficeLibrary, numbers: list[str], scope: str, city: str) -> int:
    """Plan numbers, or series rules ending in * ("105*", "מק/105*")."""
    covered = library.set_scopes(city, numbers, parse_scope(scope))
    for number, items in covered.items():
        if not items:
            print(f"{number}: נשמר ב-overrides.yaml; עדיין לא במאגר, ויחול כשתימצא.")
        for item in items:
            print(f"{number}: {item.plan.number} – {item.plan.scope.value} ({item.folder})")
    return 0


def migrate(library: OfficeLibrary, source: Path, city: str, delete_source: bool = False) -> int:
    """Copy each <source>/<plan>/ (plan.json of the old layout) into the shared library
    under <city>/ (or ארציות/), check every copied file's SHA256, and only then, with
    delete_source, remove the source folders."""
    if not library.available or library.shared is None:
        raise LibraryError("הספרייה ברשת לא זמינה")
    moved: list[Path] = []
    failed = 0
    for folder in sorted(p for p in source.iterdir() if p.is_dir()):
        plan_file = folder / PLAN_FILE
        if not plan_file.is_file():
            print(f"דילוג: {folder.name} (אין {PLAN_FILE})")
            continue
        old = LibraryPlan.model_validate_json(plan_file.read_text(encoding="utf-8"))
        plan = Plan(
            number=old.number, name=old.name, status="", source="library", aliases=old.aliases
        )
        decision = classify_scope(plan, None, None, 1.0)
        entry = old.model_copy(
            update={
                "city": old.city or city,
                "national_number": old.national_number or national_number_of(old.numbers()),
                "scope": decision.scope,
                "scope_basis": decision.basis,
                "scope_note": decision.note,
                "first_seen": min((f.added_at for f in old.files), default=None),
                "files": [],
            }
        )
        files = [
            NewFile(
                local_path=folder / f.path,
                path=f.path,
                kind=f.kind,
                title=f.title,
                source=f.source,
                url=f.url,
                added_at=f.added_at,
                added_by=f.added_by,
            )
            for f in old.files
        ]
        record = library.write(SHARED, plan_folder(entry), entry, files)
        copied = {f.sha256: f.path for f in record.plan.files}
        target = library.shared.path(record.folder)
        bad = [
            f.path
            for f in old.files
            if f.sha256 not in copied or _sha256(target / copied[f.sha256]) != f.sha256
        ]
        if bad:
            failed += 1
            print(f"שגיאה: {folder.name} → {record.folder}: לא אומתו {', '.join(bad)}")
            continue
        moved.append(folder)
        print(f"{folder.name} → {record.folder}: {len(old.files)} קבצים אומתו")
    print(f"\n{len(moved)} תכניות הועתקו ואומתו, {failed} נכשלו")
    if delete_source and not failed:
        for folder in moved:
            shutil.rmtree(long_path(folder))
        print(f"המקור נמחק: {len(moved)} תיקיות ב-{source}")
    elif delete_source:
        print("המקור לא נמחק, כי היו כשלים.")
    return 1 if failed else 0


def import_office(
    library: OfficeLibrary, sources: list[Path], city: str, dry_run: bool, report_path: Path | None
) -> int:
    if not dry_run and not library.available:
        raise LibraryError("הספרייה ברשת לא זמינה")
    report = import_office_files(library, sources, city, dry_run)
    text = report.text(sources, dry_run)
    print(text)
    if report_path is None and not dry_run and library.shared is not None:
        stamp = datetime.now().strftime("%Y-%m-%d_%H%M")
        report_path = library.shared.root / "_דוחות" / f"ייבוא_משרד_{stamp}.txt"
    if report_path is not None:
        long_path(report_path).parent.mkdir(parents=True, exist_ok=True)
        long_path(report_path).write_text(text, encoding="utf-8")
        print(f"הדוח נשמר: {report_path}")
    return 0


def _sha256(path: Path) -> str:
    return hashlib.sha256(long_path(path).read_bytes()).hexdigest()


def _library(config: Config) -> OfficeLibrary:
    library = make_library(config)
    if library is None:
        raise SystemExit("plans_library לא מוגדר ב-config.yaml")
    for warning in library.open():
        print(f"⚠ {warning}")
    return library


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(prog="parceldocs-library", description="הספרייה המשרדית")
    parser.add_argument("--config", type=Path, help="נתיב ל-config.yaml")
    commands = parser.add_subparsers(dest="command", required=True)
    listing = commands.add_parser("list", help="תכניות וסיווג plan_scope")
    listing.add_argument("--city")
    listing.add_argument("--scope", help="ספציפית / כלל-עירונית / ארצית")
    listing.add_argument("--basis", choices=[b.value for b in ScopeBasis])
    scope = commands.add_parser("scope", help="תיקון ידני של plan_scope (overrides.yaml)")
    scope.add_argument("numbers", nargs="+", help='מספרי תכניות, או כלל לסדרה כמו "105*"')
    scope.add_argument("scope", help="ספציפית / כלל-עירונית / ארצית")
    scope.add_argument("--city", default=CITY_NAME)
    commands.add_parser("sync", help="סנכרון העותק המקומי והאינדקס")
    commands.add_parser("rebuild", help="בניית האינדקס המקומי מחדש מקובצי plan.json")
    move = commands.add_parser("migrate", help="העברת ספרייה במבנה הישן למבנה לפי ערים")
    move.add_argument("--source", type=Path, required=True)
    move.add_argument("--city", default=CITY_NAME)
    move.add_argument("--delete-source", action="store_true")
    office = commands.add_parser("import-office", help='ייבוא תב"עות שהמשרד שמר (העתקה)')
    office.add_argument("--source", type=Path, action="append", required=True)
    office.add_argument("--city", default=CITY_NAME, help="העיר של קבצים בלי תיקיית עיר")
    office.add_argument("--dry-run", action="store_true", help="רק דוח, בלי להעתיק")
    office.add_argument("--report", type=Path, help="נתיב לדוח (ברירת מחדל: _דוחות בספרייה)")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    try:
        if args.command == "rebuild":
            library = make_library(config)
            if library is None:
                raise SystemExit("plans_library לא מוגדר ב-config.yaml")
            library.index.clear()
            for warning in library.open():
                print(f"⚠ {warning}")
            print(f"האינדקס נבנה מחדש: {library.status()}")
            return 0
        library = _library(config)
        if args.command == "list":
            return list_plans(library, args.city, args.scope, args.basis)
        if args.command == "scope":
            return set_scope(library, args.numbers, args.scope, args.city)
        if args.command == "sync":
            print(library.status())
            return 0
        if args.command == "import-office":
            return import_office(library, args.source, args.city, args.dry_run, args.report)
        return migrate(library, args.source, args.city, args.delete_source)
    except (LibraryError, ValueError) as exc:
        print(f"שגיאה: {exc}", file=sys.stderr)
        return 1


if __name__ == "__main__":
    sys.exit(main())
