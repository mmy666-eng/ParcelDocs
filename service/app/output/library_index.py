"""This computer's index of the office library (SQLite, local disk only).

Built from the plan.json files and safe to delete: the next sync rebuilds it. Only the
local process writes to it, so SQLite's locking is never relied on over the network.
A sync lists each city folder once and re-reads only plan folders whose modification
time changed. The index also caches municipal boundaries (GovMap muni_il).
"""

import json
import logging
import sqlite3
from collections.abc import Iterator
from contextlib import closing, contextmanager
from pathlib import Path

from pydantic import BaseModel
from shapely import wkt
from shapely.geometry.base import BaseGeometry

from app.models import PlanScope
from app.output.library import LibraryError, LibraryPlan, PlansLibrary
from app.output.plan_scope import city_key

log = logging.getLogger(__name__)

SHARED = "shared"
LOCAL = "local"
_SCHEMA = """
CREATE TABLE IF NOT EXISTS meta (key TEXT PRIMARY KEY, value TEXT);
CREATE TABLE IF NOT EXISTS plans (
    root TEXT NOT NULL, folder TEXT NOT NULL, mtime_ns INTEGER NOT NULL,
    number TEXT NOT NULL, national_number TEXT, name TEXT, city TEXT, city_key TEXT,
    plan_type TEXT, scope TEXT, scope_basis TEXT, status TEXT, approval_date TEXT,
    last_checked TEXT, complete INTEGER, files INTEGER,
    minx REAL, miny REAL, maxx REAL, maxy REAL,
    data TEXT NOT NULL,
    PRIMARY KEY (root, folder)
);
CREATE TABLE IF NOT EXISTS plan_keys (
    root TEXT NOT NULL, folder TEXT NOT NULL, key TEXT NOT NULL,
    PRIMARY KEY (root, folder, key)
);
CREATE INDEX IF NOT EXISTS plan_keys_key ON plan_keys (key);
CREATE TABLE IF NOT EXISTS plan_parcels (
    root TEXT NOT NULL, folder TEXT NOT NULL, gush INTEGER NOT NULL, helka INTEGER NOT NULL
);
CREATE INDEX IF NOT EXISTS plan_parcels_parcel ON plan_parcels (gush, helka);
CREATE TABLE IF NOT EXISTS plan_files (
    root TEXT NOT NULL, folder TEXT NOT NULL, path TEXT NOT NULL, kind TEXT,
    source TEXT, url TEXT, sha256 TEXT, size INTEGER, added_at TEXT
);
CREATE TABLE IF NOT EXISTS cities (
    key TEXT PRIMARY KEY, name TEXT NOT NULL, boundary_wkt TEXT NOT NULL, fetched_at TEXT
);
"""
_CHILD_TABLES = ("plan_keys", "plan_parcels", "plan_files")


class IndexedPlan(BaseModel):
    root: str
    folder: str
    plan: LibraryPlan


class LibraryIndex:
    def __init__(self, path: Path) -> None:
        self.path = path
        path.parent.mkdir(parents=True, exist_ok=True)
        with self._connect() as db:
            db.executescript(_SCHEMA)

    def sync(self, root: str, store: PlansLibrary) -> tuple[int, int]:
        """Bring one root up to date; (plans re-read, plans removed)."""
        on_disk = store.scan()
        with self._connect() as db:
            known = dict(db.execute("SELECT folder, mtime_ns FROM plans WHERE root = ?", (root,)))
        changed = [f for f, mtime in on_disk.items() if known.get(f) != mtime]
        removed = [f for f in known if f not in on_disk]
        for folder in removed:
            self.remove(root, folder)
        reread = 0
        for folder in changed:
            try:
                plan = store.load(folder)
            except (LibraryError, ValueError) as exc:
                log.warning("library index: skipping %s: %s", folder, exc)
                continue
            if plan is None:
                continue
            self.put(root, folder, plan, on_disk[folder])
            reread += 1
        if reread or removed:
            log.info("library index (%s): %d plans updated, %d removed", root, reread, len(removed))
        return reread, len(removed)

    def put(self, root: str, folder: str, plan: LibraryPlan, mtime_ns: int) -> None:
        bounds = _bounds(plan.boundary_wkt)
        with self._connect() as db:
            self._delete(db, root, folder)
            db.execute(
                "INSERT INTO plans VALUES (?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)",
                (
                    root,
                    folder,
                    mtime_ns,
                    plan.number,
                    plan.national_number,
                    plan.name,
                    plan.city,
                    city_key(plan.city),
                    plan.plan_type,
                    plan.scope.value,
                    plan.scope_basis.value,
                    plan.status,
                    plan.approval_date.isoformat() if plan.approval_date else None,
                    plan.last_checked.isoformat() if plan.last_checked else None,
                    int(plan.complete()),
                    len(plan.files),
                    *bounds,
                    plan.model_dump_json(),
                ),
            )
            db.executemany(
                "INSERT OR IGNORE INTO plan_keys VALUES (?,?,?)",
                [(root, folder, key) for key in plan.match_keys()],
            )
            db.executemany(
                "INSERT INTO plan_parcels VALUES (?,?,?,?)",
                [(root, folder, gush, helka) for gush, helka in plan.parcels],
            )
            db.executemany(
                "INSERT INTO plan_files VALUES (?,?,?,?,?,?,?,?,?)",
                [
                    (
                        root,
                        folder,
                        f.path,
                        f.kind.value if f.kind else None,
                        f.source,
                        f.url,
                        f.sha256,
                        f.size,
                        f.added_at.isoformat(),
                    )
                    for f in plan.files
                ],
            )

    def remove(self, root: str, folder: str) -> None:
        with self._connect() as db:
            self._delete(db, root, folder)

    def clear(self) -> None:
        with self._connect() as db:
            for table in ("plans", *_CHILD_TABLES):
                db.execute(f"DELETE FROM {table}")

    def find(self, keys: set[str], roots: tuple[str, ...] = (SHARED, LOCAL)) -> list[IndexedPlan]:
        """Plans sharing any key, the best match (most shared keys) first, by root."""
        if not keys:
            return []
        marks = ",".join("?" * len(keys))
        with self._connect() as db:
            rows = db.execute(
                f"SELECT p.root, p.folder, p.data, COUNT(*) AS hits FROM plan_keys k "
                f"JOIN plans p ON p.root = k.root AND p.folder = k.folder "
                f"WHERE k.key IN ({marks}) GROUP BY p.root, p.folder ORDER BY hits DESC",
                sorted(keys),
            ).fetchall()
        found = [_indexed(root, folder, data) for root, folder, data, _ in rows if root in roots]
        return sorted(found, key=lambda item: roots.index(item.root))

    def search(
        self,
        geometry: BaseGeometry | None,
        city: str | None,
        parcels: list[tuple[int, int]],
    ) -> list[IndexedPlan]:
        """Plans whose boundary intersects the geometry, plans reported for one of the
        parcels, and the city-wide plans of the city."""
        found: dict[tuple[str, str], IndexedPlan] = {}
        with self._connect() as db:
            if geometry is not None and not geometry.is_empty:
                minx, miny, maxx, maxy = geometry.bounds
                rows = db.execute(
                    "SELECT root, folder, data FROM plans WHERE minx <= ? AND maxx >= ? "
                    "AND miny <= ? AND maxy >= ?",
                    (maxx, minx, maxy, miny),
                ).fetchall()
                for root, folder, data in rows:
                    item = _indexed(root, folder, data)
                    boundary = item.plan.boundary_wkt
                    if boundary and wkt.loads(boundary).intersects(geometry):
                        found[(root, folder)] = item
            key = city_key(city)
            if key:
                rows = db.execute(
                    "SELECT root, folder, data FROM plans WHERE scope = ? AND city_key = ?",
                    (PlanScope.CITYWIDE.value, key),
                ).fetchall()
                for root, folder, data in rows:
                    found[(root, folder)] = _indexed(root, folder, data)
            for gush, helka in dict.fromkeys(parcels):
                rows = db.execute(
                    "SELECT p.root, p.folder, p.data FROM plan_parcels x JOIN plans p "
                    "ON p.root = x.root AND p.folder = x.folder WHERE x.gush = ? AND x.helka = ?",
                    (gush, helka),
                ).fetchall()
                for root, folder, data in rows:
                    found[(root, folder)] = _indexed(root, folder, data)
        return list(found.values())

    def plans(self, root: str | None = None) -> list[IndexedPlan]:
        query = "SELECT root, folder, data FROM plans"
        with self._connect() as db:
            if root is None:
                rows = db.execute(f"{query} ORDER BY folder").fetchall()
            else:
                rows = db.execute(f"{query} WHERE root = ? ORDER BY folder", (root,)).fetchall()
        return [_indexed(r, f, d) for r, f, d in rows]

    def count(self, root: str) -> int:
        with self._connect() as db:
            return db.execute("SELECT COUNT(*) FROM plans WHERE root = ?", (root,)).fetchone()[0]

    def city_boundary(self, city: str | None) -> BaseGeometry | None:
        key = city_key(city)
        if not key:
            return None
        with self._connect() as db:
            row = db.execute("SELECT boundary_wkt FROM cities WHERE key = ?", (key,)).fetchone()
        return wkt.loads(row[0]) if row else None

    def put_city(self, name: str, boundary_wkt: str, fetched_at: str) -> None:
        with self._connect() as db:
            db.execute(
                "INSERT OR REPLACE INTO cities VALUES (?,?,?,?)",
                (city_key(name), name, boundary_wkt, fetched_at),
            )

    def meta(self, key: str) -> str | None:
        with self._connect() as db:
            row = db.execute("SELECT value FROM meta WHERE key = ?", (key,)).fetchone()
        return row[0] if row else None

    def set_meta(self, key: str, value: str) -> None:
        with self._connect() as db:
            db.execute("INSERT OR REPLACE INTO meta VALUES (?, ?)", (key, value))

    @contextmanager
    def _connect(self) -> Iterator[sqlite3.Connection]:
        # A connection per call: the service answers from several threads.
        with closing(sqlite3.connect(self.path, timeout=30)) as db, db:
            yield db

    @staticmethod
    def _delete(db: sqlite3.Connection, root: str, folder: str) -> None:
        for table in ("plans", *_CHILD_TABLES):
            db.execute(f"DELETE FROM {table} WHERE root = ? AND folder = ?", (root, folder))


def _indexed(root: str, folder: str, data: str) -> IndexedPlan:
    return IndexedPlan(root=root, folder=folder, plan=LibraryPlan.model_validate(json.loads(data)))


def _bounds(boundary_wkt: str | None) -> tuple[float | None, ...]:
    if not boundary_wkt:
        return (None, None, None, None)
    return tuple(wkt.loads(boundary_wkt).bounds)
