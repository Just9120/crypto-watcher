/* Pure in-process operations on the already frozen storage formats.
 * Python owns every input/output reference; no database or retained state.
 */
#define PY_SSIZE_T_CLEAN
#include <Python.h>
#include <float.h>
#include <stdint.h>
#include "frozen_typed.h"
#include "frozen_source.h"
#include "frozen_columns.h"

#define LEASE_SLOTS 64
#if defined(_WIN32)
#define FROZEN_API __declspec(dllexport)
#else
#define FROZEN_API __attribute__((visibility("default")))
#endif

/* Check this scalar ABI before constructing any Python object. An untagged
 * file must never bind a different Python minor runtime/import library.
 */
FROZEN_API unsigned cw_frozen_python_minor(void) { return PY_VERSION_HEX >> 16; }
FROZEN_API unsigned cw_frozen_pointer_bits(void) { return sizeof(void *) * 8; }

static PyObject *codec_abi(PyObject *self, PyObject *unused) {
    (void)self; (void)unused;
    return PyLong_FromLong(sizeof(double) == 8 && FLT_RADIX == 2 && DBL_MANT_DIG == 53 ? 1 : 0);
}

static PyObject *lease_counts(PyObject *self, PyObject *value) {
    (void)self;
    if (!PyBytes_CheckExact(value)) Py_RETURN_NOTIMPLEMENTED;
    Py_ssize_t length = PyBytes_GET_SIZE(value);
    if (length < LEASE_SLOTS * 2 || length > LEASE_SLOTS * 18) Py_RETURN_NOTIMPLEMENTED;
    const unsigned char *raw = (const unsigned char *)PyBytes_AS_STRING(value);
    uint64_t values[LEASE_SLOTS * 2];
    Py_ssize_t pos = 0;
    for (size_t i = 0; i < LEASE_SLOTS * 2; i++) {
        Py_ssize_t start = pos;
        uint64_t number = 0;
        int complete = 0;
        for (unsigned shift = 0; shift < 63; shift += 7) {
            if (pos == length) Py_RETURN_NOTIMPLEMENTED;
            unsigned byte = raw[pos++];
            number |= (uint64_t)(byte & 127) << shift;
            if (!(byte & 128)) {
                if (pos - start > 1 && byte == 0) Py_RETURN_NOTIMPLEMENTED;
                complete = 1; break;
            }
        }
        if (!complete) Py_RETURN_NOTIMPLEMENTED;
        values[i] = number;
    }
    if (pos != length) Py_RETURN_NOTIMPLEMENTED;
    PyObject *details = PyList_New(LEASE_SLOTS);
    PyObject *quality = PyList_New(LEASE_SLOTS);
    PyObject *result = NULL;
    if (!details || !quality) goto failed;
    for (Py_ssize_t i = 0; i < LEASE_SLOTS; i++) {
        PyObject *a = PyLong_FromUnsignedLongLong(values[i]);
        if (!a) goto failed;
        PyList_SET_ITEM(details, i, a);
        PyObject *b = PyLong_FromUnsignedLongLong(values[i + LEASE_SLOTS]);
        if (!b) goto failed;
        PyList_SET_ITEM(quality, i, b);
    }
    result = PyTuple_New(2);
    if (!result) goto failed;
    PyTuple_SET_ITEM(result, 0, details);
    PyTuple_SET_ITEM(result, 1, quality);
    return result;
failed:
    Py_XDECREF(details); Py_XDECREF(quality);
    return NULL;
}

static PyMethodDef methods[] = {
    {"abi", codec_abi, METH_NOARGS, "Frozen operation ABI."},
    {"lease_counts", lease_counts, METH_O, "Exact NR1/NR2 decoded lease body or NotImplemented."},
    {"typed_pack", typed_pack, METH_VARARGS, "Exact CJ3 bytes or NotImplemented."},
    {"typed_unpack", typed_unpack, METH_VARARGS, "Exact CJ3 value or NotImplemented."},
    {"source_values", frozen_source_values, METH_VARARGS, "Immutable validated JS1 slots or NotImplemented."},
    {"source_stats", frozen_source_stats, METH_VARARGS, "Validated JS1 row count and purge or NotImplemented."},
    {"source_preserved", frozen_source_preserved, METH_VARARGS, "Exact retained JS1 values or NotImplemented."},
    {"column_values", frozen_column_values, METH_VARARGS, "Exact SB3 column values or NotImplemented."},
    {NULL, NULL, 0, NULL}
};

static struct PyModuleDef definition = {
    PyModuleDef_HEAD_INIT,
    "frozen_codec", "Stateless frozen storage operations.", -1, methods,
    NULL, NULL, NULL, NULL
};

PyMODINIT_FUNC PyInit_frozen_codec(void) {
    return PyModule_Create(&definition);
}
