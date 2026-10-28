# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Visible desktop entry using the installed Python runtime, with crash details."""
from pathlib import Path
import os
import traceback


def launch():
    try:
        from qq_activity.__main__ import main
        return main()
    except Exception:
        directory = Path(os.environ.get('LOCALAPPDATA', Path.home())) / 'QQActivityAssistant'
        log = directory / 'last_startup_error.log'
        try:
            directory.mkdir(parents=True, exist_ok=True)
            log.write_text(traceback.format_exc(), encoding='utf-8')
            detail = f'详细错误已保存到：\n{log}'
        except OSError:
            detail = '错误记录无法保存，请检查本机资料目录权限。'
        import tkinter as tk
        from tkinter import messagebox
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror('群活动助手启动失败', '程序未能启动。\n\n' + detail, parent=root)
        root.destroy()
        return 1


if __name__ == '__main__':
    raise SystemExit(launch())
