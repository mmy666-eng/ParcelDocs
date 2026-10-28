"""SQLite record of downloaded files, so repeated runs do not download them again."""

import sqlite3
from datetime import datetime
from pathlib import Path

from pydantic import BaseModel


class CachedFile(BaseModel):
    url: str
    path: Path
    sha256: str
    size: int
    downloaded_at: datetime


class DownloadCache:
    def __init__(self, db_path: Path) -> None:
        db_path.parent.mkdir(parents=True, exist_ok=True)
        self._conn = sqlite3.connect(db_path)
        self._conn.execute(
            """CREATE TABLE IF NOT EXISTS downloads (
                url TEXT PRIMARY KEY,
                path TEXT NOT NULL,
                sha256 TEXT NOT NULL,
                size INTEGER NOT NULL,
                downloaded_at TEXT NOT NULL
            )"""
        )
        self._conn.commit()

    def get(self, url: str) -> CachedFile | None:
        row = self._conn.execute(
            "SELECT url, path, sha256, size, downloaded_at FROM downloads WHERE url = ?", (url,)
        ).fetchone()
        if row is None:
            return None
        return CachedFile(
            url=row[0],
            path=Path(row[1]),
            sha256=row[2],
            size=row[3],
            downloaded_at=datetime.fromisoformat(row[4]),
        )

    def put(self, entry: CachedFile) -> None:
        self._conn.execute(
            "INSERT OR REPLACE INTO downloads (url, path, sha256, size, downloaded_at) "
            "VALUES (?, ?, ?, ?, ?)",
            (entry.url, str(entry.path), entry.sha256, entry.size, entry.downloaded_at.isoformat()),
        )
        self._conn.commit()

    def close(self) -> None:
        self._conn.close()
