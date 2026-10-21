/* Read-only bounded NF/NR/NS2 reconciliation. No persistent state or writes.
 * The caller authenticates BF1 and supplies its exact expanded row layout.
 * SHA256 is supplied by the same OpenSSL implementation as Python hashlib.
 */
#include <stddef.h>
#include <stdint.h>
#include <string.h>
#define sqlite3_api cw_nf_sqlite_api
#include "sqlite3ext.h"
SQLITE_EXTENSION_INIT1
#define ZSTD_STATIC_LINKING_ONLY
#include "zstd.h"

#ifdef _WIN32
#define CW_EXPORT __declspec(dllexport)
#else
#define CW_EXPORT __attribute__((visibility("default")))
#endif

#define CW_BLOCKS 64u
#define CW_ENTRY_MAX (73u + 64u * 34u)
#define CW_FLAT_MAX (1u + 65u * 4u + CW_BLOCKS * CW_ENTRY_MAX)
#define CW_NR_MAX (44u + 64u * 18u)
#define CW_MAX_BLOCK ((UINT64_C(9223372036854775807) - 1u) / 64u)

typedef struct evp_md_ctx_st EVP_MD_CTX;
typedef struct evp_md_st EVP_MD;
typedef struct engine_st ENGINE;
typedef struct {
    EVP_MD_CTX *(*new_ctx)(void);
    void (*free_ctx)(EVP_MD_CTX *);
    const EVP_MD *(*sha256)(void);
    int (*init)(EVP_MD_CTX *, const EVP_MD *, ENGINE *);
    int (*update)(EVP_MD_CTX *, const void *, size_t);
    int (*finish)(EVP_MD_CTX *, unsigned char *, unsigned int *);
} cw_crypto;
static cw_crypto crypto;
static int crypto_ready;

CW_EXPORT int cw_nf_crypto(const cw_crypto *ops) {
    if (!ops || !ops->new_ctx || !ops->free_ctx || !ops->sha256 ||
        !ops->init || !ops->update || !ops->finish) return 0;
    if (crypto_ready) {
        return crypto.new_ctx == ops->new_ctx && crypto.free_ctx == ops->free_ctx &&
            crypto.sha256 == ops->sha256 && crypto.init == ops->init &&
            crypto.update == ops->update && crypto.finish == ops->finish;
    }
    crypto = *ops;
    crypto_ready = 1;
    return 1;
}

static int digest(EVP_MD_CTX *ctx, const unsigned char *data, size_t size,
                  unsigned char *output) {
    unsigned int length = 0;
    return crypto.init(ctx, crypto.sha256(), NULL) == 1 &&
        crypto.update(ctx, data, size) == 1 &&
        crypto.finish(ctx, output, &length) == 1 && length == 32u;
}

CW_EXPORT int cw_nf_digest(const unsigned char *data, size_t size,
                           unsigned char *output, size_t capacity) {
    EVP_MD_CTX *ctx;
    int ok;
    if (!crypto_ready || (!data && size) || size > 26u + CW_ENTRY_MAX + CW_NR_MAX || !output || capacity < 32u) return 0;
    ctx = crypto.new_ctx();
    if (!ctx) return 0;
    ok = digest(ctx, data, size, output);
    crypto.free_ctx(ctx);
    return ok;
}

static uint32_t be32(const unsigned char *p) {
    return ((uint32_t)p[0] << 24u) | ((uint32_t)p[1] << 16u) |
        ((uint32_t)p[2] << 8u) | (uint32_t)p[3];
}
static uint64_t be64(const unsigned char *p) {
    return ((uint64_t)be32(p) << 32u) | be32(p + 4u);
}
static void put64(unsigned char *p, uint64_t value) {
    for (unsigned int i = 0; i < 8u; ++i) p[7u - i] = (unsigned char)(value >> (i * 8u));
}

static int lease(const unsigned char *raw, size_t size, const unsigned char *tag,
                 int compressed, EVP_MD_CTX *ctx, unsigned char *message) {
    unsigned char actual[32];
    int nr2;
    if (!raw || size < 44u || size > CW_NR_MAX) return 0;
    nr2 = memcmp(raw, "NR2\0", 4u) == 0;
    if ((!nr2 && memcmp(raw, "NR1\0", 4u)) || (nr2 && !compressed)) return 0;
    if (memcmp(raw + 36u, tag, 8u)) return 0;
    memcpy(message, raw, 4u);
    memcpy(message + 4u, raw + 36u, size - 36u);
    if (!digest(ctx, message, size - 32u, actual) || memcmp(actual, raw + 4u, 32u)) return 0;
    if (nr2) {
        ZSTD_frameHeader header;
        unsigned int declared;
        if (size <= 46u) return 0;
        declared = ((unsigned int)raw[44] << 8u) | raw[45];
        if (declared < 128u || declared > 1152u ||
            ZSTD_getFrameHeader(&header, raw + 46u, size - 46u) != 0u ||
            header.frameType != ZSTD_frame || header.frameContentSize != declared ||
            header.windowSize > 1152u || header.dictID || !header.checksumFlag) return 0;
    }
    return 1;
}

typedef struct {
    uint64_t id;
    unsigned int slot;
    const unsigned char *entry;
    size_t size;
} cw_entry;

static int entries(const unsigned char *flat, size_t size, cw_entry *out,
                   unsigned int *total, unsigned int *slots) {
    unsigned int n, count = 0;
    size_t offset;
    uint32_t previous = 0;
    if (!flat || size < 9u || size > CW_FLAT_MAX) return 0;
    n = flat[0];
    if (!n || n > CW_BLOCKS) return 0;
    offset = 1u + ((size_t)n + 1u) * 4u;
    if (offset > size || be32(flat + 1u)) return 0;
    for (unsigned int i = 0; i < n; ++i) {
        uint32_t next = be32(flat + 1u + (i + 1u) * 4u);
        const unsigned char *entry;
        size_t span;
        uint64_t id;
        unsigned int sum = 0, flag;
        if (next < previous || next > size - offset) return 0;
        span = next - previous;
        if (span < 73u || span > CW_ENTRY_MAX) return 0;
        entry = flat + offset + previous;
        id = be64(entry);
        flag = entry[8];
        if (flag > 1u) return 0;
        for (unsigned int j = 0; j < 64u; ++j) {
            unsigned int length = entry[9u + j];
            if (length && (flag ? length < 6u || length > 34u : length != 26u && length != 34u)) return 0;
            sum += length;
        }
        if (sum != span - 73u) return 0;
        if (id == UINT64_MAX) {
            if (flag || sum) return 0;
        } else {
            unsigned int at = count;
            if (!sum || id > CW_MAX_BLOCK || (id == CW_MAX_BLOCK && entry[72])) return 0;
            while (at && out[at - 1u].id > id) { out[at] = out[at - 1u]; --at; }
            if ((at && out[at - 1u].id == id) || (at < count && out[at].id == id)) return 0;
            out[at].id = id; out[at].slot = i; out[at].entry = entry; out[at].size = span;
            ++count;
        }
        previous = next;
    }
    if (!count || previous != size - offset) return 0;
    *total = count; *slots = n;
    return 1;
}

static void proof(sqlite3_context *context, int argc, sqlite3_value **argv) {
    cw_entry values[CW_BLOCKS];
    unsigned int count = 0, slots = 0;
    uint64_t wanted, observed = 0;
    unsigned char pointer[13] = "NF1\0", message[26u + CW_ENTRY_MAX + CW_NR_MAX], actual[32];
    sqlite3_stmt *statement = NULL;
    EVP_MD_CTX *ctx = NULL;
    const unsigned char *flat, *mask;
    int rc = SQLITE_ERROR, ok = 0, compressed;
    const char *query =
        "SELECT b.id,CASE WHEN typeof(b.payload)='blob' AND length(b.payload)<=4096 THEN b.payload END,"
        "CASE WHEN typeof(r.counts)='blob' AND length(r.counts)<=1196 THEN r.counts END,r.detail_slots,"
        "CASE WHEN typeof(r.search_seal)='blob' AND length(r.search_seal)<=32 THEN r.search_seal END "
        "FROM main.market_numeric_blocks b "
        "LEFT JOIN main.market_numeric_block_refs r ON r.id=b.id WHERE b.id IN("
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,"
        "?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?,?)";
    if (!crypto_ready || argc != 4 || sqlite3_value_type(argv[0]) != SQLITE_INTEGER ||
        sqlite3_value_int64(argv[0]) <= 0 || sqlite3_value_type(argv[1]) != SQLITE_BLOB ||
        sqlite3_value_bytes(argv[1]) < 9 || sqlite3_value_bytes(argv[1]) > (int)CW_FLAT_MAX ||
        sqlite3_value_type(argv[2]) != SQLITE_BLOB || sqlite3_value_bytes(argv[2]) != 8 ||
        sqlite3_value_type(argv[3]) != SQLITE_INTEGER ||
        (sqlite3_value_int64(argv[3]) != 0 && sqlite3_value_int64(argv[3]) != 1)) goto done;
    flat = sqlite3_value_blob(argv[1]); mask = sqlite3_value_blob(argv[2]);
    if (!flat || !mask) { rc = SQLITE_NOMEM; goto done; }
    if (!entries(flat, (size_t)sqlite3_value_bytes(argv[1]), values, &count, &slots)) goto done;
    wanted = be64(mask);
    if (slots < 64u && (wanted >> slots)) goto done;
    put64(pointer + 4u, (uint64_t)sqlite3_value_int64(argv[0]));
    compressed = sqlite3_value_int(argv[3]);
    rc = sqlite3_prepare_v2(sqlite3_context_db_handle(context), query, -1, &statement, NULL);
    if (rc != SQLITE_OK) goto done;
    for (unsigned int i = 0; i < CW_BLOCKS; ++i) {
        rc = i < count ? sqlite3_bind_int64(statement, (int)i + 1, (sqlite3_int64)values[i].id) :
            sqlite3_bind_null(statement, (int)i + 1);
        if (rc != SQLITE_OK) goto done;
    }
    ctx = crypto.new_ctx();
    if (!ctx) { rc = SQLITE_NOMEM; goto done; }
    while ((rc = sqlite3_step(statement)) == SQLITE_ROW) {
        uint64_t id;
        unsigned int left = 0, right = count;
        const cw_entry *item;
        const unsigned char *payload, *nr, *seal;
        size_t nr_size, length;
        if (sqlite3_column_type(statement, 0) != SQLITE_INTEGER) goto done;
        id = (uint64_t)sqlite3_column_int64(statement, 0);
        while (left < right) { unsigned int middle = left + (right - left) / 2u;
            if (values[middle].id < id) left = middle + 1u; else right = middle; }
        if (left == count || values[left].id != id || sqlite3_column_type(statement, 1) != SQLITE_BLOB) goto done;
        item = values + left;
        payload = sqlite3_column_blob(statement, 1);
        pointer[12] = (unsigned char)item->slot;
        if (sqlite3_column_bytes(statement, 1) != 13 || !payload || memcmp(payload, pointer, 13u)) continue;
        if (!(wanted & (UINT64_C(1) << item->slot)) || (observed & (UINT64_C(1) << item->slot)) ||
            sqlite3_column_type(statement, 2) != SQLITE_BLOB ||
            sqlite3_column_type(statement, 3) != SQLITE_INTEGER || sqlite3_column_int64(statement, 3) != 0 ||
            sqlite3_column_type(statement, 4) != SQLITE_BLOB || sqlite3_column_bytes(statement, 4) != 32) goto done;
        nr_size = (size_t)sqlite3_column_bytes(statement, 2);
        nr = sqlite3_column_blob(statement, 2); seal = sqlite3_column_blob(statement, 4);
        if (!seal || !lease(nr, nr_size, item->entry, compressed, ctx, message)) goto done;
        memcpy(message, "NS2\0", 4u); memcpy(message + 4u, item->entry, 8u);
        memcpy(message + 12u, pointer, 13u); memcpy(message + 25u, item->entry, item->size);
        memcpy(message + 25u + item->size, nr, nr_size);
        length = 25u + item->size + nr_size;
        if (!digest(ctx, message, length, actual) || memcmp(actual, seal, 32u)) goto done;
        observed |= UINT64_C(1) << item->slot;
    }
    if (rc == SQLITE_DONE && observed == wanted) ok = 1;
done:
    if (statement) sqlite3_finalize(statement);
    if (ctx) crypto.free_ctx(ctx);
    if (ok) {
        unsigned int owned = 0;
        while (observed) { observed &= observed - 1u; ++owned; }
        sqlite3_result_int(context, (int)owned);
    } else if (rc != SQLITE_ERROR && rc != SQLITE_OK && rc != SQLITE_ROW && rc != SQLITE_DONE) {
        sqlite3_result_error_code(context, rc);
    } else sqlite3_result_error(context, "cw_nf: conflicting actual numeric frame owner/lease/value seal", -1);
}

CW_EXPORT int sqlite3_extension_init(sqlite3 *db, char **error, const sqlite3_api_routines *api) {
    (void)error;
    if (!api || !crypto_ready || (sqlite3_api && sqlite3_api != api)) return SQLITE_ERROR;
    if (!sqlite3_api) { SQLITE_EXTENSION_INIT2(api); }
    return sqlite3_create_function_v2(db, "cw_nf_proof", 4, SQLITE_UTF8 | SQLITE_DIRECTONLY,
        NULL, proof, NULL, NULL, NULL);
}
