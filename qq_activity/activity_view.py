# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Activity list ordering and states, independent from Tk widgets."""
from datetime import datetime, timedelta, timezone

BEIJING = timezone(timedelta(hours=8))


def _date(value):
    if not value:
        return None
    parsed = datetime.fromisoformat(str(value).replace('Z', '+00:00'))
    return parsed.replace(tzinfo=BEIJING) if parsed.tzinfo is None else parsed


def remaining_places(activity):
    settings = activity.get('form_constraints') or {}
    limit, count = settings.get('fill_limit'), settings.get('count')
    if type(limit) is int and limit > 0 and type(count) is int and count >= 0:
        return max(0, limit-count)
    return None


def activity_state(activity, now=None):
    changed = {'cancelled':'已取消', 'paused':'已暂停', 'postponed':'已延期'}
    if activity.get('lifecycle') in changed:
        return changed[activity['lifecycle']]
    now = now or datetime.now(BEIJING)
    try:
        close, end = _date(activity.get('closes_at')), _date(activity.get('ends_at'))
        if (close and now >= close) or (end and now >= end):
            return '已截止'
        if activity.get('issues') or any(not activity.get(key) for key in ('starts_at','ends_at','opens_at','closes_at','form_url')):
            return '待核对'
        if now < _date(activity['opens_at']):
            return '未开始'
        return '已满' if remaining_places(activity) == 0 else '报名中'
    except (ValueError, TypeError, OverflowError):
        return '待核对'


def activity_tag(activity, now=None):
    state = activity_state(activity, now)
    if state in {'已截止','已取消'}:
        return 'expired'
    if state == '未开始':
        return 'upcoming'
    if state == '报名中' and remaining_places(activity) is not None and remaining_places(activity) > 0:
        return 'available'
    return 'warning' if state in {'待核对','已暂停','已延期'} else 'muted'


def sort_activities(activities, newest=True, available_first=False, now=None):
    result = list(activities)
    if newest:
        def timestamp(activity):
            try:
                value = _date(activity.get('starts_at'))
                return value.timestamp() if value else float('-inf')
            except (ValueError, TypeError, OverflowError, OSError):
                return float('-inf')
        result.sort(key=timestamp, reverse=True)
    if available_first:
        def has_places(activity):
            places = remaining_places(activity)
            return places is not None and places > 0 and activity_state(activity, now) in {'未开始','报名中'}
        result.sort(key=lambda activity: not has_places(activity))
    return result
