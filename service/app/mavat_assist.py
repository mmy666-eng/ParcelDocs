"""Semi-automatic completion of plan documents from mavat (docs/progress.md).

mavat requires reCAPTCHA, so the tool never talks to it. For each plan in a project
that still has no instructions or no map, it opens the plan's mavat page in the user's
own browser, and the user downloads the files there. The tool watches the downloads
folder, works out which plan each new PDF (or ZIP of PDFs) belongs to, names it
הוראות / תשריט / נספח, and files it in the project folder and in the office library.

A file is matched to a plan by, in order: a plan number in its file name, a plan
number on its first pages, or the plan whose mavat page was opened last (only when
the file looks like a planning document). Anything else waits for the user to assign.

Command line: python -m app.mavat_assist --folder "<project folder>"
"""

import argparse
import contextlib
import hashlib
import json
import logging
import os
import queue
import re
import shutil
import sys
import tempfile
import threading
import webbrowser
import zipfile
from collections import Counter
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel

from app.config import downloads_dir, load_config
from app.models import Document, DocumentKind, ManifestEntry, Plan
from app.output.library import LibraryError, PlansLibrary
from app.output.organizer import APPENDICES_DIR, OutputOrganizer
from app.pipeline import is_national_number, local_key, make_library, plan_id
from app.utils.filenames import long_path, safe_name
from app.utils.pdf import LARGE_PAGE_PT, PdfInfo, contains, read_pdf

log = logging.getLogger("parceldocs.mavat")

SOURCE = "mavat"
FILED_DIR = "ParcelDocs_תויק"
PARTIAL_SUFFIXES = {".crdownload", ".part", ".partial", ".tmp", ".download", ".opdownload"}
MAX_ZIP_BYTES = 2 * 1024**3

REASON_NAME = "מספר התכנית בשם הקובץ"
REASON_CONTENT = "מספר התכנית בתוכן הקובץ"
REASON_ACTIVE = 'התכנית שדף המבא"ת שלה נפתח אחרון'
REASON_MANUAL = "שיוך ידני"

_NATIONAL = re.compile(r"(?<!\d)(\d{3})-?(\d{7})(?!\d)")
_NOT_KEY_CHARS = re.compile(r"[\W_]+")
_PLAN_WORDS = ("הוראות", "תקנון", "תשריט", "נספח", "תכנית", "תוכנית", 'תב"ע', "תבע")
_MIN_LOCAL_KEY = 3


class FiledFile(BaseModel):
    name: str  # the downloaded file (or the ZIP member)
    plan_id: str
    plan_number: str
    kind: DocumentKind
    path: str  # relative to the project folder
    reason: str
    duplicate: bool = False
    in_library: bool = False


class UnidentifiedFile(BaseModel):
    name: str
    reason: str


class AssistPlan(BaseModel):
    id: str
    number: str
    name: str
    mavat_url: str | None
    has_instructions: bool
    has_map: bool
    done: bool
    files: list[str]


class AssistStatus(BaseModel):
    folder: str
    downloads_dir: str
    active: str | None
    plans: list[AssistPlan]
    filed: list[FiledFile]
    unidentified: list[UnidentifiedFile]


def national_numbers(text: str) -> list[str]:
    """501-0873794, also written without the dash."""
    return [f"{a}-{b}" for a, b in _NATIONAL.findall(text)]


def classify(name: str, info: PdfInfo | None) -> DocumentKind:
    """הוראות / תשריט / נספח by the file name, then by the first page, then by page size."""
    stem = Path(name).stem
    if "נספח" in stem:
        return DocumentKind.APPENDIX
    if "הוראות" in stem or "תקנון" in stem:
        return DocumentKind.INSTRUCTIONS
    if "תשריט" in stem:
        return DocumentKind.MAP
    if info is not None:
        if contains(info.text, "הוראות התכנית"):
            return DocumentKind.INSTRUCTIONS
        if contains(info.text, "נספח"):
            return DocumentKind.APPENDIX
        if contains(info.text, "תשריט") or info.max_side_pt >= LARGE_PAGE_PT:
            return DocumentKind.MAP
    return DocumentKind.APPENDIX


def looks_like_plan_document(name: str, info: PdfInfo | None) -> bool:
    stem = Path(name).stem
    if any(word in stem for word in _PLAN_WORDS) or national_numbers(stem):
        return True
    if info is None:
        return False
    return info.max_side_pt >= LARGE_PAGE_PT or any(
        contains(info.text, word) for word in ("תכנית", "תוכנית", "התכנון")
    )


def open_in_browser(url: str) -> None:
    """The user's default browser; the tool itself never contacts mavat."""
    if not url.startswith(("https://", "http://")):
        raise ValueError(f"not a web address: {url}")
    if hasattr(os, "startfile"):
        os.startfile(url)  # the default browser on Windows
    else:
        webbrowser.open(url)


class MavatAssist:
    def __init__(
        self,
        project_root: Path,
        downloads: Path,
        library: PlansLibrary | None = None,
        opener: Callable[[str], None] | None = None,
        move_filed: bool = True,
    ) -> None:
        self.organizer = OutputOrganizer.from_root(project_root)
        manifest_file = long_path(self.organizer.manifest_file)
        if not manifest_file.is_file():
            raise FileNotFoundError(f"no manifest.json in {project_root}")
        self._manifest = json.loads(manifest_file.read_text(encoding="utf-8"))
        self.plans = [Plan.model_validate(p) for p in self._manifest.get("plans", [])]
        self.downloads_dir = downloads
        self.library = library
        self._opener = opener or open_in_browser
        self._move_filed = move_filed
        self._before = self._snapshot()
        self._last_seen: dict[str, tuple[int, int]] = {}
        self._handled: set[tuple[str, int]] = set()
        self._lock = threading.RLock()
        self.active: str | None = None
        self.filed: list[FiledFile] = []
        self.unidentified: dict[str, UnidentifiedFile] = {}

    # --- public API (CLI and service) ------------------------------------------------

    def open(self, id_: str) -> None:
        with self._lock:
            plan = self._plan(id_)
            if not plan.mavat_url:
                raise ValueError(f'לתכנית {plan.number} אין קישור למבא"ת')
            self._opener(plan.mavat_url)
            self.active = id_
            self._log(f"opened mavat page of plan {plan.number}")

    def poll(self) -> list[FiledFile]:
        """File every new download that has finished (same size and time on two polls)."""
        with self._lock:
            filed: list[FiledFile] = []
            for path in self._ready_files():
                filed.extend(self._process(path))
            return filed

    def assign(self, name: str, id_: str) -> list[FiledFile]:
        with self._lock:
            if name not in self.unidentified:
                raise KeyError(name)
            path = self.downloads_dir / name
            if not long_path(path).is_file():
                del self.unidentified[name]
                raise FileNotFoundError(path)
            del self.unidentified[name]
            plan = self._plan(id_)
            if path.suffix.lower() == ".zip":
                return self._process_zip(path, plan, REASON_MANUAL)
            filed = [self._file(plan, path, name, read_pdf(path), REASON_MANUAL)]
            self._finish(path)
            return filed

    def status(self) -> AssistStatus:
        with self._lock:
            plans = []
            for plan in self.plans:
                instructions, maps, files = self._plan_files(plan)
                plans.append(
                    AssistPlan(
                        id=plan_id(plan),
                        number=plan.number,
                        name=plan.name,
                        mavat_url=plan.mavat_url,
                        has_instructions=instructions,
                        has_map=maps,
                        done=instructions and maps,
                        files=files,
                    )
                )
            return AssistStatus(
                folder=str(self.organizer.root),
                downloads_dir=str(self.downloads_dir),
                active=self.active,
                plans=plans,
                filed=list(self.filed),
                unidentified=list(self.unidentified.values()),
            )

    # --- watching the downloads folder -----------------------------------------------

    def _snapshot(self) -> dict[str, tuple[int, int]]:
        snapshot = {}
        for entry in self._entries():
            stat = entry.stat()
            snapshot[entry.name] = (stat.st_size, stat.st_mtime_ns)
        return snapshot

    def _entries(self) -> list[os.DirEntry[str]]:
        try:
            return [e for e in os.scandir(long_path(self.downloads_dir)) if e.is_file()]
        except FileNotFoundError:
            return []

    def _ready_files(self) -> list[Path]:
        ready = []
        seen: dict[str, tuple[int, int]] = {}
        for entry in self._entries():
            name = entry.name
            suffix = Path(name).suffix.lower()
            if name.startswith((".", "~")) or suffix in PARTIAL_SUFFIXES:
                continue
            if suffix not in (".pdf", ".zip"):
                continue
            stat = entry.stat()
            signature = (stat.st_size, stat.st_mtime_ns)
            if self._before.get(name) == signature or (name, stat.st_mtime_ns) in self._handled:
                continue
            if stat.st_size > 0 and self._last_seen.get(name) == signature:
                ready.append(self.downloads_dir / name)
            seen[name] = signature
        self._last_seen = seen
        return ready

    def _process(self, path: Path) -> list[FiledFile]:
        self._handled.add((path.name, long_path(path).stat().st_mtime_ns))
        if path.suffix.lower() == ".zip":
            return self._process_zip(path)
        info = read_pdf(path)
        plan, reason = self._identify(path.name, info)
        if plan is None:
            self._unidentified(path.name, reason)
            return []
        filed = [self._file(plan, path, path.name, info, reason)]
        self._finish(path)
        return filed

    def _process_zip(
        self, path: Path, plan: Plan | None = None, reason: str = ""
    ) -> list[FiledFile]:
        try:
            archive = zipfile.ZipFile(long_path(path))
        except zipfile.BadZipFile:
            self._unidentified(path.name, "קובץ ZIP פגום")
            return []
        with archive, tempfile.TemporaryDirectory() as temporary:
            members = [
                m
                for m in archive.infolist()
                if not m.is_dir() and m.filename.lower().endswith(".pdf")
            ]
            if not members:
                self._unidentified(path.name, "אין קובצי PDF בקובץ ה-ZIP")
                return []
            if sum(m.file_size for m in members) > MAX_ZIP_BYTES:
                self._unidentified(path.name, "קובץ ה-ZIP גדול מדי")
                return []
            extracted: list[tuple[str, Path, PdfInfo | None]] = []
            for index, member in enumerate(members):
                name = _member_name(member)
                # Our own file name: member paths never reach the file system.
                target = Path(temporary) / f"{index}_{safe_name(name, max_length=60)}"
                target.write_bytes(archive.read(member))
                extracted.append((name, target, read_pdf(target)))
            if plan is None:
                plan, reason = self._identify_zip(path.name, extracted)
            if plan is None:
                self._unidentified(path.name, reason)
                return []
            filed = [self._file(plan, file, name, info, reason) for name, file, info in extracted]
        self._finish(path)
        return filed

    def _identify_zip(
        self, zip_name: str, members: list[tuple[str, Path, PdfInfo | None]]
    ) -> tuple[Plan | None, str]:
        plan = self._match_name(Path(zip_name).stem)
        if plan is not None:
            return plan, REASON_NAME
        for name, _, info in members:
            plan, reason = self._identify(name, info, use_active=False)
            if plan is not None:
                return plan, reason
        if self.active and any(looks_like_plan_document(n, i) for n, _, i in members):
            return self._plan(self.active), REASON_ACTIVE
        return None, "לא נמצא מספר תכנית בשם או בתוכן הקבצים"

    def _identify(
        self, name: str, info: PdfInfo | None, use_active: bool = True
    ) -> tuple[Plan | None, str]:
        plan = self._match_name(Path(name).stem)
        if plan is not None:
            return plan, REASON_NAME
        if info is not None:
            plan = self._match_text(info.text)
            if plan is not None:
                return plan, REASON_CONTENT
        if use_active and self.active and looks_like_plan_document(name, info):
            return self._plan(self.active), REASON_ACTIVE
        if not looks_like_plan_document(name, info):
            return None, "הקובץ לא נראה כמסמך תכנית"
        return None, "לא נמצא מספר תכנית בשם או בתוכן הקובץ"

    def _match_name(self, text: str) -> Plan | None:
        found = set(national_numbers(text))
        if found:
            hits = [p for p in self.plans if _numbers(p) & found]
            return hits[0] if len(hits) == 1 else None
        normalized = _NOT_KEY_CHARS.sub("", text)
        best: list[Plan] = []
        best_length = 0
        for plan in self.plans:
            for key in _local_keys(plan):
                if key in normalized and len(key) >= best_length:
                    if len(key) > best_length:
                        best, best_length = [], len(key)
                    if plan not in best:
                        best.append(plan)
        return best[0] if len(best) == 1 else None

    def _match_text(self, text: str) -> Plan | None:
        counts = Counter(national_numbers(text))
        scores = [(sum(counts[n] for n in _numbers(p)), p) for p in self.plans]
        top = max((score for score, _ in scores), default=0)
        winners = [p for score, p in scores if score == top]
        return winners[0] if top > 0 and len(winners) == 1 else None

    # --- filing ----------------------------------------------------------------------

    def _file(
        self, plan: Plan, source: Path, name: str, info: PdfInfo | None, reason: str
    ) -> FiledFile:
        kind = classify(name, info)
        data = long_path(source).read_bytes()
        sha256 = hashlib.sha256(data).hexdigest()
        folder = self.organizer.plan_dir(plan)
        existing = _existing_files(folder)
        in_library = False
        if sha256 in existing:
            target, duplicate = existing[sha256], True
        else:
            target, duplicate = _free_target(folder, kind, name), False
            long_path(target).parent.mkdir(parents=True, exist_ok=True)
            long_path(target).write_bytes(data)
            document = Document(
                title=Path(name).stem, url=plan.mavat_url or "", source=SOURCE, kind=kind
            )
            self._record(plan, target, sha256, document)
            in_library = self._add_to_library(plan, target, document)
        relative = target.relative_to(self.organizer.root).as_posix()
        filed = FiledFile(
            name=name,
            plan_id=plan_id(plan),
            plan_number=plan.number,
            kind=kind,
            path=relative,
            reason=reason,
            duplicate=duplicate,
            in_library=in_library,
        )
        self.filed.append(filed)
        self._log(
            f"{name} -> {relative} ({kind.value}, plan {plan.number}, {reason}"
            + (", already filed)" if duplicate else ")")
        )
        return filed

    def _record(self, plan: Plan, target: Path, sha256: str, document: Document) -> None:
        entry = ManifestEntry(
            path=target.relative_to(self.organizer.root).as_posix(),
            source=SOURCE,
            url=plan.mavat_url or "",
            downloaded_at=datetime.now().astimezone(),
            sha256=sha256,
        )
        self._manifest.setdefault("files", []).append(entry.model_dump(mode="json"))
        missing = self._missing_kinds(plan)
        for item in self._manifest.get("plans", []):
            if item.get("number") == plan.number:
                item.setdefault("documents", []).append(document.model_dump(mode="json"))
                item["documents_source"] = item.get("documents_source") or SOURCE
                item["manual_action"] = (
                    f'חסרים: {", ".join(missing)} – להשלים ממבא"ת' if missing else None
                )
        path = long_path(self.organizer.manifest_file)
        temporary = path.with_name(f".{path.name}.tmp")
        temporary.write_text(json.dumps(self._manifest, ensure_ascii=False, indent=2), "utf-8")
        os.replace(temporary, path)

    def _add_to_library(self, plan: Plan, target: Path, document: Document) -> bool:
        if self.library is None:
            return False
        relative = target.relative_to(self.organizer.plan_dir(plan)).as_posix()
        try:
            self.library.add(plan, [(document, target, relative)])
        except (LibraryError, OSError) as exc:
            self._log(f"library: plan {plan.number} not saved: {exc}", logging.WARNING)
            return False
        return True

    def _finish(self, path: Path) -> None:
        if not self._move_filed:
            return
        filed_dir = self.downloads_dir / FILED_DIR
        long_path(filed_dir).mkdir(exist_ok=True)
        target = filed_dir / path.name
        stem, suffix = path.stem, path.suffix
        counter = 2
        while long_path(target).exists():
            target = filed_dir / f"{stem}_{counter}{suffix}"
            counter += 1
        shutil.move(long_path(path), long_path(target))

    def _unidentified(self, name: str, reason: str) -> None:
        self.unidentified[name] = UnidentifiedFile(name=name, reason=reason)
        self._log(f"{name}: not filed – {reason}", logging.WARNING)

    def _missing_kinds(self, plan: Plan) -> list[str]:
        instructions, maps, _ = self._plan_files(plan)
        found = {DocumentKind.INSTRUCTIONS: instructions, DocumentKind.MAP: maps}
        return [kind.value for kind, ok in found.items() if not ok]

    def _plan_files(self, plan: Plan) -> tuple[bool, bool, list[str]]:
        folder = long_path(self.organizer.plan_dir(plan))
        if not folder.is_dir():
            return False, False, []
        files = sorted(p.relative_to(folder).as_posix() for p in folder.rglob("*.pdf"))
        instructions = any(f.startswith(DocumentKind.INSTRUCTIONS.value) for f in files)
        maps = any(f.startswith(DocumentKind.MAP.value) for f in files)
        return instructions, maps, files

    def _plan(self, id_: str) -> Plan:
        for plan in self.plans:
            if plan_id(plan) == id_:
                return plan
        raise KeyError(id_)

    def _log(self, message: str, level: int = logging.INFO) -> None:
        log.log(level, message)
        stamp = datetime.now().strftime("%Y-%m-%d %H:%M:%S")
        # run.log is a convenience; a locked or missing log never stops the filing.
        with contextlib.suppress(OSError):
            self.organizer.write_log([f"{stamp} {logging.getLevelName(level)} mavat: {message}"])


def _numbers(plan: Plan) -> set[str]:
    return {n for n in [plan.number, *plan.aliases] if is_national_number(n)}


def _local_keys(plan: Plan) -> set[str]:
    keys = {local_key(n) for n in [plan.number, *plan.aliases]}
    return {k for k in keys if k and len(k) >= _MIN_LOCAL_KEY}


def _existing_files(folder: Path) -> dict[str, Path]:
    found = {}
    root = long_path(folder)
    if root.is_dir():
        for path in root.rglob("*.pdf"):
            found[hashlib.sha256(path.read_bytes()).hexdigest()] = folder / path.relative_to(root)
    return found


def _free_target(folder: Path, kind: DocumentKind, name: str) -> Path:
    """הוראות.pdf, הוראות_2.pdf, ... · תשריט.pdf, תשריט_2.pdf, ... · נספחים/<name>.pdf"""
    if kind == DocumentKind.APPENDIX:
        stem = safe_name(Path(name).stem, max_length=70)
        candidate = folder / APPENDICES_DIR / f"{stem}.pdf"
        counter = 2
        while long_path(candidate).exists():
            candidate = folder / APPENDICES_DIR / f"{stem}_{counter}.pdf"
            counter += 1
        return candidate
    candidate = folder / f"{kind.value}.pdf"
    counter = 2
    while long_path(candidate).exists():
        candidate = folder / f"{kind.value}_{counter}.pdf"
        counter += 1
    return candidate


def _member_name(member: zipfile.ZipInfo) -> str:
    """ZIP member name; names without the UTF-8 flag are usually DOS Hebrew (cp862)."""
    name = member.filename
    if not member.flag_bits & 0x800:
        raw = name.encode("cp437", errors="replace")
        for encoding in ("utf-8", "cp862", "cp1255"):
            try:
                name = raw.decode(encoding)
                break
            except UnicodeDecodeError:
                continue
    return Path(name.replace("\\", "/")).name


# --- command line ----------------------------------------------------------------------


def main(argv: list[str] | None = None) -> int:
    for stream in (sys.stdout, sys.stderr):
        if hasattr(stream, "reconfigure"):
            stream.reconfigure(encoding="utf-8")
    parser = argparse.ArgumentParser(
        prog="parceldocs-mavat", description='השלמת מסמכי תב"עות ממבא"ת (הורדה ידנית בדפדפן)'
    )
    parser.add_argument("--folder", type=Path, required=True, help="תיקיית הפרויקט")
    parser.add_argument("--config", type=Path, help="נתיב ל-config.yaml")
    parser.add_argument("--downloads", type=Path, help="תיקיית ההורדות של הדפדפן")
    args = parser.parse_args(argv)
    logging.basicConfig(level=logging.WARNING, format="%(levelname)s %(name)s: %(message)s")

    config = load_config(args.config)
    assist = MavatAssist(
        args.folder,
        args.downloads or downloads_dir(config),
        make_library(config),
        move_filed=config.move_filed_downloads,
    )
    pending = [p for p in assist.status().plans if not p.done and p.mavat_url]
    print(f"פרויקט: {args.folder}")
    print(f"תיקיית הורדות: {assist.downloads_dir}")
    if not pending:
        print('אין תכניות שחסרים להן הוראות או תשריט עם קישור למבא"ת.')
        return 0
    lines: queue.Queue[str] = queue.Queue()
    threading.Thread(target=_read_lines, args=(lines,), daemon=True).start()
    for index, plan in enumerate(pending, 1):
        print(f"\n[{index}/{len(pending)}] {plan.number} – {plan.name}")
        assist.open(plan.id)
        print("דף התכנית נפתח בדפדפן. הורד את ההוראות והתשריט (PDF או ZIP).")
        print("Enter = לתכנית הבאה · a = לשייך את הקבצים שלא זוהו לתכנית הזו · q = סיום")
        if _watch(assist, plan.id, lines) == "q":
            break
    _print_summary(assist.status())
    return 0


def _watch(assist: MavatAssist, id_: str, lines: queue.Queue[str]) -> str:
    reported: set[str] = set()
    while True:
        for filed in assist.poll():
            note = " (כבר קיים)" if filed.duplicate else ""
            print(f"  ✓ {filed.name} → {filed.path} [{filed.kind.value}; {filed.reason}]{note}")
        for item in assist.status().unidentified:
            if item.name not in reported:
                reported.add(item.name)
                print(f"  ? {item.name} – {item.reason}")
        try:
            line = lines.get(timeout=1.0).strip().lower()
        except queue.Empty:
            continue
        if line == "a":
            for item in assist.status().unidentified:
                for filed in assist.assign(item.name, id_):
                    print(f"  ✓ {filed.name} → {filed.path} [{filed.kind.value}; שיוך ידני]")
            continue
        return line


def _read_lines(lines: queue.Queue[str]) -> None:
    for line in sys.stdin:
        lines.put(line)
    lines.put("q")


def _print_summary(status: AssistStatus) -> None:
    print("\nסיכום:")
    for plan in status.plans:
        found = {"הוראות": plan.has_instructions, "תשריט": plan.has_map}
        missing = [kind for kind, ok in found.items() if not ok]
        note = f" – חסרים: {', '.join(missing)}" if missing else ""
        print(f"  {'✓' if plan.done else '✗'} {plan.number}{note}")
    for item in status.unidentified:
        print(f"  ? לא תויק: {item.name} – {item.reason}")


if __name__ == "__main__":
    sys.exit(main())
