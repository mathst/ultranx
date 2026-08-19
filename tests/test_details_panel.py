"""Testes do painel de detalhes do pacote.

O painel responde, dentro do app, as perguntas que o usuário faria antes de
apagar o cartão: quais arquivos, que tamanho, dá para verificar a integridade,
cabe no cartão e quanto tempo leva.
"""

from __future__ import annotations

import os
from pathlib import Path

import pytest

os.environ.setdefault("QT_QPA_PLATFORM", "offscreen")

pytest.importorskip("PyQt6")

from PyQt6.QtWidgets import QApplication  # noqa: E402

from ultranx.config import (  # noqa: E402
    ENV_BASE_URL,
    MODALITY_FULL,
    MODALITY_STANDARD,
)
from ultranx.core.drive_detector import LocalState  # noqa: E402
from ultranx.core.version_inspector import PackageInfo, VersionReport  # noqa: E402
from ultranx.ui.main_window import MainWindow  # noqa: E402

GB = 1024**3
SHA = "8ce3cab1a54c3e9f201d86823d511808eb3d3a96525541801aacc62bbcbee13d"


@pytest.fixture(scope="module")
def app() -> QApplication:
    return QApplication.instance() or QApplication(["testes"])


@pytest.fixture()
def window(app, monkeypatch, tmp_path: Path) -> MainWindow:
    """Janela isolada: sem varredura de mídia real e sem config do usuário."""
    monkeypatch.setenv(ENV_BASE_URL, "https://host/rox")
    monkeypatch.setattr("ultranx.ui.main_window.scan_removable_drives", lambda: ())
    monkeypatch.setattr(Path, "home", classmethod(lambda cls: tmp_path))
    return MainWindow()


def _report(*, com_extras: bool = True, com_hash: bool = True) -> VersionReport:
    base = PackageInfo(
        modality=MODALITY_STANDARD,
        url="",
        sha256=SHA if com_hash else None,
        size_bytes=2 * GB,
        name="UltraNX.7z",
        quickkey="kdfcc792hlkm1nf",
    )
    packages = [base]
    if com_extras:
        # Completo = base + os dois extras, como o MediaFire devolve.
        packages += [
            PackageInfo(MODALITY_FULL, "", SHA, 2 * GB, "UltraNX.7z", "a" * 11),
            PackageInfo(MODALITY_FULL, "", SHA, 14 * GB, "Android.7z", "b" * 11),
            PackageInfo(MODALITY_FULL, "", SHA, 64 * 1024 * 1024, "SwitchU.7z", "c" * 11),
        ]
    return VersionReport(
        local=LocalState(None, None, None),
        remote_version="2026-08-19",
        remote_released=None,
        packages=tuple(packages),
        manifest_available=True,
    )


def _mostrar(window: MainWindow, report: VersionReport) -> str:
    window._on_version_ready(report)
    return window.details_view.toPlainText()


def test_lists_each_file_with_size_and_checksum(window):
    texto = _mostrar(window, _report())

    assert "UltraNX.7z" in texto
    assert "2.0 GB" in texto
    assert SHA[:12] in texto
    assert "1 arquivo(s)" in texto


def test_shows_modality_total(window):
    assert "Total a baixar: 2.0 GB" in _mostrar(window, _report())


def test_switching_modality_updates_the_panel(window):
    _mostrar(window, _report())
    assert "Android.7z" not in window.details_view.toPlainText()

    indice = window.modality_combo.findData(MODALITY_FULL)
    window.modality_combo.setCurrentIndex(indice)
    texto = window.details_view.toPlainText()

    assert "Android.7z" in texto
    assert "3 arquivo(s)" in texto
    assert "Total a baixar: 16.1 GB" in texto


def test_shows_download_time_estimates(window):
    texto = _mostrar(window, _report())

    assert "Tempo estimado de download:" in texto
    assert "conexão lenta (1 MB/s)" in texto
    assert "conexão rápida (20 MB/s)" in texto


def test_warns_when_checksum_is_missing(window):
    texto = _mostrar(window, _report(com_hash=False))

    assert "SEM checksum" in texto
    assert "integridade" in texto


def test_space_verdict_fits(window, monkeypatch, tmp_path: Path):
    """Cartão folgado: o painel diz que cabe."""
    monkeypatch.setattr("ultranx.ui.main_window.free_bytes", lambda path: 100 * GB)
    monkeypatch.setattr(MainWindow, "selected_root", property(lambda self: tmp_path))

    texto = _mostrar(window, _report())

    assert "cabe no cartão" in texto
    assert "NÃO CABE" not in texto


def test_space_verdict_does_not_fit(window, monkeypatch, tmp_path: Path):
    """Cartão apertado: o veredito aparece ANTES de o usuário clicar."""
    monkeypatch.setattr("ultranx.ui.main_window.free_bytes", lambda path: 3 * GB)
    monkeypatch.setattr(MainWindow, "selected_root", property(lambda self: tmp_path))

    texto = _mostrar(window, _report())

    assert "NÃO CABE" in texto
    assert "modalidade menor" in texto


def test_panel_clears_when_state_resets(window):
    _mostrar(window, _report())
    assert window.details_view.toPlainText()

    window._reset_version_state()

    assert window.details_view.toPlainText() == ""
