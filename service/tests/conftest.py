"""Shared test helpers. Tests never touch the network: HTTP goes through httpx.MockTransport."""

from collections.abc import Callable
from pathlib import Path

import httpx
import pytest

from app.download.downloader import HttpClient

FIXTURES = Path(__file__).parent / "fixtures"


def fixture_bytes(name: str) -> bytes:
    return (FIXTURES / name).read_bytes()


Handler = Callable[[httpx.Request], httpx.Response]


def offline_http(handler: Handler) -> HttpClient:
    return HttpClient(
        user_agent="ParcelDocs-tests",
        requests_per_second=0,
        retries=1,
        transport=httpx.MockTransport(handler),
        backoff_seconds=0,
        rate_limited_wait_seconds=0,
    )


@pytest.fixture
def no_network() -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler
