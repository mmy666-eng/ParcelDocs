"""Bnei Brak engineering information system (Complot, site 75) – docs/sources.md §5.

Complot answers with HTML fragments; the parsers below follow the markup seen in
the saved fixtures under tests/fixtures.
"""

import logging
import re
from collections.abc import Collection
from datetime import date, datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel
from shapely.geometry.base import BaseGeometry

from app.download.downloader import HttpClient
from app.geo.cadastre import GovMapCadastre
from app.models import Address, Document, DocumentKind, Permit, Plan

log = logging.getLogger(__name__)

SOURCE = "bnei_brak"
IN_FORCE = "בתוקף"
CITY_CODE = 6100  # בני ברק in GetYeshuvim
CITY_NAME = "בני ברק"

_NOT_FOUND_MARKER = "לא ניתן להציג את המידע המבוקש"
_NO_RESULTS_MARKER = "לא אותרו תוצאות"
_PARCELLATED_MARKER = "עברה פרצלציה"
_GET_GUSH = re.compile(r"getGush\((\d+),\s*(\d+)\)")
_GET_TABA = re.compile(r"getTaba\((\d+)\)")
_GET_REQUEST = re.compile(r"getRequest\((\d+)\)")
_RESULTS_TOTAL = re.compile(r"מתוך\s*([\d,]+)")
_PERMIT_IN_TITLE = re.compile(r"היתר\s+(\d+)")
_DATE_IN_TITLE = re.compile(r"תאריך\s+(\d{1,2}/\d{1,2}/\d{4})")
_STREET_ITEM = re.compile(r"<label>([^<]*)</label>\s*<v>(\d+)</v>\s*<k>(\d*)</k>")
_FINAL_VERSION = ("לתוקף", "בתוקף")
_SOAP_NS = "https://handasi.complot.co.il"


class PlanRef(BaseModel):
    complot_id: int
    number: str
    name: str
    status: str
    status_date: date | None = None


class GushFile(BaseModel):
    gush: int
    helka: int
    children: list[tuple[int, int]] = []
    addresses: list[Address] = []
    plans: list[PlanRef] = []


class TabaFile(BaseModel):
    complot_id: int
    number: str
    name: str
    plan_type: str | None = None
    status: str | None = None
    status_date: date | None = None
    authority: str | None = None
    yalkut: str | None = None
    yalkut_date: date | None = None
    documents: list[Document] = []


class ComplotClient:
    def __init__(
        self, http: HttpClient, base_url: str, site_id: int, ws_url: str | None = None
    ) -> None:
        self._http = http
        self._base_url = base_url
        self._site_id = site_id
        self._ws_url = ws_url

    def gush_file(self, gush: int, helka: int) -> GushFile | None:
        html = self._xpa("GetGushFile", g=gush, h=helka)
        return parse_gush_file(html, gush, helka)

    def taba_file(self, complot_id: int) -> TabaFile | None:
        html = self._xpa("GetTabaFile", n=complot_id)
        return parse_taba_file(html, complot_id)

    def requests_by_parcel(self, gush: int, helka: int) -> list[Permit]:
        # Argument lists are spelled exactly as the city site sends them (sources.md §5.3).
        html = self._xpa(
            "GetBakashotByGush",
            "siteId,grp,t,g,h,l",
            grp=0,
            t=0,
            g=gush,
            h=helka,
            l="true",
        )
        return parse_request_list(html)

    def requests_by_address(self, street_code: int, house: str) -> list[Permit]:
        html = self._xpa(
            "GetBakashotByAddress",
            "siteId,grp,t,c,s,h,l",
            grp=0,
            t=0,
            c=CITY_CODE,
            s=street_code,
            h=house,
            l="true",
        )
        return parse_request_list(html)

    def request_file(self, request_number: str) -> Permit | None:
        html = self._xpa("GetBakashaFile", b=request_number)
        return parse_request_file(html, request_number)

    def streets(self) -> dict[str, int]:
        if self._ws_url is None:
            raise RuntimeError("streets() needs the Complot web service URL")
        envelope = (
            '<?xml version="1.0" encoding="utf-8"?>'
            '<soap:Envelope xmlns:soap="http://schemas.xmlsoap.org/soap/envelope/"><soap:Body>'
            f'<GetStreets xmlns="{_SOAP_NS}"><site_id>{self._site_id}</site_id></GetStreets>'
            "</soap:Body></soap:Envelope>"
        )
        response = self._http.post(
            self._ws_url,
            content=envelope.encode("utf-8"),
            headers={
                "Content-Type": "text/xml; charset=utf-8",
                "SOAPAction": f'"{_SOAP_NS}/GetStreets"',
            },
        )
        return parse_streets(response.content.decode("utf-8-sig", errors="replace"))

    def _xpa(self, prgname: str, arguments: str | None = None, **params: int | str) -> str:
        args = {"siteid": self._site_id, **params}
        query = urlencode(
            {
                "appname": "cixpa",
                "prgname": prgname,
                **args,
                "arguments": arguments or ",".join(args),
            },
            safe=",",
        )
        response = self._http.get(f"{self._base_url}?{query}")
        return response.content.decode("utf-8-sig", errors="replace")


class BneiBrakPlansProvider:
    def __init__(
        self,
        client: ComplotClient,
        cadastre: GovMapCadastre | None = None,
        citywide_plans: list[int] | None = None,
    ) -> None:
        self._client = client
        self._cadastre = cadastre
        self._citywide_plans = citywide_plans or []

    def resolve_parcels(self, gush: int, helka: int, max_depth: int = 3) -> list[GushFile]:
        """Municipal parcel files for gush/helka, following parcellation to the child parcels."""
        resolved: list[GushFile] = []
        pending = [(gush, helka, 0)]
        seen: set[tuple[int, int]] = set()
        while pending:
            g, h, depth = pending.pop(0)
            if (g, h) in seen:
                continue
            seen.add((g, h))
            gush_file = self._client.gush_file(g, h)
            if gush_file is None:
                log.warning("Complot: no parcel file for %d/%d", g, h)
            elif gush_file.children and depth < max_depth:
                log.info("Complot: %d/%d was parcellated into %s", g, h, gush_file.children)
                pending.extend((cg, ch, depth + 1) for cg, ch in gush_file.children)
            elif not gush_file.children:
                resolved.append(gush_file)
        return resolved

    def find_by_parcels(
        self, gush_files: list[GushFile], skip_details: Collection[int] = ()
    ) -> list[Plan]:
        """Plans in force for the parcel files, with their documents (GetTabaFile).

        Plans in skip_details (their documents are already in the office library) are
        built from the parcel file's listing alone, which saves a request each; such an
        id that no parcel file lists is left out.
        """
        ids: dict[int, PlanRef | None] = {}
        for gush_file in gush_files:
            for ref in gush_file.plans:
                if ref.status == IN_FORCE:
                    ids.setdefault(ref.complot_id, ref)
        for complot_id in self._citywide_plans:
            ids.setdefault(complot_id, None)

        plans = []
        skipped = 0
        for complot_id, ref in ids.items():
            if complot_id in skip_details:
                if ref is not None:
                    plans.append(_listed_plan(ref))
                skipped += 1
                continue
            taba = self._client.taba_file(complot_id)
            if taba is None:
                log.warning("Complot: no plan file for id %d", complot_id)
                continue
            plan = _to_plan(taba, ref)
            if plan.status != IN_FORCE:
                log.info("Complot: skipping plan %s with status %s", plan.number, plan.status)
                continue
            plans.append(plan)
        log.info(
            "Complot: %d plans in force (%d without GetTabaFile: documents in the library)",
            len(plans),
            skipped,
        )
        return plans

    def find_by_parcel(self, gush: int, helka: int) -> list[Plan]:
        return self.find_by_parcels(self.resolve_parcels(gush, helka))

    def find_by_polygon(self, polygon_itm: BaseGeometry) -> list[Plan]:
        if self._cadastre is None:
            raise RuntimeError("find_by_polygon needs a cadastre")
        gush_files = []
        for parcel in self._cadastre.find_by_polygon(polygon_itm):
            gush_files.extend(self.resolve_parcels(parcel.gush, parcel.helka))
        return self.find_by_parcels(gush_files)

    def get_documents(self, plan: Plan) -> list[Document]:
        return select_documents(plan.documents)


class BneiBrakPermitsProvider:
    def __init__(self, client: ComplotClient) -> None:
        self._client = client
        self._street_codes: dict[str, int] | None = None

    def find_by_parcel(self, gush: int, helka: int) -> list[Permit]:
        return self._client.requests_by_parcel(gush, helka)

    def find_by_address(self, street: str, number: str) -> list[Permit]:
        code = self.street_code(street)
        if code is None:
            log.warning("Complot: unknown street %r", street)
            return []
        return self._client.requests_by_address(code, number)

    def street_code(self, street: str) -> int | None:
        if self._street_codes is None:
            self._street_codes = self._client.streets()
        return self._street_codes.get(street.strip())

    def details(self, permit: Permit) -> Permit:
        """The request page: permit number and date, description, status and documents."""
        detail = self._client.request_file(permit.request_number)
        if detail is None:
            return permit
        listed = permit.model_dump(exclude_none=True, exclude_defaults=True)
        found = detail.model_dump(exclude_none=True, exclude_defaults=True)
        return Permit.model_validate(listed | found)

    def get_documents(self, permit: Permit) -> list[Document]:
        return [d for d in permit.documents if d.kind is not None]


def is_granted(permit: Permit) -> bool:
    return bool(permit.permit_number) or any(
        d.kind == DocumentKind.PERMIT for d in permit.documents
    )


def _to_plan(taba: TabaFile, ref: PlanRef | None) -> Plan:
    return Plan(
        number=taba.number or (ref.number if ref else str(taba.complot_id)),
        name=taba.name or (ref.name if ref else ""),
        status=taba.status or (ref.status if ref else ""),
        source=SOURCE,
        plan_type=taba.plan_type,
        approval_date=taba.yalkut_date or taba.status_date or (ref.status_date if ref else None),
        yalkut=taba.yalkut,
        source_id=str(taba.complot_id),
        source_ids={SOURCE: str(taba.complot_id)},
        city=CITY_NAME,
        documents=taba.documents,
    )


def _listed_plan(ref: PlanRef) -> Plan:
    return Plan(
        number=ref.number,
        name=ref.name,
        status=ref.status,
        source=SOURCE,
        approval_date=ref.status_date,
        source_id=str(ref.complot_id),
        source_ids={SOURCE: str(ref.complot_id)},
        city=CITY_NAME,
    )


def classify_document(title: str, subject: str | None) -> DocumentKind | None:
    subject = subject or ""
    if "נספח" in title or "נספח" in subject:
        return DocumentKind.APPENDIX
    if "תקנון" in subject or "הוראות" in subject:
        return DocumentKind.INSTRUCTIONS
    if "תשריט" in subject:
        return DocumentKind.MAP
    if "תקנון" in title or "הוראות" in title:
        return DocumentKind.INSTRUCTIONS
    if "תשריט" in title or "מצב מוצע" in title:
        return DocumentKind.MAP
    return None


def select_documents(documents: list[Document]) -> list[Document]:
    """Pick the approved version of the instructions, maps and appendices.

    Documents marked "לתוקף"/"בתוקף" are the approved version. When a kind has none,
    the most recent date of that kind is taken. Publications and notices are skipped.
    """
    selected: list[Document] = []
    for kind in (DocumentKind.INSTRUCTIONS, DocumentKind.MAP, DocumentKind.APPENDIX):
        group = [d for d in documents if d.kind == kind]
        if not group:
            continue
        final = [d for d in group if any(marker in d.title for marker in _FINAL_VERSION)]
        if final:
            selected.extend(final)
        elif kind == DocumentKind.APPENDIX:
            selected.extend(group)
        else:
            latest = max((d.date for d in group if d.date), default=None)
            selected.extend(d for d in group if d.date == latest)
    return selected


def parse_gush_file(html: str, gush: int, helka: int) -> GushFile | None:
    if _NOT_FOUND_MARKER in html:
        return None
    soup = BeautifulSoup(html, "html.parser")
    heading = soup.find("h2")
    if heading and _PARCELLATED_MARKER in heading.get_text():
        children = list(dict.fromkeys((int(g), int(h)) for g, h in _GET_GUSH.findall(html)))
        return GushFile(gush=gush, helka=helka, children=children)

    addresses = [
        Address(city=cells[0], street=cells[1], number=cells[2])
        for cells in _rows(soup.find(id="addresses"))
        if len(cells) >= 3 and cells[1]
    ]
    plans = []
    plans_section = soup.find(id="plans")
    if isinstance(plans_section, Tag):
        for row in plans_section.select("tbody tr"):
            cells = [_text(td) for td in row.find_all("td")]
            match = _GET_TABA.search(str(row))
            if match is None or len(cells) < 5:
                continue
            plans.append(
                PlanRef(
                    complot_id=int(match.group(1)),
                    number=cells[1],
                    name=cells[2],
                    status=cells[3],
                    status_date=_parse_date(cells[4]),
                )
            )
    return GushFile(gush=gush, helka=helka, addresses=addresses, plans=plans)


def parse_taba_file(html: str, complot_id: int) -> TabaFile | None:
    if _NOT_FOUND_MARKER in html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    header: dict[str, str] = {}
    for label in soup.select("div.result-title"):
        value = label.find_next_sibling("div")
        if value is not None:
            # The page repeats these labels in its sticky navbar; the first block is canonical.
            header.setdefault(_text(label).rstrip(":").strip(), _text(value))

    main: dict[str, str] = {}
    for row in _rows(soup.find(id="plan-main")):
        if len(row) >= 2:
            main[row[0]] = row[1]

    yalkut, yalkut_date = None, None
    for cells in _rows(soup.find(id="shlavey-tochnit")):
        if len(cells) >= 3 and "מתן תוקף" in cells[1] and "ילקוט" in cells[1] and cells[2]:
            yalkut, yalkut_date = cells[2], _parse_date(cells[0])
            break

    documents = []
    archive = soup.find(id="archive")
    if isinstance(archive, Tag):
        for row in archive.select("tbody tr"):
            link = row.find("a", href=True)
            cells = [_text(td) for td in row.find_all("td")]
            if link is None or len(cells) < 3:
                continue
            title, subject = _text(link), cells[1]
            documents.append(
                Document(
                    title=title,
                    url=str(link["href"]),
                    source=SOURCE,
                    subject=subject,
                    kind=classify_document(title, subject),
                    date=_parse_date(cells[2]),
                )
            )

    return TabaFile(
        complot_id=complot_id,
        number=header.get("מספר תוכנית", ""),
        name=header.get("שם התוכנית", ""),
        plan_type=header.get("סוג התוכנית") or None,
        status=main.get("סטטוס תוכנית"),
        status_date=_parse_date(main.get("תאריך הסטטוס", "")),
        authority=main.get("בסמכות") or None,
        yalkut=yalkut,
        yalkut_date=yalkut_date,
        documents=documents,
    )


def classify_permit_document(title: str, subject: str | None) -> DocumentKind | None:
    text = f"{subject or ''} {title}"
    if "גרמושקה" in text or "תשריט" in text:
        return DocumentKind.MAP
    if (subject or "").strip() == "היתר":
        return DocumentKind.PERMIT
    return None


def parse_request_list(html: str) -> list[Permit]:
    if _NO_RESULTS_MARKER in html or _NOT_FOUND_MARKER in html:
        return []
    total = _RESULTS_TOTAL.search(html)
    soup = BeautifulSoup(html, "html.parser")
    permits = []
    for row in soup.select("tbody tr"):
        match = _GET_REQUEST.search(str(row))
        cells = [_text(td) for td in row.find_all("td")]
        if match is None or len(cells) < 8:
            continue
        permits.append(
            Permit(
                request_number=match.group(1),
                building_file=cells[2] or None,
                request_date=_parse_date(cells[3]),
                address=cells[5] or None,
                gush=_int(cells[6]),
                helka=_int(cells[7]),
                has_documents="showRequestDocs(" in str(row),
            )
        )
    if total and int(total.group(1).replace(",", "")) > len(permits):
        log.warning(
            "Complot: list shows %d of %s requests; narrow the search", len(permits), total[1]
        )
    return permits


def parse_request_file(html: str, request_number: str) -> Permit | None:
    if _NOT_FOUND_MARKER in html:
        return None
    soup = BeautifulSoup(html, "html.parser")

    info: dict[str, str] = {}
    for cells in _rows(soup.find(id="info-main")):
        if len(cells) >= 2:
            info.setdefault(cells[0], cells[1])
    header: dict[str, str] = {}
    for label in soup.select("div.result-title"):
        value = label.find_next_sibling("div")
        if value is not None:
            header.setdefault(_text(label).rstrip(":").strip(), _text(value))

    status = None
    for cells in _rows(soup.find(id="events")):
        if len(cells) >= 2 and cells[0] == "נוכחי":
            status = cells[1]
    # Columns: (link), gush, helka, migrash, land use.
    parcel_rows = _rows(soup.find(id="gushim-helkot"))
    first = parcel_rows[0] if parcel_rows and len(parcel_rows[0]) >= 3 else None
    gush = _int(first[1]) if first else None
    helka = _int(first[2]) if first else None

    documents = []
    archive = soup.find(id="archive")
    if isinstance(archive, Tag):
        for row in archive.select("tbody tr"):
            link = row.find("a", href=True)
            cells = [_text(td) for td in row.find_all("td")]
            if link is None or len(cells) < 3:
                continue
            title, subject = _text(link), cells[1]
            documents.append(
                Document(
                    title=title,
                    url=str(link["href"]),
                    source=SOURCE,
                    subject=subject,
                    kind=classify_permit_document(title, subject),
                    date=_parse_date(cells[2]),
                )
            )

    permit_number = info.get("מספר היתר") or None
    permit_date = _parse_date(info.get("תאריך הפקת היתר", ""))
    for doc in documents:
        if doc.kind == DocumentKind.PERMIT:
            if permit_number is None and (m := _PERMIT_IN_TITLE.search(doc.title)):
                permit_number = m.group(1)
            if permit_date is None and (m := _DATE_IN_TITLE.search(doc.title)):
                permit_date = _parse_date(m.group(1))

    description = " – ".join(
        part for part in (info.get("תיאור הבקשה"), _section_text(soup, "mahut")) if part
    )
    return Permit(
        request_number=request_number,
        permit_number=permit_number,
        date=permit_date,
        request_date=_parse_date(header.get("תאריך הגשה", "")),
        description=description or None,
        status=status,
        request_type=info.get("סוג הבקשה") or None,
        building_file=info.get("מספר תיק בניין") or None,
        address=header.get("כתובת") or None,
        gush=gush,
        helka=helka,
        has_documents=bool(documents),
        documents=documents,
    )


def parse_streets(xml: str) -> dict[str, int]:
    return {
        label.strip(): int(code)
        for label, code, city in _STREET_ITEM.findall(xml)
        if city in ("", str(CITY_CODE))
    }


def _section_text(soup: BeautifulSoup, section_id: str) -> str | None:
    section = soup.find(id=section_id)
    if not isinstance(section, Tag):
        return None
    body = section.find("table") or section
    text = _text(body).replace("‏", "").strip()
    return text or None


def _int(text: str) -> int | None:
    try:
        return int(text)
    except ValueError:
        return None


def _rows(section: object) -> list[list[str]]:
    if not isinstance(section, Tag):
        return []
    return [[_text(td) for td in tr.find_all("td")] for tr in section.select("tbody tr")]


def _text(tag: Tag) -> str:
    return " ".join(tag.get_text(" ", strip=True).split())


def _parse_date(text: str) -> date | None:
    try:
        return datetime.strptime(text.strip(), "%d/%m/%Y").date()
    except ValueError:
        return None
