# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
import unittest
from datetime import datetime, timezone, timedelta

from qq_activity.parser import parse_notice


CN = timezone(timedelta(hours=8))
REF = datetime(2026, 10, 28, 12, tzinfo=CN)
NOTICE = """文明岗志愿活动
活动时间：10.29周四14:00-16:00
活动地点：学校门口
招募人数：10人
奖励：2思政学分或0.5智能云
报名：活动前一天16点开放，20点截止
https://f.wps.cn/g/DemoCivilForm/"""


class ParserTests(unittest.TestCase):
    def test_civil_notice_actual_window_and_details(self):
        a = parse_notice(NOTICE, REF)
        self.assertEqual(a['category'], '文明岗')
        self.assertEqual(a['starts_at'], '2026-10-29T14:00:00+08:00')
        self.assertEqual(a['ends_at'], '2026-10-29T16:00:00+08:00')
        self.assertEqual(a['opens_at'], '2026-10-28T16:00:00+08:00')
        self.assertEqual(a['closes_at'], '2026-10-28T20:00:00+08:00')
        self.assertEqual(a['rewards'], {'credit': '2', 'cloud': '0.5'})
        self.assertEqual(a['capacity'], 10)
        self.assertEqual(a['location'], '学校门口')
        self.assertEqual(a['issues'], [])

    def test_college_points_alias_preserves_cloud_enum_and_original_notice(self):
        for phrase in ('0.75学院积分', '学院积分：0.75', '0.75智能云', '智能云0.75'):
            with self.subTest(phrase=phrase):
                text = NOTICE.replace('0.5智能云', phrase)
                activity = parse_notice(text, REF)
                self.assertEqual(activity['rewards']['cloud'], '0.75')
                self.assertEqual(activity['raw_text'], text)

    def test_historical_civil_notice(self):
        a = parse_notice(NOTICE.replace('10.29周四14:00-16:00', '10.28（周三）10:00-12:00'), REF)
        self.assertTrue(a['opens_at'].startswith('2026-10-27T16:00'))
        self.assertTrue(a['closes_at'].startswith('2026-10-27T20:00'))

    def test_user_single_line_example_keeps_relative_day(self):
        a = parse_notice('文明岗10.29周四14:00-16:00 前一天16点开放20点截止 https://f.wps.cn/g/DemoCivilForm/', REF)
        self.assertTrue(a['opens_at'].startswith('2026-10-28T16:00'))
        self.assertTrue(a['closes_at'].startswith('2026-10-28T20:00'))
        self.assertEqual(a['issues'], [])

    def test_ambiguous_multi_session_and_distant_year_are_flagged(self):
        a = parse_notice(NOTICE.replace('10.29周四14:00-16:00', '10.29周四14:00-16:00；10.30周五14:00-16:00'), REF)
        self.assertTrue(any('多个活动' in issue for issue in a['issues']))
        a = parse_notice(NOTICE.replace('10.29周四', '4.29周三'), REF)
        self.assertTrue(any('年份' in issue for issue in a['issues']))

    def test_relative_dates_without_activity_anchor_are_incomplete(self):
        a = parse_notice('志愿活动报名：前一天16点开放，20点截止 https://f.wps.cn/g/abc/', REF)
        self.assertIsNone(a['opens_at'])
        self.assertTrue(a['issues'])

    def test_explicit_tiangong_window_overrides_relative(self):
        a = parse_notice('天宫庄园志愿活动\n活动时间10月27日周二13：00-16：00\n报名10月25日16：00开放，截止于第二天的下午16：00\n奖励思政学分3分，智能云0.75\nhttps://f.wps.cn/g/DemoGardenForm/', REF)
        self.assertEqual(a['category'], '天宫庄园')
        self.assertTrue(a['opens_at'].startswith('2026-10-25T16:00'))
        self.assertTrue(a['closes_at'].startswith('2026-10-26T16:00'))
        self.assertEqual(a['issues'], [])

    def test_fullwidth_multiline_and_categories(self):
        for title, category in [('信智青协地铁站志愿者活动', '地铁站志愿活动'), ('宁波汽车南站志愿者招募', '汽车南站志愿活动')]:
            a = parse_notice(NOTICE.replace('文明岗志愿活动', title).replace('10.29周四14:00-16:00', '１０月２９日\n（周四）１４：００—１６：００'), REF)
            self.assertEqual(a['category'], category)
            self.assertEqual(a['issues'], [])

    def test_ordinary_chat_ignored(self):
        for text in ['大家下午好', '明天10.29下午一起吃饭', '这个链接 https://example.com/',
                     '@全体成员\n志愿活动请大家不要重复报名，报名情况请看群文件',
                     '文明岗报名情况请看群文件', '天宫庄园有没有人一起去？']:
            self.assertIsNone(parse_notice(text, REF))

    def test_cancellation_is_preserved_with_meaningful_title_and_blocking_issue(self):
        a = parse_notice('@全体成员\n因天气原因，本周日天宫庄园活动取消，请注意安全。', REF)
        self.assertEqual(a['category'], '天宫庄园')
        self.assertIn('取消', a['title'])
        self.assertNotIn('@全体成员', a['title'])
        self.assertTrue(any('取消' in issue for issue in a['issues']))

    def test_change_dates_follow_message_beijing_date(self):
        observed = datetime(2026, 10, 24, 23, tzinfo=CN)
        for phrase in ('明天天宫庄园活动取消', '本周日天宫庄园活动取消', '10月25日天宫庄园活动取消'):
            a = parse_notice(phrase, observed)
            self.assertEqual(a['notice_type'], 'activity_change')
            self.assertEqual(a['lifecycle'], 'cancelled')
            self.assertEqual(a['target_dates'], ['2026-10-25'])
            self.assertEqual(a['target_issues'], [])
            self.assertFalse(any('缺少' in issue for issue in a['issues']))
        a = parse_notice('明天天宫庄园活动暂停', datetime(2026, 10, 24, 18, tzinfo=timezone.utc))
        self.assertEqual(a['target_dates'], ['2026-10-26'])
        self.assertEqual(a['lifecycle'], 'paused')
        self.assertEqual(parse_notice('10月25日天宫庄园活动延期', observed)['lifecycle'], 'postponed')
        self.assertTrue(parse_notice('10月25日周二天宫庄园活动取消', observed)['target_issues'])

    def test_ambiguous_change_and_conditional_cancellation(self):
        a = parse_notice('10月25日、10月27日天宫庄园活动取消', REF)
        self.assertEqual(len(a['target_dates']), 2)
        self.assertTrue(a['target_issues'])
        a = parse_notice(NOTICE + '\n如遇天气恶劣导致活动取消，会另行通知。', REF)
        self.assertNotEqual(a.get('notice_type'), 'activity_change')

    def test_bracketed_and_checkin_locations(self):
        for phrase in ('【活动地点】学校正门', '【志愿地点】：学校正门', '【地点】学校正门', '签到签退地点：学校正门'):
            with self.subTest(phrase=phrase):
                a = parse_notice(NOTICE.replace('活动地点：学校门口', phrase), REF)
                self.assertEqual(a['location'], '学校正门')
        a = parse_notice(NOTICE.replace('活动地点：学校门口', '签到地点：学校正门\n签退地点：学校北门'), REF)
        self.assertEqual(a['location'], '签到：学校正门；签退：学校北门')

    def test_image_only_manual_placeholder_is_preserved(self):
        a = parse_notice('【待核对图片通知】\n活动信息在图片中，请补充活动文字和报名链接。', REF)
        self.assertIsNotNone(a)
        self.assertTrue(any('图片' in issue for issue in a['issues']))
        self.assertIsNone(a['starts_at'])

    def test_mention_prefix_and_doubled_dash_in_explicit_session(self):
        a = parse_notice('@全体成员\n' + NOTICE.replace('14:00-16:00', '14：00——16：00'), REF)
        self.assertEqual(a['title'], '文明岗志愿活动')
        self.assertEqual(a['starts_at'], '2026-10-29T14:00:00+08:00')
        self.assertEqual(a['ends_at'], '2026-10-29T16:00:00+08:00')
        self.assertEqual(a['issues'], [])

    def test_malformed_day_is_not_filled_from_registration_date(self):
        a = parse_notice('宁波汽车南站志愿者招募\n【活动时间】10月日周日10：00-12：00\n10月28日16：00开始报名\nhttps://f.wps.cn/g/example/', REF)
        self.assertIsNone(a['starts_at'])
        self.assertIsNone(a['activity_date'])
        self.assertTrue(a['issues'])

    def test_missing_window_and_weekday_conflict_are_issues(self):
        a = parse_notice(NOTICE.replace('活动前一天16点开放，20点截止', '稍后开放').replace('周四', '周三'), REF)
        self.assertIsNone(a['opens_at'])
        self.assertIsNone(a['closes_at'])
        self.assertTrue(any('星期' in issue for issue in a['issues']))
        self.assertTrue(any('报名' in issue for issue in a['issues']))

    def test_undated_opening_not_silently_activity_date(self):
        a = parse_notice(NOTICE.replace('活动前一天16点开放', '16点开放'), REF)
        self.assertIsNone(a['opens_at'])
        self.assertTrue(a['issues'])

    def test_year_rollover_and_explicit_year(self):
        a = parse_notice(NOTICE.replace('10.29周四', '1.1周五'), datetime(2026, 12, 31, 10, tzinfo=CN))
        self.assertTrue(a['starts_at'].startswith('2027-01-01'))
        self.assertTrue(a['opens_at'].startswith('2026-12-31'))
        a = parse_notice(NOTICE.replace('10.29周四', '2025年10月29日周三'), REF)
        self.assertTrue(a['starts_at'].startswith('2025-10-29'))

    def test_invalid_dates_do_not_crash(self):
        a = parse_notice(NOTICE.replace('10.29', '2.30'), REF)
        self.assertIsNone(a['starts_at'])
        self.assertTrue(a['issues'])

    def test_same_link_different_dates_have_distinct_ids(self):
        a = parse_notice(NOTICE, REF)
        b = parse_notice(NOTICE.replace('10.29周四', '10.28周三'), REF)
        self.assertNotEqual(a['id'], b['id'])
        self.assertEqual(a['id'], parse_notice(NOTICE + '\n转发', REF)['id'])

    def test_same_date_time_correction_keeps_identity_for_store_guard(self):
        a = parse_notice(NOTICE, REF)
        b = parse_notice(NOTICE.replace('14:00-16:00', '13:00-15:00'), REF)
        self.assertEqual(a['id'], b['id'])

    def test_missing_url_different_titles_same_day_do_not_collide(self):
        text = NOTICE.replace('https://f.wps.cn/g/DemoCivilForm/', '')
        a = parse_notice(text, REF)
        b = parse_notice(text.replace('文明岗志愿活动', '天宫庄园志愿活动'), REF)
        c = parse_notice(text.replace('文明岗志愿活动', '文明岗志愿活动第二批'), REF)
        self.assertNotEqual(a['id'], b['id'])
        self.assertNotEqual(a['id'], c['id'])
        self.assertEqual(a['id'], parse_notice('  ' + text + '\n转发', REF)['id'])


if __name__ == '__main__':
    unittest.main()
