"""
Séance application settings.

All values are read from environment variables or a .env file.
Required fields have no default and will raise a validation error if absent.
"""

from __future__ import annotations

from pathlib import Path

from pydantic import field_validator
from pydantic_settings import BaseSettings, SettingsConfigDict


class Settings(BaseSettings):
    model_config = SettingsConfigDict(
        env_prefix="SEANCE_",
        env_file=".env",
        env_file_encoding="utf-8",
        case_sensitive=False,
    )

    # ------------------------------------------------------------------ #
    # Identity
    # ------------------------------------------------------------------ #
    contact_url: str
    """Inserted into the User-Agent header: Seance/0.1 (+<contact_url>)"""

    # ------------------------------------------------------------------ #
    # Storage
    # ------------------------------------------------------------------ #
    blob_store_root: Path = Path("./blobs")
    """Root directory for the content-addressed blob store."""

    # ------------------------------------------------------------------ #
    # Database
    # ------------------------------------------------------------------ #
    database_url: str
    """SQLAlchemy sync DSN for the seance_app role (psycopg driver).
    Example: postgresql+psycopg://seance_app:pw@localhost/seance"""

    migrator_database_url: str
    """SQLAlchemy sync DSN for the privileged migrator role (runs Alembic).
    Example: postgresql+psycopg://postgres:pw@localhost/seance"""

    app_db_password: str
    """Password used when the migration creates or updates the seance_app role."""

    # ------------------------------------------------------------------ #
    # HTTP adapter
    # ------------------------------------------------------------------ #
    http_timeout: float = 30.0
    """Per-hop connect/read timeout AND total per-request deadline (seconds)."""

    max_body_bytes: int = 52_428_800  # 50 MiB
    """Maximum decompressed response body size in bytes."""

    max_redirects: int = 10
    """Maximum number of redirects to follow per request."""

    # ------------------------------------------------------------------ #
    # Rate limiting
    # ------------------------------------------------------------------ #
    rate_limit_seconds: float = 1.0
    """Minimum interval between consecutive requests to the same host (seconds)."""

    # ------------------------------------------------------------------ #
    # robots.txt
    # ------------------------------------------------------------------ #
    robots_ttl_seconds: float = 86_400.0
    """Per-host robots.txt cache TTL (seconds). Default: 24 hours."""

    # ------------------------------------------------------------------ #
    # Normalizer
    # ------------------------------------------------------------------ #
    denylist_path: Path | None = None
    """Override path to denylist YAML file.
    When None, the bundled seance/resources/denylist.yaml is loaded via
    importlib.resources."""

    # ------------------------------------------------------------------ #
    # Validators
    # ------------------------------------------------------------------ #
    @field_validator("contact_url")
    @classmethod
    def contact_url_must_be_ascii(cls, v: str) -> str:
        try:
            v.encode("ascii")
        except UnicodeEncodeError as exc:
            raise ValueError(
                "SEANCE_CONTACT_URL must be pure ASCII (used in HTTP User-Agent)"
            ) from exc
        return v
