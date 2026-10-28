# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Start one new, normal QQ GUI process with our own build-specific bootstrap.

Never attaches to or stops an existing process. Installed files remain read-only.
The caller is responsible for the explicitly requested QQ restart beforehand.
"""
from __future__ import annotations

import ctypes as C
from ctypes import wintypes as W
import hashlib
import json
import os
from pathlib import Path
import struct
import subprocess
import sys
import time

SUPPORTED_BUILD = '9.9.35-52892'
EXE_SHA256 = 'b51c5cce851aea971d260a084e60aa0ffcb8f9ac0ea77cfcc22e23cf5a84d19e'
MODULE_SHA256 = '956b755fc6940283fce1faa4a688fe8080f07bd4a36067997b766f371beb16b5'


def _digest(path):
    with Path(path).open('rb') as stream:
        return hashlib.file_digest(stream, 'sha256').hexdigest()


def validate_build(qq_root, app_dir):
    root, app = Path(qq_root).resolve(), Path(app_dir).resolve()
    expected_app = root / 'versions' / SUPPORTED_BUILD / 'resources' / 'app'
    if app != expected_app:
        raise ValueError('QQ 版本未适配；仅支持 ' + SUPPORTED_BUILD)
    executable = root / 'QQ.exe'
    module = app.parent.parent / 'QQNT.dll'
    if _digest(executable) != EXE_SHA256 or _digest(module) != MODULE_SHA256:
        raise ValueError('QQ 文件指纹与已适配版本不一致，已停止接入')
    return executable, module


def build_environment(app_dir, module, shadow_package, bootstrap, environ=None):
    app, shadow, script = map(lambda p: Path(p).resolve(), (app_dir, shadow_package, bootstrap))
    if not shadow.is_file() or not script.is_file():
        raise ValueError('接收器启动文件缺失')
    package = json.loads(shadow.read_text(encoding='utf-8-sig'))
    main = package.get('main')
    if not isinstance(main, str) or not main or '\x00' in main:
        raise ValueError('影子入口配置无效')
    virtual = (app / main).resolve()
    if virtual.parent != app or virtual == app / 'package.json' or virtual.is_file():
        raise ValueError('影子入口必须是 QQ app 目录中未使用的直接子文件')
    env = dict(os.environ if environ is None else environ)
    for key in list(env):
        if key.upper() in ('NODE_OPTIONS', 'ELECTRON_RUN_AS_NODE') or key.upper().startswith(('NAPCAT_', 'QQ_ACTIVITY_')):
            del env[key]
    env.update(QQ_ACTIVITY_ORIGINAL_PACKAGE=str(app / 'package.json'),
               QQ_ACTIVITY_SHADOW_PACKAGE=str(shadow), QQ_ACTIVITY_VIRTUAL_BOOTSTRAP=str(virtual),
               QQ_ACTIVITY_BOOTSTRAP=str(script), QQ_ACTIVITY_MODULE=str(Path(module).resolve()))
    return env


def _environment_block(env):
    for key, value in env.items():
        if not isinstance(key, str) or not isinstance(value, str) or '\x00' in key + value or '=' in key:
            raise ValueError('无效的进程环境变量')
    return '\x00'.join(f'{key}={value}' for key, value in sorted(env.items(), key=lambda pair: pair[0].upper())) + '\x00\x00'


class _StartupInfo(C.Structure):
    _fields_ = [('cb', W.DWORD), ('reserved', W.LPWSTR), ('desktop', W.LPWSTR), ('title', W.LPWSTR),
                ('x', W.DWORD), ('y', W.DWORD), ('xsize', W.DWORD), ('ysize', W.DWORD),
                ('xchars', W.DWORD), ('ychars', W.DWORD), ('fill', W.DWORD), ('flags', W.DWORD),
                ('show', W.WORD), ('reserved_size', W.WORD), ('reserved_bytes', C.c_void_p),
                ('stdin', W.HANDLE), ('stdout', W.HANDLE), ('stderr', W.HANDLE)]


class _ProcessInfo(C.Structure):
    _fields_ = [('process', W.HANDLE), ('thread', W.HANDLE), ('pid', W.DWORD), ('tid', W.DWORD)]


class _ModuleEntry(C.Structure):
    _fields_ = [('size', W.DWORD), ('id', W.DWORD), ('pid', W.DWORD), ('global_usage', W.DWORD),
                ('process_usage', W.DWORD), ('base', C.c_void_p), ('base_size', W.DWORD),
                ('module', W.HMODULE), ('name', W.WCHAR * 256), ('path', W.WCHAR * 260)]


class _Windows:
    def __init__(self):
        self.k = C.WinDLL('kernel32', use_last_error=True)
        signatures = {
            'CreateProcessW': ([W.LPCWSTR, W.LPWSTR, C.c_void_p, C.c_void_p, W.BOOL, W.DWORD, C.c_void_p, W.LPCWSTR, C.POINTER(_StartupInfo), C.POINTER(_ProcessInfo)], W.BOOL),
            'VirtualAllocEx': ([W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD, W.DWORD], C.c_void_p),
            'VirtualFreeEx': ([W.HANDLE, C.c_void_p, C.c_size_t, W.DWORD], W.BOOL),
            'WriteProcessMemory': ([W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL),
            'ReadProcessMemory': ([W.HANDLE, C.c_void_p, C.c_void_p, C.c_size_t, C.POINTER(C.c_size_t)], W.BOOL),
            'CreateRemoteThread': ([W.HANDLE, C.c_void_p, C.c_size_t, C.c_void_p, C.c_void_p, W.DWORD, C.c_void_p], W.HANDLE),
            'GetExitCodeThread': ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
            'GetExitCodeProcess': ([W.HANDLE, C.POINTER(W.DWORD)], W.BOOL),
            'WaitForSingleObject': ([W.HANDLE, W.DWORD], W.DWORD),
            'ResumeThread': ([W.HANDLE], W.DWORD),
            'TerminateProcess': ([W.HANDLE, W.UINT], W.BOOL),
            'CloseHandle': ([W.HANDLE], W.BOOL),
            'GetModuleHandleW': ([W.LPCWSTR], W.HMODULE),
            'GetProcAddress': ([W.HMODULE, C.c_char_p], C.c_void_p),
            'GetModuleHandleExW': ([W.DWORD, C.c_void_p, C.POINTER(W.HMODULE)], W.BOOL),
            'GetModuleFileNameW': ([W.HMODULE, W.LPWSTR, W.DWORD], W.DWORD),
            'CreateToolhelp32Snapshot': ([W.DWORD, W.DWORD], W.HANDLE),
            'Module32FirstW': ([W.HANDLE, C.POINTER(_ModuleEntry)], W.BOOL),
            'Module32NextW': ([W.HANDLE, C.POINTER(_ModuleEntry)], W.BOOL),
            'K32GetMappedFileNameW': ([W.HANDLE, C.c_void_p, W.LPWSTR, W.DWORD], W.DWORD),
            'GetCurrentProcess': ([], W.HANDLE),
        }
        for name, (args, result) in signatures.items():
            function = getattr(self.k, name)
            function.argtypes, function.restype = args, result

    def create(self, executable, args, env):
        startup, info = _StartupInfo(), _ProcessInfo()
        startup.cb = C.sizeof(startup)
        command = C.create_unicode_buffer(subprocess.list2cmdline([str(executable), *args]))
        environment = C.create_unicode_buffer(_environment_block(env))
        if not self.k.CreateProcessW(str(executable), command, None, None, False, 0x4 | 0x400,
                                    environment, str(executable.parent), C.byref(startup), C.byref(info)):
            raise C.WinError(C.get_last_error())
        return info

    def loader_address(self, pid):
        """Resolve the address in the child, including a forwarded kernelbase export."""
        local = self.k.GetProcAddress(self.k.GetModuleHandleW('kernel32.dll'), b'LoadLibraryW')
        owner = W.HMODULE()
        if not local or not self.k.GetModuleHandleExW(0x4 | 0x2, local, C.byref(owner)):
            raise C.WinError(C.get_last_error())
        name = C.create_unicode_buffer(32768)
        if not self.k.GetModuleFileNameW(owner, name, len(name)):
            raise C.WinError(C.get_last_error())
        target = Path(name.value).name.casefold()
        snapshot = None
        for _ in range(8):
            snapshot = self.k.CreateToolhelp32Snapshot(0x8 | 0x10, pid)
            if snapshot != C.c_void_p(-1).value:
                break
            if C.get_last_error() != 24:  # ERROR_BAD_LENGTH: retry changing loader list.
                raise C.WinError(C.get_last_error())
            time.sleep(.025)
        if snapshot == C.c_void_p(-1).value:
            raise OSError('无法读取新进程模块列表')
        try:
            entry = _ModuleEntry()
            entry.size = C.sizeof(entry)
            valid = self.k.Module32FirstW(snapshot, C.byref(entry))
            while valid:
                if entry.name.casefold() == target:
                    return entry.base + local - owner.value
                valid = self.k.Module32NextW(snapshot, C.byref(entry))
        finally:
            self.close(snapshot)
        raise OSError('新进程尚未加载 Windows 启动模块')

    def initialize_loader(self, info):
        """Initialize Windows DLLs while the application's main thread stays suspended.

        A freshly suspended process has no usable Toolhelp module list. A thread
        that immediately exits triggers the normal Windows loader first. Only
        the already mapped ntdll function is used, after comparing both its
        mapped file identity and instruction bytes at that address in the child.
        """
        routine = self.k.GetProcAddress(self.k.GetModuleHandleW('ntdll.dll'), b'RtlExitUserThread')
        if not routine:
            raise C.WinError(C.get_last_error())
        local_name, remote_name = C.create_unicode_buffer(32768), C.create_unicode_buffer(32768)
        if not self.k.K32GetMappedFileNameW(self.k.GetCurrentProcess(), routine, local_name, len(local_name)):
            raise C.WinError(C.get_last_error())
        if not self.k.K32GetMappedFileNameW(info.process, routine, remote_name, len(remote_name)):
            raise C.WinError(C.get_last_error())
        if local_name.value.casefold() != remote_name.value.casefold():
            raise OSError('Windows 启动模块地址不一致，已停止加载')
        memory, count = C.create_string_buffer(32), C.c_size_t()
        if not self.k.ReadProcessMemory(info.process, routine, memory, 32, C.byref(count)):
            raise C.WinError(C.get_last_error())
        if count.value != 32 or memory.raw != C.string_at(routine, 32):
            raise OSError('Windows 启动模块指纹不一致，已停止加载')
        thread = self.k.CreateRemoteThread(info.process, None, 0, routine, None, 0, None)
        if not thread:
            raise C.WinError(C.get_last_error())
        try:
            if self.k.WaitForSingleObject(thread, 15000) != 0:
                raise OSError('Windows 进程初始化超时')
        finally:
            self.close(thread)

    def inject(self, info, dll):
        self.initialize_loader(info)
        data = (str(dll) + '\x00').encode('utf-16-le')
        remote = self.k.VirtualAllocEx(info.process, None, len(data), 0x1000 | 0x2000, 0x04)
        if not remote:
            raise C.WinError(C.get_last_error())
        thread = None
        completed = False
        try:
            written = C.c_size_t()
            buffer = C.create_string_buffer(data)
            if not self.k.WriteProcessMemory(info.process, remote, buffer, len(data), C.byref(written)) or written.value != len(data):
                raise C.WinError(C.get_last_error())
            loader = self.loader_address(info.pid)
            thread = self.k.CreateRemoteThread(info.process, None, 0, loader, remote, 0, None)
            if not thread:
                raise C.WinError(C.get_last_error())
            status = self.k.WaitForSingleObject(thread, 15000)
            if status != 0:
                raise OSError('接收器启动加载超时或失败')
            completed = True
            code = W.DWORD()
            if not self.k.GetExitCodeThread(thread, C.byref(code)) or not code.value:
                raise OSError('接收器启动 DLL 加载失败')
        finally:
            if thread:
                self.close(thread)
            # A timed-out thread might still reference its buffer. The outer
            # transaction terminates only this newly created child on failure.
            if not thread or completed:
                self.k.VirtualFreeEx(info.process, remote, 0, 0x8000)

    def resume(self, info):
        if self.k.ResumeThread(info.thread) == 0xffffffff:
            raise C.WinError(C.get_last_error())
        # Detect immediate fail-closed native fingerprint/initialization errors.
        wait = self.k.WaitForSingleObject(info.process, 750)
        if wait == 0:
            code = W.DWORD()
            self.k.GetExitCodeProcess(info.process, C.byref(code))
            raise OSError(f'QQ 启动提前退出（{code.value:#x}）')
        if wait != 258:
            raise C.WinError(C.get_last_error())

    def terminate(self, info):
        if self.k.TerminateProcess(info.process, 0x51410003):
            self.k.WaitForSingleObject(info.process, 3000)

    def close(self, handle):
        self.k.CloseHandle(handle)


def _start_injected(backend, executable, args, env, dll):
    info = backend.create(executable, args, env)
    try:
        backend.inject(info, dll)
        backend.resume(info)
        return {'started': True, 'pid': info.pid, 'native_boot': 'independent', 'build': SUPPORTED_BUILD}
    except BaseException:
        backend.terminate(info)
        raise
    finally:
        backend.close(info.thread)
        backend.close(info.process)


def launch_qq(qq_root, app_dir, shadow_package, bootstrap, *, dll_path=None, extra_args=None):
    if os.name != 'nt' or struct.calcsize('P') != 8:
        raise ValueError('独立 QQ 接入需要 64 位 Windows 和 64 位 Python')
    executable, module = validate_build(qq_root, app_dir)
    base = Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))
    dll = Path(dll_path or base / 'native' / 'qq_activity_boot.dll').resolve()
    if not dll.is_file():
        raise ValueError('缺少自研 QQ 启动 DLL，请先构建 native/qq_activity_boot.dll')
    env = build_environment(app_dir, module, shadow_package, bootstrap)
    return _start_injected(_Windows(), executable, list(extra_args or []), env, dll)
