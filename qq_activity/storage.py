# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Durable local state; every operation owns an independent SQLite connection."""
from __future__ import annotations

import json
import hashlib
import re
import sqlite3
import unicodedata
import uuid
from contextlib import contextmanager
from datetime import datetime, timedelta, timezone
from pathlib import Path


BEIJING = timezone(timedelta(hours=8))
INACTIVE = {'cancelled', 'paused', 'postponed'}
STATUSES = {'queued', 'running', 'success', 'full', 'expired', 'login_required',
            'attention', 'uncertain', 'cancelled', 'failed'}
PROTECTED = {'queued', 'running', 'success', 'uncertain'}
TARGET_FIELDS = ('starts_at', 'ends_at', 'opens_at', 'closes_at', 'form_url')


def _now():
    return datetime.now(BEIJING).isoformat()


def _time(value):
    try:
        parsed = datetime.fromisoformat(value)
        if parsed.tzinfo is None:
            raise ValueError('时间必须包含时区')
        return parsed
    except (TypeError, ValueError) as exc:
        raise ValueError('活动时间无效或缺少时区') from exc


def _dump(value):
    return json.dumps(value, ensure_ascii=False)


class Store:
    def __init__(self, path):
        self.path = str(Path(path).resolve())
        Path(self.path).parent.mkdir(parents=True, exist_ok=True)
        with self._db() as db:
            db.executescript('''
                CREATE TABLE IF NOT EXISTS activities (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS profiles (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS settings (key TEXT PRIMARY KEY, value TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS activity_changes (id TEXT PRIMARY KEY, data TEXT NOT NULL);
                CREATE TABLE IF NOT EXISTS jobs (
                    id TEXT PRIMARY KEY, activity_id TEXT NOT NULL, profile_id TEXT NOT NULL,
                    reward TEXT NOT NULL, status TEXT NOT NULL, detail TEXT NOT NULL,
                    created_at TEXT NOT NULL, updated_at TEXT NOT NULL,
                    UNIQUE(activity_id, profile_id));
            ''')
            db.execute("UPDATE jobs SET status='uncertain', detail=?, updated_at=? WHERE status='running'",
                       ('程序中断时任务正在执行，需人工核实结果，禁止自动重试', _now()))
            self._migrate_notices(db)
            self._reconcile_changes(db)

    @contextmanager
    def _db(self):
        db = sqlite3.connect(self.path, timeout=15)
        db.row_factory = sqlite3.Row
        try:
            db.execute('PRAGMA busy_timeout=15000')
            yield db
            db.commit()
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def _get(db, table, identifier):
        row = db.execute(f'SELECT data FROM {table} WHERE id=?', (identifier,)).fetchone()
        return json.loads(row['data']) if row else None

    def activities(self):
        with self._db() as db:
            return [json.loads(row['data']) for row in db.execute('SELECT data FROM activities ORDER BY rowid DESC')]

    def get_activity(self, identifier):
        with self._db() as db:
            return self._get(db, 'activities', identifier)

    def activity_changes(self):
        """All persisted change notices, including unmatched/ambiguous ones."""
        with self._db() as db:
            return [json.loads(row['data']) for row in db.execute('SELECT data FROM activity_changes ORDER BY rowid DESC')]

    def _save_change(self, db, change):
        change = dict(change)
        group = str(change.get('source_group_id') or '')
        source_id = change.get('source_message_id') or change.get('notice_id') or change['id']
        identifier = hashlib.sha256(f'{group}|{source_id}'.encode()).hexdigest()[:24]
        change.update(id=identifier, notice_id=identifier,
                      matched_activity_id=None, match_state='unmatched')
        db.execute('INSERT INTO activity_changes VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (identifier, _dump(change)))
        return identifier

    @staticmethod
    def _same_target(change, activity):
        group = str(change.get('source_group_id') or '')
        if not group or group != str(activity.get('source_group_id') or ''):
            return False
        category = change.get('category')
        if category and category != '其他':
            if category != activity.get('category'):
                return False
        else:
            def normalized(value):
                return re.sub(r'[\W_]+', '', unicodedata.normalize('NFKC', str(value)).casefold())
            name = normalized(change.get('target_name') or '')
            title = normalized(activity.get('title') or '')
            if not name or name != title:
                return False
        try:
            date = _time(activity.get('starts_at')).astimezone(BEIJING).date().isoformat()
        except ValueError:
            date = activity.get('activity_date')
        return bool(date and date in change.get('target_dates', []))

    def _reconcile_changes(self, db):
        activities = [json.loads(row['data']) for row in db.execute('SELECT data FROM activities')]
        changes = [json.loads(row['data']) for row in db.execute('SELECT data FROM activity_changes')]
        associated = {}
        for change in changes:
            candidates = []
            if (len(change.get('target_dates', [])) == 1 and not change.get('target_issues')
                    and change.get('lifecycle') in INACTIVE):
                candidates = [a for a in activities if self._same_target(change, a)]
            ambiguous = len(candidates) > 1 or any(
                any(re.search(r'多个活动|多个.*场次', issue) for issue in a.get('issues', [])) for a in candidates)
            change['matched_activity_id'] = None
            change['match_state'] = 'ambiguous' if ambiguous else 'unmatched'
            if not ambiguous and len(candidates) == 1:
                identifier = candidates[0]['id']
                change.update(matched_activity_id=identifier, match_state='matched')
                associated.setdefault(identifier, []).append(change)
            db.execute('UPDATE activity_changes SET data=? WHERE id=?', (_dump(change), change['id']))
        for activity in activities:
            notices = sorted(associated.get(activity['id'], []), key=lambda c: c.get('observed_at', ''))
            if not notices and not activity.get('change_notices'):
                continue
            activity['issues'] = [issue for issue in activity.get('issues', []) if not issue.startswith('活动变更：')]
            if notices:
                # Store immutable source notices separately from activity timing.
                activity['change_notices'] = [dict(c) for c in notices]
                latest = notices[-1]
                activity['change_notice'] = dict(latest)
                activity['lifecycle'] = latest['lifecycle']
                label = {'cancelled': '取消', 'paused': '暂停', 'postponed': '延期'}[latest['lifecycle']]
                message = f'活动变更：活动已{label}，自动报名已停止，请核对变更通知'
                activity['issues'].append(message)
                db.execute("UPDATE jobs SET status='cancelled',detail=?,updated_at=? WHERE activity_id=? AND status='queued'",
                           (message, _now(), activity['id']))
            else:
                # Later history may reveal a second possible session. Withdraw
                # the automatic association and leave stopped jobs for review.
                for key in ('lifecycle', 'change_notice', 'change_notices'):
                    activity.pop(key, None)
                db.execute("UPDATE jobs SET status='attention',detail='后到通知显示变更对应多个场次，请人工核对',updated_at=? WHERE activity_id=? AND status='cancelled' AND detail LIKE '活动变更：%'",
                           (_now(), activity['id']))
            db.execute('UPDATE activities SET data=? WHERE id=?', (_dump(activity), activity['id']))

    def _migrate_notices(self, db):
        """Idempotently relocate old change placeholders and fill location only."""
        from .parser import parse_notice
        for row in db.execute('SELECT id,data FROM activities').fetchall():
            activity = json.loads(row['data'])
            try:
                observed = _time(activity.get('observed_at'))
            except ValueError:
                continue
            parsed = parse_notice(activity.get('raw_text', ''), observed)
            if parsed and parsed.get('notice_type') == 'activity_change':
                for key in ('source_group_id', 'source_message_id', 'source_delivery'):
                    if key in activity:
                        parsed[key] = activity[key]
                self._save_change(db, parsed)
                db.execute("UPDATE jobs SET status='cancelled',detail='旧变更通知已迁移，停止无效任务',updated_at=? WHERE activity_id=? AND status='queued'", (_now(), row['id']))
                db.execute('DELETE FROM activities WHERE id=?', (row['id'],))
            elif parsed and not activity.get('location') and parsed.get('location'):
                activity['location'] = parsed['location']
                db.execute('UPDATE activities SET data=? WHERE id=?', (_dump(activity), row['id']))

    def _save_activity(self, db, activity, incoming=False):
        identifier = activity.get('id')
        if not identifier:
            raise ValueError('活动缺少 id')
        existing = self._get(db, 'activities', identifier)
        if incoming and existing:
            # Normal forwards never reactivate an activity or erase its source.
            for key in ('lifecycle', 'change_notice', 'change_notices', 'source_group_id'):
                if key in existing and (key != 'source_group_id' or not activity.get(key)):
                    activity[key] = existing[key]
            if existing.get('form_url') == activity.get('form_url'):
                for key in ('form_constraints', 'profile_fields'):
                    if key in existing and key not in activity:
                        activity[key] = existing[key]
            activity['issues'] = list(dict.fromkeys([*activity.get('issues', []),
                *(issue for issue in existing.get('issues', []) if issue.startswith('活动变更：'))]))
        if existing and any(existing.get(key) != activity.get(key) for key in TARGET_FIELDS):
            protected = db.execute("SELECT 1 FROM jobs WHERE activity_id=? AND status IN ('queued','running','success','uncertain') LIMIT 1", (identifier,)).fetchone()
            if protected or (incoming and existing.get('conflicting_notice')):
                if not incoming:
                    raise ValueError('该活动已有排队、执行或已提交记录，不能更换日期、报名窗口或链接；请先核对并处理已有任务')
                # Never drop a correction or let pending jobs execute a stale
                # schedule. Historical targets stay intact; the full incoming
                # notice is saved separately for explicit user reconciliation.
                correction = dict(activity)
                activity = dict(existing)
                activity['conflicting_notice'] = correction
                message = ('收到与已有任务冲突的新通知，排队任务已暂停。'
                           f"更新的活动时间：{correction.get('starts_at')}；"
                           f"报名窗口：{correction.get('opens_at')} 至 {correction.get('closes_at')}；"
                           f"表单：{correction.get('form_url')}。请核对更新通知后再排队")
                activity['issues'] = list(dict.fromkeys([*activity.get('issues', []), message]))
                db.execute("UPDATE jobs SET status='attention',detail='通知时间或报名链接发生变化，请核对更新后的通知',updated_at=? WHERE activity_id=? AND status='queued'",
                           (_now(), identifier))
        if incoming and existing and existing.get('conflicting_notice') and not activity.get('conflicting_notice'):
            # A repeat of the original notice cannot clear a pending conflict.
            activity['conflicting_notice'] = existing['conflicting_notice']
            activity['issues'] = list(dict.fromkeys([*activity.get('issues', []), *existing.get('issues', [])]))
        db.execute('INSERT INTO activities(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                   (identifier, _dump(activity)))
        return identifier

    def upsert_activity(self, activity):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            activity = dict(activity)
            if activity.get('notice_type') == 'activity_change':
                notice_id = self._save_change(db, activity)
                self._reconcile_changes(db)
                saved = self._get(db, 'activity_changes', notice_id)
                return saved.get('matched_activity_id') or notice_id
            existing = self._get(db, 'activities', activity['id'])
            if (existing and existing.get('source_group_id') and activity.get('source_group_id')
                    and str(existing['source_group_id']) != str(activity['source_group_id'])):
                activity['id'] = hashlib.sha256(f"{activity['source_group_id']}|{activity['id']}".encode()).hexdigest()[:24]
            identifier = self._save_activity(db, activity, incoming=True)
            self._reconcile_changes(db)
            return identifier

    def update_activity(self, identifier, changes):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            existing = self._get(db, 'activities', identifier)
            if not existing:
                raise ValueError('活动不存在')
            existing.update(changes)
            existing['id'] = identifier
            if 'issues' in changes and not changes['issues']:
                existing.pop('conflicting_notice', None)
            self._save_activity(db, existing)
            self._reconcile_changes(db)
            return self._get(db, 'activities', identifier)

    def profiles(self):
        with self._db() as db:
            return [json.loads(row['data']) for row in db.execute('SELECT data FROM profiles ORDER BY rowid')]

    def get_profile(self, identifier):
        with self._db() as db:
            return self._get(db, 'profiles', identifier)

    def save_profile(self, profile):
        result = dict(profile)
        result['id'] = result.get('id') or uuid.uuid4().hex
        for key in ('name', 'major', 'class_name', 'student_id', 'phone', 'email'):
            if not isinstance(result.get(key, ''), str):
                raise ValueError('个人资料字段必须为文本，以保留前导零')
            result[key] = result.get(key, '').strip()
        result['reward'] = result.get('reward') or 'credit'
        if result['reward'] not in ('credit', 'cloud'):
            raise ValueError('奖励请选择学分或学院积分')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            previous = self._get(db, 'profiles', result['id'])
            if previous and previous.get('student_id') != result['student_id']:
                if db.execute("SELECT 1 FROM jobs WHERE profile_id=? AND status IN ('queued','running','success','uncertain') LIMIT 1", (result['id'],)).fetchone():
                    raise ValueError('该人员已有排队、执行、成功或待核实记录，需保留原学号以防重复报名；其他人员请新增档案')
            db.execute('INSERT INTO profiles(id,data) VALUES(?,?) ON CONFLICT(id) DO UPDATE SET data=excluded.data',
                       (result['id'], _dump(result)))
        return result['id']

    def delete_profile(self, identifier):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            if db.execute("SELECT 1 FROM jobs WHERE profile_id=? AND status IN ('queued','running','success','uncertain')", (identifier,)).fetchone():
                raise ValueError('该人员有排队、执行、成功或待核实记录，需保留档案以防重复报名；排队任务可先取消')
            db.execute('DELETE FROM profiles WHERE id=?', (identifier,))

    def queue(self, activity_id, profile_ids, rewards=None):
        ids = list(dict.fromkeys(profile_ids))
        if not ids:
            return []
        if rewards is not None and not isinstance(rewards, dict):
            raise ValueError('奖励选择须为人员 id 到 credit/cloud 的映射')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            activity = self._get(db, 'activities', activity_id)
            if not activity or activity.get('issues') or activity.get('lifecycle') in INACTIVE:
                raise ValueError('活动信息缺失或仍有待核对事项')
            starts, ends, opens, closes = [_time(activity.get(k)) for k in TARGET_FIELDS[:4]]
            if ends <= starts or closes <= opens or opens > starts or closes > starts:
                raise ValueError('活动或报名窗口时间顺序无效')
            if not activity.get('form_url'):
                raise ValueError('缺少报名链接')
            selections = []
            for profile_id in ids:
                profile = self._get(db, 'profiles', profile_id)
                if profile is None:
                    raise ValueError('所选人员不存在')
                if any(not profile.get(key) for key in ('name', 'major', 'class_name', 'student_id', 'phone', 'email')):
                    raise ValueError('所选人员资料尚未填写完整')
                reward = (rewards or {}).get(profile_id, profile.get('reward', 'credit'))
                if reward not in ('credit', 'cloud'):
                    raise ValueError('奖励选择无效')
                selections.append((profile_id, reward))
            result = []
            stamp = _now()
            for profile_id, reward in selections:
                row = db.execute('SELECT * FROM jobs WHERE activity_id=? AND profile_id=?', (activity_id, profile_id)).fetchone()
                if row and row['status'] in PROTECTED:
                    continue
                identifier = row['id'] if row else uuid.uuid4().hex
                db.execute('''INSERT INTO jobs VALUES(?,?,?,?,?,?,?,?)
                    ON CONFLICT(activity_id,profile_id) DO UPDATE SET reward=excluded.reward,
                    status='queued',detail='',updated_at=excluded.updated_at''',
                    (identifier, activity_id, profile_id, reward, 'queued', '', stamp, stamp))
                result.append(identifier)
            return result

    def jobs(self):
        with self._db() as db:
            return [dict(row) for row in db.execute('SELECT * FROM jobs ORDER BY created_at,id')]

    def get_job(self, identifier):
        with self._db() as db:
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            return dict(row) if row else None

    def update_job(self, identifier, status, detail=''):
        if status not in STATUSES:
            raise ValueError('未知任务状态')
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('任务不存在')
            if row['status'] == 'success' and status != 'success':
                raise ValueError('已成功任务不能重新执行')
            if row['status'] == 'uncertain' and status in ('queued', 'running'):
                raise ValueError('结果待核实任务不能自动重试')
            db.execute('UPDATE jobs SET status=?,detail=?,updated_at=? WHERE id=?', (status, str(detail), _now(), identifier))

    def cancel_job(self, identifier):
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT status FROM jobs WHERE id=?', (identifier,)).fetchone()
            if not row:
                raise ValueError('任务不存在')
            if row['status'] == 'running':
                raise ValueError('任务正在执行，无法安全取消')
            if row['status'] != 'queued':
                return
            db.execute("UPDATE jobs SET status='cancelled',detail='用户取消',updated_at=? WHERE id=?", (_now(), identifier))

    def due_jobs(self, now=None):
        current = now or datetime.now(BEIJING)
        current = current.replace(tzinfo=BEIJING) if current.tzinfo is None else current
        result = []
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            for row in db.execute("SELECT * FROM jobs WHERE status='queued' ORDER BY created_at,id").fetchall():
                activity = self._get(db, 'activities', row['activity_id'])
                try:
                    if activity and activity.get('lifecycle') in INACTIVE:
                        db.execute("UPDATE jobs SET status='cancelled',detail='活动变更：活动已取消、暂停或延期，自动报名停止',updated_at=? WHERE id=?", (_now(), row['id']))
                        continue
                    if not activity or activity.get('issues'):
                        raise ValueError('活动仍有待核对信息')
                    opening, closing = _time(activity.get('opens_at')), _time(activity.get('closes_at'))
                    if current >= closing:
                        db.execute("UPDATE jobs SET status='expired',detail='报名已截止',updated_at=? WHERE id=?", (_now(), row['id']))
                    elif opening <= current:
                        result.append(dict(row))
                except ValueError:
                    db.execute("UPDATE jobs SET status='attention',detail='报名时间或活动信息待核对',updated_at=? WHERE id=?", (_now(), row['id']))
        return result

    def claim_job(self, identifier, now=None):
        """Atomically acquire a due queued job; also recheck its live deadline."""
        current = now or datetime.now(BEIJING)
        current = current.replace(tzinfo=BEIJING) if current.tzinfo is None else current
        with self._db() as db:
            db.execute('BEGIN IMMEDIATE')
            row = db.execute('SELECT * FROM jobs WHERE id=?', (identifier,)).fetchone()
            if not row or row['status'] != 'queued':
                return False
            activity = self._get(db, 'activities', row['activity_id'])
            try:
                if activity and activity.get('lifecycle') in INACTIVE:
                    db.execute("UPDATE jobs SET status='cancelled',detail='活动变更：活动已取消、暂停或延期，自动报名停止',updated_at=? WHERE id=?", (_now(), identifier))
                    return False
                if not activity or activity.get('issues'):
                    raise ValueError('活动仍有待核对信息')
                opening, closing = _time(activity.get('opens_at')), _time(activity.get('closes_at'))
            except ValueError:
                db.execute("UPDATE jobs SET status='attention',detail='报名时间或活动信息待核对',updated_at=? WHERE id=?", (_now(), identifier))
                return False
            if current >= closing:
                db.execute("UPDATE jobs SET status='expired',detail='报名已截止',updated_at=? WHERE id=?", (_now(), identifier))
                return False
            if current < opening:
                return False
            db.execute("UPDATE jobs SET status='running',detail='',updated_at=? WHERE id=? AND status='queued'", (_now(), identifier))
            return True

    def get_setting(self, key, default=None):
        with self._db() as db:
            row = db.execute('SELECT value FROM settings WHERE key=?', (key,)).fetchone()
            return json.loads(row['value']) if row else default

    def set_setting(self, key, value):
        with self._db() as db:
            db.execute('INSERT INTO settings VALUES(?,?) ON CONFLICT(key) DO UPDATE SET value=excluded.value', (key, _dump(value)))
