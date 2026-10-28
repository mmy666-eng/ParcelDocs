import json
from datetime import date

import httpx

from app.models import DocumentKind
from app.providers.rmi import (
    RmiPlansProvider,
    file_url,
    is_approved,
    parse_plans,
    parse_short_date,
)
from tests.conftest import fixture_bytes, offline_http

API = "https://apps.land.gov.il/TabaSearch/api"
FILES = "https://apps.land.gov.il"


def _payload(name: str) -> dict:
    return json.loads(fixture_bytes(name))


def test_parse_plans_of_parcel_6123_443():
    plans = parse_plans(_payload("rmi_plans_6123_443.json"), FILES)

    assert len(plans) == 12
    assert all(is_approved(p) for p in plans)
    roof = next(p for p in plans if p.number == "501-0873794")
    assert roof.source == "rmi"
    assert roof.source_id == "5051132"
    assert roof.approval_date == date(2023, 1, 26)
    assert [(d.kind, d.title) for d in roof.documents] == [
        (DocumentKind.INSTRUCTIONS, "תקנון סרוק"),
        (DocumentKind.MAP, "תשריט מצב מוצע"),
        (DocumentKind.APPENDIX, "בינוי"),
    ]
    assert roof.documents[0].url == f"{FILES}/IturTabotData/takanonim/telmer/5051132.pdf"
    # Windows separators in the API's paths become "/" (with them the server answers 404).
    assert roof.documents[1].url == f"{FILES}/IturTabotData/tabot/telmer/5051132_M.pdf"


def test_maps_proposed_state_first_and_hebrew_paths_quoted():
    plans = parse_plans(_payload("rmi_plans_6123_443.json"), FILES)
    master = next(p for p in plans if p.number == "501-0308296")

    maps = [d.title for d in master.documents if d.kind == DocumentKind.MAP]
    assert maps == ["תשריט מצב מוצע", "תשריט מצב קיים"]
    appendices = [d for d in master.documents if d.kind == DocumentKind.APPENDIX]
    assert len(appendices) == 16
    notice = next(d for d in appendices if d.title == "פרסום למתן תוקף")
    assert " " not in notice.url and "\\" not in notice.url
    assert notice.url.endswith(
        "/100/%D7%A4%D7%A8%D7%A1%D7%95%D7%9D%20%D7%9C%D7%9E%D7%AA%D7%9F%20%D7%AA%D7%95%D7%A7%D7%A3.pdf"
    )


def test_old_plans_and_two_digit_years():
    plans = {p.number: p for p in parse_plans(_payload("rmi_plans_6123_443.json"), FILES)}

    assert plans["בב/68"].approval_date == date(1959, 6, 4)
    assert plans["בב/105א"].approval_date == date(1971, 3, 11)
    assert parse_short_date(" 16/09/26", today=date(2026, 10, 28)) == date(2026, 9, 16)
    assert parse_short_date(" 16/09/27", today=date(2026, 10, 28)) == date(1927, 9, 16)
    assert parse_short_date("") is None


def test_plan_without_documents():
    plans = parse_plans(_payload("rmi_plans_501-0175646.json"), FILES)

    assert [(p.number, p.documents) for p in plans] == [("501-0175646", [])]


def test_not_yet_approved_plans_are_dropped():
    payload = _payload("rmi_plans_6123_443.json")
    payload["plansSmall"][0]["status"] = "הפקדה"
    http = offline_http(_handler(payload))

    plans = RmiPlansProvider(http, API, FILES).find_by_parcel(6123, 443)

    assert len(plans) == 11
    assert "501-0211441" not in {p.number for p in plans}


def test_search_request_and_plan_types_fetched_once():
    bodies: list[dict] = []
    http = offline_http(_handler(_payload("rmi_plans_6123_443.json"), bodies))
    provider = RmiPlansProvider(http, API, FILES)

    provider.find_by_parcel(6123, 443)
    provider.find_by_parcel(6123, 2518)

    assert http.request_count("apps.land.gov.il") == 3
    assert bodies[0]["gush"] == "6123" and bodies[0]["chelka"] == "443"
    assert bodies[0]["planNumber"] == "" and bodies[0]["planTypesUsed"] is False
    assert 21 in bodies[0]["planTypes"]  # מפורטת


def test_find_by_number_keeps_exact_matches_only():
    http = offline_http(_handler(_payload("rmi_plans_6123_443.json")))

    plans = RmiPlansProvider(http, API, FILES).find_by_number("501-0873794")

    assert [p.number for p in plans] == ["501-0873794"]


def test_file_url():
    assert file_url(FILES + "/", "/IturTabotData\\tabot\\telmer\\1.pdf") == (
        f"{FILES}/IturTabotData/tabot/telmer/1.pdf"
    )


def _handler(payload: dict, bodies: list[dict] | None = None):
    def handler(request: httpx.Request) -> httpx.Response:
        if request.url.path.endswith("GetPlanTypesList"):
            return httpx.Response(200, content=fixture_bytes("rmi_plan_types.json"))
        assert request.url.path.endswith("/SerachPlans/GetPlans")
        if bodies is not None:
            bodies.append(json.loads(request.content))
        return httpx.Response(200, json=payload)

    return handler
