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
    sources: SourcesConfig


def load_config(path: Path | None = None) -> Config:
    path = (path or DEFAULT_CONFIG_PATH).resolve()
    raw = yaml.safe_load(path.read_text(encoding="utf-8"))
    config = Config.model_validate(raw)
    base = path.parent
    config.output_dir = (base / config.output_dir).resolve()
    config.cache_db = (base / config.cache_db).resolve()
    return config
