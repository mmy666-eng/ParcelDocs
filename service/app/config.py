"""Loading of config.yaml."""

from pathlib import Path

import yaml
from pydantic import BaseModel

DEFAULT_CONFIG_PATH = Path(__file__).resolve().parent.parent / "config.yaml"


class SourcesConfig(BaseModel):
    govmap_wfs: str
    iplan_plans_layer: str
    complot_base: str
    complot_site_id: int
    complot_ws: str | None = None
    rmi_api: str = "https://apps.land.gov.il/TabaSearch/api"
    rmi_files: str = "https://apps.land.gov.il"


class Config(BaseModel):
    output_dir: Path
    cache_db: Path
    user_agent: str
    requests_per_second: float = 1.0
    timeout_seconds: float = 60
    retries: int = 3
    legacy_tls_hosts: list[str] = []
    host_min_interval_seconds: dict[str, float] = {}
    rate_limited_wait_seconds: float = 60.0
    host_max_requests_per_run: dict[str, int] = {}
    fail_fast_429_hosts: list[str] = []
    parcel_buffer_m: float = -0.5
    citywide_plans: list[int] = []
    disabled_sources: list[str] = []
    # Shared office folder of plan documents, checked before any source; None = off.
    plans_library: Path | None = None
    # Local copy used while the shared folder cannot be reached (default: next to cache_db).
    plans_library_local: Path | None = None
    # This computer's index of the library (SQLite); rebuilt from plan.json when deleted.
    library_index: Path | None = None
    # A plan whose documents are not complete in the library is listed again in the city
    # system (Complot GetTabaFile) only after this many days.
    library_recheck_days: int = 30
    # A plan whose boundary covers at least this share of the municipality is city-wide.
    citywide_min_share: float = 0.5
    # Where the browser saves files the user downloads from mavat; None = ~/Downloads.
    downloads_dir: Path | None = None
    # Move filed downloads into a subfolder of downloads_dir instead of leaving them.
    move_filed_downloads: bool = True
    sources: SourcesConfig


def load_config(path: Path | None = None) -> Config:
    path = (path or DEFAULT_CONFIG_PATH).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = Config.model_validate(raw)
    base = path.parent
    config.output_dir = _resolve(base, config.output_dir)
    config.cache_db = _resolve(base, config.cache_db)
    if config.plans_library is not None:
        config.plans_library = _resolve(base, config.plans_library)
    config.plans_library_local = _resolve(
        base, config.plans_library_local or config.cache_db.parent / "library_local"
    )
    config.library_index = _resolve(
        base, config.library_index or config.cache_db.parent / "library_index.sqlite"
    )
    if config.downloads_dir is not None:
        config.downloads_dir = _resolve(base, config.downloads_dir.expanduser())
    return config


def _resolve(base: Path, path: Path) -> Path:
    # An absolute path (such as //server/share) is kept as written: resolving it would
    # touch the network, which blocks while the server cannot be reached.
    return path if path.is_absolute() else (base / path).resolve()


def downloads_dir(config: Config) -> Path:
    return config.downloads_dir or Path.home() / "Downloads"
