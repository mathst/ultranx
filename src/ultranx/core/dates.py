"""Parsing e formatação de datas de versão.

Armazenamento sempre em ISO-8601 (``2026-08-15``), porque não depende de locale e
ordena lexicograficamente. Exibição em ``dd/mm/aaaa``, que é o que o usuário
brasileiro espera ler.

Nenhuma função aqui lê o relógio: a data de instalação vem do mtime do arquivo
gravado no cartão, o que mantém tudo determinístico e testável.
"""

from __future__ import annotations

import logging
from datetime import UTC, date, datetime
from email.utils import parsedate_to_datetime
from pathlib import Path

logger = logging.getLogger(__name__)

ISO_FORMAT = "%Y-%m-%d"
DISPLAY_FORMAT = "%d/%m/%Y"
_MAX_DATE_LENGTH = 32


def parse_iso_date(raw: str | None) -> date | None:
    """Converte ``2026-08-15`` (ou ISO com hora) em :class:`date`.

    Retorna ``None`` para entrada vazia ou malformada — data ausente é um estado
    válido (pacote publicado sem ``released``), não um erro.
    """
    if not raw:
        return None
    text = raw.strip()
    if not text or len(text) > _MAX_DATE_LENGTH:
        return None
    try:
        return date.fromisoformat(text[:10])
    except ValueError:
        logger.debug("Data ISO inválida: %r", text)
        return None


def parse_http_date(raw: str | None) -> date | None:
    """Converte um cabeçalho HTTP ``Last-Modified`` em :class:`date`.

    É o fallback para servidores que publicam ``packetVersion.txt`` sem manifest.
    """
    if not raw:
        return None
    try:
        moment = parsedate_to_datetime(raw.strip())
    except (TypeError, ValueError):
        logger.debug("Last-Modified inválido: %r", raw)
        return None
    return moment.date() if moment is not None else None


def file_date(path: Path) -> date | None:
    """Data de modificação do arquivo, em hora local. ``None`` se inacessível."""
    try:
        stamp = path.stat().st_mtime
    except OSError:
        return None
    return datetime.fromtimestamp(stamp, tz=UTC).astimezone().date()


def to_iso(value: date | None) -> str | None:
    return value.strftime(ISO_FORMAT) if value is not None else None


def format_date(value: date | None, fallback: str = "—") -> str:
    """Formata para exibição em ``dd/mm/aaaa``; ``fallback`` quando ausente."""
    return value.strftime(DISPLAY_FORMAT) if value is not None else fallback
