import httpx
import pytest

from app.download import downloader
from app.download.downloader import (
    HttpClient,
    RateLimitedError,
    RequestBudgetError,
    SourceError,
)

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


def test_fail_fast_429_stops_all_requests_to_host(sleeps: list[float]):
    responses = [httpx.Response(429), httpx.Response(200)]
    client = _client(
        responses, requests_per_second=0, fail_fast_429_hosts=["handasi.complot.co.il"]
    )
    with pytest.raises(RateLimitedError):
        client.get(URL)
    with pytest.raises(RateLimitedError):
        client.get(URL)
    assert sleeps == []
    assert len(responses) == 1  # the second request was never sent

    client.start_run()
    assert client.get(URL).status_code == 200


def test_request_budget_per_host(sleeps: list[float]):
    client = _client(
        [httpx.Response(200)] * 3,
        requests_per_second=0,
        host_max_requests={"handasi.complot.co.il": 2},
    )
    client.get(URL)
    client.get(URL)
    with pytest.raises(RequestBudgetError):
        client.get(URL)
    assert client.request_count("handasi.complot.co.il") == 2


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
