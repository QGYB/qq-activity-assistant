# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Authenticated local transport for the independently written QQ receiver."""
from __future__ import annotations

import collections
import hmac
import hashlib
import json
import math
import secrets
import threading
import time
from datetime import UTC, datetime
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path

from .parser import parse_notice


class BridgeServer:
    def __init__(self, store, data_dir, on_event=None):
        self.store, self.data_dir, self.on_event = store, Path(data_dir), on_event
        self.data_dir.mkdir(parents=True, exist_ok=True)
        self._token = secrets.token_urlsafe(36)
        self._groups = [str(x) for x in store.get_setting('group_ids', [])]
        self._lock = threading.RLock()
        self._server = None
        self._thread = None
        self._last_heartbeat = 0.0
        self._last_message = None
        self._receiver_state = 'disconnected'
        self._callback_count = 0
        self._history_state = 'idle'
        self._history_count = 0
        self._seen = collections.OrderedDict()

    def start(self):
        if self._server is not None:
            return
        owner = self

        class Handler(BaseHTTPRequestHandler):
            def log_message(self, *_args):
                pass

            def reply(self, code, payload):
                data = json.dumps(payload, ensure_ascii=False).encode('utf-8')
                self.send_response(code)
                self.send_header('Content-Type', 'application/json; charset=utf-8')
                self.send_header('Content-Length', str(len(data)))
                self.send_header('Cache-Control', 'no-store')
                self.end_headers()
                self.wfile.write(data)

            def do_GET(self):
                if self.path != '/health':
                    self.reply(404, {'error': 'not_found'})
                    return
                state = owner.status()
                self.reply(200, {k: state[k] for k in ('connected', 'receiver_state', 'last_heartbeat', 'last_message')})

            def do_POST(self):
                if self.path != '/events':
                    self.reply(404, {'error': 'not_found'})
                    return
                if not hmac.compare_digest(self.headers.get('X-Bridge-Token', ''), owner._token):
                    self.reply(401, {'error': 'unauthorized'})
                    return
                try:
                    length = int(self.headers.get('Content-Length', '0'))
                    if not 0 < length <= 512 * 1024:
                        self.reply(413, {'error': 'invalid_size'})
                        return
                    self.connection.settimeout(3)
                    event = json.loads(self.rfile.read(length))
                    if not isinstance(event, dict):
                        raise ValueError('object required')
                    result = owner._handle(event)
                except (ValueError, TypeError, UnicodeError, TimeoutError):
                    self.reply(400, {'error': 'invalid_event'})
                    return
                except Exception:
                    self.reply(500, {'error': 'processing_failed'})
                    return
                self.reply(200, {'result': result})

        self._server = ThreadingHTTPServer(('127.0.0.1', 0), Handler)
        self._server.daemon_threads = True
        self._write_config()
        self._thread = threading.Thread(target=self._server.serve_forever, name='qq-local-bridge', daemon=True)
        self._thread.start()

    def _write_config(self):
        if not self._server:
            return
        data = {'host': '127.0.0.1', 'port': self._server.server_port, 'token': self._token, 'groups': self._groups, 'poll_seconds': 15}
        path = self.data_dir / 'bridge.json'
        staging = path.with_suffix('.tmp')
        staging.write_text(json.dumps(data, ensure_ascii=False), encoding='utf-8')
        staging.replace(path)

    def set_groups(self, groups):
        validated = list(dict.fromkeys(str(x).strip() for x in groups if str(x).strip()))
        if any(not x.isascii() or not x.isdigit() or not 5 <= len(x) <= 12 for x in validated):
            raise ValueError('群号须为 5–12 位数字')
        with self._lock:
            self._groups = validated
            self.store.set_setting('group_ids', validated)
            self._write_config()

    def _handle(self, event):
        kind = event.get('type')
        if kind in ('status', 'heartbeat'):
            with self._lock:
                self._last_heartbeat = time.time()
                state = str(event.get('state', 'unknown'))
                self._receiver_state = state if state in ('waiting_native', 'native_loaded', 'listening', 'unsupported') else 'unknown'
                if kind == 'heartbeat':
                    self._callback_count = max(0, int(event.get('callback_count', 0)))
                    self._history_state = str(event.get('history_state', 'idle'))
                    self._history_count = max(0, int(event.get('history_count', 0)))
            self._notify({'type': 'receiver_status'})
            return 'accepted'
        if kind != 'message':
            raise ValueError('unsupported event')
        group, text, message_id = str(event.get('group_id', '')), event.get('text'), str(event.get('message_id', ''))
        if group not in self._groups:
            return 'ignored'
        if not isinstance(text, str) or not 0 < len(text) <= 65536 or not message_id or len(message_id) > 200:
            raise ValueError('invalid message')
        stamp = float(event.get('time', time.time()))
        if not math.isfinite(stamp) or stamp < 946684800 or stamp > time.time() + 86400:
            raise ValueError('invalid timestamp')
        key = (group, message_id)
        with self._lock:
            if key in self._seen:
                return 'duplicate'
            activity = parse_notice(text, observed_at=datetime.fromtimestamp(stamp, UTC))
            activity_id = None
            notification_type = 'message'
            if activity:
                if event.get('image_only'):
                    activity['id'] = hashlib.sha256((group + '|' + message_id).encode()).hexdigest()[:24]
                activity['source_group_id'] = group
                activity['source_message_id'] = message_id
                activity['source_delivery'] = 'history' if event.get('delivery') == 'history' else 'live'
                saved_id = self.store.upsert_activity(activity)
                if activity.get('notice_type') == 'activity_change' and not self.store.get_activity(saved_id):
                    notification_type = 'activity_change'
                else:
                    activity_id = saved_id
                    notification_type = 'activity'
            self._seen[key] = True
            if len(self._seen) > 5000:
                self._seen.popitem(last=False)
            self._last_message = datetime.now(UTC).isoformat()
        self._notify({'type': notification_type, 'activity_id': activity_id})
        return 'accepted'

    def _notify(self, event):
        if self.on_event:
            try:
                self.on_event(event)
            except Exception:
                pass

    def status(self):
        with self._lock:
            fresh = time.time() - self._last_heartbeat < 20
            return {'connected': bool(fresh and self._receiver_state == 'listening'), 'last_heartbeat': datetime.fromtimestamp(self._last_heartbeat, UTC).isoformat() if self._last_heartbeat else None, 'last_message': self._last_message, 'receiver_state': self._receiver_state if fresh else 'disconnected', 'callback_count': self._callback_count, 'history_state': self._history_state, 'history_count': self._history_count, 'port': self._server.server_port if self._server else None, 'groups': list(self._groups)}

    def stop(self):
        server, self._server = self._server, None
        if server:
            server.shutdown()
            server.server_close()
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=2)
