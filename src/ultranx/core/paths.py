"""Helpers de caminho imunes a case-sensitivity, separador e traversal.

Regras adotadas em todo o projeto:

* comparação de nomes sempre por ``casefold()`` — o SD é FAT32/exFAT (case
  insensitive) mas o host pode ser Linux (case sensitive);
* nenhum caminho é usado sem antes provar que está contido na raiz do SD
  (:func:`is_within`), o que neutraliza ``..``, links simbólicos e zip-slip;
* nenhuma função aqui toca em disco além de ``resolve()``.
"""

from __future__ import annotations

from pathlib import Path, PurePosixPath


def normalized_parts(relative: Path | PurePosixPath | str) -> tuple[str, ...]:
    """Quebra um caminho relativo em partes casefolded, sem ``.``/``..``/vazios.

    Aceita separadores mistos (``a\\b`` e ``a/b``) porque nomes vindos de ZIP
    usam ``/`` mesmo no Windows. Qualquer ``..`` invalida o caminho inteiro
    (retorna tupla vazia) em vez de subir um nível.
    """
    raw = str(relative).replace("\\", "/")
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        cleaned = part.strip().strip("/")
        if not cleaned or cleaned == ".":
            continue
        if cleaned == "..":
            return ()
        parts.append(cleaned.casefold())
    return tuple(parts)


def raw_parts(relative: Path | PurePosixPath | str) -> tuple[str, ...]:
    """Igual a :func:`normalized_parts` mas preservando o caso original."""
    raw = str(relative).replace("\\", "/")
    parts: list[str] = []
    for part in PurePosixPath(raw).parts:
        cleaned = part.strip().strip("/")
        if not cleaned or cleaned == ".":
            continue
        if cleaned == "..":
            return ()
        parts.append(cleaned)
    return tuple(parts)


def safe_resolve(path: Path | str) -> Path:
    """``resolve()`` tolerante a caminhos inexistentes (``strict=False``)."""
    return Path(path).expanduser().resolve(strict=False)


def is_within(root: Path | str, candidate: Path | str) -> bool:
    """Retorna ``True`` se ``candidate`` está dentro de ``root`` (ou é a raiz).

    Compara caminhos já resolvidos, portanto ``..`` e symlinks foram achatados.
    """
    resolved_root = safe_resolve(root)
    resolved_candidate = safe_resolve(candidate)
    try:
        resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return False
    return True


def relative_parts(root: Path | str, candidate: Path | str) -> tuple[str, ...]:
    """Partes casefolded de ``candidate`` relativas a ``root``.

    Retorna tupla vazia quando ``candidate`` não está sob ``root`` — o chamador
    deve tratar tupla vazia como "fora do escopo, não mexer".
    """
    resolved_root = safe_resolve(root)
    resolved_candidate = safe_resolve(candidate)
    try:
        relative = resolved_candidate.relative_to(resolved_root)
    except ValueError:
        return ()
    return normalized_parts(relative)


def join_within(root: Path | str, relative: Path | PurePosixPath | str) -> Path | None:
    """Junta ``relative`` a ``root`` devolvendo ``None`` se escapar da raiz.

    É o único caminho permitido para materializar entradas de ZIP em disco
    (defesa contra zip-slip).
    """
    if not normalized_parts(relative):
        return None
    original = raw_parts(relative)
    if not original:
        return None
    target = safe_resolve(root).joinpath(*original)
    return target if is_within(root, target) else None


def matches_subpath(parts: tuple[str, ...], subpath: tuple[str, ...]) -> bool:
    """``True`` se ``parts`` é igual a ``subpath`` ou está dentro dele.

    Ambos os argumentos devem já estar casefolded.
    """
    if len(parts) < len(subpath):
        return False
    return parts[: len(subpath)] == subpath


def human_size(num_bytes: int) -> str:
    """Formata bytes em unidade legível (base 1024)."""
    size = float(max(num_bytes, 0))
    for unit in ("B", "KB", "MB", "GB"):
        if size < 1024:
            precision = 0 if unit == "B" else 1
            return f"{size:.{precision}f} {unit}"
        size /= 1024
    return f"{size:.1f} TB"
