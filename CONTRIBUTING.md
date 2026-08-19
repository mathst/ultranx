# Contribuindo com o UltraNX

## Antes de abrir um PR

```bash
pip install -r requirements-dev.txt
ruff check src tests
pytest --cov=ultranx --cov-report=term-missing
```

Cobertura mínima: **80%**. A camada `core/` não importa PyQt6 — mantenha assim,
é o que permite testar tudo sem display gráfico.

## Regra inviolável: a whitelist

Qualquer mudança em `src/ultranx/config.py` (`PRESERVE_DIRS`, `PRESERVE_SUBPATHS`,
`DELETE_DIRS`) ou em `src/ultranx/core/sanitizer.py` precisa vir com teste
provando que nada protegido é removido. PRs que afrouxam a whitelist sem
justificativa técnica são recusados: um bug aqui apaga saves de gente real.

Se propõe remover uma pasta nova, o PR deve responder:

1. Qual conflito concreto ela causa após a atualização?
2. Por que sobrescrever não resolve?
3. Que dado do usuário pode existir lá dentro?

## Estilo

- Arquivos de 200–400 linhas, máximo 800; funções abaixo de 50 linhas.
- Estruturas de configuração imutáveis (`frozenset`, `tuple`, `dataclass(frozen=True)`).
- Erros sempre explícitos: converta exceções de OS/rede para a hierarquia de
  `core/errors.py`, com `guidance` acionável. Nunca engula exceção em silêncio.
- Toda I/O de rede ou disco roda em `QThread`; a UI só reage a sinais.
- Nada de segredos ou tokens no código — configuração vem de variável de ambiente.

## Commits

Conventional Commits: `feat:`, `fix:`, `refactor:`, `docs:`, `test:`, `chore:`,
`perf:`, `ci:`.

## Publicando um pacote novo

O UltraNX é só o cliente. Para publicar uma versão, o servidor precisa de:

- `packetVersion.txt` atualizado (uma linha, ex.: `1.4.2`);
- os ZIPs de cada modalidade;
- `manifest.json` com `url`, `sha256` e `size` de cada ZIP — sem isso os clientes
  baixam sem poder verificar integridade.

Gere o hash com `sha256sum arquivo.zip` (Linux) ou
`Get-FileHash arquivo.zip -Algorithm SHA256` (Windows).

## Reportando problemas

Inclua: sistema operacional, versão do UltraNX, versão local e remota do pacote,
e o log de `~/.ultranx/logs/ultranx.log`. Se a falha ocorreu durante a gravação,
há uma cópia do log em `<SD>/ultranx-logs/`.
