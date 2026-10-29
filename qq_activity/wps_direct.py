# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Submit through the loaded WPS client, retaining its auth and request encoding.

No WPS bundle, encryption key, login cookie or copied third-party implementation
is shipped here. Capability/revision checks fail before sending if the client
contract changes. Every page has a single-use, live-authorized submission gate.
"""
from __future__ import annotations

import re
from urllib.parse import urlsplit


_BIND = r"""() => {
 if (window.__qqActivityDirect) return true;
 const chunks = window.webpackChunkksform_web_pc;
 if (!Array.isArray(chunks)) return false;
 let requireClient;
 chunks.push([['qq_activity_direct_' + Date.now()], {}, r => { requireClient = r; }]);
 if (!requireClient) return false;
 let api, account, headers;
 try { api = requireClient(90212).Z; account = requireClient(15287).Z;
       headers = requireClient(76194).c; } catch (_) { return false; }
 if (!api || !['createAnswer','getFormByShareId','writePrecheck','getAnswerList']
     .every(k => typeof api[k] === 'function') || typeof headers !== 'function') return false;
 let form; const seen = new Set();
 function walk(f) {
   if (!f || seen.has(f) || seen.size > 50000 || form) return;
   seen.add(f); const candidate = f.memoizedProps?.form;
   if (candidate?.setting?.baseSetting && Array.isArray(candidate.questions) &&
       ['setAnswers','clearFormAnswers','validate','getValidAnswerJson'].every(k => typeof candidate[k] === 'function')) {
     form = candidate; return;
   }
   walk(f.child); walk(f.sibling);
 }
 for (const root of document.querySelectorAll('#root,#app')) {
   walk(root._reactRootContainer?._internalRoot?.current);
   for (const key of Object.keys(root).filter(k=>k.startsWith('__reactContainer')))
     walk(root[key]?.current || root[key]);
 }
 if (!form) return false;
 const textTypes = new Set(['input','telphone','email','number','textarea']);
 function question(q) {
   const b = q.baseInfo || {}, selection = q.selectInfo || {};
   return {qid:q.qid,title:q.title || b.title || '',
     type:textTypes.has(q.type) ? 'input' : q.type,
     answer_type:form.answers?.[q.qid]?.type || q.type,
     required:!!b.need,noRepeat:!!b.isNoRepeat,hidden:!!(b.isHidden || b.isLinkHidden),
     readonly:!!b.isReadonly,deleted:!!b.delete,
     options:(selection.selects || []).map(o=>({val:o.val,selectId:o.selectId})),
     multiType:selection.multiType,addedValType:selection.addedValType};
 }
 const canonical = value => JSON.stringify(value, function(key, item) {
   return item && typeof item === 'object' && !Array.isArray(item)
     ? Object.fromEntries(Object.keys(item).sort().map(k=>[k,item[k]])) : item;
 });
 const state = {api,account,headers,form,question,canonical,loadedAt:performance.now(),fresh:null,
                staged:false,attempted:false,answers:null};
 state.snapshot = raw => ({settings:raw.setting?.baseSetting || {},count:raw.count ?? null,
   revision:raw.editVersion,kind:raw.kind,conditional:Object.keys(raw.questionLogicMap || {}).length > 0,
   questions:Object.values(raw.questionMap || {}).map(question)});
 window.__qqActivityDirect = state;
 return true;
}"""

_REFRESH = r"""async code => {
 const s = window.__qqActivityDirect;
 const response = await s.api.getFormByShareId(code);
 if (response?.code !== 0 || !response.data?.questionMap || response.data.shareId !== code)
   throw new Error('WPS form metadata identity could not be verified');
 s.fresh = response.data;
 return s.snapshot(s.fresh);
}"""

_STAGE = r"""async answers => {
 const s = window.__qqActivityDirect, fresh = s.fresh;
 if (!fresh || s.attempted) return {state:'attention',detail:'接口状态不可用或已提交过'};
 if (fresh.setting?.baseSetting?.checkLogin && !s.account.hasLogin)
   return {state:'login_required',detail:'WPS 表单会话尚未登录；账户中心登录不代表表单会话已就绪'};
 if (fresh.editVersion !== s.form.baseInfo?.editVersion)
   return {state:'attention',detail:'表单版本已变更，需要重新读取后核对'};
 const current = s.form.questions.map(s.question), next = Object.values(fresh.questionMap).map(s.question);
 const signature = qs => JSON.stringify(qs.map(q=>({qid:q.qid,title:q.title,type:q.type,required:q.required,
   hidden:q.hidden,readonly:q.readonly,deleted:q.deleted,options:q.options}))
   .sort((a,b)=>a.qid.localeCompare(b.qid)));
 if (signature(current) !== signature(next))
   return {state:'attention',detail:'表单字段或选项在准备后发生变化，停止提交'};
 s.form.clearFormAnswers();
 s.form.setAnswers(structuredClone(answers));
 const validation = await s.form.validate();
 if (validation?.result !== true || validation.id)
   return {state:'attention',detail:'WPS 字段校验未通过，请核对档案格式',field:validation?.id || ''};
 const actual = s.form.getValidAnswerJson()?.answers;
 if (s.canonical(actual) !== s.canonical(answers))
   return {state:'attention',detail:'WPS 处理后的答案与所选档案不一致，停止提交'};
 s.answers = structuredClone(answers); s.staged = true;
 return {state:'ready'};
}"""

_SUBMIT = r"""async code => {
 const s = window.__qqActivityDirect;
 if (!s?.staged || s.attempted) return {state:'attention',detail:'提交未准备好或已尝试过，禁止重复请求'};
 let phase = 'precheck';
 try {
   const precheck = await s.api.writePrecheck(code,{channel:s.headers().Channel || ''});
   if (precheck?.code !== 0) return {state:'attention',detail:'WPS 未确认可填写，未发送答卷'};
   if (s.fresh.setting?.baseSetting?.checkLogin && !s.account.hasLogin)
     return {state:'login_required',detail:'WPS 表单登录会话已失效，未发送答卷'};
   if (s.canonical(s.form.getValidAnswerJson()?.answers) !== s.canonical(s.answers))
     return {state:'attention',detail:'提交前答案发生变化，未发送答卷'};
   const answerJson = {...s.form.getValidAnswerJson(),
      consumeTime:Math.max(0,Math.floor((performance.now()-s.loadedAt)/1000))};
   const payload = {answerJson,phoneNumber:s.form.extraAnswerInfo?.phoneNumber || '',
                    editVersion:s.fresh.editVersion,token:s.fresh.token || ''};
   phase = 'submit'; s.attempted = true;
   const response = await s.api.createAnswer(code,payload,{headers:s.headers(),timeout:12000});
   const aid = response?.data?.aid;
   if (response?.code === 0 && (typeof aid === 'string' && aid.trim() || typeof aid === 'number' && aid > 0))
     return {state:'success',detail:'WPS 接口已确认答卷提交成功；录取以活动方审核为准',
             receipt:{answer_id:String(aid),created_ts:response.data.createdTs ?? null}};
   return {state:'uncertain',detail:'答卷请求已发送，但接口缺少明确答卷回执，禁止自动重试'};
 } catch (error) {
   const detail = typeof error?.message === 'string' ? error.message.slice(0,500) : '';
   const code = typeof error?.code === 'number' ? error.code : null;
   return {state:phase === 'submit' ? 'uncertain' : 'attention',phase,code,
           detail:detail || (phase === 'submit' ? '提交请求后网络或接口中断，结果待核实' : 'WPS 提交前检查未通过，未发送答卷')};
 }
}"""


def plan_answers(form, profile, match_field, choose_option):
    """Build native answers using the current question and option IDs."""
    if form.get("conditional") or form.get("kind") not in (None, "", "form", "ksform"):
        raise ValueError("条件题或特殊表单需要人工核对，未发送答卷")
    settings = form.get("settings") or {}
    if ((settings.get("presetkeyConfig") or {}).get("presetkeyGroupId")
            or (settings.get("commitCheckConfig") or {}).get("isOpen")):
        raise ValueError("表单含特殊填写校验，需要人工核对")
    answers, used, ids = {}, set(), set()
    for q in form.get("questions") or []:
        if q.get("deleted"):
            continue
        identifier = q.get("qid")
        if not isinstance(identifier, str) or not identifier or identifier in ids:
            raise ValueError("表单字段标识缺失或重复")
        ids.add(identifier)
        field = match_field(q.get("title") or "")
        if field is None:
            if q.get("required"):
                raise ValueError("存在未知必填字段，未发送答卷")
            continue
        if field in used or q.get("hidden") or q.get("readonly"):
            raise ValueError("字段重复、隐藏或只读，需要人工核对")
        if q.get("noRepeat") and field not in {"name", "student_id", "phone", "email"}:
            raise ValueError("表单对非身份字段设有去重限制")
        used.add(field)
        value = profile.get(field)
        if not isinstance(value, str) or not value.strip():
            raise ValueError("所选档案字段不完整")
        if q.get("type") == "select":
            if q.get("multiType") != "radio" or q.get("addedValType") not in (None, "", "none"):
                raise ValueError("多选或附加填写选项需要人工核对")
            options = q.get("options") or []
            answer = choose_option([o.get("val", "") for o in options], value, field)
            matches = [o for o in options if o.get("val") == answer and o.get("selectId")]
            if answer is None or len(matches) != 1:
                raise ValueError("专业、班级或奖励选项不能唯一匹配")
            option = matches[0]
            answers[identifier] = {"type": "select", "selectValue": [
                {"selectId": option["selectId"], "val": option["val"], "addedVal": ""}]}
        elif q.get("type") == "input" and field != "reward":
            answer_type = q.get("answer_type")
            if answer_type not in {"input", "telphone", "email", "number", "textarea"}:
                raise ValueError("不支持的文本字段类型")
            answers[identifier] = {"type": answer_type, "strValue": value}
        else:
            raise ValueError("字段类型不受支持，未发送答卷")
    if not {"name", "student_id"}.issubset(used):
        raise ValueError("无法唯一核对姓名和学号")
    return answers


class DirectSession:
    def __init__(self, page, form_code, authorize=None, on_send=None):
        if not re.fullmatch(r"[A-Za-z0-9]+", form_code):
            raise ValueError("表单标识无效")
        self.page, self.form_code = page, form_code
        self.authorize = authorize or (lambda: False)
        self.on_send = on_send
        self.sent = False
        self.blocked = False

    def install_guard(self):
        def guard(route):
            request = route.request
            url = urlsplit(request.url)
            if request.method in {"GET", "HEAD", "OPTIONS"}:
                route.fallback()
                return
            target = f"/ksform/api/v3/campaign/{self.form_code}"
            if request.method == "POST" and url.hostname in {"f-api.wps.cn", "f.wps.cn"} and url.path == target:
                try:
                    allowed = not self.sent and bool(self.authorize())
                except Exception:
                    allowed = False
                if allowed:
                    self.sent = True
                    if self.on_send:
                        self.on_send()
                    route.fallback()
                    return
                self.blocked = True
            reads = {
                ("account.wps.cn", "/p/auth/check"),
                ("f-api.wps.cn", "/ksform/api/qing/v1/users/login"),
                ("f-api.wps.cn", target + "/precheck"),
                ("f-api.wps.cn", target + "/answers/list"),
            }
            if request.method == "POST" and (url.hostname, url.path) in reads:
                route.fallback()
            else:
                route.abort("blockedbyclient")
        self.page.route("**/*", guard)

    def ready(self):
        return bool(self.page.evaluate(_BIND))

    def refresh(self):
        return self.page.evaluate(_REFRESH, self.form_code)

    def stage(self, answers):
        return self.page.evaluate(_STAGE, answers)

    def submit(self):
        if not self.authorize():
            return {"state": "cancelled", "detail": "任务或档案状态已变更，未发送答卷"}
        result = self.page.evaluate(_SUBMIT, self.form_code)
        if result.get("state") == "success" and not self.sent:
            return {"state": "uncertain", "detail": "未观察到本次答卷请求，不能确认接口回执归属"}
        if not self.sent and result.get("state") == "uncertain":
            return {"state": "attention", "detail": "未发送答卷；接口调用未完成，请核对表单状态"}
        return result
