"""Configuração estática e constantes do UltraNX.

As estruturas de whitelist e remoção são imutáveis (``frozenset`` / ``tuple`` /
``dataclass(frozen=True)``) e nenhum módulo deve mutá-las em runtime.

O endereço do servidor é resolvido em três camadas, da mais forte para a mais
fraca: variável de ambiente, arquivo de configuração, placeholder embutido. O
arquivo existe porque o binário é distribuído pronto: exigir que o usuário final
defina variável de ambiente no Windows antes de abrir o app não é opção.
"""

from __future__ import annotations

import json
import logging
import os
import re
import sys
from dataclasses import dataclass
from pathlib import Path
from typing import Final

logger = logging.getLogger(__name__)

APP_NAME: Final[str] = "UltraNX"
APP_VERSION: Final[str] = "0.1.0"

# --- Arquivo de estado no SD -------------------------------------------------
# Única fonte de verdade sobre a versão instalada. Não há banco de dados.
VERSION_FILE_NAME: Final[str] = "packetVersion.txt"

# --- Endpoint remoto ---------------------------------------------------------
# Placeholder: sem configuração, o app avisa na tela em vez de tentar baixar.
DEFAULT_BASE_URL: Final[str] = (
    "https://www.mediafire.com/folder/5zz3azv8dk409/Nintendo+Switch"
)
PLACEHOLDER_HOST: Final[str] = "example.invalid"
ENV_BASE_URL: Final[str] = "ULTRANX_BASE_URL"
ENV_TIMEOUT: Final[str] = "ULTRANX_HTTP_TIMEOUT"
ENV_INSECURE_SKIP_HASH: Final[str] = "ULTRANX_SKIP_HASH_CHECK"

CONFIG_FILE_NAME: Final[str] = "ultranx.json"

# Host, com porta opcional. Aceita nome sem ponto (rede local) e IPv4.
_HOST_RE = re.compile(r"^[A-Za-z0-9._-]+(:\d{1,5})?$")

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
    # Dentro de switch/ há dado do usuário que o pacote não repõe.
    ("switch", "jksv"),  # backups de saves
    ("switch", "edizon"),  # editor de saves e seus dados
    ("switch", "nx-activity-log"),  # estatísticas de jogo
)

# Extensões preservadas em QUALQUER profundidade: perder estes arquivos é
# irreversível e nenhum pacote os repõe.
PRESERVE_ANY_DEPTH_SUFFIXES: Final[frozenset[str]] = frozenset({".keys", ".sav"})

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
# Sobrescrever por cima não basta: o mesmo nome de arquivo pode carregar conteúdo
# de outra versão, e um órfão que o pacote novo não repõe continua sendo lido
# como se fosse válido. Só a remoção garante que o que fica veio do pacote.
DELETE_DIRS: Final[frozenset[str]] = frozenset(
    {
        "atmosphere",
        "bootloader",
        "switch",
        "config",  # substituída pelo pacote; presets antigos causam conflito
        "sept",
        "warmboot_mariko",
        "stratosphere",
    }
)

# Pastas limpas item a item em vez de removidas por inteiro, porque contêm
# subcaminhos protegidos (ver PRESERVE_SUBPATHS). O diretório em si permanece; a
# extração do pacote o repovoa.
PARTIAL_DELETE_DIRS: Final[frozenset[str]] = frozenset({"switch"})

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

    @property
    def is_configured(self) -> bool:
        """``False`` enquanto o servidor for o placeholder embutido."""
        return not is_placeholder_url(self.base_url)


def is_placeholder_url(url: str) -> bool:
    return PLACEHOLDER_HOST in url


def normalize_base_url(raw: str) -> str:
    """Limpa e valida uma URL base informada pelo usuário.

    Aceita ``host/caminho`` sem esquema (assume ``https://``), remove barra final
    e levanta :class:`ValueError` com mensagem exibível quando não dá para usar.

    Nome de host sem ponto é aceito de propósito: servidor de rede local e
    ``localhost`` são casos reais de quem hospeda o pacote internamente.
    """
    text = raw.strip()
    if not text:
        raise ValueError("Informe o endereço do servidor de pacotes.")

    if "://" not in text:
        text = f"https://{text}"

    scheme, _, rest = text.partition("://")
    scheme = scheme.casefold()
    if scheme not in {"http", "https"}:
        raise ValueError("O endereço deve começar com https:// ou http://.")

    # Checa o host antes de mexer nas barras: em "http:///rox" o host é vazio, e
    # promover "rox" a servidor mudaria silenciosamente o que o usuário digitou.
    rest = rest.strip()
    if rest.startswith("/"):
        raise ValueError("O endereço não indica nenhum servidor.")

    rest = rest.rstrip("/")
    host = rest.split("/", 1)[0]
    if not host:
        raise ValueError("O endereço não indica nenhum servidor.")
    if not _HOST_RE.match(host):
        raise ValueError(f"'{host}' não é um endereço de servidor válido.")

    return f"{scheme}://{rest}"


def app_directory() -> Path:
    """Pasta do executável (binário congelado) ou da árvore de código."""
    if getattr(sys, "frozen", False):
        return Path(sys.executable).resolve().parent
    return Path(__file__).resolve().parent


def user_config_path() -> Path:
    """Configuração no perfil do usuário — sempre gravável."""
    return Path.home() / ".ultranx" / CONFIG_FILE_NAME


def config_file_paths() -> tuple[Path, ...]:
    """Locais consultados, em ordem de precedência.

    O arquivo ao lado do executável vem primeiro para permitir distribuir o
    binário já apontado para um servidor (uso portátil, pendrive incluído).
    """
    return (app_directory() / CONFIG_FILE_NAME, user_config_path())


def read_config_file() -> dict[str, object]:
    """Lê o primeiro arquivo de configuração encontrado.

    Nunca levanta: configuração corrompida degrada para vazio e o app avisa que
    o servidor não está definido, em vez de não abrir.
    """
    for candidate in config_file_paths():
        try:
            content = candidate.read_text(encoding="utf-8")
        except (OSError, ValueError):
            continue
        try:
            document = json.loads(content)
        except json.JSONDecodeError as exc:
            logger.warning("Configuração inválida em %s: %s", candidate, exc)
            continue
        if isinstance(document, dict):
            logger.info("Configuração carregada de %s", candidate)
            return document
        logger.warning("Configuração em %s não é um objeto JSON.", candidate)
    return {}


def save_base_url(raw: str) -> Path:
    """Grava a URL base no perfil do usuário e devolve o caminho gravado.

    Preserva as demais chaves já presentes no arquivo. Levanta
    :class:`ValueError` para URL inválida e :class:`OSError` se não der para
    gravar — ambos com mensagem que a UI mostra direto.
    """
    url = normalize_base_url(raw)
    target = user_config_path()
    target.parent.mkdir(parents=True, exist_ok=True)

    document: dict[str, object] = {}
    try:
        existing = json.loads(target.read_text(encoding="utf-8"))
        if isinstance(existing, dict):
            document = existing
    except (OSError, json.JSONDecodeError):
        document = {}

    document["base_url"] = url
    target.write_text(json.dumps(document, indent=2) + "\n", encoding="utf-8")
    logger.info("Servidor salvo em %s: %s", target, url)
    return target


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
    """Resolve as configurações: ambiente > arquivo > placeholder.

    Retorna sempre uma nova instância; chamadas repetidas refletem mudanças de
    ambiente ou de arquivo sem mutar estado global.
    """
    document = read_config_file()

    from_file = str(document.get("base_url", "")).strip().rstrip("/")
    from_env = os.environ.get(ENV_BASE_URL, "").strip().rstrip("/")
    base_url = from_env or from_file or DEFAULT_BASE_URL

    file_timeout = document.get("http_timeout")
    fallback_timeout = (
        float(file_timeout)
        if isinstance(file_timeout, (int, float)) and file_timeout > 0
        else DEFAULT_HTTP_TIMEOUT
    )

    return Settings(
        base_url=base_url,
        http_timeout=_read_float(ENV_TIMEOUT, fallback_timeout),
        skip_hash_check=_read_bool(ENV_INSECURE_SKIP_HASH),
    )
