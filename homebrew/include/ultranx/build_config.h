/*
 * Configuração fixa do UltraNX-NX, embutida no .nro.
 *
 * Não existe arquivo de configuração no cartão: o usuário só instala o app e
 * abre. De onde vem o pacote e o que ele apaga/instala é decidido pelo dono do
 * repositório abaixo — mudar o manifest é publicar uma release nova lá; mudar
 * este endereço exige um build novo do app.
 */
#ifndef ULTRANX_BUILD_CONFIG_H
#define ULTRANX_BUILD_CONFIG_H

#define UNX_APP_VERSION "0.1.0"

/* Sempre a release mais recente do repositório de pacotes. */
#define UNX_MANIFEST_URL \
    "https://github.com/mathst/rox-pack/releases/latest/download/manifest.json"

/* archives[].url do manifest precisa começar com este prefixo; qualquer outra
 * origem invalida o manifest. Defesa em profundidade: mesmo um manifest
 * adulterado não consegue apontar o download para outro servidor. */
#define UNX_ALLOWED_ARCHIVE_PREFIX "https://github.com/mathst/rox-pack/releases/download/"

/*
 * Build de desenvolvimento (`make DEV=1`): aceita sdmc:/ultranx-nx/dev.json
 * com manifest_url/allow_http para testar contra servidor local. O CI de
 * release nunca define UNX_DEV_BUILD, então o .nro publicado ignora o arquivo.
 */
#ifdef UNX_DEV_BUILD
#define UNX_DEV_CONFIG_PATH "sdmc:/ultranx-nx/dev.json"
#endif

#endif /* ULTRANX_BUILD_CONFIG_H */
