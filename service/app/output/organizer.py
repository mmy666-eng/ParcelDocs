"""Output folder layout (PRD §4.5). Every name goes through utils.filenames."""

import json
from datetime import date, datetime
from pathlib import Path

from app.models import Document, DocumentKind, ManifestEntry, Parcel, Plan
from app.utils.filenames import long_path, safe_name

SUMMARY_DIR = "00_סיכום"
PLANS_DIR = "02_תבעות"
LOG_DIR = "99_לוג"
APPENDICES_DIR = "נספחים"

_PLAN_NAME_LENGTH = 40


def project_folder_name(project: str, gush: int, helka: int, day: date) -> str:
    return safe_name(f"{project}_ג{gush}_ח{helka}_{day:%Y-%m-%d}", max_length=120)


class OutputOrganizer:
    def __init__(self, base_dir: Path, project: str, gush: int, helka: int, day: date) -> None:
        self.root = base_dir / project_folder_name(project, gush, helka, day)
        self.summary_dir = self.root / SUMMARY_DIR
        self.plans_dir = self.root / PLANS_DIR
        self.log_dir = self.root / LOG_DIR

    def create(self) -> None:
        for folder in (self.summary_dir, self.plans_dir, self.log_dir):
            long_path(folder).mkdir(parents=True, exist_ok=True)

    @property
    def log_file(self) -> Path:
        return self.log_dir / "run.log"

    def plan_dir(self, plan: Plan) -> Path:
        short_name = safe_name(plan.name, max_length=_PLAN_NAME_LENGTH)
        return self.plans_dir / safe_name(f"{plan.number}_{short_name}", max_length=90)

    def document_paths(self, plan: Plan, documents: list[Document]) -> list[Path]:
        """Target path per document: הוראות.pdf, תשריט.pdf, תשריט_2.pdf, נספחים/<title>.pdf."""
        folder = self.plan_dir(plan)
        counts: dict[DocumentKind, int] = {}
        totals = {kind: sum(1 for d in documents if d.kind == kind) for kind in DocumentKind}
        used: set[str] = set()
        paths = []
        for doc in documents:
            kind = doc.kind or DocumentKind.APPENDIX
            counts[kind] = counts.get(kind, 0) + 1
            if kind == DocumentKind.APPENDIX:
                name = safe_name(f"{doc.title}.pdf", max_length=80)
                stem = name.removesuffix(".pdf")
                suffix = 2
                while name in used:
                    name = f"{stem}_{suffix}.pdf"
                    suffix += 1
                used.add(name)
                paths.append(folder / APPENDICES_DIR / name)
            elif totals[kind] == 1 or counts[kind] == 1:
                paths.append(folder / f"{kind.value}.pdf")
            else:
                paths.append(folder / f"{kind.value}_{counts[kind]}.pdf")
        return paths

    def write_manifest(
        self,
        project: str,
        gush: int,
        helka: int,
        parcels: list[Parcel],
        plans: list[Plan],
        files: list[ManifestEntry],
        warnings: list[str],
    ) -> Path:
        manifest = {
            "project": project,
            "query": {"gush": gush, "helka": helka},
            "created_at": datetime.now().astimezone().isoformat(),
            "parcels": [p.model_dump(mode="json", exclude={"geometry_wkt"}) for p in parcels],
            "plans": [
                p.model_dump(mode="json", exclude={"documents"})
                | {"documents": [d.model_dump(mode="json") for d in p.documents]}
                for p in plans
            ],
            "files": [f.model_dump(mode="json") for f in files],
            "warnings": warnings,
        }
        path = self.summary_dir / "manifest.json"
        long_path(path).write_text(
            json.dumps(manifest, ensure_ascii=False, indent=2), encoding="utf-8"
        )
        return path
