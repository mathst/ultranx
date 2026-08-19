"""Ponto de entrada do UltraNX (``python -m ultranx`` e binário PyInstaller)."""

from __future__ import annotations

import argparse
import logging
import os
import sys
from pathlib import Path
from typing import TextIO

from PyQt6.QtWidgets import QApplication, QMessageBox

from .config import (
    APP_NAME,
    APP_VERSION,
    ENV_BASE_URL,
    load_settings,
    user_config_path,
)
from .logging_setup import configure_logging

# Mantém vivos os streams substitutos abertos por ensure_std_streams().
_DEVNULL_SINKS: list[TextIO] = []


def ensure_std_streams() -> None:
    """Garante ``sys.stdout``/``sys.stderr`` utilizáveis.

    Em build ``--noconsole`` do PyInstaller ambos são ``None``. Sem esta guarda,
    qualquer escrita (``--version`` do argparse, ``print``, ``StreamHandler``)
    levanta ``AttributeError`` e o PyInstaller abre um diálogo modal de
    traceback que trava a aplicação antes da janela aparecer.
    """
    for name in ("stdout", "stderr"):
        if getattr(sys, name, None) is None:
            # Sem context manager de propósito: o stream tem de viver enquanto o
            # processo viver. A referência em _DEVNULL_SINKS impede que o
            # coletor de lixo o feche.
            sink = Path(os.devnull).open("w", encoding="utf-8", errors="replace")  # noqa: SIM115
            _DEVNULL_SINKS.append(sink)
            setattr(sys, name, sink)


def _parse_args(argv: list[str]) -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        prog="ultranx",
        description=(
            "Atualizador do pacote R O X para cartões SD de Nintendo Switch. "
            f"Configure o servidor pela variável de ambiente {ENV_BASE_URL}."
        ),
    )
    parser.add_argument(
        "--version", action="version", version=f"{APP_NAME} {APP_VERSION}"
    )
    parser.add_argument("--verbose", action="store_true", help="Registra em nível DEBUG.")
    return parser.parse_args(argv)


def main(argv: list[str] | None = None) -> int:
    """Inicializa logging, Qt e a janela principal. Retorna o código de saída."""
    ensure_std_streams()
    args = _parse_args(list(sys.argv[1:] if argv is None else argv))
    configure_logging(verbose=args.verbose)
    logger = logging.getLogger(__name__)

    app = QApplication([sys.argv[0]])
    app.setApplicationName(APP_NAME)
    app.setApplicationVersion(APP_VERSION)

    # Import tardio: mantém o traceback de PyQt6 ausente legível no CLI.
    from .ui.main_window import MainWindow

    window = MainWindow()
    window.show()

    settings = load_settings()
    if not settings.is_configured:
        logger.warning("Servidor não configurado: %s", settings.base_url)
        QMessageBox.information(
            window,
            "Primeiro uso: configure o servidor",
            "Informe o endereço do servidor de pacotes no campo 1 da janela e "
            "clique em Salvar.\n\nO endereço fica guardado em "
            f"{user_config_path()} e não precisa ser digitado de novo.\n\n"
            f"Também dá para apontar por variável de ambiente ({ENV_BASE_URL}) "
            "ou por um arquivo ultranx.json ao lado do executável.",
        )

    return app.exec()


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
