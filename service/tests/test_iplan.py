import json
from datetime import date
from urllib.parse import parse_qs

import httpx
from shapely.geometry import box

from app.providers.iplan import IplanPlansProvider
from tests.conftest import fixture_bytes, offline_http

LAYER = "https://ags.iplan.gov.il/arcgisiplan/rest/services/PlanningPublic/Xplan/MapServer/1"


def test_only_approved_plans_are_returned():
    seen: list[httpx.Request] = []

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(request)
        return httpx.Response(200, content=fixture_bytes("iplan_query.json"))

    plans = IplanPlansProvider(offline_http(handler), LAYER).find_by_polygon(
        box(183608, 665815, 183635, 665838)
    )

    numbers = {p.number for p in plans}
    assert numbers == {
        "501-0211441",
        "501-0175646",
        "501-0873794",
        "501-0462721",
        "501-1494913",
        "תמא/ 60",
        "501-0416891",
        "501-0308296",
    }
    assert all(p.status == "אישור" for p in plans)

    roof = next(p for p in plans if p.number == "501-0873794")
    assert roof.approval_date == date(2023, 1, 26)
    assert roof.plan_type == "תכנית מתאר מקומית"
    assert roof.mavat_url == "https://mavat.iplan.gov.il/SV4/1/5005077848/310"

    request = seen[0]
    assert request.method == "POST"
    assert str(request.url) == LAYER + "/query"
    form = parse_qs(request.content.decode())
    assert form["inSR"] == ["2039"]
    assert json.loads(form["geometry"][0])["spatialReference"] == {"wkid": 2039}


def test_no_documents_from_mavat(no_network):
    provider = IplanPlansProvider(offline_http(no_network), LAYER)
    assert provider.get_documents(None) == []  # type: ignore[arg-type]
