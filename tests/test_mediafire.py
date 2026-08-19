"""Testes do cliente MediaFire e do provedor de versão, com HTTP mockado.

Nenhum teste toca a rede. A raspagem do link é a parte que vai quebrar quando o
site mudar, então precisa de cobertura que prove também a falha explícita, não
só o caminho felizmente.
"""

from __future__ import annotations

import base64
import json
from datetime import date
from pathlib import Path

import pytest
import requests

from ultranx.config import MODALITY_FULL, MODALITY_STANDARD, Settings
from ultranx.core import mediafire, version_inspector
from ultranx.core.errors import NetworkError, RemoteDataError
from ultranx.core.mediafire import (
    is_mediafire_url,
    list_folder,
    parse_folder_key,
    resolve_download_url,
)

PASTA = "5zz3azv8dk409"
SUB = "k8hlembs1106z"
QUICK_BASE = "kdfcc792hlkm1nf"
QUICK_EXTRA = "2zqyfflra4m0j0j"
HASH_BASE = "8ce3cab1a54c3e9f201d86823d511808eb3d3a96525541801aacc62bbcbee13d"
HASH_EXTRA = "b" * 64
LINK = "https://download1580.mediafire.com/abc123/kdfcc792hlkm1nf/UltraNX.7z"


@pytest.fixture()
def settings() -> Settings:
    return Settings(
        base_url=f"https://www.mediafire.com/folder/{PASTA}/Nintendo+Switch",
        http_timeout=5.0,
        skip_hash_check=False,
    )


class _Reply:
    def __init__(self, payload: object = None, text: str = "", status: int = 200):
        self.text = json.dumps(payload) if payload is not None else text
        self.content = self.text.encode("utf-8")
        self.status_code = status
        self.headers: dict[str, str] = {}

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            error.response = self  # type: ignore[attr-defined]
            raise error

    def json(self):
        return json.loads(self.text)


def _arquivo(nome: str, quickkey: str, size: int, criado: str, hash_: str) -> dict:
    return {
        "filename": nome,
        "quickkey": quickkey,
        "size": str(size),
        "created": criado,
        "hash": hash_,
    }


def _sucesso(corpo: dict) -> dict:
    return {"response": {"result": "Success", **corpo}}


_BASE = _arquivo("UltraNX.7z", QUICK_BASE, 2068985680, "2026-08-19 05:21:22", HASH_BASE)
_TEXTO = _arquivo("leia-me.txt", "aaaaaaaaaaa", 10, "2026-08-19 05:21:22", "")
_EXTRA = _arquivo(
    "Android.7z", QUICK_EXTRA, 15204765696, "2026-08-11 18:34:47", HASH_EXTRA
)


def _rotas(monkeypatch, *, html: str = f'<a href="{LINK}">baixar</a>') -> None:
    """Mapeia as URLs da API e da página do arquivo para respostas fixas."""

    def fake_get(url, params=None, timeout=None, headers=None, **kwargs):  # noqa: ANN001, ARG001
        params = params or {}
        if "folder/get_content.php" in url:
            chave, tipo = params.get("folder_key"), params.get("content_type")
            if chave == PASTA and tipo == "files":
                return _Reply(_sucesso({"folder_content": {"files": [_BASE, _TEXTO]}}))
            if chave == PASTA and tipo == "folders":
                return _Reply(
                    _sucesso(
                        {
                            "folder_content": {
                                "folders": [{"name": "Extra Opcional", "folderkey": SUB}]
                            }
                        }
                    )
                )
            if chave == SUB and tipo == "files":
                return _Reply(_sucesso({"folder_content": {"files": [_EXTRA]}}))
            return _Reply(_sucesso({"folder_content": {}}))

        if "file/get_info.php" in url:
            quick = params.get("quick_key")
            info = _BASE if quick == QUICK_BASE else _EXTRA
            return _Reply(_sucesso({"file_info": info}))

        if "mediafire.com/file/" in url:
            return _Reply(text=html)

        raise AssertionError(f"URL inesperada: {url}")

    monkeypatch.setattr(mediafire.requests, "get", fake_get)


# --- reconhecimento de URL --------------------------------------------------


@pytest.mark.parametrize(
    "url",
    [
        "https://www.mediafire.com/folder/5zz3azv8dk409/Nintendo+Switch",
        "http://mediafire.com/folder/5zz3azv8dk409",
        "https://MEDIAFIRE.com/folder/5zz3azv8dk409/x",
    ],
)
def test_parse_folder_key(url):
    assert parse_folder_key(url) == PASTA


def test_parse_bare_key():
    assert parse_folder_key(PASTA) == PASTA


def test_parse_rejects_other_urls():
    with pytest.raises(RemoteDataError):
        parse_folder_key("https://exemplo.org/pacotes/")


def test_is_mediafire_url():
    assert is_mediafire_url("https://www.mediafire.com/folder/x/y")
    assert not is_mediafire_url("https://exemplo.org/ultranx")


# --- listagem ---------------------------------------------------------------


def test_list_folder_parses_files_and_subfolders(monkeypatch, settings):
    _rotas(monkeypatch)
    pasta = list_folder(PASTA, settings)

    assert [f.filename for f in pasta.files] == ["UltraNX.7z", "leia-me.txt"]
    principal = pasta.files[0]
    assert principal.quickkey == QUICK_BASE
    assert principal.size_bytes == 2068985680
    assert principal.sha256 == HASH_BASE
    assert principal.created == date(2026, 8, 19)
    assert pasta.subfolders == (("Extra Opcional", SUB),)


def test_entry_without_hash_keeps_none(monkeypatch, settings):
    _rotas(monkeypatch)
    assert list_folder(PASTA, settings).files[1].sha256 is None


def test_api_failure_becomes_remote_data_error(monkeypatch, settings):
    def fake_get(url, params=None, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        return _Reply({"response": {"result": "Error", "message": "chave inválida"}})

    monkeypatch.setattr(mediafire.requests, "get", fake_get)
    with pytest.raises(RemoteDataError, match="chave inválida"):
        list_folder(PASTA, settings)


def test_network_failure_becomes_network_error(monkeypatch, settings):
    def fake_get(url, params=None, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        raise requests.exceptions.ConnectionError("sem rede")

    monkeypatch.setattr(mediafire.requests, "get", fake_get)
    with pytest.raises(NetworkError):
        list_folder(PASTA, settings)


# --- raspagem do link -------------------------------------------------------


def test_resolve_download_url_finds_href(monkeypatch, settings):
    _rotas(monkeypatch)
    assert resolve_download_url(QUICK_BASE, settings) == LINK


def test_resolve_download_url_accepts_scrambled(monkeypatch, settings):
    embaralhado = base64.b64encode(LINK.encode("utf-8")).decode("ascii")
    _rotas(monkeypatch, html=f'<a data-scrambled-url="{embaralhado}">x</a>')
    assert resolve_download_url(QUICK_BASE, settings) == LINK


def test_resolve_download_url_explains_layout_change(monkeypatch, settings):
    """Quando o MediaFire mudar o HTML, o erro precisa dizer onde consertar."""
    _rotas(monkeypatch, html="<html><body>sem link nenhum</body></html>")
    with pytest.raises(RemoteDataError, match="mudou de layout"):
        resolve_download_url(QUICK_BASE, settings)


# --- provedor de versão -----------------------------------------------------


def test_inspect_uses_mediafire_when_url_matches(monkeypatch, settings, tmp_path: Path):
    _rotas(monkeypatch)

    report = version_inspector.inspect(tmp_path, settings)

    # Versão é a data do arquivo mais recente da pasta.
    assert report.remote_version == "2026-08-19"
    assert report.remote_released == date(2026, 8, 19)
    assert report.manifest_available
    assert report.available_modalities == (MODALITY_STANDARD, MODALITY_FULL)


def test_standard_modality_has_only_root_archive(monkeypatch, settings, tmp_path: Path):
    _rotas(monkeypatch)
    packages = version_inspector.inspect(tmp_path, settings).packages_for(
        MODALITY_STANDARD
    )

    assert [p.name for p in packages] == ["UltraNX.7z"]
    assert packages[0].sha256 == HASH_BASE
    assert packages[0].url == ""  # resolvido na hora do download
    assert packages[0].quickkey == QUICK_BASE


def test_full_modality_adds_the_extras(monkeypatch, settings, tmp_path: Path):
    _rotas(monkeypatch)
    packages = version_inspector.inspect(tmp_path, settings).packages_for(MODALITY_FULL)

    assert [p.name for p in packages] == ["UltraNX.7z", "Android.7z"]


def test_total_bytes_sums_the_modality(monkeypatch, settings, tmp_path: Path):
    _rotas(monkeypatch)
    report = version_inspector.inspect(tmp_path, settings)

    assert report.total_bytes_for(MODALITY_STANDARD) == 2068985680
    assert report.total_bytes_for(MODALITY_FULL) == 2068985680 + 15204765696


def test_non_archive_files_are_ignored(monkeypatch, settings, tmp_path: Path):
    """leia-me.txt está na pasta mas não é pacote."""
    _rotas(monkeypatch)
    report = version_inspector.inspect(tmp_path, settings)
    assert "leia-me.txt" not in {p.name for p in report.packages}


def test_folder_without_archives_is_rejected(monkeypatch, settings, tmp_path: Path):
    def fake_get(url, params=None, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        if "folder/get_content.php" in url:
            return _Reply(_sucesso({"folder_content": {"files": [], "folders": []}}))
        raise AssertionError(url)

    monkeypatch.setattr(mediafire.requests, "get", fake_get)
    with pytest.raises(RemoteDataError, match="nenhum pacote"):
        version_inspector.inspect(tmp_path, settings)
