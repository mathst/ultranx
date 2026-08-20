"""Testes do Payload Installer: integridade, zip-slip, cancelamento e versão."""

from __future__ import annotations

import hashlib
import io
import zipfile
from pathlib import Path

import pytest
import requests

from ultranx.config import MODALITY_STANDARD, Settings
from ultranx.core import installer
from ultranx.core.errors import (
    InstallError,
    IntegrityError,
    NetworkError,
    OperationCancelled,
)
from ultranx.core.installer import (
    download_payload,
    extract_payload,
    install_payload,
    write_version_file,
)
from ultranx.core.version_inspector import PackageInfo


@pytest.fixture()
def settings() -> Settings:
    return Settings(base_url="https://host/rox", http_timeout=5.0, skip_hash_check=False)


def _zip_bytes(include_evil: bool = False) -> bytes:
    buffer = io.BytesIO()
    with zipfile.ZipFile(buffer, "w") as archive:
        archive.writestr("atmosphere/package3", b"pacote")
        archive.writestr("bootloader/hekate_ipl.ini", b"[config]\n")
        archive.writestr("switch/hbmenu.nro", b"nro")
        if include_evil:
            archive.writestr("../evil.bin", b"malicioso")
    return buffer.getvalue()


class FakeStream:
    """Dublê de resposta em streaming de ``requests``."""

    def __init__(self, payload: bytes, status: int = 200, declare_length: bool = True):
        self._payload = payload
        self.status_code = status
        self.headers = {"Content-Length": str(len(payload))} if declare_length else {}

    def __enter__(self):
        return self

    def __exit__(self, *exc_info):
        return False

    def raise_for_status(self) -> None:
        if self.status_code >= 400:
            error = requests.exceptions.HTTPError(f"HTTP {self.status_code}")
            error.response = self  # type: ignore[attr-defined]
            raise error

    def iter_content(self, chunk_size: int = 1):
        for start in range(0, len(self._payload), chunk_size):
            yield self._payload[start : start + chunk_size]


def _package(payload: bytes, *, with_hash: bool = True, size: int | None = None):
    return PackageInfo(
        modality=MODALITY_STANDARD,
        url="https://host/rox/rox-standard-1.4.2.zip",
        sha256=hashlib.sha256(payload).hexdigest() if with_hash else None,
        size_bytes=len(payload) if size is None else size,
    )


def _serve(monkeypatch, payload: bytes, **kwargs):
    monkeypatch.setattr(
        installer.requests,
        "get",
        lambda url, stream=False, timeout=None, **rest: FakeStream(payload, **kwargs),
    )


# --- escolha do diretório temporário -----------------------------------------


def test_choose_temp_dir_prefers_system_temp_when_it_has_room(monkeypatch, tmp_path):
    sd_root = tmp_path / "sd"
    sd_root.mkdir()
    system_temp = tmp_path / "systemp"
    system_temp.mkdir()
    monkeypatch.setattr(installer.tempfile, "gettempdir", lambda: str(system_temp))
    monkeypatch.setattr(installer, "free_bytes", lambda path: 10_000_000_000)

    assert installer._choose_temp_dir(sd_root, 1_000_000) == system_temp


def test_choose_temp_dir_falls_back_to_sd_when_system_temp_is_tight(monkeypatch, tmp_path):
    sd_root = tmp_path / "sd"
    sd_root.mkdir()
    system_temp = tmp_path / "systemp"
    system_temp.mkdir()
    monkeypatch.setattr(installer.tempfile, "gettempdir", lambda: str(system_temp))
    monkeypatch.setattr(installer, "free_bytes", lambda path: 1)

    assert installer._choose_temp_dir(sd_root, 1_000_000) == sd_root.resolve()


# --- download ---------------------------------------------------------------


def test_download_writes_temp_and_reports_progress(monkeypatch, settings, tmp_path):
    payload = _zip_bytes()
    _serve(monkeypatch, payload)
    events: list[tuple[int, int | None]] = []

    path, received = download_payload(
        _package(payload), settings, tmp_path, progress=lambda *a: events.append(a)
    )

    assert received == len(payload)
    assert path.read_bytes() == payload
    assert events[-1] == (len(payload), len(payload))
    path.unlink()


def test_download_rejects_wrong_checksum(monkeypatch, settings, tmp_path: Path):
    payload = _zip_bytes()
    _serve(monkeypatch, payload)
    corrupted = PackageInfo(
        MODALITY_STANDARD, "https://host/x.zip", "b" * 64, len(payload)
    )

    with pytest.raises(IntegrityError):
        download_payload(corrupted, settings, tmp_path)

    assert not list(tmp_path.glob("ultranx-payload-*"))


def test_download_rejects_wrong_size(monkeypatch, settings, tmp_path: Path):
    payload = _zip_bytes()
    _serve(monkeypatch, payload)

    with pytest.raises(IntegrityError):
        download_payload(_package(payload, size=len(payload) + 10), settings, tmp_path)


def test_download_accepts_package_without_hash(monkeypatch, settings, tmp_path: Path):
    payload = _zip_bytes()
    _serve(monkeypatch, payload)
    path, _ = download_payload(_package(payload, with_hash=False), settings, tmp_path)
    assert path.exists()
    path.unlink()


def test_skip_hash_check_env_bypasses_validation(monkeypatch, tmp_path: Path):
    payload = _zip_bytes()
    _serve(monkeypatch, payload)
    lax = Settings(base_url="https://host", http_timeout=5.0, skip_hash_check=True)
    wrong = PackageInfo(MODALITY_STANDARD, "https://host/x.zip", "c" * 64, len(payload))

    path, _ = download_payload(wrong, lax, tmp_path)
    assert path.exists()
    path.unlink()


def test_download_http_error(monkeypatch, settings, tmp_path: Path):
    _serve(monkeypatch, b"", status=500)
    with pytest.raises(NetworkError):
        download_payload(_package(b""), settings, tmp_path)


def test_download_cancellation_discards_temp(monkeypatch, settings, tmp_path: Path):
    payload = _zip_bytes()
    _serve(monkeypatch, payload)

    with pytest.raises(OperationCancelled):
        download_payload(
            _package(payload), settings, tmp_path, should_cancel=lambda: True
        )

    assert not list(tmp_path.glob("ultranx-payload-*"))


# --- extração ---------------------------------------------------------------


def test_extract_writes_all_entries(tmp_path: Path):
    archive = tmp_path / "p.zip"
    archive.write_bytes(_zip_bytes())
    target = tmp_path / "sd"
    target.mkdir()

    written = extract_payload(archive, target)

    assert written == 3
    assert (target / "atmosphere" / "package3").read_bytes() == b"pacote"
    assert (target / "bootloader" / "hekate_ipl.ini").exists()


def test_extract_blocks_zip_slip(tmp_path: Path):
    archive = tmp_path / "p.zip"
    archive.write_bytes(_zip_bytes(include_evil=True))
    target = tmp_path / "sd"
    target.mkdir()

    written = extract_payload(archive, target)

    assert written == 3  # a entrada maliciosa foi descartada
    assert not (tmp_path / "evil.bin").exists()


def test_extract_overwrites_existing(tmp_path: Path):
    archive = tmp_path / "p.zip"
    archive.write_bytes(_zip_bytes())
    target = tmp_path / "sd"
    (target / "atmosphere").mkdir(parents=True)
    (target / "atmosphere" / "package3").write_bytes(b"antigo")

    extract_payload(archive, target)
    assert (target / "atmosphere" / "package3").read_bytes() == b"pacote"


def test_extract_rejects_invalid_zip(tmp_path: Path):
    archive = tmp_path / "p.zip"
    archive.write_bytes(b"nao sou zip")
    with pytest.raises(IntegrityError):
        extract_payload(archive, tmp_path)


def test_extract_rejects_empty_zip(tmp_path: Path):
    archive = tmp_path / "p.zip"
    with zipfile.ZipFile(archive, "w"):
        pass
    with pytest.raises(InstallError):
        extract_payload(archive, tmp_path)


def test_extract_cancellation(tmp_path: Path):
    archive = tmp_path / "p.zip"
    archive.write_bytes(_zip_bytes())
    with pytest.raises(OperationCancelled):
        extract_payload(archive, tmp_path, should_cancel=lambda: True)


# --- versão -----------------------------------------------------------------


def test_write_version_file_persists_and_validates(tmp_path: Path):
    write_version_file(tmp_path, "1.4.2")
    assert (tmp_path / "packetVersion.txt").read_text(encoding="utf-8") == "1.4.2\n"


def test_write_version_file_strips_whitespace(tmp_path: Path):
    write_version_file(tmp_path, "  1.4.2  ")
    assert (tmp_path / "packetVersion.txt").read_text(encoding="utf-8") == "1.4.2\n"


# --- orquestração -----------------------------------------------------------


def test_install_payload_end_to_end(monkeypatch, settings, tmp_path: Path):
    payload = _zip_bytes()
    _serve(monkeypatch, payload)
    sd = tmp_path / "sd"
    sd.mkdir()

    result = install_payload(_package(payload), "1.4.2", sd, settings)

    assert result.version == "1.4.2"
    assert result.extracted_entries == 3
    assert result.payload_bytes == len(payload)
    assert (sd / "packetVersion.txt").read_text(encoding="utf-8").strip() == "1.4.2"
    assert not list(sd.glob("ultranx-payload-*"))  # temporário removido


def test_install_payload_cleans_temp_on_failure(monkeypatch, settings, tmp_path: Path):
    _serve(monkeypatch, b"nao sou zip")
    sd = tmp_path / "sd"
    sd.mkdir()
    package = PackageInfo(MODALITY_STANDARD, "https://host/x.zip", None, None)

    with pytest.raises(IntegrityError):
        install_payload(package, "1.4.2", sd, settings)

    assert not list(sd.glob("ultranx-payload-*"))
    assert not (sd / "packetVersion.txt").exists()
