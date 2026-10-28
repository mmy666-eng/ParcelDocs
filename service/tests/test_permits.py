from datetime import date
from pathlib import Path
from urllib.parse import parse_qs, urlsplit

import httpx

from app.cli import MANUAL_NOT_SCANNED, collect_permits
from app.models import Address, Document, DocumentKind, Permit
from app.output.organizer import OutputOrganizer
from app.providers.bnei_brak import (
    BneiBrakPermitsProvider,
    ComplotClient,
    classify_permit_document,
    is_granted,
    parse_request_file,
    parse_request_list,
    parse_streets,
)
from tests.conftest import fixture_bytes, offline_http

BASE = "https://handasi.complot.co.il/magicscripts/mgrqispi.dll"
WS = "https://handasi.complot.co.il/wsComplotPublicData/ComplotPublicData.asmx"
NOT_FOUND = "לא ניתן להציג את המידע המבוקש"


def _html(name: str) -> str:
    return fixture_bytes(name).decode("utf-8-sig")


def test_parse_request_list_by_parcel():
    permits = parse_request_list(_html("complot_requests_gush_6123_443.html"))

    assert [p.request_number for p in permits] == [
        "195000038",
        "195900022",
        "199000255",
        "199500079",
    ]
    first = permits[0]
    assert (first.gush, first.helka, first.building_file) == (6123, 443, "61230443")
    assert first.request_date == date(1950, 3, 15)
    assert first.address == "הבנים 3 בני ברק"
    assert first.has_documents is True


def test_parse_request_list_by_address_and_empty():
    assert len(parse_request_list(_html("complot_requests_address_423_3.html"))) == 5
    assert parse_request_list(_html("complot_requests_empty.html")) == []


def test_parse_request_file():
    permit = parse_request_file(_html("complot_request_20177.html"), "20177")

    assert permit is not None
    assert (permit.permit_number, permit.date) == ("9545", date(1989, 3, 23))
    assert permit.request_date == date(1989, 3, 1)
    assert (permit.gush, permit.helka) == (6123, 1012)
    assert permit.building_file == "61231012"
    assert permit.request_type == "בקשה להיתר"
    assert permit.description == "תוספת בניה – תוספת 2 כתות גן"
    assert permit.status == "פתיחת תיק בקשה"
    assert permit.address == "הרב קוק 33 בני ברק"
    assert [(d.kind, d.subject) for d in permit.documents] == [(DocumentKind.PERMIT, "היתר")]
    assert is_granted(permit)


def test_parse_streets():
    streets = parse_streets(_html("complot_streets.xml"))
    assert streets["הבנים"] == 423
    assert len(streets) > 300


def test_classify_permit_document():
    assert classify_permit_document("בקשה 1435 - היתר 911", "היתר") == DocumentKind.PERMIT
    assert classify_permit_document("בקשה 1435", "גרמושקה עם חותמת היתר") == DocumentKind.MAP
    assert classify_permit_document("מכתב", "התכתבות") is None


def test_permit_number_from_scanned_document_title():
    permit = Permit(
        request_number="195000038",
        documents=[
            Document(
                title="בקשה 1435 - היתר 911 - תאריך 15/3/1950",
                url="u",
                source="bnei_brak",
                subject="היתר",
                kind=DocumentKind.PERMIT,
            )
        ],
    )
    assert is_granted(permit)


def _routing_client(seen: list[str]) -> ComplotClient:
    details = {"195000038": "complot_request_20177.html", "195900022": "complot_request_20177.html"}

    def handler(request: httpx.Request) -> httpx.Response:
        seen.append(str(request.url))
        if request.url.path.endswith(".asmx"):
            assert request.headers["SOAPAction"] == '"https://handasi.complot.co.il/GetStreets"'
            return httpx.Response(200, content=fixture_bytes("complot_streets.xml"))
        query = parse_qs(urlsplit(str(request.url)).query)
        prgname = query["prgname"][0]
        if prgname == "GetBakashotByGush" and query["h"] == ["443"]:
            return httpx.Response(200, content=fixture_bytes("complot_requests_gush_6123_443.html"))
        if prgname == "GetBakashotByAddress":
            assert (query["c"], query["s"], query["h"]) == (["6100"], ["423"], ["3"])
            return httpx.Response(200, content=fixture_bytes("complot_requests_address_423_3.html"))
        if prgname == "GetBakashaFile" and query["b"][0] in details:
            return httpx.Response(200, content=fixture_bytes(details[query["b"][0]]))
        if prgname == "GetBakashotByGush":
            return httpx.Response(200, content=fixture_bytes("complot_requests_empty.html"))
        return httpx.Response(200, text=NOT_FOUND)

    return ComplotClient(offline_http(handler), BASE, 75, WS)


def test_collect_permits_searches_parcels_and_address_and_dedupes():
    seen: list[str] = []
    provider = BneiBrakPermitsProvider(_routing_client(seen))

    permits = collect_permits(
        provider,
        [(6123, 443), (6123, 2518), (6123, 443)],
        [Address(city="בני ברק", street="הבנים", number="3")],
    )

    # Both requests with a detail page carry permit 9545: one entry. Requests whose
    # page cannot be shown have no permit and are skipped.
    assert [p.permit_number for p in permits] == ["9545"]
    assert permits[0].request_number == "195000038"
    assert permits[0].manual_action is None
    by_gush = [u for u in seen if "GetBakashotByGush" in u]
    assert len(by_gush) == 2
    assert "arguments=siteId,grp,t,g,h,l" in by_gush[0]
    assert sum("GetBakashaFile" in u for u in seen) == 5


def test_granted_permit_without_documents_is_marked():
    class Provider:
        def find_by_parcel(self, gush: int, helka: int) -> list[Permit]:
            return [Permit(request_number="1")]

        def find_by_address(self, street: str, number: str) -> list[Permit]:
            return []

        def details(self, permit: Permit) -> Permit:
            return permit.model_copy(update={"permit_number": "77"})

        def get_documents(self, permit: Permit) -> list[Document]:
            return []

    permits = collect_permits(Provider(), [(1, 1)], [])  # type: ignore[arg-type]
    assert permits[0].manual_action == MANUAL_NOT_SCANNED


def test_permit_paths(tmp_path: Path):
    organizer = OutputOrganizer(tmp_path, "פ", 6123, 443, date(2026, 10, 28))
    permit = Permit(request_number="20177", permit_number="9545", date=date(1989, 3, 23))
    docs = [
        Document(title="a", url="1", source="s", kind=DocumentKind.PERMIT),
        Document(title="b", url="2", source="s", kind=DocumentKind.MAP),
        Document(title="c", url="3", source="s", kind=DocumentKind.MAP),
    ]

    folder = tmp_path / "פ_ג6123_ח443_2026-10-28" / "01_היתרי_בניה" / "1989_היתר_9545"
    assert organizer.permit_document_paths(permit, docs) == [
        folder / "היתר.pdf",
        folder / "תשריט_1.pdf",
        folder / "תשריט_2.pdf",
    ]
    no_number = Permit(request_number="195000038", request_date=date(1950, 3, 15))
    assert organizer.permit_dir(no_number).name == "1950_בקשה_195000038"
