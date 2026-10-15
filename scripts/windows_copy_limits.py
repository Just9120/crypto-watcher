"""Windows resource controls for isolated local qualification, never production."""

import ctypes
import sys
from ctypes import wintypes as wt

_JOB_HANDLE = None


def windows_limits():
    global _JOB_HANDLE
    assert sys.platform == "win32"
    k = ctypes.WinDLL("kernel32", use_last_error=True)

    class Basic(ctypes.Structure):
        _fields_ = [
            ("process_time", ctypes.c_int64),
            ("job_time", ctypes.c_int64),
            ("flags", wt.DWORD),
            ("minimum", ctypes.c_size_t),
            ("maximum", ctypes.c_size_t),
            ("active", wt.DWORD),
            ("affinity", ctypes.c_size_t),
            ("priority", wt.DWORD),
            ("scheduling", wt.DWORD),
        ]

    class Io(ctypes.Structure):
        _fields_ = [(n, ctypes.c_uint64) for n in ("ro", "wo", "oo", "rb", "wb", "ob")]

    class Extended(ctypes.Structure):
        _fields_ = [
            ("basic", Basic),
            ("io", Io),
            ("process_memory", ctypes.c_size_t),
            ("job_memory", ctypes.c_size_t),
            ("peak_process", ctypes.c_size_t),
            ("peak_job", ctypes.c_size_t),
        ]

    k.CreateJobObjectW.argtypes = [ctypes.c_void_p, wt.LPCWSTR]
    k.CreateJobObjectW.restype = wt.HANDLE
    k.SetInformationJobObject.argtypes = [wt.HANDLE, ctypes.c_int, ctypes.c_void_p, wt.DWORD]
    k.SetInformationJobObject.restype = wt.BOOL
    k.QueryInformationJobObject.argtypes = [
        wt.HANDLE,
        ctypes.c_int,
        ctypes.c_void_p,
        wt.DWORD,
        ctypes.c_void_p,
    ]
    k.QueryInformationJobObject.restype = wt.BOOL
    k.GetCurrentProcess.restype = wt.HANDLE
    k.AssignProcessToJobObject.argtypes = [wt.HANDLE, wt.HANDLE]
    k.AssignProcessToJobObject.restype = wt.BOOL
    k.SetProcessAffinityMask.argtypes = [wt.HANDLE, ctypes.c_size_t]
    k.SetProcessAffinityMask.restype = wt.BOOL
    k.GetProcessAffinityMask.argtypes = [
        wt.HANDLE,
        ctypes.POINTER(ctypes.c_size_t),
        ctypes.POINTER(ctypes.c_size_t),
    ]
    k.GetProcessAffinityMask.restype = wt.BOOL
    handle = k.CreateJobObjectW(None, None)
    assert handle, ctypes.get_last_error()
    value = Extended()
    value.basic.flags = 0x100 | 0x2000 | 0x8
    value.basic.active = 1
    value.process_memory = 4 * 1024**3
    assert k.SetInformationJobObject(handle, 9, ctypes.byref(value), ctypes.sizeof(value)), (
        ctypes.get_last_error()
    )
    process = k.GetCurrentProcess()
    assert k.AssignProcessToJobObject(handle, process), ctypes.get_last_error()
    assert k.SetProcessAffinityMask(process, 1), ctypes.get_last_error()
    checked = Extended()
    assert k.QueryInformationJobObject(handle, 9, ctypes.byref(checked), ctypes.sizeof(checked), None)
    mask, total = ctypes.c_size_t(), ctypes.c_size_t()
    assert k.GetProcessAffinityMask(process, ctypes.byref(mask), ctypes.byref(total))
    assert checked.process_memory == 4 * 1024**3 and mask.value == 1
    _JOB_HANDLE = handle
    return {
        "process_memory_limit_bytes": checked.process_memory,
        "logical_cpu_affinity_mask": mask.value,
        "os_file_cache_limit": "NOT_CONTROLLED_BY_PROCESS_JOB",
        "scope": "LOCAL_PC_ONLY_NOT_VPS_CAPACITY_PROOF",
    }


def process_memory_bytes():
    class Counters(ctypes.Structure):
        _fields_ = [("cb", wt.DWORD), ("page_faults", wt.DWORD)] + [
            (name, ctypes.c_size_t)
            for name in (
                "peak_working_set",
                "working_set",
                "peak_paged_pool",
                "paged_pool",
                "peak_nonpaged_pool",
                "nonpaged_pool",
                "pagefile",
                "peak_pagefile",
                "private",
            )
        ]

    kernel = ctypes.WinDLL("kernel32", use_last_error=True)
    kernel.GetCurrentProcess.restype = wt.HANDLE
    psapi = ctypes.WinDLL("psapi", use_last_error=True)
    psapi.GetProcessMemoryInfo.argtypes = [wt.HANDLE, ctypes.POINTER(Counters), wt.DWORD]
    psapi.GetProcessMemoryInfo.restype = wt.BOOL
    value = Counters()
    value.cb = ctypes.sizeof(value)
    if not psapi.GetProcessMemoryInfo(kernel.GetCurrentProcess(), ctypes.byref(value), value.cb):
        raise OSError(ctypes.get_last_error(), "Process memory measurement unavailable")
    assert value.private > 0 and value.working_set > 0
    return max(value.private, value.working_set)
