"""Approved plans and their documents from the Israel Land Authority plan search.

TabaSearch (docs/sources.md §3ה) is an open JSON API: plans by gush/helka or by plan
number, each with direct PDF links to the scanned instructions, maps and appendices.
It has no national plans (תמא), and some plans are listed without documents.
"""

import logging
import re
from datetime import date
from urllib.parse import quote

from app.download.downloader import HttpClient, SourceError
from app.models import Document, DocumentKind, Plan

log = logging.getLogger(__name__)

SOURCE = "rmi"
# Approved plans read "פרסום לתוקף ברשומות" or "פרסום לתוקף בעיתונים".
APPROVED_MARKER = "לתוקף"
_SHORT_DATE = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{2,4})")
# Proposed-state maps first, plain maps next, existing-state maps last.
_MAP_ORDER = ("מצב מוצע", "", "מצב קיים")


class RmiPlansProvider:
    def __init__(self, http: HttpClient, api_base: str, files_base: str) -> None:
        self._http = http
        self._api = api_base.rstrip("/")
        self._files = files_base.rstrip("/")
        self._plan_types: list[int] | None = None

    def find_by_parcel(self, gush: int, helka: int) -> list[Plan]:
        plans = self._search(gush=str(gush), helka=str(helka))
        log.info("rmi: %d approved plans for %d/%d", len(plans), gush, helka)
        return plans

    def find_by_number(self, number: str) -> list[Plan]:
        return [p for p in self._search(number=number) if p.number == number]

    def get_documents(self, plan: Plan) -> list[Document]:
        return [d for d in plan.documents if d.source == SOURCE]

    def plan_types(self) -> list[int]:
        # The search form sends every plan type code when the user picks none.
        if self._plan_types is None:
            response = self._http.get(f"{self._api}/SerachPlans/GetPlanTypesList")
            self._plan_types = [int(t["code"]) for t in response.json()]
        return self._plan_types

    def _search(self, gush: str = "", helka: str = "", number: str = "") -> list[Plan]:
        body = {
            "planNumber": number,
            "city": None,
            "gush": gush,
            "chelka": helka,
            "statuses": [],
            "planningCommitte": None,
            "planTypes": self.plan_types(),
            "planTypesUsed": False,
            "fromStatusDate": "",
            "toStatusDate": "",
        }
        response = self._http.post(f"{self._api}/SerachPlans/GetPlans", json=body)
        try:
            payload = response.json()
        except ValueError as exc:
            raise SourceError(f"rmi: not JSON: {response.text[:200]}") from exc
        return [p for p in parse_plans(payload, self._files) if is_approved(p)]


def is_approved(plan: Plan) -> bool:
    return APPROVED_MARKER in plan.status


def parse_plans(payload: dict, files_base: str) -> list[Plan]:
    plans = []
    for row in payload.get("plansSmall") or []:
        number = (row.get("planNumber") or "").strip()
        if not number:
            continue
        plans.append(
            Plan(
                number=number,
                name=(row.get("mahut") or "").strip(),
                status=(row.get("status") or "").strip(),
                source=SOURCE,
                approval_date=parse_short_date(row.get("statusDate") or ""),
                source_id=str(row["planId"]) if row.get("planId") else None,
                documents=parse_documents(row.get("documentsSet") or {}, files_base),
            )
        )
    return plans


def parse_documents(documents_set: dict, files_base: str) -> list[Document]:
    """Instructions, then maps (proposed state first), then appendices. GIS files are skipped."""
    documents = []
    takanon = documents_set.get("takanon")
    if takanon and takanon.get("path"):
        documents.append(_document(takanon, DocumentKind.INSTRUCTIONS, files_base))
    maps = [m for m in documents_set.get("tasritim") or [] if m.get("path")]
    maps.sort(key=lambda m: _map_rank(m.get("info") or ""))
    documents.extend(_document(m, DocumentKind.MAP, files_base) for m in maps)
    documents.extend(
        _document(n, DocumentKind.APPENDIX, files_base)
        for n in documents_set.get("nispachim") or []
        if n.get("path")
    )
    return documents


def file_url(files_base: str, path: str) -> str:
    # Paths come with Windows separators, which the file server answers with 404.
    return files_base.rstrip("/") + quote(path.replace("\\", "/"), safe="/")


def parse_short_date(text: str, today: date | None = None) -> date | None:
    """ "16/12/15" → 2015-12-16, "04/06/59" → 1959-06-04 (two-digit years up to this year)."""
    match = _SHORT_DATE.search(text)
    if not match:
        return None
    day, month, year = (int(g) for g in match.groups())
    if year < 100:
        this_year = (today or date.today()).year
        year += 2000 if 2000 + year <= this_year else 1900
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _document(entry: dict, kind: DocumentKind, files_base: str) -> Document:
    return Document(
        title=(entry.get("info") or kind.value).strip(),
        url=file_url(files_base, entry["path"]),
        source=SOURCE,
        kind=kind,
    )


def _map_rank(info: str) -> int:
    for rank, marker in enumerate(_MAP_ORDER):
        if marker and marker in info:
            return rank
    return _MAP_ORDER.index("")
