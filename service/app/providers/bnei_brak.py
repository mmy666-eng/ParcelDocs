"""Bnei Brak engineering information system (Complot, site 75) – docs/sources.md §5.

Complot answers with HTML fragments; the parsers below follow the markup seen in
the saved fixtures under tests/fixtures.
"""

import logging
import re
from datetime import date, datetime
from urllib.parse import urlencode

from bs4 import BeautifulSoup, Tag
from pydantic import BaseModel
from shapely.geometry.base import BaseGeometry

from app.download.downloader import HttpClient
from app.geo.cadastre import GovMapCadastre
from app.models import Address, Document, DocumentKind, Plan

log = logging.getLogger(__name__)

SOURCE = "bnei_brak"
IN_FORCE = "בתוקף"

_NOT_FOUND_MARKER = "לא ניתן להציג את המידע המבוקש"
_PARCELLATED_MARKER = "עברה פרצלציה"
_GET_GUSH = re.compile(r"getGush\((\d+),\s*(\d+)\)")
_GET_TABA = re.compile(r"getTaba\((\d+)\)")
_FINAL_VERSION = ("לתוקף", "בתוקף")


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
    def __init__(self, http: HttpClient, base_url: str, site_id: int) -> None:
        self._http = http
        self._base_url = base_url
        self._site_id = site_id

    def gush_file(self, gush: int, helka: int) -> GushFile | None:
        html = self._xpa("GetGushFile", g=gush, h=helka)
        return parse_gush_file(html, gush, helka)

    def taba_file(self, complot_id: int) -> TabaFile | None:
        html = self._xpa("GetTabaFile", n=complot_id)
        return parse_taba_file(html, complot_id)

    def _xpa(self, prgname: str, **params: int | str) -> str:
        args = {"siteid": self._site_id, **params}
        query = urlencode(
            {"appname": "cixpa", "prgname": prgname, **args, "arguments": ",".join(args)},
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

    def find_by_parcels(self, gush_files: list[GushFile]) -> list[Plan]:
        ids: dict[int, PlanRef | None] = {}
        for gush_file in gush_files:
            for ref in gush_file.plans:
                if ref.status == IN_FORCE:
                    ids.setdefault(ref.complot_id, ref)
        for complot_id in self._citywide_plans:
            ids.setdefault(complot_id, None)

        plans = []
        for complot_id, ref in ids.items():
            taba = self._client.taba_file(complot_id)
            if taba is None:
                log.warning("Complot: no plan file for id %d", complot_id)
                continue
            plan = _to_plan(taba, ref)
            if plan.status != IN_FORCE:
                log.info("Complot: skipping plan %s with status %s", plan.number, plan.status)
                continue
            plans.append(plan)
        log.info("Complot: %d plans in force", len(plans))
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
        documents=taba.documents,
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
