"""Worker de inspeção de versão.

Isola o I/O de rede do event loop da UI. Contrato de sinais: exatamente um de
``finished_ok`` ou ``failed`` é emitido, sempre a partir da thread do worker; a
UI conecta com o tipo default (``AutoConnection``), o que marshalla a entrega de
volta para a thread principal.
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..config import Settings
from ..core import version_inspector
from ..core.recovery import FailureReport, build_failure_report
from ..core.version_inspector import VersionReport

logger = logging.getLogger(__name__)

STAGE_NAME = "inspeção"


class VersionWorker(QThread):
    """Consulta a versão remota e compara com a local, fora da thread da UI."""

    started_stage = pyqtSignal(str)
    finished_ok = pyqtSignal(object)  # VersionReport
    failed = pyqtSignal(object)  # FailureReport

    def __init__(self, sd_root: Path, settings: Settings, parent=None) -> None:
        super().__init__(parent)
        self._sd_root = Path(sd_root)
        self._settings = settings

    def run(self) -> None:  # noqa: D102 - contrato do QThread
        self.started_stage.emit("Consultando versão publicada…")
        try:
            report: VersionReport = version_inspector.inspect(
                self._sd_root, self._settings
            )
        except Exception as error:  # noqa: BLE001 - fronteira thread/UI
            logger.exception("Falha na inspeção de versão")
            failure: FailureReport = build_failure_report(
                error, self._sd_root, STAGE_NAME
            )
            self.failed.emit(failure)
            return
        self.finished_ok.emit(report)
