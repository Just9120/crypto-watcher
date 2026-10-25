/* Reuse the bounded authenticated JS1 validator for materialization as well.
 * All returned source trees are immutable; public mutable views are fresh.
 */
#define CW_SOURCE_PYTHON
#include "source_guard.c"

static snapshot *frozen_source_parse(PyObject *raw, PyObject *offsets, uint64_t tile, uint64_t step) {
    if (!PyBytes_CheckExact(raw) || !PyBytes_CheckExact(offsets)) return NULL;
    snapshot *value = calloc(1, sizeof(*value));
    if (!value) { PyErr_NoMemory(); return NULL; }
    int status = parse_snapshot_common(PyBytes_AS_STRING(raw), (size_t)PyBytes_GET_SIZE(raw),
        (const unsigned char *)PyBytes_AS_STRING(offsets), (size_t)PyBytes_GET_SIZE(offsets),
        tile, step, value, 1);
    if (status == SG_VALID) return value;
    release_snapshot(value);
    if (status == SG_NOMEM && !PyErr_Occurred()) PyErr_NoMemory();
    return NULL;
}

static PyObject *frozen_source_values(PyObject *self, PyObject *args) {
    (void)self;
    unsigned long long tile, step; PyObject *raw, *offsets;
    if (!PyArg_ParseTuple(args, "KKOO", &tile, &step, &raw, &offsets)) return NULL;
    snapshot *value = frozen_source_parse(raw, offsets, tile, step);
    if (!value) return typed_fallback();
    PyObject *slots = PyTuple_New(SG_SLOTS);
    if (!slots) goto failed;
    for (size_t i = 0; i < SG_SLOTS; i++) {
        PyObject *rows = PyTuple_New((Py_ssize_t)(value->first[i + 1] - value->first[i]));
        if (!rows) goto failed;
        PyTuple_SET_ITEM(slots, (Py_ssize_t)i, rows);
        for (size_t j = value->first[i]; j < value->first[i + 1]; j++) {
            source_row *saved = &value->rows[j];
            PyObject *tail = PyTuple_New(9), *row = PyTuple_New(4);
            if (!tail || !row) { Py_XDECREF(tail); Py_XDECREF(row); goto failed; }
            PyTuple_SET_ITEM(row, 3, tail);
            PyTuple_SET_ITEM(rows, (Py_ssize_t)(j - value->first[i]), row);
            for (unsigned n = 0; n < 6; n++) {
                PyObject *number = saved->integer_mask & (1U << n) ?
                    PyLong_FromUnsignedLongLong((uint64_t)saved->values[n]) : PyFloat_FromDouble(saved->values[n]);
                if (!number) goto failed;
                PyTuple_SET_ITEM(tail, n, number);
            }
            PyObject *source = PyUnicode_DecodeUTF8(saved->source, (Py_ssize_t)saved->source_length, "strict");
            if (!source) goto failed;
            PyTuple_SET_ITEM(tail, 6, source);
            uint64_t numbers[] = {saved->revision, saved->needed, saved->purge, saved->received, saved->server};
            for (unsigned n = 0; n < 5; n++) {
                PyObject *number = PyLong_FromUnsignedLongLong(numbers[n]);
                if (!number) goto failed;
                if (n < 3) PyTuple_SET_ITEM(row, n, number);
                else PyTuple_SET_ITEM(tail, n + 4, number);
            }
        }
    }
    release_snapshot(value);
    return slots;
failed:
    release_snapshot(value); Py_XDECREF(slots);
    return typed_fallback();
}

static PyObject *frozen_source_stats(PyObject *self, PyObject *args) {
    (void)self;
    unsigned long long tile, step; PyObject *raw, *offsets;
    if (!PyArg_ParseTuple(args, "KKOO", &tile, &step, &raw, &offsets)) return NULL;
    snapshot *value = frozen_source_parse(raw, offsets, tile, step);
    if (!value) return typed_fallback();
    PyObject *result = Py_BuildValue("KK", (unsigned long long)value->count,
        (unsigned long long)value->minimum_purge);
    release_snapshot(value);
    return result;
}

static PyObject *frozen_source_preserved(PyObject *self, PyObject *args) {
    (void)self;
    unsigned long long tile, step; PyObject *old, *old_offsets, *current, *current_offsets;
    if (!PyArg_ParseTuple(args, "KKOOOO", &tile, &step, &old, &old_offsets, &current, &current_offsets)) return NULL;
    snapshot *before = frozen_source_parse(old, old_offsets, tile, step);
    if (!before) return typed_fallback();
    snapshot *after = frozen_source_parse(current, current_offsets, tile, step);
    if (!after) { release_snapshot(before); return typed_fallback(); }
    int status = snapshot_preserved(before, after);
    release_snapshot(before); release_snapshot(after);
    if (status != SG_VALID) Py_RETURN_NOTIMPLEMENTED;
    Py_RETURN_TRUE;
}
