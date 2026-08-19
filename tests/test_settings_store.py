"""Testes da configuração do servidor: arquivo, ambiente e precedência.

O binário é distribuído sem URL embutida, então esta resolução é o que separa
"app utilizável pela comunidade" de "app que só funciona na máquina de quem
compilou".
"""

from __future__ import annotations

import json
from pathlib import Path

import pytest

from ultranx import config
from ultranx.config import (
    DEFAULT_BASE_URL,
    ENV_BASE_URL,
    ENV_TIMEOUT,
    is_placeholder_url,
    load_settings,
    normalize_base_url,
    read_config_file,
    save_base_url,
)


@pytest.fixture(autouse=True)
def _isolated(monkeypatch, tmp_path: Path):
    """Isola HOME e a pasta do executável para não tocar na config real."""
    monkeypatch.delenv(ENV_BASE_URL, raising=False)
    monkeypatch.delenv(ENV_TIMEOUT, raising=False)
    home = tmp_path / "home"
    home.mkdir()
    app_dir = tmp_path / "app"
    app_dir.mkdir()
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: home))
    monkeypatch.setattr(config, "app_directory", lambda: app_dir)
    return home, app_dir


# --- normalização -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("https://host/rox", "https://host/rox"),
        ("  https://host/rox/  ", "https://host/rox"),
        ("host.exemplo/rox", "https://host.exemplo/rox"),  # assume https
        ("http://host.exemplo", "http://host.exemplo"),
        # Nome sem ponto vale: servidor de rede local é caso real.
        ("http://servidor/rox", "http://servidor/rox"),
    ],
)
def test_normalize_base_url(raw, expected):
    assert normalize_base_url(raw) == expected


@pytest.mark.parametrize("raw", ["", "   ", "ftp://host/rox", "https://", "http:///rox"])
def test_normalize_base_url_rejects_invalid(raw):
    with pytest.raises(ValueError):
        normalize_base_url(raw)


def test_placeholder_detection():
    assert is_placeholder_url(DEFAULT_BASE_URL)
    assert not is_placeholder_url("https://host/rox")


# --- leitura do arquivo -----------------------------------------------------


def test_no_config_file_means_placeholder():
    settings = load_settings()
    assert settings.base_url == DEFAULT_BASE_URL
    assert not settings.is_configured


def test_user_config_file_is_used(_isolated):
    home, _ = _isolated
    target = home / ".ultranx" / "ultranx.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"base_url": "https://host/rox"}), encoding="utf-8")

    settings = load_settings()

    assert settings.base_url == "https://host/rox"
    assert settings.is_configured
    assert settings.version_url == "https://host/rox/packetVersion.txt"


def test_file_next_to_executable_wins_over_user_profile(_isolated):
    """Permite distribuir o binário já apontado, em pendrive por exemplo."""
    home, app_dir = _isolated
    (home / ".ultranx").mkdir(parents=True)
    (home / ".ultranx" / "ultranx.json").write_text(
        json.dumps({"base_url": "https://perfil/rox"}), encoding="utf-8"
    )
    (app_dir / "ultranx.json").write_text(
        json.dumps({"base_url": "https://portatil/rox"}), encoding="utf-8"
    )

    assert load_settings().base_url == "https://portatil/rox"


def test_environment_wins_over_file(monkeypatch, _isolated):
    home, _ = _isolated
    (home / ".ultranx").mkdir(parents=True)
    (home / ".ultranx" / "ultranx.json").write_text(
        json.dumps({"base_url": "https://arquivo/rox"}), encoding="utf-8"
    )
    monkeypatch.setenv(ENV_BASE_URL, "https://ambiente/rox")

    assert load_settings().base_url == "https://ambiente/rox"


def test_timeout_from_file(_isolated):
    home, _ = _isolated
    (home / ".ultranx").mkdir(parents=True)
    (home / ".ultranx" / "ultranx.json").write_text(
        json.dumps({"base_url": "https://host/rox", "http_timeout": 45}),
        encoding="utf-8",
    )
    assert load_settings().http_timeout == 45.0


def test_corrupt_config_degrades_to_placeholder(_isolated):
    """Configuração corrompida não pode impedir o app de abrir."""
    home, _ = _isolated
    (home / ".ultranx").mkdir(parents=True)
    (home / ".ultranx" / "ultranx.json").write_text("{ isso não é json", encoding="utf-8")

    assert read_config_file() == {}
    assert load_settings().base_url == DEFAULT_BASE_URL


def test_config_that_is_not_an_object_is_ignored(_isolated):
    home, _ = _isolated
    (home / ".ultranx").mkdir(parents=True)
    (home / ".ultranx" / "ultranx.json").write_text('["lista"]', encoding="utf-8")
    assert read_config_file() == {}


# --- gravação ---------------------------------------------------------------


def test_save_base_url_persists_and_is_reloaded(_isolated):
    target = save_base_url("host.exemplo/rox/")

    assert target.exists()
    assert json.loads(target.read_text(encoding="utf-8"))["base_url"] == (
        "https://host.exemplo/rox"
    )
    assert load_settings().base_url == "https://host.exemplo/rox"


def test_save_base_url_preserves_other_keys(_isolated):
    home, _ = _isolated
    target = home / ".ultranx" / "ultranx.json"
    target.parent.mkdir(parents=True)
    target.write_text(json.dumps({"http_timeout": 60}), encoding="utf-8")

    save_base_url("https://host/rox")

    document = json.loads(target.read_text(encoding="utf-8"))
    assert document["http_timeout"] == 60
    assert document["base_url"] == "https://host/rox"


def test_save_base_url_rejects_invalid_without_writing(_isolated):
    home, _ = _isolated
    with pytest.raises(ValueError):
        save_base_url("não é url")
    assert not (home / ".ultranx" / "ultranx.json").exists()


def test_save_creates_directory_when_missing(_isolated):
    home, _ = _isolated
    assert not (home / ".ultranx").exists()
    save_base_url("https://host/rox")
    assert (home / ".ultranx" / "ultranx.json").is_file()
