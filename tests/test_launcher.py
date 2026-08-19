"""Teste de regressão do entrypoint congelado.

O PyInstaller executa o script alvo como módulo de topo, sem pacote pai. Quando
o alvo era ``src/ultranx/__main__.py``, todo import relativo do pacote falhava
com ``ImportError`` — e em build ``--noconsole`` o erro só aparecia como um
diálogo modal de traceback, travando a aplicação antes da janela existir.

Este teste roda ``launcher.py`` exatamente como o binário congelado faria
(script de topo), sem precisar construir o executável.
"""

from __future__ import annotations

import os
import subprocess
import sys
from pathlib import Path

REPO_ROOT = Path(__file__).resolve().parents[1]
LAUNCHER = REPO_ROOT / "launcher.py"


def _run_launcher(*args: str) -> subprocess.CompletedProcess[str]:
    env = {
        **os.environ,
        "PYTHONPATH": str(REPO_ROOT / "src"),
        "QT_QPA_PLATFORM": "offscreen",
    }
    return subprocess.run(
        [sys.executable, str(LAUNCHER), *args],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )


def test_launcher_exists_outside_the_package():
    """Dentro do pacote, o launcher sofreria do mesmo problema de import."""
    assert LAUNCHER.is_file()
    assert LAUNCHER.parent == REPO_ROOT


def test_launcher_runs_as_top_level_script():
    result = _run_launcher("--version")

    assert "ImportError" not in result.stderr
    assert "relative import" not in result.stderr
    assert result.returncode == 0, result.stderr
    assert "UltraNX" in result.stdout


def test_module_entrypoint_also_works():
    """``python -m ultranx`` é o caminho a partir do código-fonte."""
    env = {**os.environ, "PYTHONPATH": str(REPO_ROOT / "src")}
    result = subprocess.run(
        [sys.executable, "-m", "ultranx", "--version"],
        capture_output=True,
        text=True,
        timeout=120,
        env=env,
        cwd=str(REPO_ROOT),
    )

    assert result.returncode == 0, result.stderr
    assert "UltraNX" in result.stdout
