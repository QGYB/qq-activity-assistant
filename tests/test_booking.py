# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Semantic matching and controlled-browser tests; never submits to real WPS."""
import unittest
from datetime import datetime, timedelta, timezone
import json
import tempfile
import threading
import time
from pathlib import Path

from qq_activity.booking import match_field, choose_option, _window_status, _settings_status


class MatchingTests(unittest.TestCase):
    def test_fields_match_semantics_in_any_order(self):
        titles = ["3. 学号 *", "您的邮箱", "姓名", "班级", "手机号码", "所学专业", "奖励选择"]
        self.assertEqual([match_field(x) for x in titles],
                         ["student_id", "email", "name", "class_name", "phone", "major", "reward"])

    def test_unknown_and_ambiguous_titles_require_attention(self):
        for title in ["辅导员姓名", "姓名及学号", "紧急联系人手机号", "身份证号", "是否愿意服从调剂"]:
            self.assertIsNone(match_field(title), title)

    def test_major_alias_and_ambiguity(self):
        self.assertEqual(choose_option(["机械工程", "电气工程及其自动化"], "电气工程及自动化", "major"),
                         "电气工程及其自动化")
        self.assertIsNone(choose_option(["电气工程及其自动化", "电气工程及自动化"], "电气工程及自动化", "major"))
        self.assertIsNone(choose_option(["计算机科学", "计算机技术"], "计算机", "major"))

    def test_reward_uses_actual_text_not_fixed_numbers(self):
        for options in [["思政学分1分", "0.3智能云"], ["智能云（0.8）", "思政学分（3）"]]:
            self.assertIn("智能云", choose_option(options, "cloud", "reward"))
            self.assertIn("思政", choose_option(options, "credit", "reward"))
        self.assertIsNone(choose_option(["1学分", "2学分", "0.5智能云"], "credit", "reward"))
        self.assertIsNone(choose_option(["学分和智能云二选一"], "credit", "reward"))
        self.assertEqual(choose_option(["2思政学分", "0.8学院积分"], "cloud", "reward"), "0.8学院积分")
        self.assertEqual(choose_option(["2思政学分", "0.5智能云"], "学院积分", "reward"), "0.5智能云")

    def test_actual_sample_class_instruction(self):
        self.assertEqual(match_field("班级（电气 信安 物联 通信 人工智能+23/24/25/26+A/C+班级）"), "class_name")
        self.assertEqual(match_field("班级（网安 机电 电子 电气 人工智能+23/24/25/26+A/C+班级号"), "class_name")
        self.assertEqual(match_field("2思政学分或0.5智能云"), "reward")


class GuardTests(unittest.TestCase):
    def setUp(self):
        self.now = datetime.now(timezone.utc)
        self.activity = {"opens_at": (self.now-timedelta(hours=1)).isoformat(),
                         "closes_at": (self.now+timedelta(hours=1)).isoformat(), "issues": []}

    def test_never_submit_outside_explicit_window(self):
        self.assertIsNone(_window_status(self.activity, self.now))
        self.assertEqual(_window_status(self.activity, self.now+timedelta(hours=2))[0], "expired")
        self.assertEqual(_window_status(self.activity, self.now-timedelta(hours=2))[0], "attention")
        self.assertEqual(_window_status({**self.activity, "opens_at": None}, self.now)[0], "attention")
        self.assertEqual(_window_status({**self.activity, "issues": ["时间冲突"]}, self.now)[0], "attention")

    def test_lifecycle_blocks_execution_before_time_or_other_issues(self):
        for lifecycle, expected in [("cancelled", "cancelled"), ("paused", "attention"), ("postponed", "attention")]:
            with self.subTest(lifecycle=lifecycle):
                status = _window_status({**self.activity, "lifecycle": lifecycle}, self.now)
                self.assertIsNotNone(status)
                self.assertEqual(status[0], expected)
        self.assertEqual(_window_status({"lifecycle": "cancelled", "issues": ["other issue"]}, self.now)[0], "cancelled")

    def test_form_limits_and_conflicts_fail_closed(self):
        self.assertIsNone(_settings_status({"checkOnce": False, "fillLimit": 10}, self.activity, self.now))
        self.assertEqual(_settings_status({"checkOnce": False, "stopTime": (self.now-timedelta(seconds=1)).timestamp()*1000}, self.activity, self.now)[0], "expired")
        self.assertEqual(_settings_status({"startTime": (self.now+timedelta(seconds=10)).timestamp()*1000}, self.activity, self.now)[0], "attention")
        self.assertEqual(_settings_status({"stopTime": (self.now+timedelta(hours=2)).timestamp()*1000}, self.activity, self.now)[0], "attention")

    def test_unknown_once_limit_blocks_execution(self):
        for settings in [{}, {'checkOnce': None}, {'checkOnce': 0}, {'checkOnce': 'false'}]:
            with self.subTest(settings=settings):
                self.assertEqual(_settings_status(settings, self.activity, self.now)[0], 'attention')

    def periodic_sample(self):
        return {"checkOnce": False, "startTime": 1793088000000, "stopTime": 1793102400000,
                "periodConfig": {"checkPeriod": True, "periodType": "customDate", "checkLimit": 0,
                                 "checkUpdateData": False, "periodTimes": [{"startHour": 16, "startMinute": 0,
                                     "endHour": 20, "endMinute": 0, "commitOptionId": ""}],
                                 "periodConfigType": "custom", "isAllowOutOfPeriod": False,
                                 "customDates": [{"startTs": 1793030400000, "endtTs": 1793116799999}]}}

    def test_known_custom_date_matches_beijing_notice_window(self):
        activity = {"opens_at": "2026-10-27T16:00:00+08:00", "closes_at": "2026-10-27T20:00:00+08:00"}
        now = datetime.fromisoformat("2026-10-27T17:00:00+08:00")
        self.assertIsNone(_settings_status(self.periodic_sample(), activity, now))

    def test_custom_date_before_after_and_conflicting_window(self):
        settings = self.periodic_sample()
        activity = {"opens_at": "2026-10-27T16:00:00+08:00", "closes_at": "2026-10-27T20:00:00+08:00"}
        for now_text, expected in [("2026-10-27T15:59:59+08:00", "attention"), ("2026-10-27T20:00:00+08:00", "expired")]:
            self.assertEqual(_settings_status(settings, activity, datetime.fromisoformat(now_text))[0], expected)
        settings["periodConfig"]["periodTimes"][0]["endHour"] = 19
        self.assertEqual(_settings_status(settings, activity, datetime.fromisoformat("2026-10-27T17:00:00+08:00"))[0], "attention")

    def test_custom_date_enforces_window_even_without_global_time_limits(self):
        settings = self.periodic_sample()
        settings.pop("startTime")
        settings.pop("stopTime")
        activity = {"opens_at": "2026-10-27T16:00:00+08:00", "closes_at": "2026-10-27T20:00:00+08:00"}
        for now_text, expected in [("2026-10-27T15:59:59+08:00", "attention"), ("2026-10-27T20:00:00+08:00", "expired")]:
            status = _settings_status(settings, activity, datetime.fromisoformat(now_text))
            self.assertIsNotNone(status)
            self.assertEqual(status[0], expected)

    def test_other_period_rules_and_malformed_custom_dates_stay_manual(self):
        activity = {"opens_at": "2026-10-27T16:00:00+08:00", "closes_at": "2026-10-27T20:00:00+08:00"}
        now = datetime.fromisoformat("2026-10-27T17:00:00+08:00")
        mutations = [{"periodType": "week"}, {"checkLimit": 1}, {"checkUpdateData": True},
                     {"isAllowOutOfPeriod": True}, {"customDates": []},
                     {"customDates": [{"startTs": 1793030400000, "endtTs": 1793203199999}]},
                     {"periodTimes": [{"startHour": 20, "startMinute": 0, "endHour": 16, "endMinute": 0}]}]
        for mutation in mutations:
            with self.subTest(mutation=mutation):
                settings = self.periodic_sample()
                settings["periodConfig"].update(mutation)
                self.assertEqual(_settings_status(settings, activity, now)[0], "attention")


class AccountOnceTests(unittest.TestCase):
    def test_prior_same_form_success_or_uncertain_blocks_changed_profile(self):
        from qq_activity.booking import _once_conflicts
        activity = {"form_url": "https://f.wps.cn/g/abc/", "opens_at": "2026-10-29T16:00:00+08:00", "closes_at": "2026-10-29T20:00:00+08:00"}
        current = {"id": "new", "activity_id": "a", "profile_id": "new-person", "status": "running"}
        for status in ["success", "uncertain"]:
            prior = {"id": "old", "activity_id": "previous", "profile_id": "old-person", "status": status}
            for previous in [activity, {**activity, "opens_at": "2026-10-28T16:00:00+08:00", "closes_at": "2026-10-28T20:00:00+08:00"}]:
                conflict = _once_conflicts(current, activity, [prior, current], {"a": activity, "previous": previous})
                self.assertIsNotNone(conflict)
                self.assertIn("new", conflict[1])

    def test_pending_same_form_window_blocks_all_no_winner(self):
        from qq_activity.booking import _once_conflicts
        activity = {"form_url": "https://f.wps.cn/g/abc/", "opens_at": "2026-10-29T16:00:00+08:00", "closes_at": "2026-10-29T20:00:00+08:00"}
        current = {"id": "one", "activity_id": "a", "profile_id": "p1", "status": "running"}
        other = {"id": "two", "activity_id": "b", "profile_id": "p2", "status": "queued"}
        self.assertIsNone(_once_conflicts(current, activity, [current], {"a": activity}))
        conflict = _once_conflicts(current, activity, [current, other], {"a": activity, "b": activity})
        self.assertEqual(set(conflict[1]), {"one", "two"})
        different = {**activity, "form_url": "https://f.wps.cn/g/different/"}
        self.assertIsNone(_once_conflicts(current, activity, [current, other], {"a": activity, "b": different}))


class SchedulerPriorityTests(unittest.TestCase):
    def simulation(self, near=False):
        from qq_activity.booking import BrowserWorker
        release, inspected = threading.Event(), threading.Event()
        events = []
        now = datetime.now(timezone.utc)

        class LocalStore:
            def __init__(self):
                self.opening = now+timedelta(seconds=30 if near else -1)
                self.states = {"one": "queued", "two": "queued"}
            def jobs(self):
                return [{"id": key, "activity_id": "activity", "status": state} for key, state in self.states.items()]
            def get_activity(self, activity_id):
                return {"opens_at": self.opening.isoformat(), "closes_at": (now+timedelta(minutes=10)).isoformat()}
            def due_jobs(self):
                return [job for job in self.jobs() if job["status"] == "queued"] if self.opening <= datetime.now(timezone.utc) else []
            def claim_job(self, identifier):
                if self.states[identifier] != "queued":
                    return False
                self.states[identifier] = "running"
                return True

        class LocalWorker(BrowserWorker):
            def _lock_profile(self):
                release.wait(3)
            def _execute_job(self, identifier):
                events.append(("job", identifier))
                self.store.states[identifier] = "success"
                if identifier == "one":
                    self.inspect_activity("during-execution")
            def _inspect_activity(self, activity_id):
                events.append(("inspect", activity_id))
                inspected.set()
            def _perform_login_check(self):
                # Scheduler simulations never access a real browser or account.
                pass

        store = LocalStore()
        worker = LocalWorker(store, Path("unused"))
        return worker, store, release, inspected, events

    def test_due_jobs_precede_inspection_even_with_requests_during_execution(self):
        worker, store, release, inspected, events = self.simulation()
        worker.inspect_activity("requested-first")
        release.set()
        try:
            self.assertTrue(inspected.wait(3))
            self.assertEqual(events[:2], [("job", "one"), ("job", "two")])
        finally:
            worker.stop()

    def test_near_window_defers_deduplicated_inspection_then_recovers(self):
        worker, store, release, inspected, events = self.simulation(near=True)
        for _ in range(5):
            worker.inspect_activity("one-request")
        release.set()
        try:
            self.assertFalse(inspected.wait(.35))
            store.opening = datetime.now(timezone.utc)-timedelta(seconds=1)
            self.assertTrue(inspected.wait(3))
            self.assertEqual(events[:2], [("job", "one"), ("job", "two")])
            self.assertEqual(events.count(("inspect", "one-request")), 1)
        finally:
            worker.stop()

    def test_direct_inspection_near_window_stops_before_page_or_profile_access(self):
        from qq_activity.booking import BrowserWorker
        now = datetime.now(timezone.utc)
        class LocalStore:
            def jobs(self):
                return [{"status": "queued", "activity_id": "near"}]
            def get_activity(self, activity_id):
                if activity_id != "near":
                    raise AssertionError("Preflight should defer before loading requested form")
                return {"opens_at": (now+timedelta(seconds=20)).isoformat(), "closes_at": (now+timedelta(hours=1)).isoformat()}
        messages = []
        worker = BrowserWorker(LocalStore(), Path("unused"), messages.append)
        self.assertIs(worker._inspect_activity("requested"), False)
        self.assertTrue(messages)
        self.assertIsNone(worker._context)

    def test_login_command_precedes_due_jobs_and_cached_inspections(self):
        worker, store, release, inspected, events = self.simulation()
        login_opened = threading.Event()
        class Page:
            def goto(self, *args, **kwargs):
                events.append(("login", "opened"))
                login_opened.set()
        class Context:
            def new_page(self):
                return Page()
        worker._open_context = lambda headed=False: Context()
        worker.inspect_activity("cached")
        worker.open_login()
        release.set()
        try:
            self.assertTrue(login_opened.wait(3))
            self.assertFalse(inspected.wait(.25))
            self.assertEqual(events, [("login", "opened")])
            worker.finish_login()
            self.assertTrue(inspected.wait(3))
            self.assertEqual(events[:3], [("login", "opened"), ("job", "one"), ("job", "two")])
        finally:
            worker.stop()

    def test_login_state_check_is_deferred_near_window_until_due_jobs_finish(self):
        worker, store, release, inspected, events = self.simulation(near=True)
        checked = threading.Event()
        def check():
            events.append(("check", "account"))
            worker._login_check_pending = False
            worker._next_login_check = time.monotonic()+240
            checked.set()
        worker._perform_login_check = check
        for _ in range(10):
            worker.check_login()
        release.set()
        try:
            self.assertFalse(checked.wait(.35))
            store.opening = datetime.now(timezone.utc)-timedelta(seconds=1)
            self.assertTrue(checked.wait(3))
            self.assertEqual(events[:2], [("job", "one"), ("job", "two")])
            self.assertEqual(events.count(("check", "account")), 1)
        finally:
            worker.stop()


class LoginStateTests(unittest.TestCase):
    def test_visible_account_ui_requires_positive_evidence(self):
        from qq_activity.booking import _login_page_state
        cases = [
            ("https://account.wps.cn/", "已使用 WPS 云服务\n当前账号安全评分：80分\n企业信息\n账号昵称\n绑定手机\n绑定账号", "online"),
            ("https://account.wps.cn/", "账号昵称\n绑定手机\n扫码登录", "offline"),
            ("https://account.wps.cn/", "账号昵称\n绑定手机", "unknown"),
            ("https://account.wps.cn/", "微信扫码登录\n使用金山办公在线服务账号登录\n自动登录\nQQ账号 手机 专属账号 更多", "offline"),
            ("https://account.wps.cn/", "个人中心\n退出登录", "online"),
            ("https://account.wps.cn/usercenter", "账号安全\n个人资料", "online"),
            ("https://account.wps.cn/usercenter", "加载中", "unknown"),
            ("https://account.wps.cn/", "请完成验证码验证\n退出登录", "pending"),
            ("https://unrelated.invalid/usercenter", "退出登录", "unknown"),
            ("https://account.wps.cn/", "服务异常，请稍后重试", "unknown"),
        ]
        for url, text, expected in cases:
            with self.subTest(url=url, text=text):
                self.assertEqual(_login_page_state(url, text)[0], expected)

    def test_status_snapshot_and_deduplicated_queue_are_memory_only(self):
        from qq_activity.booking import BrowserWorker
        class NoIO:
            def __getattr__(self, name):
                raise AssertionError("Status APIs must not read Store")
        worker = BrowserWorker(NoIO(), Path("unused"))
        worker.start = lambda: None
        snapshot = worker.login_status()
        self.assertEqual(snapshot["state"], "unknown")
        snapshot["state"] = "online"
        self.assertEqual(worker.login_status()["state"], "unknown")
        for _ in range(20):
            worker.check_login()
        self.assertLessEqual(worker._commands.qsize(), 1)
        self.assertEqual(worker.login_status()["state"], "checking")
        self.assertFalse(worker.login_status()["login_window_open"])
        worker.stop()
        self.assertEqual(worker.login_status()["state"], "unknown")

    def test_stale_online_expires_and_finish_button_alone_never_confirms_login(self):
        from qq_activity.booking import BrowserWorker
        worker = BrowserWorker(object(), Path("unused"))
        worker.start = lambda: None
        worker._set_login_status("online", "local fixture", checked=True)
        with worker._login_status_lock:
            worker._login_snapshot["checked_at"] = (datetime.now(timezone.utc)-timedelta(seconds=301)).isoformat()
        self.assertEqual(worker.login_status()["state"], "unknown")
        worker.finish_login()
        self.assertEqual(worker.login_status()["state"], "checking")

    def test_open_login_page_check_reads_only_without_navigation_or_close(self):
        from qq_activity.booking import BrowserWorker
        class Body:
            def inner_text(self, **kwargs):
                return content[0]
        class Page:
            url = "https://account.wps.cn/"
            def locator(self, selector):
                return Body()
            def goto(self, *args, **kwargs):
                raise AssertionError("Do not navigate the user's login page")
            def close(self):
                raise AssertionError("Do not close the user's login page")
        class Context:
            pages = [Page()]
            def new_page(self):
                raise AssertionError("Read existing login page only")
        worker = BrowserWorker(object(), Path("unused"))
        worker._login_mode = True
        worker._context = Context()
        content = ["微信扫码登录\n使用金山办公在线服务账号登录"]
        worker._perform_login_check()
        self.assertEqual(worker.login_status()["state"], "pending")
        self.assertTrue(worker.login_status()["login_window_open"])
        self.assertLessEqual(worker._next_login_check-time.monotonic(), 2)

    def test_verified_login_automatically_returns_to_background_and_rechecks(self):
        from qq_activity.booking import BrowserWorker
        events = []
        class Page:
            url = "https://account.wps.cn/"
            def locator(self, _selector):
                return self
            def inner_text(self, **kwargs):
                return "当前账号安全评分：80分\n账号昵称\n绑定手机\n绑定账号"
            def goto(self, *args, **kwargs):
                events.append("verify-background")
            def wait_for_function(self, *args, **kwargs): pass
            def wait_for_timeout(self, *args, **kwargs): pass
            def close(self): events.append("close-probe")
        class Context:
            pages = [Page()]
            def new_page(self): return Page()
        class LocalWorker(BrowserWorker):
            def _inspection_imminent(self): return False
            def _open_context(self, headed=False):
                events.append(("open-context", headed))
                self._context = Context()
                return self._context
        worker = LocalWorker(object(), Path("unused"))
        worker._context = Context()
        worker._login_mode = True
        worker._perform_login_check()
        self.assertEqual(worker.login_status()["state"], "online")
        self.assertFalse(worker.login_status()["login_window_open"])
        self.assertEqual(events, [("open-context", False), "verify-background", "close-probe"])


class LoginCheckIntegrationTests(unittest.TestCase):
    def test_account_ui_states_network_failure_and_owner_thread(self):
        from qq_activity.booking import BrowserWorker
        from qq_activity.storage import Store
        cases = [("微信扫码登录\n使用金山办公在线服务账号登录", "offline"),
                 ("个人中心\n退出登录", "online"), ("请完成验证码验证", "pending"),
                 ("正在加载账户页面...", "unknown"), (None, "error")]
        for text, expected in cases:
            with self.subTest(expected=expected), tempfile.TemporaryDirectory() as temp:
                store = Store(Path(temp)/"fixture.db")
                owner_threads, request_count = set(), []
                arrived, release = threading.Event(), threading.Event()
                class LocalWorker(BrowserWorker):
                    def _open_context(self, headed=False):
                        context = super()._open_context(headed)
                        owner_threads.add(threading.get_ident())
                        def serve(route):
                            owner_threads.add(threading.get_ident())
                            request_count.append(1)
                            arrived.set()
                            release.wait(3)
                            if text is None:
                                route.abort()
                            else:
                                route.fulfill(status=200, content_type="text/html; charset=utf-8", body="<html><body>"+text.replace("\n", "<br>")+"</body></html>")
                        context.route("**/*", serve)
                        return context
                def forbidden(*args):
                    raise AssertionError("Account check must not read user profiles")
                store.get_profile = store.profiles = forbidden
                worker = LocalWorker(store, Path(temp)/"browser")
                worker.start()
                try:
                    self.assertTrue(arrived.wait(12))
                    for _ in range(20):
                        worker.check_login()
                    release.set()
                    deadline = time.monotonic()+12
                    while time.monotonic()<deadline and not worker.login_status()["checked_at"]:
                        time.sleep(.05)
                    snapshot = worker.login_status()
                    self.assertEqual(snapshot["state"], expected)
                    self.assertIsNotNone(datetime.fromisoformat(snapshot["checked_at"]).tzinfo)
                    self.assertLessEqual(worker._next_login_check-time.monotonic(), 240)
                    time.sleep(.25)
                    self.assertEqual(len(request_count), 1)
                    self.assertEqual(len(owner_threads), 1)
                    self.assertNotIn(threading.get_ident(), owner_threads)
                finally:
                    release.set()
                    worker.stop()
                    worker._thread.join(timeout=15)
                    self.assertFalse(worker._thread.is_alive())
                self.assertEqual(worker.login_status()["state"], "unknown")


class ProfileSchemaTests(unittest.TestCase):
    def test_duplicate_or_invalid_profile_schema_rejected(self):
        from qq_activity.booking import _public_profile_fields, _Attention
        major = {"title": "专业", "type": "select", "options": ["物联网工程", "电气工程及其自动化"]}
        class_name = {"title": "班级", "type": "input", "options": []}
        invalid = [[major, major, class_name], [major], [major, {**class_name, "type": "file"}],
                   [{**major, "options": []}, class_name], [{**major, "options": ["物联网工程", "物联网工程"]}, class_name]]
        for questions in invalid:
            with self.subTest(questions=questions), self.assertRaises(_Attention):
                _public_profile_fields({"questions": questions})

    def test_public_description_is_text_and_does_not_invent_class_options(self):
        from qq_activity.booking import _public_profile_fields
        fields = _public_profile_fields({"questions": [
            {"title": "专业", "type": "input", "options": []},
            {"title": "班级", "type": "input", "options": [], "description": "<p>专业简称+年级+A/C+班级号</p>"}]})
        self.assertEqual(fields["class_name"], {"title": "班级", "kind": "text", "options": [], "description": "专业简称+年级+A/C+班级号"})


class PreflightTests(unittest.TestCase):
    def test_login_and_unopened_form_inspects_without_profiles_or_submission(self):
        from qq_activity.booking import BrowserWorker
        from qq_activity.storage import Store
        submitted, owner_threads, contexts = [], [], []
        now = datetime.now(timezone.utc)
        opens, closes = now+timedelta(days=1), now+timedelta(days=1,hours=4)
        settings = {"checkOnce": True, "checkLogin": True, "fillLimit": 10,
                    "startTime": round(opens.timestamp()*1000), "stopTime": round(closes.timestamp()*1000),
                    "callBackUrl": "never-persist-this", "token": "never-persist-this"}
        class_title = "班级（网安 机电 电子 电气 人工智能 +23/24/25/26+A/C+班级号 ）"
        html = fixture_html(extra="此表单需要登录才能填写哦", settings=settings, class_title=class_title)
        # WPS initially exposes a default empty form before its live data arrives.
        html += '''<script>const props=document.querySelector('#root')._reactRootContainer._internalRoot.current.memoizedProps;
        const actualForm=props.form;props.form={questions:[],setting:{baseSetting:{checkOnce:false,checkLogin:false,fillLimit:0}},baseInfo:{count:null}};
        setTimeout(()=>props.form=actualForm,350);</script>'''
        served_html = [html]

        class LocalWorker(BrowserWorker):
            def _open_context(self, headed=False):
                context = super()._open_context(headed)
                context.route("**/*", lambda route: route.fulfill(status=200, content_type="text/html; charset=utf-8", body=served_html[0]))
                context.on("page", lambda page: page.on("console", lambda msg: submitted.append(msg.text) if msg.text.startswith("ANSWERS:") else None))
                contexts.append(context)
                owner_threads.append(threading.get_ident())
                return context

        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"test.db")
            activity = {"id": "preflight", "form_url": "https://f.wps.cn/g/localPreflight/", "opens_at": opens.isoformat(), "closes_at": closes.isoformat(), "issues": ["keep original issue"]}
            store.upsert_activity(activity)
            def forbidden(*args):
                raise AssertionError("Preflight must never inspect personal profiles")
            store.get_profile = store.profiles = forbidden
            worker = LocalWorker(store, Path(temp)/"browser")
            try:
                started = time.monotonic()
                worker.inspect_activity("preflight")
                self.assertLess(time.monotonic()-started, 1)
                deadline = time.monotonic()+20
                while time.monotonic()<deadline and not store.get_activity("preflight").get("form_constraints"):
                    time.sleep(.1)
                saved = store.get_activity("preflight")
                constraints = saved.get("form_constraints")
                self.assertIsNotNone(constraints)
                self.assertEqual(set(constraints), {"check_once", "check_login", "fill_limit", "count", "start_ms", "stop_ms", "checked_at"})
                self.assertEqual((constraints["check_once"], constraints["check_login"], constraints["fill_limit"], constraints["count"]), (True, True, 10, 0))
                self.assertEqual(constraints["start_ms"], settings["startTime"])
                self.assertIsNotNone(datetime.fromisoformat(constraints["checked_at"]).tzinfo)
                self.assertEqual(saved["opens_at"], activity["opens_at"])
                self.assertEqual(saved["closes_at"], activity["closes_at"])
                self.assertEqual(saved["issues"], activity["issues"])
                fields = saved.get("profile_fields")
                self.assertIsNotNone(fields)
                self.assertEqual(fields["major"], {"title": "专业", "kind": "choice", "options": ["计算机科学与技术", "电气工程及其自动化"]})
                self.assertEqual(fields["class_name"], {"title": class_title, "kind": "text", "options": []})
                # A later malformed schema must leave both previously saved values intact.
                served_html[0] = fixture_html()+'''<script>const form=document.querySelector('#root')._reactRootContainer._internalRoot.current.memoizedProps.form;
                form.questions.push({...form.questions.find(q=>q.title==='专业'),qid:'duplicate-major'});</script>'''
                worker.inspect_activity("preflight")
                deadline = time.monotonic()+5
                while time.monotonic()<deadline and "重复" not in (worker._last_notice or ""):
                    time.sleep(.1)
                self.assertIn("重复", worker._last_notice)
                unchanged = store.get_activity("preflight")
                self.assertEqual(unchanged["form_constraints"], constraints)
                self.assertEqual(unchanged["profile_fields"], fields)
            finally:
                worker.stop()
                worker._thread.join(timeout=15)
            self.assertEqual(submitted, [])
            self.assertEqual(store.jobs(), [])
            self.assertEqual(len(contexts), 1)
            self.assertNotIn(threading.get_ident(), owner_threads)

    def test_preflight_during_login_keeps_context_and_does_not_open_pages(self):
        from qq_activity.booking import BrowserWorker
        class NeverStore:
            def get_activity(self, *args):
                raise AssertionError("Login mode must defer inspection")
        messages = []
        worker = BrowserWorker(NeverStore(), Path("unused"), messages.append)
        worker._login_mode = True
        sentinel = object()
        worker._context = sentinel
        worker._inspect_activity("anything")
        self.assertIs(worker._context, sentinel)
        self.assertTrue(messages)


def fixture_html(extra="", confirmation="提交成功", settings=None, class_title="班级"):
    # Same semantic question structure as the actual WPS read-only sample.
    items = [("email", "邮箱"), ("reward", "奖励"), ("student_id", "学号"),
             ("name", "姓名"), ("phone", "手机号"), ("class_name", "班级"), ("major", "专业")]
    questions, blocks = [], []
    for field, title in items:
        if field == "class_name":
            title = class_title
        questions.append({"qid": field, "title": title, "type": "select" if field in {"major", "reward"} else "input",
                          "baseInfo": {"need": True, "isNoRepeat": False}})
        if field in {"major", "reward"}:
            options = ["计算机科学与技术", "电气工程及其自动化"] if field == "major" else ["思政学分3分", "0.8智能云"]
            questions[-1]["selectInfo"] = {"selects": [{"val": option} for option in options]}
            control = "".join(f'<label><input type="radio" name="{field}" value="{x}">{x}</label>' for x in options)
        else:
            control = f'<input type="text" name="{field}">'
        blocks.append(f'<div class="ksapc-questions-write-container" id="{field}"><div class="ksapc-question-title-required"><pre class="ksapc-question-title-title">{title}</pre></div>{control}</div>')
    form = {"questions": questions, "setting": {"baseSetting": {"checkOnce": False, "checkLogin": False, "fillLimit": 10, **(settings or {})}}, "baseInfo": {"count": 0}}
    return ("<!doctype html><html><meta charset=utf-8><body><div id=root>" + "".join(blocks) + extra +
            '<button id=submit>提 交</button></div><script>' +
            f'document.querySelector("#root")._reactRootContainer={{_internalRoot:{{current:{{memoizedProps:{{form:{json.dumps(form,ensure_ascii=False)}}}}}}}}};' +
            'document.querySelector("#submit").onclick=()=>{const answers={};for(const e of document.querySelectorAll("input")){if(e.type!=="radio"||e.checked)answers[e.name]=e.value;}console.log("ANSWERS:"+JSON.stringify(answers));document.body.innerHTML=' + json.dumps(confirmation) + ';};</script></body></html>')


class ControlledBrowserTests(unittest.TestCase):
    """Routes WPS URL entirely to local HTML; no WPS server request is sent."""
    def run_worker(self, html, count=2, opening_delay=-60, closing_delay=600, worker_delay=0, observe_before=None, duplicate_student=False, prior_status=None):
        from qq_activity.booking import BrowserWorker
        from qq_activity.storage import Store
        submitted, threads = [], set()

        class ControlledWorker(BrowserWorker):
            def _open_context(self, headed=False):
                context = super()._open_context(headed)
                def serve(route):
                    route.fulfill(status=200, content_type="text/html; charset=utf-8", body=html)
                context.route("**/*", serve)
                context.on("page", lambda page: page.on("console", lambda msg: submitted.append(json.loads(msg.text[8:])) if msg.text.startswith("ANSWERS:") else None))
                threads.add(threading.get_ident())
                return context

        with tempfile.TemporaryDirectory() as temp:
            store = Store(Path(temp)/"test.db")
            now = datetime.now(timezone.utc)
            activity = {"id": "fixture", "title": "Controlled local form", "form_url": "https://f.wps.cn/g/localTest/",
                        "starts_at": (now+timedelta(days=1)).isoformat(), "ends_at": (now+timedelta(days=1,hours=2)).isoformat(),
                        "opens_at": (now+timedelta(seconds=opening_delay)).isoformat(), "closes_at": (now+timedelta(seconds=closing_delay)).isoformat(), "issues": []}
            store.upsert_activity(activity)
            ids = []
            for index in range(count):
                profile = {"name": f"Test Person {index}", "major": "电气工程及自动化", "class_name": f"TestClass{index}",
                           "student_id": "000" if duplicate_student else f"00{index}", "phone": f"1380000000{index}", "email": f"p{index}@example.invalid", "reward": "cloud"}
                ids.append(store.save_profile(profile))
            store.queue("fixture", ids)
            if prior_status:
                old = next(job for job in store.jobs() if job["profile_id"] == ids[0])
                store.update_job(old["id"], prior_status, "previous local fixture result")
            worker = ControlledWorker(store, Path(temp)/"browser")
            if worker_delay:
                time.sleep(worker_delay)
            worker.start()
            if observe_before is not None:
                time.sleep(.3)
                observe_before.append(len(submitted))
            deadline = time.monotonic()+35
            try:
                while time.monotonic()<deadline and any(job["status"] in {"queued", "running"} for job in store.jobs()):
                    time.sleep(.1)
            finally:
                worker.stop()
                worker._thread.join(timeout=15)
            return store.jobs(), submitted, threads

    def test_reordered_fields_and_independent_personal_answers(self):
        jobs, submitted, threads = self.run_worker(fixture_html())
        self.assertEqual([job["status"] for job in jobs], ["success", "success"])
        self.assertEqual(len(submitted), 2)
        self.assertEqual({x["student_id"] for x in submitted}, {"000", "001"})
        for data in submitted:
            index = int(data["student_id"])
            self.assertEqual(data["name"], f"Test Person {index}")
            self.assertEqual(data["email"], f"p{index}@example.invalid")
            self.assertEqual(data["reward"], "0.8智能云")
        self.assertEqual(len(threads), 1)
        self.assertNotIn(threading.get_ident(), threads)

    def test_once_account_first_single_person_can_submit(self):
        jobs, submitted, _ = self.run_worker(fixture_html(settings={"checkOnce": True}), count=1)
        self.assertEqual(jobs[0]["status"], "success")
        self.assertEqual(len(submitted), 1)

    def test_once_account_multi_person_blocks_everyone_before_click(self):
        jobs, submitted, _ = self.run_worker(fixture_html(settings={"checkOnce": True}))
        self.assertEqual([job["status"] for job in jobs], ["attention", "attention"])
        self.assertEqual(submitted, [])

    def test_once_account_prior_result_blocks_other_person_before_click(self):
        for prior_status in ["success", "uncertain"]:
            with self.subTest(prior_status=prior_status):
                jobs, submitted, _ = self.run_worker(fixture_html(settings={"checkOnce": True}), prior_status=prior_status)
                self.assertEqual(sorted(job["status"] for job in jobs), sorted([prior_status, "attention"]))
                self.assertEqual(submitted, [])

    def test_unknown_required_question_never_clicks(self):
        extra = '<div class="ksapc-questions-write-container" id="unexpected"><div class="ksapc-question-title-required"><pre class="ksapc-question-title-title">身份证号</pre></div><input required></div>'
        jobs, submitted, _ = self.run_worker(fixture_html(extra), count=1)
        self.assertEqual(jobs[0]["status"], "attention")
        self.assertEqual(submitted, [])

    def test_unconfirmed_click_is_uncertain_and_never_retried(self):
        jobs, submitted, _ = self.run_worker(fixture_html(confirmation="正在处理"), count=1)
        self.assertEqual(jobs[0]["status"], "uncertain")
        self.assertEqual(len(submitted), 1)

    def test_captcha_after_click_keeps_possible_submission_uncertain(self):
        jobs, submitted, _ = self.run_worker(fixture_html(confirmation="请完成验证码"), count=1)
        self.assertEqual(jobs[0]["status"], "uncertain")
        self.assertEqual(len(submitted), 1)

    def test_scheduler_waits_for_opening_then_submits(self):
        before = []
        jobs, submitted, _ = self.run_worker(fixture_html(), count=1, opening_delay=1.5, observe_before=before)
        self.assertEqual(before, [0])
        self.assertEqual(jobs[0]["status"], "success")
        self.assertEqual(len(submitted), 1)

    def test_expired_job_never_submits_even_with_startup_login_check(self):
        jobs, submitted, threads = self.run_worker(fixture_html(), count=1, closing_delay=.2, worker_delay=.4)
        self.assertEqual(jobs[0]["status"], "expired")
        self.assertEqual(submitted, [])
        # The independent startup login check may open Edge, while the expired
        # activity itself must never produce a submitted answer.

    def test_unexpected_selected_option_is_blocked(self):
        script = '''<script>document.addEventListener('change',e=>{if(e.target.name==='major') document.querySelector('input[name=reward]').checked=true;});</script>'''
        jobs, submitted, _ = self.run_worker(fixture_html()+script, count=1)
        self.assertEqual(jobs[0]["status"], "attention")
        self.assertEqual(submitted, [])

    def test_duplicate_student_in_two_profiles_never_submits_twice(self):
        jobs, submitted, _ = self.run_worker(fixture_html(), duplicate_student=True)
        self.assertEqual(sorted(job["status"] for job in jobs), ["attention", "success"])
        self.assertEqual(len(submitted), 1)

    def test_page_states_block_before_click(self):
        for banner, expected in [("此表单需要登录才能填写哦", "login_required"), ("此表单已暂停收集", "attention"), ("当前收集数量已达上限", "full")]:
            with self.subTest(banner=banner):
                jobs, submitted, _ = self.run_worker(fixture_html()+banner, count=1)
                self.assertEqual(jobs[0]["status"], expected)
                self.assertEqual(submitted, [])


if __name__ == "__main__":
    unittest.main()
