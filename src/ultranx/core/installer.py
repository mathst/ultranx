"""Payload Installer — download em streaming, extração e gravação de versão.

Sequência: baixar para arquivo temporário (nunca direto sobre o SD final),
validar integridade, extrair sobrescrevendo a raiz, gravar
``packetVersion.txt`` e reler para confirmar. O temporário fica no temp do
sistema (disco local, normalmente bem mais rápido que o cartão via leitor
USB) quando há espaço; só cai para o próprio SD se o disco local estiver
apertado. Extrair sempre lê o arquivo baixado e escreve o conteúdo
descompactado — nunca é um simples move, mesmo com os dois no mesmo drive —
então manter o temporário fora do SD evita que ele sofra a escrita do
download e a releitura na extração além da escrita final, cortando o
tráfego pelo canal mais lento a menos da metade.

Todo callback de progresso é opcional e síncrono: os workers PyQt6 apenas
repassam para ``pyqtSignal.emit``.
"""

from __future__ import annotations

import hashlib
import logging
import os
import tempfile
from collections.abc import Callable, Sequence
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import requests

from ..config import DOWNLOAD_CHUNK_SIZE, VERSION_FILE_NAME, Settings
from . import mediafire
from .archives import ExtractProgress, extract_archive, free_bytes
from .dates import to_iso
from .errors import (
    DriveDisconnectedError,
    InstallError,
    IntegrityError,
    NetworkError,
    OperationCancelled,
    PermissionDeniedError,
)
from .paths import human_size, safe_resolve
from .version_inspector import PackageInfo

logger = logging.getLogger(__name__)

# (bytes_recebidos, bytes_totais_ou_None)
DownloadProgress = Callable[[int, int | None], None]
CancelCheck = Callable[[], bool]
# (indice_do_arquivo, total_de_arquivos, nome)
ArchiveStart = Callable[[int, int, str], None]

_TEMP_PREFIX = "ultranx-payload-"
_SAFETY_MARGIN = 1.15  # o pacote e a extração convivem no mesmo cartão
# Folga sobre o tamanho comprimido: o pacote baixado e o conteúdo extraído
# convivem no cartão, e 7z de pacote de Switch expande ~1,6x.
INSTALL_SIZE_RATIO = 2.6


@dataclass(frozen=True, slots=True)
class InstallResult:
    """Resultado de uma instalação bem-sucedida."""

    version: str
    released: date | None
    modality: str
    extracted_entries: int
    payload_bytes: int
    archives: int = 1


def _choose_temp_dir(sd_root: Path, expected_size: int | None) -> Path:
    """Prefere o temp do sistema; cai para o próprio SD se faltar espaço lá."""
    system_temp = Path(tempfile.gettempdir())
    needed = int((expected_size or 0) * _SAFETY_MARGIN)
    if expected_size is None or free_bytes(system_temp) > needed:
        return system_temp
    root = safe_resolve(sd_root)
    logger.info(
        "Espaço insuficiente em %s para o temporário (%s necessários); usando o próprio SD.",
        system_temp,
        human_size(needed),
    )
    return root


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
    download_url = resolve_url(package, settings)
    suffix = Path(package.label).suffix.casefold() or ".zip"
    try:
        handle, temp_name = tempfile.mkstemp(
            prefix=_TEMP_PREFIX, suffix=suffix, dir=str(temp_dir)
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
            download_url, stream=True, timeout=settings.http_timeout
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
    logger.info("Download concluído: %s (%s).", package.label, human_size(received))
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
            "Pacote sem sha256 — integridade NÃO verificada para %s.",
            package.label,
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


def resolve_url(package: PackageInfo, settings: Settings) -> str:
    """Devolve a URL de download do pacote.

    Pacote sem ``url`` traz ``quickkey``: o link é resolvido agora, não na
    inspeção, porque o link direto do MediaFire é temporário e pode expirar
    entre "verificar atualização" e "atualizar cartão".
    """
    if package.url:
        return package.url
    if not package.quickkey:
        raise InstallError(
            f"O pacote '{package.label}' não tem URL nem identificador para "
            "resolver o download."
        )
    return mediafire.resolve_download_url(package.quickkey, settings)


def extract_payload(
    archive_path: Path,
    sd_root: Path,
    progress: ExtractProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> int:
    """Extrai o pacote sobre a raiz do SD. Aceita ``.7z`` e ``.zip``."""
    return extract_archive(archive_path, sd_root, progress, should_cancel)


def ensure_space(sd_root: Path, packages: Sequence[PackageInfo]) -> None:
    """Verifica espaço antes de apagar ou baixar qualquer coisa.

    Descobrir no meio da extração que o cartão encheu é o pior momento possível:
    o SD já está sem as pastas antigas e sem as novas. A conta considera o pacote
    comprimido e o conteúdo extraído convivendo no cartão.
    """
    sizes = [p.size_bytes for p in packages if p.size_bytes]
    if not sizes:
        return

    compressed = sum(sizes)
    needed = int(compressed * INSTALL_SIZE_RATIO)
    available = free_bytes(safe_resolve(sd_root))
    if available and available < needed:
        raise InstallError(
            f"Espaço insuficiente: o pacote tem {human_size(compressed)} e a "
            f"instalação precisa de cerca de {human_size(needed)}, mas há apenas "
            f"{human_size(available)} livres no cartão. Libere espaço ou escolha "
            "a modalidade menor."
        )


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
    """Instala um arquivo e grava a versão. Atalho de :func:`install_packages`."""
    return install_packages(
        (package,),
        version,
        sd_root,
        settings,
        released,
        download_progress,
        extract_progress,
        should_cancel,
    )


def install_packages(
    packages: Sequence[PackageInfo],
    version: str,
    sd_root: Path,
    settings: Settings,
    released: date | None = None,
    download_progress: DownloadProgress | None = None,
    extract_progress: ExtractProgress | None = None,
    should_cancel: CancelCheck | None = None,
    on_archive_start: ArchiveStart | None = None,
) -> InstallResult:
    """Baixa e extrai todos os arquivos da modalidade, na ordem recebida.

    A versão só é gravada depois do último arquivo: ``packetVersion.txt`` é o
    estado do cartão, e escrevê-lo no meio faria o cartão declarar uma versão
    que ainda não está inteira lá.

    O temporário de cada arquivo é removido antes de baixar o próximo, para não
    exigir espaço para todos os pacotes ao mesmo tempo.
    """
    if not packages:
        raise InstallError("Nenhum arquivo para instalar nesta modalidade.")

    total_archives = len(packages)
    entries = payload_bytes = 0

    for index, package in enumerate(packages, start=1):
        if should_cancel is not None and should_cancel():
            raise OperationCancelled("Instalação cancelada pelo usuário.")
        if on_archive_start is not None:
            on_archive_start(index, total_archives, package.label)

        archive_path, received = download_payload(
            package, settings, sd_root, download_progress, should_cancel
        )
        try:
            entries += extract_archive(
                archive_path, sd_root, extract_progress, should_cancel
            )
            payload_bytes += received
        finally:
            _discard(archive_path)

    write_version_file(sd_root, version, released)

    return InstallResult(
        version=version,
        released=released,
        modality=packages[0].modality,
        extracted_entries=entries,
        payload_bytes=payload_bytes,
        archives=total_archives,
    )
