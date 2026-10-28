from datetime import date
from pathlib import Path

from app.models import Document, DocumentKind, Plan
from app.output.organizer import OutputOrganizer, project_folder_name


def test_project_folder_name():
    assert (
        project_folder_name("בדיקה", 6123, 2518, date(2026, 10, 28))
        == "בדיקה_ג6123_ח2518_2026-10-28"
    )


def test_document_paths(tmp_path: Path):
    organizer = OutputOrganizer(tmp_path, "פ", 6123, 2518, date(2026, 10, 28))
    plan = Plan(number="מק/105/פ/1", name='תיקון תמ"א', status="בתוקף", source="bnei_brak")
    docs = [
        Document(title="תקנון לתוקף", url="1", source="s", kind=DocumentKind.INSTRUCTIONS),
        Document(title="מצב מוצע לתוקף", url="2", source="s", kind=DocumentKind.MAP),
        Document(title="בינוי לתוקף", url="3", source="s", kind=DocumentKind.MAP),
        Document(title="נספח תנועה", url="4", source="s", kind=DocumentKind.APPENDIX),
        Document(title="נספח תנועה", url="5", source="s", kind=DocumentKind.APPENDIX),
    ]

    paths = organizer.document_paths(plan, docs)

    plan_dir = tmp_path / "פ_ג6123_ח2518_2026-10-28" / "02_תבעות" / "מק-105-פ-1_תיקון_תמ״א"
    assert paths == [
        plan_dir / "הוראות.pdf",
        plan_dir / "תשריט.pdf",
        plan_dir / "תשריט_2.pdf",
        plan_dir / "נספחים" / "נספח_תנועה.pdf",
        plan_dir / "נספחים" / "נספח_תנועה_2.pdf",
    ]
