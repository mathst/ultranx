# Contratos de API

O UltraNX-NX não expõe API: ele **consome** HTTP do GitHub Releases. Este
documento fixa o que o cliente pede, o que espera de volta e como cada resposta
vira erro. O formato do manifest está em [`data-model.md`](data-model.md).

## Regras gerais do cliente HTTP

| Item | Valor |
| --- | --- |
| Biblioteca | libcurl (`switch-curl`, backend TLS da libnx) |
| TLS | `CURLOPT_SSL_VERIFYPEER=1`, `CURLOPT_SSL_VERIFYHOST=2`; nunca desligado |
| Redirects | `CURLOPT_FOLLOWLOCATION=1`, máximo 5; só `https://` (`CURLOPT_REDIR_PROTOCOLS_STR="https"`), exceto `http` quando `allow_http` |
| User-Agent | `UltraNX-NX/<versão do app>` (o GitHub rejeita requisição sem UA) |
| Timeout de conexão | 15 s (`CURLOPT_CONNECTTIMEOUT`) |
| Timeout de baixa vazão | aborta se < 1 KiB/s por 30 s (`LOW_SPEED_LIMIT`/`LOW_SPEED_TIME`); sem timeout total, porque partes de 1,9 GB levam muito tempo |
| Compressão | `Accept-Encoding` desligado nas partes (o zip já é comprimido e o SHA é do corpo bruto) |
| Retries | 3 tentativas com backoff 2 s → 4 s → 8 s, só para erros transitórios (ver tabela) |
| Pré-checagem | `nifmIsAnyInternetRequestAccepted`; sem conexão ⇒ `UNX_E_NET_OFFLINE` sem tentar |

## 1. Manifest

```
GET https://github.com/OWNER/rox-pack/releases/latest/download/manifest.json
User-Agent: UltraNX-NX/1.0.0
```

O GitHub responde `302` para `release-assets.githubusercontent.com` ou
`objects.githubusercontent.com` (URL assinada e temporária); o curl segue o
redirect. A resposta final esperada é `200` com JSON.

- Limite de corpo: 1 MiB. Acima disso ⇒ `UNX_E_MANIFEST_INVALID`.
- O conteúdo do `Content-Type` é ignorado (o GitHub serve `application/octet-stream`).
- URLs relativas em `archives[].url` são resolvidas contra a URL **original**
  do manifest (antes dos redirects), não contra a URL assinada.
- `latest/download/` sempre aponta para a release mais recente. Para fixar uma
  versão em testes, `config.json` pode usar `releases/download/vX.Y.Z/manifest.json`.

## 2. `packetVersion.txt` remoto (opcional)

```
GET <diretório do manifest>/packetVersion.txt
```

Só para checagem cruzada: se existir e a linha 1 divergir de
`manifest.version`, registra aviso no log e o manifest vence. `404` é
ignorado. Não bloqueia o fluxo.

## 3. Partes do pacote

```
GET <archives[i].url>
User-Agent: UltraNX-NX/1.0.0
Range: bytes=<tamanho do .part>-        # só quando retomando
```

| Resposta | Ação |
| --- | --- |
| `200` sem `Range` enviado | grava do início |
| `200` com `Range` enviado | servidor ignorou o Range: trunca o `.part` e grava do início |
| `206` | confere `Content-Range: bytes <início>-*/<total>`; `<início>` igual ao tamanho do `.part` e `<total>` igual a `size`, senão trunca e recomeça sem Range |
| `416` | `.part` já tem o tamanho todo (ou mais): verifica o SHA; se divergir, apaga e recomeça |

Durante a gravação, o SHA-256 é calculado em streaming. Na retomada, o trecho
já baixado é re-hasheado do disco antes de continuar. Ao terminar:
`tamanho == size` e `sha256 == archives[i].sha256`, senão o `.part` é apagado
e o erro é `UNX_E_SIZE_MISMATCH` / `UNX_E_HASH_MISMATCH`. Se o corpo passar
de `size`, o download aborta na hora com `UNX_E_SIZE_MISMATCH`.

A URL assinada do redirect expira (cerca de 5 min no GitHub); por isso toda
tentativa, inclusive retry, começa pela URL do manifest, e não pela URL final.

## Mapeamento de status para erro

| Condição | Transitório (retry)? | `UnxCode` |
| --- | --- | --- |
| DNS/conexão recusada (`CURLE_COULDNT_RESOLVE_HOST`, `CURLE_COULDNT_CONNECT`) | sim | `UNX_E_NET_OFFLINE` |
| `CURLE_OPERATION_TIMEDOUT`, baixa vazão | sim | `UNX_E_NET_TIMEOUT` |
| `CURLE_RECV_ERROR`, `CURLE_PARTIAL_FILE` | sim (retoma com Range) | `UNX_E_NET_TIMEOUT` |
| `CURLE_SSL_*`, `CURLE_PEER_FAILED_VERIFICATION` | não | `UNX_E_NET_TLS` |
| `CURLE_TOO_MANY_REDIRECTS`, redirect para http | não | `UNX_E_NET_HTTP` |
| `404` | não | `UNX_E_NET_HTTP` (no manifest: "pacote não publicado") |
| `403`, `429` (rate limit) | sim, respeitando `Retry-After` até 60 s | `UNX_E_NET_HTTP` |
| `5xx` | sim | `UNX_E_NET_HTTP` |
| outros `4xx` | não | `UNX_E_NET_HTTP` |
| `CURLE_ABORTED_BY_CALLBACK` (B pressionado) | não | `UNX_E_CANCELLED` |
| `CURLE_WRITE_ERROR` (falha ao gravar no SD) | não | `UNX_E_IO` (ou `UNX_E_NO_SPACE` se `errno == ENOSPC`) |

`UnxError.message` inclui o host (nunca a query string assinada) e o status
HTTP ou o código do curl.

## Envelope de erro interno

Todas as camadas devolvem `bool` + `UnxError *err` preenchido em caso de falha
(definição em `architecture.md` §`errors.h`):

```c
typedef struct {
    UnxCode  code;          /* categoria, decide a orientação */
    UnxStage stage;         /* etapa em que ocorreu */
    char     message[256];  /* detalhe técnico: caminho, host, status */
    const char *guidance;   /* texto fixo por código, mostrado ao usuário */
    bool     card_partial;  /* true ⇒ a tela de erro manda reabrir para retomar */
} UnxError;
```

Exemplo do que a tela recebe:

```
code=UNX_E_HASH_MISMATCH stage=UNX_STAGE_DOWNLOAD card_partial=false
message="part2.zip: sha256 3f9a…e1 != esperado 7c02…4b"
guidance="O download veio corrompido. Tente de novo; se repetir, o pacote publicado está com erro."
```

## CLI `tools/make_release.py`

Gera as partes e o manifest a partir de uma pasta com a raiz do SD já montada.
Só stdlib (`zipfile`, `hashlib`, `json`, `argparse`).

```
python tools/make_release.py \
  --version 1.5.0 \
  --released 2026-10-01 \
  --modality standard=build/rox-standard \
  --modality full=build/rox-full \
  --base-url https://github.com/OWNER/rox-pack/releases/download/v1.5.0/ \
  --out dist/v1.5.0 \
  [--part-size 1900M] [--cleanup cleanup.json] [--min-updater 1.0.0] \
  [--reboot-payload atmosphere/reboot_payload.bin]
```

| Argumento | Obrig. | Significado |
| --- | --- | --- |
| `--version` | sim | vai para `manifest.version` e para `packetVersion.txt` |
| `--released` | não | ISO `YYYY-MM-DD`; default = hoje |
| `--modality nome=pasta` | sim, repetível | `standard`/`full` → pasta com a raiz do SD |
| `--base-url` | não | prefixo das URLs das partes; ausente ⇒ URLs relativas (nome do arquivo) |
| `--out` | sim | pasta de saída (criada; falha se não estiver vazia) |
| `--part-size` | não | teto por parte, default `1900M`; máximo `1990M` (limite de 2 GB do GitHub) |
| `--cleanup` | não | JSON com `delete`/`delete_children`/`preserve`; ausente ⇒ omite `cleanup` (cliente usa os defaults) |
| `--min-updater`, `--reboot-payload` | não | copiados para o manifest |

Comportamento:
- Arquivos ordenados por caminho e distribuídos em partes sem passar do teto;
  um arquivo nunca é dividido entre partes. Um arquivo maior que o teto ⇒ erro.
- Cada parte é um zip válido e independente (deflate), com nome
  `rox-<modalidade>-<versão>.partN.zip`.
- Caminhos dentro do zip usam `/`, sem `..` e sem caminho absoluto.
- `cleanup` passa pelas mesmas regras de validação de caminho de
  `data-model.md` §Validação, e o script falha se algum item cair na whitelist
  embutida.
- Escreve `manifest.json` (schema 2) e `packetVersion.txt` em `--out` e, no
  fim, valida o manifest contra `docs/manifest.v2.schema.json`.

Saída (stdout):

```
standard: 1 parte(s), 734.0 MB → dist/v1.5.0/rox-standard-1.5.0.part1.zip
full:     3 parte(s), 5.37 GB → dist/v1.5.0/rox-full-1.5.0.part{1..3}.zip
manifest: dist/v1.5.0/manifest.json (schema 2, válido)
```

Código de saída: `0` ok, `1` erro de entrada/validação, `2` erro de I/O.

Publicação (manual ou CI):

```
gh release create v1.5.0 dist/v1.5.0/* --repo OWNER/rox-pack
```
