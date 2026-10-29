"""Import of plan files the office kept by hand into the office library (copies only).

The same file often sits in two folders under two names, so PDFs are grouped by content
(SHA256). A group is imported when its plan number is clear:
  1. a national number (501-0282152) in a file name, a folder name, or the PDF text;
  2. otherwise one local number in the file names (105, 107א, בב 185, בב.מק.3054.א,
     534בב, מק 3010), and every copy of the file must agree on it.
Anything else – no number, names that disagree, more than one number, not a PDF – goes
to the report and is not imported. Imported files keep their name under
<plan folder>/משרד/, with the source "office": they fill gaps and never replace a
plan's documents from a source (pipeline.choose_documents). The source folders are only
read.
"""

import hashlib
import logging
import re
from collections import Counter
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.models import DocumentKind, Plan
from app.output.library import OFFICE_SOURCE, LibraryError, LibraryPlan, NewFile, plan_folder
from app.output.office_library import LibraryUpdate, OfficeLibrary
from app.output.plan_scope import classify_scope
from app.pipeline import known_decision
from app.utils.filenames import long_path, safe_name
from app.utils.pdf import LARGE_PAGE_PT, PdfInfo, contains, read_pdf
from app.utils.plan_numbers import local_key, national_numbers_in

log = logging.getLogger("parceldocs")

OFFICE_DIR = "משרד"
PLAN_EXTENSIONS = {".pdf"}
_COPY_MARK = re.compile(r"\(\d+\)")
_DOCUMENT_WORDS = re.compile(r"הוראות|תשריט|תקנון|נספח|תב['\"״׳]*ע|תבע")
_CITY_MARK = re.compile(r"בב")
_SEPARATORS = re.compile(r"[\s._\-]+")
_PARTS = re.compile(r"\d+|[א-ת]+|[A-Za-z]+")


class OfficeFile(BaseModel):
    path: Path
    # Relative to the folder it was found in, with that folder's name first.
    shown: str
    city: str
    national: list[str] = []
    local: str | None = None


class ImportedPlan(BaseModel):
    number: str
    folder: str
    files: list[str]
    kind: DocumentKind | None
    # Set when the same content was already in the plan's folder, so nothing was copied.
    note: str = ""


class SkippedFile(BaseModel):
    shown: str
    reason: str


class ImportReport(BaseModel):
    imported: list[ImportedPlan] = []
    skipped: list[SkippedFile] = []
    warnings: list[str] = []

    def text(self, sources: list[Path], dry_run: bool) -> str:
        files = sum(len(item.files) for item in self.imported)
        plans = len({item.folder for item in self.imported})
        lines = [
            f'ייבוא תב"עות שהמשרד שמר ידנית – {datetime.now():%Y-%m-%d %H:%M}'
            + (" (בדיקה בלבד, לא הועתק דבר)" if dry_run else ""),
            "מקורות (לא שונו): " + ", ".join(str(s) for s in sources),
            f"יובאו: {len(self.imported)} קבצים שונים ({files} עותקים) ל-{plans} תכניות.",
            f"לא יובאו: {len(self.skipped)} קבצים.",
            *(f"⚠ {w}" for w in dict.fromkeys(self.warnings)),
            "",
            "== יובאו ==",
        ]
        for item in sorted(self.imported, key=lambda i: i.folder):
            kind = item.kind.value if item.kind else "סוג לא ידוע"
            note = f" – {item.note}" if item.note else ""
            lines.append(f"{item.folder} ({kind}) ← {' | '.join(item.files)}{note}")
        lines += ["", "== לא יובאו =="]
        for item in sorted(self.skipped, key=lambda i: i.shown):
            lines.append(f"{item.shown} – {item.reason}")
        return "\n".join(lines) + "\n"


def local_number_from_name(stem: str) -> str | None:
    """105 → 105, 107א → 107/א, בב.מק.3054.א → מק/3054/א, 534בב → 534; None when the
    name holds no number, or more than one (1 3, 3.36.18, 158 2)."""
    text = _CITY_MARK.sub(" ", _DOCUMENT_WORDS.sub(" ", _COPY_MARK.sub(" ", stem)))
    parts = [p for token in _SEPARATORS.split(text) if token for p in _PARTS.findall(token)]
    approved_locally = bool(parts) and parts[0] == "מק"
    if approved_locally:
        parts = parts[1:]
    if not parts or not parts[0].isdigit() or len(parts[0]) > 4 or len(parts) > 2:
        return None
    if len(parts) == 2 and not re.fullmatch(r"[א-ת]", parts[1]):
        return None
    number = "/".join(parts)
    return f"מק/{number}" if approved_locally else number


def office_kind(names: list[str], info: PdfInfo | None) -> DocumentKind | None:
    """Only what is clear: a word in a name, a page larger than A3 (a map sheet, even when
    its legend quotes "הוראות התכנית"), or the text."""
    for name in names:
        if "נספח" in name:
            return DocumentKind.APPENDIX
        if "הוראות" in name or "תקנון" in name:
            return DocumentKind.INSTRUCTIONS
        if "תשריט" in name:
            return DocumentKind.MAP
    if info is not None:
        if info.max_side_pt >= LARGE_PAGE_PT:
            return DocumentKind.MAP
        if contains(info.text, "הוראות התכנית"):
            return DocumentKind.INSTRUCTIONS
        if contains(info.text, "תשריט"):
            return DocumentKind.MAP
    return None


def collect(
    sources: list[Path], default_city: str
) -> tuple[dict[str, list[OfficeFile]], list[SkippedFile]]:
    """PDFs by SHA256; other files are reported. A first-level folder without a national
    number in its name is taken as a city (ראש העין, ערד); otherwise default_city."""
    groups: dict[str, list[OfficeFile]] = {}
    skipped: list[SkippedFile] = []
    for source in sources:
        for path in sorted(p for p in long_path(source).rglob("*") if p.is_file()):
            relative = path.relative_to(long_path(source))
            shown = f"{source.name}/{relative.as_posix()}"
            if path.suffix.lower() not in PLAN_EXTENSIONS:
                skipped.append(
                    SkippedFile(shown=shown, reason=f"לא PDF ({path.suffix or 'בלי סיומת'})")
                )
                continue
            folders = list(relative.parts[:-1])
            national = sorted(
                national_numbers_in(path.stem)
                | {n for f in folders for n in national_numbers_in(f)}
            )
            city = default_city
            if folders and not national_numbers_in(folders[0]):
                city = folders[0]
            sha256 = hashlib.sha256(path.read_bytes()).hexdigest()
            groups.setdefault(sha256, []).append(
                OfficeFile(
                    path=path,
                    shown=shown,
                    city=city,
                    national=national,
                    local=local_number_from_name(path.stem),
                )
            )
    return groups, skipped


def identify(files: list[OfficeFile], info: PdfInfo | None) -> tuple[str | None, list[str], str]:
    """(plan number, aliases, "") or (None, [], reason). With a national number, a single
    local number in the names becomes an alias (819.pdf of 501-0246629, בב/819)."""
    numbers = {f.local for f in files if f.local}
    keys = {local_key(n) for n in numbers}
    national = {n for f in files for n in f.national}
    if not national and info is not None:
        counts = Counter(national_numbers_in(info.text)).most_common(2)
        if counts and (len(counts) == 1 or counts[0][1] > counts[1][1]):
            national = {counts[0][0]}
    if len(national) == 1:
        return national.pop(), sorted(numbers, key=len)[-1:] if len(keys) == 1 else [], ""
    if len(national) > 1:
        return None, [], f"כמה מספרים ארציים: {', '.join(sorted(national))}"
    if len(keys) == 1:
        return sorted(numbers, key=len)[-1], [], ""
    if numbers:
        names = ", ".join(f"{f.shown} → {f.local or '?'}" for f in files)
        return None, [], f"השמות של אותו קובץ סותרים: {names}"
    return None, [], "לא זוהה מספר תכנית"


def import_office_files(
    library: OfficeLibrary, sources: list[Path], default_city: str, dry_run: bool = False
) -> ImportReport:
    groups, skipped = collect(sources, default_city)
    report = ImportReport(skipped=skipped)
    for sha256, files in groups.items():
        first = files[0]
        try:
            info = read_pdf(first.path, pages=2)
        except Exception as exc:  # noqa: BLE001 – a broken PDF is reported, not fatal
            log.warning("office import: cannot read %s: %s", first.shown, exc)
            info = None
        number, aliases, reason = identify(files, info)
        if number is None:
            report.skipped.extend(SkippedFile(shown=f.shown, reason=reason) for f in files)
            continue
        cities = Counter(f.city for f in files)
        plan = Plan(
            number=number,
            name="",
            status="",
            source=OFFICE_SOURCE,
            aliases=aliases,
            city=cities.most_common(1)[0][0],
        )
        kind = office_kind([f.path.stem for f in files], info)
        # The name that says the most: one with a document word, else the first.
        named = next((f for f in files if office_kind([f.path.stem], None)), first)
        record = library.find(plan)
        decision = known_decision(library, plan, record.plan if record else None)
        decision = decision or classify_scope(plan, None, None, 1.0)
        present = {f.sha256: f.source for f in record.plan.files} if record else {}
        note = ""
        if sha256 in present:
            source = present[sha256]
            note = "כבר יובא" if source == OFFICE_SOURCE else f"זהה לקובץ מ-{source} שכבר בספרייה"
        if record is not None:
            folder = record.folder
        else:
            folder = plan_folder(LibraryPlan(number=number, city=plan.city, scope=decision.scope))
        if not dry_run:
            file = NewFile(
                local_path=first.path,
                path=f"{OFFICE_DIR}/{safe_name(named.path.name, max_length=80)}",
                kind=kind,
                title=named.path.stem,
                source=OFFICE_SOURCE,
                url=named.path.as_uri(),
            )
            warnings: list[str] = []
            try:
                folder = library.record(
                    plan, LibraryUpdate(decision=decision, reported=False), [file], warnings
                ).folder
            except (LibraryError, OSError) as exc:
                report.skipped.extend(
                    SkippedFile(shown=f.shown, reason=f"שגיאה בכתיבה לספרייה: {exc}") for f in files
                )
                continue
            # The share went away: the rest waits in the local copy for the next sync.
            report.warnings.extend(warnings)
        report.imported.append(
            ImportedPlan(
                number=number, folder=folder, files=[f.shown for f in files], kind=kind, note=note
            )
        )
    return report
