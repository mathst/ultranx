"""Version Inspector — comparação entre versão local e remota.

Fluxo: baixa ``packetVersion.txt`` remoto (fonte canônica da versão publicada),
tenta enriquecer com ``manifest.json`` (URL/sha256/tamanho por modalidade) e
compara com o ``packetVersion.txt`` gravado no SD.

O manifest é opcional: sem ele o inspector monta URLs por convenção, e o
installer avisa que não há checksum para validar.
"""

from __future__ import annotations

import json
import logging
import re
from dataclasses import dataclass
from pathlib import Path

import requests

from ..config import (
    MODALITY_FULL,
    MODALITY_STANDARD,
    Settings,
)
from .drive_detector import read_local_version
from .errors import NetworkError, RemoteDataError

logger = logging.getLogger(__name__)

_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_VERSION_TOKEN_RE = re.compile(r"\d+|[a-z]+")
_MAX_TEXT_BYTES = 64 * 1024  # protege contra resposta gigante em endpoint errado


@dataclass(frozen=True, slots=True)
class PackageInfo:
    """Metadados de uma modalidade de pacote."""

    modality: str
    url: str
    sha256: str | None
    size_bytes: int | None


@dataclass(frozen=True, slots=True)
class VersionReport:
    """Resultado completo da inspeção (imutável)."""

    local_version: str | None
    remote_version: str
    packages: tuple[PackageInfo, ...]
    manifest_available: bool

    @property
    def update_available(self) -> bool:
        if self.local_version is None:
            return True
        return compare_versions(self.remote_version, self.local_version) > 0

    @property
    def is_downgrade(self) -> bool:
        if self.local_version is None:
            return False
        return compare_versions(self.remote_version, self.local_version) < 0

    def package_for(self, modality: str) -> PackageInfo:
        for package in self.packages:
            if package.modality == modality:
                return package
        raise RemoteDataError(
            f"Modalidade '{modality}' não está disponível na versão "
            f"{self.remote_version}."
        )

    @property
    def available_modalities(self) -> tuple[str, ...]:
        return tuple(package.modality for package in self.packages)


def _version_key(version: str) -> tuple[object, ...]:
    """Chave de ordenação tolerante a formatos (``1.4.2``, ``v1.4-rc1``)."""
    tokens = _VERSION_TOKEN_RE.findall(version.strip().casefold().lstrip("v"))
    # Números comparam como int; sufixos textuais comparam depois, como str.
    return tuple((0, int(t)) if t.isdigit() else (1, t) for t in tokens)


def compare_versions(left: str, right: str) -> int:
    """Compara duas versões: ``1`` se ``left`` > ``right``, ``-1``, ou ``0``."""
    left_key, right_key = _version_key(left), _version_key(right)
    if left_key == right_key:
        return 0
    return 1 if left_key > right_key else -1


def _get(url: str, settings: Settings) -> requests.Response:
    """GET com timeout e conversão de qualquer falha em :class:`NetworkError`."""
    try:
        response = requests.get(url, timeout=settings.http_timeout)
        response.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise NetworkError(f"Tempo esgotado ao acessar {url}.") from exc
    except requests.exceptions.SSLError as exc:
        raise NetworkError(f"Falha de certificado TLS ao acessar {url}.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise NetworkError(f"Sem conexão com {url}.") from exc
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", "?")
        raise NetworkError(f"Servidor respondeu HTTP {status} para {url}.") from exc
    except requests.exceptions.RequestException as exc:
        raise NetworkError(f"Falha de rede ao acessar {url}: {exc}.") from exc
    return response


def _first_line(text: str) -> str:
    lines = [line.strip() for line in text.strip().splitlines() if line.strip()]
    if not lines:
        raise RemoteDataError("O packetVersion.txt remoto está vazio.")
    return lines[0]


def fetch_remote_version(settings: Settings) -> str:
    """Baixa e valida a versão publicada."""
    response = _get(settings.version_url, settings)
    if len(response.content) > _MAX_TEXT_BYTES:
        raise RemoteDataError(
            "A resposta de packetVersion.txt é grande demais para ser uma versão."
        )
    version = _first_line(response.text)
    if len(version) > 64 or not any(ch.isdigit() for ch in version):
        raise RemoteDataError(f"Versão remota inesperada: '{version[:64]}'.")
    return version


def _default_packages(settings: Settings, version: str) -> tuple[PackageInfo, ...]:
    """URLs por convenção quando não há manifest."""
    return tuple(
        PackageInfo(
            modality=modality,
            url=f"{settings.base_url}/rox-{modality}-{version}.zip",
            sha256=None,
            size_bytes=None,
        )
        for modality in (MODALITY_STANDARD, MODALITY_FULL)
    )


def _parse_package(modality: str, raw: object, base_url: str) -> PackageInfo | None:
    """Converte uma entrada de manifest em :class:`PackageInfo`.

    Entradas malformadas são descartadas (log em WARNING) em vez de derrubar a
    inspeção inteira: uma modalidade quebrada não deve bloquear a outra.
    """
    if not isinstance(raw, dict):
        logger.warning("Entrada de manifest inválida para '%s'.", modality)
        return None

    url = str(raw.get("url", "")).strip()
    if not url:
        logger.warning("Manifest sem 'url' para '%s'.", modality)
        return None
    if not url.startswith(("http://", "https://")):
        url = f"{base_url}/{url.lstrip('/')}"
    if not url.startswith("https://"):
        logger.warning("URL não-HTTPS para '%s': %s", modality, url)

    sha256 = str(raw.get("sha256", "")).strip().casefold() or None
    if sha256 and not _SHA256_RE.match(sha256):
        logger.warning("sha256 inválido para '%s'; ignorando.", modality)
        sha256 = None

    size_raw = raw.get("size")
    size = int(size_raw) if isinstance(size_raw, int) and size_raw > 0 else None

    return PackageInfo(modality=modality, url=url, sha256=sha256, size_bytes=size)


def fetch_manifest(settings: Settings, version: str) -> tuple[PackageInfo, ...]:
    """Baixa o manifest opcional. Falha de rede/JSON degrada para convenção."""
    try:
        response = _get(settings.manifest_url, settings)
        document = response.json()
    except (NetworkError, json.JSONDecodeError, ValueError) as exc:
        logger.info("manifest.json indisponível (%s); usando URLs por convenção.", exc)
        return ()

    if not isinstance(document, dict):
        logger.warning("manifest.json não é um objeto JSON; ignorando.")
        return ()

    manifest_version = str(document.get("version", version)).strip()
    if manifest_version and compare_versions(manifest_version, version) != 0:
        logger.warning(
            "manifest.json declara versão %s mas packetVersion.txt declara %s.",
            manifest_version,
            version,
        )

    raw_packages = document.get("packages")
    if not isinstance(raw_packages, dict):
        logger.warning("manifest.json sem objeto 'packages'; ignorando.")
        return ()

    parsed = [
        package
        for modality in (MODALITY_STANDARD, MODALITY_FULL)
        if (
            package := _parse_package(
                modality, raw_packages.get(modality), settings.base_url
            )
        )
        is not None
    ]
    return tuple(parsed)


def inspect(sd_root: Path, settings: Settings) -> VersionReport:
    """Executa a inspeção completa. Chamado de dentro de uma ``QThread``."""
    remote_version = fetch_remote_version(settings)
    packages = fetch_manifest(settings, remote_version)
    manifest_available = bool(packages)
    if not manifest_available:
        packages = _default_packages(settings, remote_version)

    report = VersionReport(
        local_version=read_local_version(sd_root),
        remote_version=remote_version,
        packages=packages,
        manifest_available=manifest_available,
    )
    logger.info(
        "Inspeção: local=%s remoto=%s manifest=%s modalidades=%s",
        report.local_version,
        report.remote_version,
        manifest_available,
        ",".join(report.available_modalities),
    )
    return report
