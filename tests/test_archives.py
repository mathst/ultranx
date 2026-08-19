"""Testes da extração: .zip pelo stdlib, .7z pelo py7zr, guarda anti-traversal."""

from __future__ import annotations

import io
import zipfile
from pathlib import Path

import pytest

from ultranx.core.archives import extract_archive, free_bytes, supported_suffixes
from ultranx.core.errors import InstallError, IntegrityError, OperationCancelled

CONTEUDO = {
    "atmosphere/package3": b"pacote novo",
    "bootloader/hekate_ipl.ini": b"[config]\n",
    "switch/daybreak.nro": b"app",
}


def _zip(tmp_path: Path, *, malicioso: bool = False) -> Path:
    destino = tmp_path / "pacote.zip"
    with zipfile.ZipFile(destino, "w") as arq:
        for nome, dados in CONTEUDO.items():
            arq.writestr(nome, dados)
        if malicioso:
            arq.writestr("../evil.bin", b"fora da raiz")
    return destino


def _sevenzip(tmp_path: Path) -> Path:
    py7zr = pytest.importorskip("py7zr")
    destino = tmp_path / "pacote.7z"
    with py7zr.SevenZipFile(destino, mode="w") as arq:
        for nome, dados in CONTEUDO.items():
            arq.writef(io.BytesIO(dados), nome)
    return destino


def test_supported_suffixes_includes_both():
    assert set(supported_suffixes()) == {".7z", ".zip"}


@pytest.mark.parametrize("fabrica", [_zip, _sevenzip])
def test_extract_writes_every_entry(tmp_path: Path, fabrica):
    arquivo = fabrica(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()

    gravadas = extract_archive(arquivo, sd)

    assert gravadas == len(CONTEUDO)
    assert (sd / "atmosphere" / "package3").read_bytes() == b"pacote novo"
    assert (sd / "bootloader" / "hekate_ipl.ini").exists()
    assert (sd / "switch" / "daybreak.nro").exists()


def test_extract_zip_blocks_traversal(tmp_path: Path):
    """Entrada com '..' é descartada em vez de escapar da raiz (zip-slip)."""
    arquivo = _zip(tmp_path, malicioso=True)
    sd = tmp_path / "sd"
    sd.mkdir()

    extract_archive(arquivo, sd)

    assert not (tmp_path / "evil.bin").exists()
    assert (sd / "atmosphere" / "package3").exists()


def test_extract_7z_blocks_traversal(tmp_path: Path, monkeypatch):
    """Guarda do 7z testada interceptando a listagem.

    py7zr recusa criar um arquivo com '../' (valida na escrita), então forjar o
    pacote malicioso com ele é impossível — o caminho testável é a listagem, que
    é exatamente onde a guarda age antes de qualquer gravação.
    """
    py7zr = pytest.importorskip("py7zr")
    arquivo = _sevenzip(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()

    monkeypatch.setattr(
        py7zr.SevenZipFile,
        "getnames",
        lambda self: ["../evil.bin", "../../pior.bin"],
    )

    with pytest.raises(IntegrityError, match="fora da raiz"):
        extract_archive(arquivo, sd)

    assert not (tmp_path / "evil.bin").exists()
    assert not (tmp_path.parent / "pior.bin").exists()


def test_absolute_paths_are_normalized_into_the_root(tmp_path: Path, monkeypatch):
    """Caminho absoluto no pacote vira relativo à raiz, como faz o zipfile.

    Não é escapada: o destino continua contido no cartão. O que a guarda barra é
    traversal com '..', que sairia da raiz de fato.
    """
    py7zr = pytest.importorskip("py7zr")
    arquivo = _sevenzip(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()

    monkeypatch.setattr(
        py7zr.SevenZipFile, "getnames", lambda self: ["/atmosphere/package3"]
    )

    # Não levanta: o caminho é aceito, ancorado dentro da raiz.
    extract_archive(arquivo, sd)


@pytest.mark.parametrize("fabrica", [_zip, _sevenzip])
def test_extract_overwrites_existing(tmp_path: Path, fabrica):
    arquivo = fabrica(tmp_path)
    sd = tmp_path / "sd"
    (sd / "atmosphere").mkdir(parents=True)
    (sd / "atmosphere" / "package3").write_bytes(b"versao antiga")

    extract_archive(arquivo, sd)

    assert (sd / "atmosphere" / "package3").read_bytes() == b"pacote novo"


@pytest.mark.parametrize("fabrica", [_zip, _sevenzip])
def test_extract_reports_progress(tmp_path: Path, fabrica):
    arquivo = fabrica(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    eventos: list[tuple[int, int, str]] = []

    extract_archive(arquivo, sd, progress=lambda *args: eventos.append(args))

    assert eventos
    assert eventos[-1][0] == eventos[-1][1]


@pytest.mark.parametrize("fabrica", [_zip, _sevenzip])
def test_extract_honours_cancellation(tmp_path: Path, fabrica):
    arquivo = fabrica(tmp_path)
    sd = tmp_path / "sd"
    sd.mkdir()
    with pytest.raises(OperationCancelled):
        extract_archive(arquivo, sd, should_cancel=lambda: True)


def test_unknown_format_is_rejected(tmp_path: Path):
    arquivo = tmp_path / "pacote.rar"
    arquivo.write_bytes(b"nao importa")
    with pytest.raises(InstallError, match="não suportado"):
        extract_archive(arquivo, tmp_path)


def test_corrupt_zip_is_integrity_error(tmp_path: Path):
    arquivo = tmp_path / "pacote.zip"
    arquivo.write_bytes(b"nao sou zip")
    with pytest.raises(IntegrityError):
        extract_archive(arquivo, tmp_path)


def test_corrupt_7z_is_integrity_error(tmp_path: Path):
    pytest.importorskip("py7zr")
    arquivo = tmp_path / "pacote.7z"
    arquivo.write_bytes(b"nao sou 7z")
    with pytest.raises(IntegrityError):
        extract_archive(arquivo, tmp_path)


def test_empty_zip_is_rejected(tmp_path: Path):
    arquivo = tmp_path / "pacote.zip"
    with zipfile.ZipFile(arquivo, "w"):
        pass
    with pytest.raises(InstallError, match="vazio"):
        extract_archive(arquivo, tmp_path)


def test_free_bytes_of_real_dir(tmp_path: Path):
    assert free_bytes(tmp_path) > 0


def test_free_bytes_of_missing_dir(tmp_path: Path):
    assert free_bytes(tmp_path / "nao-existe") >= 0
