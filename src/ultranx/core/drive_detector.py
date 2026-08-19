"""Drive Detector — varredura e validação de mídias removíveis.

Responsabilidade única: descobrir candidatos a "raiz de SD de Switch" e validar
um diretório escolhido manualmente. Não faz I/O de escrita e não conhece PyQt6 —
a UI decide quando abrir o ``QFileDialog`` de fallback.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from datetime import date
from pathlib import Path

import psutil

from ..config import SWITCH_ROOT_MARKERS, VERSION_FILE_NAME
from .dates import file_date, parse_iso_date
from .errors import DriveError
from .paths import safe_resolve

logger = logging.getLogger(__name__)

# Sistemas de arquivos aceitos. Windows reporta "FAT32"/"exFAT"; Linux reporta
# "vfat"/"exfat"/"msdos" via /proc/mounts.
_ACCEPTED_FSTYPES: frozenset[str] = frozenset(
    {"fat", "fat32", "vfat", "msdos", "exfat", "fuseblk"}
)
_REMOVABLE_OPTS: frozenset[str] = frozenset({"removable", "hotplug"})


@dataclass(frozen=True, slots=True)
class LocalState:
    """Estado gravado no cartão: versão, data de lançamento e de instalação.

    ``released`` vem da segunda linha de ``packetVersion.txt`` (ausente em
    cartões atualizados manualmente ou por versões antigas do UltraNX).
    ``installed_at`` é o mtime do arquivo — quando a gravação aconteceu.
    """

    version: str | None
    released: date | None
    installed_at: date | None

    @property
    def is_installed(self) -> bool:
        return self.version is not None


@dataclass(frozen=True, slots=True)
class DriveCandidate:
    """Uma mídia candidata, já com o veredito de "parece um SD de Switch"."""

    mountpoint: Path
    device: str
    fstype: str
    is_switch_root: bool
    local_state: LocalState
    total_bytes: int
    free_bytes: int

    @property
    def local_version(self) -> str | None:
        """Versão instalada, ou ``None`` em cartão virgem."""
        return self.local_state.version

    @property
    def label(self) -> str:
        """Rótulo curto para exibição em combo box."""
        marker = "SD Switch" if self.is_switch_root else "removível"
        version = f" — v{self.local_state.version}" if self.local_state.version else ""
        return f"{self.mountpoint} ({self.fstype}, {marker}){version}"


def _is_removable(partition: psutil._common.sdiskpart) -> bool:
    """Heurística de mídia removível cross-platform.

    Windows: ``opts`` contém ``removable``. Linux: ``opts`` raramente marca
    removível, então aceitamos qualquer FS da lista montado sob os pontos de
    montagem de mídia do usuário.
    """
    opts = {opt.strip().casefold() for opt in partition.opts.split(",") if opt.strip()}
    if opts & _REMOVABLE_OPTS:
        return True
    mount = partition.mountpoint.replace("\\", "/").casefold()
    return mount.startswith(("/media/", "/run/media/", "/mnt/", "/volumes/"))


def _fstype_accepted(fstype: str) -> bool:
    return fstype.strip().casefold() in _ACCEPTED_FSTYPES


def read_local_state(root: Path) -> LocalState:
    """Lê ``packetVersion.txt`` na raiz do SD.

    Jamais levanta: "sem versão" é um estado válido (SD virgem). Formato:

    .. code-block:: text

       1.4.2          <- linha 1: versão instalada
       2026-08-15     <- linha 2 (opcional): data de lançamento dessa versão
    """
    version_file = safe_resolve(root) / VERSION_FILE_NAME
    try:
        content = version_file.read_text(encoding="utf-8", errors="replace")
    except (OSError, ValueError):
        logger.debug("packetVersion.txt ausente ou ilegível em %s", root)
        return LocalState(None, None, None)

    lines = [line.strip() for line in content.splitlines()]
    version = lines[0] if lines and lines[0] else None
    released = parse_iso_date(lines[1]) if len(lines) > 1 else None
    installed_at = file_date(version_file) if version else None
    return LocalState(version=version, released=released, installed_at=installed_at)


def read_local_version(root: Path) -> str | None:
    """Atalho para a versão instalada. ``None`` em cartão virgem."""
    return read_local_state(root).version


def looks_like_switch_root(root: Path) -> bool:
    """``True`` se a raiz contém pelo menos um marcador conhecido do Switch."""
    resolved = safe_resolve(root)
    try:
        entries = {entry.name.casefold() for entry in resolved.iterdir()}
    except (OSError, PermissionError):
        logger.debug("Não foi possível listar %s", resolved)
        return False
    return bool(entries & SWITCH_ROOT_MARKERS)


def _describe(mountpoint: Path, device: str, fstype: str) -> DriveCandidate:
    total = free = 0
    try:
        usage = psutil.disk_usage(str(mountpoint))
        total, free = usage.total, usage.free
    except (OSError, PermissionError):
        logger.debug("disk_usage falhou para %s", mountpoint)
    return DriveCandidate(
        mountpoint=mountpoint,
        device=device,
        fstype=fstype,
        is_switch_root=looks_like_switch_root(mountpoint),
        local_state=read_local_state(mountpoint),
        total_bytes=total,
        free_bytes=free,
    )


def scan_removable_drives() -> tuple[DriveCandidate, ...]:
    """Varre partições e retorna candidatos removíveis com FS compatível.

    Candidatos que aparentam ser raiz de Switch vêm primeiro. Nunca levanta:
    ambiente sem mídia devolve tupla vazia.
    """
    try:
        partitions = psutil.disk_partitions(all=False)
    except (OSError, RuntimeError) as exc:
        logger.warning("psutil.disk_partitions falhou: %s", exc)
        return ()

    candidates: list[DriveCandidate] = []
    for partition in partitions:
        if not _fstype_accepted(partition.fstype) or not _is_removable(partition):
            continue
        mountpoint = safe_resolve(Path(partition.mountpoint))
        if not mountpoint.is_dir():
            continue
        candidates.append(_describe(mountpoint, partition.device, partition.fstype))

    candidates.sort(key=lambda c: (not c.is_switch_root, str(c.mountpoint)))
    logger.info("%d mídia(s) removível(is) detectada(s)", len(candidates))
    return tuple(candidates)


def detect_switch_root() -> DriveCandidate | None:
    """Retorna o único candidato que parece raiz de Switch, ou ``None``.

    Com zero ou mais de um candidato válido devolve ``None`` — a decisão fica
    com o usuário, que escolhe na UI ou via ``QFileDialog``.
    """
    switch_roots = [c for c in scan_removable_drives() if c.is_switch_root]
    return switch_roots[0] if len(switch_roots) == 1 else None


def validate_manual_root(path: Path | str) -> DriveCandidate:
    """Valida um diretório escolhido manualmente pelo usuário.

    Aceita raízes sem marcadores (SD virgem), mas exige diretório existente e
    gravável. Levanta :class:`DriveError` com orientação acionável.
    """
    root = safe_resolve(path)
    if not root.exists():
        raise DriveError(f"O caminho '{root}' não existe.")
    if not root.is_dir():
        raise DriveError(f"'{root}' não é um diretório.")

    probe = root / ".ultranx-write-test"
    try:
        probe.write_bytes(b"")
        probe.unlink()
    except (OSError, PermissionError) as exc:
        raise DriveError(
            f"Sem permissão de escrita em '{root}' ({exc.__class__.__name__})."
        ) from exc

    return _describe(root, device=str(root), fstype="manual")
