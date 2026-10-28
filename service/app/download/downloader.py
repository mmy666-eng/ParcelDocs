"""Polite HTTP access to the sources: identifying User-Agent, per-host rate limit, retries."""

import hashlib
import logging
import shutil
import ssl
import time
from datetime import datetime
from pathlib import Path
from urllib.parse import urlsplit

import httpx

from app.download.cache import CachedFile, DownloadCache
from app.models import ManifestEntry
from app.utils.filenames import long_path

log = logging.getLogger(__name__)

_RETRY_STATUS = {429, 500, 502, 503, 504}


class SourceError(Exception):
    """A source answered with something we cannot use."""


class RateLimitedError(SourceError):
    """A host configured to fail fast answered 429; it is not contacted again this run."""


class RequestBudgetError(SourceError):
    """The configured number of requests to a host for this run is used up."""


class HttpClient:
    def __init__(
        self,
        user_agent: str,
        requests_per_second: float = 1.0,
        timeout_seconds: float = 60,
        retries: int = 3,
        transport: httpx.BaseTransport | None = None,
        backoff_seconds: float = 2.0,
        legacy_tls_hosts: list[str] | None = None,
        host_min_interval_seconds: dict[str, float] | None = None,
        rate_limited_wait_seconds: float = 60.0,
        host_max_requests: dict[str, int] | None = None,
        fail_fast_429_hosts: list[str] | None = None,
    ) -> None:
        self._min_interval = 1.0 / requests_per_second if requests_per_second > 0 else 0.0
        self._host_min_interval = host_min_interval_seconds or {}
        self._host_max_requests = host_max_requests or {}
        self._fail_fast_429 = set(fail_fast_429_hosts or [])
        self._request_count: dict[str, int] = {}
        self._blocked_hosts: set[str] = set()
        self._retries = retries
        self._backoff = backoff_seconds
        self._rate_limited_wait = rate_limited_wait_seconds
        self._last_request: dict[str, float] = {}
        self._legacy_tls_hosts = set(legacy_tls_hosts or [])
        settings = {
            "headers": {"User-Agent": user_agent},
            "timeout": timeout_seconds,
            "follow_redirects": True,
            "transport": transport,
        }
        self._client = httpx.Client(**settings)  # type: ignore[arg-type]
        # Some government servers only offer RSA key exchange with SHA-1 ciphers, which
        # OpenSSL 3 leaves out by default. Certificates are still verified.
        legacy = ssl.create_default_context()
        legacy.set_ciphers("DEFAULT:@SECLEVEL=1")
        self._legacy_client = httpx.Client(verify=legacy, **settings)  # type: ignore[arg-type]

    def get(self, url: str, params: dict[str, str] | None = None) -> httpx.Response:
        return self._request("GET", url, params=params)

    def post(
        self,
        url: str,
        data: dict[str, str] | None = None,
        content: bytes | None = None,
        headers: dict[str, str] | None = None,
    ) -> httpx.Response:
        return self._request("POST", url, data=data, content=content, headers=headers)

    def _request(self, method: str, url: str, **kwargs: object) -> httpx.Response:
        host = urlsplit(url).netloc
        client = self._legacy_client if host in self._legacy_tls_hosts else self._client
        for attempt in range(1, self._retries + 1):
            self._check_allowed(host)
            self._wait_turn(host)
            self._request_count[host] = self._request_count.get(host, 0) + 1
            try:
                response = client.request(method, url, **kwargs)  # type: ignore[arg-type]
            except httpx.TransportError as exc:
                if attempt == self._retries:
                    raise SourceError(f"{method} {url} failed: {exc}") from exc
                log.warning("%s %s failed (%s), retry %d", method, url, exc, attempt)
            else:
                if response.status_code not in _RETRY_STATUS:
                    if response.is_error:
                        raise SourceError(f"{method} {url} returned HTTP {response.status_code}")
                    return response
                if response.status_code == 429 and host in self._fail_fast_429:
                    self._blocked_hosts.add(host)
                    raise RateLimitedError(f"{host} returned HTTP 429; no further requests")
                if attempt == self._retries:
                    raise SourceError(f"{method} {url} returned HTTP {response.status_code}")
                wait = self._backoff * attempt
                if response.status_code == 429:
                    wait = _retry_after(response) or self._rate_limited_wait * attempt
                log.warning(
                    "%s %s returned %d, waiting %.0fs before retry %d",
                    method,
                    url,
                    response.status_code,
                    wait,
                    attempt,
                )
                time.sleep(wait)
                continue
            time.sleep(self._backoff * attempt)
        raise AssertionError("unreachable")

    def _check_allowed(self, host: str) -> None:
        if host in self._blocked_hosts:
            raise RateLimitedError(f"{host} already returned HTTP 429 in this run")
        limit = self._host_max_requests.get(host)
        if limit is not None and self._request_count.get(host, 0) >= limit:
            raise RequestBudgetError(f"{host}: request budget of {limit} for this run is used up")

    def request_count(self, host: str) -> int:
        return self._request_count.get(host, 0)

    def start_run(self) -> None:
        """Reset per-run request budgets and 429 stops (the service handles many runs)."""
        self._request_count.clear()
        self._blocked_hosts.clear()

    def _wait_turn(self, host: str) -> None:
        last = self._last_request.get(host)
        if last is not None:
            interval = self._host_min_interval.get(host, self._min_interval)
            delay = interval - (time.monotonic() - last)
            if delay > 0:
                time.sleep(delay)
        self._last_request[host] = time.monotonic()

    def close(self) -> None:
        self._client.close()
        self._legacy_client.close()


def _retry_after(response: httpx.Response) -> float | None:
    try:
        return float(response.headers["Retry-After"])
    except (KeyError, ValueError):
        return None


class Downloader:
    """Downloads files once; later runs copy them from the earlier download (PRD §6)."""

    def __init__(self, http: HttpClient, cache: DownloadCache) -> None:
        self._http = http
        self._cache = cache

    def download(self, url: str, dest: Path, source: str, root: Path) -> ManifestEntry:
        dest.parent.mkdir(parents=True, exist_ok=True)
        cached = self._cache.get(url)
        if cached is not None and self._is_intact(cached):
            if long_path(cached.path) != long_path(dest):
                shutil.copyfile(long_path(cached.path), long_path(dest))
            log.info("from cache: %s -> %s", url, dest)
            return self._entry(dest, root, source, url, cached.sha256, cached.downloaded_at, True)

        response = self._http.get(url)
        content = response.content
        long_path(dest).write_bytes(content)
        sha256 = hashlib.sha256(content).hexdigest()
        now = datetime.now().astimezone()
        self._cache.put(
            CachedFile(
                url=url, path=dest.resolve(), sha256=sha256, size=len(content), downloaded_at=now
            )
        )
        log.info("downloaded: %s -> %s (%d bytes)", url, dest, len(content))
        return self._entry(dest, root, source, url, sha256, now, False)

    @staticmethod
    def _is_intact(cached: CachedFile) -> bool:
        path = long_path(cached.path)
        if not path.is_file() or path.stat().st_size != cached.size:
            return False
        return hashlib.sha256(path.read_bytes()).hexdigest() == cached.sha256

    @staticmethod
    def _entry(
        dest: Path,
        root: Path,
        source: str,
        url: str,
        sha256: str,
        downloaded_at: datetime,
        from_cache: bool,
    ) -> ManifestEntry:
        return ManifestEntry(
            path=dest.resolve().relative_to(root.resolve()).as_posix(),
            source=source,
            url=url,
            downloaded_at=downloaded_at,
            sha256=sha256,
            from_cache=from_cache,
        )
