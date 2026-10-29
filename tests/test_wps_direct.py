# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Native-client submission contract, routed entirely to controlled local responses."""
import importlib
import json
from pathlib import Path
import tempfile
import time
import unittest
from datetime import datetime, timedelta, timezone

from qq_activity.booking import match_field, choose_option


PROFILE = {"name": "Fixture Person", "student_id": "00001", "major": "电气工程及自动化",
           "class_name": "Fixture Class", "phone": "13800000000", "email": "fixture@example.invalid",
           "reward": "cloud"}


def metadata():
    questions = []
    for field, title in (("name", "姓名"), ("major", "专业"), ("class_name", "班级"),
                         ("student_id", "学号"), ("phone", "手机号"), ("email", "邮箱"), ("reward", "奖励")):
        options = ([{"val": "电气工程及其自动化", "selectId": "major-a"}]
                   if field == "major" else [{"val": "2思政学分", "selectId": "credit-a"},
                                            {"val": "0.5智能云", "selectId": "cloud-a"}]
                   if field == "reward" else [])
        questions.append({"qid": field, "title": title,
                          "type": "select" if options else "input",
                          "answer_type": "select" if options else "telphone" if field == "phone" else "input",
                          "required": True, "noRepeat": False, "hidden": False, "readonly": False,
                          "deleted": False, "options": options, "multiType": "radio", "addedValType": "none"})
    return {"settings": {"checkOnce": False, "checkLogin": False, "fillLimit": 10},
            "count": 0, "revision": 1, "questions": questions, "conditional": False}


def client_fixture(check_login=False, logged_in=True, validation=True):
    public = metadata()
    questions = []
    for question in public["questions"]:
        questions.append({"qid": question["qid"], "title": question["title"],
                          "type": question["answer_type"],
                          "baseInfo": {"need": True},
                          "selectInfo": {"multiType": "radio", "addedValType": "none", "selects": question["options"]}})
    raw = {"shareId": "localTest", "count": 0, "editVersion": 1, "kind": "ksform",
           "token": "fixture-token-stays-in-page", "setting": {"baseSetting": {**public["settings"], "checkLogin": check_login}},
           "questionMap": {question["qid"]: question for question in questions}, "questionLogicMap": {}}
    return '''<!doctype html><meta charset=utf-8><body><div id="root"><input id="untouched" value="">
      <button id="human-submit" onclick="window.humanClicked=true">提交</button></div><script>
      const raw=RAW;
      const form={questions:Object.values(raw.questionMap), setting:raw.setting,
        baseInfo:{shareId:raw.shareId,editVersion:1}, answerJson:{answers:{}}, extraAnswerInfo:{},
        get answers(){return this.answerJson.answers},
        clearFormAnswers(){this.answerJson={answers:{}}},
        setAnswers(answers){this.answerJson.answers=structuredClone(answers)},
        async validate(){return {result:VALIDATION,message:'Fixture validation error',id:VALIDATION?undefined:'class_name'}},
        getValidAnswerJson(){return structuredClone(this.answerJson)}};
      document.querySelector('#root')._reactRootContainer={_internalRoot:{current:{memoizedProps:{form}}}};
      const api={
        async getFormByShareId(code){return {code:0,data:structuredClone(raw)}},
        async writePrecheck(code){return {code:0,data:{}}},
        async createAnswer(code,payload,config){const r=await fetch('https://f-api.wps.cn/ksform/api/v3/campaign/'+code,
          {method:'POST',headers:{'Content-Type':'application/json',...config.headers},body:JSON.stringify(payload)});
          const result=await r.json();if(result.code!==0)throw result;return result},
        getAnswerList(){throw Error('Not needed by fixture')}
      };
      window.webpackChunkksform_web_pc=[];
      window.webpackChunkksform_web_pc.push=function(chunk){chunk[2](id=>{
        if(id===90212)return {Z:api};if(id===15287)return {Z:{hasLogin:LOGGED_IN}};
        if(id===76194)return {c:()=>({'Client-Type':'pc','Deviceid':'fixture-device','Channel':''})};
        throw Error('Unknown fixture module');})};
      </script>'''.replace("RAW", json.dumps(raw, ensure_ascii=False)).replace("VALIDATION", str(validation).lower()).replace("LOGGED_IN", str(logged_in).lower())


class DirectPlanTests(unittest.TestCase):
    def adapter(self):
        try:
            return importlib.import_module("qq_activity.wps_direct")
        except ImportError:
            self.fail("Direct WPS adapter has not been implemented")

    def test_native_answers_use_real_choice_ids_and_phone_type(self):
        direct = self.adapter()
        answers = direct.plan_answers(metadata(), PROFILE, match_field, choose_option)
        self.assertEqual(answers["phone"], {"type": "telphone", "strValue": PROFILE["phone"]})
        self.assertEqual(answers["reward"]["selectValue"], [{"selectId": "cloud-a", "val": "0.5智能云", "addedVal": ""}])
        self.assertEqual(answers["major"]["selectValue"][0]["selectId"], "major-a")

    def test_actual_ksform_kind_is_supported(self):
        form = metadata()
        form["kind"] = "ksform"
        self.assertEqual(len(self.adapter().plan_answers(form, PROFILE, match_field, choose_option)), 7)

    def test_duplicate_server_codes_keep_uncertain_protection_without_chinese_text(self):
        from qq_activity.booking import BrowserWorker
        for code in (13015, 13017, 13027):
            for detail in ("", "Already submitted"):
                with self.subTest(code=code, detail=detail):
                    state, _ = BrowserWorker._direct_result({"state": "uncertain", "code": code, "detail": detail})
                    self.assertEqual(state, "uncertain")

    def test_unknown_required_hidden_duplicate_or_changed_option_stops(self):
        direct = self.adapter()
        cases = []
        unknown = metadata(); unknown["questions"].append({"qid": "unknown", "title": "身份证号", "required": True})
        cases.append(unknown)
        hidden = metadata(); hidden["questions"][0]["hidden"] = True; cases.append(hidden)
        duplicate = metadata(); duplicate["questions"][1]["title"] = "姓名"; cases.append(duplicate)
        option = metadata(); option["questions"][1]["options"] = []; cases.append(option)
        conditional = metadata(); conditional["conditional"] = True; cases.append(conditional)
        for form in cases:
            with self.subTest(form=form):
                with self.assertRaises(ValueError):
                    direct.plan_answers(form, PROFILE, match_field, choose_option)


class DirectBrowserTests(DirectPlanTests):
    def run_direct(self, response=None, allowed=True, check_login=False, logged_in=True, validation=True, repeat=False, abort=False):
        direct = self.adapter()
        from playwright.sync_api import sync_playwright
        captured = []
        response = response if response is not None else {"code": 0, "data": {"aid": "fixture-receipt", "createdTs": 1}}
        with sync_playwright() as playwright:
            browser = playwright.chromium.launch(channel="msedge", headless=True)
            try:
                context = browser.new_context()
                def serve(route):
                    if route.request.method == "POST":
                        captured.append(route.request.post_data_json)
                        if abort:
                            route.abort("connectionreset")
                        else:
                            route.fulfill(status=200, content_type="application/json", body=json.dumps(response))
                    else:
                        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=client_fixture(check_login, logged_in, validation))
                context.route("**/*", serve)
                page = context.new_page()
                session = direct.DirectSession(page, "localTest", authorize=allowed if callable(allowed) else lambda: allowed)
                session.install_guard()
                page.goto("https://f.wps.cn/g/localTest/", wait_until="domcontentloaded")
                self.assertTrue(session.ready())
                form = session.refresh()
                answers = direct.plan_answers(form, PROFILE, match_field, choose_option)
                staged = session.stage(answers)
                started = time.perf_counter()
                result = session.submit() if staged["state"] == "ready" else staged
                if repeat:
                    result = session.submit()
                elapsed = time.perf_counter()-started
                untouched = page.locator("#untouched").input_value()
                clicked = page.evaluate("Boolean(window.humanClicked)")
                return result, captured, session.sent, elapsed, untouched, clicked
            finally:
                browser.close()

    def test_direct_submission_returns_server_receipt_without_typing_or_clicking(self):
        result, captured, sent, elapsed, untouched, clicked = self.run_direct()
        self.assertEqual(result["state"], "success")
        self.assertEqual(result["receipt"]["answer_id"], "fixture-receipt")
        self.assertEqual(len(captured), 1)
        self.assertEqual(captured[0]["answerJson"]["answers"]["name"]["strValue"], PROFILE["name"])
        self.assertTrue(sent)
        self.assertEqual(untouched, "")
        self.assertFalse(clicked)
        self.assertLess(elapsed, 3)

    def test_code_zero_without_answer_id_is_not_success(self):
        result, captured, sent, *_ = self.run_direct({"code": 0, "data": {}})
        self.assertEqual(result["state"], "uncertain")
        self.assertEqual(len(captured), 1)

    def test_revoked_job_never_sends_request(self):
        result, captured, sent, *_ = self.run_direct(allowed=False)
        self.assertNotEqual(result["state"], "success")
        self.assertEqual(captured, [])
        self.assertFalse(sent)

    def test_form_login_is_checked_separately_from_account_center(self):
        result, captured, sent, *_ = self.run_direct(check_login=True, logged_in=False)
        self.assertEqual(result["state"], "login_required")
        self.assertEqual(captured, [])

    def test_native_validation_failure_never_sends_request(self):
        result, captured, sent, *_ = self.run_direct(validation=False)
        self.assertEqual(result["state"], "attention")
        self.assertEqual(captured, [])

    def test_second_attempt_does_not_resend(self):
        result, captured, sent, *_ = self.run_direct(repeat=True)
        self.assertEqual(result["state"], "attention")
        self.assertEqual(len(captured), 1)

    def test_lost_response_remains_uncertain_without_retry(self):
        result, captured, sent, *_ = self.run_direct(abort=True)
        self.assertEqual(result["state"], "uncertain")
        self.assertTrue(sent)
        self.assertEqual(len(captured), 1)

    def test_revoked_during_precheck_is_blocked_at_network_boundary(self):
        calls = []
        def authorize():
            calls.append(True)
            return len(calls) == 1
        result, captured, sent, *_ = self.run_direct(allowed=authorize)
        self.assertNotEqual(result["state"], "success")
        self.assertFalse(sent)
        self.assertEqual(captured, [])


class NativeQueueTests(unittest.TestCase):
    def test_warm_queue_submits_once_after_opening_within_three_seconds(self):
        from qq_activity.booking import BrowserWorker
        from qq_activity.storage import Store
        self.assertTrue(hasattr(BrowserWorker, "_warm_next_job"), "Queue does not prepare the direct client before opening")
        submitted = []
        class FixtureWorker(BrowserWorker):
            def _open_context(self, headed=False):
                context = super()._open_context(headed)
                def serve(route):
                    if route.request.method == "POST":
                        submitted.append((datetime.now(timezone.utc), route.request.post_data_json))
                        route.fulfill(status=200, content_type="application/json", body=json.dumps(
                            {"code": 0, "data": {"aid": "queue-receipt", "createdTs": 1}}))
                    else:
                        route.fulfill(status=200, content_type="text/html; charset=utf-8", body=client_fixture())
                context.route("**/*", serve)
                return context
        with tempfile.TemporaryDirectory() as folder:
            root = Path(folder)
            store = Store(root / "state.sqlite")
            opening = datetime.now(timezone.utc)+timedelta(seconds=7)
            store.upsert_activity({"id": "native-queue", "title": "Local direct fixture", "issues": [],
                "form_url": "https://f.wps.cn/g/localTest/", "opens_at": opening.isoformat(),
                "closes_at": (opening+timedelta(minutes=5)).isoformat(),
                "starts_at": (opening+timedelta(days=1)).isoformat(),
                "ends_at": (opening+timedelta(days=1,hours=2)).isoformat()})
            profile_id = store.save_profile(PROFILE)
            job_id = store.queue("native-queue", [profile_id])[0]
            worker = FixtureWorker(store, root / "browser")
            worker.start()
            deadline = time.monotonic()+22
            try:
                while time.monotonic() < deadline and store.get_job(job_id)["status"] in {"queued", "running"}:
                    time.sleep(.05)
            finally:
                worker.stop(timeout=15)
            self.assertEqual(store.get_job(job_id)["status"], "success")
            self.assertLess((datetime.fromisoformat(store.get_job(job_id)["updated_at"])-opening).total_seconds(), 3)
            self.assertEqual(len(submitted), 1)
            delta = (submitted[0][0]-opening).total_seconds()
            self.assertGreaterEqual(delta, 0)
            self.assertLess(delta, 3)
            log = root / "browser" / "booking-logs" / f"{job_id}.jsonl"
            events = [json.loads(line) for line in log.read_text(encoding="utf-8").splitlines()]
            self.assertTrue(any(event["event"] == "page_prepared" for event in events))
            self.assertTrue(any(event["event"] == "request_sent" for event in events))
            self.assertEqual(events[-1]["status"], "success")
            self.assertNotIn(PROFILE["email"], log.read_text(encoding="utf-8"))
            print("DIRECT_FIXTURE_TIMING " + json.dumps({event["event"]: event["since_open_ms"]
                  for event in events if event["event"] in {"page_prepared", "request_sent", "server_receipt", "result"}}))


if __name__ == "__main__":
    unittest.main()
