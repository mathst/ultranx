"""Cliente do MediaFire: listagem por API e link de download por raspagem.

Duas metades com robustez muito diferente, de propósito isoladas neste módulo:

* **Listagem e metadados** usam a API pública 1.5 (``folder/get_content.php`` e
  ``file/get_info.php``). É contrato estável e entrega tamanho, data de criação e
  **SHA-256** de cada arquivo — ou seja, integridade e data de lançamento saem de
  graça, sem manter ``manifest.json`` nenhum.

* **Link de download é raspado do HTML** da página do arquivo. Conta grátis não
  recebe ``direct_download`` pela API (erro 45, *Insufficient Permissions*),
  então não há alternativa. Isso é frágil por natureza: quando o MediaFire mudar
  o HTML, :func:`resolve_download_url` deixa de achar o link e levanta
  :class:`RemoteDataError` dizendo exatamente isso. Todo o conserto futuro cabe
  em :data:`_DIRECT_LINK_PATTERNS`.
"""

from __future__ import annotations

import base64
import binascii
import json
import logging
import re
from dataclasses import dataclass
from datetime import date

import requests

from ..config import Settings
from .dates import parse_iso_date
from .errors import NetworkError, RemoteDataError

logger = logging.getLogger(__name__)

API_BASE = "https://www.mediafire.com/api/1.5"
FILE_PAGE = "https://www.mediafire.com/file/{quickkey}/file"

_FOLDER_URL_RE = re.compile(r"mediafire\.com/folder/(?P<key>[A-Za-z0-9]+)", re.IGNORECASE)
_SHA256_RE = re.compile(r"^[0-9a-f]{64}$")
_QUICKKEY_RE = re.compile(r"^[A-Za-z0-9]{11,32}$")

# Ordem importa: o primeiro padrão que casar vence. Quando o MediaFire mudar o
# HTML, é aqui que se adiciona o formato novo.
_DIRECT_LINK_PATTERNS: tuple[re.Pattern[str], ...] = (
    re.compile(r'href="(https://download[0-9]*\.mediafire\.com/[^"]+)"'),
    re.compile(r'data-scrambled-url="([A-Za-z0-9+/=]+)"'),
)

_MAX_PAGE_BYTES = 8 * 1024 * 1024
_BROWSER_UA = (
    "Mozilla/5.0 (Windows NT 10.0; Win64; x64) AppleWebKit/537.36 "
    "(KHTML, like Gecko) Chrome/120.0 Safari/537.36"
)


@dataclass(frozen=True, slots=True)
class MediaFireFile:
    """Um arquivo publicado, já com o que o instalador precisa."""

    filename: str
    quickkey: str
    size_bytes: int | None
    sha256: str | None
    created: date | None


@dataclass(frozen=True, slots=True)
class MediaFireFolder:
    """Conteúdo de uma pasta: arquivos e subpastas (nome, chave)."""

    key: str
    files: tuple[MediaFireFile, ...]
    subfolders: tuple[tuple[str, str], ...]


def is_mediafire_url(url: str) -> bool:
    return "mediafire.com" in url.casefold()


def parse_folder_key(url: str) -> str:
    """Extrai a chave da pasta de uma URL do MediaFire.

    Aceita também a chave nua, para quem colar só ela no campo do servidor.
    """
    match = _FOLDER_URL_RE.search(url)
    if match:
        return match.group("key")
    candidate = url.strip().rstrip("/").rsplit("/", 1)[-1]
    if _QUICKKEY_RE.match(candidate):
        return candidate
    raise RemoteDataError(
        "Não reconheci uma pasta do MediaFire nesse endereço. Use o link no "
        "formato https://www.mediafire.com/folder/CHAVE/Nome."
    )


def _api(path: str, params: dict[str, str], settings: Settings) -> dict:
    """Chama a API e devolve ``response``, convertendo falhas em erros do domínio."""
    url = f"{API_BASE}/{path}"
    query = {**params, "response_format": "json"}
    try:
        reply = requests.get(url, params=query, timeout=settings.http_timeout)
        reply.raise_for_status()
        document = reply.json()
    except requests.exceptions.Timeout as exc:
        raise NetworkError(f"Tempo esgotado ao consultar o MediaFire ({path}).") from exc
    except requests.exceptions.ConnectionError as exc:
        raise NetworkError("Sem conexão com o MediaFire.") from exc
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", "?")
        raise NetworkError(f"MediaFire respondeu HTTP {status} em {path}.") from exc
    except requests.exceptions.RequestException as exc:
        raise NetworkError(f"Falha de rede ao consultar o MediaFire: {exc}.") from exc
    except (json.JSONDecodeError, ValueError) as exc:
        raise RemoteDataError(
            f"Resposta do MediaFire não é JSON válido ({path})."
        ) from exc

    response = document.get("response") if isinstance(document, dict) else None
    if not isinstance(response, dict):
        raise RemoteDataError(f"Resposta do MediaFire sem objeto 'response' ({path}).")
    if response.get("result") != "Success":
        message = response.get("message", "motivo não informado")
        raise RemoteDataError(f"MediaFire recusou a consulta: {message}.")
    return response


def _parse_size(raw: object) -> int | None:
    try:
        value = int(str(raw))
    except (TypeError, ValueError):
        return None
    return value if value > 0 else None


def _parse_file(entry: object) -> MediaFireFile | None:
    """Converte uma entrada da API; ``None`` descarta a entrada malformada."""
    if not isinstance(entry, dict):
        return None
    filename = str(entry.get("filename", "")).strip()
    quickkey = str(entry.get("quickkey", "")).strip()
    if not filename or not _QUICKKEY_RE.match(quickkey):
        logger.warning("Entrada de pasta ignorada: %r", str(entry)[:120])
        return None

    raw_hash = str(entry.get("hash", "")).strip().casefold()
    return MediaFireFile(
        filename=filename,
        quickkey=quickkey,
        size_bytes=_parse_size(entry.get("size")),
        sha256=raw_hash if _SHA256_RE.match(raw_hash) else None,
        created=parse_iso_date(str(entry.get("created", "")).replace(" ", "T")),
    )


def list_folder(folder_key: str, settings: Settings) -> MediaFireFolder:
    """Lista arquivos e subpastas de uma pasta pública."""
    files_response = _api(
        "folder/get_content.php",
        {"folder_key": folder_key, "content_type": "files"},
        settings,
    )
    folders_response = _api(
        "folder/get_content.php",
        {"folder_key": folder_key, "content_type": "folders"},
        settings,
    )

    raw_files = (files_response.get("folder_content") or {}).get("files") or []
    files = tuple(
        parsed for entry in raw_files if (parsed := _parse_file(entry)) is not None
    )

    raw_folders = (folders_response.get("folder_content") or {}).get("folders") or []
    subfolders = tuple(
        (str(item.get("name", "")).strip(), str(item.get("folderkey", "")).strip())
        for item in raw_folders
        if isinstance(item, dict) and item.get("folderkey")
    )

    logger.info(
        "Pasta %s: %d arquivo(s), %d subpasta(s).",
        folder_key,
        len(files),
        len(subfolders),
    )
    return MediaFireFolder(key=folder_key, files=files, subfolders=subfolders)


def file_details(quickkey: str, settings: Settings) -> MediaFireFile:
    """Consulta metadados de um arquivo, incluindo o SHA-256."""
    response = _api("file/get_info.php", {"quick_key": quickkey}, settings)
    parsed = _parse_file(response.get("file_info"))
    if parsed is None:
        raise RemoteDataError(f"MediaFire não devolveu metadados de {quickkey}.")
    return parsed


def _decode_scrambled(value: str) -> str | None:
    """Decodifica o link ofuscado em base64 usado em algumas páginas."""
    try:
        decoded = base64.b64decode(value, validate=True).decode("utf-8", "replace")
    except (binascii.Error, ValueError):
        return None
    return decoded if decoded.startswith("https://") else None


def resolve_download_url(quickkey: str, settings: Settings) -> str:
    """Descobre a URL de download direto raspando a página do arquivo.

    Necessário porque conta grátis não recebe ``direct_download`` pela API. É a
    parte frágil do projeto: mudança de HTML no MediaFire cai aqui, e a mensagem
    de erro diz exatamente isso, para o conserto ser óbvio.
    """
    page_url = FILE_PAGE.format(quickkey=quickkey)
    try:
        reply = requests.get(
            page_url,
            timeout=settings.http_timeout,
            headers={"User-Agent": _BROWSER_UA},
        )
        reply.raise_for_status()
    except requests.exceptions.Timeout as exc:
        raise NetworkError("Tempo esgotado ao abrir a página do arquivo.") from exc
    except requests.exceptions.ConnectionError as exc:
        raise NetworkError("Sem conexão com o MediaFire.") from exc
    except requests.exceptions.HTTPError as exc:
        status = getattr(exc.response, "status_code", "?")
        raise NetworkError(
            f"MediaFire respondeu HTTP {status} na página do arquivo."
        ) from exc
    except requests.exceptions.RequestException as exc:
        raise NetworkError(f"Falha de rede ao abrir a página do arquivo: {exc}.") from exc

    if len(reply.content) > _MAX_PAGE_BYTES:
        raise RemoteDataError("A página do arquivo é grande demais; abortando.")

    html = reply.text
    for pattern in _DIRECT_LINK_PATTERNS:
        match = pattern.search(html)
        if match is None:
            continue
        candidate = match.group(1)
        if not candidate.startswith("https://"):
            decoded = _decode_scrambled(candidate)
            if decoded is None:
                continue
            candidate = decoded
        logger.info("Link de download resolvido para %s.", quickkey)
        return candidate

    raise RemoteDataError(
        "Não encontrei o link de download na página do MediaFire. O site "
        "provavelmente mudou de layout: atualize os padrões em "
        "core/mediafire.py (_DIRECT_LINK_PATTERNS)."
    )
