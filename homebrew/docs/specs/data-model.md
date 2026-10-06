# Modelo de dados

Sem banco. Três artefatos: **manifest** (servidor), **`packetVersion.txt`**
(cartão) e **staging** (cartão, transitório). O manifest v2 é contrato
compartilhado entre o homebrew e o app de PC.

## Manifest v2 (`manifest.json`)

```json
{
  "schema": 2,
  "version": "1.5.0",
  "released": "2026-10-01",
  "min_updater": "1.0.0",
  "packages": {
    "standard": {
      "label": "Pacote Padrão",
      "archives": [
        {
          "url": "https://github.com/OWNER/rox-pack/releases/download/v1.5.0/rox-standard-1.5.0.part1.zip",
          "sha256": "<64 hex minúsculos>",
          "size": 734003200,
          "extracted_size": 812000000
        }
      ]
    },
    "full": {
      "label": "Pacote Completo (Android/Linux)",
      "archives": [
        { "url": "...part1.zip", "sha256": "...", "size": 1990000000, "extracted_size": 2100000000 },
        { "url": "...part2.zip", "sha256": "...", "size": 1990000000, "extracted_size": 2050000000 },
        { "url": "...part3.zip", "sha256": "...", "size": 1388709120, "extracted_size": 1500000000 }
      ]
    }
  },
  "cleanup": {
    "delete": [
      "atmosphere", "bootloader", "config", "sept", "warmboot_mariko",
      "stratosphere", "payload.bin", "reboot_payload.bin"
    ],
    "delete_children": ["switch"],
    "preserve": ["switch/JKSV", "switch/EdiZon", "switch/NX-Activity-Log"]
  },
  "reboot_payload": "atmosphere/reboot_payload.bin"
}
```

### Campos

| Campo | Tipo | Obrig. | Regra |
| --- | --- | --- | --- |
| `schema` | int | sim (v2) | `2`. Ausente ⇒ v1 |
| `version` | string | sim | `^[0-9]+(\.[0-9]+){0,3}$`; deve bater com linha 1 do `packetVersion.txt` remoto (divergência = aviso, manifest vence) |
| `released` | string | não | data ISO `YYYY-MM-DD` |
| `min_updater` | string | não | versão mínima do homebrew; cliente mais velho bloqueia e pede atualização do app |
| `packages` | objeto | sim | chaves conhecidas: `standard`, `full`; chaves desconhecidas ignoradas |
| `packages.*.label` | string | não | ≤ 64 chars; fallback = rótulo embutido |
| `packages.*.archives` | array | sim | 1–16 itens, aplicados **na ordem** (parte posterior sobrescreve anterior) |
| `archives[].url` | string | sim | `https://` absoluta, ou relativa à URL do manifest; ≤ 1024 chars |
| `archives[].sha256` | string | sim (v2) | 64 hex; comparado em minúsculas |
| `archives[].size` | int | sim | bytes, `1 ≤ size < 4294967295` (FAT32) |
| `archives[].extracted_size` | int | não | bytes; usado no cálculo de espaço; ausente ⇒ `size × 2` |
| `cleanup` | objeto | não | ausente ⇒ defaults embutidos (= `config.py` atual) |
| `cleanup.delete` | string[] | não | caminhos relativos à raiz, removidos inteiros; ≤ 64 itens |
| `cleanup.delete_children` | string[] | não | pastas mantidas, cada filho avaliado contra a whitelist e removido |
| `cleanup.preserve` | string[] | não | **soma** à whitelist embutida; nunca a reduz |
| `reboot_payload` | string | não | caminho relativo do payload pós-instalação; default `atmosphere/reboot_payload.bin`; ausente no SD ⇒ pede reinício manual |

### Compatibilidade v1

Manifest sem `schema` (formato atual de `docs/manifest.example.json`):
`packages.<m>.{url, sha256, size}` vira `archives: [{url, sha256, size}]`, e
`cleanup` usa os defaults embutidos. App de PC continua aceitando v1 e passa a
aceitar v2; homebrew aceita ambos.

### Validação (entrada rejeitada ⇒ manifest inteiro inválido, nada é feito)

1. JSON não-objeto, `version` ausente/fora do padrão.
2. Nenhuma modalidade válida em `packages`.
3. `archives` vazio, > 16, ou item sem `url`/`size`; `sha256` ausente em v2.
4. `url` com esquema diferente de `https` (exceto `http` para host de rede
   local quando `config.json` tiver `allow_http: true`).
5. Caminho em `cleanup.*` ou `reboot_payload` que: seja absoluto, contenha
   `..`, `\`, `:`, componente vazio, ou caractere de controle; tenha > 4
   componentes; ou > 255 chars.
6. Caminho de `cleanup.delete`/`delete_children` que caia na whitelist embutida
   **não** invalida o manifest — é descartado com aviso no log (falha segura,
   mesmo comportamento do `is_protected` do PC).

Comparações de caminho em casefold (FAT32 é case-insensitive).

### Limites

| Item | Limite | Ao exceder |
| --- | --- | --- |
| Tamanho do `manifest.json` | 256 KiB | manifest inválido |
| Entradas por zip | 200.000 | parte rejeitada antes de extrair |
| Bytes extraídos por parte | `extracted_size × 1,1` | extração abortada (zip bomb) |
| Payload de reboot | `0x2F000` bytes (IRAM) | pede reinício manual |

### Itens críticos

Definidos no core, não no manifest: `atmosphere/package3` e
`atmosphere/stratosphere.romfs`. Se um deles não puder ser removido, a
aplicação para com `UNX_E_REMOVE_CRITICAL` (misturar versões desses dois
impede o boot). Demais falhas de remoção viram aviso.

### Pasta a apagar com descendente protegido

Se um item de `cleanup.delete` contém um descendente protegido (ex.:
`preserve: ["atmosphere/contents/0100...ABC"]` ou um `*.keys` dentro), a pasta
**não** é removida inteira nem preservada inteira: vira remoção seletiva
recursiva — tudo sai, exceto os caminhos protegidos e seus ancestrais. Isso
diverge do PC atual (que protegeria a pasta inteira) e F2.H alinha o PC a esta
regra. Motivo: preservar `atmosphere/` inteira por causa de um arquivo
deixaria o sistema antigo misturado ao novo.

### Espaço necessário

`necessário = Σ archives[].size + Σ extracted_size + 64 MiB (margem) − bytes
já presentes no staging`. Partes ficam todas no staging antes da limpeza (download-antes-de-
apagar), por isso as duas somas. O espaço liberado pela limpeza **não** é
descontado: a checagem acontece antes de apagar qualquer coisa.

## Whitelist embutida (não sobreponível)

Igual a `src/ultranx/config.py` (`PRESERVE_DIRS`, `PRESERVE_SUBPATHS`,
`PRESERVE_ANY_DEPTH_SUFFIXES`, `PRESERVE_ROOT_FILES`,
`PRESERVE_ROOT_FILE_SUFFIXES`) mais:

- `switch/ultranx-nx` — pasta do próprio homebrew;
- `ultranx-nx` — staging e logs no console;
- `hbmenu.nro` — sem ele o usuário não volta ao app.

Fonte única: `docs/whitelist.json` gerado de `config.py` e convertido em header
C no build (`include/ultranx/whitelist_gen.h`), para PC e console não divergirem.

## `packetVersion.txt` (raiz do SD)

Formato inalterado:

```
1.5.0
2026-10-01
```

Linha 1 = versão (estado). Linha 2 = data de lançamento ISO (metadado,
opcional). Gravado ao **final** da extração com `fsync` e relido para
confirmar. Ausente ⇒ "nenhuma versão instalada".

## Staging (`sdmc:/ultranx-nx/`)

```
ultranx-nx/
  config.json              # manifest_url, allow_http (opcional)
  staging/
    state.json             # progresso da aplicação
    APPLYING               # marcador: existe só entre início da limpeza e fim da gravação da versão
    part1.zip ...          # partes baixadas (*.part enquanto incompletas)
  logs/ultranx-nx.log      # rotação: mantém 3 últimos
```

`state.json`:

```json
{
  "version": "1.5.0",
  "modality": "full",
  "archives": [
    { "file": "part1.zip", "sha256": "...", "size": 1990000000, "verified": true, "extracted": true },
    { "file": "part2.zip", "sha256": "...", "size": 1990000000, "verified": true, "extracted": false }
  ],
  "cleanup_done": true
}
```

Regras:
- Download grava em `partN.zip.part`; renomeia para `partN.zip` só após
  SHA-256 conferir. Retomada usa o tamanho do `.part` como `Range`.
- `APPLYING` presente na abertura do app ⇒ tela de retomada: pula
  limpeza se `cleanup_done`, re-extrai partes com `extracted: false` (na ordem),
  grava versão, apaga staging.
- Staging é apagado só após `packetVersion.txt` confirmado.
- `state.json` gravado com escrita em arquivo temporário + rename.

## Configuração do homebrew (`ultranx-nx/config.json`)

```json
{ "manifest_url": "https://github.com/OWNER/rox-pack/releases/latest/download/manifest.json" }
```

Ausente ⇒ URL embutida no build (`ULTRANX_MANIFEST_URL` no Makefile).
