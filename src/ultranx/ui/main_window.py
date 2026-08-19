"""Janela principal do UltraNX.

A UI nunca faz I/O bloqueante: toda operação de rede ou disco roda em
:class:`~ultranx.workers.version_worker.VersionWorker` ou
:class:`~ultranx.workers.update_worker.UpdateWorker` e volta por sinal.

Máquina de estados da interface:

``IDLE`` → detectar/selecionar SD → ``READY`` → verificar → ``INSPECTED``
→ atualizar → ``RUNNING`` → ``DONE`` | ``FAILED`` (volta a ``READY``).
"""

from __future__ import annotations

import logging
from pathlib import Path

from PyQt6.QtCore import Qt
from PyQt6.QtWidgets import (
    QComboBox,
    QFileDialog,
    QGroupBox,
    QHBoxLayout,
    QLabel,
    QLineEdit,
    QMainWindow,
    QMessageBox,
    QProgressBar,
    QPushButton,
    QTextEdit,
    QVBoxLayout,
    QWidget,
)

from ..config import (
    APP_NAME,
    APP_VERSION,
    MODALITY_LABELS,
    load_settings,
    save_base_url,
)
from ..core import recovery
from ..core.archives import free_bytes
from ..core.dates import format_date
from ..core.drive_detector import (
    DriveCandidate,
    LocalState,
    scan_removable_drives,
    validate_manual_root,
)
from ..core.errors import DriveError, UltraNXError
from ..core.installer import INSTALL_SIZE_RATIO, InstallResult
from ..core.paths import human_size
from ..core.progress import RateEstimator, format_duration
from ..core.recovery import FailureReport
from ..core.sanitizer import CleanupPlan, build_plan
from ..core.version_inspector import VersionReport
from ..workers.update_worker import UpdateWorker
from ..workers.version_worker import VersionWorker

logger = logging.getLogger(__name__)


class MainWindow(QMainWindow):
    """Janela única: seleção de mídia, verificação e execução da atualização."""

    def __init__(self) -> None:
        super().__init__()
        self._settings = load_settings()
        self._candidates: tuple[DriveCandidate, ...] = ()
        self._report: VersionReport | None = None
        self._version_worker: VersionWorker | None = None
        self._update_worker: UpdateWorker | None = None
        # Cronômetro do processo inteiro: cada etapa já estima o seu próprio
        # tempo restante, mas o usuário também quer saber quanto já correu.
        self._overall = RateEstimator()

        self.setWindowTitle(f"{APP_NAME} {APP_VERSION}")
        self.setMinimumSize(680, 760)
        self._build_ui()
        self.refresh_drives()

    # --- construção ---------------------------------------------------------

    def _build_ui(self) -> None:
        root = QWidget(self)
        layout = QVBoxLayout(root)

        layout.addWidget(self._build_server_group())
        layout.addWidget(self._build_drive_group())
        layout.addWidget(self._build_version_group())
        layout.addWidget(self._build_progress_group())

        self.log_view = QTextEdit(readOnly=True)
        self.log_view.setPlaceholderText("O andamento da operação aparece aqui.")
        layout.addWidget(self.log_view, stretch=1)

        self.setCentralWidget(root)

    def _build_server_group(self) -> QGroupBox:
        """Campo do servidor: o binário é distribuído sem URL embutida."""
        group = QGroupBox("1. Servidor de pacotes", self)
        row = QHBoxLayout(group)

        self.server_edit = QLineEdit()
        self.server_edit.setPlaceholderText("https://servidor.exemplo/ultranx")
        if self._settings.is_configured:
            self.server_edit.setText(self._settings.base_url)
        self.server_edit.returnPressed.connect(self.save_server_url)
        row.addWidget(self.server_edit, stretch=1)

        self.save_server_button = QPushButton("Salvar")
        self.save_server_button.clicked.connect(self.save_server_url)
        row.addWidget(self.save_server_button)

        return group

    def _build_drive_group(self) -> QGroupBox:
        group = QGroupBox("2. Cartão SD", self)
        row = QHBoxLayout(group)

        self.drive_combo = QComboBox()
        self.drive_combo.setMinimumWidth(320)
        self.drive_combo.currentIndexChanged.connect(self._on_drive_changed)
        row.addWidget(self.drive_combo, stretch=1)

        self.refresh_button = QPushButton("Detectar")
        self.refresh_button.clicked.connect(self.refresh_drives)
        row.addWidget(self.refresh_button)

        self.browse_button = QPushButton("Selecionar pasta…")
        self.browse_button.clicked.connect(self.choose_root_manually)
        row.addWidget(self.browse_button)

        return group

    def _build_version_group(self) -> QGroupBox:
        group = QGroupBox("3. Versão e modalidade", self)
        layout = QVBoxLayout(group)

        self.version_label = QLabel(self._version_text())
        self.version_label.setWordWrap(True)
        layout.addWidget(self.version_label)

        row = QHBoxLayout()
        self.check_button = QPushButton("Verificar atualização")
        self.check_button.clicked.connect(self.check_version)
        row.addWidget(self.check_button)

        self.modality_combo = QComboBox()
        self.modality_combo.setEnabled(False)
        self.modality_combo.currentIndexChanged.connect(self._on_modality_changed)
        row.addWidget(self.modality_combo, stretch=1)
        layout.addLayout(row)

        # Tudo que o usuário precisa para decidir antes de apagar o cartão:
        # arquivos, tamanhos, integridade, espaço e tempo estimado.
        self.details_view = QTextEdit(readOnly=True)
        self.details_view.setPlaceholderText(
            "Os detalhes do pacote aparecem aqui depois de verificar."
        )
        # Alto o bastante para o relatório da modalidade maior caber sem rolar.
        self.details_view.setMinimumHeight(230)
        layout.addWidget(self.details_view)

        return group

    def _build_progress_group(self) -> QGroupBox:
        group = QGroupBox("4. Atualização", self)
        layout = QVBoxLayout(group)

        self.stage_label = QLabel("Aguardando.")
        self.stage_label.setWordWrap(True)
        layout.addWidget(self.stage_label)

        self.elapsed_label = QLabel("")
        layout.addWidget(self.elapsed_label)

        self.progress = QProgressBar()
        self.progress.setRange(0, 100)
        self.progress.setValue(0)
        layout.addWidget(self.progress)

        row = QHBoxLayout()
        self.update_button = QPushButton("Atualizar cartão")
        self.update_button.setEnabled(False)
        self.update_button.clicked.connect(self.start_update)
        row.addWidget(self.update_button)

        self.cancel_button = QPushButton("Cancelar")
        self.cancel_button.setEnabled(False)
        self.cancel_button.clicked.connect(self.cancel_update)
        row.addWidget(self.cancel_button)
        layout.addLayout(row)

        return group

    # --- helpers ------------------------------------------------------------

    def _append(self, message: str) -> None:
        self.log_view.append(message)
        logger.info("UI: %s", message)

    def _version_text(
        self,
        local: LocalState | None = None,
        report: VersionReport | None = None,
    ) -> str:
        """Monta o texto de versões com as datas de cada lado.

        Da versão instalada mostra tanto a data de lançamento quanto a data em
        que foi gravada no cartão — são coisas diferentes, e a segunda é a única
        disponível num cartão atualizado à mão.
        """
        state = report.local if report is not None else local

        if state is None or state.version is None:
            installed = "Versão instalada: não instalada"
        else:
            details = [f"lançada em {format_date(state.released)}"]
            if state.installed_at is not None:
                details.append(f"gravada em {format_date(state.installed_at)}")
            installed = f"Versão instalada: {state.version} ({', '.join(details)})"

        if report is None:
            published = "Versão publicada: —"
        else:
            published = (
                f"Versão publicada: {report.remote_version} "
                f"(lançada em {format_date(report.remote_released)})"
            )
        return f"{installed}\n{published}"

    @property
    def selected_root(self) -> Path | None:
        index = self.drive_combo.currentIndex()
        if index < 0 or index >= len(self._candidates):
            return None
        return self._candidates[index].mountpoint

    def _set_candidates(self, candidates: tuple[DriveCandidate, ...]) -> None:
        """Repovoa o combo sem disparar handlers intermediários."""
        self._candidates = candidates
        self.drive_combo.blockSignals(True)
        self.drive_combo.clear()
        for candidate in candidates:
            self.drive_combo.addItem(candidate.label)
        self.drive_combo.blockSignals(False)
        if candidates:
            self.drive_combo.setCurrentIndex(0)
            self._on_drive_changed(0)

    def _reset_version_state(self) -> None:
        self._report = None
        self.details_view.clear()
        self.modality_combo.clear()
        self.modality_combo.setEnabled(False)
        self.update_button.setEnabled(False)
        self.progress.setValue(0)
        self.stage_label.setText("Aguardando.")

    # --- slots: servidor ----------------------------------------------------

    def save_server_url(self) -> None:
        """Persiste a URL informada e recarrega as configurações."""
        try:
            target = save_base_url(self.server_edit.text())
        except ValueError as error:
            QMessageBox.warning(self, "Endereço inválido", str(error))
            return
        except OSError as error:
            QMessageBox.warning(
                self,
                "Não foi possível salvar",
                f"Falha ao gravar a configuração: {error}",
            )
            return

        self._settings = load_settings()
        self._reset_version_state()
        self.server_edit.setText(self._settings.base_url)
        self._append(f"Servidor salvo em {target}: {self._settings.base_url}")
        if self._settings.base_url.startswith("http://"):
            self._append(
                "AVISO: endereço sem HTTPS. O download fica exposto a alteração "
                "no caminho; use https:// se o servidor suportar."
            )

    # --- slots: mídia -------------------------------------------------------

    def refresh_drives(self) -> None:
        """Varre mídias removíveis e atualiza o combo."""
        self._reset_version_state()
        candidates = scan_removable_drives()
        self._set_candidates(candidates)

        if not candidates:
            self._append(
                "Nenhuma mídia removível FAT32/exFAT encontrada. Use "
                "'Selecionar pasta…' para indicar a raiz do cartão."
            )
            return
        self._append(f"{len(candidates)} mídia(s) detectada(s).")

    def choose_root_manually(self) -> None:
        """Fallback manual via ``QFileDialog`` quando a detecção não resolve."""
        chosen = QFileDialog.getExistingDirectory(
            self, "Selecione a raiz do cartão SD do Switch"
        )
        if not chosen:
            return
        try:
            candidate = validate_manual_root(Path(chosen))
        except DriveError as error:
            QMessageBox.warning(self, "Cartão inválido", f"{error}\n\n{error.guidance}")
            return

        self._reset_version_state()
        self._set_candidates((candidate, *self._candidates))
        self._append(f"Raiz selecionada manualmente: {candidate.mountpoint}")

    def _on_drive_changed(self, index: int) -> None:
        if index < 0 or index >= len(self._candidates):
            return
        candidate = self._candidates[index]
        self._reset_version_state()
        self.version_label.setText(self._version_text(local=candidate.local_state))
        if candidate.total_bytes:
            self._append(
                f"{candidate.mountpoint}: {human_size(candidate.free_bytes)} livres "
                f"de {human_size(candidate.total_bytes)}."
            )

    # --- slots: versão ------------------------------------------------------

    def check_version(self) -> None:
        root = self.selected_root
        if root is None:
            QMessageBox.information(
                self, APP_NAME, "Selecione o cartão SD antes de verificar."
            )
            return

        self._settings = load_settings()
        if not self._settings.is_configured:
            QMessageBox.information(
                self,
                "Servidor não configurado",
                "Informe o endereço do servidor de pacotes no campo 1 e clique "
                "em Salvar antes de verificar.",
            )
            self.server_edit.setFocus()
            return

        self.check_button.setEnabled(False)
        self.stage_label.setText("Consultando servidor…")

        worker = VersionWorker(root, self._settings, parent=self)
        worker.started_stage.connect(self.stage_label.setText)
        worker.finished_ok.connect(self._on_version_ready)
        worker.failed.connect(self._on_failure)
        worker.finished.connect(lambda: self.check_button.setEnabled(True))
        self._version_worker = worker
        worker.start()

    def _on_version_ready(self, report: VersionReport) -> None:
        self._report = report
        self.version_label.setText(self._version_text(report=report))

        self.modality_combo.blockSignals(True)
        self.modality_combo.clear()
        for modality in report.available_modalities:
            self.modality_combo.addItem(MODALITY_LABELS.get(modality, modality), modality)
        self.modality_combo.blockSignals(False)
        self.modality_combo.setEnabled(bool(report.available_modalities))
        self.update_button.setEnabled(bool(report.available_modalities))
        self._on_modality_changed()

        if not report.manifest_available:
            self._append(
                "AVISO: o servidor não publicou checksums — o download não "
                "poderá ser validado."
            )
        if report.is_downgrade:
            self._append(
                f"AVISO: a versão publicada ({report.remote_version}) é anterior "
                f"à instalada ({report.local_version})."
            )
        elif report.update_available:
            self._append(f"Atualização disponível: {report.remote_version}.")
        else:
            self._append("O cartão já está na versão publicada.")
        self.stage_label.setText("Pronto para atualizar.")

    def _on_modality_changed(self, index: int = -1) -> None:  # noqa: ARG002
        """Repopula o painel de detalhes para a modalidade selecionada."""
        self.details_view.setPlainText(self._details_text())

    def _details_text(self) -> str:
        """Monta o relatório da modalidade: arquivos, integridade, espaço e tempo.

        Tudo o que decide se vale apertar "Atualizar cartão" fica visível ANTES
        do clique — inclusive o veredito de espaço, que é o que impede começar
        uma instalação que não tem como terminar.
        """
        report = self._report
        modality = self.modality_combo.currentData()
        if report is None or not modality:
            return ""

        try:
            packages = report.packages_for(modality)
        except UltraNXError as error:
            return str(error)

        titulo = MODALITY_LABELS.get(modality, modality)
        lines = [f"{titulo} — {len(packages)} arquivo(s)"]
        for package in packages:
            size = human_size(package.size_bytes) if package.size_bytes else "tamanho ?"
            check = (
                f"SHA-256 {package.sha256[:12]}…" if package.sha256 else "SEM checksum"
            )
            lines.append(f"  • {package.label}  ({size}, {check})")

        total = report.total_bytes_for(modality)
        if total:
            lines.append(f"\nTotal a baixar: {human_size(total)}")

        root = self.selected_root
        if root is not None and total:
            free = free_bytes(root)
            needed = int(total * INSTALL_SIZE_RATIO)
            lines.append(
                f"Espaço: {human_size(free)} livres | "
                f"~{human_size(needed)} necessários na instalação"
            )
            lines.append(
                "  cabe no cartão"
                if free >= needed
                else "  NÃO CABE — escolha a modalidade menor ou libere espaço"
            )

        if total:
            lines.append("\nTempo estimado de download:")
            for label, mbps in (("lenta", 1.0), ("média", 5.0), ("rápida", 20.0)):
                seconds = total / (mbps * 1024 * 1024)
                lines.append(
                    f"  conexão {label} ({mbps:.0f} MB/s): {format_duration(seconds)}"
                )
            lines.append(
                "  A estimativa real aparece na barra assim que o download começa."
            )

        if not all(package.sha256 for package in packages):
            lines.append(
                "\nAVISO: nem todos os arquivos têm checksum publicado; a "
                "integridade desses não poderá ser verificada."
            )
        return "\n".join(lines)

    # --- slots: atualização -------------------------------------------------

    @staticmethod
    def _summarize_plan(plan: CleanupPlan, root: Path) -> tuple[str, str, str]:
        """Resume o plano em ``(remoções, preservações, lista completa)``.

        Agrupa por pasta-pai em vez de listar tudo em fila: num cartão real são
        dezenas de itens dentro de ``switch/``, e uma lista corrida cresce muito
        além da altura da tela — o diálogo é a trava de segurança, precisa caber
        nela. A lista item a item vai para "Mostrar detalhes".
        """
        root_items = [item for item in plan.items if item.path.parent == root]
        nested: dict[str, int] = {}
        for item in plan.items:
            if item.path.parent != root:
                nested[item.path.parent.name] = nested.get(item.path.parent.name, 0) + 1

        removal_lines = [f"  • {item.path.name}" for item in root_items]
        removal_lines += [
            f"  • {parent}/ — {count} item(ns) de dentro (a pasta permanece)"
            for parent, count in sorted(nested.items())
        ]
        removals = "\n".join(removal_lines) or "  (nada a remover)"

        # O que é preservado sai do plano real, não de texto fixo.
        root_kept = sorted(
            (path.name for path in plan.preserved if path.parent == root),
            key=str.casefold,
        )
        nested_kept = sorted(
            (
                f"{path.parent.name}/{path.name}"
                for path in plan.preserved
                if path.parent != root
            ),
            key=str.casefold,
        )
        preserved_lines = []
        if nested_kept:
            preserved_lines.append(
                "  • dado seu dentro das pastas limpas: " + ", ".join(nested_kept)
            )
        if root_kept:
            preserved_lines.append(
                f"  • {len(root_kept)} itens na raiz: " + ", ".join(root_kept)
            )
        preserved = "\n".join(preserved_lines) or "  (nada a preservar)"

        detailed = "REMOVER:\n" + "\n".join(
            f"  {item.path.relative_to(root)}  ({item.reason})" for item in plan.items
        )
        detailed += "\n\nPRESERVAR:\n" + "\n".join(
            f"  {path.relative_to(root)}" for path in plan.preserved
        )
        return removals, preserved, detailed

    def _confirm_update(self, root: Path) -> bool:
        plan = build_plan(root)
        removals, preserved, detailed = self._summarize_plan(plan, root)

        box = QMessageBox(self)
        box.setIcon(QMessageBox.Icon.Warning)
        box.setWindowTitle("Confirmar atualização")
        box.setText(
            f"Isto vai apagar {len(plan.items)} item(ns) de {root} antes de "
            "instalar a versão nova."
        )
        box.setInformativeText(
            f"Será removido:\n{removals}\n\n"
            f"Será preservado:\n{preserved}\n\n"
            "A remoção é necessária porque sobrescrever não basta: o mesmo nome "
            "de arquivo pode carregar conteúdo de outra versão.\n\nContinuar?"
        )
        box.setDetailedText(detailed)
        box.setStandardButtons(
            QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No
        )
        box.setDefaultButton(QMessageBox.StandardButton.No)
        return box.exec() == QMessageBox.StandardButton.Yes

    def start_update(self) -> None:
        root, report = self.selected_root, self._report
        if root is None or report is None:
            QMessageBox.information(
                self, APP_NAME, "Verifique a versão antes de atualizar."
            )
            return

        modality = self.modality_combo.currentData()
        if not modality:
            QMessageBox.information(self, APP_NAME, "Selecione uma modalidade.")
            return

        try:
            packages = report.packages_for(modality)
        except Exception as error:  # noqa: BLE001 - mensagem já é acionável
            QMessageBox.warning(self, "Modalidade indisponível", str(error))
            return

        if not self._confirm_update(root):
            self._append("Atualização cancelada pelo usuário antes de iniciar.")
            return

        self._set_running(True)
        self._overall.reset()
        self.elapsed_label.setText("Tempo decorrido: poucos segundos")
        worker = UpdateWorker(
            root,
            packages,
            report.remote_version,
            self._settings,
            released=report.remote_released,
            parent=self,
        )
        worker.stage_changed.connect(self.stage_label.setText)
        worker.progress_changed.connect(self._on_progress)
        worker.finished_ok.connect(self._on_update_done)
        worker.failed.connect(self._on_failure)
        worker.finished.connect(lambda: self._set_running(False))
        self._update_worker = worker
        worker.start()

    def cancel_update(self) -> None:
        if self._update_worker is not None and self._update_worker.isRunning():
            self._update_worker.request_cancel()
            self.stage_label.setText("Cancelando após a etapa atual…")
            self.cancel_button.setEnabled(False)

    def _set_running(self, running: bool) -> None:
        self.cancel_button.setEnabled(running)
        self.update_button.setEnabled(not running and self._report is not None)
        self.check_button.setEnabled(not running)
        self.refresh_button.setEnabled(not running)
        self.browse_button.setEnabled(not running)
        self.drive_combo.setEnabled(not running)
        self.server_edit.setEnabled(not running)
        self.save_server_button.setEnabled(not running)
        self.modality_combo.setEnabled(not running and self._report is not None)

    def _on_progress(self, percent: int, detail: str) -> None:
        self.progress.setValue(percent)
        self.stage_label.setText(detail)
        self._overall.update(percent, 100)
        self.elapsed_label.setText(
            f"Tempo decorrido: {format_duration(self._overall.elapsed())}"
        )

    def _on_update_done(self, result: InstallResult) -> None:
        total = format_duration(self._overall.elapsed())
        self.elapsed_label.setText(f"Concluído em {total}.")
        self.progress.setValue(100)
        self.stage_label.setText(f"Atualizado para {result.version}.")
        self._append(
            f"Concluído: {result.extracted_entries} arquivo(s), "
            f"{human_size(result.payload_bytes)} baixados "
            f"({MODALITY_LABELS.get(result.modality, result.modality)})."
        )
        QMessageBox.information(
            self,
            "Atualização concluída",
            f"O cartão está na versão {result.version}.\n\n{recovery.eject_guidance()}",
        )

    def _on_failure(self, report: FailureReport) -> None:
        self.stage_label.setText(f"Falha na etapa: {report.stage}")
        self._append(report.as_text())
        box = QMessageBox(self)
        box.setIcon(
            QMessageBox.Icon.Critical if report.sd_dirty else QMessageBox.Icon.Warning
        )
        box.setWindowTitle("Não foi possível concluir")
        box.setText(report.message)
        box.setInformativeText(report.guidance)
        box.setTextInteractionFlags(Qt.TextInteractionFlag.TextBrowserInteraction)
        box.exec()

    # --- ciclo de vida ------------------------------------------------------

    def closeEvent(self, event) -> None:  # noqa: N802 - override do Qt
        """Impede fechar a janela com gravação em andamento."""
        if self._update_worker is not None and self._update_worker.isRunning():
            answer = QMessageBox.question(
                self,
                "Operação em andamento",
                "Uma atualização está em execução. Fechar agora pode deixar o "
                "cartão em estado parcial.\n\nCancelar a operação e sair?",
                QMessageBox.StandardButton.Yes | QMessageBox.StandardButton.No,
                QMessageBox.StandardButton.No,
            )
            if answer != QMessageBox.StandardButton.Yes:
                event.ignore()
                return
            self._update_worker.request_cancel()
            self._update_worker.wait(15_000)
        event.accept()
