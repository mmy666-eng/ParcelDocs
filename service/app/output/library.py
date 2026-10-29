"""Office library of plan documents, shared between users, projects and computers.

    <library>/<city>/<plan number>/הוראות.pdf, תשריט.pdf, נספחים/<title>.pdf, plan.json
    <library>/ארציות/<plan number>/...      national and district plans
    <library>/overrides.yaml                manual plan_scope corrections

plan.json is the record of a plan: numbers, city, type, plan_scope, status, approval
date, boundary, its ids in the sources, the parcels it was reported for, and every file
with where it came from. It is the only shared state. Each computer keeps its own index
of these files (library_index.py), because SQLite on a network share is not safe with
several writers. A plan folder is locked while it is updated, files are written under
a temporary name and then renamed, and a plan folder keeps its name once created.
"""

import getpass
import hashlib
import logging
import os
import shutil
import time
from collections.abc import Iterator
from contextlib import contextmanager
from datetime import date, datetime
from pathlib import Path

from pydantic import BaseModel

from app.models import DocumentKind, PlanScope
from app.output.plan_scope import NATIONAL_FOLDER, ScopeBasis, city_folder, identity_keys
from app.utils.filenames import long_path, safe_name
from app.utils.plan_numbers import is_national_number

log = logging.getLogger(__name__)

LIBRARY_SOURCE = "library"
# Copies the office kept by hand (app.office_import): never preferred over a source.
OFFICE_SOURCE = "office"
PLAN_FILE = "plan.json"
LOCK_FILE = ".lock"
SCHEMA_VERSION = 2
CORE_KINDS = (DocumentKind.INSTRUCTIONS, DocumentKind.MAP)


class LibraryError(Exception):
    """The library folder cannot be read or written (offline share, lock held too long)."""


class LibraryFile(BaseModel):
    path: str  # relative to the plan folder, with "/" separators
    kind: DocumentKind | None = None
    title: str
    sha256: str
    size: int
    source: str
    url: str
    added_at: datetime
    added_by: str | None = None


class NewFile(BaseModel):
    """A file to copy into a plan folder, under `path` (relative, "/" separators)."""

    local_path: Path
    path: str
    kind: DocumentKind | None = None
    title: str
    source: str
    url: str
    added_at: datetime | None = None
    added_by: str | None = None


class SourceRef(BaseModel):
    source: str
    id: str
    # When the plan's documents were last listed in this source.
    checked_at: datetime | None = None


class LibraryPlan(BaseModel):
    version: int = SCHEMA_VERSION
    number: str
    national_number: str | None = None
    aliases: list[str] = []
    name: str = ""
    city: str | None = None
    plan_type: str | None = None
    scope: PlanScope = PlanScope.SPECIFIC
    scope_basis: ScopeBasis = ScopeBasis.DEFAULT
    scope_note: str = ""
    status: str | None = None
    approval_date: date | None = None
    mavat_url: str | None = None
    # ITM WKT, rounded to centimetres.
    boundary_wkt: str | None = None
    sources: list[SourceRef] = []
    # Parcels a source reported the plan for; kept for plans that are not city-wide.
    parcels: list[tuple[int, int]] = []
    files: list[LibraryFile] = []
    first_seen: datetime | None = None
    last_checked: datetime | None = None

    def numbers(self) -> list[str]:
        numbers = [self.number, *self.aliases, self.national_number or ""]
        return [n for n in dict.fromkeys(numbers) if n]

    def match_keys(self) -> set[str]:
        keys = identity_keys(self.numbers(), self.city, self.scope == PlanScope.NATIONAL)
        return keys | {f"s:{ref.source}:{ref.id}" for ref in self.sources}

    def kinds(self) -> set[DocumentKind]:
        return {f.kind for f in self.files if f.kind is not None}

    def complete(self) -> bool:
        """Instructions and a map from a source; office copies do not count."""
        kinds = {f.kind for f in self.files if f.kind is not None and f.source != OFFICE_SOURCE}
        return all(kind in kinds for kind in CORE_KINDS)

    def source_ref(self, source: str) -> SourceRef | None:
        return next((ref for ref in self.sources if ref.source == source), None)


def plan_folder(plan: LibraryPlan, name: str | None = None) -> str:
    """<city>/<number>, or ארציות/<number> for a national plan; name keeps an existing one."""
    parent = NATIONAL_FOLDER if plan.scope == PlanScope.NATIONAL else city_folder(plan.city)
    return f"{parent}/{name or safe_name(plan.number, max_length=60)}"


def merge_plan(old: LibraryPlan, new: LibraryPlan) -> LibraryPlan:
    """old (on disk) updated with what a run learned (new). Files are not merged here:
    PlansLibrary.update copies new files in and appends them."""
    merged = old.model_copy(deep=True)
    numbers = [*old.numbers(), *new.numbers()]
    merged.aliases = [n for n in dict.fromkeys(numbers) if n != old.number]
    merged.national_number = old.national_number or new.national_number
    merged.name = old.name or new.name
    merged.city = old.city or new.city
    merged.plan_type = old.plan_type or new.plan_type
    if new.scope_basis == ScopeBasis.MANUAL or (
        old.scope_basis == ScopeBasis.DEFAULT and new.scope_basis != ScopeBasis.DEFAULT
    ):
        merged.scope, merged.scope_basis, merged.scope_note = (
            new.scope,
            new.scope_basis,
            new.scope_note,
        )
    merged.status = new.status or old.status
    # Sources date an approval differently (gazette, newspapers); keep the first one seen.
    merged.approval_date = old.approval_date or new.approval_date
    merged.mavat_url = new.mavat_url or old.mavat_url
    merged.boundary_wkt = new.boundary_wkt or old.boundary_wkt
    refs = {(ref.source, ref.id): ref.model_copy() for ref in old.sources}
    for ref in new.sources:
        known = refs.get((ref.source, ref.id))
        if known is None:
            refs[(ref.source, ref.id)] = ref.model_copy()
        elif ref.checked_at and (known.checked_at is None or ref.checked_at > known.checked_at):
            known.checked_at = ref.checked_at
    merged.sources = list(refs.values())
    merged.parcels = (
        []
        if merged.scope == PlanScope.CITYWIDE
        else sorted({*map(tuple, old.parcels), *map(tuple, new.parcels)})
    )
    merged.first_seen = min(filter(None, [old.first_seen, new.first_seen]), default=None)
    merged.last_checked = max(filter(None, [old.last_checked, new.last_checked]), default=None)
    return merged


def national_number_of(numbers: list[str]) -> str | None:
    return next((n.strip() for n in numbers if is_national_number(n)), None)


def _differs(old: LibraryPlan, new: LibraryPlan) -> bool:
    """Worth writing: anything changed, or the last check moved to another day."""
    skip = {"last_checked"}
    if old.model_dump(exclude=skip) != new.model_dump(exclude=skip):
        return True
    day = [p.last_checked.date() if p.last_checked else None for p in (old, new)]
    return day[0] != day[1]


class UpdateResult(BaseModel):
    plan: LibraryPlan
    added: int
    written: bool
    mtime_ns: int


class PlansLibrary:
    """One library root: the shared folder, or this computer's local copy."""

    def __init__(
        self, root: Path, lock_timeout_seconds: float = 30.0, stale_lock_seconds: float = 600.0
    ) -> None:
        self.root = root
        self._lock_timeout = lock_timeout_seconds
        self._stale_lock = stale_lock_seconds

    def path(self, folder: str) -> Path:
        return self.root.joinpath(*folder.split("/"))

    def load(self, folder: str) -> LibraryPlan | None:
        return self._load(self.path(folder))

    def scan(self) -> dict[str, int]:
        """Every <city>/<plan> folder with its modification time, which changes whenever
        its plan.json is replaced. One directory listing per city."""
        found: dict[str, int] = {}
        root = long_path(self.root)
        if not root.is_dir():
            return found
        try:
            with os.scandir(root) as cities:
                for city in cities:
                    if not city.is_dir() or city.name.startswith((".", "_")):
                        continue
                    with os.scandir(city.path) as plans:
                        for plan in plans:
                            if plan.is_dir() and not plan.name.startswith((".", "_")):
                                found[f"{city.name}/{plan.name}"] = plan.stat().st_mtime_ns
        except OSError as exc:
            raise LibraryError(f"cannot list {self.root}: {exc}") from exc
        return found

    def update(
        self, folder: str, incoming: LibraryPlan, files: list[NewFile] | None = None
    ) -> UpdateResult:
        """Merge a plan record into <folder>/plan.json and copy new files in.

        A file whose SHA256 is already in the folder is skipped; a different file under
        a name that is taken gets a _2, _3 suffix.
        """
        target = self.path(folder)
        added = 0
        with self._lock(target):
            old = self._load(target)
            if old is None:
                plan = incoming.model_copy(deep=True, update={"files": []})
                plan.first_seen = plan.first_seen or _now()
            else:
                plan = merge_plan(old, incoming)
            known = {f.sha256 for f in plan.files}
            taken = {f.path for f in plan.files}
            by_sha = {f.sha256: f for f in plan.files}
            corrected = False
            for item in files or []:
                data = long_path(item.local_path).read_bytes()
                sha256 = hashlib.sha256(data).hexdigest()
                if sha256 in known:
                    # The same source again with a better idea of the document's kind.
                    same = by_sha.get(sha256)
                    if same and same.source == item.source and item.kind and same.kind != item.kind:
                        same.kind, corrected = item.kind, True
                    continue
                relative = _free_name(item.path, taken)
                _write_atomic(target / relative, data)
                plan.files.append(
                    LibraryFile(
                        path=relative,
                        kind=item.kind,
                        title=item.title,
                        sha256=sha256,
                        size=len(data),
                        source=item.source,
                        url=item.url,
                        added_at=item.added_at or _now(),
                        added_by=item.added_by or _user(),
                    )
                )
                known.add(sha256)
                taken.add(relative)
                added += 1
            written = old is None or added > 0 or corrected or _differs(old, plan)
            if written:
                _write_atomic(target / PLAN_FILE, plan.model_dump_json(indent=2).encode("utf-8"))
        if added:
            log.info("library: %d new files for plan %s in %s", added, plan.number, folder)
        return UpdateResult(plan=plan, added=added, written=written, mtime_ns=self._mtime(target))

    def move(self, old: str, new: str) -> int:
        """Rename a plan folder (a change to or from a national plan); new mtime."""
        source, target = self.path(old), self.path(new)
        if long_path(target).exists():
            raise LibraryError(f"{target} already exists")
        lock = self._acquire(source)
        try:
            long_path(target).parent.mkdir(parents=True, exist_ok=True)
            os.rename(long_path(source), long_path(target))
        except OSError as exc:
            lock.unlink(missing_ok=True)
            raise LibraryError(f"cannot move {source} to {target}: {exc}") from exc
        long_path(target / LOCK_FILE).unlink(missing_ok=True)
        log.info("library: moved %s to %s", old, new)
        return self._mtime(target)

    def remove(self, folder: str) -> None:
        shutil.rmtree(long_path(self.path(folder)))

    def read_root_file(self, name: str) -> str | None:
        try:
            return long_path(self.root / name).read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LibraryError(f"cannot read {self.root / name}: {exc}") from exc

    @contextmanager
    def locked_root(self) -> Iterator[None]:
        """For files in the library root (overrides.yaml)."""
        with self._lock(self.root):
            yield

    def write_root_file(self, name: str, text: str) -> None:
        _write_atomic(self.root / name, text.encode("utf-8"))

    @staticmethod
    def _load(folder: Path) -> LibraryPlan | None:
        path = long_path(folder / PLAN_FILE)
        try:
            text = path.read_text(encoding="utf-8")
        except FileNotFoundError:
            return None
        except OSError as exc:
            raise LibraryError(f"cannot read {path}: {exc}") from exc
        return LibraryPlan.model_validate_json(text)

    @staticmethod
    def _mtime(folder: Path) -> int:
        return long_path(folder).stat().st_mtime_ns

    @contextmanager
    def _lock(self, folder: Path) -> Iterator[None]:
        lock = self._acquire(folder)
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)

    def _acquire(self, folder: Path) -> Path:
        try:
            long_path(folder).mkdir(parents=True, exist_ok=True)
        except OSError as exc:
            raise LibraryError(f"cannot create {folder}: {exc}") from exc
        lock = long_path(folder / LOCK_FILE)
        deadline = time.monotonic() + self._lock_timeout
        while True:
            try:
                fd = os.open(lock, os.O_CREAT | os.O_EXCL | os.O_WRONLY)
            except FileExistsError:
                if _age_seconds(lock) > self._stale_lock:
                    log.warning("library: removing stale lock %s", lock)
                    lock.unlink(missing_ok=True)
                    continue
                if time.monotonic() > deadline:
                    raise LibraryError(f"{folder} is locked by another user") from None
                time.sleep(0.2)
                continue
            except OSError as exc:
                raise LibraryError(f"cannot lock {folder}: {exc}") from exc
            os.write(fd, f"{_user()} pid {os.getpid()}".encode())
            os.close(fd)
            return lock


def _free_name(relative: str, taken: set[str]) -> str:
    if relative not in taken:
        return relative
    stem, dot, ext = relative.rpartition(".")
    if not dot:
        stem, ext = relative, ""
    suffix = 2
    while True:
        candidate = f"{stem}_{suffix}.{ext}" if ext else f"{stem}_{suffix}"
        if candidate not in taken:
            return candidate
        suffix += 1


def _write_atomic(path: Path, data: bytes) -> None:
    target = long_path(path)
    target.parent.mkdir(parents=True, exist_ok=True)
    temporary = target.with_name(f".{target.name}.{os.getpid()}.tmp")
    temporary.write_bytes(data)
    os.replace(temporary, target)


def copy_into(source: Path, target: Path) -> None:
    long_path(target).parent.mkdir(parents=True, exist_ok=True)
    shutil.copyfile(long_path(source), long_path(target))


def _age_seconds(path: Path) -> float:
    try:
        return time.time() - path.stat().st_mtime
    except FileNotFoundError:
        return 0.0


def _now() -> datetime:
    return datetime.now().astimezone()


def _user() -> str | None:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None
