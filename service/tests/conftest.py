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


def make_pdf(text: str = "", width: float = 595, height: float = 842) -> bytes:
    """A one-page PDF with ASCII text (Helvetica) and the given page size in points."""
    escaped = text.replace("\\", "\\\\").replace("(", "\\(").replace(")", "\\)")
    stream = f"BT /F1 12 Tf 72 720 Td ({escaped}) Tj ET".encode("ascii")
    objects = [
        b"<< /Type /Catalog /Pages 2 0 R >>",
        b"<< /Type /Pages /Kids [3 0 R] /Count 1 >>",
        f"<< /Type /Page /Parent 2 0 R /MediaBox [0 0 {width} {height}] /Contents 4 0 R "
        "/Resources << /Font << /F1 5 0 R >> >> >>".encode("ascii"),
        b"<< /Length %d >>\nstream\n" % len(stream) + stream + b"\nendstream",
        b"<< /Type /Font /Subtype /Type1 /BaseFont /Helvetica >>",
    ]
    out = bytearray(b"%PDF-1.4\n")
    offsets = []
    for number, body in enumerate(objects, 1):
        offsets.append(len(out))
        out += b"%d 0 obj\n" % number + body + b"\nendobj\n"
    xref = len(out)
    out += b"xref\n0 %d\n0000000000 65535 f \n" % (len(objects) + 1)
    out += b"".join(b"%010d 00000 n \n" % offset for offset in offsets)
    out += b"trailer\n<< /Size %d /Root 1 0 R >>\nstartxref\n%d\n%%%%EOF\n" % (
        len(objects) + 1,
        xref,
    )
    return bytes(out)


@pytest.fixture
def no_network() -> Handler:
    def handler(request: httpx.Request) -> httpx.Response:
        raise AssertionError(f"unexpected request: {request.method} {request.url}")

    return handler
