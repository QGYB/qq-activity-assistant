# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
# -*- mode: python ; coding: utf-8 -*-


a = Analysis(
    ['app.py'],
    pathex=[],
    binaries=[('native/qq_activity_boot.dll', 'native')],
    datas=[('receiver/preload.cjs', 'receiver'), ('qq_activity/assets', 'qq_activity/assets'),
           ('LICENSE', '.'), ('LICENSES', 'LICENSES'), ('README.md', '.'),
           ('THIRD_PARTY_NOTICES.md', '.'), ('native/MinGW-w64-LICENSE.txt', 'native')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name='群活动助手',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,
    console=False,
    icon='qq_activity/assets/app.ico',
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
)
coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    upx_exclude=[],
    name='群活动助手',
)
