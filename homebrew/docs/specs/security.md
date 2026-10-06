# Segurança

Escopo: homebrew `homebrew/` (console) e a parte do app de PC que passa a
consumir `cleanup` do manifest v2 (F2.H). O ativo a proteger é o **dado
irrecuperável do usuário no SD** (saves, `emummc`, `*.keys`, backups do JKSV) e
a **capacidade do console de dar boot**.

## Modelo de ameaças (STRIDE)

| # | Categoria | Ameaça | Mitigação |
| --- | --- | --- | --- |
| T1 | Spoofing | MITM na rede (Wi-Fi público, DNS envenenado) entrega manifest/zip falso | TLS com `CURLOPT_SSL_VERIFYPEER=1` e `VERIFYHOST=2`, backend libnx (CA do sistema); URL do manifest fixa no binário; partes só aceitas sob `UNX_ALLOWED_ARCHIVE_PREFIX` (https do GitHub); `http://` só em build `DEV=1` |
| T2 | Tampering | Manifest adulterado manda apagar dado do usuário | Whitelist embutida vence sempre (§Whitelist); `preserve` do manifest só soma; caminho protegido é descartado com aviso, nunca removido |
| T3 | Tampering | Zip trocado/corrompido em trânsito ou no host | SHA-256 de cada parte (`archives[].sha256`) conferido antes de renomear `.part`; nada é apagado antes de **todas** as partes conferirem |
| T4 | Tampering | Servidor (conta GitHub) comprometido publica manifest + zip coerentes e maliciosos | Fora do alcance do SHA (o atacante controla os dois). Dano limitado pela whitelist e pelos limites de extração. Evolução: assinatura ed25519 (§Evolução) |
| T5 | Tampering | Path traversal em `cleanup.*` / `reboot_payload` (`../`, absoluto, `\`, `:`) | Validação de `data-model.md` §Validação item 5 **rejeita o manifest inteiro**; contenção revalidada na execução (`paths_is_within`) |
| T6 | Tampering | Zip-slip: entrada `../../x`, absoluta, com `\` ou `:` | Cada entrada passa por `paths_join_within(root, name)`; fora da raiz ⇒ descartada com WARNING; entrada que cairia em caminho protegido da whitelist ⇒ descartada (pacote não sobrescreve save) |
| T7 | Tampering | Symlink em zip | miniz não cria symlinks; entradas com atributo de link são ignoradas |
| T8 | Repudiation | Usuário não sabe o que foi apagado | Log em `ultranx-nx/logs/` lista cada item removido/preservado e cada entrada descartada |
| T9 | Info disclosure | Log expõe dado sensível | Log só registra caminhos relativos do SD, versões, tamanhos, códigos de erro. **Nunca**: conteúdo de arquivo, nome de save/título, número de série, MAC, token, cabeçalhos HTTP completos. Query string de URL é cortada |
| T10 | DoS | Zip bomb | Aborta extração se bytes escritos > `extracted_size × 1,1` da parte, ou se entradas > 200.000, ou se uma entrada declarar > 4 GiB − 1 (FAT32); checagem antes **e** durante a escrita (tamanho declarado no zip não é confiável) |
| T11 | DoS | Esgotamento de espaço no SD no meio da aplicação | Pré-checagem antes de baixar: `livre ≥ Σ size + Σ extracted_size + 64 MiB`; se falhar, nada é baixado nem apagado. Download aborta se `Content-Length` ou bytes recebidos > `size` declarado |
| T12 | DoS | Manifest gigante / resposta infinita | Manifest limitado a 256 KiB (leitura cortada ⇒ inválido); timeouts de conexão (15 s) e de baixa vazão (`LOW_SPEED_LIMIT` 1 KiB/s por 60 s) |
| T13 | DoS | Arquivo de `cleanup` aberto por sysmodule bloqueia remoção | Falha vira aviso; aborta só se o item for crítico (`atmosphere/package3`) — ver `architecture.md` §Falhas |
| T14 | Elevation | Manifest força downgrade para pacote antigo vulnerável | Versão remota menor que a instalada exige confirmação explícita na tela (sem atalho padrão) |
| T15 | Elevation | `min_updater` / `reboot_payload` apontando para binário arbitrário | `reboot_payload` validado como caminho relativo contido e deve existir após extração; tamanho do payload ≤ 0x2F000 (limite da IRAM do `amsBpcSetRebootPayload`); fora disso pede reinício manual |

## Whitelist

Fonte única: `docs/whitelist.json` gerado de `src/ultranx/config.py`, mais
`switch/ultranx-nx`, `ultranx-nx` e `hbmenu.nro` (`data-model.md`). Comparações
em casefold. A whitelist é consultada **na montagem do plano e de novo antes de
cada remoção** (mesmas três camadas do sanitizer do PC: contenção → whitelist →
revalidação).

### Casos de teste obrigatórios (F2.B, `tests/test_plan.c`)

| ID | Entrada (manifest) | SD de teste contém | Esperado |
| --- | --- | --- | --- |
| W01 | `delete: ["Nintendo"]` | `Nintendo/` | Descartado com aviso; `Nintendo/` intacto; manifest válido |
| W02 | `delete: ["emummc"]` | `emummc/` | Descartado com aviso; intacto |
| W03 | `delete: ["switch"]` | `switch/JKSV/`, `switch/app/` | Remoção seletiva recursiva (`data-model.md`): `switch/app/` removido, `switch/JKSV/` preservado, `switch/` mantida |
| W04 | `delete_children: ["switch"]` | `switch/JKSV/`, `switch/app/` | `switch/app/` removido; `switch/JKSV/` preservado |
| W05 | `delete: ["switch/JKSV"]` | `switch/JKSV/` | Descartado com aviso; intacto |
| W06 | `delete: ["switch/app"]` | `switch/app/prod.keys`, `switch/app/a.nro` | `switch/app/` não é removido inteiro; `a.nro` removido, `prod.keys` preservado (sufixo protegido em qualquer profundidade) |
| W07 | `delete: ["../x"]` | — | Manifest **inválido** (item 5) |
| W08 | `delete: ["/atmosphere"]` | — | Manifest **inválido** (absoluto) |
| W09 | `delete: ["ATMOSPHERE"]` | `atmosphere/` | Removido (casefold casa com `atmosphere`) |
| W10 | `delete: ["atmosphere\\x"]` | — | Manifest **inválido** (`\`) |
| W11 | `delete: ["a/b/c/d/e"]` | — | Manifest **inválido** (> 4 componentes) |
| W12 | `delete: ["switch/ultranx-nx"]` | `switch/ultranx-nx/` | Descartado com aviso; intacto |
| W13 | `delete: ["hbmenu.nro"]` | `hbmenu.nro` | Descartado com aviso; intacto |
| W14 | `delete: ["ultranx-nx"]` | `ultranx-nx/staging/` | Descartado com aviso; staging intacto |
| W15 | `delete: ["sd:/x"]` / `"x:y"` | — | Manifest **inválido** (`:`) |
| W16 | `delete: ["atmosphere//x"]` | — | Manifest **inválido** (componente vazio) |
| W17 | `delete: ["atm\u0001"]` | — | Manifest **inválido** (caractere de controle) |
| W18 | `preserve: ["atmosphere/contents/0100"]` + `delete: ["atmosphere"]` | `atmosphere/contents/0100/` | Remoção seletiva recursiva: todo o resto de `atmosphere/` sai; `atmosphere/contents/0100/` e seus ancestrais ficam |
| W19 | Plano adulterado em memória após montagem inclui `Nintendo` | `Nintendo/` | Revalidação na execução recusa; WARNING `GUARD` |
| W20 | Entrada zip `../../Nintendo/x` | — | Descartada com WARNING; nada escrito fora da raiz |
| W21 | Entrada zip `emummc/x.bin` | `emummc/` | Descartada (destino protegido) |
| W22 | Parte extrai > `extracted_size × 1,1` | — | Extração abortada, erro `UNX_E_ARCHIVE_TOO_LARGE`, staging mantido para diagnóstico |
| W23 | Zip com 200.001 entradas | — | Abortado antes de escrever |

W01–W19 rodam no host contra um SD falso em diretório temporário. W20–W23 rodam
no host (miniz compila no host) **e** no `selftest` do console.

## Segredos

O homebrew não tem segredo: URLs públicas, sem token. Repositório do pacote
precisa ser público (download anônimo).

## Origem fixa

URL do manifest e prefixo das partes são constantes de
`include/ultranx/build_config.h`; o `.nro` de release não lê configuração do
cartão. Quem pode mudar o que é instalado: só quem publica release em
`mathst/rox-pack`. Proteja essa conta (2FA, sem tokens de escrita soltos) —
ela é a raiz de confiança até a assinatura ed25519 (evolução abaixo).

## Rate limit

O GitHub limita downloads anônimos por IP. O cliente faz no máximo 3
tentativas por parte, com backoff exponencial (2 s, 8 s, 30 s), e retoma via
`Range` em vez de reiniciar do zero.

## Evolução (fora de escopo v1)

Assinatura do manifest com **ed25519**: `manifest.json.sig` publicado ao lado,
chave pública embutida no `.nro` e no app de PC, verificação antes de qualquer
uso do manifest. Fecha T4 (conta do servidor comprometida). Fica de fora da v1
porque a whitelist embutida já impede a perda de dado irrecuperável; o dano
residual é um pacote ruim, recuperável reinstalando pelo PC.
