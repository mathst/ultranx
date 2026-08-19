"""Testes das datas de versão: leitura, gravação, fallback e exibição."""

from __future__ import annotations

import json
from datetime import date
from pathlib import Path

import pytest

from ultranx.config import MODALITY_STANDARD, Settings
from ultranx.core import version_inspector
from ultranx.core.dates import (
    file_date,
    format_date,
    parse_http_date,
    parse_iso_date,
    to_iso,
)
from ultranx.core.drive_detector import read_local_state
from ultranx.core.installer import write_version_file
from ultranx.core.version_inspector import inspect

SHA = "a" * 64
LANCAMENTO = date(2026, 8, 15)


# --- parsing ----------------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "expected"),
    [
        ("2026-08-15", LANCAMENTO),
        ("  2026-08-15  ", LANCAMENTO),
        ("2026-08-15T10:30:00", LANCAMENTO),
        ("2026-08-15T10:30:00Z", LANCAMENTO),
    ],
)
def test_parse_iso_date_accepts_valid(raw, expected):
    assert parse_iso_date(raw) == expected


@pytest.mark.parametrize(
    "raw", [None, "", "   ", "15/08/2026", "ontem", "2026-13-45", "x" * 40]
)
def test_parse_iso_date_rejects_invalid(raw):
    assert parse_iso_date(raw) is None


def test_parse_http_date():
    assert parse_http_date("Sat, 15 Aug 2026 10:00:00 GMT") == LANCAMENTO


@pytest.mark.parametrize("raw", [None, "", "não é data"])
def test_parse_http_date_rejects_invalid(raw):
    assert parse_http_date(raw) is None


def test_file_date_uses_mtime(tmp_path: Path):
    target = tmp_path / "arquivo.txt"
    target.write_text("x", encoding="utf-8")
    assert file_date(target) is not None


def test_file_date_of_missing_file(tmp_path: Path):
    assert file_date(tmp_path / "ausente.txt") is None


def test_to_iso_roundtrip():
    assert to_iso(LANCAMENTO) == "2026-08-15"
    assert to_iso(None) is None
    assert parse_iso_date(to_iso(LANCAMENTO)) == LANCAMENTO


def test_format_date_is_brazilian():
    assert format_date(LANCAMENTO) == "15/08/2026"
    assert format_date(None) == "—"
    assert format_date(None, fallback="desconhecida") == "desconhecida"


# --- estado local -----------------------------------------------------------


def test_read_local_state_with_release_date(tmp_path: Path):
    (tmp_path / "packetVersion.txt").write_text("1.4.2\n2026-08-15\n", encoding="utf-8")

    state = read_local_state(tmp_path)

    assert state.version == "1.4.2"
    assert state.released == LANCAMENTO
    assert state.installed_at is not None  # vem do mtime
    assert state.is_installed


def test_read_local_state_without_date_is_backward_compatible(tmp_path: Path):
    """Cartão gravado à mão ou por versão antiga: só a versão, sem data."""
    (tmp_path / "packetVersion.txt").write_text("1.4.2\n", encoding="utf-8")

    state = read_local_state(tmp_path)

    assert state.version == "1.4.2"
    assert state.released is None
    assert state.installed_at is not None


def test_read_local_state_ignores_garbage_date(tmp_path: Path):
    (tmp_path / "packetVersion.txt").write_text("1.4.2\nontem\n", encoding="utf-8")

    state = read_local_state(tmp_path)

    assert state.version == "1.4.2"
    assert state.released is None


def test_read_local_state_on_blank_card(tmp_path: Path):
    state = read_local_state(tmp_path)
    assert not state.is_installed
    assert state.version is None
    assert state.released is None
    assert state.installed_at is None


# --- gravação ---------------------------------------------------------------


def test_write_version_file_records_release_date(tmp_path: Path):
    write_version_file(tmp_path, "1.4.2", LANCAMENTO)

    content = (tmp_path / "packetVersion.txt").read_text(encoding="utf-8")
    assert content == "1.4.2\n2026-08-15\n"
    assert read_local_state(tmp_path).released == LANCAMENTO


def test_write_version_file_without_date_keeps_single_line(tmp_path: Path):
    write_version_file(tmp_path, "1.4.2")
    assert (tmp_path / "packetVersion.txt").read_text(encoding="utf-8") == "1.4.2\n"


def test_written_file_is_readable_by_version_only_consumers(tmp_path: Path):
    """Quem lê só a primeira linha continua funcionando com o formato novo."""
    write_version_file(tmp_path, "1.4.2", LANCAMENTO)
    lines = (tmp_path / "packetVersion.txt").read_text(encoding="utf-8").split("\n")
    assert lines[0] == "1.4.2"


# --- data remota ------------------------------------------------------------


class _Response:
    def __init__(self, text: str, last_modified: str | None = None) -> None:
        self.text = text
        self.content = text.encode("utf-8")
        self.status_code = 200
        self.headers = {"Last-Modified": last_modified} if last_modified else {}

    def raise_for_status(self) -> None:
        return None

    def json(self):
        return json.loads(self.text)


def _serve(monkeypatch, manifest: dict | None, last_modified: str | None = None):
    def fake_get(url, timeout=None, **kwargs):  # noqa: ANN001, ARG001
        if url.endswith("packetVersion.txt"):
            return _Response("1.4.2\n", last_modified=last_modified)
        if url.endswith("manifest.json"):
            if manifest is None:
                raise version_inspector.requests.exceptions.ConnectionError("sem")
            return _Response(json.dumps(manifest))
        raise AssertionError(url)

    monkeypatch.setattr(version_inspector.requests, "get", fake_get)


def _manifest(**extra) -> dict:
    document = {
        "version": "1.4.2",
        "packages": {
            MODALITY_STANDARD: {"url": "a.zip", "sha256": SHA, "size": 10},
        },
    }
    document.update(extra)
    return document


@pytest.fixture()
def settings() -> Settings:
    return Settings(base_url="https://host/rox", http_timeout=5.0, skip_hash_check=False)


def test_remote_release_date_comes_from_manifest(monkeypatch, settings, tmp_path: Path):
    _serve(monkeypatch, _manifest(released="2026-08-15"))
    assert inspect(tmp_path, settings).remote_released == LANCAMENTO


def test_manifest_date_wins_over_last_modified(monkeypatch, settings, tmp_path: Path):
    _serve(
        monkeypatch,
        _manifest(released="2026-08-15"),
        last_modified="Mon, 01 Jun 2020 10:00:00 GMT",
    )
    assert inspect(tmp_path, settings).remote_released == LANCAMENTO


def test_last_modified_is_the_fallback(monkeypatch, settings, tmp_path: Path):
    """Servidor sem manifest: a data vem do cabeçalho HTTP."""
    _serve(monkeypatch, None, last_modified="Sat, 15 Aug 2026 10:00:00 GMT")

    report = inspect(tmp_path, settings)

    assert not report.manifest_available
    assert report.remote_released == LANCAMENTO


def test_no_date_anywhere_is_none(monkeypatch, settings, tmp_path: Path):
    _serve(monkeypatch, _manifest())
    assert inspect(tmp_path, settings).remote_released is None


def test_invalid_manifest_date_is_ignored(monkeypatch, settings, tmp_path: Path):
    _serve(monkeypatch, _manifest(released="15/08/2026"))
    assert inspect(tmp_path, settings).remote_released is None


def test_report_exposes_both_sides(monkeypatch, settings, tmp_path: Path):
    (tmp_path / "packetVersion.txt").write_text("1.0.0\n2026-01-10\n", encoding="utf-8")
    _serve(monkeypatch, _manifest(released="2026-08-15"))

    report = inspect(tmp_path, settings)

    assert report.local.version == "1.0.0"
    assert report.local.released == date(2026, 1, 10)
    assert report.local.installed_at is not None
    assert report.remote_version == "1.4.2"
    assert report.remote_released == LANCAMENTO
    assert report.update_available
