/* Stateless JSON packing/projection; body admission belongs to the caller. */
#include "yyjson.h"
#include <stdint.h>
#include <stdlib.h>
#include <string.h>
#include <math.h>

/* Stable CPython ABI. cw_validate must be invoked with the GIL held. */
extern double PyOS_string_to_double(const char *, char **, void *);
extern char *PyOS_double_to_string(double, char, int, int, int *);
extern void PyMem_Free(void *);

#define BODY_LIMIT (32u * 1024u * 1024u)
typedef struct { char *data; size_t length; } cw_part;
typedef struct {
    int status;
    char *packed;
    size_t packed_length;
    cw_part *parts;
    size_t count;
    size_t capacity;
} cw_result;

static int name_is(yyjson_val *key, const char *name) {
    size_t length = strlen(name);
    return yyjson_get_len(key) == length &&
           memcmp(yyjson_get_str(key), name, length) == 0;
}
static int shared(yyjson_val *key) {
    return name_is(key, "settings_snapshot") || name_is(key, "effective_parameters") ||
           name_is(key, "source_context") || name_is(key, "enrichment");
}
static int window(yyjson_val *key) {
    return name_is(key, "source_inputs") || name_is(key, "future_inputs");
}
static yyjson_mut_val *reference(yyjson_mut_doc *doc, size_t index) {
    yyjson_mut_val *ref = yyjson_mut_obj(doc), *ids = yyjson_mut_arr(doc);
    if (!ref || !ids || !yyjson_mut_arr_add_val(ids, yyjson_mut_uint(doc, index)) ||
        !yyjson_mut_obj_add_str(doc, ref, "mode", "value") ||
        !yyjson_mut_obj_add(ref, yyjson_mut_str(doc, "ids"), ids) ||
        !yyjson_mut_obj_add(ref, yyjson_mut_str(doc, "sha256"), yyjson_mut_uint(doc, index)))
        return NULL;
    return ref;
}
static int add_part(cw_result *result, char *raw, size_t length, size_t *index) {
    if (length > BODY_LIMIT || result->count >= 65536) {
        free(raw); result->status = 1; return 0;
    }
    if (result->count == result->capacity) {
        size_t capacity = result->capacity ? result->capacity * 2 : 8;
        cw_part *parts = realloc(result->parts, capacity * sizeof(*parts));
        if (!parts) { free(raw); result->status = 2; return 0; }
        result->parts = parts; result->capacity = capacity;
    }
    *index = result->count;
    result->parts[result->count].data = raw;
    result->parts[result->count].length = length;
    result->count++;
    return 1;
}
typedef int (*cw_save_fn)(const char *, size_t, char *, void *);
static yyjson_mut_val *saved_reference(yyjson_mut_doc *doc, const char *key) {
    yyjson_mut_val *ref = yyjson_mut_obj(doc), *ids = yyjson_mut_arr(doc);
    if (!ref || !ids || !yyjson_mut_arr_add_val(ids, yyjson_mut_strncpy(doc, key, 64)) ||
        !yyjson_mut_obj_add_str(doc, ref, "mode", "value") ||
        !yyjson_mut_obj_add(ref, yyjson_mut_str(doc, "ids"), ids) ||
        !yyjson_mut_obj_add(ref, yyjson_mut_str(doc, "sha256"), yyjson_mut_strncpy(doc, key, 64))) return NULL;
    return ref;
}
static yyjson_mut_val *pack(yyjson_mut_doc *doc, yyjson_val *value,
                            cw_result *result, unsigned depth, cw_save_fn save, void *context) {
    if (depth > 200) { result->status = 1; return NULL; }
    if (yyjson_is_arr(value)) {
        size_t i, length; yyjson_val *child;
        yyjson_mut_val *array = yyjson_mut_arr(doc);
        if (!array) goto allocation;
        yyjson_arr_foreach(value, i, length, child) {
            yyjson_mut_val *item = pack(doc, child, result, depth + 1, save, context);
            if (!item) return NULL;
            if (!yyjson_mut_arr_add_val(array, item)) goto allocation;
        }
        return array;
    }
    if (!yyjson_is_obj(value)) {
        yyjson_mut_val *copy = yyjson_val_mut_copy(doc, value);
        if (!copy) goto allocation;
        return copy;
    }
    size_t i, length; yyjson_val *key, *child;
    yyjson_mut_val *object = yyjson_mut_obj(doc), *fields = yyjson_mut_obj(doc);
    int has_fields = 0;
    if (!object || !fields) goto allocation;
    yyjson_obj_foreach(value, i, length, key, child) {
        if (name_is(key, "_history_storage")) { result->status = 1; return NULL; }
        int share = shared(key) && yyjson_is_obj(child);
        int is_window = window(key) && yyjson_is_arr(child) && yyjson_get_len(child);
        char *raw = NULL; size_t raw_length = 0;
        if (share || is_window) {
            raw = yyjson_val_write(child, 0, &raw_length);
            if (!raw) goto allocation;
            if (share && raw_length <= 256) { free(raw); raw = NULL; share = 0; }
        }
        yyjson_mut_val *mutable_key = yyjson_mut_strncpy(doc, yyjson_get_str(key), yyjson_get_len(key));
        if (!mutable_key) { free(raw); goto allocation; }
        if (share || is_window) {
            size_t index;
            if (!add_part(result, raw, raw_length, &index)) return NULL;
            yyjson_mut_val *ref;
            if (save) {
                char key[65] = {0};
                if (!save(result->parts[index].data, result->parts[index].length, key, context) || key[64]) {
                    result->status = 3; return NULL;
                }
                for (size_t j = 0; j < 64; j++) if (!((key[j] >= '0' && key[j] <= '9') || (key[j] >= 'a' && key[j] <= 'f'))) {
                    result->status = 3; return NULL;
                }
                ref = saved_reference(doc, key);
            } else ref = reference(doc, index);
            if (!ref || !yyjson_mut_obj_add(fields, mutable_key, ref)) goto allocation;
            has_fields = 1;
            if (name_is(key, "settings_snapshot")) {
                yyjson_mut_val *projection = yyjson_mut_obj(doc);
                const char *names[] = {"profile", "alert_universe_mode"};
                if (!projection) goto allocation;
                for (size_t n = 0; n < 2; n++) {
                    yyjson_val *part = yyjson_obj_get(child, names[n]);
                    if (part) {
                        yyjson_mut_val *copy = yyjson_val_mut_copy(doc, part);
                        if (!copy || !yyjson_mut_obj_add(projection, yyjson_mut_str(doc, names[n]), copy))
                            goto allocation;
                    }
                }
                yyjson_mut_val *projection_key = yyjson_mut_strncpy(doc, yyjson_get_str(key), yyjson_get_len(key));
                if (!projection_key || !yyjson_mut_obj_add(object, projection_key, projection)) goto allocation;
            }
        } else {
            yyjson_mut_val *copy = pack(doc, child, result, depth + 1, save, context);
            if (!copy) return NULL;
            if (!yyjson_mut_obj_add(object, mutable_key, copy)) goto allocation;
        }
    }
    if (has_fields) {
        yyjson_mut_val *storage = yyjson_mut_obj(doc);
        if (!storage || !yyjson_mut_obj_add_str(doc, storage, "schema", "cryptowatcher.shared-fields.v1") ||
            !yyjson_mut_obj_add(storage, yyjson_mut_str(doc, "fields"), fields) ||
            !yyjson_mut_obj_add(object, yyjson_mut_str(doc, "_history_storage"), storage)) goto allocation;
    }
    return object;
allocation:
    result->status = 2; return NULL;
}

cw_result *cw_extract_saved(const char *data, size_t length, cw_save_fn save, void *context) {
    cw_result *result = calloc(1, sizeof(*result));
    if (!result) return NULL;
    yyjson_doc *source = yyjson_read(data, length, YYJSON_READ_NUMBER_AS_RAW);
    if (!source) { result->status = 3; return result; }
    yyjson_mut_doc *destination = yyjson_mut_doc_new(NULL);
    if (!destination) { yyjson_doc_free(source); result->status = 2; return result; }
    yyjson_val *root = yyjson_doc_get_root(source);
    if (!yyjson_is_obj(root)) result->status = 3;
    else {
        yyjson_mut_val *packed = pack(destination, root, result, 0, save, context);
        if (packed) {
            yyjson_mut_doc_set_root(destination, packed);
            result->packed = yyjson_mut_write(destination, 0, &result->packed_length);
            if (!result->packed) result->status = 2;
        }
    }
    yyjson_mut_doc_free(destination); yyjson_doc_free(source);
    return result;
}
cw_result *cw_extract(const char *data, size_t length) {
    return cw_extract_saved(data, length, NULL, NULL);
}
void cw_release(cw_result *result) {
    if (!result) return;
    for (size_t i = 0; i < result->count; i++) free(result->parts[i].data);
    free(result->parts); free(result->packed); free(result);
}

cw_result *cw_head(const char *data, size_t length, unsigned kind) {
    static const char *event[] = {"schema","event_id","revision","episode_id","market_observation_ms",
        "available_at_ms","market_type","detector_id","detector_version","symbol","timeframe","direction","strength",NULL};
    static const char *outcome[] = {"schema","outcome_id","revision","event_id","event_revision",
        "available_at_ms","state","horizon_min","methodology_version","classification","noise_threshold_pct","metrics",NULL};
    static const char *episode[] = {"schema","episode_id","revision","instrument","direction","state",
        "started_ms","last_observation_ms","longest_tf_ms","strength","available_at_ms",NULL};
    static const char *comparison[] = {"schema","comparison_id","revision","event_id","available_at_ms","state",NULL};
    const char **fields = kind == 0 ? event : kind == 1 ? outcome : kind == 2 ? episode : comparison;
    cw_result *result = calloc(1, sizeof(*result));
    if (!result) return NULL;
    if (kind > 3) { result->status = 3; return result; }
    yyjson_doc *source = yyjson_read(data, length, YYJSON_READ_NUMBER_AS_RAW);
    if (!source) { result->status = 3; return result; }
    yyjson_mut_doc *destination = yyjson_mut_doc_new(NULL);
    if (!destination) { yyjson_doc_free(source); result->status = 2; return result; }
    yyjson_val *root = yyjson_doc_get_root(source);
    if (!yyjson_is_obj(root)) result->status = 3;
    else {
        yyjson_mut_val *object = yyjson_mut_obj(destination);
        if (!object) result->status = 2;
        else {
            for (size_t n = 0; fields[n]; n++) {
                yyjson_val *value = yyjson_obj_get(root, fields[n]);
                if (value) {
                    yyjson_mut_val *copy = yyjson_val_mut_copy(destination, value);
                    if (!copy || !yyjson_mut_obj_add(object, yyjson_mut_str(destination, fields[n]), copy)) {
                        result->status = 2; break;
                    }
                }
            }
            if (!result->status) {
                yyjson_mut_doc_set_root(destination, object);
                result->packed = yyjson_mut_write(destination, 0, &result->packed_length);
                if (!result->packed) result->status = 2;
            }
        }
    }
    yyjson_mut_doc_free(destination); yyjson_doc_free(source);
    return result;
}

static int canonical_node(yyjson_val *value, unsigned depth, unsigned int_limit) {
    if (depth > 200) return 1; /* unsupported: caller keeps the Python gate */
    if (yyjson_is_raw(value)) {
        const char *number = yyjson_get_raw(value);
        size_t length = yyjson_get_len(value);
        int floating = 0;
        for (size_t i = 0; i < length; i++)
            if (number[i] == '.' || number[i] == 'e' || number[i] == 'E') floating = 1;
        if (!floating) {
            size_t digits = length - (length && number[0] == '-');
            if (int_limit && digits > int_limit) return 1;
            return length == 2 && number[0] == '-' && number[1] == '0' ? 4 : 0;
        }
        if (length > 128) return 1;
        char token[129]; memcpy(token, number, length); token[length] = 0;
        char *end = NULL;
        double parsed = PyOS_string_to_double(token, &end, NULL);
        if (end != token + length || !isfinite(parsed)) return 4;
        char *rendered = PyOS_double_to_string(parsed, 'r', 0, 2, NULL);
        if (!rendered) return 2;
        int status = strlen(rendered) == length && memcmp(rendered, number, length) == 0 ? 0 : 4;
        PyMem_Free(rendered);
        return status;
    }
    if (yyjson_is_arr(value)) {
        size_t i, length; yyjson_val *child;
        yyjson_arr_foreach(value, i, length, child) {
            int status = canonical_node(child, depth + 1, int_limit);
            if (status) return status;
        }
    } else if (yyjson_is_obj(value)) {
        size_t i, length; yyjson_val *key, *child, *previous = NULL;
        yyjson_obj_foreach(value, i, length, key, child) {
            if (previous) {
                size_t a = yyjson_get_len(previous), b = yyjson_get_len(key);
                int order = memcmp(yyjson_get_str(previous), yyjson_get_str(key), a < b ? a : b);
                if (order > 0 || (order == 0 && a >= b)) return 4;
            }
            int status = canonical_node(child, depth + 1, int_limit);
            if (status) return status;
            previous = key;
        }
    }
    return 0;
}

int cw_validate(const char *data, size_t length, unsigned int_limit, size_t *array_length) {
    yyjson_doc *document = yyjson_read(data, length, YYJSON_READ_NUMBER_AS_RAW);
    if (!document) return 3;
    yyjson_val *root = yyjson_doc_get_root(document);
    int status = canonical_node(root, 0, int_limit);
    if (!status) {
        size_t written = 0;
        char *canonical = yyjson_write(document, 0, &written);
        if (!canonical) status = 2;
        else {
            if (written != length || memcmp(canonical, data, length) != 0) status = 4;
            free(canonical);
        }
    }
    *array_length = yyjson_is_arr(root) ? yyjson_get_len(root) : (size_t)-1;
    yyjson_doc_free(document);
    return status;
}
