"""Native Windows ACLs for private storage, without optional dependencies.

A private object is owned by the current user or a privileged Windows account.
Its discretionary ACL grants access only to that user, SYSTEM, and Administrators (the Windows equivalent
of privileged Unix root access). New directories give children the same ACL.
Existing permissions are checked, never silently rewritten.
"""

from __future__ import annotations

import ctypes
import os
from ctypes import wintypes as w
from functools import lru_cache
from pathlib import Path
from typing import Any, cast


class _SecurityAttributes(ctypes.Structure):
    _fields_ = [("length", w.DWORD), ("descriptor", w.LPVOID), ("inherit", w.BOOL)]


class _AclSize(ctypes.Structure):
    _fields_ = [("count", w.DWORD), ("used", w.DWORD), ("free", w.DWORD)]


@lru_cache(maxsize=1)
def _api() -> tuple[Any, Any]:
    # Imported lazily: ctypes.WinDLL is unavailable on Unix.
    adv = cast(Any, ctypes).WinDLL("advapi32", use_last_error=True)
    kernel = cast(Any, ctypes).WinDLL("kernel32", use_last_error=True)
    signatures = [
        (kernel.GetCurrentProcess, [], w.HANDLE),
        (kernel.CloseHandle, [w.HANDLE], w.BOOL),
        (kernel.LocalFree, [w.HLOCAL], w.HLOCAL),
        (
            kernel.CreateDirectoryW,
            [w.LPCWSTR, ctypes.POINTER(_SecurityAttributes)],
            w.BOOL,
        ),
        (
            kernel.CreateFileW,
            [
                w.LPCWSTR,
                w.DWORD,
                w.DWORD,
                ctypes.POINTER(_SecurityAttributes),
                w.DWORD,
                w.DWORD,
                w.HANDLE,
            ],
            w.HANDLE,
        ),
        (adv.OpenProcessToken, [w.HANDLE, w.DWORD, ctypes.POINTER(w.HANDLE)], w.BOOL),
        (
            adv.GetTokenInformation,
            [w.HANDLE, ctypes.c_int, w.LPVOID, w.DWORD, ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
        (adv.ConvertSidToStringSidW, [w.LPVOID, ctypes.POINTER(w.LPWSTR)], w.BOOL),
        (
            adv.ConvertStringSecurityDescriptorToSecurityDescriptorW,
            [w.LPCWSTR, w.DWORD, ctypes.POINTER(w.LPVOID), ctypes.POINTER(w.DWORD)],
            w.BOOL,
        ),
        (
            adv.GetNamedSecurityInfoW,
            [
                w.LPWSTR,
                ctypes.c_int,
                w.DWORD,
                ctypes.POINTER(w.LPVOID),
                ctypes.POINTER(w.LPVOID),
                ctypes.POINTER(w.LPVOID),
                ctypes.POINTER(w.LPVOID),
                ctypes.POINTER(w.LPVOID),
            ],
            w.DWORD,
        ),
        (adv.GetAclInformation, [w.LPVOID, w.LPVOID, w.DWORD, ctypes.c_int], w.BOOL),
        (adv.GetAce, [w.LPVOID, w.DWORD, ctypes.POINTER(w.LPVOID)], w.BOOL),
    ]
    for function, arguments, result in signatures:
        function.argtypes, function.restype = arguments, result
    return adv, kernel


def _check(ok: Any) -> None:
    if not ok:
        raise OSError("Windows could not validate private storage permissions")


def _sid_text(sid: Any) -> str:
    adv, kernel = _api()
    text = w.LPWSTR()
    _check(adv.ConvertSidToStringSidW(sid, ctypes.byref(text)))
    try:
        return str(text.value)
    finally:
        kernel.LocalFree(text)


@lru_cache(maxsize=1)
def _user_sid() -> str:
    adv, kernel = _api()
    token = w.HANDLE()
    _check(
        adv.OpenProcessToken(kernel.GetCurrentProcess(), 0x0008, ctypes.byref(token))
    )
    try:
        size = w.DWORD()
        adv.GetTokenInformation(token, 1, None, 0, ctypes.byref(size))
        buffer = ctypes.create_string_buffer(size.value)
        _check(adv.GetTokenInformation(token, 1, buffer, size, ctypes.byref(size)))
        sid = ctypes.cast(buffer, ctypes.POINTER(w.LPVOID))[0]
        return _sid_text(sid)
    finally:
        kernel.CloseHandle(token)


def check_private(path: Path) -> None:
    """Reject links, foreign owners, and ACL grants to unprivileged others."""
    if (
        cast(Any, path.lstat()).st_file_attributes & 0x400
    ):  # FILE_ATTRIBUTE_REPARSE_POINT
        raise OSError("private storage must not be a Windows reparse point")
    adv, kernel = _api()
    owner, dacl, descriptor = w.LPVOID(), w.LPVOID(), w.LPVOID()
    status = adv.GetNamedSecurityInfoW(
        str(path.absolute()),
        1,
        0x0001 | 0x0004,
        ctypes.byref(owner),
        None,
        ctypes.byref(dacl),
        None,
        ctypes.byref(descriptor),
    )
    if status:
        raise OSError("Windows could not read private storage permissions")
    try:
        allowed = {_user_sid(), "S-1-5-18", "S-1-5-32-544"}
        if _sid_text(owner) not in allowed or not dacl:
            raise OSError(
                "private storage must be owned by you and have a restricted ACL"
            )
        size = _AclSize()
        _check(adv.GetAclInformation(dacl, ctypes.byref(size), ctypes.sizeof(size), 2))
        for index in range(size.count):
            ace = w.LPVOID()
            _check(adv.GetAce(dacl, index, ctypes.byref(ace)))
            assert ace.value is not None
            kind = ctypes.c_ubyte.from_address(ace.value).value
            if kind == 1:  # ACCESS_DENIED_ACE never grants access
                continue
            # Reject unfamiliar/object/callback grants conservatively.
            if kind != 0 or _sid_text(ace.value + 8) not in allowed:
                raise OSError("private storage grants access to other Windows accounts")
    finally:
        kernel.LocalFree(descriptor)


def create_private(path: Path, *, directory: bool = False) -> None:
    """Create an object with a restricted ACL, or validate an existing one."""
    adv, kernel = _api()
    descriptor = w.LPVOID()
    flags = "OICI" if directory else ""
    sid = _user_sid()
    sddl = f"O:{sid}D:P(A;{flags};FA;;;{sid})(A;{flags};FA;;;SY)(A;{flags};FA;;;BA)"
    _check(
        adv.ConvertStringSecurityDescriptorToSecurityDescriptorW(
            sddl,
            1,
            ctypes.byref(descriptor),
            None,
        )
    )
    attributes = _SecurityAttributes(
        ctypes.sizeof(_SecurityAttributes), descriptor, False
    )
    try:
        if directory:
            created = kernel.CreateDirectoryW(
                str(path.absolute()), ctypes.byref(attributes)
            )
            if not created and cast(Any, ctypes).get_last_error() != 183:
                raise OSError("Windows could not create a private directory")
        else:
            handle = kernel.CreateFileW(
                str(path.absolute()),
                0x80000000 | 0x40000000,
                7,
                ctypes.byref(attributes),
                1,
                0x80,
                None,  # CREATE_NEW, normal file
            )
            if handle == w.HANDLE(-1).value:
                if cast(Any, ctypes).get_last_error() not in (80, 183):
                    raise OSError("Windows could not create a private file")
            else:
                kernel.CloseHandle(handle)
    finally:
        kernel.LocalFree(descriptor)
    check_private(path)


def is_windows() -> bool:
    """Whether native ACL validation is required on this interpreter."""
    return os.name == "nt"
