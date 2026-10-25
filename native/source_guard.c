/* Stateless validation of the existing JS1 scalar source body.
 * The caller authenticates the enclosing SB2/JS1 headers and decompression.
 * CPython float conversion requires the GIL throughout both entry points.
 * No deletion, database, mutable cache or retained pointer authority lives here.
 */
#include "yyjson.h"
#include <float.h>
#include <math.h>
#include <stdint.h>
#include <stdlib.h>
#include <string.h>

extern double PyOS_string_to_double(const char *, char **, void *);
extern void *PyErr_Occurred(void);
extern void PyErr_Clear(void);
extern int PyErr_ExceptionMatches(void *);
extern void *PyExc_MemoryError;

#define SG_API __attribute__((visibility("default")))
#define SG_BYTES (512u * 1024u)
#define SG_MEMORY (16u * 1024u * 1024u)
#define SG_ROWS 256u
#define SG_SLOTS 32u
#define SG_VALID 0
#define SG_PORTABLE 1
#define SG_NOMEM 2
#define SG_ARGUMENT 3

typedef union { size_t size; long double alignment; void *pointer; } allocation;
typedef struct { size_t used; int failed; } budget;
typedef struct {
    uint64_t revision, needed, purge, received, server;
    double values[6];
    const char *source;
    size_t source_length;
} source_row;
typedef struct {
    yyjson_doc *documents[SG_SLOTS];
    source_row rows[SG_ROWS];
    size_t first[SG_SLOTS + 1], count;
    uint64_t minimum_purge;
    budget memory;
} snapshot;

static void *limited_malloc(void *context, size_t size) {
    budget *b = context;
    if (size > SIZE_MAX - sizeof(allocation) ||
        size + sizeof(allocation) > SG_MEMORY - b->used) {
        b->failed = 1; return NULL;
    }
    allocation *p = malloc(sizeof(*p) + size);
    if (!p) { b->failed = 1; return NULL; }
    p->size = size; b->used += sizeof(*p) + size;
    return p + 1;
}

static void limited_free(void *context, void *pointer) {
    if (!pointer) return;
    budget *b = context; allocation *p = (allocation *)pointer - 1;
    b->used -= sizeof(*p) + p->size; free(p);
}

static void *limited_realloc(void *context, void *pointer, size_t old_size, size_t size) {
    (void)old_size;
    if (!pointer) return limited_malloc(context, size);
    if (!size) { limited_free(context, pointer); return NULL; }
    budget *b = context; allocation *old = (allocation *)pointer - 1;
    size_t previous = sizeof(*old) + old->size;
    if (size > SIZE_MAX - sizeof(allocation) ||
        size + sizeof(allocation) > SG_MEMORY - (b->used - previous)) {
        b->failed = 1; return NULL;
    }
    allocation *p = realloc(old, sizeof(*p) + size);
    if (!p) { b->failed = 1; return NULL; }
    p->size = size; b->used = b->used - previous + sizeof(*p) + size;
    return p + 1;
}

static uint32_t big_endian(const unsigned char *p) {
    return (uint32_t)p[0] << 24 | (uint32_t)p[1] << 16 |
           (uint32_t)p[2] << 8 | (uint32_t)p[3];
}

static int unsigned63(yyjson_val *value, uint64_t *out) {
    if (!yyjson_is_raw(value)) return 0;
    const char *text = yyjson_get_raw(value); size_t length = yyjson_get_len(value);
    if (length == 2 && text[0] == '-' && text[1] == '0') { *out = 0; return 1; }
    if (!length || length > 19) return 0;
    uint64_t n = 0;
    for (size_t i = 0; i < length; i++) {
        if (text[i] < '0' || text[i] > '9') return 0;
        unsigned digit = (unsigned)(text[i] - '0');
        if (n > (UINT64_C(9223372036854775807) - digit) / 10) return 0;
        n = n * 10 + digit;
    }
    *out = n; return 1;
}

static int floating(yyjson_val *value, double *out) {
    if (!yyjson_is_raw(value)) return SG_PORTABLE;
    const char *text = yyjson_get_raw(value); size_t length = yyjson_get_len(value);
    if (!length || length > 128) return SG_PORTABLE;
    int is_float = 0;
    for (size_t i = 0; i < length; i++)
        if (text[i] == '.' || text[i] == 'e' || text[i] == 'E') is_float = 1;
    if (!is_float) return SG_PORTABLE; /* Preserve all integer semantics in Python. */
    char token[129]; memcpy(token, text, length); token[length] = 0;
    char *end = NULL; double number = PyOS_string_to_double(token, &end, NULL);
    if (PyErr_Occurred()) {
        if (PyErr_ExceptionMatches(PyExc_MemoryError)) return SG_NOMEM;
        PyErr_Clear(); return SG_PORTABLE;
    }
    if (end != token + length || !isfinite(number)) return SG_PORTABLE;
    *out = number; return SG_VALID;
}

static void release_snapshot(snapshot *value) {
    if (!value) return;
    for (size_t i = 0; i < SG_SLOTS; i++) yyjson_doc_free(value->documents[i]);
    free(value);
}

static int parse_snapshot(const char *data, size_t length,
                          const unsigned char *offsets, size_t offset_bytes,
                          uint64_t tile, uint64_t step, snapshot *out) {
    if (!data || !offsets || !length || length > SG_BYTES || offset_bytes != 132)
        return SG_ARGUMENT;
    if ((step != 60000 && step != 180000 && step != 300000 && step != 900000) ||
        tile % (SG_SLOTS * step) ||
        tile > UINT64_C(9007199254740992) - (SG_SLOTS - 1) * step)
        return SG_PORTABLE;
    if (big_endian(offsets) != 0 || big_endian(offsets + 128) != length) return SG_ARGUMENT;
    out->minimum_purge = UINT64_MAX;
    yyjson_alc allocator = {limited_malloc, limited_realloc, limited_free, &out->memory};
    for (size_t slot = 0; slot < SG_SLOTS; slot++) {
        size_t a = big_endian(offsets + slot * 4), b = big_endian(offsets + (slot + 1) * 4);
        if (a >= b || b > length) return SG_ARGUMENT;
        yyjson_read_err error;
        out->documents[slot] = yyjson_read_opts((char *)data + a, b - a,
                                               YYJSON_READ_NUMBER_AS_RAW, &allocator, &error);
        if (!out->documents[slot]) return out->memory.failed ? SG_NOMEM : SG_PORTABLE;
        yyjson_val *rows = yyjson_doc_get_root(out->documents[slot]);
        if (!yyjson_is_arr(rows) || yyjson_arr_size(rows) > SG_ROWS - out->count) return SG_PORTABLE;
        out->first[slot] = out->count;
        size_t index, count; yyjson_val *row; uint64_t previous = 0; int has_previous = 0;
        yyjson_arr_foreach(rows, index, count, row) {
            if (!yyjson_is_arr(row) || yyjson_arr_size(row) != 4) return SG_PORTABLE;
            source_row *saved = &out->rows[out->count];
            if (!unsigned63(yyjson_arr_get(row, 0), &saved->revision) ||
                !unsigned63(yyjson_arr_get(row, 1), &saved->needed) ||
                !unsigned63(yyjson_arr_get(row, 2), &saved->purge) ||
                (has_previous && saved->revision <= previous)) return SG_PORTABLE;
            previous = saved->revision; has_previous = 1;
            yyjson_val *tail = yyjson_arr_get(row, 3);
            if (!yyjson_is_arr(tail) || yyjson_arr_size(tail) != 9) return SG_PORTABLE;
            for (size_t n = 0; n < 6; n++) {
                int status = floating(yyjson_arr_get(tail, n), &saved->values[n]);
                if (status != SG_VALID) return status;
            }
            yyjson_val *source = yyjson_arr_get(tail, 6);
            if (!yyjson_is_str(source) ||
                !unsigned63(yyjson_arr_get(tail, 7), &saved->received) ||
                !unsigned63(yyjson_arr_get(tail, 8), &saved->server)) return SG_PORTABLE;
            saved->source = yyjson_get_str(source); saved->source_length = yyjson_get_len(source);
            double *v = saved->values;
            if (v[0] <= 0 || v[1] <= 0 || v[2] <= 0 || v[3] <= 0 ||
                v[1] < v[0] || v[1] < v[3] || v[2] > v[0] || v[2] > v[3] ||
                v[2] > v[1] || v[4] < 0 || v[5] < 0 ||
                tile + (slot + 1) * step > saved->server) return SG_PORTABLE;
            if (saved->purge < out->minimum_purge) out->minimum_purge = saved->purge;
            out->count++;
        }
    }
    out->first[SG_SLOTS] = out->count;
    return out->count ? SG_VALID : SG_PORTABLE;
}

SG_API unsigned cw_source_guard_abi(void) {
    return sizeof(double) == 8 && FLT_RADIX == 2 && DBL_MANT_DIG == 53 ? 1 : 0;
}

SG_API int cw_source_guard_stats(const char *data, size_t length,
                                 const unsigned char *offsets, size_t offset_bytes,
                                 uint64_t tile, uint64_t step, uint64_t *result) {
    if (!result) return SG_ARGUMENT;
    snapshot *value = calloc(1, sizeof(*value));
    if (!value) return SG_NOMEM;
    int status = parse_snapshot(data, length, offsets, offset_bytes, tile, step, value);
    if (status == SG_VALID) { result[0] = value->count; result[1] = value->minimum_purge; }
    release_snapshot(value); return status;
}

SG_API int cw_source_guard_preserved(const char *old, size_t old_length,
                                     const unsigned char *old_offsets, size_t old_offset_bytes,
                                     const char *current, size_t current_length,
                                     const unsigned char *current_offsets, size_t current_offset_bytes,
                                     uint64_t tile, uint64_t step) {
    snapshot *before = calloc(1, sizeof(*before)), *after = calloc(1, sizeof(*after));
    int status = SG_NOMEM;
    if (!before || !after) goto done;
    status = parse_snapshot(old, old_length, old_offsets, old_offset_bytes, tile, step, before);
    if (status != SG_VALID) goto done;
    status = parse_snapshot(current, current_length, current_offsets, current_offset_bytes, tile, step, after);
    if (status != SG_VALID) goto done;
    for (size_t slot = 0; slot < SG_SLOTS; slot++) {
        size_t j = after->first[slot];
        for (size_t i = before->first[slot]; i < before->first[slot + 1]; i++) {
            source_row *a = &before->rows[i];
            while (j < after->first[slot + 1] && after->rows[j].revision < a->revision) j++;
            if (j == after->first[slot + 1]) { status = SG_PORTABLE; goto done; }
            source_row *b = &after->rows[j++];
            if (a->revision != b->revision || a->received != b->received || a->server != b->server ||
                a->source_length != b->source_length || memcmp(a->source, b->source, a->source_length) ||
                memcmp(a->values, b->values, sizeof(a->values)) ||
                b->needed < a->needed || b->purge < a->purge) {
                status = SG_PORTABLE; goto done;
            }
        }
    }
done:
    release_snapshot(before); release_snapshot(after); return status;
}
