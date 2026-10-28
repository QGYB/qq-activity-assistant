# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Application-owned Windows identity and icons; no process injection or hooks."""
from __future__ import annotations

from contextlib import contextmanager
import ctypes as C
from ctypes import wintypes as W
from pathlib import Path
import subprocess
import sys
import uuid

APP_ID = 'QQActivityAssistant.Desktop'
APP_NAME = '群活动助手'


def asset_path(name='app.ico'):
    return Path(__file__).resolve().parent / 'assets' / name


def set_process_identity():
    if sys.platform != 'win32':
        return False
    function = C.windll.shell32.SetCurrentProcessExplicitAppUserModelID
    function.argtypes = [W.LPCWSTR]
    function.restype = C.c_long
    return function(APP_ID) >= 0


class _GUID(C.Structure):
    _fields_ = [('data1',W.DWORD),('data2',W.WORD),('data3',W.WORD),('data4',C.c_ubyte*8)]

    @classmethod
    def parse(cls, text):
        return cls.from_buffer_copy(uuid.UUID(text).bytes_le)


class _Key(C.Structure):
    _fields_ = [('fmtid',_GUID),('pid',W.DWORD)]


class _VariantData(C.Union):
    _fields_ = [('pointer',C.c_void_p),('space',C.c_ubyte*16)]


class _Variant(C.Structure):
    _fields_ = [('vt',W.WORD),('reserved1',W.WORD),('reserved2',W.WORD),('reserved3',W.WORD),('data',_VariantData)]


def _check(result):
    if result < 0:
        raise OSError(f'Windows application identity HRESULT 0x{result & 0xffffffff:08X}')


def _method(store, slot, result, *arguments):
    table = C.cast(store,C.POINTER(C.POINTER(C.c_void_p))).contents
    return C.WINFUNCTYPE(result,C.c_void_p,*arguments)(table[slot])


@contextmanager
def _store(*, hwnd=None, shortcut=None):
    ole = C.windll.ole32
    ole.CoInitializeEx.argtypes=[C.c_void_p,W.DWORD]
    ole.CoInitializeEx.restype=C.c_long
    initialized = ole.CoInitializeEx(None,2) >= 0
    pointer = C.c_void_p()
    iid = _GUID.parse('886d8eeb-8cf2-4446-8d02-cdba1dbdcf99')
    try:
        if shortcut is not None:
            function=C.windll.shell32.SHGetPropertyStoreFromParsingName
            function.argtypes=[W.LPCWSTR,C.c_void_p,W.DWORD,C.POINTER(_GUID),C.POINTER(C.c_void_p)]
            function.restype=C.c_long
            _check(function(str(Path(shortcut).resolve()),None,2,C.byref(iid),C.byref(pointer)))
        else:
            function=C.windll.shell32.SHGetPropertyStoreForWindow
            function.argtypes=[W.HWND,C.POINTER(_GUID),C.POINTER(C.c_void_p)]
            function.restype=C.c_long
            _check(function(hwnd,C.byref(iid),C.byref(pointer)))
        yield pointer
    finally:
        if pointer:
            _method(pointer,2,W.ULONG)(pointer)
        if initialized:
            ole.CoUninitialize()


def _set_string(store, identifier, text):
    key=_Key(_GUID.parse('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3'),identifier)
    buffer=C.create_unicode_buffer(text)
    value=_Variant()
    value.vt=31  # VT_LPWSTR; SetValue copies the supplied string.
    value.data.pointer=C.cast(buffer,C.c_void_p).value
    _check(_method(store,6,C.c_long,C.POINTER(_Key),C.POINTER(_Variant))(store,C.byref(key),C.byref(value)))


def _get_string(store, identifier):
    key=_Key(_GUID.parse('9f4c2855-9f79-4b39-a8d0-e1d42de1d5f3'),identifier)
    value=_Variant()
    _check(_method(store,5,C.c_long,C.POINTER(_Key),C.POINTER(_Variant))(store,C.byref(key),C.byref(value)))
    try:
        return C.wstring_at(value.data.pointer) if value.vt==31 and value.data.pointer else ''
    finally:
        C.windll.ole32.PropVariantClear(C.byref(value))


def set_shortcut_identity(path):
    """Give an existing owned shortcut the same identity as its app process."""
    with _store(shortcut=path) as store:
        _set_string(store,5,APP_ID)
        _check(_method(store,7,C.c_long)(store))


def shortcut_identity(path):
    with _store(shortcut=path) as store:
        return _get_string(store,5)


def set_window_identity(hwnd, command=None):
    """Set taskbar grouping, icon and pin/relaunch metadata on our own window.

    Relaunch properties are assigned before ID, as required by the Shell API.
    https://learn.microsoft.com/windows/win32/properties/props-system-appusermodel-id
    """
    if command is None:
        arguments=[sys.executable] if getattr(sys,'frozen',False) else [sys.executable,str(Path(__file__).resolve().parent.parent/'启动桌面.pyw'),*sys.argv[1:]]
        command=subprocess.list2cmdline(arguments)
    with _store(hwnd=hwnd) as store:
        for identifier,text in ((2,command),(3,str(asset_path())+',0'),(4,APP_NAME),(5,APP_ID)):
            _set_string(store,identifier,text)


def apply_tk_icon(root):
    """Set the root/default dialog icons and retain their Tk image objects."""
    import tkinter as tk
    images=[tk.PhotoImage(master=root,file=str(asset_path(f'app-{size}.png'))) for size in (256,32)]
    root._application_icon_images=images
    root.iconphoto(True,*images)
    if sys.platform=='win32':
        root.iconbitmap(default=str(asset_path()))
        root.iconbitmap(str(asset_path()))
        def decorate():
            if root.winfo_exists():
                try:
                    set_window_identity(int(root.wm_frame(),0))
                except OSError:
                    pass  # Icons still work if optional Shell metadata is unavailable.
        root.after(100,decorate)
