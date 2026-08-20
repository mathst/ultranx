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
import stat
from collections.abc import Callable, Iterable
from dataclasses import dataclass
from pathlib import Path

from ..config import (
    DELETE_DIRS,
    DELETE_ROOT_FILES,
    PARTIAL_DELETE_DIRS,
    PRESERVE_ANY_DEPTH_SUFFIXES,
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
    * o caminho cai sob algum ``PRESERVE_SUBPATHS`` (ex.: ``switch/JKSV``);
    * a extensão está em ``PRESERVE_ANY_DEPTH_SUFFIXES`` — ``*.keys`` e
      ``*.sav`` em qualquer profundidade, porque perdê-los é irreversível;
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
        if matches_subpath(parts, subpath):
            return True
        # Ancestral de um subcaminho protegido também é protegido — exceto
        # quando a pasta é limpa item a item, caso em que a decisão desce para
        # cada filho (é o que permite limpar switch/ sem levar switch/JKSV).
        if matches_subpath(subpath, parts) and parts[0] not in PARTIAL_DELETE_DIRS:
            return True

    # Keys e saves são insubstituíveis: protegidos em qualquer profundidade.
    if Path(parts[-1]).suffix.casefold() in PRESERVE_ANY_DEPTH_SUFFIXES:
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


def _plan_partial_dir(
    root: Path, directory: Path
) -> tuple[tuple[CleanupItem, ...], tuple[Path, ...]]:
    """Planeja a limpeza item a item de uma pasta em ``PARTIAL_DELETE_DIRS``.

    A pasta em si é mantida (a extração do pacote a repovoa) e cada filho passa
    por :func:`is_protected`. É assim que ``switch/`` é limpo sem levar embora
    ``switch/JKSV``, ``switch/EdiZon`` ou as ``*.keys``.
    """
    items: list[CleanupItem] = []
    preserved: list[Path] = []
    try:
        children = sorted(directory.iterdir(), key=lambda p: p.name.casefold())
    except FileNotFoundError:
        return (), ()
    except PermissionError as exc:
        raise PermissionDeniedError(f"Sem permissão para listar '{directory}'.") from exc
    except OSError as exc:
        raise SanitizerError(f"Falha de I/O ao listar '{directory}': {exc}.") from exc

    for child in children:
        if is_protected(root, child):
            preserved.append(child)
        else:
            items.append(
                CleanupItem(
                    child,
                    child.is_dir(),
                    f"conteúdo legado de {directory.name}/ (conflito)",
                )
            )
    return tuple(items), tuple(preserved)


def build_plan(sd_root: Path) -> CleanupPlan:
    """Monta o plano de limpeza a partir do primeiro nível da raiz.

    Só desce um nível adicional nas pastas de ``PARTIAL_DELETE_DIRS``, que
    precisam de limpeza seletiva por conterem dado do usuário. Manter o escopo
    raso é o que mantém a whitelist auditável a olho nu.
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
            if name in PARTIAL_DELETE_DIRS:
                partial_items, partial_preserved = _plan_partial_dir(root, entry)
                items.extend(partial_items)
                preserved.extend(partial_preserved)
            elif name in DELETE_DIRS:
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


def _do_remove(path: Path, is_dir: bool) -> None:
    if is_dir:
        shutil.rmtree(path)
    else:
        path.unlink()


def _clear_readonly(path: Path) -> None:
    """Limpa o atributo somente-leitura de ``path`` e de tudo abaixo dele.

    Cartões formatados no Windows carregam esse atributo do lado do host, e
    homebrews antigos o deixam ligado em pastas próprias (ex.: cache de ícones).
    Isso barra a remoção com ``PermissionError`` mesmo com o processo tendo
    permissão de escrita plena no cartão — sem essa limpeza, um único arquivo
    read-only travaria a atualização inteira nessa etapa.
    """
    candidates: Iterable[Path] = (*path.rglob("*"), path) if path.is_dir() else (path,)
    for candidate in candidates:
        try:
            mode = candidate.stat().st_mode
            if not mode & stat.S_IWRITE:
                candidate.chmod(mode | stat.S_IWRITE)
        except OSError:
            pass


def _raise_removal_error(item: CleanupItem, exc: OSError) -> None:
    # ENOENT no pai / dispositivo ausente aparece como OSError genérico.
    if not item.path.parent.exists():
        raise DriveDisconnectedError(
            f"O cartão foi desconectado ao remover '{item.path.name}'."
        ) from exc
    raise SanitizerError(
        f"Falha ao remover '{item.path}': {exc.__class__.__name__}: {exc}."
    ) from exc


def _remove(item: CleanupItem) -> None:
    """Remove um item convertendo exceções de OS em erros de domínio.

    Numa primeira falha por permissão, limpa o atributo somente-leitura e
    tenta de novo antes de desistir (ver :func:`_clear_readonly`).
    """
    try:
        _do_remove(item.path, item.is_dir)
        return
    except FileNotFoundError:
        logger.debug("%s já não existia; seguindo.", item.path)
        return
    except PermissionError:
        _clear_readonly(item.path)
    except OSError as exc:
        _raise_removal_error(item, exc)

    try:
        _do_remove(item.path, item.is_dir)
    except FileNotFoundError:
        logger.debug("%s já não existia; seguindo.", item.path)
    except PermissionError as exc:
        raise PermissionDeniedError(f"Sem permissão para remover '{item.path}'.") from exc
    except OSError as exc:
        _raise_removal_error(item, exc)


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
