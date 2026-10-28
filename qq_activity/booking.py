# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Conservative WPS UI executor. All Playwright objects live on the worker thread.

Only a positive, visible confirmation counts as success. No request replay, captcha
solving, encrypted endpoint assumptions, or retries after a possible submission.
"""
from __future__ import annotations

import queue
import json
import html
import re
import threading
import time
import unicodedata
from datetime import datetime, timedelta, timezone
from pathlib import Path
from urllib.parse import urlsplit


def _normalized(value):
    return re.sub(r"[\s\W_]+", "", unicodedata.normalize("NFKC", str(value)).lower())


def match_field(title: str) -> str | None:
    text = unicodedata.normalize("NFKC", str(title)).strip()
    text = re.sub(r"^\s*\d+\s*[.、)）:：]\s*", "", text)
    text = re.sub(r"[（(]必填[）)]", "", text)
    # WPS sample documents the class format inside a parenthesis in its title.
    if re.fullmatch(r"班级\s*[（(][^()（）]*[）)]?\s*[*＊]?", text) and not re.search(r"姓名|学号|手机号|邮箱", text):
        text = "班级"
    if re.fullmatch(r"\d+(?:\.\d+)?\s*思政学分\s*(?:或|/)\s*\d+(?:\.\d+)?\s*(?:智能云|学院积分)", text):
        text = "奖励"
    text = _normalized(text)
    text = re.sub(r"^(?:请填写|请输入|请填|填写|输入|请选择|选择)?(?:您的|你的|本人)?", "", text)
    aliases = {
        "name": {"姓名", "名字", "学生姓名"},
        "major": {"专业", "所学专业", "专业名称"},
        "class_name": {"班级", "所在班级", "行政班级", "班级名称"},
        "student_id": {"学号", "学生学号"},
        "phone": {"手机", "手机号", "手机号码", "电话", "电话号码", "联系电话", "联系方式"},
        "email": {"邮箱", "电子邮箱", "电子邮件", "邮箱地址", "电子邮箱地址", "email"},
        "reward": {"奖励", "奖励选择", "奖励类型", "奖励方式", "奖励选项", "选择奖励", "奖励偏好", "奖励形式"},
    }
    found = [field for field, names in aliases.items() if text in names]
    return found[0] if len(found) == 1 else None


def choose_option(options, profile_value: str, kind: str) -> str | None:
    """Return the one actual option matching a semantic preference; never guess."""
    options = list(options)
    wanted = _normalized(profile_value)
    if kind == "major":
        canonical = lambda text: _normalized(text).replace("电气工程及其自动化", "电气工程及自动化")
        matches = [text for text in options if canonical(text) == canonical(profile_value)]
    elif kind == "reward":
        def category(text):
            credit = bool(re.search(r"思政|学分", text))
            cloud = "智能云" in text or "学院积分" in text
            return "credit" if credit and not cloud else "cloud" if cloud and not credit else None
        desired = wanted if wanted in {"credit", "cloud"} else category(str(profile_value))
        matches = [text for text in options if desired and category(str(text)) == desired]
    else:
        matches = [text for text in options if _normalized(text) == wanted]
    return matches[0] if len(matches) == 1 else None


def _time(value):
    if value in (None, "", 0, "0"):
        return None
    if isinstance(value, (int, float)) or (isinstance(value, str) and value.isdigit()):
        number = float(value)
        return datetime.fromtimestamp(number / 1000 if number > 100000000000 else number, timezone.utc)
    result = datetime.fromisoformat(str(value).replace("Z", "+00:00"))
    if result.tzinfo is None:
        raise ValueError("timezone missing")
    return result


def _window_status(activity, now=None):
    lifecycle = activity.get("lifecycle")
    if lifecycle == "cancelled":
        return "cancelled", "活动已取消，禁止执行报名"
    if lifecycle == "paused":
        return "attention", "活动已暂停，请核对恢复通知后重新排队"
    if lifecycle == "postponed":
        return "attention", "活动已延期，请核对新的活动及报名时间"
    now = now or datetime.now(timezone.utc)
    if activity.get("issues"):
        return "attention", "活动资料有待核对项，请先在界面修正"
    try:
        opens, closes = _time(activity.get("opens_at")), _time(activity.get("closes_at"))
        if not opens or not closes or opens >= closes:
            return "attention", "报名窗口缺失或冲突"
        if now >= closes:
            return "expired", "通知中的报名截止时间已过"
        if now < opens:
            return "attention", "尚未到通知中的报名开放时间"
    except (ValueError, TypeError, OverflowError, OSError):
        return "attention", "无法核对报名时间及其时区"
    return None


def _settings_status(settings, activity, now=None):
    now = now or datetime.now(timezone.utc)
    if not isinstance(settings, dict):
        return "attention", "无法读取表单实时设置"
    if type(settings.get("checkOnce")) is not bool:
        return "attention", "无法核对表单填写次数限制"
    try:
        for key, activity_key in (("startTime", "opens_at"), ("stopTime", "closes_at")):
            value = _time(settings.get(key))
            if value:
                if key == "stopTime" and now >= value:
                    return "expired", "WPS 表单已截止"
                if key == "startTime" and now < value:
                    return "attention", "WPS 表单尚未开放"
                notice = _time(activity.get(activity_key))
                if notice and abs((notice-value).total_seconds()) > 60:
                    return "attention", "通知和 WPS 表单报名时间不一致"
    except (ValueError, TypeError, OverflowError, OSError):
        return "attention", "无法解释 WPS 表单时间限制"
    return _period_status(settings.get("periodConfig"), activity, now)


def _period_status(config, activity, now):
    """Validate the observed single-day WPS customDate format in Beijing time.

    This deliberately supports one full calendar date, one intraday interval,
    no per-period quota, and no updates. It does not infer recurrence semantics.
    """
    if config is None:
        return None
    manual = ("attention", "表单分时段规则无法完整核对，需要人工处理")
    if not isinstance(config, dict):
        return manual
    if not config.get("checkPeriod"):
        return None
    if (config.get("checkPeriod") is not True or config.get("periodType") != "customDate"
            or config.get("periodConfigType") != "custom" or config.get("checkLimit") != 0
            or config.get("checkUpdateData") is not False
            or config.get("isAllowOutOfPeriod") is not False):
        return manual
    dates, times = config.get("customDates"), config.get("periodTimes")
    if not isinstance(dates, list) or len(dates) != 1 or not isinstance(times, list) or len(times) != 1:
        return manual
    if not isinstance(dates[0], dict) or not isinstance(times[0], dict):
        return manual
    interval = times[0]
    if interval.get("commitOptionId") not in (None, ""):
        return manual
    keys = ("startHour", "startMinute", "endHour", "endMinute")
    if any(type(interval.get(key)) is not int for key in keys):
        return manual
    start_hour, start_minute, end_hour, end_minute = [interval[key] for key in keys]
    if not (0 <= start_hour <= 23 and 0 <= end_hour <= 23 and 0 <= start_minute <= 59 and 0 <= end_minute <= 59):
        return manual
    try:
        beijing = timezone(timedelta(hours=8))
        day_start = _time(dates[0].get("startTs"))
        day_end = _time(dates[0].get("endtTs"))
        if day_start is None or day_end is None:
            return manual
        day_start, day_end = day_start.astimezone(beijing), day_end.astimezone(beijing)
        midnight = day_start.replace(hour=0, minute=0, second=0, microsecond=0)
        if day_start != midnight or day_end != midnight + timedelta(days=1, milliseconds=-1):
            return manual
        opening = midnight.replace(hour=start_hour, minute=start_minute)
        closing = midnight.replace(hour=end_hour, minute=end_minute)
        if opening >= closing:
            return manual
        notice_open, notice_close = _time(activity.get("opens_at")), _time(activity.get("closes_at"))
        if notice_open != opening or notice_close != closing:
            return "attention", "通知和 WPS 单日分时段报名窗口不一致"
        if now >= closing:
            return "expired", "WPS 单日分时段报名窗口已截止"
        if now < opening:
            return "attention", "尚未到 WPS 单日分时段报名开放时间"
    except (ValueError, TypeError, OverflowError, OSError):
        return manual
    return None


def _form_code(activity):
    url = urlsplit(activity.get("form_url") or "")
    match = re.fullmatch(r"/(?:g/|ksform/w/write/)([A-Za-z0-9]+)/?", url.path)
    return match.group(1) if url.hostname == "f.wps.cn" and match else None


def _once_conflicts(current, activity, jobs, activities):
    """Return (reason, pending job ids) for one persistent account's once limit.

    Completed/uncertain answers for a reused form remain blocking across dates:
    changing the collection window does not prove the server reset its limit.
    Disjoint future windows are excluded only from the pending-person count.
    """
    code = _form_code(activity)
    pending, prior = [], False
    for job in jobs:
        other = activities.get(job["activity_id"])
        if not other or _form_code(other) != code:
            continue
        if job["id"] != current["id"] and job["status"] in {"success", "uncertain"}:
            prior = True
        if job["status"] not in {"queued", "running"}:
            continue
        try:
            opening, closing = _time(activity.get("opens_at")), _time(activity.get("closes_at"))
            other_open, other_close = _time(other.get("opens_at")), _time(other.get("closes_at"))
            disjoint = all((opening, closing, other_open, other_close)) and (other_close <= opening or closing <= other_open)
        except (ValueError, TypeError, OverflowError, OSError):
            disjoint = False
        if not disjoint:
            pending.append(job["id"])
    if prior:
        return "表单开启一次填写限制；同一登录资料已有该表单成功或待核实记录，需人工核对；更换报名日期不代表限制已重置", pending
    if len(pending) > 1:
        return "表单开启一次填写限制；账号多人填报需核对，所有相关任务均暂停，请只选择一人后重新排队", pending
    return None


def _public_constraints(form):
    """Only public settings and counts may be persisted by read-only preflight."""
    settings = form["settings"]
    if type(settings.get("checkOnce")) is not bool or type(settings.get("checkLogin")) is not bool:
        raise _Attention("表单登录或填写次数设置无法核对")
    limit, count = settings.get("fillLimit", 0), form.get("count")
    if type(limit) is not int or limit < 0 or (count is not None and (type(count) is not int or count < 0)):
        raise _Attention("表单收集份数设置无法核对")
    values = {}
    for source, target in (("startTime", "start_ms"), ("stopTime", "stop_ms")):
        value = _time(settings.get(source))
        values[target] = round(value.timestamp()*1000) if value else None
    return {"check_once": settings["checkOnce"], "check_login": settings["checkLogin"],
            "fill_limit": limit, "count": count, **values,
            "checked_at": datetime.now(timezone.utc).isoformat()}


def _public_profile_fields(form):
    """Extract only actual public major/class questions, never inferred answers."""
    questions = form.get("questions")
    if not isinstance(questions, list) or not questions:
        raise _Attention("表单专业和班级题目结构无法核对")
    fields = {}
    for question in questions:
        if not isinstance(question, dict):
            raise _Attention("表单题目结构异常，预检资料未更新")
        title = question.get("title")
        field = match_field(title)
        if field not in {"major", "class_name"}:
            continue
        if field in fields:
            raise _Attention("表单有重复的专业或班级题目，需要人工核对")
        if not isinstance(title, str) or not title.strip() or question.get("hidden") or question.get("readonly"):
            raise _Attention("专业或班级题目不可编辑或结构异常")
        options = question.get("options")
        if not isinstance(options, list) or any(not isinstance(value, str) or not value.strip() for value in options):
            raise _Attention("专业或班级实际选项无法核对")
        if len({_normalized(option) for option in options}) != len(options):
            raise _Attention("专业或班级选项重复，需要人工核对")
        if question.get("type") == "select" and options:
            kind = "choice"
        elif question.get("type") == "input" and not options:
            kind = "text"
        else:
            raise _Attention("专业或班级题型不受支持，预检资料未更新")
        entry = {"title": title, "options": list(options), "kind": kind}
        description = question.get("description")
        if isinstance(description, str) and description.strip():
            plain = re.sub(r"<(script|style)\b[^>]*>.*?</\1\s*>", "", description, flags=re.I | re.S)
            plain = html.unescape(re.sub(r"<[^>]+>", "", plain)).strip()[:1000]
            if plain:
                entry["description"] = plain
        fields[field] = entry
    if set(fields) != {"major", "class_name"}:
        raise _Attention("未唯一识别专业和班级题目，预检资料未更新")
    return fields


def _login_page_state(url, visible_text):
    """Classify only official account UI; neither cookies nor redirects prove login."""
    location = urlsplit(url)
    if location.scheme != "https" or location.hostname != "account.wps.cn":
        return "unknown", "未能在官方账户中心确认 WPS 登录状态"
    compact = re.sub(r"\s+", "", visible_text)
    lines = {re.sub(r"\s+", "", line).strip("。！! ") for line in visible_text.splitlines()}
    if re.search(r"请.*验证|拖动滑块|滑块验证|请输入验证码|安全验证|完成验证码", compact):
        return "pending", "WPS 账户页需要验证，尚未确认登录"
    if (lines & {"微信扫码登录", "扫码登录", "账号密码登录", "手机号登录", "手机号码登录"}
            or "使用金山办公在线服务账号登录" in compact):
        return "offline", "WPS 账户页显示登录入口，当前未确认登录"
    if lines & {"退出登录", "退出帐号", "退出账号", "已登录"}:
        return "online", "已在 WPS 官方账户中心确认登录"
    # The current account center also lives at / and does not always expose a
    # logout control. Require the combined private account-management sections;
    # a redirect, a cookie, or a single generic label is not login evidence.
    if all(marker in compact for marker in ("账号昵称", "绑定手机", "绑定账号")) and "账号安全评分" in compact:
        return "online", "已在 WPS 官方账户中心确认登录"
    center_route = bool(re.match(r"^/(?:usercenter|accountcenter)(?:/|$)", location.path.lower()))
    management = sum(marker in compact for marker in ("账号安全", "账户安全", "个人资料", "个人信息", "安全设置", "账号管理", "账户管理", "账号信息", "绑定手机"))
    if center_route and management >= 2:
        return "online", "已在 WPS 官方账户中心确认账户管理页面"
    return "unknown", "自动核验未能识别 WPS 账户页，请点击刷新重试，或打开 WPS 登录窗口核对"


# Read only selected public form metadata; never copy account tokens or answers.
_FORM_SNAPSHOT = r"""() => {
 const roots = [...document.querySelectorAll('#root, #app')];
 let result = null;
 const seen = new Set();
 function walk(f) {
   if (!f || seen.has(f) || result || seen.size > 50000) return;
   seen.add(f);
   const form = f.memoizedProps?.form;
   if (form?.setting?.baseSetting && Array.isArray(form.questions) && form.questions.length > 0) {
     result = {settings: form.setting.baseSetting,
       count: form.baseInfo?.count ?? form.baseInfo?.countInfo?.count ?? null,
       questions: form.questions.map(q => ({qid:q.qid, title:q.title || q.baseInfo?.title || '',
         type:q.type, required:!!q.baseInfo?.need, noRepeat:!!q.baseInfo?.isNoRepeat,
         hidden:!!q.baseInfo?.isHidden, readonly:!!q.baseInfo?.isReadonly,
         description:typeof q.description === 'string' ? q.description :
           typeof q.baseInfo?.note === 'string' ? q.baseInfo.note : '',
         options:q.selectInfo?.selects?.map(o=>o.val) || []}))};
     return;
   }
   walk(f.child); walk(f.sibling);
 }
 for (const r of roots) {
   walk(r._reactRootContainer?._internalRoot?.current);
   for (const key of Object.keys(r).filter(k=>k.startsWith('__reactContainer'))) {
     walk(r[key]?.current || r[key]);
   }
 }
 return result;
}"""

_DOM_QUESTIONS = r"""() => [...document.querySelectorAll('.ksapc-questions-write-container')].map(q => ({
 id:q.id, title:q.querySelector('.ksapc-question-title-title')?.innerText || '',
 required:!!q.querySelector('.ksapc-question-title-required, [required], [aria-required="true"]'),
 visible:!!(q.offsetWidth || q.offsetHeight || q.getClientRects().length),
 inputs:[...q.querySelectorAll('input:not([type=hidden]), textarea, select, [contenteditable=true]')].map(e=>({
   tag:e.tagName.toLowerCase(), type:e.type || '', value:e.value ?? e.innerText,
   checked:!!e.checked, disabled:!!e.disabled, readonly:!!e.readOnly,
   text:e.closest('label')?.innerText?.trim() || '',
   options:e.tagName==='SELECT'?[...e.options].filter(o=>o.value).map(o=>o.textContent.trim()):[]
 }))
}))"""


class _Attention(Exception):
    def __init__(self, detail, status="attention"):
        self.status, self.detail = status, detail
        super().__init__(detail)


class BrowserWorker:
    """Queue-driven owner for the sync Playwright API and a dedicated Edge profile."""
    def __init__(self, store, data_dir, on_status=None):
        self.store = store
        self.data_dir = Path(data_dir)
        self.on_status = on_status
        self._commands = queue.Queue()
        self._pending_inspections = {}
        self._stop = threading.Event()
        self._mode_change_pending = threading.Event()
        self._thread = None
        self._context = None
        self._playwright = None
        self._login_mode = False
        self._profile_lock = None
        self._last_notice = None
        self._login_status_lock = threading.Lock()
        self._login_snapshot = {"state": "unknown", "checked_at": None, "detail": "尚未检查 WPS 登录状态"}
        self._login_check_pending = True
        self._login_check_running = False
        self._next_login_check = 0.0

    def login_status(self):
        """Thread-safe memory snapshot; safe to poll from Tk without any I/O."""
        with self._login_status_lock:
            if self._login_snapshot["state"] == "online":
                try:
                    stale = (datetime.now(timezone.utc)-_time(self._login_snapshot["checked_at"])).total_seconds() >= 300
                except (ValueError, TypeError, OverflowError, OSError):
                    stale = True
                if stale:
                    self._login_snapshot = {**self._login_snapshot, "state": "unknown", "detail": "WPS 登录状态已过期，等待重新核对"}
            return {**self._login_snapshot, "login_window_open": bool(self._login_mode)}

    def _set_login_status(self, state, detail, checked=False):
        with self._login_status_lock:
            if self._stop.is_set():
                self._login_snapshot = {"state": "unknown", "checked_at": None, "detail": "WPS 状态检查已停止"}
            else:
                checked_at = datetime.now(timezone.utc).isoformat() if checked else self._login_snapshot["checked_at"]
                self._login_snapshot = {"state": state, "checked_at": checked_at, "detail": detail}

    def check_login(self):
        """Request one low-priority UI check; never navigate or block the caller."""
        self.start()
        with self._login_status_lock:
            duplicate = self._login_check_pending or self._login_check_running
            if not duplicate:
                self._login_check_pending = True
            # Keep this write in the same critical section as the pending flag,
            # so a completed owner-thread check cannot be overwritten afterward.
            self._login_snapshot = {**self._login_snapshot, "state": "checking", "detail": "WPS 登录检查已排队，报名任务优先"}
        if not duplicate:
            self._commands.put("check_login")

    def _notify(self, text):
        if text == self._last_notice:
            return
        self._last_notice = text
        if self.on_status:
            try:
                self.on_status(text)
            except Exception:
                pass  # UI callbacks must not interrupt persistence or submission handling.

    def start(self):
        if self._thread and self._thread.is_alive():
            return
        self._stop.clear()
        self._mode_change_pending.clear()
        self._commands = queue.Queue()
        self._pending_inspections = {}
        with self._login_status_lock:
            self._login_check_pending = True
            self._next_login_check = 0.0
        self._thread = threading.Thread(target=self._run, name="wps-browser-owner", daemon=True)
        self._thread.start()

    def stop(self, timeout=3):
        self._stop.set()
        self._set_login_status("unknown", "WPS 状态检查已停止")
        self._commands.put("stop")
        if self._thread and self._thread is not threading.current_thread():
            self._thread.join(timeout=timeout)

    def open_login(self):
        self.start()
        self._mode_change_pending.set()
        self._set_login_status("pending", "请在 WPS 登录窗口完成登录，尚未确认在线")
        with self._login_status_lock:
            self._login_check_pending = True
        self._commands.put("login")

    def finish_login(self):
        self.start()
        self._mode_change_pending.set()
        self._set_login_status("checking", "完成登录后正在重新核对 WPS 官方账户页")
        with self._login_status_lock:
            self._login_check_pending = True
        self._commands.put("finish_login")

    def inspect_activity(self, activity_id):
        """Enqueue public form preflight and return immediately; never read profiles."""
        self.start()
        self._commands.put(("inspect", activity_id))

    def _lock_profile(self):
        self.data_dir.mkdir(parents=True, exist_ok=True)
        handle = (self.data_dir / "browser-owner.lock").open("a+b")
        handle.seek(0)
        if not handle.read(1):
            handle.write(b"0")
            handle.flush()
        handle.seek(0)
        try:
            import os
            if os.name == "nt":
                import msvcrt
                msvcrt.locking(handle.fileno(), msvcrt.LK_NBLCK, 1)
            else:
                import fcntl
                fcntl.flock(handle, fcntl.LOCK_EX | fcntl.LOCK_NB)
        except (OSError, ImportError):
            handle.close()
            raise _Attention("同一登录资料已有浏览器执行器运行")
        self._profile_lock = handle

    def _open_context(self, headed=False):
        if self._context is not None:
            self._context.close()
            self._context = None
        if self._playwright is None:
            from playwright.sync_api import sync_playwright
            self._playwright = sync_playwright().start()
        context = self._playwright.chromium.launch_persistent_context(
            str(self.data_dir / "edge-profile"), channel="msedge", headless=not headed,
            locale="zh-CN", timezone_id="Asia/Shanghai", accept_downloads=False,
            timeout=25000, handle_sigint=False, handle_sigterm=False, handle_sighup=False,
        )
        context.set_default_timeout(7000)
        context.set_default_navigation_timeout(25000)
        self._context = context
        context.on("close", lambda: self._set_login_status("unknown", "WPS 浏览器已关闭，登录状态未确认"))
        return context

    def _run(self):
        try:
            self._lock_profile()
            self._notify("报名执行器就绪，等待已选择的任务")
            while not self._stop.is_set():
                try:
                    commands = [self._commands.get(timeout=.2)]
                except queue.Empty:
                    commands = []
                # Drain a bounded batch so low-priority duplicate preflights do
                # not hide login/stop commands or consume a scheduling tick each.
                for _ in range(255):
                    try:
                        commands.append(self._commands.get_nowait())
                    except queue.Empty:
                        break
                for command in commands:
                    if command == "stop" or self._stop.is_set():
                        self._stop.set()
                        break
                    if isinstance(command, tuple) and command[0] == "inspect":
                        self._pending_inspections[command[1]] = None
                        continue
                    try:
                        if command == "login":
                            self._login_mode = True
                            context = self._open_context(headed=True)
                            page = context.new_page()
                            page.goto("https://account.wps.cn/", wait_until="domcontentloaded")
                            self._notify("请在 Edge 中完成 WPS 登录；程序每 2 秒自动核验，确认后恢复后台报名")
                        elif command == "finish_login":
                            self._open_context(headed=False)
                            self._login_mode = False
                            self._notify("已保存浏览器资料并恢复后台；实际登录状态在报名时核对")
                    except Exception:
                        self._notify("WPS 浏览器无法打开或已关闭，请检查系统 Edge 后重新操作")
                    finally:
                        if command in ("login", "finish_login"):
                            self._mode_change_pending.clear()
                if self._stop.is_set():
                    break
                with self._login_status_lock:
                    if time.monotonic() >= self._next_login_check:
                        self._login_check_pending = True
                    check_pending = self._login_check_pending
                if self._login_mode:
                    if check_pending:
                        self._perform_login_check()
                    continue
                if self._mode_change_pending.is_set():
                    continue
                for job in self.store.due_jobs():
                    if self._stop.is_set() or self._mode_change_pending.is_set():
                        break
                    # The store performs queued -> running in a single SQL statement.
                    if self.store.claim_job(job["id"]):
                        self._execute_job(job["id"])
                if self._stop.is_set() or self._login_mode or self._mode_change_pending.is_set():
                    continue
                # Account status should not wait behind the public-form scans
                # requested by a page refresh. Due bookings still run first.
                with self._login_status_lock:
                    check_pending = self._login_check_pending
                if (check_pending and not self._stop.is_set() and not self._mode_change_pending.is_set()
                        and not self._inspection_imminent()):
                    self._perform_login_check()
                if self._pending_inspections:
                    if self._inspection_imminent():
                        self._notify("临近报名窗口，优先处理报名；表单资料稍后读取")
                    else:
                        activity_id = next(iter(self._pending_inspections))
                        if self._inspect_activity(activity_id) is not False:
                            self._pending_inspections.pop(activity_id, None)
        except _Attention as exc:
            self._notify(exc.detail)
        except Exception:
            self._notify("报名执行器停止：本地状态或浏览器初始化失败，请检查后重启工具")
        finally:
            try:
                if self._context:
                    self._context.close()
            except Exception:
                pass
            try:
                if self._playwright:
                    self._playwright.stop()
            except Exception:
                pass
            self._context = self._playwright = None
            self._login_mode = False
            self._set_login_status("unknown", "WPS 执行器已停止，登录状态未确认")
            if self._profile_lock:
                self._profile_lock.close()
                self._profile_lock = None

    def _perform_login_check(self):
        page, own_page = None, False
        with self._login_status_lock:
            self._login_check_pending = False
            self._login_check_running = True
        try:
            if self._login_mode:
                self._set_login_status("pending", "WPS 登录窗口保持打开，等待完成登录")
                pages = self._context.pages if self._context is not None else []
                page = next((candidate for candidate in reversed(pages)
                             if urlsplit(candidate.url).hostname == "account.wps.cn"), None)
                if page is None:
                    self._set_login_status("pending", "尚未在登录窗口确认 WPS 账户，请完成后点击完成登录")
                    return
            else:
                if self._inspection_imminent():
                    with self._login_status_lock:
                        self._login_check_pending = True
                    self._set_login_status("checking", "临近报名窗口，WPS 状态稍后检查")
                    return
                self._set_login_status("checking", "正在读取 WPS 官方账户页确认登录状态")
                if self._context is None:
                    self._open_context(headed=False)
                if self._stop.is_set():
                    return
                page = self._context.new_page()
                own_page = True
                page.goto("https://account.wps.cn/", wait_until="domcontentloaded", timeout=10000)
                page.wait_for_function("() => document.body && document.body.innerText.trim().length > 0", timeout=5000)
                page.wait_for_timeout(300)
            state, detail = self._read_login_page(page, wait=own_page)
            if self._login_mode:
                if state == "online" and not self._mode_change_pending.is_set():
                    self._set_login_status("checking", "登录已完成，正在核验后台登录状态")
                    self._open_context(headed=False)
                    self._login_mode = False
                    page = self._context.new_page()
                    own_page = True
                    page.goto("https://account.wps.cn/", wait_until="domcontentloaded", timeout=10000)
                    state, detail = self._read_login_page(page, wait=True)
                    if state == "online":
                        self._notify("WPS 登录已自动确认，已恢复后台报名")
                else:
                    state, detail = "pending", "程序每 2 秒自动核验；请在已打开的 WPS 窗口完成登录或验证"
            elif self._mode_change_pending.is_set():
                state, detail = "pending", "WPS 登录模式正在切换，等待重新核对"
            self._set_login_status(state, detail, checked=True)
        except Exception:
            self._set_login_status("error", "WPS 官方账户页无法读取，登录状态未确认", checked=True)
        finally:
            if own_page and page is not None:
                try:
                    page.close()
                except Exception:
                    pass
            with self._login_status_lock:
                self._login_check_running = False
                interval = 2 if self._login_mode else (30 if self._login_snapshot["state"] in {"unknown", "error"} else 240)
                self._next_login_check = time.monotonic()+interval

    def _read_login_page(self, page, wait=False):
        # The account UI renders after DOMContentLoaded. Give it a bounded
        # chance to render instead of classifying a temporary empty shell.
        deadline = time.monotonic() + (5 if wait else 0)
        while True:
            state, detail = _login_page_state(page.url, page.locator("body").inner_text(timeout=3000))
            if state != "unknown" or self._stop.is_set() or time.monotonic() >= deadline:
                return state, detail
            page.wait_for_timeout(250)

    def _inspection_imminent(self, now=None):
        now = now or datetime.now(timezone.utc)
        for job in self.store.jobs():
            if job.get("status") != "queued":
                continue
            activity = self.store.get_activity(job["activity_id"])
            if not activity:
                continue
            try:
                opening, closing = _time(activity.get("opens_at")), _time(activity.get("closes_at"))
                if opening and closing and closing > now and opening <= now+timedelta(seconds=60):
                    return True
            except (ValueError, TypeError, OverflowError, OSError):
                continue  # due_jobs handles malformed windows; never navigate here.
        return False

    def _inspect_activity(self, activity_id):
        if self._login_mode:
            self._notify("请先点击完成 WPS 登录，再执行表单预检；当前登录窗口保持打开")
            return False
        if self._inspection_imminent():
            self._notify("临近报名窗口，优先处理报名；表单资料稍后读取")
            return False
        page = None
        try:
            activity = self.store.get_activity(activity_id)
            if not activity:
                raise _Attention("预检活动不存在")
            url = urlsplit(activity.get("form_url") or "")
            if url.scheme != "https" or url.hostname != "f.wps.cn" or not _form_code(activity) or url.username or url.password:
                raise _Attention("表单预检仅支持已核对的 WPS 链接")
            if self._context is None:
                self._open_context(headed=False)
            page = self._context.new_page()
            page.goto(activity["form_url"], wait_until="domcontentloaded", timeout=10000)
            page.wait_for_function(_FORM_SNAPSHOT, timeout=10000)
            form = page.evaluate(_FORM_SNAPSHOT)
            if not form:
                raise _Attention("无法读取表单公共设置，预检未更新")
            constraints = _public_constraints(form)
            profile_fields = _public_profile_fields(form)
            current = self.store.get_activity(activity_id)
            if not current or current.get("form_url") != activity.get("form_url"):
                raise _Attention("预检期间活动链接发生变化，请重新预检")
            self.store.update_activity(activity_id, {"form_constraints": constraints, "profile_fields": profile_fields})
            self._notify("WPS 表单预检已更新；仅核对公共设置，未填写或提交")
        except _Attention as exc:
            self._notify(exc.detail)
        except Exception:
            self._notify("WPS 表单预检失败，未更新公共设置；请检查网络后重试")
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass

    def _enforce_account_once(self, job, activity, form):
        if form["settings"].get("checkOnce") is not True:
            return
        activities = {entry["id"]: entry for entry in self.store.activities()}
        conflict = _once_conflicts(job, activity, self.store.jobs(), activities)
        if conflict:
            detail, affected = conflict
            for identifier in affected:
                pending = self.store.get_job(identifier)
                if pending and pending["status"] in {"queued", "running"}:
                    self.store.update_job(identifier, "attention", detail)
            raise _Attention(detail)

    def _load_job(self, job_id):
        job = self.store.get_job(job_id)
        if not job or job["status"] != "running":
            raise _Attention("任务状态已变更，停止自动提交", "cancelled")
        activity = self.store.get_activity(job["activity_id"])
        profile = self.store.get_profile(job["profile_id"])
        if not activity or not profile:
            raise _Attention("活动或人员档案已删除")
        guard = _window_status(activity)
        if guard:
            raise _Attention(guard[1], guard[0])
        url = urlsplit(activity.get("form_url") or "")
        if url.scheme != "https" or url.hostname != "f.wps.cn" or not re.fullmatch(r"/g/[A-Za-z0-9]+/?", url.path) or url.username or url.password:
            raise _Attention("仅支持经过核对的 WPS 表单链接")
        reward = job.get("reward") or profile.get("reward")
        if reward not in {"credit", "cloud"}:
            raise _Attention("奖励偏好需要选择思政学分或学院积分")
        profile = {**profile, "reward": reward}
        for key in ("name", "major", "class_name", "student_id", "phone", "email"):
            if not isinstance(profile.get(key), str) or not profile[key].strip():
                raise _Attention("人员档案缺少必要字段，请补全后重排任务")
        if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", profile["email"]):
            raise _Attention("人员邮箱格式需要人工核对")
        if not re.fullmatch(r"\+?[0-9 ()-]{7,20}", profile["phone"]):
            raise _Attention("人员手机号格式需要人工核对")
        # Two profile records can still refer to the same student. A second
        # record must not bypass the activity/person duplicate protection.
        for other in self.store.jobs():
            if (other["id"] != job_id and other["activity_id"] == job["activity_id"]
                    and other["status"] in {"running", "success", "uncertain"}):
                previous = self.store.get_profile(other["profile_id"])
                if previous and previous.get("student_id", "").strip() == profile["student_id"].strip():
                    raise _Attention("同一学号已有执行、成功或待核实记录，禁止重复提交")
        return job, activity, profile

    def _visible_status(self, page, after_click=False):
        # Visible body text only: do not mistake cached/hidden success markup for a result.
        text = page.locator("body").inner_text(timeout=5000)
        compact = re.sub(r"\s+", "", text)
        if page.locator("iframe[src*='captcha' i]:visible, iframe[title*='验证码']:visible, input[autocomplete='one-time-code']:visible").count():
            return "attention", "需要验证码或短信验证，请人工处理"
        if re.search(r"验证码|滑块验证|拖动滑块|请完成验证|安全验证|短信校验", compact):
            return "attention", "需要验证码或安全验证，请人工处理"
        if re.search(r"需要登录|请先登录|登录后(?:才能|才可)|登录才能", compact):
            self._set_login_status("offline", "WPS 表单要求重新登录，当前登录状态已失效", checked=True)
            return "login_required", "WPS 登录已失效或该表单需要登录"
        if re.search(r"已暂停收集|停止收集|暂停收集|不再接受|已关闭收集", compact):
            return "attention", "WPS 表单已暂停或停止收集"
        if re.search(r"达到上限|已达上限|已满|名额已满|份数上限", compact):
            return "full", "WPS 表单已达到收集上限"
        if re.search(r"已截止|已经截止|已结束收集|收集已结束|填写已结束", compact):
            return "expired", "WPS 表单已截止"
        if re.search(r"尚未开始|还未开始|未到开始时间|尚未开放", compact):
            return "attention", "WPS 表单尚未开放"
        if re.search(r"已填写过|已经填写|重复提交|不能重复|只能填写一次", compact):
            return "attention", "WPS 提示已填写或不允许重复提交，请人工核对"
        if after_click:
            # Success must be a standalone visible confirmation, never an option/title.
            lines = [line.strip(" ！!。.") for line in text.splitlines()]
            if any(line in {"提交成功", "填写成功", "已成功提交", "感谢您的填写", "您的填写已提交"} for line in lines):
                if not page.locator(".ksapc-questions-write-container:visible").count():
                    return "success", "WPS 页面已确认提交成功；是否录取需等待活动方审核"
        return None

    def _inspect(self, page, activity):
        status = self._visible_status(page)
        if status:
            raise _Attention(status[1], status[0])
        form = page.evaluate(_FORM_SNAPSHOT)
        if not form:
            raise _Attention("无法读取 WPS 表单设置，未尝试提交")
        settings = form["settings"]
        status = _settings_status(settings, activity)
        if status:
            raise _Attention(status[1], status[0])
        if settings.get("checkPhone"):
            raise _Attention("表单要求手机验证，需要人工处理")
        if settings.get("onePageOneQuestion") or settings.get("channel", {}).get("isOnlyChannelAccess"):
            raise _Attention("该表单使用分页或限定渠道，需人工填写")
        try:
            limit = int(settings.get("fillLimit") or 0)
            count = form.get("count")
            if limit and count is None:
                raise _Attention("无法核对表单实时收集份数")
            if limit and int(count) >= limit:
                raise _Attention("WPS 表单已达到收集上限", "full")
        except (ValueError, TypeError):
            raise _Attention("表单收集份数设置无法核对")
        questions = page.evaluate(_DOM_QUESTIONS)
        if not questions or any(not q["id"] for q in questions):
            raise _Attention("未能识别表单字段结构")
        if len({q["id"] for q in questions}) != len(questions):
            raise _Attention("表单字段标识重复，停止自动填写")
        metadata = {q["qid"]: q for q in form["questions"]}
        visible = [q for q in questions if q["visible"]]
        for q in visible:
            meta = metadata.get(q["id"])
            if not meta or _normalized(q["title"]) != _normalized(meta["title"]):
                raise _Attention("表单字段和设置不一致，需要人工核对")
            q["required"] = q["required"] or meta["required"]
            if meta["readonly"] or (meta["required"] and meta["hidden"]):
                raise _Attention("表单含只读或隐藏必填字段")
            if meta["noRepeat"] and match_field(q["title"]) not in {"name", "student_id", "phone", "email"}:
                raise _Attention("表单对非个人身份字段设置去重限制，需要人工核对")
            if meta["type"] not in {"input", "select"} and q["required"]:
                raise _Attention("表单有暂不支持的必填题型")
        for meta in metadata.values():
            if meta["required"] and meta["qid"] not in {q["id"] for q in visible}:
                raise _Attention("必填字段不可见，停止自动填写")
        return form, visible

    @staticmethod
    def _question(page, qid):
        return page.locator(".ksapc-questions-write-container[id=" + json.dumps(qid) + "]")

    def _fill(self, page, questions, profile):
        plan, used = [], set()
        for q in questions:
            field = match_field(q["title"])
            if field is None:
                if q["required"] or any(i["checked"] or (i["type"] != "radio" and i["value"]) for i in q["inputs"]):
                    raise _Attention("存在未知必填项或未知预填答案，请人工核对")
                continue
            if field in used:
                raise _Attention("多个问题映射到同一档案字段，需要人工核对")
            used.add(field)
            controls = q["inputs"]
            if not controls or any(i["disabled"] or i["readonly"] for i in controls):
                raise _Attention("字段不可编辑或没有受支持的输入控件")
            value = profile[field]
            if all(i["type"] == "radio" for i in controls):
                options = [i["text"] for i in controls]
                answer = choose_option(options, value, field)
                if answer is None:
                    raise _Attention("选项无法唯一对应档案专业或奖励偏好")
                kind = "radio"
            elif len(controls) == 1 and controls[0]["tag"] == "select":
                answer = choose_option(controls[0]["options"], value, field)
                if answer is None:
                    raise _Attention("下拉选项无法唯一匹配")
                kind = "select"
            elif len(controls) == 1 and controls[0]["type"] in {"text", "textarea", "email", "tel", "number", ""}:
                if field == "reward":
                    raise _Attention("奖励未以实际可选项呈现，需要人工核对数值")
                answer, kind = value, "text"
            else:
                raise _Attention("字段输入控件复杂或不受支持，需要人工处理")
            plan.append((q["id"], field, kind, answer))
        if "name" not in used or "student_id" not in used:
            raise _Attention("表单缺少姓名或学号，无法核对人员身份")
        # Validate every question and option before touching any control.
        for qid, field, kind, answer in plan:
            if self._stop.is_set():
                raise _Attention("执行器已停止，未提交", "cancelled")
            container = self._question(page, qid)
            if kind == "text":
                container.locator("input:not([type=hidden]), textarea").fill(answer)
            elif kind == "radio":
                radios = container.locator("input[type=radio]")
                labels = [radios.nth(i).evaluate("e => e.closest('label')?.innerText.trim() || ''") for i in range(radios.count())]
                if labels.count(answer) != 1:
                    raise _Attention("选项在填写时发生变化")
                radios.nth(labels.index(answer)).check()
            else:
                container.locator("select").select_option(label=answer)
        self._verify_answers(page, plan)
        return plan

    def _verify_answers(self, page, plan):
        planned_ids = {entry[0] for entry in plan}
        for question in page.evaluate(_DOM_QUESTIONS):
            if question["visible"] and question["id"] not in planned_ids:
                if question["required"] or any(i["checked"] or (i["type"] != "radio" and i["value"]) for i in question["inputs"]):
                    raise _Attention("出现未核对的必填项或预填答案，停止提交")
        for qid, field, kind, answer in plan:
            container = self._question(page, qid)
            if kind == "text":
                actual = container.locator("input:not([type=hidden]), textarea").input_value()
            elif kind == "radio":
                checked = container.locator("input[type=radio]:checked")
                if checked.count() != 1:
                    raise _Attention("单选答案数量异常，停止提交")
                actual = checked.evaluate("e => e.closest('label')?.innerText.trim() || ''")
            else:
                actual = container.locator("select option:checked").inner_text().strip()
            if actual != answer:
                raise _Attention("填写后的答案与档案不一致，停止提交")

    def _execute_job(self, job_id):
        page, clicked = None, False
        try:
            job, activity, profile = self._load_job(job_id)
            if self._context is None:
                self._open_context(headed=False)
            page = self._context.new_page()  # Every person starts with a fresh document.
            page.goto(activity["form_url"], wait_until="domcontentloaded")
            page.wait_for_function("() => document.querySelector('.ksapc-questions-write-container') || /需要登录|暂停收集|达到上限/.test(document.body.innerText)", timeout=18000)
            # Let React finish reconciling defaults before inventory and filling.
            page.wait_for_timeout(500)
            form, questions = self._inspect(page, activity)
            self._enforce_account_once(job, activity, form)
            plan = self._fill(page, questions, profile)
            job2, activity2, profile2 = self._load_job(job_id)
            if profile2 != profile or activity2 != activity:
                raise _Attention("活动或人员档案在执行中发生变更，停止提交")
            form2, questions2 = self._inspect(page, activity2)
            self._enforce_account_once(job2, activity2, form2)
            if form2 != form or [(q["id"], q["title"], q["required"]) for q in questions] != [(q["id"], q["title"], q["required"]) for q in questions2]:
                raise _Attention("表单结构或限制在执行中发生变更，停止提交")
            self._verify_answers(page, plan)
            if self._stop.is_set() or self._login_mode or self._mode_change_pending.is_set():
                raise _Attention("浏览器模式即将切换或执行器停止，未提交", "cancelled")
            submit = page.get_by_role("button", name=re.compile(r"^\s*提\s*交\s*$"))
            if submit.count() != 1 or not submit.is_visible() or not submit.is_enabled():
                raise _Attention("提交按钮不可用或不唯一，未提交")
            # Set before invoking click: a click timeout can occur after the server received it.
            clicked = True
            submit.click(timeout=7000)
            deadline = time.monotonic()+12
            while time.monotonic() < deadline:
                status = self._visible_status(page, after_click=True)
                if status:
                    if status[0] == "success":
                        self.store.update_job(job_id, "success", status[1])
                    else:
                        self.store.update_job(job_id, "uncertain", status[1]+"；已点击提交，结果待核实，禁止自动重试")
                    self._notify("已记录一名参与人的报名结果")
                    return
                if self._stop.is_set():
                    break
                page.wait_for_timeout(300)
            raise _Attention("已点击提交但无法确认 WPS 服务端结果，请人工核实；禁止自动重试", "uncertain")
        except _Attention as exc:
            self.store.update_job(job_id, exc.status, exc.detail)
            self._notify(exc.detail)
        except Exception:
            status = "uncertain" if clicked else "failed"
            detail = ("提交动作后浏览器或网络中断，结果待核实；禁止自动重试" if clicked else
                      "提交前浏览器或网络失败，未自动重试；请检查 Edge、网络和表单页面")
            self.store.update_job(job_id, status, detail)
            self._notify(detail)
        finally:
            if page is not None:
                try:
                    page.close()
                except Exception:
                    pass
