/*
 * UltraNX-NX — spike F0.
 *
 * Valida no console, antes de qualquer módulo de produção, as quatro premissas
 * de plataforma do PLAN.md: toolchain (F0.1), download HTTPS do GitHub com
 * verificação de certificado e SHA-256 (F0.2), extração de zip no SD (F0.3) e
 * reboot para payload via Atmosphère (F0.4).
 *
 * Tudo que aparece na tela também vai para sdmc:/ultranx-nx/spike/spike.log,
 * que é o que volta para preencher "Resultados do spike F0" em architecture.md.
 *
 * Código descartável: não é base para src/.
 */
#include <errno.h>
#include <stdarg.h>
#include <stdio.h>
#include <stdlib.h>
#include <string.h>
#include <sys/stat.h>

#include <curl/curl.h>
#include <switch.h>

#include "miniz.h"

#define SPIKE_VERSION "0.0.1"
#define SPIKE_DIR "sdmc:/ultranx-nx/spike"
#define URL_FILE SPIKE_DIR "/url.txt"
#define DL_FILE SPIKE_DIR "/download.zip"
#define OUT_DIR SPIKE_DIR "/out"
#define LOG_FILE SPIKE_DIR "/spike.log"

#define WRITE_BUFFER_SIZE (1024 * 1024)
#define PROGRESS_INTERVAL_NS 1000000000ULL
#define EXTRACT_REPORT_EVERY 100

/* Atmosphère aceita payload até o fim da IRAM livre; o spike testa os dois
 * tamanhos conhecidos e registra qual foi aceito. */
#define IRAM_PAYLOAD_MAX_SIZE 0x2F000
#define IRAM_PAYLOAD_LEGACY_SIZE 0x24000

static FILE *g_log;
static PadState g_pad;

/* ------------------------------------------------------------------------ */
/* Saída                                                                     */
/* ------------------------------------------------------------------------ */

static void out(const char *fmt, ...) {
    va_list args;
    va_start(args, fmt);
    vprintf(fmt, args);
    va_end(args);
    if (g_log) {
        va_start(args, fmt);
        vfprintf(g_log, fmt, args);
        va_end(args);
        fflush(g_log);
    }
    consoleUpdate(NULL);
}

static double seconds_since(u64 start_tick) {
    return (double)armTicksToNs(armGetSystemTick() - start_tick) / 1e9;
}

static void hex_digest(const u8 *digest, char *hex) {
    for (int i = 0; i < SHA256_HASH_SIZE; i++)
        sprintf(hex + i * 2, "%02x", digest[i]);
    hex[SHA256_HASH_SIZE * 2] = '\0';
}

static bool button_pressed(u64 mask) {
    padUpdate(&g_pad);
    return (padGetButtonsDown(&g_pad) & mask) != 0;
}

static int mkdir_p(const char *path) {
    char tmp[FS_MAX_PATH];
    snprintf(tmp, sizeof(tmp), "%s", path);
    /* Pula "sdmc:/" para não tentar criar o dispositivo. */
    char *start = strchr(tmp, '/');
    for (char *p = start ? start + 1 : tmp; *p; p++) {
        if (*p != '/')
            continue;
        *p = '\0';
        if (mkdir(tmp, 0777) != 0 && errno != EEXIST)
            return -1;
        *p = '/';
    }
    if (mkdir(tmp, 0777) != 0 && errno != EEXIST)
        return -1;
    return 0;
}

/* ------------------------------------------------------------------------ */
/* F0.1 — ambiente                                                           */
/* ------------------------------------------------------------------------ */

static void report_environment(void) {
    u32 hos = hosversionGet();
    out("UltraNX spike %s\n", SPIKE_VERSION);
    out("HOS %u.%u.%u | Atmosphere: %s\n", HOSVER_MAJOR(hos), HOSVER_MINOR(hos),
        HOSVER_MICRO(hos), hosversionIsAtmosphere() ? "sim" : "nao");
    out("applet: %s\n",
        appletGetAppletType() == AppletType_Application ? "application (title override)"
                                                        : "applet mode");
    out("%s\n", curl_version());

    const curl_version_info_data *info = curl_version_info(CURLVERSION_NOW);
    out("SSL backend: %s\n", info->ssl_version ? info->ssl_version : "(nenhum)");

    s64 free_bytes = 0;
    if (R_SUCCEEDED(fsFsGetFreeSpace(fsdevGetDeviceFileSystem("sdmc"), "/", &free_bytes)))
        out("SD livre: %.1f GiB\n", (double)free_bytes / (1024.0 * 1024 * 1024));
}

/* ------------------------------------------------------------------------ */
/* F0.2 — download HTTPS                                                     */
/* ------------------------------------------------------------------------ */

typedef struct {
    FILE *file;
    Sha256Context sha;
    u64 bytes;
    u64 start_tick;
    u64 last_report_tick;
    bool cancelled;
} Download;

static size_t on_write(char *data, size_t size, size_t nmemb, void *userdata) {
    Download *dl = userdata;
    size_t len = size * nmemb;
    if (fwrite(data, 1, len, dl->file) != len)
        return 0; /* curl converte em CURLE_WRITE_ERROR */
    sha256ContextUpdate(&dl->sha, data, len);
    dl->bytes += len;
    return len;
}

static int on_progress(void *userdata, curl_off_t total, curl_off_t now,
                       curl_off_t ultotal, curl_off_t ulnow) {
    (void)ultotal;
    (void)ulnow;
    Download *dl = userdata;
    if (button_pressed(HidNpadButton_B)) {
        dl->cancelled = true;
        return 1;
    }
    u64 tick = armGetSystemTick();
    if (armTicksToNs(tick - dl->last_report_tick) < PROGRESS_INTERVAL_NS)
        return 0;
    dl->last_report_tick = tick;
    double secs = seconds_since(dl->start_tick);
    double mib = (double)now / (1024.0 * 1024);
    printf("\r  %.1f / %.1f MiB  %.2f MiB/s   ", mib, (double)total / (1024.0 * 1024),
           secs > 0 ? mib / secs : 0.0);
    consoleUpdate(NULL);
    return 0;
}

static bool read_url(char *url, size_t len) {
    FILE *f = fopen(URL_FILE, "r");
    if (!f) {
        out("Falta %s (1a linha = URL do zip).\n", URL_FILE);
        return false;
    }
    bool ok = fgets(url, (int)len, f) != NULL;
    fclose(f);
    url[strcspn(url, "\r\n")] = '\0';
    return ok && url[0] != '\0';
}

static void test_download(void) {
    char url[1025];
    if (!read_url(url, sizeof(url)))
        return;
    out("\n[F0.2] GET %s\n", url);

    Download dl = {0};
    dl.file = fopen(DL_FILE, "wb");
    if (!dl.file) {
        out("  falha ao criar %s: %s\n", DL_FILE, strerror(errno));
        return;
    }
    static char write_buffer[WRITE_BUFFER_SIZE];
    setvbuf(dl.file, write_buffer, _IOFBF, sizeof(write_buffer));
    sha256ContextCreate(&dl.sha);
    dl.start_tick = dl.last_report_tick = armGetSystemTick();

    char curl_error[CURL_ERROR_SIZE] = "";
    CURL *curl = curl_easy_init();
    curl_easy_setopt(curl, CURLOPT_URL, url);
    curl_easy_setopt(curl, CURLOPT_FOLLOWLOCATION, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYPEER, 1L);
    curl_easy_setopt(curl, CURLOPT_SSL_VERIFYHOST, 2L);
    curl_easy_setopt(curl, CURLOPT_FAILONERROR, 1L);
    curl_easy_setopt(curl, CURLOPT_USERAGENT, "UltraNX-NX-spike/" SPIKE_VERSION);
    curl_easy_setopt(curl, CURLOPT_CONNECTTIMEOUT, 15L);
    curl_easy_setopt(curl, CURLOPT_LOW_SPEED_LIMIT, 1L);
    curl_easy_setopt(curl, CURLOPT_LOW_SPEED_TIME, 30L);
    curl_easy_setopt(curl, CURLOPT_BUFFERSIZE, 512L * 1024);
    curl_easy_setopt(curl, CURLOPT_ERRORBUFFER, curl_error);
    curl_easy_setopt(curl, CURLOPT_WRITEFUNCTION, on_write);
    curl_easy_setopt(curl, CURLOPT_WRITEDATA, &dl);
    curl_easy_setopt(curl, CURLOPT_NOPROGRESS, 0L);
    curl_easy_setopt(curl, CURLOPT_XFERINFOFUNCTION, on_progress);
    curl_easy_setopt(curl, CURLOPT_XFERINFODATA, &dl);

    CURLcode rc = curl_easy_perform(curl);
    long http = 0;
    char *effective = NULL;
    curl_easy_getinfo(curl, CURLINFO_RESPONSE_CODE, &http);
    curl_easy_getinfo(curl, CURLINFO_EFFECTIVE_URL, &effective);
    double secs = seconds_since(dl.start_tick);
    fclose(dl.file);

    out("\n  curl=%d (%s) http=%ld\n", rc, rc ? curl_error : "ok", http);
    out("  final: %.120s\n", effective ? effective : "?");
    if (dl.cancelled)
        out("  cancelado pelo usuario (B)\n");

    u8 digest[SHA256_HASH_SIZE];
    char hex[SHA256_HASH_SIZE * 2 + 1];
    sha256ContextGetHash(&dl.sha, digest);
    hex_digest(digest, hex);
    double mib = (double)dl.bytes / (1024.0 * 1024);
    out("  %llu bytes em %.1f s = %.2f MiB/s\n", (unsigned long long)dl.bytes, secs,
        secs > 0 ? mib / secs : 0.0);
    out("  sha256 %s\n", hex);
    curl_easy_cleanup(curl);
}

/* ------------------------------------------------------------------------ */
/* F0.3 — extração                                                           */
/* ------------------------------------------------------------------------ */

static bool entry_name_is_safe(const char *name) {
    if (name[0] == '/' || name[0] == '\\' || strchr(name, ':'))
        return false;
    for (const char *p = name; (p = strstr(p, "..")) != NULL; p += 2) {
        bool starts = p == name || p[-1] == '/' || p[-1] == '\\';
        bool ends = p[2] == '\0' || p[2] == '/' || p[2] == '\\';
        if (starts && ends)
            return false;
    }
    return true;
}

static bool extract_entry(mz_zip_archive *zip, mz_uint index,
                          const mz_zip_archive_file_stat *st, u64 *bytes) {
    char dest[FS_MAX_PATH];
    snprintf(dest, sizeof(dest), "%s/%s", OUT_DIR, st->m_filename);

    if (st->m_is_directory)
        return mkdir_p(dest) == 0;

    char *slash = strrchr(dest, '/');
    *slash = '\0';
    int made = mkdir_p(dest);
    *slash = '/';
    if (made != 0) {
        out("  mkdir falhou: %s\n", dest);
        return false;
    }
    if (!mz_zip_reader_extract_to_file(zip, index, dest, 0)) {
        out("  falha em %s: %s\n", st->m_filename,
            mz_zip_get_error_string(mz_zip_get_last_error(zip)));
        return false;
    }
    *bytes += st->m_uncomp_size;
    return true;
}

static void test_extract(void) {
    out("\n[F0.3] extraindo %s -> %s\n", DL_FILE, OUT_DIR);
    mz_zip_archive zip;
    memset(&zip, 0, sizeof(zip));
    if (!mz_zip_reader_init_file(&zip, DL_FILE, 0)) {
        out("  zip invalido: %s\n", mz_zip_get_error_string(mz_zip_get_last_error(&zip)));
        return;
    }
    mkdir_p(OUT_DIR);

    mz_uint total = mz_zip_reader_get_num_files(&zip);
    u64 start = armGetSystemTick();
    u64 bytes = 0;
    mz_uint done = 0, skipped = 0, failed = 0;

    for (mz_uint i = 0; i < total; i++) {
        mz_zip_archive_file_stat st;
        if (!mz_zip_reader_file_stat(&zip, i, &st)) {
            failed++;
            continue;
        }
        if (!entry_name_is_safe(st.m_filename)) {
            out("  zip-slip descartado: %s\n", st.m_filename);
            skipped++;
            continue;
        }
        if (extract_entry(&zip, i, &st, &bytes))
            done++;
        else
            failed++;
        if ((i + 1) % EXTRACT_REPORT_EVERY == 0) {
            printf("\r  %u / %u entradas", i + 1, total);
            consoleUpdate(NULL);
        }
    }
    mz_zip_reader_end(&zip);

    double secs = seconds_since(start);
    double mib = (double)bytes / (1024.0 * 1024);
    out("\n  %u ok, %u descartadas, %u falhas de %u entradas\n", done, skipped, failed, total);
    out("  %.1f MiB em %.1f s = %.2f MiB/s, %.1f arquivos/s\n", mib, secs,
        secs > 0 ? mib / secs : 0.0, secs > 0 ? done / secs : 0.0);
}

/* ------------------------------------------------------------------------ */
/* F0.4 — reboot para payload                                                */
/* ------------------------------------------------------------------------ */

/* bpc:ams é extensão do Atmosphère, não está na libnx; mesmo protocolo do
 * troposphere/reboot_to_payload. */
static Service g_ams_bpc;

static Result ams_bpc_initialize(void) {
    Handle handle;
    Result rc = smGetServiceOriginal(&handle, smEncodeName("bpc:ams"));
    if (R_SUCCEEDED(rc))
        serviceCreate(&g_ams_bpc, handle);
    return rc;
}

static Result ams_bpc_set_reboot_payload(const void *payload, size_t size) {
    return serviceDispatch(&g_ams_bpc, 65001,
                           .buffer_attrs = {SfBufferAttr_In | SfBufferAttr_HipcMapAlias},
                           .buffers = {{payload, size}}, );
}

static size_t load_payload(u8 *buffer, size_t capacity, const char **source) {
    static const char *candidates[] = {
        "sdmc:/atmosphere/reboot_payload.bin",
        "sdmc:/bootloader/update.bin",
    };
    for (size_t i = 0; i < sizeof(candidates) / sizeof(candidates[0]); i++) {
        FILE *f = fopen(candidates[i], "rb");
        if (!f)
            continue;
        size_t len = fread(buffer, 1, capacity, f);
        bool too_big = fgetc(f) != EOF;
        fclose(f);
        if (too_big) {
            out("  %s maior que 0x%zx; ignorado\n", candidates[i], capacity);
            continue;
        }
        *source = candidates[i];
        return len;
    }
    return 0;
}

static bool confirm(const char *question) {
    out("%s  A=sim  B=nao\n", question);
    while (appletMainLoop()) {
        padUpdate(&g_pad);
        u64 down = padGetButtonsDown(&g_pad);
        if (down & HidNpadButton_A)
            return true;
        if (down & HidNpadButton_B)
            return false;
        consoleUpdate(NULL);
    }
    return false;
}

static void test_reboot(void) {
    static u8 payload[IRAM_PAYLOAD_MAX_SIZE] __attribute__((aligned(0x1000)));
    out("\n[F0.4] reboot para payload\n");
    if (!hosversionIsAtmosphere()) {
        out("  sem Atmosphere: bpc:ams indisponivel\n");
        return;
    }
    memset(payload, 0, sizeof(payload));
    const char *source = NULL;
    size_t len = load_payload(payload, sizeof(payload), &source);
    if (len == 0) {
        out("  nenhum payload encontrado\n");
        return;
    }
    out("  payload %s (%zu bytes)\n", source, len);
    if (!confirm("  Reiniciar agora?"))
        return;

    Result rc = ams_bpc_initialize();
    if (R_FAILED(rc)) {
        out("  bpc:ams falhou: 0x%x\n", rc);
        return;
    }
    rc = ams_bpc_set_reboot_payload(payload, IRAM_PAYLOAD_MAX_SIZE);
    out("  SetRebootPayload(0x%x) = 0x%x\n", IRAM_PAYLOAD_MAX_SIZE, rc);
    if (R_FAILED(rc)) {
        rc = ams_bpc_set_reboot_payload(payload, IRAM_PAYLOAD_LEGACY_SIZE);
        out("  SetRebootPayload(0x%x) = 0x%x\n", IRAM_PAYLOAD_LEGACY_SIZE, rc);
    }
    serviceClose(&g_ams_bpc);
    if (R_FAILED(rc))
        return;

    out("  reiniciando...\n");
    if (g_log)
        fclose(g_log);
    g_log = NULL;
    spsmInitialize();
    spsmShutdown(true);
}

/* ------------------------------------------------------------------------ */

static void print_menu(void) {
    out("\nA = download (F0.2)   Y = extrair (F0.3)\n"
        "X = reboot payload (F0.4)   + = sair\n");
}

int main(int argc, char **argv) {
    (void)argc;
    (void)argv;
    consoleInit(NULL);
    padConfigureInput(1, HidNpadStyleSet_NpadStandard);
    padInitializeDefault(&g_pad);
    appletSetAutoSleepDisabled(true);

    mkdir_p(SPIKE_DIR);
    g_log = fopen(LOG_FILE, "a");
    out("\n==== nova execucao ====\n");

    socketInitializeDefault();
    curl_global_init(CURL_GLOBAL_DEFAULT);

    report_environment();
    print_menu();

    while (appletMainLoop()) {
        padUpdate(&g_pad);
        u64 down = padGetButtonsDown(&g_pad);
        if (down & HidNpadButton_Plus)
            break;
        if (down & HidNpadButton_A) {
            test_download();
            print_menu();
        } else if (down & HidNpadButton_Y) {
            test_extract();
            print_menu();
        } else if (down & HidNpadButton_X) {
            test_reboot();
            print_menu();
        }
        consoleUpdate(NULL);
    }

    curl_global_cleanup();
    socketExit();
    appletSetAutoSleepDisabled(false);
    if (g_log)
        fclose(g_log);
    consoleExit(NULL);
    return 0;
}
