# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Configure and launch our own read-only QQ receiver without editing QQ files."""
from __future__ import annotations

import csv
import ctypes
import hashlib
import json
import os
from pathlib import Path
import shutil
import subprocess
import sys
import threading

_SETUP_LOCK = threading.RLock()


def _hash(data):
    return hashlib.sha256(data).hexdigest()


def _default_data():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'QQActivityAssistant'


def _assets():
    return Path(getattr(sys, '_MEIPASS', Path(__file__).resolve().parent.parent))


def _app_dir(qq_root):
    import re
    root = Path(qq_root).resolve()
    versions = root / 'versions'
    candidates = []
    if versions.is_dir():
        ordered = sorted(versions.iterdir(), key=lambda p: tuple(int(x) for x in re.findall(r'\d+', p.name)), reverse=True)
        candidates += [p / 'resources' / 'app' for p in ordered if p.is_dir()]
    candidates.append(root / 'resources' / 'app')
    for app in candidates:
        if (app / 'package.json').is_file():
            package = json.loads((app / 'package.json').read_text(encoding='utf-8-sig'))
            if package.get('name') == 'qq-chat' and isinstance(package.get('main'), str):
                return app
    raise ValueError('没有找到 QQ NT，请选择包含 QQ.exe 的安装目录')


def discover_qq_root():
    guesses = [Path(os.environ.get('LOCALAPPDATA', '')) / 'Programs/QQ', Path('C:/Program Files/Tencent/QQNT'), Path('C:/Program Files (x86)/Tencent/QQNT')]
    try:
        command = "[Console]::OutputEncoding=[System.Text.UTF8Encoding]::new(); Get-Process -Name QQ -ErrorAction SilentlyContinue | Select-Object -First 1 -ExpandProperty Path"
        output = subprocess.check_output(['powershell', '-NoProfile', '-Command', command], creationflags=getattr(subprocess, 'CREATE_NO_WINDOW', 0), timeout=5).decode('utf-8-sig').strip()
        if output:
            guesses.insert(0, Path(output).parent)
    except (OSError, ValueError, subprocess.SubprocessError):
        pass
    for root in guesses:
        try:
            _app_dir(root)
            return root
        except (OSError, ValueError):
            continue
    return None


def _write(path, data):
    staging = path.with_name(path.name + '.tmp')
    staging.write_bytes(data)
    staging.replace(path)


def install_receiver(qq_root, data_dir):
    from .native_boot import validate_build
    with _SETUP_LOCK:
        root, data = Path(qq_root).resolve(), Path(data_dir).resolve()
        app = _app_dir(root)
        validate_build(root, app)  # Check compatibility before changing any configuration.
        original = (app / 'package.json').read_bytes()
        package = json.loads(original.decode('utf-8-sig'))
        original_main = (app / package['main']).resolve()
        if app.resolve() not in original_main.parents:
            raise ValueError('QQ 原入口位于 app 目录之外，无法自动配置')
        receiver = data / 'receiver'
        receiver.mkdir(parents=True, exist_ok=True)
        source_dll = _assets() / 'native/qq_activity_boot.dll'
        if not source_dll.is_file():
            raise ValueError('缺少自研启动组件，请使用完整程序目录')
        dll_bytes = source_dll.read_bytes()
        dll = receiver / ('qq-activity-' + _hash(dll_bytes)[:16] + '.dll')
        if not dll.exists():
            _write(dll, dll_bytes)
        elif dll.read_bytes() != dll_bytes:
            raise ValueError('本机接收组件校验失败，请重新配置')
        script = receiver / 'preload.cjs'
        script_bytes = (_assets() / 'receiver/preload.cjs').read_bytes()
        if not script.exists() or script.read_bytes() != script_bytes:
            _write(script, script_bytes)
        backup = receiver / ('original-package-' + _hash(original)[:16] + '.json')
        if not backup.exists():
            _write(backup, original)
        original_entry = package['main']
        package['main'] = './qq-activity-entry.cjs'
        shadow = receiver / 'shadow-package.json'
        _write(shadow, (json.dumps(package, ensure_ascii=False, indent=2) + '\n').encode('utf-8'))
        bootstrap = receiver / 'entry.cjs'
        code = "'use strict';\n"
        code += f"try{{require({json.dumps(str(script))}).start({json.dumps(str(data / 'bridge.json'))});}}catch(e){{console.error('Activity receiver init failed:',e.name);}}\n"
        code += f"require({json.dumps(str(original_main))});\n"
        code += f"setImmediate(()=>{{try{{global.launcher.installPathPkgJson.main={json.dumps(original_entry)};}}catch{{}}}});\n"
        _write(bootstrap, code.encode('utf-8'))
        config = {'qq_root': str(root), 'app_dir': str(app), 'shadow_package': str(shadow), 'bootstrap': str(bootstrap), 'dll': str(dll), 'manifest_sha256': _hash(original), 'dll_sha256': _hash(dll_bytes), 'native_boot': 'independent'}
        _write(receiver / 'launch.json', json.dumps(config, ensure_ascii=False, indent=2).encode('utf-8'))
        return {'installed': True, 'restart_required': True, 'native_boot': 'independent', 'message': '自研接收启动器已配置；点击“重启 QQ”加载。以后可通过本工具启动 QQ。'}


def restore_receiver(qq_root, data_dir=None):
    with _SETUP_LOCK:
        root, data = Path(qq_root).resolve(), Path(data_dir or _default_data()).resolve()
        config = data / 'receiver/launch.json'
        if config.exists():
            saved = json.loads(config.read_text(encoding='utf-8'))
            if Path(saved.get('qq_root', '')).resolve() != root:
                raise ValueError('接收配置属于另一个 QQ 安装目录，未清除')
            config.unlink()
            return {'restored': True, 'restart_required': True, 'message': '已清除接收启动配置。QQ 原文件一直保持原样；下次普通启动即可恢复。'}
        return {'restored': False, 'message': '未配置自研启动器，QQ 原文件保持原样。'}


def _stop_matching_qq(executable):
    """Stop only this exact executable after an explicit restart action."""
    k = ctypes.WinDLL('kernel32', use_last_error=True)
    k.OpenProcess.argtypes = [ctypes.c_uint32, ctypes.c_int, ctypes.c_uint32]
    k.OpenProcess.restype = ctypes.c_void_p
    k.QueryFullProcessImageNameW.argtypes = [ctypes.c_void_p, ctypes.c_uint32, ctypes.c_wchar_p, ctypes.POINTER(ctypes.c_uint32)]
    k.TerminateProcess.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.WaitForSingleObject.argtypes = [ctypes.c_void_p, ctypes.c_uint32]
    k.CloseHandle.argtypes = [ctypes.c_void_p]
    rows = csv.reader(subprocess.check_output(['tasklist', '/FI', 'IMAGENAME eq QQ.exe', '/FO', 'CSV', '/NH'], creationflags=subprocess.CREATE_NO_WINDOW).decode(errors='replace').splitlines())
    handles = []
    try:
        for row in rows:
            if len(row) < 2 or not row[1].isdigit():
                continue
            h = k.OpenProcess(0x100000 | 0x1000 | 1, False, int(row[1]))
            if not h:
                continue
            buf, size = ctypes.create_unicode_buffer(32768), ctypes.c_uint32(32768)
            if k.QueryFullProcessImageNameW(h, 0, buf, ctypes.byref(size)) and Path(buf.value).resolve() == executable:
                handles.append(h)
            else:
                k.CloseHandle(h)
        for h in handles:
            if k.WaitForSingleObject(h, 0) == 0:
                continue
            if not k.TerminateProcess(h, 0):
                if k.WaitForSingleObject(h, 0) == 0:
                    continue
                raise OSError('QQ 无法关闭，请先从托盘正常退出')
            k.WaitForSingleObject(h, 3000)
    finally:
        for h in handles:
            k.CloseHandle(h)


def restart_qq(qq_root, data_dir=None):
    root, data = Path(qq_root).resolve(), Path(data_dir or _default_data()).resolve()
    executable = root / 'QQ.exe'
    if os.name != 'nt' or not executable.is_file():
        raise ValueError('请在 Windows 中选择包含 QQ.exe 的有效目录')
    with _SETUP_LOCK:
        config_path = data / 'receiver/launch.json'
        config = None
        if config_path.exists():
            from .native_boot import validate_build, build_environment
            config = json.loads(config_path.read_text(encoding='utf-8'))
            if Path(config['qq_root']).resolve() != root:
                raise ValueError('启动配置属于另一个 QQ 目录，请重新配置')
            app = Path(config['app_dir'])
            _, module = validate_build(root, app)
            if _hash((app / 'package.json').read_bytes()) != config['manifest_sha256'] or _hash(Path(config['dll']).read_bytes()) != config['dll_sha256']:
                raise ValueError('QQ 或接收组件发生变化，请重新配置；当前 QQ 未关闭')
            build_environment(app, module, config['shadow_package'], config['bootstrap'])
        _stop_matching_qq(executable)
        if config:
            from .native_boot import launch_qq
            try:
                return launch_qq(root, config['app_dir'], config['shadow_package'], config['bootstrap'], dll_path=config['dll'])
            except Exception:
                # Preserve ordinary QQ usage if the new independent startup fails.
                subprocess.Popen([str(executable)], cwd=str(root), creationflags=subprocess.CREATE_NO_WINDOW)
                raise
        process = subprocess.Popen([str(executable)], cwd=str(root), creationflags=subprocess.CREATE_NO_WINDOW)
        return {'started': True, 'pid': process.pid, 'message': 'QQ 已普通启动；接收器尚未配置。'}
