"""Ponto de entrada do binário congelado (PyInstaller).

Existe por um motivo específico: o PyInstaller executa o script alvo como
módulo de topo, sem pacote pai, então apontar a build direto para
``src/ultranx/__main__.py`` faz todo import relativo (``from .config import …``)
falhar com ``ImportError``. Em build ``--noconsole`` isso não aparece no
terminal — abre um diálogo modal de traceback e a aplicação trava antes da
janela existir.

Este arquivo importa o pacote de forma absoluta e delega. Para uso a partir do
código-fonte, ``python -m ultranx`` continua sendo o caminho normal.
"""

from __future__ import annotations

import sys

from ultranx.__main__ import main

if __name__ == "__main__":
    sys.exit(main())
