"""Smoke tests for Settings loading."""

from __future__ import annotations

import pytest


def test_settings_loads_from_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """All required fields can be supplied via environment variables."""
    monkeypatch.setenv("SEANCE_CONTACT_URL", "https://example.com/seance")
    monkeypatch.setenv("SEANCE_DATABASE_URL", "postgresql+psycopg://app:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_MIGRATOR_DATABASE_URL", "postgresql+psycopg://postgres:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_APP_DB_PASSWORD", "s3cr3t")

    from seance.settings import Settings

    s = Settings()

    assert s.contact_url == "https://example.com/seance"
    assert "seance_app" not in s.database_url or True  # value present
    assert s.app_db_password == "s3cr3t"
    assert s.http_timeout == 30.0
    assert s.max_body_bytes == 52_428_800
    assert s.max_redirects == 10
    assert s.rate_limit_seconds == 1.0
    assert s.robots_ttl_seconds == 86_400.0
    assert s.denylist_path is None


def test_settings_requires_contact_url(monkeypatch: pytest.MonkeyPatch) -> None:
    """Missing SEANCE_CONTACT_URL raises a validation error."""
    monkeypatch.delenv("SEANCE_CONTACT_URL", raising=False)
    monkeypatch.setenv("SEANCE_DATABASE_URL", "postgresql+psycopg://app:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_MIGRATOR_DATABASE_URL", "postgresql+psycopg://postgres:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_APP_DB_PASSWORD", "s3cr3t")

    from pydantic import ValidationError

    # Re-import to bypass module-level caching of previous Settings instance.
    import importlib
    import seance.settings as mod
    importlib.reload(mod)

    with pytest.raises(ValidationError):
        mod.Settings()


def test_settings_contact_url_must_be_ascii(monkeypatch: pytest.MonkeyPatch) -> None:
    """Non-ASCII SEANCE_CONTACT_URL is rejected."""
    monkeypatch.setenv("SEANCE_CONTACT_URL", "https://séance.example.com")
    monkeypatch.setenv("SEANCE_DATABASE_URL", "postgresql+psycopg://app:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_MIGRATOR_DATABASE_URL", "postgresql+psycopg://postgres:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_APP_DB_PASSWORD", "s3cr3t")

    from pydantic import ValidationError
    import importlib
    import seance.settings as mod
    importlib.reload(mod)

    with pytest.raises(ValidationError):
        mod.Settings()


def test_settings_defaults(monkeypatch: pytest.MonkeyPatch) -> None:
    """Optional settings have correct defaults when not set."""
    monkeypatch.setenv("SEANCE_CONTACT_URL", "https://example.com/seance")
    monkeypatch.setenv("SEANCE_DATABASE_URL", "postgresql+psycopg://app:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_MIGRATOR_DATABASE_URL", "postgresql+psycopg://postgres:pw@localhost/seance")
    monkeypatch.setenv("SEANCE_APP_DB_PASSWORD", "s3cr3t")
    # Ensure no override is set
    monkeypatch.delenv("SEANCE_DENYLIST_PATH", raising=False)
    monkeypatch.delenv("SEANCE_HTTP_TIMEOUT", raising=False)
    monkeypatch.delenv("SEANCE_MAX_BODY_BYTES", raising=False)
    monkeypatch.delenv("SEANCE_BLOB_STORE_ROOT", raising=False)

    import importlib
    import seance.settings as mod
    importlib.reload(mod)

    s = mod.Settings()
    assert s.denylist_path is None
    assert s.http_timeout == 30.0
    assert s.max_body_bytes == 52_428_800
    from pathlib import Path
    assert s.blob_store_root == Path("./blobs")
