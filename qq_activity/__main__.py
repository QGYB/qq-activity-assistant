# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
from __future__ import annotations

import argparse
import os
import sys
from pathlib import Path


def default_data_dir():
    return Path(os.environ.get('LOCALAPPDATA', Path.home() / '.local/share')) / 'QQActivityAssistant'


def main():
    from .app_identity import set_process_identity
    set_process_identity()
    parser = argparse.ArgumentParser(description='QQ 群志愿活动助手')
    parser.add_argument('--data-dir', type=Path, default=default_data_dir(), help='本机资料与浏览器登录状态目录')
    args = parser.parse_args()
    args.data_dir.mkdir(parents=True, exist_ok=True)
    lock = (args.data_dir / 'app.lock').open('a+b')
    try:
        if lock.seek(0, 2) == 0:
            lock.write(b'0')
            lock.flush()
        lock.seek(0)
        if sys.platform == 'win32':
            import msvcrt
            msvcrt.locking(lock.fileno(), msvcrt.LK_NBLCK, 1)
        else:
            import fcntl
            fcntl.flock(lock.fileno(), fcntl.LOCK_EX | fcntl.LOCK_NB)
    except (OSError, IOError):
        lock.close()
        import tkinter.messagebox
        tkinter.messagebox.showinfo('活动助手', '这个资料目录的活动助手已在运行，请使用已打开的窗口。')
        return 1
    from .storage import Store
    from .bridge import BridgeServer
    from .booking import BrowserWorker
    from .ui import MainWindow

    store = Store(args.data_dir / 'activities.db')
    bridge = BridgeServer(store, args.data_dir)
    browser = BrowserWorker(store, args.data_dir)
    window = MainWindow(store, browser, bridge, args.data_dir)
    bridge.on_event, browser.on_status = window.post_event, window.post_status
    try:
        bridge.start()
        browser.start()
        window.run()
    finally:
        # Tk is already closed: allow the owner to release the persistent Edge
        # profile before releasing the app lock or returning to the caller.
        browser.stop(timeout=45)
        bridge.stop()
        lock.close()
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
