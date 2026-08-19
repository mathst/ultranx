"""Safe Sanitizer — limpeza seletiva por whitelist estrita.

Modelo de segurança em três camadas, aplicadas nesta ordem:

1. **Contenção** — todo caminho candidato precisa provar que está dentro da raiz
   do SD (:func:`~ultranx.core.paths.is_within`); nada fora é sequer considerado.
2. **Whitelist** — :func:`is_protected` é consultada para todo candidato. Se
   protege, o item nunca entra no plano. A whitelist vence a lista de remoção
   em qualquer conflito.
3. **Plano imutável** — :func:`build_plan` só lê o disco e devolve um
   :class:`CleanupPlan`. A remoção real (:func:`execute_plan`) revalida cada
   item antes de apagar, então mesmo um plano manipulado não escapa das camadas
   anteriores.
"""

from __future__ import annotations

import logging
import shutil
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    DELETE_DIRS,
    DELETE_ROOT_FILES,
    PRESERVE_DIRS,
    PRESERVE_ROOT_FILE_SUFFIXES,
    PRESERVE_ROOT_FILES,
    PRESERVE_SUBPATHS,
)
from .errors import DriveDisconnectedError, PermissionDeniedError, SanitizerError
from .paths import is_within, matches_subpath, relative_parts, safe_resolve

logger = logging.getLogger(__name__)

ProgressCallback = Callable[[int, int, str], None]


@dataclass(frozen=True, slots=True)
class CleanupItem:
    """Um item marcado para remoção."""

    path: Path
    is_dir: bool
    reason: str


@dataclass(frozen=True, slots=True)
class CleanupPlan:
    """Plano de limpeza imutável, pronto para exibição e execução."""

    sd_root: Path
    items: tuple[CleanupItem, ...]
    preserved: tuple[Path, ...]

    @property
    def is_empty(self) -> bool:
        return not self.items

    def describe(self) -> str:
        """Resumo textual para log e para a UI."""
        if self.is_empty:
            return "Nada a remover: o SD já está limpo."
        removed = "\n".join(f"  - remover {item.path.name}" for item in self.items)
        kept = "\n".join(f"  - preservar {path.name}" for path in self.preserved)
        return f"Plano de limpeza em {self.sd_root}:\n{removed}\n{kept}"


def is_protected(sd_root: Path, candidate: Path) -> bool:
    """Veredito único de proteção. Consultada por plano E por execução.

    Protege quando:

    * o candidato é a própria raiz, ou está fora dela (contenção);
    * o primeiro componente está em ``PRESERVE_DIRS``;
    * o caminho cai sob algum ``PRESERVE_SUBPATHS``;
    * é arquivo de raiz na whitelist de nomes ou de extensões (binários
      standalone, saves, screenshots).
    """
    if not is_within(sd_root, candidate):
        return True

    parts = relative_parts(sd_root, candidate)
    if not parts:
        # Tupla vazia = é a própria raiz ou não é relativo a ela.
        return True

    if parts[0] in PRESERVE_DIRS:
        return True

    for subpath in PRESERVE_SUBPATHS:
        if matches_subpath(parts, subpath) or matches_subpath(subpath, parts):
            return True

    if len(parts) == 1:
        name = parts[0]
        if name in PRESERVE_ROOT_FILES:
            return True
        if name in DELETE_ROOT_FILES or name in DELETE_DIRS:
            return False
        suffix = Path(name).suffix.casefold()
        if suffix in PRESERVE_ROOT_FILE_SUFFIXES:
            return True
        # Desconhecido na raiz: preservar. Falha segura.
        return True

    return False


def _iter_root_entries(sd_root: Path) -> Iterable[Path]:
    resolved = safe_resolve(sd_root)
    try:
        yield from sorted(resolved.iterdir(), key=lambda p: p.name.casefold())
    except FileNotFoundError as exc:
        raise DriveDisconnectedError(
            f"A raiz '{resolved}' desapareceu durante a varredura."
        ) from exc
    except PermissionError as exc:
        raise PermissionDeniedError(f"Sem permissão para listar '{resolved}'.") from exc
    except OSError as exc:
        raise SanitizerError(f"Falha de I/O ao listar '{resolved}': {exc}.") from exc


def build_plan(sd_root: Path) -> CleanupPlan:
    """Monta o plano de limpeza lendo apenas o primeiro nível da raiz.

    Não desce recursivamente: o escopo do sanitizer são pastas de sistema de
    primeiro nível e arquivos soltos na raiz. Isso mantém a whitelist auditável.
    """
    root = safe_resolve(sd_root)
    items: list[CleanupItem] = []
    preserved: list[Path] = []

    for entry in _iter_root_entries(root):
        if is_protected(root, entry):
            preserved.append(entry)
            continue

        name = entry.name.casefold()
        if entry.is_dir():
            if name in DELETE_DIRS:
                items.append(
                    CleanupItem(entry, True, "pasta de sistema legada (conflito)")
                )
            else:
                preserved.append(entry)
            continue

        if name in DELETE_ROOT_FILES:
            items.append(CleanupItem(entry, False, "arquivo regravado pelo pacote"))
        else:
            preserved.append(entry)

    plan = CleanupPlan(sd_root=root, items=tuple(items), preserved=tuple(preserved))
    logger.info(
        "Plano de limpeza: %d remoção(ões), %d preservado(s).",
        len(plan.items),
        len(plan.preserved),
    )
    return plan


def _remove(item: CleanupItem) -> None:
    """Remove um item convertendo exceções de OS em erros de domínio."""
    try:
        if item.is_dir:
            shutil.rmtree(item.path)
        else:
            item.path.unlink()
    except FileNotFoundError:
        logger.debug("%s já não existia; seguindo.", item.path)
    except PermissionError as exc:
        raise PermissionDeniedError(f"Sem permissão para remover '{item.path}'.") from exc
    except OSError as exc:
        # ENOENT no pai / dispositivo ausente aparece como OSError genérico.
        if not item.path.parent.exists():
            raise DriveDisconnectedError(
                f"O cartão foi desconectado ao remover '{item.path.name}'."
            ) from exc
        raise SanitizerError(
            f"Falha ao remover '{item.path}': {exc.__class__.__name__}: {exc}."
        ) from exc


def execute_plan(
    plan: CleanupPlan,
    progress: ProgressCallback | None = None,
    should_cancel: Callable[[], bool] | None = None,
) -> tuple[Path, ...]:
    """Executa o plano e devolve os caminhos efetivamente removidos.

    Revalida ``is_protected`` para cada item antes de apagar — camada 3 do
    modelo de segurança. Um item que passou a ser protegido é ignorado com log
    em WARNING, nunca removido.
    """
    removed: list[Path] = []
    total = len(plan.items)

    for index, item in enumerate(plan.items, start=1):
        if should_cancel is not None and should_cancel():
            logger.info("Limpeza cancelada após %d/%d itens.", index - 1, total)
            break

        if is_protected(plan.sd_root, item.path):
            logger.warning(
                "GUARD: '%s' está protegido pela whitelist; remoção abortada.",
                item.path,
            )
            continue

        if progress is not None:
            progress(index, total, item.path.name)

        _remove(item)
        removed.append(item.path)
        logger.info("Removido: %s (%s)", item.path.name, item.reason)

    return tuple(removed)
