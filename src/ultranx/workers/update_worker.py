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
from collections.abc import Sequence
from datetime import date
from pathlib import Path

from PyQt6.QtCore import QThread, pyqtSignal

from ..config import Settings
from ..core import installer, recovery, sanitizer
from ..core.drive_detector import looks_like_switch_root
from ..core.errors import DriveDisconnectedError, OperationCancelled
from ..core.installer import InstallResult
from ..core.paths import human_size
from ..core.progress import RateEstimator, format_duration, format_rate
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
        packages: Sequence[PackageInfo],
        version: str,
        settings: Settings,
        released: date | None = None,
        parent=None,
    ) -> None:
        super().__init__(parent)
        self._sd_root = Path(sd_root)
        self._packages = tuple(packages)
        self._version = version
        self._released = released
        self._settings = settings
        self._cancelled = False
        self._stage = "preparação"
        # Um estimador por etapa: as unidades são diferentes (itens, bytes,
        # entradas de ZIP) e a vazão de uma não prevê a da outra.
        self._clean_eta = RateEstimator()
        self._download_eta = RateEstimator()
        self._extract_eta = RateEstimator()

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
        eta = self._clean_eta.update(index, total)
        detail = f"Removendo {name}… ({index}/{total})"
        if eta is not None:
            detail += f" — restam {format_duration(eta)}"
        self.progress_changed.emit(_scaled(fraction, _CLEAN_RANGE), detail)

    def _on_download(self, received: int, total: int | None) -> None:
        eta = self._download_eta.update(received, total)
        rate = format_rate(self._download_eta.rate)
        if total:
            detail = f"Baixando {human_size(received)} de {human_size(total)}"
            percent = _scaled(received / total, _DOWNLOAD_RANGE)
        else:
            detail = f"Baixando {human_size(received)}"
            percent = _DOWNLOAD_RANGE[0]
        if rate:
            detail += f" a {rate}"
        if eta is not None:
            detail += f" — restam {format_duration(eta)}"
        self.progress_changed.emit(percent, detail)

    def _on_archive_start(self, index: int, total: int, name: str) -> None:
        # Cada arquivo tem vazão própria: reiniciar os estimadores evita que a
        # média do anterior contamine a estimativa do seguinte.
        self._download_eta.reset()
        self._extract_eta.reset()
        prefix = f"[{index}/{total}] " if total > 1 else ""
        self._set_stage("download", f"{prefix}Baixando {name}…")

    def _on_extract(self, index: int, total: int, name: str) -> None:
        fraction = index / total if total else 1.0
        eta = self._extract_eta.update(index, total)
        detail = f"Extraindo {Path(name).name}… ({index}/{total})"
        if eta is not None:
            detail += f" — restam {format_duration(eta)}"
        self.progress_changed.emit(_scaled(fraction, _EXTRACT_RANGE), detail)

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

        # Espaço conferido ANTES da limpeza: descobrir que o cartão encheu
        # depois de apagar as pastas antigas deixa o SD sem as duas versões.
        self._set_stage("verificação", "Conferindo espaço no cartão…")
        installer.ensure_space(self._sd_root, self._packages)

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
        result: InstallResult = installer.install_packages(
            packages=self._packages,
            version=self._version,
            sd_root=self._sd_root,
            settings=self._settings,
            released=self._released,
            download_progress=self._on_download,
            extract_progress=self._on_extract,
            should_cancel=self._should_cancel,
            on_archive_start=self._on_archive_start,
        )

        # 5. Finalização
        self._set_stage("finalização", "Finalizando gravação no cartão…")
        self.progress_changed.emit(_FINALIZE_RANGE[0], "Confirmando gravação…")
        recovery.finalize_media(self._sd_root)
        self.progress_changed.emit(_FINALIZE_RANGE[1], "Concluído.")
        self.finished_ok.emit(result)
