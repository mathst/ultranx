"""Recovery & Safety — preservação de log, relatório de falha e ejeção segura.

Nenhuma função aqui levanta exceção: este módulo roda justamente quando algo já
deu errado, e uma falha secundária (ex.: SD removido antes de copiar o log) não
pode mascarar o erro original.
"""

from __future__ import annotations

import logging
import os
import shutil
from dataclasses import dataclass
from pathlib import Path

from ..config import VERSION_FILE_NAME
from ..logging_setup import log_file_path
from .errors import (
    DriveDisconnectedError,
    IntegrityError,
    NetworkError,
    UltraNXError,
)
from .paths import safe_resolve

logger = logging.getLogger(__name__)

LOG_COPY_DIR = "ultranx-logs"  # está em PRESERVE_DIRS: o sanitizer nunca remove
_MAX_LOG_COPIES = 20


@dataclass(frozen=True, slots=True)
class FailureReport:
    """Relatório acionável apresentado ao usuário após uma falha."""

    stage: str
    message: str
    guidance: str
    sd_dirty: bool
    log_path: Path | None
    log_copy_path: Path | None

    def as_text(self) -> str:
        lines = [f"Falha na etapa: {self.stage}", "", self.message, "", self.guidance]
        if self.sd_dirty:
            lines += [
                "",
                "ATENÇÃO: o cartão pode estar em estado parcial. NÃO inicie o "
                "console antes de concluir uma atualização com sucesso.",
            ]
        if self.log_copy_path is not None:
            lines += ["", f"Log copiado para: {self.log_copy_path}"]
        elif self.log_path is not None:
            lines += ["", f"Log completo em: {self.log_path}"]
        return "\n".join(lines)


def _next_copy_path(directory: Path, stage: str) -> Path:
    slug = "".join(ch if ch.isalnum() else "-" for ch in stage.casefold()).strip("-")
    for index in range(1, _MAX_LOG_COPIES + 1):
        candidate = directory / f"ultranx-{slug or 'erro'}-{index:02d}.log"
        if not candidate.exists():
            return candidate
    return directory / f"ultranx-{slug or 'erro'}-{_MAX_LOG_COPIES:02d}.log"


def preserve_log(sd_root: Path | None, stage: str = "erro") -> Path | None:
    """Copia o log atual para ``<SD>/ultranx-logs/``.

    Retorna o caminho da cópia ou ``None`` quando não foi possível copiar (SD
    ausente, somente-leitura, log inexistente). Best effort por definição.
    """
    source = log_file_path()
    if not source.exists():
        return None
    if sd_root is None:
        return None

    root = safe_resolve(sd_root)
    if not root.is_dir():
        logger.info("Raiz %s indisponível; log preservado apenas no perfil.", root)
        return None

    try:
        directory = root / LOG_COPY_DIR
        directory.mkdir(parents=True, exist_ok=True)
        target = _next_copy_path(directory, stage)
        shutil.copy2(source, target)
    except (OSError, PermissionError) as exc:
        logger.warning("Não foi possível copiar o log para o cartão: %s", exc)
        return None

    logger.info("Log preservado em %s", target)
    return target


def _stage_leaves_sd_dirty(stage: str, error: BaseException) -> bool:
    """Heurística: falhas antes de escrever no SD não deixam estado parcial."""
    if isinstance(error, (NetworkError, IntegrityError)):
        # Ambas ocorrem antes de qualquer gravação na raiz.
        return stage.casefold() not in {"download", "inspeção", "inspecao"}
    return stage.casefold() in {
        "limpeza",
        "extração",
        "extracao",
        "finalização",
        "finalizacao",
    }


def build_failure_report(
    error: BaseException,
    sd_root: Path | None,
    stage: str,
) -> FailureReport:
    """Converte qualquer exceção num relatório com orientação de recuperação."""
    if isinstance(error, UltraNXError):
        message, guidance = error.message, error.guidance
    else:
        message = f"Erro inesperado: {error.__class__.__name__}: {error}"
        guidance = (
            "Consulte o log, mantenha o cartão conectado e execute o UltraNX "
            "novamente. Se o erro persistir, reporte o log aos mantenedores."
        )

    # Desconexão física sempre deixa o cartão suspeito, em qualquer etapa.
    sd_dirty = isinstance(error, DriveDisconnectedError) or _stage_leaves_sd_dirty(
        stage, error
    )

    copy_path = preserve_log(sd_root, stage)
    report = FailureReport(
        stage=stage,
        message=message,
        guidance=guidance,
        sd_dirty=sd_dirty,
        log_path=log_file_path(),
        log_copy_path=copy_path,
    )
    logger.error("Relatório de falha (%s): %s", stage, message)
    return report


def finalize_media(sd_root: Path) -> bool:
    """Força o flush do sistema de arquivos e libera handles antes da ejeção.

    Retorna ``True`` quando o flush foi confirmado. Em Windows não há syscall
    portátil de sync por volume, então validamos relendo o arquivo de versão —
    o que também confirma que o cartão ainda responde.
    """
    root = safe_resolve(sd_root)
    try:
        if hasattr(os, "sync"):
            os.sync()  # type: ignore[attr-defined]
        version_file = root / VERSION_FILE_NAME
        if version_file.exists():
            version_file.read_bytes()
    except (OSError, PermissionError) as exc:
        logger.warning("Finalização da mídia incompleta: %s", exc)
        return False
    logger.info("Mídia finalizada; pronta para ejeção segura.")
    return True


def eject_guidance() -> str:
    """Texto de orientação de ejeção segura, dependente da plataforma."""
    if os.name == "nt":
        return (
            "Feche esta janela, clique em 'Remover hardware com segurança' na "
            "bandeja do Windows e só então retire o cartão."
        )
    return (
        "Execute 'sync' e desmonte o cartão (botão de ejetar no gerenciador de "
        "arquivos ou 'umount') antes de retirá-lo."
    )
