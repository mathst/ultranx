"""Ponto de entrada do UltraNX (``python -m ultranx`` e binário PyInstaller)."""

from __future__ import annotations

import argparse
import logging
import sys

from PyQt6.QtWidgets import QApplication, QMessageBox

from .config import APP_NAME, APP_VERSION, ENV_BASE_URL, load_settings
from .logging_setup import configure_logging


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
    if "example.invalid" in settings.base_url:
        logger.warning("Servidor não configurado: %s", settings.base_url)
        QMessageBox.warning(
            window,
            "Servidor não configurado",
            "Nenhum servidor de pacotes foi configurado.\n\nDefina a variável de "
            f"ambiente {ENV_BASE_URL} com a URL base do repositório do pacote "
            "antes de verificar atualizações.",
        )

    return app.exec()


if __name__ == "__main__":  # pragma: no cover - entrypoint
    sys.exit(main())
