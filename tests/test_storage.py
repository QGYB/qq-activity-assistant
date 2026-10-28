# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
import tempfile
import json
import sqlite3
import unittest
from concurrent.futures import ThreadPoolExecutor
from datetime import datetime, timedelta, timezone
from pathlib import Path

from qq_activity.parser import parse_notice
from qq_activity.storage import Store


CN = timezone(timedelta(hours=8))
NOW = datetime(2026, 10, 28, 17, tzinfo=CN)
TEXT = '文明岗活动\n活动时间10.29周四14:00-16:00\n前一天16点开放，20点截止\nhttps://f.wps.cn/g/abc/'


class StorageTests(unittest.TestCase):
    def setUp(self):
        self.temp = tempfile.TemporaryDirectory()
        self.path = Path(self.temp.name) / 'state.sqlite'
        self.s = Store(self.path)
        self.a = parse_notice(TEXT, NOW)
        self.s.upsert_activity(self.a)
        self.p = self.s.save_profile(dict(name='张三', major='电气', class_name='01', student_id='00123', phone='013812345678', email='a@example.com', reward='credit'))
        self.q = self.s.save_profile(dict(name='李四', major='机械', class_name='02', student_id='00002', phone='013812345679', email='b@example.com', reward='cloud'))

    def tearDown(self):
        self.temp.cleanup()

    def test_profile_roundtrip_and_only_selected_people_queue(self):
        self.assertEqual(self.s.get_profile(self.p)['student_id'], '00123')
        self.assertEqual(self.s.queue(self.a['id'], []), [])
        ids = self.s.queue(self.a['id'], [self.p], {self.p: 'cloud'})
        self.assertEqual(len(ids), 1)
        self.assertEqual(self.s.get_job(ids[0])['reward'], 'cloud')
        self.assertEqual([j['profile_id'] for j in self.s.jobs()], [self.p])
        self.assertEqual(len(self.s.profiles()), 2)

    def test_success_and_uncertain_cannot_duplicate(self):
        for status, profile in [('success', self.p), ('uncertain', self.q)]:
            jid = self.s.queue(self.a['id'], [profile])[0]
            self.s.update_job(jid, 'running')
            self.s.update_job(jid, status)
            self.s.queue(self.a['id'], [profile])
            self.assertEqual(self.s.get_job(jid)['status'], status)
        self.assertEqual(len(self.s.jobs()), 2)

    def test_due_only_during_window_and_expire(self):
        jid = self.s.queue(self.a['id'], [self.p])[0]
        self.assertEqual(self.s.due_jobs(NOW.replace(hour=15)), [])
        self.assertEqual(self.s.due_jobs(NOW)[0]['id'], jid)
        self.assertEqual(self.s.due_jobs(NOW.replace(hour=20)), [])
        self.assertEqual(self.s.get_job(jid)['status'], 'expired')

    def test_crash_recovery_only_on_startup(self):
        jid = self.s.queue(self.a['id'], [self.p])[0]
        self.s.update_job(jid, 'running')
        self.s.activities()
        self.s.jobs()
        self.s.due_jobs(NOW)
        self.assertEqual(self.s.get_job(jid)['status'], 'running')
        reopened = Store(self.path)
        self.assertEqual(reopened.get_job(jid)['status'], 'uncertain')

    def test_queue_rejects_incomplete_activity_atomically(self):
        self.s.update_activity(self.a['id'], {'issues': ['待核对']})
        with self.assertRaises(ValueError):
            self.s.queue(self.a['id'], [self.p])
        self.assertEqual(self.s.jobs(), [])

    def test_bad_profile_selection_does_not_partially_queue(self):
        with self.assertRaises(ValueError):
            self.s.queue(self.a['id'], [self.p, 'missing'])
        self.assertEqual(self.s.jobs(), [])

    def test_duplicate_notices_do_not_retarget_queued_jobs(self):
        jid = self.s.queue(self.a['id'], [self.p])[0]
        changed = dict(self.a, form_url='https://f.wps.cn/g/other/')
        self.s.upsert_activity(changed)
        saved = self.s.get_activity(self.a['id'])
        self.assertEqual(saved['form_url'], self.a['form_url'])
        self.assertEqual(saved['conflicting_notice']['form_url'], changed['form_url'])
        self.assertTrue(saved['issues'])
        self.assertEqual(self.s.get_job(jid)['status'], 'attention')
        self.assertEqual(self.s.due_jobs(NOW), [])
        self.assertFalse(self.s.claim_job(jid, NOW))
        self.s.upsert_activity(self.a)
        self.assertEqual(self.s.get_activity(self.a['id'])['conflicting_notice']['form_url'], changed['form_url'])
        self.assertTrue(self.s.get_activity(self.a['id'])['issues'])
        self.assertEqual(len(self.s.activities()), 1)
        self.s.update_activity(self.a['id'], {'category': '其他'})

    def test_same_notice_keeps_read_only_form_fields_and_counts(self):
        fields = {'major': {'title':'专业','options':['真实专业'],'kind':'choice'}}
        constraints = {'fill_limit':10,'count':3,'checked_at':NOW.isoformat()}
        self.s.update_activity(self.a['id'], {'profile_fields':fields, 'form_constraints':constraints})
        self.s.upsert_activity(self.a)
        saved = self.s.get_activity(self.a['id'])
        self.assertEqual(saved['profile_fields'],fields)
        self.assertEqual(saved['form_constraints'],constraints)

    def test_conflicting_schedule_pauses_queue_and_preserves_historical_targets(self):
        for status in ('running', 'success', 'uncertain'):
            with self.subTest(status=status):
                activity = dict(self.a, id='historical-' + status)
                self.s.upsert_activity(activity)
                old = self.s.queue(activity['id'], [self.p])[0]
                pending = self.s.queue(activity['id'], [self.q])[0]
                self.s.update_job(old, 'running')
                self.s.update_job(old, status)
                incoming = dict(activity, opens_at='2026-10-28T18:00:00+08:00', raw_text='更新后的通知原文')
                self.s.upsert_activity(incoming)
                saved = self.s.get_activity(activity['id'])
                self.assertEqual(saved['opens_at'], activity['opens_at'])
                self.assertEqual(saved['raw_text'], activity['raw_text'])
                self.assertEqual(saved['conflicting_notice']['raw_text'], '更新后的通知原文')
                self.assertEqual(self.s.get_job(old)['status'], status)
                self.assertEqual(self.s.get_job(pending)['status'], 'attention')

    def test_paused_conflict_can_be_explicitly_resolved_then_requeued(self):
        jid = self.s.queue(self.a['id'], [self.p])[0]
        changed = dict(self.a, opens_at='2026-10-28T18:00:00+08:00')
        self.s.upsert_activity(changed)
        self.s.update_activity(self.a['id'], {'opens_at': changed['opens_at'], 'issues': []})
        self.assertNotIn('conflicting_notice', self.s.get_activity(self.a['id']))
        self.s.queue(self.a['id'], [self.p])
        self.assertEqual(self.s.get_job(jid)['status'], 'queued')
        self.assertFalse(self.s.claim_job(jid, NOW))

    def test_profile_delete_protected_and_cancelled_job_can_requeue(self):
        jid = self.s.queue(self.a['id'], [self.p])[0]
        with self.assertRaises(ValueError):
            self.s.delete_profile(self.p)
        self.s.cancel_job(jid)
        self.assertEqual(self.s.get_job(jid)['status'], 'cancelled')
        self.s.queue(self.a['id'], [self.p])
        self.assertEqual(len(self.s.jobs()), 1)
        self.assertEqual(self.s.get_job(jid)['status'], 'queued')
        self.s.cancel_job(jid)
        self.s.delete_profile(self.p)
        self.assertIsNone(self.s.get_profile(self.p))

    def test_submitted_identity_survives_profile_edits_and_deletion(self):
        for status, identifier in [('success', self.p), ('uncertain', self.q)]:
            with self.subTest(status=status):
                jid = self.s.queue(self.a['id'], [identifier])[0]
                self.s.update_job(jid, status)
                profile = self.s.get_profile(identifier)
                with self.assertRaises(ValueError):
                    self.s.delete_profile(identifier)
                with self.assertRaises(ValueError):
                    self.s.save_profile({**profile, 'student_id': 'different-student'})
                self.assertEqual(self.s.get_profile(identifier)['student_id'], profile['student_id'])
                self.s.save_profile({**profile, 'email': 'updated@example.com'})
                self.assertEqual(self.s.get_profile(identifier)['email'], 'updated@example.com')

    def test_cross_thread_connections_and_settings_persist(self):
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.s.queue(self.a['id'], [self.p]), range(12)))
        self.assertEqual(len(self.s.jobs()), 1)
        self.s.set_setting('groups', ['123', '456'])
        self.assertEqual(Store(self.path).get_setting('groups'), ['123', '456'])
        self.assertEqual(self.s.get_setting('absent', 'fallback'), 'fallback')

    def test_atomic_claim_only_one_worker_wins(self):
        jid = self.s.queue(self.a['id'], [self.p])[0]
        with ThreadPoolExecutor(max_workers=4) as pool:
            results = list(pool.map(lambda _: self.s.claim_job(jid, NOW), range(12)))
        self.assertEqual(results.count(True), 1)
        self.assertEqual(self.s.get_job(jid)['status'], 'running')

    def test_claim_refuses_before_opening_and_after_deadline(self):
        jid = self.s.queue(self.a['id'], [self.p])[0]
        self.assertFalse(self.s.claim_job(jid, NOW.replace(hour=15)))
        self.assertFalse(self.s.claim_job(jid, NOW.replace(hour=20)))
        self.assertEqual(self.s.get_job(jid)['status'], 'expired')

    def _change(self, text='明天天宫庄园活动取消', group='10001'):
        notice = parse_notice(text, datetime(2026, 10, 24, 10, tzinfo=CN))
        notice['source_group_id'] = group
        return notice

    def _session(self, day=25, group='10001', suffix='a'):
        activity = parse_notice(TEXT.replace('文明岗', '天宫庄园').replace('10.29周四', f'10.{day} ').replace('/abc/', '/' + suffix + '/'), NOW)
        activity['source_group_id'] = group
        self.s.upsert_activity(activity)
        return activity

    def test_unique_change_links_original_and_stops_pending_without_extra_activity(self):
        a = self._session()
        pending = self.s.queue(a['id'], [self.p])[0]
        running = self.s.queue(a['id'], [self.q])[0]
        self.s.update_job(running, 'running')
        change = self._change()
        self.assertEqual(self.s.upsert_activity(change), a['id'])
        saved = self.s.get_activity(a['id'])
        self.assertEqual(saved['lifecycle'], 'cancelled')
        self.assertEqual(saved['change_notice']['raw_text'], change['raw_text'])
        self.assertEqual(saved['change_notice']['observed_at'], change['observed_at'])
        self.assertTrue(saved['change_notice']['notice_id'])
        self.assertEqual(self.s.get_job(pending)['status'], 'cancelled')
        self.assertEqual(self.s.get_job(running)['status'], 'running')
        self.assertEqual(len(self.s.activities()), 2)
        self.assertEqual(self.s.activity_changes()[0]['matched_activity_id'], a['id'])
        self.assertEqual(self.s.activity_changes()[0]['match_state'], 'matched')
        self.s.upsert_activity(a)
        self.s.upsert_activity(change)
        self.assertEqual(self.s.get_activity(a['id'])['lifecycle'], 'cancelled')
        self.assertEqual(len(self.s.activity_changes()), 1)
        self.assertEqual(len(self.s.get_activity(a['id'])['change_notices']), 1)
        with self.assertRaises(ValueError):
            self.s.queue(a['id'], [self.p])
        # Clearing visible issues must not bypass lifecycle guards.
        self.s.update_activity(a['id'], {'issues': []})
        with self.assertRaises(ValueError):
            self.s.queue(a['id'], [self.p])
        with self.s._db() as db:
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (pending,))
        self.assertFalse(self.s.claim_job(pending, NOW))
        with self.s._db() as db:
            db.execute("UPDATE jobs SET status='queued' WHERE id=?", (pending,))
        self.assertEqual(self.s.due_jobs(NOW), [])
        self.assertEqual(self.s.get_job(pending)['status'], 'cancelled')

    def test_ambiguous_change_never_cancels_multiple_sessions(self):
        a, b = self._session(suffix='a'), self._session(suffix='b')
        self.s.upsert_activity(self._change())
        self.assertIsNone(self.s.get_activity(a['id']).get('lifecycle'))
        self.assertIsNone(self.s.get_activity(b['id']).get('lifecycle'))
        self.assertEqual(self.s.activity_changes()[0]['match_state'], 'ambiguous')

    def test_later_second_session_withdraws_automatic_unique_match(self):
        a = self._session(suffix='a')
        self.s.upsert_activity(self._change())
        self.assertEqual(self.s.get_activity(a['id'])['lifecycle'], 'cancelled')
        b = self._session(suffix='b')
        self.assertIsNone(self.s.get_activity(a['id']).get('lifecycle'))
        self.assertIsNone(self.s.get_activity(b['id']).get('lifecycle'))
        self.assertEqual(self.s.activity_changes()[0]['match_state'], 'ambiguous')

    def test_shared_form_in_different_groups_remains_distinct_for_change_matching(self):
        a = self._session()
        duplicate = dict(a, source_group_id='20002')
        second_id = self.s.upsert_activity(duplicate)
        self.assertNotEqual(second_id, a['id'])
        self.s.upsert_activity(self._change(group='20002'))
        self.assertEqual(self.s.get_activity(second_id)['lifecycle'], 'cancelled')
        self.assertIsNone(self.s.get_activity(a['id']).get('lifecycle'))

    def test_wrong_date_or_group_does_not_cancel_and_late_history_can_match(self):
        wrong_day = self._session(27)
        other_group = self._session(25, group='20002')
        self.s.upsert_activity(self._change())
        self.assertIsNone(self.s.get_activity(wrong_day['id']).get('lifecycle'))
        self.assertIsNone(self.s.get_activity(other_group['id']).get('lifecycle'))
        self.assertEqual(self.s.activity_changes()[0]['match_state'], 'unmatched')
        correct = self._session(25, suffix='correct')
        self.assertEqual(self.s.get_activity(correct['id'])['lifecycle'], 'cancelled')
        self.assertEqual(self.s.activity_changes()[0]['matched_activity_id'], correct['id'])

    def test_incomplete_multi_date_and_multi_session_activity_never_auto_match(self):
        a = self._session()
        self.s.upsert_activity(self._change('天宫庄园活动取消'))
        self.s.upsert_activity(self._change('10月25日、10月27日天宫庄园活动取消'))
        self.assertIsNone(self.s.get_activity(a['id']).get('lifecycle'))
        self.assertTrue(all(c['matched_activity_id'] is None for c in self.s.activity_changes()))
        self.s.update_activity(a['id'], {'issues': ['通知包含多个活动日期或场次，请人工选择后补全']})
        self.s.upsert_activity(self._change())
        self.assertIsNone(self.s.get_activity(a['id']).get('lifecycle'))
        self.assertIn('ambiguous', [c['match_state'] for c in self.s.activity_changes()])

    def test_startup_migrates_old_pseudo_notice_idempotently_and_only_fills_location(self):
        a = self._session()
        original = self.s.get_activity(a['id'])
        original['location'] = ''
        original['raw_text'] += '\n【志愿地点】测试服务点'
        legacy = dict(self._change(), id='legacy-cancel-placeholder', title='@全体成员')
        for key in ('notice_type', 'lifecycle', 'target_dates', 'target_issues', 'notice_id'):
            legacy.pop(key, None)
        with self.s._db() as db:
            for activity in (original, legacy):
                db.execute('INSERT OR REPLACE INTO activities VALUES(?,?)', (activity['id'], json.dumps(activity)))
        reopened = Store(self.path)
        self.assertIsNone(reopened.get_activity(legacy['id']))
        saved = reopened.get_activity(a['id'])
        self.assertEqual(saved['location'], '测试服务点')
        self.assertEqual(saved['starts_at'], original['starts_at'])
        self.assertEqual(saved['id'], original['id'])
        self.assertEqual(saved['lifecycle'], 'cancelled')
        self.assertEqual(len(Store(self.path).activity_changes()), 1)


if __name__ == '__main__':
    unittest.main()
