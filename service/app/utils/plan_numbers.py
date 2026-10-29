"""Plan numbers: national (501-0873794) and local (בב/105א, מק/105/ה) forms."""

import re

_NATIONAL_NUMBER = re.compile(r"\d{3}-\d{7}")
_CITY_PREFIX = re.compile(r"^בב\s*/\s*")
_NOT_KEY_CHARS = re.compile(r"[\W_]+")


def is_national_number(number: str) -> bool:
    return _NATIONAL_NUMBER.fullmatch(number.strip()) is not None


def national_numbers_in(text: str) -> set[str]:
    return set(_NATIONAL_NUMBER.findall(text))


def local_key(number: str) -> str | None:
    """Comparable form of a local plan number: rmi "בב/105א" and Complot "105/א" → "105א"."""
    if is_national_number(number):
        return None
    return _NOT_KEY_CHARS.sub("", _CITY_PREFIX.sub("", number.strip())) or None
