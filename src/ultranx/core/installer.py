"""Payload Installer — download em streaming, extração e gravação de versão.

Sequência: baixar para arquivo temporário (nunca direto sobre o SD final),
validar integridade, extrair sobrescrevendo a raiz, gravar
``packetVersion.txt`` e reler para confirmar. O temporário fica no próprio SD
quando há espaço, para que a extração seja um move local; caso contrário usa o
temp do sistema.

Todo callback de progresso é opcional e síncrono: os workers PyQt6 apenas
repassam para ``pyqtSignal.emit``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import shutil
import tempfile
import zipfile
from collections.abc import Callable
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from ..config import DOWNLOAD_CHUNK_SIZE, VERSION_FILE_NAME, Settings
from .dates import to_iso
from .errors import (
    DriveDisconnectedError,
    InstallError,
    IntegrityError,
    NetworkError,
    OperationCancelled,
    PermissionDeniedError,
)
from .paths import human_size, join_within, safe_resolve
from .version_inspector import PackageInfo

logger = logging.getLogger(__name__)

# (bytes_recebidos, bytes_totais_ou_None)
DownloadProgress = Callable[[int, int | None], None]
# (entradas_extraidas, entradas_totais, nome_atual)
ExtractProgress = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]

_TEMP_PREFIX = "ultranx-payload-"
_SAFETY_MARGIN = 1.15  # ZIP + extração convivem no mesmo cartão


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Resultado de uma instalação bem-sucedida."""

    version: str
    released: date | None
    modality: str
    extracted_entries: int
    payload_bytes: int


def _free_bytes(path: Path) -> int:
    """Espaço livre em bytes; ``0`` quando indeterminável (força fallback)."""
    try:
        return int(shutil.disk_usage(str(path)).free)
    except (OSError, ValueError):
        logger.debug("disk_usage indisponível para %s", path)
        return 0


def _choose_temp_dir(sd_root: Path, expected_size: int | None) -> Path:
    """Prefere o próprio SD; cai para o temp do sistema se faltar espaço."""
    root = safe_resolve(sd_root)
    needed = int((expected_size or 0) * _SAFETY_MARGIN)
    if expected_size is None or _free_bytes(root) > needed:
        return root
    logger.info(
        "Espaço insuficiente no SD para o temporário (%s necessários); usando %s.",
        human_size(needed),
        tempfile.gettempdir(),
    )
    return Path(tempfile.gettempdir())


def download_payload(
    package: PackageInfo,
    settings: Settings,
    sd_root: Path,
    progress: DownloadProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> tuple[Path, int]:
    """Baixa o pacote em chunks para um arquivo temporário.

    Retorna ``(caminho_temporario, bytes_baixados)``. O chamador é responsável
    por remover o temporário (ver :func:`install_payload`, que já faz isso).
    """
    temp_dir = _choose_temp_dir(sd_root, package.size_bytes)
    try:
        handle, temp_name = tempfile.mkstemp(
            prefix=_TEMP_PREFIX, suffix=".zip", dir=str(temp_dir)
        )
    except (OSError, PermissionError) as exc:
        raise PermissionDeniedError(
            f"Não foi possível criar arquivo temporário em '{temp_dir}'."
        ) from exc

    temp_path = Path(temp_name)
    digest = hashlib.sha256()
    received = 0

    try:
        with requests.get(
            package.url, stream=True, timeout=settings.http_timeout
        ) as response:
            response.raise_for_status()
            declared = response.headers.get("Content-Length")
            total = (
                int(declared) if declared and declared.isdigit() else package.size_bytes
            )

            with os.fdopen(handle, "wb") as sink:
                for chunk in response.iter_content(chunk_size=DOWNLOAD_CHUNK_SIZE):
                    if should_cancel is not None and should_cancel():
                        raise OperationCancelled("Download cancelado pelo usuário.")
                    if not chunk:
                        continue
                    sink.write(chunk)
                    digest.update(chunk)
                    received += len(chunk)
                    if progress is not None:
                        progress(received, total)
                sink.flush()
                os.fsync(sink.fileno())
    except OperationCancelled:
        _discard(temp_path)
        raise
    except requests.exceptions.Timeout as exc:
        _discard(temp_path)
        raise NetworkError("Tempo esgotado durante o download do pacote.") from exc
    except requests.exceptions.HTTPError as exc:
        _discard(temp_path)
        status = getattr(exc.response, "status_code", "?")
        raise NetworkError(
            f"Servidor respondeu HTTP {status} ao baixar o pacote."
        ) from exc
    except requests.exceptions.RequestException as exc:
        _discard(temp_path)
        raise NetworkError(f"Falha de rede durante o download: {exc}.") from exc
    except PermissionError as exc:
        _discard(temp_path)
        raise PermissionDeniedError(
            "Sem permissão de escrita ao gravar o pacote temporário."
        ) from exc
    except OSError as exc:
        _discard(temp_path)
        if not temp_dir.exists():
            raise DriveDisconnectedError(
                "O cartão foi desconectado durante o download."
            ) from exc
        raise InstallError(f"Falha de I/O durante o download: {exc}.") from exc

    _verify_integrity(package, digest.hexdigest(), received, settings, temp_path)
    logger.info("Download concluído: %s (%s).", package.url, human_size(received))
    return temp_path, received


def _verify_integrity(
    package: PackageInfo,
    actual_sha256: str,
    actual_size: int,
    settings: Settings,
    temp_path: Path,
) -> None:
    """Valida sha256 e tamanho. Descarta o temporário em caso de divergência."""
    if package.size_bytes is not None and actual_size != package.size_bytes:
        _discard(temp_path)
        raise IntegrityError(
            f"Tamanho divergente: esperado {human_size(package.size_bytes)}, "
            f"recebido {human_size(actual_size)}."
        )

    if package.sha256 is None:
        logger.warning(
            "Pacote sem sha256 no manifest — integridade NÃO verificada para %s.",
            package.url,
        )
        return

    if settings.skip_hash_check:
        logger.warning("Verificação de hash desabilitada por variável de ambiente.")
        return

    if actual_sha256 != package.sha256:
        _discard(temp_path)
        raise IntegrityError(
            "Checksum SHA-256 do download não corresponde ao manifest "
            f"(esperado {package.sha256[:12]}…, obtido {actual_sha256[:12]}…)."
        )


def _discard(path: Path) -> None:
    """Remove o temporário sem nunca mascarar a exceção em curso."""
    try:
        path.unlink(missing_ok=True)
    except OSError as exc:  # pragma: no cover - best effort
        logger.warning("Não foi possível remover o temporário %s: %s", path, exc)


def extract_payload(
    archive_path: Path,
    sd_root: Path,
    progress: ExtractProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> int:
    """Extrai o ZIP sobre a raiz do SD, sobrescrevendo o que colidir.

    Cada entrada passa por :func:`~ultranx.core.paths.join_within`; entradas que
    escapariam da raiz (zip-slip) são descartadas com log em WARNING. Retorna o
    número de entradas gravadas.
    """
    root = safe_resolve(sd_root)
    written = 0

    try:
        with zipfile.ZipFile(archive_path) as archive:
            entries = [item for item in archive.infolist() if item.filename]
            total = len(entries)
            if total == 0:
                raise InstallError("O pacote baixado está vazio.")

            for index, entry in enumerate(entries, start=1):
                if should_cancel is not None and should_cancel():
                    raise OperationCancelled("Extração cancelada pelo usuário.")

                target = join_within(root, entry.filename)
                if target is None:
                    logger.warning(
                        "GUARD: entrada '%s' fora da raiz; descartada.", entry.filename
                    )
                    continue

                if entry.is_dir():
                    target.mkdir(parents=True, exist_ok=True)
                    continue

                target.parent.mkdir(parents=True, exist_ok=True)
                with archive.open(entry) as source, target.open("wb") as sink:
                    while block := source.read(DOWNLOAD_CHUNK_SIZE):
                        sink.write(block)
                written += 1

                if progress is not None:
                    progress(index, total, entry.filename)
    except zipfile.BadZipFile as exc:
        raise IntegrityError("O pacote baixado não é um ZIP válido.") from exc
    except OperationCancelled:
        raise
    except PermissionError as exc:
        raise PermissionDeniedError(
            "Sem permissão de escrita ao extrair o pacote no cartão."
        ) from exc
    except FileNotFoundError as exc:
        raise DriveDisconnectedError(
            "O cartão foi desconectado durante a extração."
        ) from exc
    except OSError as exc:
        if not root.exists():
            raise DriveDisconnectedError(
                "O cartão foi desconectado durante a extração."
            ) from exc
        raise InstallError(f"Falha de I/O durante a extração: {exc}.") from exc

    logger.info("Extração concluída: %d entrada(s) gravada(s).", written)
    return written


def write_version_file(sd_root: Path, version: str, released: date | None = None) -> None:
    """Grava e revalida ``packetVersion.txt``.

    Formato: versão na primeira linha e, quando conhecida, a data de lançamento
    em ISO-8601 na segunda. Quem lê só a primeira linha continua funcionando.

    A releitura é obrigatória: em FAT32 uma escrita "bem-sucedida" pode não
    persistir se o cartão for removido antes do flush.
    """
    target = safe_resolve(sd_root) / VERSION_FILE_NAME
    iso = to_iso(released)
    payload = f"{version.strip()}\n" if iso is None else f"{version.strip()}\n{iso}\n"

    try:
        with target.open("w", encoding="utf-8", newline="\n") as sink:
            sink.write(payload)
            sink.flush()
            os.fsync(sink.fileno())
    except PermissionError as exc:
        raise PermissionDeniedError(
            f"Sem permissão para gravar '{VERSION_FILE_NAME}' na raiz do cartão."
        ) from exc
    except FileNotFoundError as exc:
        raise DriveDisconnectedError(
            f"O cartão desapareceu ao gravar '{VERSION_FILE_NAME}'."
        ) from exc
    except OSError as exc:
        raise InstallError(f"Falha ao gravar '{VERSION_FILE_NAME}': {exc}.") from exc

    # Valida pela primeira linha: a data é metadado, a versão é o estado.
    written = target.read_text(encoding="utf-8", errors="replace").splitlines()
    confirmed = written[0].strip() if written else ""
    if confirmed != version.strip():
        raise InstallError(
            f"Validação falhou: '{VERSION_FILE_NAME}' contém '{confirmed}' em vez "
            f"de '{version.strip()}'. Reexecute a atualização."
        )
    logger.info(
        "packetVersion.txt gravado e validado: %s (lançamento: %s)", version, iso or "—"
    )


def install_payload(
    package: PackageInfo,
    version: str,
    sd_root: Path,
    settings: Settings,
    released: date | None = None,
    download_progress: DownloadProgress | None = None,
    extract_progress: ExtractProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> InstallResult:
    """Orquestra download → extração → gravação de versão.

    O temporário é sempre removido, inclusive em falha, para não deixar lixo de
    centenas de MB no cartão do usuário.
    """
    archive_path, payload_bytes = download_payload(
        package, settings, sd_root, download_progress, should_cancel
    )
    try:
        entries = extract_payload(archive_path, sd_root, extract_progress, should_cancel)
        write_version_file(sd_root, version, released)
    finally:
        _discard(archive_path)

    return InstallResult(
        version=version,
        released=released,
        modality=package.modality,
        extracted_entries=entries,
        payload_bytes=payload_bytes,
    )
