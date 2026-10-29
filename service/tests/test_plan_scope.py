import json

import pytest
from shapely import wkt
from shapely.geometry import shape

from app.models import Plan, PlanScope
from app.output.plan_scope import (
    Overrides,
    ScopeBasis,
    city_key,
    classify_scope,
    identity_keys,
    lookup_keys,
    parse_scope,
)
from app.providers.iplan import _to_plan
from tests.conftest import FIXTURES


def _iplan_plans() -> dict[str, Plan]:
    payload = json.loads((FIXTURES / "iplan_query_6123_443_geometry.json").read_text("utf-8"))
    plans = [_to_plan(f["attributes"], f.get("geometry")) for f in payload["features"]]
    return {p.number: p for p in plans}


def _bnei_brak():
    payload = json.loads((FIXTURES / "govmap_muni_bnei_brak.json").read_text("utf-8"))
    return shape(payload["features"][0]["geometry"])


def _classify(plan: Plan, city_boundary=None):
    boundary = wkt.loads(plan.boundary_wkt) if plan.boundary_wkt else None
    return classify_scope(plan, boundary, city_boundary, 0.5, {"3896"})


def test_real_iplan_plans_on_6123_443():
    plans = _iplan_plans()
    city = _bnei_brak()

    roof = plans["501-0873794"]
    assert roof.city == "בני ברק" and roof.authority_level == 2
    decision = _classify(roof, city)
    assert (decision.scope, decision.basis) == (PlanScope.CITYWIDE, ScopeBasis.AREA)
    assert decision.note == "הגבול מכסה 95% משטח הרשות"

    metro = plans["תמא/ 60"]
    assert metro.authority_level == 1 and metro.city is None
    assert _classify(metro, city).scope == PlanScope.NATIONAL
    # Without the municipal boundary a boundary alone decides nothing yet.
    assert _classify(roof).basis == ScopeBasis.DEFAULT


def test_rules_without_iplan():
    def plan(number: str, **fields) -> Plan:
        return Plan(number=number, name="", status="", source="bnei_brak", **fields)

    assert _classify(plan('תמ"א 38')).basis == ScopeBasis.NUMBER
    assert _classify(plan("תממ/5")).scope == PlanScope.NATIONAL
    assert _classify(plan("תמל/1001")).scope == PlanScope.SPECIFIC
    listed = plan("מק/105/ה", source_ids={"bnei_brak": "3896"})
    assert (_classify(listed).scope, _classify(listed).basis) == (
        PlanScope.CITYWIDE,
        ScopeBasis.CONFIG,
    )
    small = plan("501-0000001", boundary_wkt="POLYGON ((183600 665800, 183700 665800, "
                 "183700 665900, 183600 665800))")  # fmt: skip
    decision = _classify(small, _bnei_brak())
    assert (decision.scope, decision.basis) == (PlanScope.SPECIFIC, ScopeBasis.AREA)


def test_city_names_and_keys():
    assert city_key("בני ברק") == "בני_ברק"
    assert city_key("תל אביב -יפו") == city_key("תל אביב-יפו") == "תל_אביב_יפו"
    assert city_key(None) == ""
    # A local number is unique only within its city; a national number everywhere.
    assert identity_keys(["105/א"], "בני ברק", False) != identity_keys(["105/א"], "ערד", False)
    assert identity_keys(["בב/105א"], "בני ברק", False) == {"l:בני_ברק:105א"}
    assert identity_keys(["501-0873794"], "ערד", False) == {"n:501-0873794"}
    plan = Plan(number="תמא/ 60", name="", status="", source="iplan", city="בני ברק")
    assert "l::תמא60" in lookup_keys(plan)


def test_overrides_by_city_and_any_number_form():
    overrides = Overrides({"בני ברק": {"מק/מעליות": "כלל-עירונית", "105/א": "ספציפית"}})
    plan = Plan(number="501-1", name="", status="", source="rmi", city="בני ברק",
                aliases=["בב/105א"])  # fmt: skip

    assert overrides.lookup(lookup_keys(plan)) == PlanScope.SPECIFIC
    elevators = Plan(number="מק/מעליות", name="", status="", source="x", city="בני ברק")
    assert overrides.lookup(lookup_keys(elevators)) == PlanScope.CITYWIDE
    elsewhere = elevators.model_copy(update={"city": "ערד"})
    assert overrides.lookup(lookup_keys(elsewhere)) is None
    assert parse_scope("כלל עירונית") == PlanScope.CITYWIDE
    with pytest.raises(ValueError):
        Overrides({"בני ברק": {"1": "עירונית"}})
