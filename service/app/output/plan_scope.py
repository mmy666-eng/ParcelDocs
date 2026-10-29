"""plan_scope: whether a plan covers one area, a whole city, or is a national/district plan.

Decided once per plan and kept in the office library. A manual correction in
overrides.yaml (library root) wins over the rules, and applies in every run in that
city. A plan without a boundary is "specific" by default and is classified again when
a boundary arrives; every other decision is final.
"""

import re
from enum import StrEnum

from pydantic import BaseModel
from shapely.geometry.base import BaseGeometry

from app.models import Plan, PlanScope
from app.utils.filenames import safe_name
from app.utils.plan_numbers import is_national_number, local_key

NATIONAL_FOLDER = "ארציות"
UNKNOWN_CITY_FOLDER = "ללא_עיר"
# iplan entity_subtype_desc of national and district outline plans.
NATIONAL_TYPES = ("תכנית מתאר ארצית", "תכנית מתאר מחוזית")
NATIONAL_AUTHORITY = 1
# תמא/ 60, תמ"א 38, תממ/5, תמ"מ 5 – but not תמל (a site plan of the national committee).
_NATIONAL_PREFIX = re.compile(r'^\s*תמ"?[אמ](?![֐-׿])')
_CITY_SEPARATORS = re.compile(r"[\s\-–־_]+")

SCOPE_NAMES = {
    "ספציפית": PlanScope.SPECIFIC,
    "כלל-עירונית": PlanScope.CITYWIDE,
    "כלל עירונית": PlanScope.CITYWIDE,
    "ארצית": PlanScope.NATIONAL,
    "specific": PlanScope.SPECIFIC,
    "citywide": PlanScope.CITYWIDE,
    "national": PlanScope.NATIONAL,
}


class ScopeBasis(StrEnum):
    MANUAL = "manual"
    AUTHORITY = "authority"
    NUMBER = "number"
    CONFIG = "config"
    AREA = "area"
    # No boundary yet: "specific" until one arrives.
    DEFAULT = "default"


class ScopeDecision(BaseModel):
    scope: PlanScope
    basis: ScopeBasis
    note: str


def parse_scope(text: str) -> PlanScope:
    try:
        return SCOPE_NAMES[text.strip().lower()]
    except KeyError:
        names = " / ".join(s.value for s in PlanScope)
        raise ValueError(f"סיווג לא מוכר: {text!r} (אפשר: {names})") from None


def city_key(city: str | None) -> str:
    """Folder name of a city, also used to compare city names from different sources:
    "בני ברק" → "בני_ברק", "תל אביב -יפו" and "תל אביב-יפו" → "תל_אביב_יפו"."""
    if not city or not city.strip():
        return ""
    return safe_name(_CITY_SEPARATORS.sub("_", city.strip()).strip("_"), max_length=60)


def city_folder(city: str | None) -> str:
    return city_key(city) or UNKNOWN_CITY_FOLDER


def has_national_number_form(number: str) -> bool:
    return _NATIONAL_PREFIX.match(number) is not None


def identity_keys(
    numbers: list[str],
    city: str | None,
    national_scope: bool,
    source_ids: dict[str, str] | None = None,
) -> set[str]:
    """Keys that identify a plan across sources and runs.

    National numbers are unique in the country; local numbers only within a city, so
    their key includes the city (national plans keep theirs in a national space).
    """
    space = "" if national_scope else city_key(city)
    keys: set[str] = set()
    for number in numbers:
        number = number.strip()
        if not number:
            continue
        if is_national_number(number):
            keys.add(f"n:{number}")
        elif key := local_key(number):
            keys.add(f"l:{space}:{key}")
    for source, source_id in (source_ids or {}).items():
        keys.add(f"s:{source}:{source_id}")
    return keys


def lookup_keys(plan: Plan) -> set[str]:
    """Keys to find a plan from a source in the library: its city's keys, and the national
    space in case the library already holds it as a national plan."""
    numbers = [plan.number, *plan.aliases]
    return identity_keys(numbers, plan.city, False, plan.source_ids) | identity_keys(
        numbers, None, True
    )


def classify_scope(
    plan: Plan,
    boundary: BaseGeometry | None,
    city_boundary: BaseGeometry | None,
    min_share: float,
    citywide_ids: set[str] | None = None,
) -> ScopeDecision:
    """The automatic rules, in order: national authority or type, national number form,
    city-wide plans listed in config, share of the municipal area covered, default."""
    if plan.authority_level == NATIONAL_AUTHORITY or plan.plan_type in NATIONAL_TYPES:
        return ScopeDecision(
            scope=PlanScope.NATIONAL,
            basis=ScopeBasis.AUTHORITY,
            note=f"iplan: {plan.plan_type or 'סמכות ארצית'}",
        )
    if any(has_national_number_form(n) for n in [plan.number, *plan.aliases]):
        return ScopeDecision(
            scope=PlanScope.NATIONAL, basis=ScopeBasis.NUMBER, note=f"מספר {plan.number}"
        )
    city_id = plan.source_ids.get("bnei_brak")
    if city_id and city_id in (citywide_ids or set()):
        return ScopeDecision(
            scope=PlanScope.CITYWIDE, basis=ScopeBasis.CONFIG, note="citywide_plans ב-config"
        )
    if boundary is not None and city_boundary is not None and city_boundary.area > 0:
        share = boundary.intersection(city_boundary).area / city_boundary.area
        scope = PlanScope.CITYWIDE if share >= min_share else PlanScope.SPECIFIC
        return ScopeDecision(
            scope=scope, basis=ScopeBasis.AREA, note=f"הגבול מכסה {share:.0%} משטח הרשות"
        )
    return ScopeDecision(
        scope=PlanScope.SPECIFIC, basis=ScopeBasis.DEFAULT, note="אין גבול תכנית – ברירת מחדל"
    )


class Overrides:
    """Manual plan_scope corrections: {city: {plan number or alias: scope}}."""

    def __init__(self, raw: dict | None = None) -> None:
        self.raw: dict[str, dict[str, str]] = {}
        self._by_key: dict[str, PlanScope] = {}
        for city, plans in (raw or {}).items():
            for number, scope in (plans or {}).items():
                self.add(str(city), str(number), parse_scope(str(scope)))

    def add(self, city: str, number: str, scope: PlanScope) -> None:
        self.raw.setdefault(city, {})[number] = scope.value
        for key in self.keys(city, number, scope):
            self._by_key[key] = scope

    def lookup(self, keys: set[str]) -> PlanScope | None:
        for key in sorted(keys):
            if key in self._by_key and not key.startswith("s:"):
                return self._by_key[key]
        return None

    def items(self) -> list[tuple[str, str, PlanScope]]:
        return [
            (city, number, parse_scope(scope))
            for city, plans in self.raw.items()
            for number, scope in plans.items()
        ]

    @staticmethod
    def keys(city: str, number: str, scope: PlanScope | None = None) -> set[str]:
        """The city's key; national plans (by number form, or set to national) also keep
        theirs in the national space."""
        keys = identity_keys([number], city, False)
        if scope == PlanScope.NATIONAL or has_national_number_form(number):
            keys |= identity_keys([number], None, True)
        return keys
