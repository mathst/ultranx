# Arquitetura do UltraNX

## Camadas

```
ui/            PyQt6: widgets, diálogos, máquina de estados da tela
  ^ sinais
workers/       QThread: isola I/O do event loop; traduz exceção -> FailureReport
  ^ chamadas diretas
core/          domínio puro (nenhum import de PyQt6): regras e I/O
config.py      constantes imutáveis + Settings resolvido do ambiente
```

Regra estrutural: `core/` **não** importa PyQt6. É o que permite testar limpeza,
integridade e comparação de versão sem display gráfico, e é verificado
indiretamente pela suíte rodando headless no CI.

## Módulos de domínio

| Módulo                 | Responsabilidade                                                        |
| ---------------------- | ----------------------------------------------------------------------- |
| `drive_detector.py`    | Varre `psutil.disk_partitions()`, filtra FAT32/exFAT removível, valida raiz escolhida à mão |
| `version_inspector.py` | Busca `packetVersion.txt` remoto + `manifest.json`, compara com o local |
| `sanitizer.py`         | Plano de limpeza por whitelist estrita e sua execução                   |
| `installer.py`         | Download em chunks, verificação SHA-256, extração, gravação da versão   |
| `recovery.py`          | Preserva log, monta relatório de falha, finaliza a mídia                 |
| `paths.py`             | Contenção de caminho, casefold, anti-traversal                          |
| `errors.py`            | Hierarquia de exceções, cada uma com orientação de recuperação           |

## Concorrência

Dois workers `QThread`, um por operação, ambos com o mesmo contrato: emitem
**exatamente um** de `finished_ok` ou `failed`.

- `VersionWorker` — I/O de rede da inspeção.
- `UpdateWorker` — limpeza, download, extração e finalização, com progresso
  fatiado em faixas fixas (0–10 limpeza, 10–70 download, 70–95 extração,
  95–100 finalização) para a barra avançar monotonicamente.

Callbacks de progresso do `core` são funções síncronas comuns; o worker apenas
as encaminha para `pyqtSignal.emit`. É o que mantém `core/` livre de Qt.

Cada etapa tem seu próprio `RateEstimator` (`core/progress.py`): as unidades são
diferentes — itens na limpeza, bytes no download, entradas de ZIP na extração — e
a vazão de uma não prevê a da outra. O relógio entra por parâmetro, com
`time.monotonic` como padrão, o que mantém os testes determinísticos e protege a
conta de ajuste de horário do sistema no meio da gravação.

Duas guardas evitam estimativa mentirosa: amostras separadas por menos de 0,35 s
são descartadas (chunk vindo de cache infla a vazão) e nada é estimado antes de
2% de progresso, onde qualquer conta erra por ordens de magnitude.

Cancelamento é **cooperativo**: `request_cancel()` levanta uma flag que as
funções de core consultam entre chunks e entradas do ZIP. `QThread.terminate()`
nunca é usado — matar a thread no meio de uma escrita FAT32 deixaria o cartão em
estado indefinido.

## O modelo de segurança da limpeza

Três camadas independentes, todas obrigatórias:

1. **Contenção** — `is_within(root, candidate)` compara caminhos resolvidos.
   Neutraliza `..`, symlinks e barras invertidas.
2. **Whitelist na montagem do plano** — `is_protected()` decide item por item.
   Em conflito, a whitelist vence a lista de remoção. Desconhecido na raiz é
   preservado (falha segura).
3. **Revalidação na execução** — `execute_plan()` chama `is_protected()` de novo
   antes de cada remoção. Um plano adulterado não apaga nada protegido.

O sanitizer varre o **primeiro nível** da raiz e desce um único nível extra nas
pastas de `PARTIAL_DELETE_DIRS`. Isso é deliberado: mantém a whitelist auditável
a olho nu e limita o dano possível de um bug.

A remoção existe porque sobrescrever não basta: o mesmo nome de arquivo pode
carregar conteúdo de outra versão, e um órfão que o pacote novo não repõe segue
sendo lido como válido. `config/` entra nessa categoria — é substituída inteira
pelo pacote.

`PARTIAL_DELETE_DIRS` existe porque `switch/` mistura, no mesmo lugar, apps que o
pacote repõe e dado que ele não repõe (backups do JKSV, `*.keys`). Ela é limpa
filho por filho, e o diretório em si permanece para a extração repovoar.

Detalhe de implementação que é fácil quebrar: ser ancestral de um subcaminho
protegido normalmente protege a pasta inteira (é o que segura `themes/` por causa
de `themes/ThemezerNX`). Para as pastas de `PARTIAL_DELETE_DIRS` essa regra é
suspensa de propósito — sem isso, `switch/` seria protegida por ser pai de
`switch/JKSV` e a limpeza seletiva nunca aconteceria.

## Integridade do payload

O download vai para arquivo temporário — nunca direto sobre a raiz — com SHA-256
calculado em streaming durante a escrita. Divergência de hash ou de tamanho
descarta o temporário antes de qualquer gravação no cartão.

Na extração, cada entrada do ZIP passa por `join_within()`; entradas que
escapariam da raiz (zip-slip) são descartadas com log em WARNING.

O temporário fica no próprio SD quando há espaço (extração local, mais rápida) e
cai para o temp do sistema quando não há. É sempre removido, inclusive em falha.

## Estado

Nenhum banco. `packetVersion.txt` na raiz do cartão guarda a versão na primeira
linha e a data de lançamento dela, em ISO-8601, na segunda. A validação pós-escrita
compara **só a primeira linha**: a versão é o estado, a data é metadado.

A data de instalação não é gravada em lugar nenhum — é o mtime do arquivo. Isso
evita ler o relógio dentro do domínio e mantém os testes determinísticos.

O arquivo é gravado com `fsync` e **relido para confirmação** — em FAT32 uma
escrita aparentemente bem-sucedida pode não persistir se o cartão sair antes do
flush.

## Falhas

Todo erro esperado é convertido para a hierarquia de `core/errors.py`, cada
classe carregando um `guidance` acionável. O worker embala isso num
`FailureReport` que inclui a etapa, se o cartão pode estar em estado parcial e
onde o log foi preservado. A UI mostra o relatório; o usuário nunca vê traceback.
