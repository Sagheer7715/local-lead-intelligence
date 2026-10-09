"""Configuration, from the environment and an optional .env file.

The kit this replaces shipped a `.env.example` that nothing ever read: both its
scripts went straight to `os.environ`, and its compose file had no `env_file`
and no variable substitution. Setting `SCRAPER_API_KEY` there silently did
nothing. `load_env()` is the fix, and every entry point calls it once at start.
"""
from __future__ import annotations

import os
import stat
from dataclasses import dataclass, field
from pathlib import Path

PROJECT_ROOT = Path(__file__).resolve().parent.parent

# Nominatim and Overpass are free, shared, donation-funded infrastructure. Their
# usage policies require a descriptive User-Agent with a way to reach the
# operator. Put a real contact in CONTACT_URL before running at any volume.
DEFAULT_UA = "local-lead-intelligence/0.1 (+{contact})"


class ConfigError(Exception):
    pass


def _unquote(value: str) -> str:
    if len(value) >= 2 and value[0] == value[-1] and value[0] in "\"'":
        return value[1:-1]
    return value


def load_env(path=None, *, override=False) -> dict:
    """Load KEY=VALUE pairs from a .env file into os.environ.

    Returns the keys it set. Existing environment variables win unless
    `override=True`, so an explicit `FOO=bar python3 -m ...` always beats the
    file. Values are not shell-expanded and inline comments are not stripped:
    quote anything containing a `#`.
    """
    env_path = Path(path) if path else PROJECT_ROOT / ".env"
    if not env_path.exists():
        return {}

    # A .env may hold a token. Refuse to read one that the rest of the machine
    # can read too, rather than loading it and hoping.
    mode = env_path.stat().st_mode
    if mode & (stat.S_IRGRP | stat.S_IROTH):
        raise ConfigError(
            f"{env_path} is group/world-readable. Run: chmod 600 {env_path}")

    loaded = {}
    for lineno, raw in enumerate(env_path.read_text(encoding="utf-8").splitlines(), 1):
        line = raw.strip()
        if not line or line.startswith("#"):
            continue
        if line.startswith("export "):
            line = line[len("export "):].lstrip()
        if "=" not in line:
            raise ConfigError(f"{env_path}:{lineno}: expected KEY=VALUE, got {raw!r}")
        key, _, value = line.partition("=")
        key = key.strip()
        if not key:
            raise ConfigError(f"{env_path}:{lineno}: empty key")
        if override or key not in os.environ:
            os.environ[key] = _unquote(value.strip())
            loaded[key] = os.environ[key]
    return loaded


def _env_int(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return int(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be an integer, got {raw!r}") from e


def _env_float(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    try:
        return float(raw)
    except ValueError as e:
        raise ConfigError(f"{name} must be a number, got {raw!r}") from e


def _env_bool(name, default):
    raw = os.environ.get(name)
    if raw is None or raw == "":
        return default
    return raw.strip().lower() in ("1", "true", "yes", "on")


@dataclass
class Config:
    data_dir: Path = field(default_factory=lambda: PROJECT_ROOT / "data")
    db_path: Path = field(default_factory=lambda: PROJECT_ROOT / "data" / "leads.db")
    contact_url: str = "https://example.invalid/bot"

    # Tier 1 — discovery (free, open data, no key)
    overpass_url: str = "https://overpass-api.de/api/interpreter"
    nominatim_url: str = "https://nominatim.openstreetmap.org/search"

    # Tier 2 — enrichment crawl
    http_timeout: float = 10.0
    http_max_bytes: int = 400_000
    max_workers: int = 6
    per_host_delay: float = 1.0
    cache_ttl_days: int = 30
    respect_robots: bool = True

    # Tier 3 — optional Google Maps pass (off unless explicitly enabled)
    tier3_enabled: bool = False
    scraper_base_url: str = "http://127.0.0.1:8080"
    scraper_api_key: str = ""

    # Data retention. Scraped phones and emails are personal data.
    retention_days: int = 90

    @property
    def user_agent(self) -> str:
        return DEFAULT_UA.format(contact=self.contact_url)

    def __repr__(self) -> str:
        # Never let a token reach a log, a traceback or a terminal.
        key = "<set>" if self.scraper_api_key else "<unset>"
        return (f"Config(db_path={self.db_path}, contact_url={self.contact_url!r}, "
                f"tier3_enabled={self.tier3_enabled}, scraper_api_key={key})")


def load(path=None) -> Config:
    """Load .env, then build a Config from the environment."""
    load_env(path)
    data_dir = Path(os.environ.get("LLI_DATA_DIR") or (PROJECT_ROOT / "data"))
    cfg = Config(
        data_dir=data_dir,
        db_path=Path(os.environ.get("LLI_DB_PATH") or (data_dir / "leads.db")),
        contact_url=os.environ.get("LLI_CONTACT_URL") or Config.contact_url,
        overpass_url=os.environ.get("LLI_OVERPASS_URL") or Config.overpass_url,
        nominatim_url=os.environ.get("LLI_NOMINATIM_URL") or Config.nominatim_url,
        http_timeout=_env_float("LLI_HTTP_TIMEOUT", 10.0),
        http_max_bytes=_env_int("LLI_HTTP_MAX_BYTES", 400_000),
        max_workers=_env_int("LLI_MAX_WORKERS", 6),
        per_host_delay=_env_float("LLI_PER_HOST_DELAY", 1.0),
        cache_ttl_days=_env_int("LLI_CACHE_TTL_DAYS", 30),
        respect_robots=_env_bool("LLI_RESPECT_ROBOTS", True),
        tier3_enabled=_env_bool("LLI_TIER3_ENABLED", False),
        scraper_base_url=os.environ.get("SCRAPER_BASE_URL") or Config.scraper_base_url,
        scraper_api_key=os.environ.get("SCRAPER_API_KEY") or "",
        retention_days=_env_int("LLI_RETENTION_DAYS", 90),
    )
    if cfg.max_workers < 1:
        raise ConfigError("LLI_MAX_WORKERS must be >= 1")
    cfg.data_dir.mkdir(parents=True, exist_ok=True)
    return cfg
