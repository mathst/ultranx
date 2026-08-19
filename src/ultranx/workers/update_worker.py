"""Worker de atualização — orquestra limpeza, download, extração e finalização.

Divide o progresso global (0–100) em faixas fixas por etapa para que a barra da
UI avance monotonicamente:

======  ==========================================
faixa   etapa
======  ==========================================
0–10    limpeza seletiva (Safe Sanitizer)
10–70   download em streaming
70–95   extração sobre a raiz
95–100  gravação e validação do packetVersion.txt
======  ==========================================

Cancelamento é cooperativo: :meth:`UpdateWorker.request_cancel` apenas levanta
uma flag; as funções de core a consultam entre chunks/entradas. Nunca usamos
``terminate()``, que deixaria o SD em estado indefinido.
"""

from __future__ import annotations

import logging
from datetime import date
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..config import Settings
from ..core import installer, recovery, sanitizer
from ..core.drive_detector import looks_like_switch_root
from ..core.errors import DriveDisconnectedError, OperationCancelled
from ..core.installer import InstallResult
from ..core.paths import human_size
from ..core.version_inspector import PackageInfo

logger = logging.getLogger(__name__)

_CLEAN_RANGE = (0, 10)
_DOWNLOAD_RANGE = (10, 70)
_EXTRACT_RANGE = (70, 95)
_FINALIZE_RANGE = (95, 100)


def _scaled(fraction: float, span: tuple[int, int]) -> int:
    """Mapeia uma fração 0.0–1.0 para dentro de uma faixa da barra global."""
    low, high = span
    clamped = min(max(fraction, 0.0), 1.0)
    return int(low + (high - low) * clamped)


class UpdateWorker(QThread):
    """Executa a atualização completa numa thread dedicada."""

    stage_changed = pyqtSignal(str)
    progress_changed = pyqtSignal(int, str)  # percentual, detalhe
    finished_ok = pyqtSignal(object)  # InstallResult
    failed = pyqtSignal(object)  # FailureReport

    def __init__(
        self,
        sd_root: Path,
        package: PackageInfo,
        version: str,
        settings: Settings,
        released: date | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._sd_root = Path(sd_root)
        self._package = package
        self._version = version
        self._released = released
        self._settings = settings
        self._cancelled = False
        self._stage = "preparação"

    # --- controle -----------------------------------------------------------

    def request_cancel(self) -> None:
        """Marca o cancelamento; seguro para chamar da thread da UI."""
        logger.info("Cancelamento solicitado durante a etapa '%s'.", self._stage)
        self._cancelled = True

    def _should_cancel(self) -> bool:
        return self._cancelled

    def _set_stage(self, stage: str, message: str) -> None:
        self._stage = stage
        self.stage_changed.emit(message)

    # --- callbacks de progresso ---------------------------------------------

    def _on_clean(self, index: int, total: int, name: str) -> None:
        fraction = index / total if total else 1.0
        self.progress_changed.emit(_scaled(fraction, _CLEAN_RANGE), f"Removendo {name}…")

    def _on_download(self, received: int, total: int | None) -> None:
        if total:
            detail = f"Baixando {human_size(received)} de {human_size(total)}"
            percent = _scaled(received / total, _DOWNLOAD_RANGE)
        else:
            detail = f"Baixando {human_size(received)}"
            percent = _DOWNLOAD_RANGE[0]
        self.progress_changed.emit(percent, detail)

    def _on_extract(self, index: int, total: int, name: str) -> None:
        fraction = index / total if total else 1.0
        self.progress_changed.emit(
            _scaled(fraction, _EXTRACT_RANGE), f"Extraindo {Path(name).name}…"
        )

    # --- execução -----------------------------------------------------------

    def _guard_media(self) -> None:
        """Revalida a presença do cartão antes de cada etapa destrutiva."""
        if not self._sd_root.is_dir():
            raise DriveDisconnectedError(
                f"A raiz '{self._sd_root}' não está mais acessível."
            )

    def run(self) -> None:  # noqa: D102 - contrato do QThread
        try:
            self._run_stages()
        except Exception as error:  # noqa: BLE001 - fronteira thread/UI
            if not isinstance(error, OperationCancelled):
                logger.exception("Falha na atualização (etapa: %s)", self._stage)
            report = recovery.build_failure_report(error, self._sd_root, self._stage)
            self.failed.emit(report)

    def _run_stages(self) -> None:
        self._guard_media()
        if not looks_like_switch_root(self._sd_root):
            logger.warning(
                "Raiz %s sem marcadores de Switch; prosseguindo como SD virgem.",
                self._sd_root,
            )

        # 1. Limpeza seletiva
        self._set_stage("limpeza", "Limpando pastas de sistema legadas…")
        plan = sanitizer.build_plan(self._sd_root)
        logger.info("%s", plan.describe())
        sanitizer.execute_plan(plan, self._on_clean, self._should_cancel)
        if self._should_cancel():
            raise OperationCancelled("Atualização cancelada após a limpeza.")

        # 2–4. Download, extração e gravação de versão
        self._set_stage("download", "Baixando o pacote…")
        self._guard_media()
        result: InstallResult = installer.install_payload(
            package=self._package,
            version=self._version,
            sd_root=self._sd_root,
            settings=self._settings,
            released=self._released,
            download_progress=self._on_download,
            extract_progress=self._on_extract,
            should_cancel=self._should_cancel,
        )

        # 5. Finalização
        self._set_stage("finalização", "Finalizando gravação no cartão…")
        self.progress_changed.emit(_FINALIZE_RANGE[0], "Confirmando gravação…")
        recovery.finalize_media(self._sd_root)
        self.progress_changed.emit(_FINALIZE_RANGE[1], "Concluído.")
        self.finished_ok.emit(result)
