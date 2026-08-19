"""Testes de resolução de configuração por ambiente e setup de logging."""

from __future__ import annotations

import logging
from pathlib import Path

import pytest

from ultranx.config import (
    DEFAULT_BASE_URL,
    DEFAULT_HTTP_TIMEOUT,
    ENV_BASE_URL,
    ENV_INSECURE_SKIP_HASH,
    ENV_TIMEOUT,
    PRESERVE_DIRS,
    load_settings,
)
from ultranx.logging_setup import configure_logging, log_file_path


@pytest.fixture(autouse=True)
def _clean_env(monkeypatch):
    for name in (ENV_BASE_URL, ENV_TIMEOUT, ENV_INSECURE_SKIP_HASH):
        monkeypatch.delenv(name, raising=False)


def test_defaults_when_env_absent():
    settings = load_settings()
    assert settings.base_url == DEFAULT_BASE_URL
    assert settings.http_timeout == DEFAULT_HTTP_TIMEOUT
    assert not settings.skip_hash_check


def test_base_url_override_strips_trailing_slash(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "https://host/rox/  ")
    settings = load_settings()
    assert settings.base_url == "https://host/rox"
    assert settings.version_url == "https://host/rox/packetVersion.txt"
    assert settings.manifest_url == "https://host/rox/manifest.json"


def test_blank_base_url_falls_back(monkeypatch):
    monkeypatch.setenv(ENV_BASE_URL, "   ")
    assert load_settings().base_url == DEFAULT_BASE_URL


@pytest.mark.parametrize("raw", ["abc", "-5", "0", ""])
def test_invalid_timeout_falls_back(monkeypatch, raw):
    monkeypatch.setenv(ENV_TIMEOUT, raw)
    assert load_settings().http_timeout == DEFAULT_HTTP_TIMEOUT


def test_valid_timeout_is_used(monkeypatch):
    monkeypatch.setenv(ENV_TIMEOUT, "7.5")
    assert load_settings().http_timeout == 7.5


@pytest.mark.parametrize("raw", ["1", "true", "YES", "on"])
def test_skip_hash_flag_truthy(monkeypatch, raw):
    monkeypatch.setenv(ENV_INSECURE_SKIP_HASH, raw)
    assert load_settings().skip_hash_check


@pytest.mark.parametrize("raw", ["0", "false", "no", "qualquer"])
def test_skip_hash_flag_falsy(monkeypatch, raw):
    monkeypatch.setenv(ENV_INSECURE_SKIP_HASH, raw)
    assert not load_settings().skip_hash_check


def test_settings_are_immutable():
    settings = load_settings()
    with pytest.raises(AttributeError):
        settings.base_url = "https://outro"  # type: ignore[misc]


def test_log_copy_dir_is_whitelisted():
    """A pasta de logs no SD precisa estar protegida do sanitizer."""
    from ultranx.core.recovery import LOG_COPY_DIR

    assert LOG_COPY_DIR.casefold() in PRESERVE_DIRS


def test_configure_logging_creates_file(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    target = configure_logging()

    logging.getLogger("ultranx.teste").info("mensagem de verificação")
    for handler in logging.getLogger().handlers:
        handler.flush()

    assert target == log_file_path()
    assert target.exists()
    assert "mensagem de verificação" in target.read_text(encoding="utf-8")


def test_configure_logging_without_stderr_uses_only_file(monkeypatch, tmp_path: Path):
    """Build --noconsole não tem stderr: nenhum StreamHandler deve ser criado."""
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    monkeypatch.setattr("ultranx.logging_setup.sys.stderr", None)

    configure_logging()
    handlers = logging.getLogger().handlers

    assert len(handlers) == 1
    assert isinstance(handlers[0], logging.FileHandler)
    logging.getLogger("ultranx.teste").info("sem stderr")  # não deve levantar


def test_ensure_std_streams_replaces_none(monkeypatch):
    """Sem esta guarda, --version travaria num diálogo de traceback do Qt."""
    import sys as _sys

    from ultranx.__main__ import ensure_std_streams

    monkeypatch.setattr("sys.stdout", None)
    monkeypatch.setattr("sys.stderr", None)

    ensure_std_streams()

    assert _sys.stdout is not None
    assert _sys.stderr is not None
    _sys.stdout.write("descartado")  # não deve levantar
    _sys.stderr.write("descartado")


def test_ensure_std_streams_keeps_existing():
    import sys as _sys

    from ultranx.__main__ import ensure_std_streams

    original = _sys.stdout
    ensure_std_streams()
    assert _sys.stdout is original


def test_configure_logging_is_idempotent(monkeypatch, tmp_path: Path):
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    configure_logging()
    first = len(logging.getLogger().handlers)
    configure_logging(verbose=True)

    assert len(logging.getLogger().handlers) == first
    assert logging.getLogger().level == logging.DEBUG
