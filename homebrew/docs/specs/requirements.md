# Requisitos

Escopo e premissas em `../PLAN.md`; formatos em `data-model.md`.

## Histórias de usuário

- **HU-1** — Como dono do console, quero ver a versão instalada e a publicada
  no próprio Switch, para saber se preciso atualizar sem ligar o PC.
- **HU-2** — Como dono do console, quero escolher entre Pacote Padrão e
  Completo e ver, antes de confirmar, o que será apagado, o que será
  preservado e quanto vou baixar, para não perder saves nem ficar sem espaço.
- **HU-3** — Como dono do console, quero que nada seja apagado se o download
  falhar, para não ficar com o sistema quebrado por causa da Wi-Fi.
- **HU-4** — Como dono do console, quero que, se o console desligar no meio,
  o app retome de onde parou na próxima abertura.
- **HU-5** — Como mantenedor do pacote, quero mudar a lista do que apagar e as
  partes do pacote só publicando um novo manifest, sem lançar app novo.
- **HU-6** — Como dono do console, quero que ao fim o console reinicie sozinho
  no hekate, já com a versão nova.

## Requisitos funcionais

| ID | Requisito | Critério de aceite |
| --- | --- | --- |
| RF-01 | Ler `ultranx-nx/config.json` (ou URL embutida) e baixar o manifest v1/v2 | Sem `config.json`, usa `ULTRANX_MANIFEST_URL`; manifest inválido (regras de `data-model.md` §Validação) mostra a tela de Erro e não habilita a confirmação |
| RF-02 | Ler `packetVersion.txt` local e comparar com `version` do manifest | Tela Início mostra "instalada", "publicada" e o estado (atualizado/desatualizado/nenhuma instalada); comparação numérica por componente (`1.10.0 > 1.9.0`) |
| RF-03 | Escolher a modalidade (`standard`/`full`) entre as presentes no manifest | Só aparecem modalidades válidas; com uma só, ela já vem selecionada; o rótulo vem de `label` ou do fallback embutido |
| RF-04 | Bloquear quando `min_updater` > versão do app | Tela de Erro "Atualize o UltraNX-NX" com a versão exigida; nenhuma ação destrutiva fica disponível |
| RF-05 | Montar o plano de limpeza a partir de `cleanup` (ou defaults) com a whitelist embutida | Itens protegidos nunca entram no plano; caminho do manifest que cai na whitelist é descartado e registrado no log |
| RF-06 | Mostrar o resumo antes de confirmar | A tela Confirmação lista: nº de itens a apagar (e os 10 primeiros), itens preservados relevantes, bytes a baixar, espaço livre × necessário, bateria/carregador |
| RF-07 | Recusar começar sem espaço ou sem bateria | Livre < `Σ size + Σ extracted_size` ⇒ bloqueia mostrando quanto falta; bateria < 30% sem carregador ⇒ bloqueia |
| RF-08 | Confirmação dupla | A primeira tecla A mostra "Isto apaga N itens. Pressione A de novo para confirmar"; qualquer outra tecla cancela |
| RF-09 | Baixar **todas** as partes para o staging e verificar SHA-256 **antes** de apagar qualquer coisa | Uma falha de rede ou de hash em qualquer parte termina sem nenhum arquivo do usuário removido (verificável por listagem antes/depois) |
| RF-10 | Retomar o download parcial | Um `.part` existente é continuado via `Range`; um servidor sem suporte a `Range` recomeça do zero |
| RF-11 | Cancelar só antes da limpeza | B cancela durante o download/verificação; a partir da limpeza o B é ignorado e a tela mostra "Não desligue" |
| RF-12 | Executar a limpeza, extrair as partes na ordem e gravar `packetVersion.txt` | Arquivos do pacote presentes; versão relida = `version` do manifest; staging apagado só depois disso |
| RF-13 | Retomada por `APPLYING` | Abrir o app com `staging/APPLYING` leva à tela Retomada; continuar pula a limpeza se `cleanup_done` e extrai só as partes com `extracted: false` |
| RF-14 | Reiniciar para o payload | Com o `reboot_payload` presente: contagem de 10 s e reboot (A antecipa); ausente: mensagem para reiniciar manualmente pelo hekate |
| RF-15 | Registrar o log em `ultranx-nx/logs/ultranx-nx.log` | Cada etapa, item removido/preservado e erro registrado; mantém os 3 últimos |
| RF-16 | Bloquear HOME e auto-sleep durante a aplicação | Da limpeza ao reboot, o HOME não sai do app e a tela não apaga |

## Requisitos não funcionais

| ID | Requisito | Critério de aceite |
| --- | --- | --- |
| RNF-01 | Segurança de dados | O manifest não remove `Nintendo/`, `emummc/`, `*.keys`, `*.sav`, `switch/JKSV`, `switch/ultranx-nx`, `hbmenu.nro` — testado em `test_plan.c` |
| RNF-02 | Integridade | Toda parte passa por SHA-256 antes da extração; `.part` só é renomeado após conferir |
| RNF-03 | TLS | HTTPS com verificação de certificado ligada; `http` só com `allow_http: true` |
| RNF-04 | Memória | Download e extração em streaming; uso de heap < 64 MB (funciona em modo applet) |
| RNF-05 | Desempenho | O download é limitado pela rede, não pela escrita; a extração de 1 GB com muitos arquivos pequenos termina em tempo anotado no spike F0.3 |
| RNF-06 | Robustez a energia | Um desligamento em qualquer etapa deixa um estado recuperável (download: retomar; aplicação: `APPLYING`) |
| RNF-07 | Portabilidade do core | `src/core/` compila e testa no host sem libnx; cobertura ≥ 80% |
| RNF-08 | Idioma | Todos os textos em português, curtos (≤ 78 colunas por linha) |
| RNF-09 | Consistência PC × console | Mesma whitelist (gerada de `config.py`) e mesmo formato de `packetVersion.txt` |

## Fora de escopo

Igual a `../PLAN.md` §Fora de escopo.
