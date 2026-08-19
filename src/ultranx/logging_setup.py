"""Configuração de logging da aplicação.

Em build ``--noconsole`` do PyInstaller não existe stdout utilizável, portanto o
arquivo é a única trilha de auditoria. O log fica no perfil do usuário (nunca no
SD, que pode ser desconectado a qualquer momento) e é copiado para o cartão sob
demanda por :mod:`ultranx.core.recovery`.
"""

from __future__ import annotations

import logging
import sys
from logging.handlers import RotatingFileHandler
from pathlib import Path

from .config import APP_NAME, APP_VERSION

LOG_DIR_NAME = ".ultranx"
LOG_FILE_NAME = "ultranx.log"
_MAX_BYTES = 2 * 1024 * 1024
_BACKUP_COUNT = 3
_FORMAT = "%(asctime)s | %(levelname)-8s | %(name)s | %(message)s"


def log_directory() -> Path:
    """Diretório de logs no perfil do usuário, criado se necessário."""
    directory = Path.home() / LOG_DIR_NAME / "logs"
    directory.mkdir(parents=True, exist_ok=True)
    return directory


def log_file_path() -> Path:
    return log_directory() / LOG_FILE_NAME


def configure_logging(verbose: bool = False) -> Path:
    """Instala handlers idempotentemente e devolve o caminho do arquivo de log.

    Falha de escrita não derruba o app: nesse caso apenas o handler de stream é
    mantido, porque não poder logar é menos grave que não poder atualizar o SD.
    """
    root = logging.getLogger()
    root.setLevel(logging.DEBUG if verbose else logging.INFO)

    for handler in list(root.handlers):
        root.removeHandler(handler)
        handler.close()

    formatter = logging.Formatter(_FORMAT)
    target = log_file_path()

    try:
        file_handler = RotatingFileHandler(
            target,
            maxBytes=_MAX_BYTES,
            backupCount=_BACKUP_COUNT,
            encoding="utf-8",
        )
        file_handler.setFormatter(formatter)
        root.addHandler(file_handler)
    except (OSError, PermissionError) as exc:
        print(f"[{APP_NAME}] log em arquivo indisponível: {exc}", file=sys.stderr)

    stream_handler = logging.StreamHandler(sys.stderr)
    stream_handler.setFormatter(formatter)
    root.addHandler(stream_handler)

    # requests/urllib3 em DEBUG vazam URLs e headers; manter em WARNING.
    logging.getLogger("urllib3").setLevel(logging.WARNING)

    logging.getLogger(__name__).info(
        "%s %s iniciado (log: %s)", APP_NAME, APP_VERSION, target
    )
    return target
