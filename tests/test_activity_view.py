# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
import unittest
from datetime import datetime, timedelta, timezone

from qq_activity.activity_view import activity_state, activity_tag, remaining_places, sort_activities


NOW = datetime(2026, 10, 28, 17, tzinfo=timezone(timedelta(hours=8)))


def activity(identifier, day=30, count=None, limit=10, **extra):
    return {'id': identifier, 'starts_at': NOW.replace(day=day).isoformat(),
            'ends_at': NOW.replace(day=day, hour=19).isoformat(),
            'opens_at': (NOW-timedelta(hours=1)).isoformat(),
            'closes_at': (NOW+timedelta(hours=1)).isoformat(), 'form_url':'https://f.wps.cn/g/local/',
            'issues': [], 'form_constraints': {'fill_limit':limit,'count':count}, **extra}


class ActivityViewTests(unittest.TestCase):
    def test_time_descending_is_default_with_unknown_last(self):
        rows = [activity('old',29), activity('missing',starts_at=None), activity('new',31)]
        self.assertEqual([a['id'] for a in sort_activities(rows,now=NOW)], ['new','old','missing'])

    def test_available_only_is_stable_and_does_not_sort_by_number(self):
        rows = [activity('full',count=10), activity('one',29,count=9), activity('five',31,count=5), activity('unknown')]
        self.assertEqual([a['id'] for a in sort_activities(rows,newest=False,available_first=True,now=NOW)], ['one','five','full','unknown'])

    def test_available_precedes_time_and_expired_never_rises(self):
        rows = [activity('new-full',31,count=10),activity('old-open',29,count=8),activity('new-open',30,count=9),
                activity('expired',31,count=0,closes_at=(NOW-timedelta(days=1)).isoformat())]
        self.assertEqual([a['id'] for a in sort_activities(rows,available_first=True,now=NOW)], ['new-open','old-open','new-full','expired'])

    def test_unknown_capacity_is_not_remaining_and_bad_counts_stay_unknown(self):
        for constraints in [{}, {'fill_limit':10,'count':None}, {'fill_limit':0,'count':0}, {'fill_limit':True,'count':0}, {'fill_limit':10,'count':-1}]:
            self.assertIsNone(remaining_places(activity('a',capacity=10,form_constraints=constraints)))
        self.assertEqual(remaining_places(activity('a',count=12)),0)

    def test_status_colors_follow_window_and_confirmed_places(self):
        rows = [(activity('closed',count=0,closes_at=(NOW-timedelta(seconds=1)).isoformat()),'已截止','expired'),
                (activity('future',count=0,opens_at=(NOW+timedelta(minutes=1)).isoformat()),'未开始','upcoming'),
                (activity('open',count=9),'报名中','available'),
                (activity('unknown'),'报名中','muted'),
                (activity('cancelled',count=0,lifecycle='cancelled'),'已取消','expired')]
        for row,state,tag in rows:
            with self.subTest(state=state):
                self.assertEqual(activity_state(row,NOW),state)
                self.assertEqual(activity_tag(row,NOW),tag)


if __name__ == '__main__':
    unittest.main()
