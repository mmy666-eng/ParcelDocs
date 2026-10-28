from app.cli import MANUAL_MAVAT, merge_plans
from app.models import Plan


def _city(number: str, name: str, source_id: str) -> Plan:
    return Plan(number=number, name=name, status="בתוקף", source="bnei_brak", source_id=source_id)


def _iplan(number: str, name: str) -> Plan:
    return Plan(
        number=number,
        name=name,
        status="אישור",
        source="iplan",
        plan_type="תכנית מתאר מקומית",
        mavat_url=f"https://mavat.iplan.gov.il/{number}",
    )


def test_match_by_national_number_in_city_name():
    city = [_city("מק/105/פ/1", "תיקון לתכנית דירות גג בב/מק/105/פ (מס'501-0211441)", "3283")]
    merged = merge_plans([_iplan("501-0211441", "שינויי בינוי בדירות הגג - בב/מק/105/פ/1")], city)

    assert len(merged) == 1
    plan = merged[0]
    assert plan.number == "501-0211441"
    assert plan.aliases == ["מק/105/פ/1"]
    assert plan.source == "bnei_brak+iplan"
    assert plan.mavat_url == "https://mavat.iplan.gov.il/501-0211441"
    assert plan.manual_action is None


def test_match_by_local_number_in_iplan_name():
    merged = merge_plans([_iplan("501-0416891", "בב/105/צ/2")], [_city("105/צ/2", "צ", "9")])

    assert [(p.number, p.aliases) for p in merged] == [("501-0416891", ["105/צ/2"])]


def test_parent_plan_number_in_name_does_not_steal_match():
    city = [
        _city("מק/105/פ", "פנטהאוזים", "1470"),
        _city("מק/105/פ/1", "תיקון לתכנית דירות גג בב/מק/105/פ (מס'501-0211441)", "3283"),
    ]
    merged = merge_plans([_iplan("501-0211441", "בב/מק/105/פ/1")], city)

    by_id = {p.source_id: p for p in merged}
    assert by_id["3283"].number == "501-0211441"
    assert by_id["1470"].number == "מק/105/פ"


def test_iplan_only_plan_needs_manual_action():
    merged = merge_plans([_iplan("תמא/ 60", "מטרו")], [])

    assert merged[0].manual_action == MANUAL_MAVAT
