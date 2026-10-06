# Deploy, CI e operação

Dois artefatos distintos, com ciclos de release independentes:

| Artefato | Onde vive | Tag | Quem consome |
| --- | --- | --- | --- |
| Homebrew `ultranx-nx.nro` | este repositório (`homebrew/`) | `homebrew-vX.Y.Z` | usuário instala uma vez; depois vem dentro do pacote |
| Pacote R O X (partes `.zip` + `manifest.json`) | repositório público separado `OWNER/rox-pack` | `vX.Y.Z` | homebrew e app de PC, via manifest |

O app de PC mantém o fluxo atual (`build.yml`, tags `v*`). A tag do homebrew
usa prefixo próprio para não disparar o build do PC.

## Ambientes

### Desenvolvimento

- Servidor HTTP local no PC servindo uma pasta com `manifest.json` e partes:
  `python -m http.server 8000 --directory ./release-out`.
- No console, `sdmc:/ultranx-nx/config.json`:

  ```json
  { "manifest_url": "http://192.168.0.10:8000/manifest.json", "allow_http": true }
  ```

  `allow_http` só vale para host de rede local (regra 4 de `data-model.md`
  §Validação); fora disso, `http://` invalida o manifest.
- Envio do `.nro` sem tirar o cartão: `nxlink -s ultranx-nx.nro` (Homebrew Menu
  com `Y` pressionado), que também espelha o `stdout` no terminal do PC.
- SD de teste dedicado: nunca o cartão de uso diário (ver `testing.md` §E2E).

### Produção

- `manifest_url` padrão embutido no build:
  `https://github.com/OWNER/rox-pack/releases/latest/download/manifest.json`.
  O GitHub redireciona `latest/download/<arquivo>` para o asset da release
  marcada como *latest*. É uma URL estável que nunca muda entre versões.
- As URLs das partes dentro do manifest apontam para a tag fixa
  (`releases/download/vX.Y.Z/...`), então um manifest antigo continua
  resolvendo as próprias partes.

## Configuração

Resolução do endereço do manifest, da mais forte para a mais fraca (mesmo
modelo de três camadas do PC, sem variável de ambiente, que não existe no
console):

1. `sdmc:/ultranx-nx/config.json` → `manifest_url`.
2. Constante de build `ULTRANX_MANIFEST_URL` (Makefile, passada como
   `-DULTRANX_MANIFEST_URL="..."`; sobrescrevível com
   `make ULTRANX_MANIFEST_URL=...`).

`config.json` inválido (JSON quebrado, URL fora das regras) é ignorado com
aviso no log e na tela inicial, e cai na constante de build. Nunca trava o app.

Versão do homebrew: `APP_VERSION` no Makefile, gravada no NACP do `.nro` e
comparada com `min_updater` do manifest.

## CI (`.github/workflows/homebrew.yml`)

Disparo: `push`/`pull_request` com `paths: ["homebrew/**", "tools/**",
".github/workflows/homebrew.yml"]`, mais `workflow_dispatch` e tags
`homebrew-v*`. `permissions: contents: read` por padrão (igual ao `ci.yml`).
`timeout-minutes` em todo job, pelo mesmo motivo registrado no `ci.yml`: um
mirror pendurado não pode segurar o job por horas.

### Job `host-test` (ubuntu-latest, `timeout-minutes: 10`)

1. `make -C homebrew test COVERAGE=1`: compila `src/core/` + `tests/` com gcc
   `-std=c11 -Wall -Wextra -Werror -fsanitize=address,undefined --coverage`.
2. `gcovr -r homebrew --filter homebrew/src/core --fail-under-line 80`.
3. Validação de schema: `python -m jsonschema` de
   `homebrew/docs/manifest.v2.schema.json` contra os exemplos v1/v2.
4. `pytest tests/test_make_release.py` (gerador de release).
5. Upload de `coverage.xml` como artefato.

### Job `nro-build` (container `devkitpro/devkita64`, `timeout-minutes: 15`)

1. `dkp-pacman -Syu --noconfirm switch-curl switch-jansson switch-zlib`,
   com `timeout-minutes: 6` no passo. As versões instaladas são impressas
   (`dkp-pacman -Q`) e comparadas às anotadas na F0 de `architecture.md`.
2. `make -C homebrew` gera `homebrew/ultranx-nx.nro`.
3. Checagem: o `.nro` existe e o tamanho fica abaixo de 8 MB (sem romfs; um
   tamanho fora disso indica asset embutido por engano).
4. `actions/upload-artifact@v4` com `name: ultranx-nx`,
   `if-no-files-found: error`.

### Job `release` (só em tag `homebrew-v*`)

`needs: [host-test, nro-build]`, `permissions: contents: write`. Baixa o
artefato e publica com `softprops/action-gh-release@v2` (mesmo padrão do
`build.yml`), anexando `ultranx-nx.nro` e o `sha256` dele em
`ultranx-nx.nro.sha256`.

## Publicação do pacote R O X

Repositório `OWNER/rox-pack`, **público**: o console baixa sem token. As partes
nunca vão para o git, só para os assets da release (limite de 2 GB por asset).

Passo a passo para publicar `1.5.0`:

1. Montar a árvore do pacote por modalidade, exatamente como deve ficar na
   raiz do SD (`pack/standard/`, `pack/full/`), incluindo
   `switch/ultranx-nx/ultranx-nx.nro` da última release do homebrew e
   `atmosphere/reboot_payload.bin` (hekate).
2. Gerar partes e manifest:

   ```bash
   python tools/make_release.py \
     --version 1.5.0 --released 2026-10-01 \
     --standard pack/standard --full pack/full \
     --base-url https://github.com/OWNER/rox-pack/releases/download/v1.5.0 \
     --cleanup cleanup.json \
     --out release-out/
   ```

   Saída: `rox-<modalidade>-1.5.0.partN.zip` (≤ 1,9 GB cada, zip independente),
   `manifest.json` (v2, já validado contra o schema) e `packetVersion.txt`.
3. Testar localmente: servir `release-out/` com `http.server` e rodar o fluxo
   completo num SD de teste (PC e console).
4. Publicar:

   ```bash
   gh release create v1.5.0 --repo OWNER/rox-pack --latest \
     --title "R O X 1.5.0" --notes-file NOTES.md \
     release-out/*.zip release-out/manifest.json release-out/packetVersion.txt
   ```

   O `manifest.json` é anexado **por último na mesma chamada**. Como a release
   só aparece como *latest* quando `gh release create` termina, o console nunca
   vê um manifest apontando para partes ainda em upload.
5. Conferir:
   `curl -sL https://github.com/OWNER/rox-pack/releases/latest/download/manifest.json`
   retorna `"version": "1.5.0"`.

O app de PC passa a apontar para a mesma URL. O `packetVersion.txt` anexado à
release mantém a resolução por convenção do PC
(`base_url/packetVersion.txt`).

## Instalação no console

Primeira vez (manual, uma única vez):

1. Baixar `ultranx-nx.nro` da release `homebrew-vX.Y.Z`.
2. Copiar para `sdmc:/switch/ultranx-nx/ultranx-nx.nro`.
3. Abrir pelo Homebrew Menu (preferencialmente pelo modo título, segurando R
   ao abrir um jogo, que dá mais memória do que o modo applet do Álbum).

Depois disso o app se atualiza junto com o pacote: o R O X traz
`switch/ultranx-nx/ultranx-nx.nro`, a extração sobrescreve o arquivo (seguro
porque o app não usa romfs e o `.nro` não fica aberto) e a pasta continua
protegida da limpeza pela whitelist embutida.

Se `min_updater` do manifest for maior que a versão do app, a tela inicial
bloqueia a atualização e mostra o link da release do homebrew. Esse é o único
caso que exige cópia manual de novo.

## Observabilidade

Sem telemetria e sem rede além do manifest e das partes. Tudo fica no SD:

- `sdmc:/ultranx-nx/logs/ultranx-nx.log`, com rotação na abertura do app:
  `ultranx-nx.log` → `.1` → `.2`, mantendo 3 arquivos.
- Formato de linha: `[+segundos-desde-abertura] NÍVEL etapa: mensagem`. O
  relógio do console não é confiável, então usa tempo relativo.
- O que é logado:
  - versões (app, instalada, manifest, `schema`) e `manifest_url` efetiva
    (sem query string);
  - modalidade escolhida, bateria e espaço livre no início;
  - plano de limpeza completo, incluindo os itens do manifest descartados
    pela whitelist (nível WARNING);
  - por parte: URL, tamanho, retomadas por `Range`, vazão média, SHA-256
    esperado × obtido;
  - entradas de zip descartadas por zip-slip (WARNING);
  - falhas de remoção e se foram toleradas ou abortaram;
  - transições do `state.json` e presença do marcador `APPLYING`;
  - resultado final e o payload usado no reboot.
- Nunca é logado: conteúdo de saves ou keys, nem lista de arquivos do usuário
  fora dos itens do plano.
- O log é gravado com `fflush` a cada linha. Se o app travar, o que aconteceu
  até ali está no cartão para ser lido no PC.

## Rollback

### Pacote publicado com defeito

1. Na `OWNER/rox-pack`, marcar a release anterior boa como *latest*:
   `gh release edit v1.4.2 --latest`. O `releases/latest/download/manifest.json`
   volta a apontar para ela imediatamente.
2. Opcional: rebaixar a release ruim a *pre-release* ou apagá-la. As partes da
   versão boa continuam intactas na tag dela.

### No console

- Quem já instalou a versão ruim abre o app, que agora vê `1.4.2` publicada e
  oferece a reinstalação. Instalar a versão anterior é o mesmo fluxo: limpeza
  pelo manifest da 1.4.2 seguida da extração dela. Não há caminho especial de
  downgrade.
- Falha durante o download nunca derruba o sistema: nada é apagado antes de
  **todas** as partes estarem baixadas e com SHA-256 conferido (ver
  `data-model.md` §Staging). Basta abrir o app de novo; os `.part` retomam.
- Falha depois do início da limpeza (energia, travamento): o marcador
  `APPLYING` faz o app oferecer a retomada na próxima abertura, usando as
  partes já verificadas no staging, sem baixar nada de novo.
- Se o console não boota mais (Atmosphère removido e não reextraído): ligar
  pelo hekate, montar o SD por USB (UMS) e usar o app de PC para reinstalar o
  pacote. É o caminho de recuperação documentado na tela de erro.

### Homebrew com defeito

Republicar a release `homebrew-v` anterior e, no próximo pacote, embutir
aquele `.nro`. Enquanto isso, o usuário pode copiar o `.nro` anterior para
`switch/ultranx-nx/` manualmente.
