# Estratégia de testes

Três níveis, com o mesmo corte do app de PC: o que é lógica pura roda no CI;
o que depende do console roda no console.

| Nível | O quê | Onde | Ferramenta | Meta |
| --- | --- | --- | --- | --- |
| Unit | `src/core/` (manifest, paths, plan, version) | host (CI) | gcc + `greatest.h` + gcov/gcovr | cobertura de linhas ≥ 80% em `src/core/`, falha o CI abaixo disso |
| Unit Python | F2.H (app de PC) e F2.I (`make_release.py`) | host (CI) | pytest + pytest-cov | ≥ 80% nos módulos tocados; suíte existente continua verde |
| Integração | `src/platform/` (net, archive, system) | console real | `.nro` `selftest` | todos os casos `I*` passam |
| E2E | fluxo completo do app | console real + SD de teste | checklist manual §E2E | todos os itens marcados antes de release |

## TDD

Obrigatório em F2.A, F2.B, F2.C, F2.H e F2.I (`tdd-guide`): teste escrito e
falhando antes da implementação. Os casos de `security.md` §Whitelist (W01–W23)
e de `data-model.md` §Validação são a lista mínima — entram como testes antes
de existir `plan.c`/`manifest.c`.

## Unit no host (`make test`)

- `Makefile` alvo `test`: compila `src/core/*.c` + `tests/*.c` com
  `gcc -std=c11 -Wall -Wextra -Werror --coverage -fsanitize=address,undefined`.
- `src/core/` não inclui `<switch.h>`; dependência de jansson vem do pacote do
  sistema (`libjansson-dev`) no CI.
- SD falso: cada teste cria árvore em diretório temporário (`mkdtemp`) e a
  remove no teardown.
- Fixtures de manifest em `tests/fixtures/` (v1 atual, v2 standard, v2 full com
  3 partes, e um arquivo por regra de rejeição).
- `miniz` também compila no host: casos W20–W23 (zip-slip, destino
  protegido, zip bomb, excesso de entradas) usam zips gerados no próprio teste.

## Integração no console (`selftest.nro`)

Build separado (`make selftest`) que linka `src/platform/` e roda contra um
servidor local (`python -m http.server` + `tools/serve_https.py` com
certificado de teste instalado não é viável no console ⇒ casos TLS usam
GitHub Releases de um repo de teste público). Saída: lista `PASS/FAIL` na
tela e em `ultranx-nx/logs/selftest.log`.

| ID | Caso | Esperado |
| --- | --- | --- |
| I01 | Download 50 MB do GitHub Releases (302 → CDN) | SHA confere; progresso monotônico |
| I02 | Download interrompido (Wi-Fi desligado no meio) e retomado | Retoma por `Range` a partir do `.part`; SHA confere |
| I03 | SHA divergente | `.part` apagado; erro `UNX_E_HASH_MISMATCH`; nada renomeado |
| I04 | Servidor manda mais bytes que `size` | Abortado; erro de tamanho |
| I05 | Certificado inválido (host de teste com cert expirado) | Falha TLS; nada baixado |
| I06 | Extração de zip com 1.000 arquivos | Tudo extraído; tempo anotado |
| I07 | Zip-slip e destino protegido | Entradas descartadas; log WARNING |
| I08 | Bateria / carregador / espaço livre | Valores coerentes com o menu do sistema |
| I09 | Bloqueio de HOME e auto-sleep | HOME ignorado durante a fase; liberado ao sair |
| I10 | Reboot para payload | Console reinicia no hekate |

## Python

- **F2.H** (`tests/test_version_inspector.py`, `tests/test_sanitizer.py`):
  manifest v1 continua gerando o mesmo `PackageInfo`; v2 com `archives[]`
  múltiplos; `cleanup` ausente ⇒ defaults de `config.py`; `cleanup.delete`
  com item protegido ⇒ descartado com aviso; `preserve` só soma; caminhos
  inválidos ⇒ manifest rejeitado; paridade com `docs/whitelist.json`.
- **F2.I** (`tests/test_make_release.py`): pasta de pacote sintética fatiada
  com limite pequeno (ex.: 1 MiB) gera N zips válidos e independentes; nenhum
  arquivo duplicado entre partes; nenhum zip acima do limite; `sha256`, `size`
  e `extracted_size` batem com os arquivos; manifest passa em
  `manifest.v2.schema.json`; arquivo maior que o limite ⇒ erro claro.
- Schema: CI valida `docs/manifest.example.json` (v1) e exemplos v2 com
  `jsonschema`.

## E2E em hardware (§E2E)

Pré-condição: SD de teste com pacote versão N-1 instalado, saves de teste em
`Nintendo/`, `switch/JKSV/` com um backup, `prod.keys` em `switch/app/`, e
release de teste N publicada. Após cada item, conferir que `Nintendo/`,
`emummc/`, `switch/JKSV/` e `*.keys` estão byte-a-byte iguais (hash da árvore
antes/depois).

| ID | Cenário | Esperado |
| --- | --- | --- |
| E01 | Fluxo feliz, Pacote Padrão | Reinicia no hekate; boot no Atmosphère; `packetVersion.txt` = N; staging apagado |
| E02 | Fluxo feliz, Pacote Completo (3 partes) | Idem E01; partes aplicadas na ordem |
| E03 | Cancelar durante o download (antes da limpeza) | Nada apagado; `.part` mantido; reabrir retoma o download |
| E04 | Desligar o console durante o download | Ao religar, sistema N-1 intacto; app retoma download |
| E05 | Desligar o console durante a extração | Boot pode falhar ⇒ abrir app via hekate/hbmenu; tela de retomada pula limpeza, re-extrai partes pendentes, grava versão |
| E06 | Sem rede ao abrir | Tela de erro com orientação; nada alterado |
| E07 | SHA errado no manifest | Erro de integridade; nada apagado |
| E08 | Espaço insuficiente | Bloqueado na confirmação com valores necessário × livre; nada baixado |
| E09 | Bateria < 30% sem carregador | Aplicação bloqueada até conectar |
| E10 | Manifest inválido (ex.: `../x` em `cleanup`) | Erro "manifest inválido"; nada alterado |
| E11 | `min_updater` maior que a versão do app | Bloqueado com instrução de atualizar o app |
| E12 | `reboot_payload` ausente após extração | Instalação concluída; tela pede reinício manual |
| E13 | Manifest manda apagar `Nintendo` | Item descartado com aviso; resto do fluxo normal |

## Casos críticos (bloqueiam release)

W01–W06, W12–W14, W19–W22, I03, I05, E01, E02, E05, E07, E13 — são os que
protegem dado irrecuperável ou a capacidade de boot.
