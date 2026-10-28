# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
import json
import tempfile
import unittest
import urllib.request
import urllib.error
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qq_activity.bridge import BridgeServer
from qq_activity.storage import Store


class BridgeTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.path = Path(self.tmp.name)
        self.store = Store(self.path / 'test.db')
        self.bridge = BridgeServer(self.store, self.path)
        self.bridge.start()
        self.bridge.set_groups(['100001234'])
        self.config = json.loads((self.path / 'bridge.json').read_text(encoding='utf-8'))

    def tearDown(self):
        self.bridge.stop()
        self.tmp.cleanup()

    def post(self, event, token=None):
        req = urllib.request.Request(f'http://127.0.0.1:{self.config["port"]}/events', data=json.dumps(event).encode(), headers={'Content-Type': 'application/json', 'X-Bridge-Token': token or self.config['token']})
        with urllib.request.urlopen(req, timeout=3) as r:
            return r.status, json.load(r)

    def test_bad_token_never_records_an_activity(self):
        with self.assertRaises(urllib.error.HTTPError) as cm:
            self.post({'type': 'message', 'group_id': '100001234', 'text': '活动'}, 'bad')
        self.assertEqual(cm.exception.code, 401)
        self.assertEqual(self.store.activities(), [])

    def test_other_group_is_ignored_and_health_hides_token(self):
        _, result = self.post({'type': 'message', 'group_id': '123456', 'text': '活动'})
        self.assertEqual(result['result'], 'ignored')
        with urllib.request.urlopen(f'http://127.0.0.1:{self.config["port"]}/health') as r:
            self.assertNotIn(self.config['token'], r.read().decode())

    def test_allowed_real_notice_is_parsed_once(self):
        event = {'type': 'message', 'group_id': '100001234', 'message_id': '1', 'time': 1793174400, 'text': '【文明岗活动】\n★活动时间：10.29周四14:00-16:00\n★活动链接：https://f.wps.cn/g/DemoCivilForm/\n每次活动的前一天16点开放表单填写，20点截止报名'}
        self.post(event)
        self.post(event)
        rows = self.store.activities()
        self.assertEqual(len(rows), 1)
        self.assertIn('16:00', rows[0]['opens_at'])

    def test_change_notices_are_stored_and_notify_linked_activity(self):
        events = []
        self.bridge.on_event = events.append
        stamp = datetime(2026, 10, 24, 10, tzinfo=timezone(timedelta(hours=8))).timestamp()
        change = {'type': 'message', 'group_id': '100001234', 'message_id': 'change', 'time': stamp, 'text': '明天天宫庄园活动取消'}
        self.post(change)
        self.assertEqual(self.store.activities(), [])
        self.assertEqual(len(self.store.activity_changes()), 1)
        self.assertEqual(events[-1]['type'], 'activity_change')
        self.assertIsNone(events[-1]['activity_id'])
        normal = dict(change, message_id='normal', text='天宫庄园志愿活动\n活动时间10月25日周日13:00-16:00\n报名活动前一天16点开放，20点截止\nhttps://f.wps.cn/g/example/')
        self.post(normal)
        activity = self.store.activities()[0]
        self.assertEqual(activity['lifecycle'], 'cancelled')
        self.assertEqual(events[-1]['activity_id'], activity['id'])
        self.post(dict(change, message_id='change-again'))
        self.assertEqual(events[-1]['type'], 'activity')
        self.assertEqual(events[-1]['activity_id'], activity['id'])
        self.assertEqual(len(self.store.activities()), 1)


if __name__ == '__main__':
    unittest.main()
