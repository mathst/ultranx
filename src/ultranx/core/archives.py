"""Extração de pacotes ``.7z`` e ``.zip`` sobre a raiz do cartão.

O pacote R O X é distribuído em 7-Zip, que o stdlib não abre — daí a dependência
``py7zr``. O suporte a ZIP continua porque é o formato mais fácil de servir num
host estático e não custa nada manter.

Invariante de segurança comum aos dois formatos: nenhuma entrada é gravada sem
passar por :func:`~ultranx.core.paths.join_within`, então caminho com ``..`` ou
absoluto (zip-slip) é descartado em vez de escapar da raiz.
"""

from __future__ import annotations

import logging
import shutil
import tempfile
import threading
import zipfile
from collections.abc import Callable
from concurrent.futures import ThreadPoolExecutor
from pathlib import Path

from ..config import (
    DELETE_DIRS,
    DELETE_ROOT_FILES,
    DOWNLOAD_CHUNK_SIZE,
    PARTIAL_DELETE_DIRS,
    PRESERVE_DIRS,
    SWITCH_ROOT_MARKERS,
)
from .errors import (
    DriveDisconnectedError,
    InstallError,
    IntegrityError,
    OperationCancelled,
    PermissionDeniedError,
)
from .paths import join_within, safe_resolve

logger = logging.getLogger(__name__)

# (entradas_extraidas, entradas_totais, nome_atual)
ExtractProgress = Callable[[int, int, str], None]
CancelCheck = Callable[[], bool]

SUFFIX_ZIP = ".zip"
SUFFIX_7Z = ".7z"

# py7zr não extrai arquivo por arquivo com progresso confiável, então o 7z é
# extraído em lotes: dá progresso visível sem materializar tudo em memória.
#
# Pacotes .7z de homebrew são SÓLIDOS (muitos arquivos pequenos compartilham
# um único bloco comprimido — ver ArchiveInfo.solid). Nesse regime, cada
# chamada extract()+reset() paga parte do custo de descompactar o bloco desde
# o início outra vez; quanto menor o lote, mais chamadas, mais redundância.
# Medido com 25 mil entradas sólidas (~375 MB): lotes de 40 levam 232s contra
# 90s com lotes de 500 — o valor antigo (40) multiplicava o tempo por >2.5x.
# 500 mede igual ou melhor que uma passada única, sem abrir mão do progresso
# incremental nem materializar tudo de uma vez.
_SEVENZIP_BATCH = 500

# Medido no leitor/cartão real (ver histórico): 4 threads já satura o ganho de
# escrever muitos arquivos pequenos (~1.3x sobre sequencial); mais threads não
# ajudam porque o controlador do cartão não tem fila profunda. Usado só pelo
# caminho .zip — .7z é sólido e não se beneficia (ver _extract_7z).
_EXTRACT_WORKERS = 4


def supported_suffixes() -> tuple[str, ...]:
    return (SUFFIX_7Z, SUFFIX_ZIP)


# Nomes que já são pasta/arquivo legítimo na raiz do Switch. Um pacote que só
# toca "atmosphere/" tem um único nome de topo por motivo real, não porque foi
# embrulhado — só um nome de topo DESCONHECIDO indica invólucro acidental.
_KNOWN_SD_ROOT_NAMES = frozenset(
    name.casefold()
    for name in (
        *DELETE_DIRS,
        *PARTIAL_DELETE_DIRS,
        *PRESERVE_DIRS,
        *SWITCH_ROOT_MARKERS,
        *DELETE_ROOT_FILES,
    )
)


def _shared_top_level(names: list[str]) -> str | None:
    """Nome da pasta única que embrulha o pacote inteiro, se houver.

    Pacotes montados por "compactar pasta" (comum em upload da comunidade,
    ex.: MediaFire) embrulham tudo num diretório de topo com o nome do
    pacote. Sem detectar e descartar esse invólucro, atmosphere/switch/etc.
    nunca chegam na raiz do cartão — ficam presos dentro dessa pasta extra e
    o Atmosphere não boota.
    """
    cleaned = [name.replace("\\", "/").strip("/") for name in names if name.strip("/")]
    if not cleaned:
        return None
    tops = {name.split("/", 1)[0] for name in cleaned}
    if len(tops) != 1:
        return None
    if not any("/" in name for name in cleaned):
        return None  # arquivo único solto na raiz do pacote, não é invólucro
    top = next(iter(tops))
    if top.casefold() in _KNOWN_SD_ROOT_NAMES:
        return None  # pasta real do Switch, não invólucro
    return top


def _entry_names(archive_path: Path, suffix: str) -> list[str]:
    """Lista os nomes internos do pacote, só para detectar invólucro."""
    if suffix == SUFFIX_ZIP:
        with zipfile.ZipFile(archive_path) as archive:
            return [item.filename for item in archive.infolist() if item.filename]
    if suffix == SUFFIX_7Z:
        import py7zr

        with py7zr.SevenZipFile(archive_path, mode="r") as archive:
            return [name for name in archive.getnames() if name]
    return []


def _merge_up(src: Path, dst: Path) -> None:
    """Move o conteúdo de ``src`` para dentro de ``dst`` e remove ``src`` ao final.

    Mescla pastas que já existem em ``dst`` em vez de apagá-las, e sobrescreve
    arquivos com o mesmo nome — o mesmo efeito que a extração teria tido se o
    pacote não viesse embrulhado numa pasta de topo.
    """
    dst.mkdir(parents=True, exist_ok=True)
    for entry in src.iterdir():
        target = dst / entry.name
        if entry.is_dir() and not entry.is_symlink():
            _merge_up(entry, target)
        else:
            if target.is_dir() and not target.is_symlink():
                shutil.rmtree(target)
            elif target.exists() or target.is_symlink():
                target.unlink()
            shutil.move(str(entry), str(target))
    src.rmdir()


def _target_for(root: Path, name: str) -> Path | None:
    """Resolve o destino de uma entrada, ou ``None`` se ela escaparia da raiz."""
    target = join_within(root, name)
    if target is None:
        logger.warning("GUARD: entrada '%s' fora da raiz; descartada.", name)
    return target


def _extract_zip(
    archive_path: Path,
    root: Path,
    progress: ExtractProgress | None,
    should_cancel: CancelCheck | None,
) -> int:
    """Extrai em paralelo (``_EXTRACT_WORKERS`` threads).

    Resolve todo destino e cria todo diretório ANTES de paralelizar, numa
    passada só, sequencial. ``Path.resolve()`` (dentro de :func:`_target_for`)
    tem uma corrida real no Windows quando roda ao mesmo tempo que outra
    thread cria (``mkdir``) o mesmo diretório pai: o passeio pelos componentes
    do caminho pode pegar o diretório num estado intermediário e resolver
    errado. Isolar planejamento (sequencial) de escrita (paralela) elimina a
    corrida, já que nenhuma thread mexe em diretório depois que a fase
    paralela começa.

    ``zipfile.ZipFile`` é seguro para leitura concorrente: um ``RLock`` interno
    serializa só o seek no arquivo compartilhado, a descompressão de cada
    entrada roda solta. Cada thread escreve num arquivo de destino distinto, e
    a contagem/progresso é protegida por ``lock``.
    """
    with zipfile.ZipFile(archive_path) as archive:
        entries = [item for item in archive.infolist() if item.filename]
        total = len(entries)
        if total == 0:
            raise InstallError("O pacote baixado está vazio.")

        planned: list[tuple[zipfile.ZipInfo, Path]] = []
        dirs_to_create: set[Path] = set()
        for entry in entries:
            target = _target_for(root, entry.filename)
            if target is None:
                continue
            if entry.is_dir():
                dirs_to_create.add(target)
            else:
                dirs_to_create.add(target.parent)
                planned.append((entry, target))

        for directory in dirs_to_create:
            directory.mkdir(parents=True, exist_ok=True)

        written = 0
        lock = threading.Lock()
        cancelled = threading.Event()

        def extract_one(pair: tuple[zipfile.ZipInfo, Path]) -> None:
            nonlocal written
            if cancelled.is_set():
                return
            if should_cancel is not None and should_cancel():
                cancelled.set()
                return

            entry, target = pair
            with archive.open(entry) as source, target.open("wb") as sink:
                while block := source.read(DOWNLOAD_CHUNK_SIZE):
                    sink.write(block)

            with lock:
                written += 1
                count = written
            if progress is not None:
                progress(count, total, entry.filename)

        with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as pool:
            for future in [pool.submit(extract_one, pair) for pair in planned]:
                future.result()

    if cancelled.is_set():
        raise OperationCancelled("Extração cancelada pelo usuário.")
    return written


def _extract_7z(
    archive_path: Path,
    root: Path,
    progress: ExtractProgress | None,
    should_cancel: CancelCheck | None,
) -> int:
    """Extrai sequencialmente, em lotes de ``_SEVENZIP_BATCH``.

    NÃO paralelizar por partições aqui: pacotes de homebrew são sólidos (um
    bloco comprimido cobrindo muitos arquivos), e abrir o mesmo ``.7z`` em N
    threads para decodificar pedaços diferentes do MESMO bloco faz cada
    thread redecodificar boa parte do bloco desde o início — medido ~3x mais
    lento que sequencial num pacote de 120 MB. Não há como paralelizar a
    decodificação de um bloco sólido sem essa redundância; só a escrita em
    disco (já paralela no caminho ``.zip``, que não sofre desse problema)
    valeria a pena, e replicar isso aqui exigiria uma ``WriterFactory``
    customizada seguravel — desproporcional ao ganho.
    """
    try:
        import py7zr
    except ImportError as exc:  # pragma: no cover - dependência declarada
        raise InstallError(
            "Suporte a .7z indisponível: a biblioteca py7zr não está instalada."
        ) from exc

    written = 0
    with py7zr.SevenZipFile(archive_path, mode="r") as archive:
        names = [name for name in archive.getnames() if name]
        total = len(names)
        if total == 0:
            raise InstallError("O pacote baixado está vazio.")

        # Valida todos os caminhos ANTES de extrair: py7zr escreve direto no
        # disco, então a checagem tem de acontecer antes, não durante.
        safe: list[str] = []
        for name in names:
            if _target_for(root, name) is not None:
                safe.append(name)
        if not safe:
            raise IntegrityError(
                "Todas as entradas do pacote apontam para fora da raiz do cartão."
            )

        for start in range(0, len(safe), _SEVENZIP_BATCH):
            if should_cancel is not None and should_cancel():
                raise OperationCancelled("Extração cancelada pelo usuário.")

            batch = safe[start : start + _SEVENZIP_BATCH]
            archive.extract(path=str(root), targets=batch)
            archive.reset()  # py7zr exige reset entre extrações parciais
            written += len(batch)

            if progress is not None:
                progress(min(written, total), total, batch[-1])
    return written


def _plan_copy(src: Path, dst: Path) -> tuple[list[tuple[Path, Path]], set[Path]]:
    """Lista arquivos e diretórios necessários para copiar ``src`` para ``dst``."""
    files: list[tuple[Path, Path]] = []
    dirs: set[Path] = {dst}
    stack = [(src, dst)]
    while stack:
        current_src, current_dst = stack.pop()
        for entry in current_src.iterdir():
            target = current_dst / entry.name
            if entry.is_dir() and not entry.is_symlink():
                dirs.add(target)
                stack.append((entry, target))
            else:
                dirs.add(target.parent)
                files.append((entry, target))
    return files, dirs


def _copy_tree_parallel(
    src: Path,
    dst: Path,
    progress: ExtractProgress | None,
    should_cancel: CancelCheck | None,
) -> int:
    """Copia o conteúdo de ``src`` para dentro de ``dst`` com ``_EXTRACT_WORKERS``
    threads, mesclando com o que já existe (sobrescreve arquivo com mesmo nome,
    preserva o resto).

    É o único ponto que toca a mídia lenta (cartão via leitor USB): a
    descompactação já rodou inteira em ``src`` (disco local rápido). Reusa o
    mesmo padrão de :func:`_extract_zip` — planeja todo diretório antes,
    sequencial, depois copia em paralelo — pelo mesmo motivo (corrida de
    ``Path.resolve()`` contra ``mkdir`` concorrente).
    """
    files, dirs = _plan_copy(src, dst)
    for directory in dirs:
        directory.mkdir(parents=True, exist_ok=True)

    total = len(files)
    written = 0
    lock = threading.Lock()
    cancelled = threading.Event()

    def copy_one(pair: tuple[Path, Path]) -> None:
        nonlocal written
        if cancelled.is_set():
            return
        if should_cancel is not None and should_cancel():
            cancelled.set()
            return

        source, target = pair
        with source.open("rb") as reader, target.open("wb") as sink:
            while block := reader.read(DOWNLOAD_CHUNK_SIZE):
                sink.write(block)

        with lock:
            written += 1
            count = written
        if progress is not None:
            progress(count, total, target.name)

    with ThreadPoolExecutor(max_workers=_EXTRACT_WORKERS) as pool:
        for future in [pool.submit(copy_one, pair) for pair in files]:
            future.result()

    if cancelled.is_set():
        raise OperationCancelled("Extração cancelada pelo usuário.")
    return written


def extract_archive(
    archive_path: Path,
    sd_root: Path,
    progress: ExtractProgress | None = None,
    should_cancel: CancelCheck | None = None,
) -> int:
    """Extrai ``archive_path`` sobre ``sd_root``, sobrescrevendo o que colidir.

    Em duas fases: descompacta inteiro num diretório temporário no disco local
    (rápido, SSD) e só depois copia o resultado para o cartão em paralelo. O
    cartão via leitor USB é sempre o gargalo — descompactar direto nele
    intercala descompressão (que não pode paralelizar em pacote sólido, ver
    :func:`_extract_7z`) com escrita lenta. Separando as fases, a
    descompactação corre solta no SSD e só a cópia final — que É paralelizável
    em qualquer formato — toca o cartão.

    O formato é escolhido pela extensão. Retorna o número de entradas gravadas e
    converte qualquer falha de OS na hierarquia de :mod:`ultranx.core.errors`.
    """
    root = safe_resolve(sd_root)
    suffix = archive_path.suffix.casefold()
    staging = Path(tempfile.mkdtemp(prefix="ultranx-extract-"))

    try:
        try:
            wrapper = _shared_top_level(_entry_names(archive_path, suffix))
            if suffix == SUFFIX_ZIP:
                _extract_zip(archive_path, staging, None, should_cancel)
            elif suffix == SUFFIX_7Z:
                _extract_7z(archive_path, staging, None, should_cancel)
            else:
                raise InstallError(
                    f"Formato '{suffix or archive_path.name}' não suportado. "
                    f"Formatos aceitos: {', '.join(supported_suffixes())}."
                )
            if wrapper is not None:
                logger.info(
                    "Pacote embrulhado em '%s/'; descartando invólucro no staging.",
                    wrapper,
                )
                _merge_up(staging / wrapper, staging)

            written = _copy_tree_parallel(staging, root, progress, should_cancel)
        except zipfile.BadZipFile as exc:
            raise IntegrityError("O pacote baixado não é um ZIP válido.") from exc
        except (OperationCancelled, InstallError, IntegrityError):
            raise
        except PermissionError as exc:
            raise PermissionDeniedError(
                "Sem permissão de escrita ao extrair o pacote no cartão."
            ) from exc
        except FileNotFoundError as exc:
            raise DriveDisconnectedError(
                "O cartão foi desconectado durante a extração."
            ) from exc
        except OSError as exc:
            if not root.exists():
                raise DriveDisconnectedError(
                    "O cartão foi desconectado durante a extração."
                ) from exc
            raise InstallError(f"Falha de I/O durante a extração: {exc}.") from exc
        except Exception as exc:  # py7zr sinaliza arquivo corrompido de várias formas
            raise IntegrityError(
                f"O pacote baixado está corrompido ou ilegível: {exc}."
            ) from exc
    finally:
        shutil.rmtree(staging, ignore_errors=True)

    logger.info("Extração concluída: %d entrada(s) de %s.", written, archive_path.name)
    return written


def free_bytes(path: Path) -> int:
    """Espaço livre em bytes; ``0`` quando indeterminável."""
    try:
        return int(shutil.disk_usage(str(path)).free)
    except (OSError, ValueError):
        logger.debug("disk_usage indisponível para %s", path)
        return 0
