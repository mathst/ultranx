"""Configuração estática e constantes do UltraNX.

Todas as estruturas expostas aqui são imutáveis (``frozenset`` / ``tuple`` /
``dataclass(frozen=True)``). Nenhum módulo deve mutar estes valores em runtime;
overrides são feitos via variáveis de ambiente lidas por :func:`load_settings`.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from typing import Final

APP_NAME: Final[str] = "UltraNX"
APP_VERSION: Final[str] = "0.1.0"

# --- Arquivo de estado no SD -------------------------------------------------
# Única fonte de verdade sobre a versão instalada. Não há banco de dados.
VERSION_FILE_NAME: Final[str] = "packetVersion.txt"

# --- Endpoint remoto ---------------------------------------------------------
# Placeholder. Configure via env ULTRANX_BASE_URL ou edite antes de distribuir.
DEFAULT_BASE_URL: Final[str] = "https://example.invalid/ultranx"
ENV_BASE_URL: Final[str] = "ULTRANX_BASE_URL"
ENV_TIMEOUT: Final[str] = "ULTRANX_HTTP_TIMEOUT"
ENV_INSECURE_SKIP_HASH: Final[str] = "ULTRANX_SKIP_HASH_CHECK"

REMOTE_VERSION_PATH: Final[str] = "packetVersion.txt"
REMOTE_MANIFEST_PATH: Final[str] = "manifest.json"

DEFAULT_HTTP_TIMEOUT: Final[float] = 30.0
DOWNLOAD_CHUNK_SIZE: Final[int] = 1024 * 256  # 256 KiB por chunk

# --- Modalidades de pacote ---------------------------------------------------
MODALITY_STANDARD: Final[str] = "standard"
MODALITY_FULL: Final[str] = "full"

MODALITY_LABELS: Final[dict[str, str]] = {
    MODALITY_STANDARD: "Pacote Padrão",
    MODALITY_FULL: "Pacote Completo (Android/Linux)",
}

# --- Assinaturas de identificação da raiz do Switch --------------------------
# Presença de qualquer um destes na raiz caracteriza um SD de Switch.
SWITCH_ROOT_MARKERS: Final[frozenset[str]] = frozenset(
    {
        "atmosphere",
        "bootloader",
        "switch",
        "nintendo",
        "emummc",
        VERSION_FILE_NAME.casefold(),
        "payload.bin",
    }
)

# --- Whitelist de preservação (Safe Sanitizer) -------------------------------
# INVARIANTE DE SEGURANÇA: nada abaixo pode ser removido pelo sanitizer, mesmo
# que também apareça em DELETE_DIRS. Nomes comparados em casefold.
PRESERVE_DIRS: Final[frozenset[str]] = frozenset(
    {
        "nintendo",  # saves/jogos do sysNAND
        "emummc",  # partição do emuMMC
        "tico",  # contém tico/roms
        "mods2",  # mods do usuário
        "themes",  # contém themes/ThemezerNX
        "mods",  # mods de jogos
        "atmosphere-mods",
        "saves",
        "backup",
        "backups",
        "jksv",  # backups de saves
        "ultranx-logs",  # logs desta ferramenta
    }
)

# Subcaminhos que devem ser preservados mesmo quando o pai é removível.
PRESERVE_SUBPATHS: Final[tuple[tuple[str, ...], ...]] = (
    ("tico", "roms"),
    ("themes", "themeznx"),
    ("themes", "themezernx"),
)

# Arquivos soltos na raiz preservados (binários standalone do usuário).
PRESERVE_ROOT_FILE_SUFFIXES: Final[frozenset[str]] = frozenset(
    {".nro", ".nsp", ".xci", ".nca", ".sav", ".jpg", ".png", ".log"}
)
PRESERVE_ROOT_FILES: Final[frozenset[str]] = frozenset(
    {
        "boot.dat",
        "hbmenu.nro",
    }
)

# --- Pastas legadas removidas antes da instalação ----------------------------
# Causam conflito pós-atualização do Atmosphere quando sobrescritas por cima.
DELETE_DIRS: Final[frozenset[str]] = frozenset(
    {
        "atmosphere",
        "bootloader",
        "switch",
        "config",
        "sept",
        "warmboot_mariko",
        "stratosphere",
    }
)

# Arquivos de raiz removidos (regravados pelo pacote novo).
DELETE_ROOT_FILES: Final[frozenset[str]] = frozenset(
    {
        "payload.bin",
        "reboot_payload.bin",
        VERSION_FILE_NAME.casefold(),
    }
)


@dataclass(frozen=True, slots=True)
class Settings:
    """Configuração resolvida em runtime (imutável)."""

    base_url: str
    http_timeout: float
    skip_hash_check: bool

    @property
    def version_url(self) -> str:
        return f"{self.base_url}/{REMOTE_VERSION_PATH}"

    @property
    def manifest_url(self) -> str:
        return f"{self.base_url}/{REMOTE_MANIFEST_PATH}"


def _read_float(name: str, fallback: float) -> float:
    """Lê um float de env var, ignorando valores inválidos ou não positivos."""
    raw = os.environ.get(name)
    if not raw:
        return fallback
    try:
        value = float(raw)
    except ValueError:
        return fallback
    return value if value > 0 else fallback


def _read_bool(name: str) -> bool:
    return os.environ.get(name, "").strip().casefold() in {"1", "true", "yes", "on"}


def load_settings() -> Settings:
    """Resolve as configurações a partir do ambiente.

    Retorna sempre uma nova instância; chamadas repetidas refletem mudanças de
    ambiente sem mutar estado global.
    """
    base_url = os.environ.get(ENV_BASE_URL, DEFAULT_BASE_URL).strip().rstrip("/")
    return Settings(
        base_url=base_url or DEFAULT_BASE_URL,
        http_timeout=_read_float(ENV_TIMEOUT, DEFAULT_HTTP_TIMEOUT),
        skip_hash_check=_read_bool(ENV_INSECURE_SKIP_HASH),
    )
