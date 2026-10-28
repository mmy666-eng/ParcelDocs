"""Office library of plan documents, shared between users and projects.

    <library>/<plan number>/הוראות.pdf, תשריט.pdf, נספחים/<title>.pdf, plan.json

The library is checked before any source, and every plan document that was downloaded
(or filed by hand from mavat) is added to it. plan.json records where each file first
came from. Several users may write at once, so each plan folder is locked while it is
updated, and files are written under a temporary name and then renamed.
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

from app.models import Document, DocumentKind, Plan
from app.utils.filenames import long_path, safe_name

log = logging.getLogger(__name__)

LIBRARY_SOURCE = "library"
PLAN_FILE = "plan.json"
LOCK_FILE = ".lock"


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


class LibraryPlan(BaseModel):
    number: str
    aliases: list[str] = []
    name: str = ""
    approval_date: date | None = None
    files: list[LibraryFile] = []


class PlansLibrary:
    def __init__(
        self, root: Path, lock_timeout_seconds: float = 30.0, stale_lock_seconds: float = 600.0
    ) -> None:
        self.root = root
        self._lock_timeout = lock_timeout_seconds
        self._stale_lock = stale_lock_seconds

    def folder(self, number: str) -> Path:
        return self.root / safe_name(number, max_length=60)

    def find(self, plan: Plan) -> tuple[Path, LibraryPlan] | None:
        """The library entry of a plan, by its number or any of its aliases."""
        for number in dict.fromkeys([plan.number, *plan.aliases]):
            folder = self.folder(number)
            entry = self._load(folder)
            if entry is not None:
                return folder, entry
        return None

    def documents(self, plan: Plan) -> list[Document]:
        found = self.find(plan)
        if found is None:
            return []
        folder, entry = found
        documents = []
        for item in entry.files:
            path = folder / item.path
            if not long_path(path).is_file():
                log.warning("library: %s listed in %s but missing", item.path, folder)
                continue
            documents.append(
                Document(
                    title=item.title,
                    url=path.resolve().as_uri(),
                    source=LIBRARY_SOURCE,
                    kind=item.kind,
                    origin_source=item.source,
                    origin_url=item.url,
                )
            )
        return documents

    def add(self, plan: Plan, files: list[tuple[Document, Path, str]]) -> int:
        """Copy a plan's files into the library: (document, local file, relative path).

        A file whose SHA256 is already in the plan folder is skipped. A different file
        under a name that is taken gets a _2, _3 suffix. Returns the number of new files.
        """
        found = self.find(plan)
        folder = found[0] if found else self.folder(plan.number)
        added = 0
        with self._lock(folder):
            entry = self._load(folder) or LibraryPlan(number=plan.number)
            aliases = [a for a in [plan.number, *plan.aliases] if a != entry.number]
            entry.aliases = list(dict.fromkeys([*entry.aliases, *aliases]))
            entry.name = entry.name or plan.name
            entry.approval_date = entry.approval_date or plan.approval_date
            known = {f.sha256 for f in entry.files}
            taken = {f.path for f in entry.files}
            for document, source_path, relative in files:
                data = long_path(source_path).read_bytes()
                sha256 = hashlib.sha256(data).hexdigest()
                if sha256 in known:
                    continue
                relative = _free_name(relative, taken)
                _write_atomic(folder / relative, data)
                entry.files.append(
                    LibraryFile(
                        path=relative,
                        kind=document.kind,
                        title=document.title,
                        sha256=sha256,
                        size=len(data),
                        source=document.origin_source or document.source,
                        url=document.origin_url or document.url,
                        added_at=datetime.now().astimezone(),
                        added_by=_user(),
                    )
                )
                known.add(sha256)
                taken.add(relative)
                added += 1
            if added:
                _write_atomic(
                    folder / PLAN_FILE,
                    entry.model_dump_json(indent=2).encode("utf-8"),
                )
        if added:
            log.info("library: %d new files for plan %s in %s", added, plan.number, folder)
        return added

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

    @contextmanager
    def _lock(self, folder: Path) -> Iterator[None]:
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
            break
        try:
            yield
        finally:
            lock.unlink(missing_ok=True)


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


def _user() -> str | None:
    try:
        return getpass.getuser()
    except (KeyError, OSError):
        return None
