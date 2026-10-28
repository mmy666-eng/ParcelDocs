from pathlib import Path

from app.utils.filenames import long_path, safe_name


def test_plan_number_slashes_become_dashes():
    assert safe_name("מק/105/פ/1") == "מק-105-פ-1"


def test_hebrew_quote_becomes_gershayim():
    assert safe_name('תמ"א 38') == "תמ״א_38"


def test_invalid_characters_and_spaces():
    assert safe_name('a<b>c:d|e?f*g "h"') == "a_b_c_d_e_f_g_״h״"


def test_trailing_dots_and_spaces_removed():
    assert safe_name("שם. . ") == "שם"


def test_reserved_windows_name():
    assert safe_name("CON") == "_CON"
    assert safe_name("nul.pdf") == "_nul.pdf"


def test_shortening_keeps_extension():
    name = safe_name("א" * 200 + ".pdf", max_length=50)
    assert len(name) == 50
    assert name.endswith(".pdf")


def test_empty_text():
    assert safe_name("  ") == "_"


def test_long_path_is_absolute(tmp_path: Path):
    assert long_path(tmp_path / "x").is_absolute()
