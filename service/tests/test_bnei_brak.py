from datetime import date
from urllib.parse import parse_qs, urlsplit

import httpx

from app.models import Document, DocumentKind
from app.providers.bnei_brak import (
    BneiBrakPlansProvider,
    ComplotClient,
    classify_document,
    parse_gush_file,
    parse_taba_file,
    select_documents,
)
from tests.conftest import fixture_bytes, offline_http

BASE = "https://handasi.complot.co.il/magicscripts/mgrqispi.dll"


def _html(name: str) -> str:
    return fixture_bytes(name).decode("utf-8-sig")


def test_parse_parcel_file():
    gush_file = parse_gush_file(_html("complot_gush_6123_2518.html"), 6123, 2518)

    assert gush_file is not None
    assert gush_file.children == []
    assert [(a.street, a.number) for a in gush_file.addresses] == [("הבנים", "3")]
    assert len(gush_file.plans) == 14
    first = gush_file.plans[0]
    assert (first.complot_id, first.number, first.status) == (3896, "501-0873794", "בתוקף")
    assert first.status_date == date(2023, 1, 26)


def test_parse_parcellated_parcel():
    gush_file = parse_gush_file(_html("complot_gush_6123_443_parcellated.html"), 6123, 443)

    assert gush_file is not None
    assert gush_file.children == [(6123, 2518), (6123, 2519)]
    assert gush_file.plans == []


def test_parse_not_found_page():
    html = "<p>מצטערים,</p><p>לא ניתן להציג את המידע המבוקש.</p>"
    assert parse_gush_file(html, 1, 1) is None
    assert parse_taba_file(html, 1) is None


def test_parse_plan_file():
    taba = parse_taba_file(_html("complot_taba_3896.html"), 3896)

    assert taba is not None
    assert taba.number == "501-0873794"
    assert taba.name == "קביעת הוראות לחדרי גג ודירות גג"
    assert taba.plan_type == "תכנית מתאר מקומית"
    assert (taba.status, taba.status_date) == ("בתוקף", date(2023, 1, 26))
    assert taba.authority == "ועדה מחוזית"
    assert (taba.yalkut, taba.yalkut_date) == ("11071", date(2023, 1, 26))
    assert len(taba.documents) == 13
    assert all(d.url.startswith("https://archive.gis-net.co.il/") for d in taba.documents)


def test_select_approved_version_documents():
    taba = parse_taba_file(_html("complot_taba_3896.html"), 3896)
    assert taba is not None

    selected = select_documents(taba.documents)

    assert [(d.kind, d.title) for d in selected] == [
        (DocumentKind.INSTRUCTIONS, "תקנון לתוקף"),
        (DocumentKind.MAP, "מצב מוצע לתוקף"),
        (DocumentKind.MAP, "בינוי לתוקף"),
    ]


def test_old_plan_instructions_found_by_title():
    taba = parse_taba_file(_html("complot_taba_1201.html"), 1201)
    assert taba is not None
    assert (taba.number, taba.yalkut) == ("מק/105/ה", "5056")

    selected = select_documents(taba.documents)

    assert [(d.kind, d.title) for d in selected] == [
        (DocumentKind.INSTRUCTIONS, "+ תקנון בתוקף מק/105/ה")
    ]


def test_latest_version_when_none_marked_final():
    docs = [
        Document(
            title="תשריט א", url="u1", source="s", kind=DocumentKind.MAP, date=date(2020, 1, 1)
        ),
        Document(
            title="תשריט ב", url="u2", source="s", kind=DocumentKind.MAP, date=date(2021, 1, 1)
        ),
    ]
    assert [d.url for d in select_documents(docs)] == ["u2"]


def test_classify_document():
    assert classify_document("נספח תנועה", 'צרופות לתב"ע') == DocumentKind.APPENDIX
    assert classify_document("פרסום בשבע", 'צרופות לתב"ע') is None
    assert classify_document("מצב מוצע", None) == DocumentKind.MAP


def test_resolve_follows_parcellation_and_fetches_plans():
    pages = {
        ("GetGushFile", "443"): "complot_gush_6123_443_parcellated.html",
        ("GetGushFile", "2518"): "complot_gush_6123_2518.html",
        ("GetTabaFile", "3896"): "complot_taba_3896.html",
    }
    seen: list[str] = []

    def handler(request: httpx.Request) -> httpx.Response:
        query = parse_qs(urlsplit(str(request.url)).query)
        prgname = query["prgname"][0]
        key = query.get("h", query.get("n"))[0]
        seen.append(str(request.url))
        assert query["siteid"] == ["75"]
        name = pages.get((prgname, key))
        if name is None:
            return httpx.Response(200, text="לא ניתן להציג את המידע המבוקש")
        return httpx.Response(200, content=fixture_bytes(name))

    provider = BneiBrakPlansProvider(ComplotClient(offline_http(handler), BASE, 75))
    gush_files = provider.resolve_parcels(6123, 443)

    assert [(g.gush, g.helka) for g in gush_files] == [(6123, 2518)]
    assert "arguments=siteid,g,h" in seen[0]

    plans = provider.find_by_parcels(gush_files)
    assert [p.number for p in plans] == ["501-0873794"]
    assert plans[0].source_id == "3896"
    assert plans[0].approval_date == date(2023, 1, 26)
