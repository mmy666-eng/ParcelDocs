"""The office library as a run sees it: the shared folder, a local copy for when the share
cannot be reached, this computer's index, and the manual plan_scope corrections.

At the start of a run (open): check the share (with a time limit, as an unreachable
server can block for half a minute), bring the index up to date, push what the local
copy collected while the share was away, and apply overrides.yaml. When the share is
away, the run searches the index as of its last sync, and writes go to the local copy.
"""

import contextlib
import json
import logging
import os
import threading
from datetime import datetime, timedelta
from pathlib import Path

import yaml
from pydantic import BaseModel
from shapely import wkt
from shapely.geometry.base import BaseGeometry

from app.models import Document, Plan, PlanScope
from app.output.library import (
    LIBRARY_SOURCE,
    LibraryError,
    LibraryPlan,
    NewFile,
    PlansLibrary,
    SourceRef,
    national_number_of,
    plan_folder,
)
from app.output.library_index import LOCAL, SHARED, IndexedPlan, LibraryIndex
from app.output.plan_scope import (
    NATIONAL_FOLDER,
    Overrides,
    ScopeBasis,
    ScopeDecision,
    lookup_keys,
)
from app.utils.filenames import long_path

log = logging.getLogger("parceldocs")

OVERRIDES_FILE = "overrides.yaml"
SHARED_UNAVAILABLE = (
    "הספרייה ברשת לא זמינה – עובדים מול עותק מקומי; קבצים חדשים יסונכרנו כשהיא תחזור"
)
MANUAL_NOTE = "תיקון ידני (overrides.yaml)"
_OVERRIDES_HEADER = """\
# ParcelDocs – תיקונים ידניים של plan_scope, לפי עיר ומספר תכנית (או כינוי).
# ערכים: ספציפית / כלל-עירונית / ארצית.
# כלל-עירונית / ארצית: התכנית חלה על כל העיר ולא יורדת – רק שורה בדוח. ארצית עוברת ל-ארציות.
# מספר שמסתיים ב-* הוא כלל לסדרה: "105*" = 105, 105/ש, בב/105א (לא 1050). תכנית בשמה גוברת על כלל.
# אפשר לערוך ביד, או: python -m app.library scope "<מספר>" <סיווג> --city "<עיר>"
"""


class LibraryUpdate(BaseModel):
    """What a run learned about a plan; written to the library after the download."""

    decision: ScopeDecision
    parcels: list[tuple[int, int]] = []
    # Sources whose document list for the plan was read in this run.
    checked_sources: list[str] = []
    # A source reported the plan in this run (it did not come only from the library).
    reported: bool = True


class OfficeLibrary:
    def __init__(
        self,
        shared: Path | None,
        local: Path,
        index: Path,
        timeout_seconds: float = 5.0,
        lock_timeout_seconds: float = 30.0,
    ) -> None:
        self.shared = PlansLibrary(shared, lock_timeout_seconds) if shared else None
        self.local = PlansLibrary(local, lock_timeout_seconds)
        self.index = LibraryIndex(index)
        self.overrides = Overrides()
        self._timeout = timeout_seconds
        self._available = False

    @property
    def available(self) -> bool:
        """The shared folder answered at the last check (open or status)."""
        return self.shared is not None and self._available

    # --- run start ---------------------------------------------------------------------

    def open(self) -> list[str]:
        """Check the share, sync the index, push the local copy, apply overrides."""
        warnings: list[str] = []
        self._available = self._reachable()
        if self.shared is not None and self._available:
            try:
                self.index.sync(SHARED, self.shared)
                self._load_overrides(warnings)
                self.index.sync(LOCAL, self.local)
                pushed = self.push_local()
                if pushed:
                    log.info("library: %d plans from the local copy synced to the share", pushed)
                self._apply_overrides()
            except (LibraryError, OSError) as exc:
                log.error("library: shared folder failed: %s", exc)
                self._available = False
                warnings.append(f"{SHARED_UNAVAILABLE}: {exc}")
        elif self.shared is not None:
            log.warning("library: %s (%s)", SHARED_UNAVAILABLE, self.shared.root)
            warnings.append(SHARED_UNAVAILABLE)
            cached = self.index.meta("overrides")
            self.overrides = Overrides(json.loads(cached) if cached else {})
        try:
            self.index.sync(LOCAL, self.local)
        except LibraryError as exc:
            log.error("library: local copy failed: %s", exc)
        return warnings

    def status(self, timeout_seconds: float | None = None) -> dict[str, object]:
        self._available = self._reachable(timeout_seconds)
        with contextlib.suppress(LibraryError):
            self.index.sync(LOCAL, self.local)
        return {
            "path": str(self.shared.root) if self.shared else None,
            "available": self.available,
            "plans": self.index.count(SHARED),
            "pending_local": self.index.count(LOCAL),
            "local_copy": str(self.local.root),
        }

    def push_local(self) -> int:
        """Merge every plan of the local copy into the share, then remove it locally."""
        if self.shared is None:
            return 0
        pushed = 0
        for item in self.index.plans(LOCAL):
            folder = self.local.path(item.folder)
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
                for f in item.plan.files
                if long_path(folder / f.path).is_file()
            ]
            target = self._best(self.index.find(item.plan.match_keys(), (SHARED,)))
            self.write(SHARED, target.folder if target else item.folder, item.plan, files)
            self.local.remove(item.folder)
            self.index.remove(LOCAL, item.folder)
            log.info("library: plan %s synced from the local copy", item.plan.number)
            pushed += 1
        return pushed

    # --- reading ---------------------------------------------------------------------

    def find(self, plan: Plan) -> IndexedPlan | None:
        return self._best(self.index.find(lookup_keys(plan)))

    def find_source(self, source: str, source_id: str) -> IndexedPlan | None:
        return self._best(self.index.find({f"s:{source}:{source_id}"}))

    def search(
        self, geometry: BaseGeometry | None, city: str | None, parcels: list[tuple[int, int]]
    ) -> list[IndexedPlan]:
        """Plans for a place (PRD §4.3 step א), one entry per plan: a plan with both a
        shared record and pending local changes is returned once, as the shared one."""
        found = sorted(self.index.search(geometry, city, parcels), key=lambda i: i.root != SHARED)
        kept: list[IndexedPlan] = []
        for item in found:
            keys = item.plan.match_keys()
            if not any(keys & other.plan.match_keys() for other in kept):
                kept.append(item)
        return kept

    def documents(self, plan: Plan) -> list[Document]:
        """The plan's files in the library (shared and local copy), as file: documents."""
        documents: list[Document] = []
        seen: set[str] = set()
        matches = self.index.find(lookup_keys(plan))
        for root, store in ((SHARED, self.shared), (LOCAL, self.local)):
            item = self._best([m for m in matches if m.root == root])
            if item is None or store is None or (root == SHARED and not self.available):
                continue
            folder = store.path(item.folder)
            for entry in item.plan.files:
                path = folder / entry.path
                if entry.sha256 in seen:
                    continue
                if not long_path(path).is_file():
                    log.warning("library: %s listed in %s but missing", entry.path, folder)
                    continue
                seen.add(entry.sha256)
                documents.append(
                    Document(
                        title=entry.title,
                        url=path.resolve().as_uri(),
                        source=LIBRARY_SOURCE,
                        kind=entry.kind,
                        origin_source=entry.source,
                        origin_url=entry.url,
                    )
                )
        return documents

    def override_for(self, plan: Plan) -> PlanScope | None:
        return self.overrides.lookup(lookup_keys(plan))

    def city_boundary(self, city: str | None) -> BaseGeometry | None:
        return self.index.city_boundary(city)

    def put_city(self, name: str, boundary_wkt: str) -> None:
        self.index.put_city(name, boundary_wkt, datetime.now().astimezone().isoformat())

    # --- writing ---------------------------------------------------------------------

    def record(
        self,
        plan: Plan,
        update: LibraryUpdate,
        files: list[NewFile],
        warnings: list[str],
    ) -> IndexedPlan:
        """Write what a run learned about a plan (and its new files) to the share, or to
        the local copy when the share is away or fails."""
        incoming = library_plan(plan, update)
        if self.available:
            try:
                return self._record_in(SHARED, plan, incoming, files)
            except (LibraryError, OSError) as exc:
                log.error("library: cannot write plan %s to the share: %s", plan.number, exc)
                self._available = False
                warnings.append(f"{SHARED_UNAVAILABLE}: {exc}")
        return self._record_in(LOCAL, plan, incoming, files)

    def set_scope(self, city: str, number: str, scope: PlanScope) -> IndexedPlan | None:
        """One manual correction; the plan it now applies to, if the library has it."""
        found = self.set_scopes(city, [number], scope)[number]
        return found[0] if found else None

    def set_scopes(
        self, city: str, numbers: list[str], scope: PlanScope
    ) -> dict[str, list[IndexedPlan]]:
        """Manual corrections (plan numbers, or series rules such as "105*"), kept in
        overrides.yaml on the share and applied now; per number, the plans it covers."""
        if self.shared is None or not self._reachable():
            raise LibraryError(SHARED_UNAVAILABLE)
        self._available = True
        with self.shared.locked_root():
            text = self.shared.read_root_file(OVERRIDES_FILE)
            overrides = Overrides(yaml.safe_load(text) if text else {})
            for number in numbers:
                overrides.add(city, number, scope)
            body = yaml.safe_dump(overrides.raw, allow_unicode=True, sort_keys=True)
            self.shared.write_root_file(OVERRIDES_FILE, _OVERRIDES_HEADER + body)
        self.overrides = overrides
        self.index.set_meta("overrides", json.dumps(overrides.raw, ensure_ascii=False))
        self.index.sync(SHARED, self.shared)
        self._apply_overrides()
        plans = self.index.plans(SHARED)
        covered: dict[str, list[IndexedPlan]] = {}
        for number in numbers:
            single = Overrides({city: {number: scope.value}})
            covered[number] = [i for i in plans if single.lookup(i.plan.match_keys())]
        return covered

    # --- internals -------------------------------------------------------------------

    def _record_in(
        self, root: str, plan: Plan, incoming: LibraryPlan, files: list[NewFile]
    ) -> IndexedPlan:
        keys = lookup_keys(plan) | incoming.match_keys()
        existing = self._best(self.index.find(keys, (root,)))
        if existing is None and root == LOCAL:
            # Use the share's folder name, so that the sync goes back to the same folder.
            existing = self._best(self.index.find(keys, (SHARED,)))
        folder = existing.folder if existing else plan_folder(incoming)
        return self.write(root, folder, incoming, files)

    def write(
        self, root: str, folder: str, incoming: LibraryPlan, files: list[NewFile]
    ) -> IndexedPlan:
        """Merge a record into one plan folder of one root and index the result."""
        store = self.shared if root == SHARED else self.local
        assert store is not None
        result = store.update(folder, incoming, files)
        mtime = result.mtime_ns
        # A change to or from a national plan moves the folder; nothing else renames it.
        national = result.plan.scope == PlanScope.NATIONAL
        if (folder.split("/")[0] == NATIONAL_FOLDER) != national:
            target = plan_folder(result.plan, name=folder.split("/")[-1])
            try:
                mtime = store.move(folder, target)
                self.index.remove(root, folder)
                folder = target
            except LibraryError as exc:
                log.warning("library: plan %s stays in %s: %s", result.plan.number, folder, exc)
        self.index.put(root, folder, result.plan, mtime)
        return IndexedPlan(root=root, folder=folder, plan=result.plan)

    def _load_overrides(self, warnings: list[str]) -> None:
        assert self.shared is not None
        text = self.shared.read_root_file(OVERRIDES_FILE)
        try:
            self.overrides = Overrides(yaml.safe_load(text) if text else {})
        except (yaml.YAMLError, ValueError, AttributeError) as exc:
            log.error("library: %s ignored: %s", OVERRIDES_FILE, exc)
            warnings.append(f"{OVERRIDES_FILE} בספרייה לא תקין ולא הוחל: {exc}")
            return
        self.index.set_meta("overrides", json.dumps(self.overrides.raw, ensure_ascii=False))

    def _apply_overrides(self) -> None:
        """Every plan in the share that a correction or a series rule covers."""
        for item in self.index.plans(SHARED):
            plan = item.plan
            scope = self.overrides.lookup(plan.match_keys())
            if scope is None or (plan.scope == scope and plan.scope_basis == ScopeBasis.MANUAL):
                continue
            log.info("library: plan %s set to %s by %s", plan.number, scope.value, OVERRIDES_FILE)
            corrected = plan.model_copy(
                update={
                    "scope": scope,
                    "scope_basis": ScopeBasis.MANUAL,
                    "scope_note": MANUAL_NOTE,
                    "files": [],
                }
            )
            self.write(SHARED, item.folder, corrected, [])

    def _reachable(self, timeout_seconds: float | None = None) -> bool:
        if self.shared is None:
            return False
        result: dict[str, bool] = {}
        root = self.shared.root

        def probe() -> None:
            result["ok"] = os.path.isdir(root)

        thread = threading.Thread(target=probe, daemon=True)
        thread.start()
        thread.join(self._timeout if timeout_seconds is None else timeout_seconds)
        return result.get("ok", False)

    @staticmethod
    def _best(items: list[IndexedPlan]) -> IndexedPlan | None:
        return items[0] if items else None


def library_plan(plan: Plan, update: LibraryUpdate) -> LibraryPlan:
    """The library record of a plan as found in a run."""
    now = datetime.now().astimezone()
    decision = update.decision
    return LibraryPlan(
        number=plan.number,
        national_number=national_number_of([plan.number, *plan.aliases]),
        aliases=[a for a in plan.aliases if a != plan.number],
        name=plan.name,
        city=plan.city,
        plan_type=plan.plan_type,
        scope=decision.scope,
        scope_basis=decision.basis,
        scope_note=decision.note,
        status=(plan.status or None) if update.reported else None,
        approval_date=plan.approval_date,
        mavat_url=plan.mavat_url,
        boundary_wkt=rounded_wkt(plan.boundary_wkt),
        sources=[
            SourceRef(
                source=source,
                id=source_id,
                checked_at=now if source in update.checked_sources else None,
            )
            for source, source_id in plan.source_ids.items()
        ],
        parcels=[] if decision.scope == PlanScope.CITYWIDE else sorted(set(update.parcels)),
        last_checked=now if update.reported else None,
    )


def rounded_wkt(boundary_wkt: str | None) -> str | None:
    if not boundary_wkt:
        return None
    return wkt.dumps(wkt.loads(boundary_wkt), rounding_precision=2, trim=True)


def recently_checked(plan: LibraryPlan, source: str, days: int) -> bool:
    ref = plan.source_ref(source)
    if ref is None or ref.checked_at is None:
        return False
    return datetime.now().astimezone() - ref.checked_at < timedelta(days=days)
