/* Existing lossless SB3 column materializer, now under standard module ownership. */
#define ROWS 256
static int bad(const char *message) { PyErr_SetString(PyExc_ValueError,message); return 0; }
static int integer_bound(PyObject *v, PyObject *lower, PyObject *upper) {
    if (!PyLong_CheckExact(v)) return bad("Source column integer bound");
    int a=PyObject_RichCompareBool(v,lower,Py_GT), b=PyObject_RichCompareBool(v,upper,Py_LT);
    if (a<0 || b<0) return 0;
    return (a && b) || bad("Source column integer bound");
}
static PyObject *cumulative(PyObject *values, Py_ssize_t n, PyObject *lower, PyObject *upper) {
    if (!PyList_CheckExact(values) || PyList_GET_SIZE(values)!=n) { bad("Source column cardinality mismatch"); return NULL; }
    PyObject *out=PyList_New(n), *total=PyLong_FromLong(0);
    if (!out || !total) { Py_XDECREF(out); Py_XDECREF(total); return NULL; }
    for (Py_ssize_t i=0;i<n;i++) {
        PyObject *delta=PyList_GET_ITEM(values,i);
        if (!integer_bound(delta,lower,upper)) goto fail;
        PyObject *next=PyNumber_Add(total,delta);
        if (!next) goto fail;
        Py_SETREF(total,next);
        if (!integer_bound(total,lower,upper)) goto fail;
        Py_INCREF(total); PyList_SET_ITEM(out,i,total);
    }
    Py_DECREF(total); return out;
fail:
    Py_DECREF(total); Py_DECREF(out); return NULL;
}
static PyObject *decode(int kind, PyObject *value, Py_ssize_t n, PyObject *lower, PyObject *upper) {
    if (kind=='i') return cumulative(value,n,lower,upper);
    if (kind=='v') {
        if (!PyList_CheckExact(value) || PyList_GET_SIZE(value)!=n) { bad("Invalid source value column"); return NULL; }
        return Py_NewRef(value);
    }
    if (kind!='d' || !PyList_CheckExact(value) || PyList_GET_SIZE(value)!=3) { bad("Invalid decimal source column"); return NULL; }
    PyObject *scale_obj=PyList_GET_ITEM(value,0), *mask=PyList_GET_ITEM(value,2);
    if (!PyLong_CheckExact(scale_obj) || !PyLong_CheckExact(mask)) { bad("Source decimal allocation bound"); return NULL; }
    int overflow=0; long scale=PyLong_AsLongAndOverflow(scale_obj,&overflow);
    if (overflow || scale < -324 || scale > 308) { bad("Source decimal allocation bound"); return NULL; }
    PyObject *one=PyLong_FromLong(1), *shift=PyLong_FromSsize_t(n), *zero=PyLong_FromLong(0), *limit=NULL;
    if (!one || !shift || !zero) goto mask_fail;
    limit=PyNumber_Lshift(one,shift);
    if (!limit) goto mask_fail;
    int nonnegative=PyObject_RichCompareBool(mask,zero,Py_GE), bounded=PyObject_RichCompareBool(mask,limit,Py_LT);
    if (nonnegative<0 || bounded<0) goto mask_fail;
    if (!nonnegative || !bounded) { bad("Source decimal allocation bound"); goto mask_fail; }
    uint64_t words[4]={0,0,0,0};
    for (Py_ssize_t i=0;i<(n+63)/64;i++) {
        PyObject *bits=PyLong_FromSsize_t(i*64), *part=NULL;
        if (bits) part=PyNumber_Rshift(mask,bits);
        Py_XDECREF(bits);
        if (!part) goto mask_fail;
        words[i]=(uint64_t)PyLong_AsUnsignedLongLongMask(part); Py_DECREF(part);
        if (PyErr_Occurred()) goto mask_fail;
    }
    Py_DECREF(one); Py_DECREF(shift); Py_DECREF(zero); Py_DECREF(limit);
    PyObject *totals=cumulative(PyList_GET_ITEM(value,1),n,lower,upper), *out=NULL, *suffix=NULL, *minus=NULL;
    if (!totals) return NULL;
    out=PyList_New(n); suffix=PyUnicode_FromFormat("e%ld",scale); minus=PyUnicode_FromString("-");
    if (!out || !suffix || !minus) goto decimal_fail;
    for (Py_ssize_t i=0;i<n;i++) {
        PyObject *coefficient=PyList_GET_ITEM(totals,i), *text=PyObject_Str(coefficient);
        if (!text) goto decimal_fail;
        if (words[i/64] & (UINT64_C(1)<<(i%64))) {
            int truth=PyObject_IsTrue(coefficient);
            if (truth<0) { Py_DECREF(text); goto decimal_fail; }
            if (truth) { Py_DECREF(text); bad("Invalid source negative-zero mask"); goto decimal_fail; }
            PyObject *signed_text=PyUnicode_Concat(minus,text); Py_DECREF(text); text=signed_text;
            if (!text) goto decimal_fail;
        }
        PyObject *lexeme=PyUnicode_Concat(text,suffix); Py_DECREF(text);
        if (!lexeme) goto decimal_fail;
        PyObject *number=PyFloat_FromString(lexeme); Py_DECREF(lexeme);
        if (!number) goto decimal_fail;
        if (!isfinite(PyFloat_AS_DOUBLE(number))) { Py_DECREF(number); bad("Non-finite source decimal"); goto decimal_fail; }
        PyList_SET_ITEM(out,i,number);
    }
    Py_DECREF(totals); Py_DECREF(suffix); Py_DECREF(minus); return out;
decimal_fail:
    Py_DECREF(totals); Py_XDECREF(out); Py_XDECREF(suffix); Py_XDECREF(minus); return NULL;
mask_fail:
    Py_XDECREF(one); Py_XDECREF(shift); Py_XDECREF(zero); Py_XDECREF(limit); return NULL;
}
static int coordinate(PyObject *v) {
    if (!PyLong_CheckExact(v)) return 0;
    long long n=PyLong_AsLongLong(v);
    if (PyErr_Occurred()) { if (PyErr_ExceptionMatches(PyExc_OverflowError)) PyErr_Clear(); return 0; }
    return n>=0;
}
static int scalar_number(PyObject *v) {
    double n;
    if (PyFloat_CheckExact(v)) n=PyFloat_AS_DOUBLE(v);
    else if (PyLong_CheckExact(v)) {
        n=PyLong_AsDouble(v);
        if (PyErr_Occurred()) { if (PyErr_ExceptionMatches(PyExc_OverflowError)) PyErr_Clear(); return 0; }
    } else return 0;
    return isfinite(n);
}

static PyObject *frozen_column_restore_values(PyObject *counts,PyObject *kinds,PyObject *values,Py_ssize_t n) {
    if (n<=0 || n>ROWS || !PyList_CheckExact(counts) || PyList_GET_SIZE(counts)!=32 || !PyList_CheckExact(kinds) || PyList_GET_SIZE(kinds)!=12 || !PyList_CheckExact(values) || PyList_GET_SIZE(values)!=12) { bad("Source columns cardinality mismatch"); return NULL; }
    Py_ssize_t sizes[32], sum=0;
    for (Py_ssize_t i=0;i<32;i++) {
        PyObject *v=PyList_GET_ITEM(counts,i);
        if (!PyLong_CheckExact(v)) { bad("Source columns cardinality mismatch"); return NULL; }
        int overflow=0; long count=PyLong_AsLongAndOverflow(v,&overflow);
        if (overflow || count<0 || count>ROWS || sum+count>n) { bad("Source columns cardinality mismatch"); return NULL; }
        sizes[i]=(Py_ssize_t)count; sum+=count;
    }
    if (sum!=n) { bad("Source columns cardinality mismatch"); return NULL; }
    PyObject *columns[12]={NULL}, *one=PyLong_FromLong(1), *shift=PyLong_FromLong(8192), *upper=NULL, *lower=NULL, *rows=NULL, *out=NULL;
    if (!one || !shift) goto fail;
    upper=PyNumber_Lshift(one,shift);
    if (!upper) goto fail;
    lower=PyNumber_Negative(upper);
    if (!lower) goto fail;
    for (Py_ssize_t i=0;i<12;i++) {
        PyObject *kind=PyList_GET_ITEM(kinds,i);
        if (!PyUnicode_Check(kind) || PyUnicode_GetLength(kind)!=1) { bad("Invalid source column descriptor"); goto fail; }
        Py_UCS4 code=PyUnicode_ReadChar(kind,0);
        if (PyErr_Occurred()) goto fail;
        columns[i]=decode((int)code,PyList_GET_ITEM(values,i),n,lower,upper);
        if (!columns[i]) goto fail;
    }
    rows=PyList_New(n); out=PyList_New(32);
    if (!rows || !out) goto fail;
    for (Py_ssize_t i=0;i<n;i++) {
        int valid=1;
        for (Py_ssize_t field=0;field<12;field++) {
            PyObject *v=PyList_GET_ITEM(columns[field],i);
            if (field<3 || field>9) valid=coordinate(v);
            else if (field==9) {
                valid=PyUnicode_Check(v) && PyUnicode_AsUTF8(v)!=NULL;
                if (!valid && PyErr_ExceptionMatches(PyExc_UnicodeError)) PyErr_Clear();
            } else valid=scalar_number(v);
            if (!valid) { if (!PyErr_Occurred()) bad("Invalid source column slot"); goto fail; }
        }
        PyObject *row=PyList_New(4), *tail=PyList_New(9);
        if (!row || !tail) { Py_XDECREF(row); Py_XDECREF(tail); goto fail; }
        for (Py_ssize_t field=0;field<3;field++) { PyObject *v=PyList_GET_ITEM(columns[field],i); Py_INCREF(v); PyList_SET_ITEM(row,field,v); }
        for (Py_ssize_t field=3;field<12;field++) { PyObject *v=PyList_GET_ITEM(columns[field],i); Py_INCREF(v); PyList_SET_ITEM(tail,field-3,v); }
        PyList_SET_ITEM(row,3,tail); PyList_SET_ITEM(rows,i,row);
    }
    sum=0;
    for (Py_ssize_t i=0;i<32;i++) { PyObject *slot=PyList_GetSlice(rows,sum,sum+sizes[i]); if (!slot) goto fail; PyList_SET_ITEM(out,i,slot); sum+=sizes[i]; }
    Py_DECREF(one); Py_DECREF(shift); Py_DECREF(upper); Py_DECREF(lower); Py_DECREF(rows);
    for (Py_ssize_t i=0;i<12;i++) Py_DECREF(columns[i]);
    return out;
fail:
    Py_XDECREF(one); Py_XDECREF(shift); Py_XDECREF(upper); Py_XDECREF(lower); Py_XDECREF(rows); Py_XDECREF(out);
    for (Py_ssize_t i=0;i<12;i++) Py_XDECREF(columns[i]);
    return NULL;
}

static PyObject *frozen_column_values(PyObject *self, PyObject *args) {
    (void)self;
    PyObject *counts, *kinds, *values; Py_ssize_t n;
    if (!PyArg_ParseTuple(args, "OOOn", &counts, &kinds, &values, &n)) return NULL;
    PyObject *result = frozen_column_restore_values(counts, kinds, values, n);
    return result ? result : typed_fallback();
}
