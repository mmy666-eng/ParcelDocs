from app.models import Document, DocumentKind, Plan
from app.pipeline import choose_documents, local_key, merge_plans, merge_rmi


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


def test_iplan_only_plan_is_kept_without_documents():
    merged = merge_plans([_iplan("תמא/ 60", "מטרו")], [])

    assert [(p.number, p.source, p.manual_action) for p in merged] == [("תמא/ 60", "iplan", None)]


def _rmi(number: str, *kinds: DocumentKind) -> Plan:
    documents = [
        Document(title=kind.value, url=f"https://apps.land.gov.il/{number}/{i}.pdf", source="rmi",
                 kind=kind)
        for i, kind in enumerate(kinds)
    ]  # fmt: skip
    return Plan(number=number, name="", status="פרסום לתוקף ברשומות", source="rmi",
                documents=documents)  # fmt: skip


def test_rmi_joins_by_national_number_and_adds_unknown_plans():
    plans = [_iplan("501-0873794", "חדרי יציאה לגג")]
    rmi = [
        _rmi("501-0873794", DocumentKind.INSTRUCTIONS, DocumentKind.MAP),
        _rmi("בב/68", DocumentKind.INSTRUCTIONS),
    ]

    merged = merge_rmi(plans, rmi)

    assert [(p.number, p.source) for p in merged] == [
        ("501-0873794", "iplan+rmi"),
        ("בב/68", "rmi"),
    ]
    assert len(merged[0].documents) == 2
    assert merged[0].mavat_url == "https://mavat.iplan.gov.il/501-0873794"


def test_rmi_local_number_matches_city_number_written_differently():
    city = [_city("105/א", "תכנית מתאר", "77")]

    merged = merge_rmi(city, [_rmi("בב/105א", DocumentKind.MAP)])

    assert [(p.number, p.aliases, p.source) for p in merged] == [
        ("105/א", ["בב/105א"], "bnei_brak+rmi")
    ]
    assert local_key("בב/105א") == local_key("105/א") == "105א"
    assert local_key("105/א/1") != local_key("105/א")
    assert local_key("501-0873794") is None


def test_documents_come_from_complete_library_then_rmi_then_city():
    city_doc = Document(title="תקנון לתוקף", url="https://archive/x.pdf", source="bnei_brak",
                        kind=DocumentKind.INSTRUCTIONS)  # fmt: skip
    plan = _rmi("501-0873794", DocumentKind.INSTRUCTIONS, DocumentKind.MAP)
    plan.documents.append(city_doc)
    library_full = [
        Document(title="הוראות", url="file:///l/h.pdf", source="library",
                 kind=DocumentKind.INSTRUCTIONS),
        Document(title="תשריט", url="file:///l/t.pdf", source="library", kind=DocumentKind.MAP),
    ]  # fmt: skip

    assert choose_documents(plan, library_full) == (library_full, "library")
    # A library entry without a map does not beat rmi.
    documents, source = choose_documents(plan, library_full[:1])
    assert source == "rmi" and len(documents) == 2
    # Without rmi documents the city's approved version is used.
    city_only = plan.model_copy(update={"documents": [city_doc]})
    assert choose_documents(city_only, library_full[:1]) == ([city_doc], "bnei_brak")
    # Nothing anywhere but a partial library entry: use it.
    empty = plan.model_copy(update={"documents": []})
    assert choose_documents(empty, library_full[:1]) == (library_full[:1], "library")
    assert choose_documents(empty, []) == ([], "")
