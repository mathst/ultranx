"""Extração de pacotes ``.7z`` e ``.zip`` sobre a raiz do cartão.

O pacote R O X é distribuído em 7-Zip, que o stdlib não abre — daí a dependência
``py7zr``. O suporte a ZIP continua porque é o formato mais fácil de servir num
host estático e não custa nada manter.

Invariante de segurança comum aos dois formatos: nenhuma entrada é gravada sem
passar por :func:`~ultranx.core.paths.join_within`, então caminho com ``..`` ou
absoluto (zip-slip) é descartado em vez de escapar da raiz.
"""

from __future__ import annotations

import logging
import shutil
import zipfile
from collections.abc import Callable
from pathlib import Path

from ..config import DOWNLOAD_CHUNK_SIZE
from .errors import (
    DriveDisconnectedError,
    InstallError,
    IntegrityError,
    OperationCancelled,
    PermissionDeniedError,
)
from .paths import join_within, safe_resolve

logger = logging.getLogger(__name__)

# (entradas_extraidas, entradas_totais, nome_atual)
ExtractProgress = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]

SUFFIX_ZIP = ".zip"
SUFFIX_7Z = ".7z"

# py7zr não extrai arquivo por arquivo com progresso confiável, então o 7z é
# extraído em lotes: dá progresso visível sem materializar tudo em memória.
_SEVENZIP_BATCH = 40


def supported_suffixes() -> tuple[str, ...]:
    return (SUFFIX_7Z, SUFFIX_ZIP)


def _target_for(root: Path, name: str) -> Path | None:
    """Resolve o destino de uma entrada, ou ``None`` se ela escaparia da raiz."""
    target = join_within(root, name)
    if target is None:
        logger.warning("GUARD: entrada '%s' fora da raiz; descartada.", name)
    return target


def _extract_zip(
    archive_path: Path,
    root: Path,
    progress: ExtractProgress | None,
    should_cancel: CancelCheck | None,
) -> int:
    written = 0
    with zipfile.ZipFile(archive_path) as archive:
        entries = [item for item in archive.infolist() if item.filename]
        total = len(entries)
        if total == 0:
            raise InstallError("O pacote baixado está vazio.")

        for index, entry in enumerate(entries, start=1):
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("Extração cancelada pelo usuário.")

            target = _target_for(root, entry.filename)
            if target is None:
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
    return written


def _extract_7z(
    archive_path: Path,
    root: Path,
    progress: ExtractProgress | None,
    should_cancel: CancelCheck | None,
) -> int:
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - dependência declarada
        raise InstallError(
            "Suporte a .7z indisponível: a biblioteca py7zr não está instalada."
        ) from exc

    written = 0
    with py7zr.SevenZipFile(archive_path, mode="r") as archive:
        names = [name for name in archive.getnames() if name]
        total = len(names)
        if total == 0:
            raise InstallError("O pacote baixado está vazio.")

        # Valida todos os caminhos ANTES de extrair: py7zr escreve direto no
        # disco, então a checagem tem de acontecer antes, não durante.
        safe: list[str] = []
        for name in names:
            if _target_for(root, name) is not None:
                safe.append(name)
        if not safe:
            raise IntegrityError(
                "Todas as entradas do pacote apontam para fora da raiz do cartão."
            )

        for start in range(0, len(safe), _SEVENZIP_BATCH):
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("Extração cancelada pelo usuário.")

            batch = safe[start : start + _SEVENZIP_BATCH]
            archive.extract(path=str(root), targets=batch)
            archive.reset()  # py7zr exige reset entre extrações parciais
            written += len(batch)

            if progress is not None:
                progress(min(written, total), total, batch[-1])
    return written


def extract_archive(
    archive_path: Path,
    sd_root: Path,
    progress: ExtractProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> int:
    """Extrai ``archive_path`` sobre ``sd_root``, sobrescrevendo o que colidir.

    O formato é escolhido pela extensão. Retorna o número de entradas gravadas e
    converte qualquer falha de OS na hierarquia de :mod:`ultranx.core.errors`.
    """
    root = safe_resolve(sd_root)
    suffix = archive_path.suffix.casefold()

    try:
        if suffix == SUFFIX_ZIP:
            written = _extract_zip(archive_path, root, progress, should_cancel)
        elif suffix == SUFFIX_7Z:
            written = _extract_7z(archive_path, root, progress, should_cancel)
        else:
            raise InstallError(
                f"Formato '{suffix or archive_path.name}' não suportado. "
                f"Formatos aceitos: {', '.join(supported_suffixes())}."
            )
    except zipfile.BadZipFile as exc:
        raise IntegrityError("O pacote baixado não é um ZIP válido.") from exc
    except (OperationCancelled, InstallError, IntegrityError):
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
    except Exception as exc:  # py7zr sinaliza arquivo corrompido de várias formas
        raise IntegrityError(
            f"O pacote baixado está corrompido ou ilegível: {exc}."
        ) from exc

    logger.info("Extração concluída: %d entrada(s) de %s.", written, archive_path.name)
    return written


def free_bytes(path: Path) -> int:
    """Espaço livre em bytes; ``0`` quando indeterminável."""
    try:
        return int(shutil.disk_usage(str(path)).free)
    except (OSError, ValueError):
        logger.debug("disk_usage indisponível para %s", path)
        return 0
