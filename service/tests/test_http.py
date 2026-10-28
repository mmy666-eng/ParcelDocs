import httpx
import pytest

from app.download import downloader
from app.download.downloader import HttpClient, SourceError

URL = "https://handasi.complot.co.il/x"


@pytest.fixture
def sleeps(monkeypatch: pytest.MonkeyPatch) -> list[float]:
    recorded: list[float] = []
    monkeypatch.setattr(downloader.time, "sleep", recorded.append)
    return recorded


def _client(responses: list[httpx.Response], **kwargs: object) -> HttpClient:
    def handler(request: httpx.Request) -> httpx.Response:
        return responses.pop(0)

    return HttpClient("t", transport=httpx.MockTransport(handler), **kwargs)  # type: ignore[arg-type]


def test_429_honors_retry_after(sleeps: list[float]):
    client = _client(
        [httpx.Response(429, headers={"Retry-After": "7"}), httpx.Response(200, text="ok")],
        requests_per_second=0,
    )
    assert client.get(URL).text == "ok"
    assert sleeps == [7.0]


def test_429_without_header_waits_rate_limited_time(sleeps: list[float]):
    client = _client(
        [httpx.Response(429), httpx.Response(429), httpx.Response(200)],
        requests_per_second=0,
        rate_limited_wait_seconds=30,
    )
    client.get(URL)
    assert sleeps == [30.0, 60.0]


def test_gives_up_after_retries(sleeps: list[float]):
    client = _client([httpx.Response(429)] * 2, requests_per_second=0, retries=2)
    with pytest.raises(SourceError, match="429"):
        client.get(URL)


def test_client_errors_are_not_retried(sleeps: list[float]):
    client = _client([httpx.Response(404)], requests_per_second=0)
    with pytest.raises(SourceError, match="404"):
        client.get(URL)
    assert sleeps == []


def test_host_interval_overrides_default(sleeps: list[float]):
    client = _client(
        [httpx.Response(200), httpx.Response(200)],
        requests_per_second=100,
        host_min_interval_seconds={"handasi.complot.co.il": 3.0},
    )
    client.get(URL)
    client.get(URL)
    assert len(sleeps) == 1
    assert 2.9 < sleeps[0] <= 3.0
