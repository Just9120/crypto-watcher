/* CJ3 is frozen: exact tags, insertion order, float bits and column layout.
 * Unsupported/oversized objects fall back to the complete portable codec.
 * This helper owns no Python objects after returning and never touches SQL.
 */
#include <math.h>
#include <string.h>

#define TYPED_BYTES (4U * 1024U * 1024U)
#define TYPED_NODES 1000000U
#define TYPED_DEPTH 64U

typedef struct {
    unsigned char *data;
    size_t length, capacity, nodes, allocated;
    PyObject *words;
} TypedWriter;

typedef struct {
    const unsigned char *data;
    size_t length, pos, nodes, allocated;
    PyObject *words;
} TypedReader;

static int typed_write(TypedWriter *out, const void *raw, size_t size) {
    if (size > TYPED_BYTES - out->length) return 0;
    size_t required = out->length + size;
    if (required > out->capacity) {
        size_t capacity = out->capacity ? out->capacity : 256;
        while (capacity < required) capacity *= 2;
        unsigned char *next = PyMem_Realloc(out->data, capacity);
        if (!next) { PyErr_NoMemory(); return 0; }
        out->data = next; out->capacity = capacity;
    }
    if (size) memcpy(out->data + out->length, raw, size);
    out->length = required;
    return 1;
}

static int typed_tag(TypedWriter *out, unsigned char tag) {
    return typed_write(out, &tag, 1);
}

static int typed_number(TypedWriter *out, uint64_t number) {
    unsigned char raw[10];
    size_t size = 0;
    while (number >= 128) { raw[size++] = (number & 127) | 128; number >>= 7; }
    raw[size++] = (unsigned char)number;
    return typed_write(out, raw, size);
}

static int typed_hex(unsigned char c) {
    if (c >= '0' && c <= '9') return c - '0';
    if (c >= 'a' && c <= 'f') return c - 'a' + 10;
    return -1;
}

static int typed_put(TypedWriter *, PyObject *, unsigned);

static int typed_put_string(TypedWriter *out, PyObject *value) {
    PyObject *word = PyDict_GetItemWithError(out->words, value);
    if (word) {
        if (!PyLong_CheckExact(word)) return 0;
        unsigned long long index = PyLong_AsUnsignedLongLong(word);
        if (PyErr_Occurred()) return 0;
        return typed_tag(out, 8) && typed_number(out, index);
    }
    if (PyErr_Occurred()) return 0;
    Py_ssize_t size;
    const char *raw = PyUnicode_AsUTF8AndSize(value, &size);
    if (!raw) return 0;
    if (size == 36 || size == 64) {
        unsigned char binary[32];
        size_t count = 0;
        int high = -1, valid = 1;
        for (Py_ssize_t i = 0; i < size; i++) {
            if (size == 36 && (i == 8 || i == 13 || i == 18 || i == 23)) {
                if (raw[i] != '-') { valid = 0; break; }
                continue;
            }
            int digit = typed_hex((unsigned char)raw[i]);
            if (digit < 0) { valid = 0; break; }
            if (high < 0) high = digit;
            else { binary[count++] = (unsigned char)((high << 4) | digit); high = -1; }
        }
        if (valid && high < 0)
            return typed_tag(out, size == 36 ? 9 : 10) && typed_write(out, binary, count);
    }
    return typed_tag(out, 5) && typed_number(out, (uint64_t)size) && typed_write(out, raw, (size_t)size);
}

static int typed_put_list(TypedWriter *out, PyObject *value, unsigned depth) {
    Py_ssize_t size = PyList_GET_SIZE(value);
    if (size > TYPED_NODES) return 0;
    PyObject *keys = NULL;
    int columnar = 0;
    if (size >= 4 && PyDict_CheckExact(PyList_GET_ITEM(value, 0))) {
        Py_ssize_t width = PyDict_Size(PyList_GET_ITEM(value, 0));
        if ((uint64_t)width > (32U * 1024U * 1024U - out->allocated) / sizeof(PyObject *)) return 0;
        out->allocated += (size_t)width * sizeof(PyObject *);
        keys = PyDict_Keys(PyList_GET_ITEM(value, 0));
        if (!keys) return 0;
        for (Py_ssize_t j = 0; j < PyList_GET_SIZE(keys); j++)
            if (!PyUnicode_CheckExact(PyList_GET_ITEM(keys, j))) { Py_DECREF(keys); return 0; }
        columnar = PyList_GET_SIZE(keys) > 0;
        for (Py_ssize_t i = 1; columnar && i < size; i++) {
            PyObject *row = PyList_GET_ITEM(value, i);
            if (!PyDict_CheckExact(row)) { columnar = 0; break; }
            if (PyDict_Size(row) != width) { columnar = 0; break; }
            Py_ssize_t at = 0, j = 0; PyObject *key, *child;
            while (PyDict_Next(row, &at, &key, &child)) {
                if (!PyUnicode_CheckExact(key)) { Py_DECREF(keys); return 0; }
                int compared = PyUnicode_Compare(key, PyList_GET_ITEM(keys, j++));
                if (PyErr_Occurred()) { Py_DECREF(keys); return 0; }
                if (compared) { columnar = 0; break; }
            }
        }
    }
    if (!columnar) {
        Py_XDECREF(keys);
        if (!typed_tag(out, 6) || !typed_number(out, (uint64_t)size)) return 0;
        for (Py_ssize_t i = 0; i < size; i++)
            if (!typed_put(out, PyList_GET_ITEM(value, i), depth + 1)) return 0;
        return 1;
    }
    int ok = typed_tag(out, 11) && typed_number(out, (uint64_t)size) && typed_put(out, keys, depth + 1);
    for (Py_ssize_t j = 0; ok && j < PyList_GET_SIZE(keys); j++) {
        if ((uint64_t)size > (32U * 1024U * 1024U - out->allocated) / sizeof(PyObject *)) { ok = 0; break; }
        out->allocated += (size_t)size * sizeof(PyObject *);
        PyObject *column = PyList_New(size);
        if (!column) { ok = 0; break; }
        PyObject *key = PyList_GET_ITEM(keys, j);
        for (Py_ssize_t i = 0; i < size; i++) {
            PyObject *child = PyDict_GetItemWithError(PyList_GET_ITEM(value, i), key);
            if (!child) { ok = 0; break; }
            Py_INCREF(child); PyList_SET_ITEM(column, i, child);
        }
        if (ok) ok = typed_put(out, column, depth + 1);
        Py_DECREF(column);
    }
    Py_DECREF(keys);
    return ok;
}

static int typed_put(TypedWriter *out, PyObject *value, unsigned depth) {
    if (depth > TYPED_DEPTH || ++out->nodes > TYPED_NODES) return 0;
    if (value == Py_None) return typed_tag(out, 0);
    if (value == Py_False) return typed_tag(out, 1);
    if (value == Py_True) return typed_tag(out, 2);
    if (PyLong_CheckExact(value)) {
        int overflow;
        long long signed_value = PyLong_AsLongLongAndOverflow(value, &overflow);
        if (overflow || PyErr_Occurred()) return 0;
        uint64_t number = (uint64_t)signed_value << 1;
        if (signed_value < 0) number = ~number;
        return typed_tag(out, 3) && typed_number(out, number);
    }
    if (PyFloat_CheckExact(value)) {
        double number = PyFloat_AS_DOUBLE(value);
        if (!isfinite(number)) return 0;
        uint64_t bits; unsigned char raw[8];
        memcpy(&bits, &number, sizeof(bits));
        for (unsigned i = 0; i < 8; i++) raw[i] = (unsigned char)(bits >> (56 - i * 8));
        return typed_tag(out, 4) && typed_write(out, raw, 8);
    }
    if (PyUnicode_CheckExact(value)) return typed_put_string(out, value);
    if (PyList_CheckExact(value)) return typed_put_list(out, value, depth);
    if (PyDict_CheckExact(value)) {
        if (PyDict_Size(value) > TYPED_NODES) return 0;
        if (!typed_tag(out, 7) || !typed_number(out, (uint64_t)PyDict_Size(value))) return 0;
        Py_ssize_t pos = 0; PyObject *key, *child;
        while (PyDict_Next(value, &pos, &key, &child)) {
            if (!PyUnicode_CheckExact(key) || !typed_put(out, key, depth + 1) ||
                !typed_put(out, child, depth + 1)) return 0;
        }
        return 1;
    }
    return 0;
}

/* General CJ3 accepts overlong integers. Values wider than uint64 and all
 * other unsupported encodings go to the original arbitrary-precision reader.
 */
static int typed_read_number(TypedReader *in, uint64_t *value) {
    uint64_t number = 0;
    for (unsigned shift = 0; shift < 70; shift += 7) {
        if (in->pos == in->length) return 0;
        unsigned byte = in->data[in->pos++];
        if (shift == 63 && (byte & 127) > 1) return 0;
        number |= (uint64_t)(byte & 127) << shift;
        if (byte < 128) { *value = number; return 1; }
    }
    return 0;
}

static PyObject *typed_get(TypedReader *, unsigned);

/* Bound temporary native allocation even for deeply nested tiny malformed
 * inputs declaring huge collections. This is a fast-path limit, not a new
 * wire-format restriction: the original reader decides unsupported cases.
 */
static int typed_reserve(TypedReader *in, uint64_t count, size_t per_item) {
    const size_t budget = 32U * 1024U * 1024U;
    if (count > (budget - in->allocated) / per_item) return 0;
    in->allocated += (size_t)count * per_item;
    return 1;
}

static PyObject *typed_get_columns(TypedReader *in, uint64_t size, unsigned depth) {
    PyObject *keys = typed_get(in, depth + 1);
    PyObject *unique = NULL, *columns = NULL, *result = NULL;
    if (!keys || !PyList_CheckExact(keys) || PyList_GET_SIZE(keys) == 0) goto failed;
    Py_ssize_t width = PyList_GET_SIZE(keys);
    if (size && (uint64_t)width > TYPED_NODES / size) goto failed;
    unique = PySet_New(NULL);
    if (!unique) goto failed;
    for (Py_ssize_t i = 0; i < width; i++) {
        PyObject *key = PyList_GET_ITEM(keys, i);
        if (!PyUnicode_CheckExact(key)) goto failed;
        int found = PySet_Contains(unique, key);
        if (found != 0 || PySet_Add(unique, key) < 0) goto failed;
    }
    if (!typed_reserve(in, (uint64_t)width, sizeof(PyObject *))) goto failed;
    columns = PyList_New(width);
    if (!columns) goto failed;
    for (Py_ssize_t i = 0; i < width; i++) {
        PyObject *column = typed_get(in, depth + 1);
        if (!column) goto failed;
        PyList_SET_ITEM(columns, i, column);
        if (!PyList_CheckExact(column) || (uint64_t)PyList_GET_SIZE(column) != size) goto failed;
    }
    if (!typed_reserve(in, size, 128 + (size_t)width * 48)) goto failed;
    result = PyList_New((Py_ssize_t)size);
    if (!result) goto failed;
    for (Py_ssize_t i = 0; (uint64_t)i < size; i++) {
        PyObject *row = PyDict_New();
        if (!row) goto failed;
        PyList_SET_ITEM(result, i, row);
        for (Py_ssize_t j = 0; j < width; j++)
            if (PyDict_SetItem(row, PyList_GET_ITEM(keys, j),
                PyList_GET_ITEM(PyList_GET_ITEM(columns, j), i)) < 0) goto failed;
    }
    Py_DECREF(keys); Py_DECREF(unique); Py_DECREF(columns);
    return result;
failed:
    Py_XDECREF(keys); Py_XDECREF(unique); Py_XDECREF(columns); Py_XDECREF(result);
    return NULL;
}

static PyObject *typed_get(TypedReader *in, unsigned depth) {
    if (depth > TYPED_DEPTH || ++in->nodes > TYPED_NODES || in->pos == in->length) return NULL;
    unsigned tag = in->data[in->pos++];
    if (tag == 0) Py_RETURN_NONE;
    if (tag == 1) Py_RETURN_FALSE;
    if (tag == 2) Py_RETURN_TRUE;
    uint64_t number;
    if (tag == 3) {
        if (!typed_read_number(in, &number)) return NULL;
        return number & 1 ? PyLong_FromLongLong(-(int64_t)(number >> 1) - 1) :
            PyLong_FromUnsignedLongLong(number >> 1);
    }
    if (tag == 4) {
        if (in->length - in->pos < 8) return NULL;
        uint64_t bits = 0;
        for (unsigned i = 0; i < 8; i++) bits = (bits << 8) | in->data[in->pos++];
        double value; memcpy(&value, &bits, sizeof(value));
        return isfinite(value) ? PyFloat_FromDouble(value) : NULL;
    }
    if (tag == 5) {
        if (!typed_read_number(in, &number) || number > in->length - in->pos) return NULL;
        if (!typed_reserve(in, number, 4)) return NULL;
        PyObject *value = PyUnicode_DecodeUTF8((const char *)in->data + in->pos, (Py_ssize_t)number, "strict");
        in->pos += (size_t)number;
        return value;
    }
    if (tag == 8) {
        if (!typed_read_number(in, &number) || number >= (uint64_t)PyTuple_GET_SIZE(in->words)) return NULL;
        PyObject *value = PyTuple_GET_ITEM(in->words, (Py_ssize_t)number);
        if (!PyUnicode_CheckExact(value)) return NULL;
        Py_INCREF(value); return value;
    }
    if (tag == 9 || tag == 10) {
        size_t bytes = tag == 9 ? 16 : 32;
        if (bytes > in->length - in->pos) return NULL;
        const char *digits = "0123456789abcdef";
        char text[64]; size_t pos = 0;
        for (size_t i = 0; i < bytes; i++) {
            if (tag == 9 && (i == 4 || i == 6 || i == 8 || i == 10)) text[pos++] = '-';
            unsigned byte = in->data[in->pos++];
            text[pos++] = digits[byte >> 4]; text[pos++] = digits[byte & 15];
        }
        return PyUnicode_FromStringAndSize(text, (Py_ssize_t)pos);
    }
    if ((tag != 6 && tag != 7 && tag != 11) || !typed_read_number(in, &number) ||
        number > TYPED_NODES || number > in->length) return NULL;
    if (tag == 11) return typed_get_columns(in, number, depth);
    if (!typed_reserve(in, number, tag == 6 ? sizeof(PyObject *) : 64)) return NULL;
    PyObject *result = tag == 6 ? PyList_New((Py_ssize_t)number) : PyDict_New();
    if (!result) return NULL;
    for (Py_ssize_t i = 0; (uint64_t)i < number; i++) {
        PyObject *child = typed_get(in, depth + 1);
        if (!child) { Py_DECREF(result); return NULL; }
        if (tag == 6) { PyList_SET_ITEM(result, i, child); continue; }
        if (!PyUnicode_CheckExact(child) || PyDict_Contains(result, child) != 0) {
            Py_DECREF(child); Py_DECREF(result); return NULL;
        }
        PyObject *value = typed_get(in, depth + 1);
        int ok = value && PyDict_SetItem(result, child, value) == 0;
        Py_DECREF(child); Py_XDECREF(value);
        if (!ok) { Py_DECREF(result); return NULL; }
    }
    return result;
}

static PyObject *typed_fallback(void) {
    if (PyErr_Occurred()) {
        if (PyErr_ExceptionMatches(PyExc_MemoryError)) return NULL;
        PyErr_Clear();
    }
    Py_RETURN_NOTIMPLEMENTED;
}

static PyObject *typed_pack(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *value, *words;
    if (!PyArg_ParseTuple(args, "OO", &value, &words)) return NULL;
    if (!PyDict_CheckExact(words)) Py_RETURN_NOTIMPLEMENTED;
    TypedWriter out = {NULL, 0, 0, 0, 0, words};
    int ok = typed_put(&out, value, 0);
    PyObject *result = ok ? PyBytes_FromStringAndSize((const char *)out.data, (Py_ssize_t)out.length) : NULL;
    PyMem_Free(out.data);
    return result ? result : typed_fallback();
}

static PyObject *typed_unpack(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *raw, *words;
    if (!PyArg_ParseTuple(args, "OO", &raw, &words)) return NULL;
    if (!PyBytes_CheckExact(raw) || !PyTuple_CheckExact(words)) Py_RETURN_NOTIMPLEMENTED;
    Py_ssize_t size = PyBytes_GET_SIZE(raw);
    if (size <= 0 || size > TYPED_BYTES) Py_RETURN_NOTIMPLEMENTED;
    TypedReader in = {(const unsigned char *)PyBytes_AS_STRING(raw), (size_t)size, 0, 0, 0, words};
    PyObject *result = typed_get(&in, 0);
    if (result && in.pos != in.length) { Py_DECREF(result); result = NULL; }
    return result ? result : typed_fallback();
}
