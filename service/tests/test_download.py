import hashlib
from pathlib import Path

import httpx

from app.download.cache import DownloadCache
from app.download.downloader import Downloader
from tests.conftest import offline_http

URL = "https://archive.gis-net.co.il/bneybrak/Pirsumim/76/x.pdf"
PDF = b"%PDF-1.4 test"


def test_second_run_uses_cache(tmp_path: Path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF)

    cache = DownloadCache(tmp_path / "cache.sqlite")
    downloader = Downloader(offline_http(handler), cache)
    root1, root2 = tmp_path / "run1", tmp_path / "run2"

    first = downloader.download(URL, root1 / "02_תבעות" / "הוראות.pdf", "bnei_brak", root1)
    second = downloader.download(URL, root2 / "02_תבעות" / "הוראות.pdf", "bnei_brak", root2)

    assert calls == [URL]
    assert first.sha256 == second.sha256 == hashlib.sha256(PDF).hexdigest()
    assert (first.from_cache, second.from_cache) == (False, True)
    assert second.path == "02_תבעות/הוראות.pdf"
    assert (root2 / "02_תבעות" / "הוראות.pdf").read_bytes() == PDF
    cache.close()


def test_corrupted_cached_file_is_downloaded_again(tmp_path: Path):
    calls: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        calls.append(str(request.url))
        return httpx.Response(200, content=PDF)

    cache = DownloadCache(tmp_path / "cache.sqlite")
    downloader = Downloader(offline_http(handler), cache)
    target = tmp_path / "run" / "a.pdf"

    downloader.download(URL, target, "s", tmp_path / "run")
    target.write_bytes(b"broken")
    entry = downloader.download(URL, target, "s", tmp_path / "run")

    assert len(calls) == 2
    assert entry.from_cache is False
    cache.close()
