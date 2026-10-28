# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Conservative extraction of Chinese activity notices, using Beijing time."""
from __future__ import annotations

import hashlib
import re
import unicodedata
from datetime import datetime, timedelta, timezone


BEIJING = timezone(timedelta(hours=8), 'Asia/Shanghai')
DATE = re.compile(r'(?<!\d)(?<!\d:)(?:(?P<year>20\d{2})\s*[年./-]\s*)?(?P<month>\d{1,2})\s*[月./-]\s*(?P<day>\d{1,2})(?:[日号]|(?![\d:]))')
TIME = re.compile(r'(?P<part>上午|下午|晚上|中午)?\s*(?P<hour>\d{1,2})\s*(?::\s*(?P<minute>\d{2})|[点时](?:(?P<minute2>\d{1,2})分?)?)')
RANGE = re.compile(r'(?:上午|下午|晚上|中午)?\s*\d{1,2}\s*(?::\s*\d{2}|[点时](?:\d{1,2}分?)?)\s*[-—–~～至到]+\s*(?:上午|下午|晚上|中午)?\s*\d{1,2}\s*(?::\s*\d{2}|[点时](?:\d{1,2}分?)?)')
MARKER = re.compile(r'开放|开启|开始报名|报名开始|开始收集|截止|结束报名|报名结束')
CHANGE = re.compile(r'(?:活动|志愿服务)(?:已|现已|临时|决定)?(?:取消|暂停|延期|撤销)|(?:取消|暂停|延期|撤销)(?:本次|本周末)?(?:的)?(?:文明岗|天宫庄园|地铁站|汽车南站)?(?:志愿)?活动')


def _clock(match):
    hour = int(match['hour'])
    minute = int(match['minute'] or match['minute2'] or 0)
    if match['part'] in ('下午', '晚上', '中午') and hour < 12:
        hour += 12
    if hour > 23 or minute > 59:
        raise ValueError('无效时刻')
    return hour, minute


def _date(match, reference, issues):
    month, day = int(match['month']), int(match['day'])
    if match['year']:
        return datetime(int(match['year']), month, day, tzinfo=BEIJING)
    candidates = []
    for year in (reference.year - 1, reference.year, reference.year + 1):
        try:
            candidates.append(datetime(year, month, day, tzinfo=BEIJING))
        except ValueError:
            pass
    if not candidates:
        raise ValueError('无效日期')
    candidates.sort(key=lambda d: abs((d - reference).total_seconds()))
    if abs((candidates[0].date() - reference.date()).days) > 120:
        issues.append('缺少年份且距离消息日期较远，请核对活动年份')
    return candidates[0]


def _iso(value):
    return value.isoformat() if value is not None else None


def _change_match(source):
    for match in CHANGE.finditer(source):
        start = max(source.rfind(mark, 0, match.start()) + 1 for mark in ('\n', '。', '，', ',', '；', ';'))
        end = min([len(source)] + [at for mark in ('\n', '。', '，', ',', '；', ';') if (at := source.find(mark, match.end())) >= 0])
        clause = source[start:end]
        if re.search(r'如果|如遇|若遇|如有|若有|可能|一旦|假如|如因|如若|[?？]', clause):
            continue
        return match
    return None


def _change_dates(source, observed):
    dates, issues = set(), []
    explicit = list(DATE.finditer(source))
    for match in explicit:
        try:
            date = _date(match, observed, issues)
            dates.add(date.date().isoformat())
            weekday = re.match(r'\s*[(（]?\s*(?:周|星期)([一二三四五六日天])', source[match.end():])
            if weekday and '一二三四五六日'.index(weekday[1].replace('天', '日')) != date.weekday():
                issues.append('变更通知中的日期与星期不一致')
        except ValueError:
            issues.append('变更通知中的日期无效')
    for word, delta in [('明天', 1), ('明日', 1), ('后天', 2), ('今天', 0), ('今日', 0)]:
        if word in source:
            dates.add((observed + timedelta(days=delta)).date().isoformat())
    # An explicit month/day already identifies its weekday. Do not turn a
    # parenthesized weekday into a second independently guessed date.
    if not explicit:
        for match in re.finditer(r'(本|这|下)?(?:周|星期)([一二三四五六日天])', source):
            weekday = '一二三四五六日'.index(match[2].replace('天', '日'))
            delta = weekday - observed.weekday()
            if match[1] == '下':
                delta += 7
            elif match[1] not in ('本', '这'):
                delta %= 7
            dates.add((observed + timedelta(days=delta)).date().isoformat())
    if len(dates) != 1:
        issues.append('变更通知缺少唯一的活动日期，请人工核对')
    return sorted(dates), list(dict.fromkeys(issues))


def _location(source):
    matches = list(re.finditer(r'(?P<label>签到签退地点|签到及签退地点|签到地点|签退地点|活动地点|志愿地点|服务地点|地点)\s*(?:[】\]]\s*[:：]?|[:：])\s*(?P<value>[^\n，,。；;【]+)', source))
    main = [match for match in matches if match['label'] not in ('签到地点', '签退地点')]
    if main:
        return main[0]['value'].strip()
    return '；'.join(('签到' if match['label'] == '签到地点' else '签退') + '：' + match['value'].strip() for match in matches)


def parse_notice(text: str, observed_at: datetime | None = None) -> dict | None:
    if not isinstance(text, str) or not text.strip():
        return None
    source = unicodedata.normalize('NFKC', text)
    image_notice = '待核对图片通知' in source
    cancellation = _change_match(source)
    topic = re.search(r'文明岗|天宫庄园|地铁站.*志愿|汽车南站.*志愿|志愿.*(?:活动|招募)|f\.wps\.cn/g/', source, re.I)
    metadata = (DATE.search(source) or TIME.search(source) or
                re.search(r'f\.wps\.cn/g/|活动(?:时间|地点|工时|名称)|招募(?:人数|对象|要求)|报名链接|奖励\s*[:：]', source, re.I))
    if not image_notice and (not topic or not (metadata or cancellation)):
        return None
    observed = observed_at or datetime.now(BEIJING)
    observed = observed.replace(tzinfo=BEIJING) if observed.tzinfo is None else observed.astimezone(BEIJING)
    issues = []
    if image_notice:
        issues.append('活动信息仅在图片中，请人工补充活动文字和报名链接')
    if cancellation:
        issues.append('通知提及活动取消、暂停或延期，请人工核对并停止相关活动的自动报名')
    urls = list(dict.fromkeys(re.findall(r'https?://f\.wps\.cn/g/[A-Za-z0-9_-]+/?', source, re.I)))
    form_url = urls[0].rstrip('/') + '/' if urls else None
    if not form_url:
        issues.append('缺少 WPS 报名链接')
    elif len(urls) > 1:
        issues.append('通知包含多个报名链接，请核对对应表单')
    category = next((label for pattern, label in [
        ('文明岗', '文明岗'), ('天宫庄园', '天宫庄园'),
        ('地铁站', '地铁站志愿活动'), ('汽车南站', '汽车南站志愿活动')
    ] if pattern in source), '其他')
    lines = [re.sub(r'^@(?:全体成员|所有人)\s*', '', line.strip()) for line in source.splitlines()]
    lines = [line for line in lines if line]
    title = lines[0][:160] if lines else '待核对活动'
    if cancellation:
        change = re.search(r'取消|暂停|延期|撤销', cancellation.group())[0]
        title = f'{category}活动{change}（待核对）'
        target_dates, target_issues = _change_dates(source, observed)
        lifecycle = {'取消': 'cancelled', '撤销': 'cancelled', '暂停': 'paused', '延期': 'postponed'}[change]
        name = re.search(r'活动名称\s*[】\]]?\s*[:：]?\s*([^\n，,。；;]+)', source)
        target_name = name[1].strip() if name else (category if category != '其他' else '')
        identity = f'change|{lifecycle}|{category}|{target_dates}|{observed.date()}|{source.strip()}'
        notice_id = hashlib.sha256(identity.encode()).hexdigest()[:24]
        return dict(id=notice_id, notice_id=notice_id, notice_type='activity_change',
                    title=title, category=category, lifecycle=lifecycle,
                    target_dates=target_dates, target_name=target_name, target_issues=target_issues,
                    starts_at=None, ends_at=None, opens_at=None, closes_at=None,
                    location='', capacity=None, rewards={}, form_url=form_url,
                    raw_text=text, observed_at=observed.isoformat(),
                    issues=['活动变更：活动已' + change + '，停止相关活动的自动报名', *target_issues])
    starts = ends = activity_date = None
    chosen = None
    # A date followed by an activity time range is stronger evidence than a
    # registration date. Newlines between date and range are allowed.
    candidates = []
    for dm in DATE.finditer(source):
        before = source[max(0, source.rfind('\n', 0, dm.start()) + 1):dm.start()]
        after = source[dm.end():dm.end() + 100]
        next_date = DATE.search(after)
        if next_date:
            after = after[:next_date.start()]
        span = RANGE.search(after)
        if (re.search(r'报名|开放|截止|收集', before) or
                (not span and re.match(r'\s*(?:上午|下午|晚上|中午)?\s*\d{1,2}\s*[:点时][^\n，,。]{0,12}(?:开始报名|开放|截止)', after))):
            continue
        score = (4 if '活动时间' in before else 0) + (2 if span else 0)
        candidates.append((score, dm, span))
    if candidates:
        if sum(1 for _, _, span in candidates if span) > 1:
            issues.append('通知包含多个活动日期或场次，请人工选择后补全')
        _, chosen, span = max(candidates, key=lambda entry: entry[0])
        try:
            activity_date = _date(chosen, observed, issues)
            if span:
                clocks = list(TIME.finditer(span.group()))
                starts = activity_date.replace(hour=_clock(clocks[0])[0], minute=_clock(clocks[0])[1])
                ends = activity_date.replace(hour=_clock(clocks[1])[0], minute=_clock(clocks[1])[1])
                if ends <= starts:
                    issues.append('活动结束时间不晚于开始时间，请核对是否跨日')
            else:
                issues.append('缺少活动起止时刻')
            weekday = re.search(r'(?:周|星期)([一二三四五六日天])', source[chosen.end():chosen.end() + 20])
            if weekday and '一二三四五六日'.index(weekday[1].replace('天', '日')) != activity_date.weekday():
                issues.append('活动日期与星期不一致')
        except ValueError:
            issues.append('活动日期或时刻无效')
            starts = ends = activity_date = None
    else:
        issues.append('缺少活动日期及起止时间')

    opens = closes = None
    # Resolve each keyword within its own clause, preventing an activity range
    # from being accidentally used as an undated registration window.
    previous_marker_end = 0
    markers = list(MARKER.finditer(source))
    for index, marker in enumerate(markers):
        is_close = marker.group() in ('截止', '结束报名', '报名结束')
        start = max(source.rfind('\n', 0, marker.start()) + 1,
                    source.rfind('，', 0, marker.start()) + 1,
                    source.rfind(',', 0, marker.start()) + 1,
                    source.rfind('。', 0, marker.start()) + 1,
                    previous_marker_end)
        end = len(source)
        for separator in ('\n', '，', ',', '。', ';', '；'):
            at = source.find(separator, marker.end())
            if at >= 0:
                end = min(end, at)
        if index + 1 < len(markers):
            end = min(end, markers[index + 1].start())
        before, after = source[start:marker.start()], source[marker.end():end]
        activity_ranges = list(RANGE.finditer(before))
        if activity_ranges:
            before = before[activity_ranges[-1].end():]
        before_times = list(TIME.finditer(before))
        after_time = TIME.search(after)
        clock = before_times[-1] if before_times else after_time
        fragment = before if before_times else after
        previous_marker_end = marker.end()
        if clock is None:
            continue
        try:
            hour, minute = _clock(clock)
            dates = list(DATE.finditer(fragment[:clock.start()]))
            relative_context = fragment[:clock.start()]
            if dates:
                day = _date(dates[-1], activity_date or observed, issues)
            elif re.search(r'第二天|次日|翌日', relative_context) and is_close and opens:
                day = opens.replace(hour=0, minute=0, second=0, microsecond=0) + timedelta(days=1)
            elif re.search(r'前一天|前1天|前一日', relative_context) and activity_date:
                day = activity_date - timedelta(days=1)
            elif re.search(r'当天|当日', relative_context) and activity_date:
                day = activity_date
            elif is_close and opens:
                day = opens.replace(hour=0, minute=0, second=0, microsecond=0)
            else:
                continue
            value = day.replace(hour=hour, minute=minute, second=0, microsecond=0)
            if is_close:
                if closes is not None and closes != value:
                    issues.append('通知包含相互冲突的报名截止时间')
                closes = value
            else:
                if opens is not None and opens != value:
                    issues.append('通知包含相互冲突的报名开放时间')
                opens = value
        except ValueError:
            issues.append('报名日期或时刻无效')
    if not opens:
        issues.append('缺少明确的报名开放日期和时刻')
    if not closes:
        issues.append('缺少明确的报名截止日期和时刻')
    if opens and closes and closes <= opens:
        issues.append('报名截止时间不晚于开放时间，不能自动推定跨日')
    if activity_date and opens and opens.date() > activity_date.date():
        issues.append('报名开放时间晚于活动日期')
    if starts and closes and closes > starts:
        issues.append('报名截止时间晚于活动开始，请核对')

    rewards = {}
    for key, label in [('credit', r'(?:思政)?学分'), ('cloud', r'(?:智能云|学院积分)')]:
        match = re.search(r'(\d+(?:\.\d+)?)\s*' + label, source)
        if not match:
            match = re.search(label + r'\s*[:：]?\s*(\d+(?:\.\d+)?)', source)
        if match:
            rewards[key] = match[1]
    capacity = re.search(r'(?:招募人数|活动人数|人数|名额)\s*[:：]?\s*(\d+)\s*(?:人|名)?', source)
    date_identity = activity_date.date().isoformat() if activity_date else (chosen.group() if chosen else title)
    identity = f'{form_url}|{date_identity}'
    if not form_url:
        title_identity = re.sub(r'\s+', '', unicodedata.normalize('NFKC', title)).casefold()
        identity = f'no-url|{category}|{title_identity}|{date_identity}'
    return dict(id=hashlib.sha256(identity.encode()).hexdigest()[:24], title=title,
                category=category, starts_at=_iso(starts), ends_at=_iso(ends),
                opens_at=_iso(opens), closes_at=_iso(closes),
                location=_location(source), activity_date=activity_date.date().isoformat() if activity_date else None,
                capacity=int(capacity[1]) if capacity else None, rewards=rewards,
                form_url=form_url, raw_text=text, issues=list(dict.fromkeys(issues)),
                observed_at=observed.isoformat())
