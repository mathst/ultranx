# UltraNX

Ferramenta desktop que automatiza a atualização do pacote **R O X** em cartões SD
de Nintendo Switch. Substitui a rotina manual de apagar pastas, baixar o ZIP e
extrair na raiz — rotina em que um clique errado apaga saves ou deixa o
Atmosphere inconsistente.

Sem banco de dados: o estado fica no próprio cartão, no arquivo
`packetVersion.txt` da raiz.

## O que ele faz

1. **Detecta o cartão** — varre mídias removíveis FAT32/exFAT via `psutil`; se não
   identificar a raiz do Switch, você aponta a pasta manualmente.
2. **Compara versões** — lê o `packetVersion.txt` local e o publicado no servidor,
   e libera a escolha entre **Pacote Padrão** e **Pacote Completo (Android/Linux)**.
3. **Limpa com whitelist estrita** — remove só as pastas de sistema legadas que
   causam conflito (`atmosphere`, `bootloader`, `config`, `sept`,
   `warmboot_mariko`, `payload.bin`). `Nintendo`, `emummc`, `tico/roms`,
   `themes/ThemezerNX`, `mods2`, pastas de mods e binários standalone **nunca**
   são tocados.

   Por que remover em vez de só sobrescrever: o mesmo nome de arquivo pode
   carregar conteúdo de outra versão, e um órfão que o pacote novo não repõe
   continua sendo lido como se fosse válido.

   `switch/` é limpa **item a item**, não removida por inteiro: os apps que o
   pacote repõe saem, mas `switch/JKSV` (backups de saves), `switch/EdiZon`,
   `switch/NX-Activity-Log` e qualquer `*.keys` ou `*.sav` ficam — perder esses
   arquivos é irreversível e nenhum pacote os traz de volta.
4. **Instala** — download em streaming com barra de progresso real, validação de
   SHA-256, extração sobre a raiz e gravação verificada do novo
   `packetVersion.txt`. Cada etapa estima o tempo restante a partir da vazão
   medida ao vivo, com média móvel exponencial: cartão SD tem vazão irregular, e
   média simples faria a estimativa pular de "2 min" para "20 min" e voltar.
5. **Recupera e finaliza** — em falha de I/O ou desconexão, preserva o log e
   mostra o que fazer; em sucesso, faz o flush e orienta a ejeção segura.

## Instalação

### Binário pronto (recomendado)

Baixe o executável da página de [Releases](../../releases) — arquivo único,
portátil, sem instalador. Windows e Linux.

### A partir do código

```bash
git clone https://github.com/mathst/ultranx.git
cd ultranx
python -m venv .venv
# Windows: .venv\Scripts\activate     |  Linux: source .venv/bin/activate
pip install -e ".[dev]"
python -m ultranx
```

Requer Python 3.11+.

## Configuração do servidor

O binário não vem com servidor embutido. **Abra o app, digite o endereço no campo
"1. Servidor de pacotes" e clique em Salvar.** Fica guardado em
`~/.ultranx/ultranx.json` e não precisa ser digitado de novo.

Para distribuir o executável já apontado — num pendrive, por exemplo — coloque um
`ultranx.json` na mesma pasta do executável (veja
[`docs/ultranx.example.json`](docs/ultranx.example.json)):

```json
{
  "base_url": "https://servidor.exemplo/ultranx",
  "http_timeout": 30
}
```

A resolução tem três camadas, da mais forte para a mais fraca:

1. variável de ambiente `ULTRANX_BASE_URL`;
2. `ultranx.json` ao lado do executável (portátil, vence o perfil do usuário);
3. `~/.ultranx/ultranx.json` (o que o botão Salvar grava).

Sem nenhuma das três, o app abre, avisa que falta configurar e bloqueia a
verificação — nunca tenta baixar de um endereço inválido.

| Variável                  | Efeito                                                        |
| ------------------------- | ------------------------------------------------------------- |
| `ULTRANX_BASE_URL`        | URL base do repositório do pacote                             |
| `ULTRANX_HTTP_TIMEOUT`    | Timeout HTTP em segundos (padrão `30`)                        |
| `ULTRANX_SKIP_HASH_CHECK` | `1` desativa a checagem de SHA-256 — **só para depuração**     |

Endereço sem `http://`/`https://` assume `https://`. Nome de host sem ponto é
aceito de propósito (`http://servidor/rox`), para quem hospeda em rede local.

O servidor precisa expor:

- `packetVersion.txt` — uma linha com a versão publicada (ex.: `1.4.2`);
- `manifest.json` — opcional, mas é o que habilita a validação por checksum. Veja
  [`docs/manifest.example.json`](docs/manifest.example.json).

### Datas de versão

A tela mostra três datas, e vale saber de onde cada uma vem:

| Data | Origem |
| --- | --- |
| lançamento da versão publicada | `released` no `manifest.json`; sem manifest, cai no `Last-Modified` do `packetVersion.txt` remoto |
| lançamento da versão instalada | segunda linha do `packetVersion.txt` do cartão, gravada na atualização |
| gravação no cartão | data de modificação do `packetVersion.txt` |

O `packetVersion.txt` gravado no cartão fica assim:

```text
1.4.2
2026-08-15
```

Quem lê apenas a primeira linha continua funcionando, e cartão atualizado à mão
(sem a segunda linha) mostra `—` no lançamento e ainda assim exibe a data de
gravação.

Sem `manifest.json` o app monta as URLs por convenção
(`rox-<modalidade>-<versão>.zip`) e avisa na tela que o download não pôde ser
verificado.

## Segurança do seu cartão

A whitelist de preservação é o invariante central do projeto e é aplicada em três
camadas independentes: contenção de caminho, consulta à whitelist ao montar o
plano e **revalidação de cada item imediatamente antes de apagar**. Um plano
corrompido ou adulterado não remove nada protegido — há teste cobrindo
exatamente esse caso.

Ainda assim: **faça backup do cartão antes da primeira execução.** Nenhuma
ferramenta substitui uma cópia.

## Desenvolvimento

```bash
pytest --cov=ultranx --cov-report=term-missing   # testes + cobertura (mínimo 80%)
ruff check src tests                             # lint
pyinstaller ultranx.spec --noconfirm             # binário portátil
```

Arquitetura em [`docs/ARCHITECTURE.md`](docs/ARCHITECTURE.md).

## Contribuindo

Pacotes novos, correções e melhorias são bem-vindos — veja
[CONTRIBUTING.md](CONTRIBUTING.md).

## Licença

MIT — veja [LICENSE](LICENSE).

## Aviso

Projeto independente e não oficial, sem vínculo com a Nintendo. Destinado ao uso
de homebrew em hardware próprio. Você é responsável pelo que instala no seu
console; o UltraNX não distribui, baixa nem viabiliza conteúdo protegido por
direitos autorais.
