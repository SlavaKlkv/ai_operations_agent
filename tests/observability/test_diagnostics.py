"""Диагностический отчёт не должен переносить секреты."""

from __future__ import annotations

import json

from app.core.config import Settings
from app.observability.diagnostics import MASK, build_report, redact


def _settings(**overrides) -> Settings:
    return Settings(_env_file=None, **overrides)  # type: ignore[call-arg]


def test_github_tokens_are_masked():
    token = "ghp_" + "A" * 36

    redacted = redact(f"используется {token} для доступа")

    assert token not in redacted
    assert MASK in redacted


def test_bearer_and_client_secret_are_masked():
    assert "abcdef1234567890" not in redact("Authorization: Bearer abcdef1234567890")
    assert "super-secret-value" not in redact("client_secret: super-secret-value")


def test_private_key_blocks_are_masked():
    block = "-----BEGIN RSA PRIVATE KEY-----\nMIIEexample\n-----END RSA PRIVATE KEY-----"

    redacted = redact(block)

    assert "MIIEexample" not in redacted


def test_url_credentials_are_masked_but_host_is_kept():
    redacted = redact("postgresql+asyncpg://agent:pw-1234567890@localhost:5432/db")

    assert "pw-1234567890" not in redacted
    assert "@localhost:5432/db" in redacted
    assert MASK in redacted


def test_ordinary_text_is_untouched():
    text = "storage=sqlite mcp_enabled=true model=qwen3:8b"

    assert redact(text) == text


def test_report_hides_the_database_password():
    report = build_report(_settings(storage_backend="postgres", postgres_password="pw-1234567890"))

    dumped = json.dumps(report, ensure_ascii=False)
    assert "pw-1234567890" not in dumped
    assert MASK in dumped


def test_report_for_sqlite_has_no_secrets(tmp_path):
    report = build_report(_settings(sqlite_path=tmp_path / "agent.db"))

    assert report["storage_backend"] == "sqlite"
    assert report["cache_backend"] == "memory"
    assert MASK not in json.dumps(report, ensure_ascii=False)
