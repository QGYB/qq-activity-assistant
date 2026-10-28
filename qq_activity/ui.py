# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Chinese Tk desktop for reviewing notices and scheduling individual sign-ups.

Only the Tk thread touches widgets. Worker callbacks can use ``post_status`` and
``post_event``; both merely place data in a thread-safe queue.
"""
from __future__ import annotations

import json
from datetime import datetime, timedelta, timezone
from pathlib import Path
import queue
import re
import sys
import threading
import tkinter as tk
from tkinter import filedialog, messagebox, ttk
from urllib.parse import urlparse
import webbrowser

from .activity_view import activity_state as _activity_state, activity_tag, remaining_places, sort_activities
from .ui_theme import PALETTE, apply_theme
from .ui_widgets import DataTable
from .app_identity import apply_tk_icon, asset_path


BEIJING = timezone(timedelta(hours=8), "北京时间")
STATUS = {
    "queued": "等待开放", "running": "执行中", "success": "提交成功",
    "full": "已满", "expired": "已截止", "login_required": "需登录",
    "attention": "需人工处理", "uncertain": "结果待核实",
    "cancelled": "已取消", "failed": "失败",
}
PROFILE_FIELDS = (
    ("name", "姓名"), ("major", "专业"), ("class_name", "班级"),
    ("student_id", "学号"), ("phone", "手机号"), ("email", "邮箱"),
)


def _display_text(value):
    """Use the chosen reward name in generated UI; leave stored source intact."""
    return str(value).replace("智能云", "学院积分")


def _date(value):
    if not value:
        return None
    result = datetime.fromisoformat(str(value).strip().replace("Z", "+00:00"))
    if result.tzinfo is None:
        result = result.replace(tzinfo=BEIJING)
    return result.astimezone(BEIJING)


def _when(value, short=False):
    try:
        parsed = _date(value)
        return parsed.strftime("%m-%d %H:%M" if short else "%Y-%m-%d %H:%M") if parsed else "待补充"
    except (TypeError, ValueError):
        return "时间待核对"


def _table_when(value, ends=None, short_date=False):
    try:
        stamp = _date(value)
        if not stamp:
            return "通知未注明"
        clock = stamp.strftime("%H:%M")
        end = _date(ends)
        if end:
            clock += " – " + end.strftime("%H:%M")
        return stamp.strftime("%m-%d" if short_date else "%m 月 %d 日") + "\n" + clock
    except (ValueError, TypeError):
        return "时间待核对"


class MainWindow:
    """UI boundary; dependencies implement the interfaces in IMPLEMENTATION.md."""

    def __init__(self, store, browser, bridge, data_dir):
        self.store, self.browser, self.bridge = store, browser, bridge
        self.data_dir = Path(data_dir)
        if sys.platform == "win32":
            # Avoid Windows enlarging the whole window beyond the screen at 150%.
            try:
                import ctypes
                ctypes.windll.shcore.SetProcessDpiAwareness(1)
            except (AttributeError, OSError):
                pass
        self.root = tk.Tk()
        self.root.title("群活动助手 · 志愿报名工作台")
        apply_tk_icon(self.root)
        screen_width, screen_height = self.root.winfo_screenwidth(), self.root.winfo_screenheight()
        display_scale = max(.85, min(2.25, float(self.root.tk.call("tk", "scaling")) / (96 / 72)))
        width, height = min(round(1440 * display_scale), screen_width - 80), min(round(900 * display_scale), screen_height - 100)
        self.root.geometry(f"{width}x{height}")
        self.root.minsize(min(round(1040 * display_scale), width), min(round(700 * display_scale), height))
        self.root.configure(bg=PALETTE["canvas"])
        self.root.protocol("WM_DELETE_WINDOW", self._close)
        self._events = queue.Queue()
        self._closed = False
        self._activities = []
        self._profiles = []
        self._jobs = []
        self._selected = None
        self._snapshot = None
        self._profile_choices = {}
        self._detail_dialog = None
        self.detail = None
        self.detail_canvas = None
        self._startup_popup = None
        self._startup_checked = False
        self._preflight_seen = set()
        self._preflight_pending = {}
        self._browser_status = "WPS 登录状态由浏览器实际检查"
        self._last_poll_error = ""
        self._refresh_busy = False
        self.search = tk.StringVar()
        ordering = self.store.get_setting("activity_order", {}) or {}
        self.sort_newest = tk.BooleanVar(value=ordering.get("newest", True))
        self.sort_available = tk.BooleanVar(value=ordering.get("available_first", False))
        self._list_checks = {}
        self._list_check_pending = None
        self.clock = tk.StringVar()
        self.connection = tk.StringVar(value="QQ 接收：正在检查")
        self.wps_connection = tk.StringVar(value="○ WPS 检查中")
        self.wps_account_info = tk.StringVar(value="正在核对 WPS 登录状态…")
        self.feedback = tk.StringVar(value="新通知只展示；选定活动和人员后才会自动报名。")
        self._style()
        self._build()
        self.root.bind("<Control-i>", lambda _e: self._import_dialog())
        self.root.bind("<Control-n>", lambda _e: self._profile_editor())
        self.root.bind("<F5>", self._refresh_all)
        self.search.trace_add("write", lambda *_: self._render_activity_list())
        self._poll()
        self.root.after(2500, self._check_list_forms)
        self.root.after(5000, self._startup_binding_check)

    def _style(self):
        apply_theme(self.root)

    def _build(self):
        workspace = ttk.Frame(self.root)
        workspace.pack(fill="both", expand=True)
        sidebar = ttk.Frame(workspace, style="Paper.TFrame", width=round(190 * self.root._ui_scale))
        sidebar.pack(side="left", fill="y")
        sidebar.pack_propagate(False)
        brand = ttk.Frame(sidebar, style="Paper.TFrame", padding=(22, 30, 12, 27))
        brand.pack(fill="x")
        self._brand_icon = tk.PhotoImage(master=self.root, file=str(asset_path('app-32.png')))
        ttk.Label(brand, image=self._brand_icon, style="Paper.TLabel").pack(side="left", padx=(0, 10))
        ttk.Label(brand, text="群活动助手", style="Paper.TLabel", font=("Microsoft YaHei UI", 12, "bold")).pack(side="left")
        nav_frame = ttk.Frame(sidebar, style="Paper.TFrame", padding=(14, 6))
        nav_frame.pack(fill="x")
        side_bottom = ttk.Frame(sidebar, style="Paper.TFrame", padding=(24, 18, 18, 24))
        side_bottom.pack(side="bottom", fill="x")
        ttk.Separator(side_bottom).pack(fill="x", pady=(0, 17))
        ttk.Label(side_bottom, text="接收群", style="Paper.TLabel", foreground=PALETTE["muted"], font=("Microsoft YaHei UI", 9)).pack(anchor="w")
        self.sidebar_groups = tk.StringVar()
        ttk.Label(side_bottom, textvariable=self.sidebar_groups, style="Paper.TLabel", wraplength=145, font=("Segoe UI", 10)).pack(anchor="w", pady=(7, 0))
        unit = lambda value: round(value * self.root._ui_scale)
        shell = ttk.Frame(workspace, padding=(unit(26), unit(24), unit(26), unit(14)))
        shell.pack(side="left", fill="both", expand=True)
        top = ttk.Frame(shell)
        top.pack(fill="x", pady=(0, 22))
        title = ttk.Frame(top)
        title.pack(side="left")
        self.page_title = tk.StringVar(value="活动通知")
        self.page_caption = tk.StringVar(value="核对活动与报名时间，选择本次参与人")
        ttk.Label(title, textvariable=self.page_title, style="Title.TLabel").pack(anchor="w")
        ttk.Label(title, textvariable=self.page_caption, style="Small.TLabel").pack(anchor="w", pady=(6, 0))
        top_right = ttk.Frame(top)
        top_right.pack(side="right", anchor="n")
        clock_box = ttk.Frame(top_right)
        clock_box.pack(side="right", padx=(17, 0))
        ttk.Label(clock_box, text="北京时间", style="Small.TLabel").pack(anchor="e")
        ttk.Label(clock_box, textvariable=self.clock, font=("Segoe UI", 9), foreground=PALETTE["muted"]).pack(anchor="e", pady=(5, 0))
        self.wps_connection_label = tk.Label(top_right, textvariable=self.wps_connection, font=("Microsoft YaHei UI", 9), bg="#F0F4F8", fg=PALETTE["muted"], padx=11, pady=7, cursor="hand2")
        self.wps_connection_label.pack(side="right", padx=(10, 0), pady=(4, 0))
        self.wps_connection_label.bind("<Button-1>", self._wps_status_click)
        self.connection_label = tk.Label(top_right, textvariable=self.connection, font=("Microsoft YaHei UI", 9), bg="#F0F4F8", fg=PALETTE["muted"], padx=11, pady=7)
        self.connection_label.pack(side="right", pady=(4, 0))
        self.refresh_button = ttk.Button(top_right, text="刷新", command=self._refresh_all)
        self.refresh_button.pack(side="left", padx=(0, 14), pady=(4, 0))
        ttk.Label(shell, textvariable=self.feedback, style="Small.TLabel", wraplength=1000).pack(side="bottom", fill="x", pady=(13, 0))
        self.notebook = ttk.Notebook(shell, style="Workspace.TNotebook")
        self.notebook.pack(fill="both", expand=True)
        self.activity_page = ttk.Frame(self.notebook)
        self.queue_page = ttk.Frame(self.notebook)
        self.profile_page = ttk.Frame(self.notebook)
        self.settings_page = ttk.Frame(self.notebook)
        self._nav_buttons = []
        self._page_info = {}
        self._nav_icons = self._load_nav_icons()
        pages = ((self.activity_page, "活动通知", "核对活动与报名时间，选择本次参与人"), (self.queue_page, "报名队列", "查看每位参与人的报名进度与结果"), (self.profile_page, "个人档案", "保存参与人资料，专业与班级按实际表单填写"), (self.settings_page, "连接与设置", "管理 QQ 接收、WPS 登录与本机启动配置"))
        for index, (page, label, caption) in enumerate(pages):
            self.notebook.add(page, text=label)
            self._page_info[str(page)] = (label, caption)
            button = ttk.Button(nav_frame, text=label, style="Nav.TButton", command=lambda destination=page: self.notebook.select(destination))
            if self._nav_icons:
                button.configure(image=self._nav_icons[index][0], compound="left")
            button.pack(fill="x", pady=4)
            self._nav_buttons.append((page, button, index))
            self.root.bind(f"<Control-Key-{index+1}>", lambda _e, destination=page: self.notebook.select(destination))
        self.notebook.bind("<<NotebookTabChanged>>", self._sync_navigation)
        self._build_activities()
        self._build_queue()
        self._build_profiles()
        self._build_settings()
        self.root.bind_all("<MouseWheel>", self._mousewheel)
        self._sync_navigation()

    def _load_nav_icons(self):
        assets = Path(__file__).resolve().parent / "assets"
        try:
            return [(tk.PhotoImage(master=self.root, file=str(assets / f"nav-{name}.png")), tk.PhotoImage(master=self.root, file=str(assets / f"nav-{name}-active.png"))) for name in ("activities", "queue", "profiles", "settings")]
        except (OSError, tk.TclError):
            return []

    def _sync_navigation(self, _event=None):
        selected = self.notebook.select()
        label, caption = self._page_info.get(selected, ("活动通知", "核对活动与报名时间，选择本次参与人"))
        self.page_title.set(label)
        self.page_caption.set(caption)
        for page, button, index in self._nav_buttons:
            active = str(page) == selected
            button.configure(style="Active.Nav.TButton" if active else "Nav.TButton")
            if self._nav_icons:
                button.configure(image=self._nav_icons[index][int(active)])

    def _update_wps_status(self):
        getter = getattr(self.browser, "login_status", None)
        try:
            status = getter() if callable(getter) else {"state":"unknown", "detail":"尚未检查 WPS 登录状态"}
        except Exception:
            status = {"state":"error", "detail":"暂时无法读取 WPS 登录状态"}
        if not isinstance(status, dict):
            status = {"state":"error", "detail":"暂时无法读取 WPS 登录状态"}
        state = status.get("state", "unknown")
        colors = {
            "online": ("● WPS 在线", "#147D4E", "#ECFDF3"),
            "offline": ("○ WPS 未登录", "#92400E", "#FFFBEB"),
            "pending": ("○ WPS 登录中", "#92400E", "#FFFBEB"),
            "checking": ("○ WPS 检查中", PALETTE["muted"], "#F0F4F8"),
            "error": ("○ WPS 检查失败", PALETTE["red"], "#FEF2F2"),
            "unknown": ("○ WPS 未确认", PALETTE["muted"], "#F0F4F8"),
        }
        text, foreground, background = colors.get(state, colors["unknown"])
        self.wps_connection.set(text)
        self.wps_connection_label.configure(foreground=foreground, background=background)
        detail = status.get("detail") or "点击顶部 WPS 状态可登录或重新检查。"
        if status.get("checked_at"):
            detail += f"\n最近检查：{_when(status['checked_at'])}（北京时间）"
        self.wps_account_info.set(_display_text(detail))

    def _wps_status_click(self, _event=None):
        getter = getattr(self.browser, "login_status", None)
        try:
            status = getter() if callable(getter) else {"state":"unknown"}
        except Exception:
            status = {"state":"unknown"}
        if not isinstance(status, dict):
            status = {"state":"unknown"}
        if status.get("state") == "pending" and status.get("login_window_open", True):
            self.notebook.select(self.settings_page)
            self.browser.check_login()
            self.feedback.set("请在已打开的 Edge 窗口完成 WPS 登录，程序会自动确认；也可点击刷新立即核验。")
        elif status.get("state") in {"online", "checking"}:
            check = getattr(self.browser, "check_login", None)
            if callable(check):
                check()
                self._update_wps_status()
        else:
            self._browser_action("open_login")

    def _build_activities(self):
        toolbar = ttk.Frame(self.activity_page)
        toolbar.pack(fill="x", pady=(0, 16))
        toolbar.columnconfigure(0, weight=1)
        search_box = ttk.Frame(toolbar)
        search_box.grid(row=0, column=0, sticky="ew", padx=(0, 12))
        ttk.Label(search_box, text="名称 / 地点", style="Small.TLabel").pack(side="left", padx=(0, 8))
        self.search_entry = ttk.Entry(search_box, textvariable=self.search, width=22)
        self.search_entry.pack(side="left", fill="x", expand=True)
        self.search_entry.bind("<Escape>", lambda _e: self.search.set(""))
        self.time_sort_button = ttk.Button(toolbar, text="时间 ↓ 最新在前", command=lambda: self._toggle_sort("newest"))
        self.time_sort_button.grid(row=0, column=1, padx=(0, 8))
        self.available_sort_button = ttk.Button(toolbar, text="有名额优先", command=lambda: self._toggle_sort("available_first"))
        self.available_sort_button.grid(row=0, column=2, padx=(0, 8))
        ttk.Button(toolbar, text="变更通知", command=self._show_activity_changes).grid(row=0, column=3, padx=(0, 8))
        ttk.Button(toolbar, text="＋ 补录通知", command=self._import_dialog, style="Accent.TButton").grid(row=0, column=4)
        panel = ttk.Frame(self.activity_page, style="Paper.TFrame", padding=1)
        panel.pack(fill="both", expand=True)
        actions = ttk.Frame(panel, style="Paper.TFrame", padding=(16, 10, 12, 10))
        actions.pack(fill="x")
        ttk.Label(actions, text="全部活动", style="Paper.TLabel", font=("Microsoft YaHei UI", 10, "bold")).pack(side="left")
        self.detail_button = ttk.Button(actions, text="查看详情  ›", command=self._open_detail, style="Quiet.TButton")
        self.detail_button.pack(side="right")
        self.detail_button.state(["disabled"])
        self.activity_tree = self._tree(panel, ("title", "location", "date", "opens", "closes", "remaining", "state"), ("活动名称", "地点", "活动时间", "报名开始", "报名截止", "剩余名额", "状态"), (260, 175, 155, 115, 115, 90, 100), height=6)
        self.activity_tree.tag_configure("expired", foreground=PALETTE["red"])
        self.activity_tree.tag_configure("upcoming", foreground="#A77800")
        self.activity_tree.tag_configure("available", foreground="#238447")
        self.activity_tree.bind("<<TreeviewSelect>>", self._select_activity)
        self.activity_tree.bind("<Double-1>", lambda _e: self._open_detail())
        self.activity_tree.bind("<Return>", lambda _e: self._open_detail())
        self.list_note = ttk.Label(self.activity_page, text="等待真实群通知，或补录已有通知。", style="Small.TLabel", wraplength=1100)
        self.list_note.pack(anchor="w", pady=10)
        self._update_sort_buttons()

    def _update_sort_buttons(self):
        self.time_sort_button.configure(style="Active.Nav.TButton" if self.sort_newest.get() else "TButton")
        self.available_sort_button.configure(style="Active.Nav.TButton" if self.sort_available.get() else "TButton")

    def _toggle_sort(self, kind):
        variable = self.sort_newest if kind == "newest" else self.sort_available
        variable.set(not variable.get())
        self.store.set_setting("activity_order", {"newest":self.sort_newest.get(), "available_first":self.sort_available.get()})
        self._update_sort_buttons()
        self._render_activity_list()

    def _show_activity_changes(self):
        dialog = self._dialog("取消与变更通知", 750, 580)
        frame = ttk.Frame(dialog, padding=22)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="取消、暂停与延期通知", style="Heading.TLabel").pack(anchor="w", pady=(0, 12))
        text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI",10), padx=12, pady=12, relief="flat")
        text.pack(fill="both", expand=True)
        getter = getattr(self.store, "activity_changes", None)
        notices = getter() if callable(getter) else []
        for notice in notices:
            matched = notice.get("matched_activity_id")
            text.insert("end", f"{_when(notice.get('observed_at'))} · {'已关联活动' if matched else '尚未找到唯一对应活动，需核对'}\n{notice.get('raw_text','')}\n\n")
        if not notices:
            text.insert("end", "尚无取消或变更通知。收到后会核对原消息时间及活动，再关联到对应场次。")
        text.configure(state="disabled")

    def _open_detail(self):
        selected = self.activity_tree.selection()
        if selected and selected[0] != self._selected:
            self._selected = selected[0]
            self._profile_choices = {}
        activity = next((a for a in self._activities if str(a["id"]) == str(self._selected)), None)
        if not activity:
            self.feedback.set("请先选择一条活动通知，再查看详情。")
            return
        if self._detail_dialog is not None and self._detail_dialog.winfo_exists():
            self._render_detail()
            self._detail_dialog.deiconify()
            self._detail_dialog.lift()
            return
        dialog = tk.Toplevel(self.root)
        self._detail_dialog = dialog
        self._preflight_seen = set()
        dialog.title("活动详情 · 北京时间")
        scale = self.root._ui_scale
        dialog.geometry(f"{min(round(740*scale), self.root.winfo_screenwidth()-80)}x{min(round(880*scale), self.root.winfo_screenheight()-100)}")
        dialog.minsize(round(600*scale), round(560*scale))
        dialog.configure(bg=PALETTE["paper"])
        dialog.transient(self.root)
        # Modeless: the full-width activity list stays usable alongside details.
        def close_detail(_event=None):
            dialog.destroy()
            self._detail_dialog = None
            self.detail = None
            self.detail_canvas = None
        dialog.protocol("WM_DELETE_WINDOW", close_detail)
        dialog.bind("<Escape>", close_detail)
        self.detail_canvas = tk.Canvas(dialog, background="white", highlightthickness=0)
        scroll = ttk.Scrollbar(dialog, orient="vertical", command=self.detail_canvas.yview)
        self.detail_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.detail_canvas.pack(side="left", fill="both", expand=True)
        self.detail = ttk.Frame(self.detail_canvas, style="Paper.TFrame", padding=22)
        self.detail_window = self.detail_canvas.create_window((0, 0), window=self.detail, anchor="nw")
        self.detail.bind("<Configure>", lambda _e: self.detail_canvas.configure(scrollregion=self.detail_canvas.bbox("all")))
        self.detail_canvas.bind("<Configure>", lambda e: self.detail_canvas.itemconfigure(self.detail_window, width=e.width))
        self._render_detail()

    def _mousewheel(self, event):
        if self._detail_dialog is not None and event.widget.winfo_toplevel() == self._detail_dialog:
            self.detail_canvas.yview_scroll(int(-event.delta / 120), "units")
            return
        if event.widget.winfo_toplevel() != self.root:
            return
        if self.notebook.select() == str(self.settings_page):
            self.settings_canvas.yview_scroll(int(-event.delta / 120), "units")

    def _startup_binding_check(self):
        if self._closed or self._startup_checked:
            return
        self._startup_checked = True
        try:
            groups = self.store.get_setting("group_ids", []) or []
            if isinstance(groups, str):
                try:
                    groups = json.loads(groups)
                except ValueError:
                    groups = re.split(r"[\s,，;；]+", groups.strip())
            groups = [str(group).strip() for group in groups if str(group).strip()]
        except Exception:
            groups = []
        # Binding persists across launches. A delayed heartbeat or a temporary
        # disconnect belongs in the status bar, not the first-use setup hint.
        if groups:
            return
        popup = tk.Toplevel(self.root)
        self._startup_popup = popup
        popup.title("QQ 接收提示")
        popup.geometry("630x390")
        popup.minsize(530, 350)
        popup.configure(bg=PALETTE["canvas"])
        popup.transient(self.root)
        frame = ttk.Frame(popup, padding=26)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="从一条真实通知开始", style="Heading.TLabel").pack(anchor="w", pady=(0, 12))
        text = "尚未配置接收群。前往「连接与设置」填写群号，并核对 QQ 接收启动器。"
        ttk.Label(frame, text=text, wraplength=560, justify="left").pack(fill="x", pady=(0, 14))
        ttk.Label(frame, text="接收群通知 → 核对时间 → 选择参与人", foreground=PALETTE["teal"], font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w", pady=(0, 10))
        ttk.Label(frame, text="已有历史通知可在活动页补录。此提示不影响继续使用工具，本次启动关闭后不会再次弹出。", style="Small.TLabel", wraplength=560).pack(fill="x")
        buttons = ttk.Frame(frame)
        buttons.pack(side="bottom", fill="x", pady=(16, 0))
        def dismiss():
            popup.destroy()
            self._startup_popup = None
        def settings():
            self.notebook.select(self.settings_page)
            dismiss()
        ttk.Button(buttons, text="前往设置", style="Accent.TButton", command=settings).pack(side="right")
        ttk.Button(buttons, text="稍后再说", command=dismiss).pack(side="right", padx=8)
        popup.protocol("WM_DELETE_WINDOW", dismiss)
        popup.bind("<Escape>", lambda _e: dismiss())

    def _tree(self, parent, columns, labels, widths, *, height=None):
        tree = DataTable(parent, columns, labels, widths, height=height or 5)
        tree.pack(fill="both", expand=True)
        tree.tag_configure("warning", foreground=PALETTE["amber"])
        tree.tag_configure("success", foreground="#15803D")
        tree.tag_configure("muted", foreground=PALETTE["muted"])
        return tree

    def _render_activity_list(self):
        selected = self.activity_tree.selection()
        self.activity_tree.delete(*self.activity_tree.get_children())
        term = self.search.get().strip().lower()
        visible = 0
        for activity in sort_activities(self._activities, newest=self.sort_newest.get(), available_first=self.sort_available.get()):
            if term and term not in (str(activity.get("title", "")) + str(activity.get("location", ""))).lower():
                continue
            state = _activity_state(activity)
            tags = (activity_tag(activity),)
            activity_id = str(activity["id"])
            places = remaining_places(activity)
            missing_time = "原通知缺日期" if not activity.get("starts_at") else _table_when(activity["starts_at"], activity.get("ends_at"))
            self.activity_tree.insert("", "end", iid=activity_id, values=(_display_text(activity.get("title") or "通知未写活动名称").strip("【】"), activity.get("location") or "通知未注明", missing_time, _table_when(activity.get("opens_at"), short_date=True), _table_when(activity.get("closes_at"), short_date=True), str(places) if places is not None else "待检查", state), tags=tags)
            visible += 1
        if selected and self.activity_tree.exists(selected[0]):
            self.activity_tree.selection_set(selected[0])
        elif self._selected and self.activity_tree.exists(str(self._selected)):
            self.activity_tree.selection_set(str(self._selected))
        self.detail_button.state(["!disabled"] if self.activity_tree.selection() else ["disabled"])
        self.list_note.configure(text=f"共 {visible} 项活动    ·    双击查看详情    ·    名额为最近检查结果，执行前再次核对" if self._activities else "尚无活动。等待群通知，或点击「补录通知」粘贴已有通知。")

    def _check_list_forms(self):
        if self._closed:
            return
        try:
            inspect = getattr(self.browser, "inspect_activity", None)
            now = datetime.now(BEIJING)
            # Keep read-only background checks out of a scheduled job's window.
            due = any(job.get("status") in {"queued", "running"} and (
                job.get("status") == "running" or any(
                    str(a["id"]) == str(job.get("activity_id")) and _date(a.get("opens_at"))
                    and _date(a["opens_at"]) <= now+timedelta(minutes=1)
                    for a in self._activities)) for job in self._jobs)
            if not callable(inspect) or due:
                return
            if self._list_check_pending:
                identifier, baseline, started = self._list_check_pending
                current = next((a for a in self._activities if str(a["id"]) == identifier), {})
                checked = (current.get("form_constraints") or {}).get("checked_at")
                if checked == baseline and (now-started).total_seconds() < 45:
                    return
                self._list_check_pending = None
            for activity in sort_activities(self._activities):
                identifier = str(activity["id"])
                if not activity.get("form_url") or _activity_state(activity) in {"已截止", "已取消", "已暂停", "已延期"}:
                    continue
                checked = _date((activity.get("form_constraints") or {}).get("checked_at"))
                attempted = self._list_checks.get(identifier)
                if (checked and now-checked < timedelta(minutes=5)) or (attempted and now-attempted < timedelta(minutes=5)):
                    continue
                self._list_checks[identifier] = now
                self._list_check_pending = (identifier, (activity.get("form_constraints") or {}).get("checked_at"), now)
                inspect(identifier)
                break
        except (TypeError, ValueError, OSError):
            pass
        finally:
            if not self._closed:
                self.root.after(1500, self._check_list_forms)

    def _select_activity(self, _event=None):
        selected = self.activity_tree.selection()
        self.detail_button.state(["!disabled"] if selected else ["disabled"])
        if selected and selected[0] != self._selected:
            self._selected = selected[0]
            self._profile_choices = {}
            self._render_detail()

    def _paper_label(self, text, **kwargs):
        if "wraplength" in kwargs:
            kwargs["wraplength"] = round(kwargs["wraplength"] * self.root._ui_scale)
        label = ttk.Label(self.detail, text=_display_text(text), style="Paper.TLabel", **kwargs)
        label.pack(anchor="w", fill="x", pady=(0, 8))
        return label

    def _request_form_inspection(self, activity, force=False):
        inspect = getattr(self.browser, "inspect_activity", None)
        if not callable(inspect):
            return
        activity_id = str(activity["id"])
        if not force:
            if activity_id in self._preflight_seen:
                return
            self._preflight_seen.add(activity_id)
            constraints = activity.get("form_constraints") or {}
            try:
                checked = _date(constraints.get("checked_at"))
                if checked and datetime.now(BEIJING) - checked < timedelta(minutes=5):
                    return
            except (TypeError, ValueError):
                pass
        if activity_id in self._preflight_pending:
            return
        baseline = json.dumps(activity.get("form_constraints") or {}, sort_keys=True, default=str)
        token = object()
        self._preflight_pending[activity_id] = (baseline, token)
        self.feedback.set("正在核对表单限制；检查只读取页面，不会提交报名。")
        self._async("检查表单", lambda: inspect(activity_id))
        def expire():
            pending = self._preflight_pending.get(activity_id)
            if not self._closed and pending and pending[1] is token:
                self._preflight_pending.pop(activity_id, None)
                self._render_detail()
        self.root.after(60000, expire)

    def _form_constraints_text(self, activity):
        activity_id = str(activity["id"])
        constraints = activity.get("form_constraints") or {}
        pending = self._preflight_pending.get(activity_id)
        if pending and pending[0] != json.dumps(constraints, sort_keys=True, default=str):
            self._preflight_pending.pop(activity_id, None)
            pending = None
        if constraints.get("check_once") is True:
            text = "表单开启一次填写限制；账号多人填报需核对，请只选择一位参与人。"
        elif constraints.get("check_once") is False:
            text = "最近检查未发现一次填写限制；执行前会再次核对。"
        else:
            text = "尚未取得表单限制。可点击「检查表单」，执行时也会再次核对。"
        if pending:
            text += "\n正在核对表单限制…"
        notes = []
        if constraints.get("check_login"):
            notes.append("需要 WPS 登录")
        if constraints.get("fill_limit"):
            notes.append(f"总份数上限 {constraints['fill_limit']}")
        if constraints.get("count") is not None:
            notes.append(f"已收 {constraints['count']} 份")
        if constraints.get("checked_at"):
            notes.append(f"检查于 {_when(constraints['checked_at'])}（北京时间）")
        if notes:
            text += "\n" + " · ".join(notes)
        return text

    def _inspect_current_form(self, activity):
        self._request_form_inspection(activity, force=True)
        self._render_detail()

    def _render_detail(self):
        if self._detail_dialog is None or not self._detail_dialog.winfo_exists() or self.detail is None:
            return
        for child in self.detail.winfo_children():
            child.destroy()
        activity = next((a for a in self._activities if str(a["id"]) == str(self._selected)), None)
        if not activity:
            self._paper_label("请在活动列表中选择一条通知。", foreground=PALETTE["muted"])
            return
        self._request_form_inspection(activity)
        color = {"expired":PALETTE["red"], "upcoming":"#A77800", "available":"#238447"}.get(activity_tag(activity), PALETTE["muted"])
        self._paper_label(_activity_state(activity), foreground=color, font=("Microsoft YaHei UI", 10, "bold"))
        self._paper_label(activity.get("title") or "待补充活动名称", font=("Microsoft YaHei UI", 18, "bold"), wraplength=480)
        # The teal registration strip is the visual anchor: the window that drives scheduling.
        strip = tk.Frame(self.detail, bg=PALETTE["pale"], padx=15, pady=12)
        strip.pack(fill="x", pady=(4, 14))
        tk.Label(strip, text="报名窗口 · 北京时间", bg=PALETTE["pale"], fg=PALETTE["teal"], font=("Microsoft YaHei UI", 9, "bold")).pack(anchor="w")
        tk.Label(strip, text=f"{_when(activity.get('opens_at'))}  →\n{_when(activity.get('closes_at'))}", bg=PALETTE["pale"], fg=PALETTE["ink"], font=("Microsoft YaHei UI", 13, "bold"), justify="left").pack(anchor="w", pady=(6, 0))
        self._paper_label(f"活动时间  {_when(activity.get('starts_at'))} — {_when(activity.get('ends_at'))}", wraplength=480)
        self._paper_label(f"地点  {activity.get('location') or '通知未注明'}    ·    名额  {activity.get('capacity') or '通知未注明'}", wraplength=480)
        rewards = activity.get("rewards") or {}
        self._paper_label("奖励  " + " / ".join(f"{name} {rewards[key]}" for key, name in (("credit", "思政学分"), ("cloud", "学院积分")) if rewards.get(key)) if rewards else "奖励  通知未注明", wraplength=480)
        issues = activity.get("issues") or []
        if issues:
            self._paper_label("需核对：\n" + "\n".join("• " + str(issue) for issue in issues), foreground=PALETTE["amber"], wraplength=480)
        actions = ttk.Frame(self.detail, style="Paper.TFrame")
        actions.pack(fill="x", pady=(0, 15))
        ttk.Button(actions, text="核对 / 修改活动", command=lambda: self._activity_editor(activity)).pack(side="left")
        if activity.get("form_url"):
            ttk.Button(actions, text="查看表单", command=lambda: self._open_url(activity["form_url"])).pack(side="left", padx=8)
            if callable(getattr(self.browser, "inspect_activity", None)):
                ttk.Button(actions, text="检查表单", command=lambda: self._inspect_current_form(activity)).pack(side="left")
        self._paper_label(self._form_constraints_text(activity), wraplength=620, foreground=PALETTE["amber"] if (activity.get("form_constraints") or {}).get("check_once") is True else PALETTE["muted"])
        if activity.get("change_notice"):
            change = activity["change_notice"]
            self._paper_label("活动变更通知\n" + str(change.get("raw_text", "") if isinstance(change, dict) else change), foreground=PALETTE["red"], wraplength=620)
        self._paper_label("选择此次参与人", font=("Microsoft YaHei UI", 12, "bold"))
        if not self._profiles:
            self._paper_label("还没有个人档案。添加后，逐人选择本次奖励。", foreground=PALETTE["muted"], wraplength=480)
            ttk.Button(self.detail, text="添加个人档案", command=self._profile_editor).pack(anchor="w", pady=(0, 12))
        for profile in self._profiles:
            pid = str(profile["id"])
            previous = self._profile_choices.get(pid)
            selected = previous[0] if previous else tk.BooleanVar(value=False)
            reward = previous[1] if previous else tk.StringVar(value="思政学分" if profile.get("reward", "credit") == "credit" else "学院积分")
            self._profile_choices[pid] = (selected, reward)
            row = ttk.Frame(self.detail, style="Paper.TFrame")
            row.pack(fill="x", pady=3)
            ttk.Checkbutton(row, text=f"{profile.get('name', '')}  ·  {profile.get('student_id', '')}", variable=selected).pack(side="left")
            ttk.Combobox(row, textvariable=reward, values=("思政学分", "学院积分"), width=10, state="readonly").pack(side="right")
        self.queue_button = ttk.Button(self.detail, text="将选中人员加入报名队列", style="Accent.TButton", command=self._queue_selected)
        self.queue_button.pack(fill="x", pady=(12, 7))
        if issues or _activity_state(activity) in ("待核对", "已截止", "已取消", "已暂停", "已延期", "已满") or not self._profiles:
            self.queue_button.state(["disabled"])
        self._paper_label("加入后会在报名开放时自动填写并提交。提交成功不代表审核录取。", foreground=PALETTE["muted"], wraplength=480, font=("Microsoft YaHei UI", 9))
        self._paper_label("原始通知", font=("Microsoft YaHei UI", 12, "bold"))
        raw = tk.Text(self.detail, height=10, wrap="word", font=("Microsoft YaHei UI", 10), bg="#F5F8FA", fg=PALETTE["muted"], relief="flat", padx=12, pady=10)
        raw.insert("1.0", activity.get("raw_text") or "未保存通知正文")
        raw.configure(state="disabled")
        raw.pack(fill="x", pady=(0, 10))
        if activity.get("form_url"):
            self._paper_label(str(activity["form_url"]), wraplength=480, foreground=PALETTE["muted"], font=("Consolas", 9))

    def _queue_selected(self):
        activity = self.store.get_activity(self._selected)
        if not activity or _activity_state(activity) in ("待核对", "已截止"):
            messagebox.showwarning("先核对活动", "请补全活动和报名时间、表单链接，并解决通知中的歧义。", parent=self.root)
            return
        selected = [pid for pid, (checked, _) in self._profile_choices.items() if checked.get() and any(str(p["id"]) == pid for p in self._profiles)]
        if not selected:
            messagebox.showinfo("选择参与人", "请勾选至少一位参与人。", parent=self.root)
            return
        if (activity.get("form_constraints") or {}).get("check_once") is True and len(selected) > 1:
            messagebox.showwarning("表单开启一次填写限制", "表单开启一次填写限制；账号多人填报需核对。\n\n当前工具复用同一个 WPS 账号，请只选择一位参与人。", parent=self._detail_dialog or self.root)
            return
        if not activity.get("form_constraints"):
            self._request_form_inspection(activity)
            self._render_detail()
        rewards = {pid: ("credit" if self._profile_choices[pid][1].get() == "思政学分" else "cloud") for pid in selected}
        names = [p.get("name", "") for p in self._profiles if str(p["id"]) in selected]
        if not messagebox.askyesno("确认自动报名", f"活动：{activity.get('title')}\n参与人：{'、'.join(names)}\n\n加入队列后，工具会在报名窗口内使用这些人员资料自动提交 WPS 表单。", parent=self.root):
            return
        try:
            ids = self.store.queue(self._selected, selected, rewards=rewards)
            self.feedback.set(f"已处理 {len(ids)} 位参与人的队列请求。查看报名队列核对逐人状态。")
            self._refresh(force=True)
            self.notebook.select(self.queue_page)
        except Exception as exc:
            self._error("加入队列失败", exc)

    def _build_queue(self):
        top = ttk.Frame(self.queue_page)
        top.pack(fill="x", pady=(2, 12))
        ttk.Label(top, text="全部报名任务", style="Heading.TLabel").pack(side="left")
        ttk.Button(top, text="取消等待中的任务", command=self._cancel_job).pack(side="right")
        ttk.Button(top, text="返回活动详情", command=self._job_activity).pack(side="right", padx=8)
        self.job_tree = self._tree(self.queue_page, ("activity", "person", "reward", "status", "detail"), ("活动", "参与人", "此次奖励", "状态", "结果 / 原因"), (260, 100, 100, 110, 390))
        self.job_tree.bind("<<TreeviewSelect>>", self._job_detail)
        self.job_note = tk.StringVar(value="尚无报名任务。在活动详情中选择参与人，再加入报名队列。")
        ttk.Label(self.queue_page, textvariable=self.job_note, style="Small.TLabel", wraplength=1120).pack(fill="x", pady=(12, 4))
        ttk.Label(self.queue_page, text="已成功任务不会重复提交。结果待核实的任务需先人工核对；取消仅对尚未执行的任务生效。", style="Small.TLabel", wraplength=1120).pack(fill="x", pady=4)

    def _render_jobs(self):
        selected = self.job_tree.selection()
        self.job_tree.delete(*self.job_tree.get_children())
        activity_names = {str(a["id"]): a.get("title", "") for a in self._activities}
        names = {str(p["id"]): p.get("name", "") for p in self._profiles}
        for job in self._jobs:
            status = job.get("status", "")
            tags = ("success",) if status == "success" else ("expired",) if status in ("failed", "expired", "full", "cancelled") else ("upcoming",) if status in ("queued", "running") else ("warning",) if status in ("attention", "uncertain", "login_required") else ()
            self.job_tree.insert("", "end", iid=str(job["id"]), values=(_display_text(activity_names.get(str(job.get("activity_id")), "已移除的活动")), names.get(str(job.get("profile_id")), "已移除的档案"), "思政学分" if job.get("reward") == "credit" else "学院积分", STATUS.get(status, status), _display_text(job.get("detail") or "—")), tags=tags)
        if selected and self.job_tree.exists(selected[0]):
            self.job_tree.selection_set(selected[0])
        if not self._jobs:
            self.job_note.set("尚无报名任务。在活动详情中选择参与人，再加入报名队列。")
        else:
            count = sum(j.get("status") == "queued" for j in self._jobs)
            self.job_note.set(f"共 {len(self._jobs)} 条逐人记录 · 等待执行 {count} 条。选择一条记录查看完整原因。")

    def _job_detail(self, _event=None):
        selection = self.job_tree.selection()
        job = next((j for j in self._jobs if selection and str(j["id"]) == selection[0]), None)
        if job:
            self.job_note.set(_display_text(f"{STATUS.get(job.get('status'), job.get('status', ''))} · {job.get('detail') or '暂无额外说明'} · 更新于 {_when(job.get('updated_at'))}（北京时间）"))

    def _cancel_job(self):
        selection = self.job_tree.selection()
        if not selection:
            return
        try:
            job = self.store.get_job(selection[0])
            if not job or job.get("status") != "queued":
                messagebox.showinfo("任务无法取消", "只能取消尚未执行的排队任务。", parent=self.root)
                return
            self.store.cancel_job(selection[0])
            self.feedback.set("已请求取消该人员的排队任务。")
            self._refresh(force=True)
        except Exception as exc:
            self._error("取消失败", exc)

    def _job_activity(self):
        selection = self.job_tree.selection()
        job = next((j for j in self._jobs if selection and str(j["id"]) == selection[0]), None)
        if not job:
            return
        self._selected = str(job["activity_id"])
        self._profile_choices = {}
        self.search.set("")
        self._render_activity_list()
        self._render_detail()
        self.notebook.select(self.activity_page)
        self._open_detail()
        self.feedback.set("要修改等待中的人员或奖励，请先取消对应旧任务，再选择人员和奖励加入队列。")

    def _build_profiles(self):
        top = ttk.Frame(self.profile_page)
        top.pack(fill="x", pady=(2, 12))
        ttk.Label(top, text="已保存的参与人", style="Heading.TLabel").pack(side="left")
        ttk.Button(top, text="新增档案", style="Accent.TButton", command=self._profile_editor).pack(side="right")
        ttk.Button(top, text="编辑", command=self._edit_selected_profile).pack(side="right", padx=8)
        ttk.Button(top, text="删除", style="Danger.TButton", command=self._delete_profile).pack(side="right")
        self.profile_tree = self._tree(self.profile_page, ("name", "major", "class_name", "student_id", "phone", "email", "reward"), ("姓名", "专业", "班级", "学号", "手机号", "邮箱", "奖励偏好"), (90, 190, 100, 130, 130, 200, 100))
        self.profile_tree.bind("<Double-1>", lambda _e: self._edit_selected_profile())
        ttk.Label(self.profile_page, text="个人资料保存在本机。奖励偏好可在每次报名时单独调整；WPS 登录请在「连接与设置」中打开浏览器完成。", style="Small.TLabel", wraplength=1120).pack(fill="x", pady=12)

    def _render_profiles(self):
        selected = self.profile_tree.selection()
        self.profile_tree.delete(*self.profile_tree.get_children())
        for profile in self._profiles:
            self.profile_tree.insert("", "end", iid=str(profile["id"]), values=tuple(profile.get(key, "") for key, _ in PROFILE_FIELDS) + ("思政学分" if profile.get("reward", "credit") == "credit" else "学院积分",))
        if selected and self.profile_tree.exists(selected[0]):
            self.profile_tree.selection_set(selected[0])

    def _dialog(self, title, width=610, height=580):
        dialog = tk.Toplevel(self.root)
        dialog.title(title)
        scale = self.root._ui_scale
        actual_width = min(round(width*scale), self.root.winfo_screenwidth()-80)
        actual_height = min(round(height*scale), self.root.winfo_screenheight()-100)
        dialog.geometry(f"{actual_width}x{actual_height}")
        dialog.minsize(min(actual_width, round(width*scale)), min(actual_height, round(500*scale)))
        dialog.configure(bg=PALETTE["canvas"])
        dialog.transient(self.root)
        dialog.grab_set()
        dialog.bind("<Escape>", lambda _e: dialog.destroy())
        dialog.after_idle(lambda: self._paper_controls(dialog) if dialog.winfo_exists() else None)
        return dialog

    def _paper_controls(self, parent):
        """Give nested dialog and settings surfaces the shared white treatment."""
        for child in parent.winfo_children():
            if isinstance(child, ttk.Frame) and not child.cget("style"):
                child.configure(style="Paper.TFrame")
            elif isinstance(child, ttk.Label):
                current = str(child.cget("style"))
                if current in {"", "TLabel", "Small.TLabel", "Muted.TLabel", "Heading.TLabel"}:
                    child.configure(background=PALETTE["paper"])
                wrap = int(child.cget("wraplength") or 0)
                if wrap and not getattr(child, "_ui_wrap_scaled", False):
                    child.configure(wraplength=round(wrap*self.root._ui_scale))
                    child._ui_wrap_scaled = True
            self._paper_controls(child)

    def _edit_selected_profile(self):
        selection = self.profile_tree.selection()
        if selection:
            self._profile_editor(self.store.get_profile(selection[0]))

    def _profile_editor(self, profile=None):
        profile = profile or {}
        dialog = self._dialog("编辑个人档案" if profile else "新增个人档案", 700, 720)
        canvas = tk.Canvas(dialog, background=PALETTE["canvas"], highlightthickness=0)
        scroll = ttk.Scrollbar(dialog, orient="vertical", command=canvas.yview)
        canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        canvas.pack(side="left", fill="both", expand=True)
        frame = ttk.Frame(canvas, padding=25)
        inside = canvas.create_window((0,0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _e: canvas.configure(scrollregion=canvas.bbox("all")))
        canvas.bind("<Configure>", lambda e: canvas.itemconfigure(inside, width=e.width))
        dialog.bind("<MouseWheel>", lambda e: canvas.yview_scroll(int(-e.delta/120), "units"))
        ttk.Label(frame, text="个人档案", style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="专业和班级读取下方活动的 WPS 表单；学号与手机号保留原始格式。", style="Small.TLabel", wraplength=620).grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        activities = sort_activities([a for a in self._activities if a.get("form_url")])
        choices = {f"{_when(a.get('starts_at'), True)} · {a.get('title', '')[:40]} · {str(a['id'])[-6:]}": str(a["id"]) for a in activities}
        default_id = profile.get("form_source_activity_id") or self._selected
        if str(default_id) not in choices.values():
            default_id = next((str(a["id"]) for a in activities if _activity_state(a) not in {"已截止", "已取消", "已暂停", "已延期"}), str(activities[0]["id"]) if activities else None)
        source = tk.StringVar(value=next((label for label,identifier in choices.items() if identifier == str(default_id)), ""))
        ttk.Label(frame, text="选项来源").grid(row=2, column=0, sticky="w", padx=(0,18))
        source_row = ttk.Frame(frame)
        source_row.grid(row=2, column=1, sticky="ew")
        source_box = ttk.Combobox(source_row, textvariable=source, values=tuple(choices), state="readonly", width=34)
        source_box.pack(side="left", fill="x", expand=True)
        source_status = tk.StringVar(value="选择活动后读取专业选项和班级格式。" if choices else "先接收或补录活动通知，才能读取对应表单选项。")
        ttk.Label(frame, textvariable=source_status, style="Small.TLabel", wraplength=620).grid(row=3, column=0, columnspan=2, sticky="w", pady=(6,8))
        variables = {}
        widgets = {}
        hints = {}
        field_errors = {}
        first = None
        positions = {"name":4, "major":5, "class_name":7, "student_id":9, "phone":10, "email":11}
        for key, label in PROFILE_FIELDS:
            index = positions[key]
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", padx=(0, 18), pady=7)
            var = tk.StringVar(value=str(profile.get(key, "")))
            variables[key] = var
            entry = ttk.Combobox(frame, textvariable=var, state="readonly") if key in {"major", "class_name"} else ttk.Entry(frame, textvariable=var)
            widgets[key] = entry
            entry.grid(row=index, column=1, sticky="ew", pady=7)
            field_errors[key] = ttk.Label(frame, text="", foreground=PALETTE["red"], style="Paper.TLabel", wraplength=round(150*self.root._ui_scale))
            if key in {"major", "class_name"}:
                hints[key] = tk.StringVar(value="等待读取表单…")
                ttk.Label(frame, textvariable=hints[key], style="Small.TLabel", wraplength=510, justify="left").grid(row=index+1, column=1, sticky="w", pady=(0,6))
            first = first or entry
        reward = tk.StringVar(value="思政学分" if profile.get("reward", "credit") == "credit" else "学院积分")
        ttk.Label(frame, text="奖励偏好").grid(row=12, column=0, sticky="w", pady=7)
        ttk.Combobox(frame, textvariable=reward, values=("思政学分", "学院积分"), state="readonly").grid(row=12, column=1, sticky="ew", pady=7)
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, foreground=PALETTE["red"], wraplength=620).grid(row=13, column=0, columnspan=2, sticky="w", pady=8)
        current_fields = {}
        requested = {}
        rendered = None

        def load_source(force=False):
            identifier = choices.get(source.get())
            current_fields.clear()
            activity = self.store.get_activity(identifier) if identifier else None
            fields = (activity or {}).get("profile_fields") or {}
            for key in ("major", "class_name"):
                field = fields.get(key) or {}
                options = field.get("options") or []
                choice = field.get("kind") != "text"
                if isinstance(widgets[key], ttk.Combobox) != choice:
                    widgets[key].destroy()
                    widgets[key] = ttk.Combobox(frame, textvariable=variables[key], state="readonly") if choice else ttk.Entry(frame, textvariable=variables[key])
                    widgets[key].grid(row=positions[key], column=1, sticky="ew", pady=7)
                if choice:
                    widgets[key].configure(values=options, state="readonly")
                title = field.get("title", "")
                description = field.get("description", "")
                hint = "\n".join(str(text) for text in (title, description) if text)
                if field.get("kind") == "choice":
                    hint += "\n从表单实际选项中选择。"
                elif field:
                    hint += "\n此项为文字题，请按表单格式填写。"
                hints[key].set(_display_text(hint or "尚未读到该字段，请点击「重新读取」。"))
                if field:
                    current_fields[key] = field
            if fields:
                source_status.set("已读取该活动表单的专业选项和班级要求；切换活动可核对不同表单。")
            elif not identifier:
                source_status.set("先接收或补录活动通知，才能读取对应表单选项。")
            else:
                source_status.set("尚未取得该表单的字段，请点击「重新读取」。")
            stamp = _date(((activity or {}).get("form_constraints") or {}).get("checked_at"))
            fresh = bool(stamp and datetime.now(BEIJING)-stamp < timedelta(minutes=5))
            inspect = getattr(self.browser, "inspect_activity", None)
            if identifier and callable(inspect) and (force or (identifier not in requested and (not fields or not fresh))):
                requested[identifier] = datetime.now(BEIJING)
                self._list_checks[identifier] = requested[identifier]
                source_status.set("正在读取此活动的 WPS 选项和填写格式…")
                inspect(identifier)
            return json.dumps((identifier, fields), sort_keys=True, ensure_ascii=False)

        def watch_source():
            nonlocal rendered
            if not dialog.winfo_exists():
                return
            identifier = choices.get(source.get())
            activity = self.store.get_activity(identifier) if identifier else None
            snapshot = json.dumps((identifier, (activity or {}).get("profile_fields") or {}), sort_keys=True, ensure_ascii=False)
            if snapshot != rendered:
                rendered = load_source()
            if identifier in requested and not current_fields and datetime.now(BEIJING)-requested[identifier] > timedelta(seconds=40):
                source_status.set("暂未读到选项。请检查网络与 WPS 登录后点击「重新读取」；已有内容已保留。")
            dialog.after(600, watch_source)

        def changed_source(_event=None):
            nonlocal rendered
            rendered = load_source()

        source_box.bind("<<ComboboxSelected>>", changed_source)
        ttk.Button(source_row, text="重新读取", command=lambda: load_source(force=True)).pack(side="right", padx=(8,0))

        def save():
            for label in field_errors.values():
                label.grid_remove()
            def field_error(key, message):
                label = field_errors[key]
                label.configure(text=message)
                label.grid(row=positions[key], column=2, sticky="w", padx=(10, 0))
            values = {key: var.get().strip() for key, var in variables.items()}
            if not choices.get(source.get()) or any(key not in current_fields for key in ("major", "class_name")):
                error.set("请先读取来源表单的专业、班级要求，再保存档案；现有输入已保留。")
                return
            missing = [label for key, label in PROFILE_FIELDS if not values[key]]
            if missing:
                error.set("请补全：" + "、".join(missing))
                for key, label in PROFILE_FIELDS:
                    if not values[key]:
                        field_error(key, "请填写" + label)
                return
            for key, label in (("major", "专业"), ("class_name", "班级")):
                options = (current_fields.get(key) or {}).get("options") or []
                if options and values[key] not in options:
                    error.set(f"{label}请选择当前表单提供的选项。切换来源不会自动覆盖已填写内容。")
                    field_error(key, "请选择该表单的实际选项")
                    return
            if not re.fullmatch(r"[^\s@]+@[^\s@]+\.[^\s@]+", values["email"]):
                error.set("请检查邮箱格式。")
                field_error("email", "邮箱格式不正确")
                return
            if not re.fullmatch(r"\+?[0-9 ()-]{7,20}", values["phone"]):
                error.set("请检查手机号格式。")
                field_error("phone", "手机号格式不正确")
                return
            values["reward"] = "credit" if reward.get() == "思政学分" else "cloud"
            values["form_source_activity_id"] = choices.get(source.get())
            if profile.get("id"):
                values["id"] = profile["id"]
            try:
                self.store.save_profile(values)
                dialog.destroy()
                self.feedback.set("个人档案已保存。")
                self._refresh(force=True)
            except Exception as exc:
                error.set(_display_text(exc))

        buttons = ttk.Frame(frame)
        buttons.grid(row=14, column=0, columnspan=2, sticky="e", pady=(0,10))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=8)
        ttk.Button(buttons, text="保存档案", style="Accent.TButton", command=save).pack(side="left")
        frame.columnconfigure(1, weight=1)
        first.focus_set()
        rendered = load_source()
        dialog.after(600, watch_source)
        return dialog

    def _delete_profile(self):
        selection = self.profile_tree.selection()
        if not selection:
            return
        profile = self.store.get_profile(selection[0])
        if not profile:
            return
        if messagebox.askyesno("删除个人档案", f"删除 {profile.get('name')} 的本机档案？\n有待执行任务时请先取消任务。", parent=self.root):
            try:
                self.store.delete_profile(selection[0])
                self._refresh(force=True)
                self.feedback.set("个人档案已删除。")
            except Exception as exc:
                self._error("删除失败", exc)

    def _activity_editor(self, activity):
        dialog = self._dialog("核对活动 · 全部时间使用北京时间", 690, 800)
        editor_canvas = tk.Canvas(dialog, background=PALETTE["canvas"], highlightthickness=0)
        editor_scroll = ttk.Scrollbar(dialog, orient="vertical", command=editor_canvas.yview)
        editor_scroll.pack(side="right", fill="y")
        editor_canvas.pack(side="left", fill="both", expand=True)
        editor_canvas.configure(yscrollcommand=editor_scroll.set)
        frame = ttk.Frame(editor_canvas, padding=24)
        editor_window = editor_canvas.create_window((0, 0), window=frame, anchor="nw")
        frame.bind("<Configure>", lambda _e: editor_canvas.configure(scrollregion=editor_canvas.bbox("all")))
        editor_canvas.bind("<Configure>", lambda e: editor_canvas.itemconfigure(editor_window, width=e.width))
        dialog.bind("<MouseWheel>", lambda e: editor_canvas.yview_scroll(int(-e.delta / 120), "units"))
        ttk.Label(frame, text="核对通知与报名窗口", style="Heading.TLabel").grid(row=0, column=0, columnspan=2, sticky="w", pady=(0, 8))
        ttk.Label(frame, text="时间格式：2026-10-29 14:00；以北京时间保存。", style="Small.TLabel").grid(row=1, column=0, columnspan=2, sticky="w", pady=(0, 12))
        fields = (("title", "活动名称"), ("starts_at", "活动开始"), ("ends_at", "活动结束"), ("opens_at", "报名开放"), ("closes_at", "报名截止"), ("location", "活动地点"), ("capacity", "名额（可空）"), ("form_url", "WPS 表单链接"), ("credit", "思政学分（可空）"), ("cloud", "学院积分（可空）"))
        variables = {}
        for index, (key, label) in enumerate(fields, 2):
            value = activity.get(key, "")
            if key.endswith("_at"):
                value = "" if not value else _when(value)
            elif key in ("credit", "cloud"):
                value = (activity.get("rewards") or {}).get(key, "")
            variables[key] = tk.StringVar(value=str(value or ""))
            ttk.Label(frame, text=label).grid(row=index, column=0, sticky="w", pady=5, padx=(0, 15))
            widget = ttk.Entry(frame, textvariable=variables[key])
            widget.grid(row=index, column=1, sticky="ew", pady=5)
        issues = activity.get("issues") or []
        acknowledged = tk.BooleanVar(value=not issues)
        issue_text = "待核对：" + "；".join(map(str, issues)) if issues else "保存时将校验活动与报名时间。"
        ttk.Label(frame, text=issue_text, foreground=PALETTE["amber"], wraplength=620).grid(row=13, column=0, columnspan=2, sticky="w", pady=(12, 5))
        if issues:
            ttk.Checkbutton(frame, text="已对照原通知核实并修正以上问题", variable=acknowledged).grid(row=14, column=0, columnspan=2, sticky="w")
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, foreground=PALETTE["red"], wraplength=620).grid(row=15, column=0, columnspan=2, sticky="w", pady=8)

        def save():
            try:
                values = {key: var.get().strip() for key, var in variables.items()}
                if not values["title"]:
                    raise ValueError("请填写活动名称。")
                for key in ("starts_at", "ends_at", "opens_at", "closes_at"):
                    if not values[key]:
                        raise ValueError("请补全全部活动和报名时间。")
                    values[key] = activity[key] if values[key] == _when(activity.get(key)) else _date(values[key]).isoformat()
                if _date(values["starts_at"]) >= _date(values["ends_at"]):
                    raise ValueError("活动结束必须晚于活动开始。")
                if _date(values["opens_at"]) >= _date(values["closes_at"]):
                    raise ValueError("报名截止必须晚于报名开放。")
                if _date(values["closes_at"]) > _date(values["starts_at"]) or _date(values["opens_at"]) > _date(values["starts_at"]):
                    raise ValueError("报名窗口不能晚于活动开始，请对照通知核实。")
                url = urlparse(values["form_url"])
                if url.scheme != "https" or url.hostname != "f.wps.cn" or not re.fullmatch(r"/g/[A-Za-z0-9]+/?", url.path) or url.username or url.password:
                    raise ValueError("请填写 https://f.wps.cn/g/ 开头的 WPS 表单链接。")
                if values["capacity"] and (not values["capacity"].isdigit() or int(values["capacity"]) <= 0):
                    raise ValueError("名额须为正整数，或留空。")
                if not acknowledged.get():
                    raise ValueError("请先核对原通知，并勾选确认修正问题。")
                values["capacity"] = int(values["capacity"]) if values["capacity"] else None
                values["rewards"] = {key: values.pop(key) for key in ("credit", "cloud")}
                values["issues"] = []
                self.store.update_activity(activity["id"], values)
                dialog.destroy()
                self._refresh(force=True)
                self.feedback.set("活动信息已保存；请再次核对原通知后选择参与人。")
            except Exception as exc:
                error.set(_display_text(exc))

        buttons = ttk.Frame(frame)
        buttons.grid(row=16, column=0, columnspan=2, sticky="e", pady=(5, 0))
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="left", padx=8)
        ttk.Button(buttons, text="保存核对结果", style="Accent.TButton", command=save).pack(side="left")
        frame.columnconfigure(1, weight=1)

    def _import_dialog(self):
        dialog = self._dialog("补录历史通知", 770, 660)
        frame = ttk.Frame(dialog, padding=24)
        frame.pack(fill="both", expand=True)
        ttk.Label(frame, text="粘贴群内的原始活动通知", style="Heading.TLabel").pack(anchor="w")
        ttk.Label(frame, text="用于补录已有消息；日常新消息由 QQ 后台接收。缺少日期或报名窗口时，请在导入后人工核对。", style="Small.TLabel", wraplength=710).pack(fill="x", pady=(8, 14))
        row = ttk.Frame(frame)
        row.pack(fill="x", pady=(0, 12))
        ttk.Label(row, text="原消息时间（北京时间）").pack(side="left")
        observed = tk.StringVar(value=datetime.now(BEIJING).strftime("%Y-%m-%d %H:%M"))
        ttk.Entry(row, textvariable=observed, width=23).pack(side="left", padx=10)
        text = tk.Text(frame, wrap="word", font=("Microsoft YaHei UI", 11), relief="flat", bg="white", padx=14, pady=12, undo=True)
        text.pack(fill="both", expand=True)
        error = tk.StringVar()
        ttk.Label(frame, textvariable=error, foreground=PALETTE["red"], wraplength=710).pack(fill="x", pady=8)

        def import_file():
            path = filedialog.askopenfilename(parent=dialog, title="导入通知文本", filetypes=(("文本通知", "*.txt"), ("所有文件", "*.*")))
            if path:
                try:
                    data = Path(path).read_bytes()
                    try:
                        content = data.decode("utf-8-sig")
                    except UnicodeDecodeError:
                        content = data.decode("gb18030")
                    text.delete("1.0", "end")
                    text.insert("1.0", content)
                except Exception as exc:
                    error.set(f"无法读取文件：{exc}")

        def import_notice():
            try:
                from .parser import parse_notice
                content = text.get("1.0", "end").strip()
                if not content:
                    raise ValueError("请粘贴通知正文。")
                notice = parse_notice(content, observed_at=_date(observed.get()))
                if not notice:
                    raise ValueError("尚未识别到活动特征。请包含活动名称、日期及报名通知正文。")
                result = self.store.upsert_activity(notice)
                self._selected = str(result["id"] if isinstance(result, dict) else result) if result else None
                dialog.destroy()
                self.search.set("")
                self._refresh(force=True)
                self._open_detail()
                self.feedback.set("通知已导入。请核对活动详情；尚未加入报名队列。")
            except Exception as exc:
                error.set(_display_text(exc))

        buttons = ttk.Frame(frame)
        buttons.pack(fill="x", pady=(4, 0))
        ttk.Button(buttons, text="从文本文件读取", command=import_file).pack(side="left")
        ttk.Button(buttons, text="导入并核对", style="Accent.TButton", command=import_notice).pack(side="right")
        ttk.Button(buttons, text="取消", command=dialog.destroy).pack(side="right", padx=8)
        text.focus_set()

    def _build_settings(self):
        self.settings_canvas = tk.Canvas(self.settings_page, background=PALETTE["canvas"], highlightthickness=0)
        scrollbar = ttk.Scrollbar(self.settings_page, orient="vertical", command=self.settings_canvas.yview)
        scrollbar.pack(side="right", fill="y")
        self.settings_canvas.pack(side="left", fill="both", expand=True)
        self.settings_canvas.configure(yscrollcommand=scrollbar.set)
        host = ttk.Frame(self.settings_canvas)
        settings_window = self.settings_canvas.create_window((0, 0), window=host, anchor="nw")
        host.columnconfigure(0, weight=1, uniform="settings")
        host.columnconfigure(1, weight=1, uniform="settings")
        left = ttk.Frame(host, style="Paper.TFrame", padding=(20, 18, 20, 22))
        right = ttk.Frame(host, style="Paper.TFrame", padding=(20, 18, 20, 22))
        left.grid(row=0, column=0, sticky="nsew", padx=(0, 9))
        right.grid(row=0, column=1, sticky="nsew", padx=(9, 0))
        host.bind("<Configure>", lambda _e: self.settings_canvas.configure(scrollregion=self.settings_canvas.bbox("all")))
        def resize(event):
            self.settings_canvas.itemconfigure(settings_window, width=event.width)
            # Keep explanatory text inside its own column at Windows display scaling.
            for panel in (left, right):
                for widget in panel.winfo_children():
                    if isinstance(widget, ttk.Label) and int(widget.cget("wraplength") or 0):
                        widget.configure(wraplength=max(210, event.width // 2 - 64))
        self.settings_canvas.bind("<Configure>", resize)
        ttk.Label(left, text="QQ 后台接收", style="Heading.TLabel").pack(anchor="w", pady=(4, 10))
        self.receiver_info = tk.StringVar(value="正在读取本机接收状态…")
        ttk.Label(left, textvariable=self.receiver_info, wraplength=530, justify="left").pack(fill="x", pady=(0, 15))
        ttk.Label(left, text="接收群号", font=("Microsoft YaHei UI", 10, "bold")).pack(anchor="w")
        ttk.Label(left, text="只接收这些群的通知；多个群号用换行、空格或逗号分隔。", style="Small.TLabel", wraplength=530).pack(fill="x", pady=(4, 8))
        self.groups_text = tk.Text(left, width=25, height=4, font=("Consolas", 11), bg="white", relief="flat", padx=10, pady=10)
        self.groups_text.pack(fill="x")
        try:
            groups = self.store.get_setting("group_ids", self.store.get_setting("groups", []))
            if isinstance(groups, str):
                try:
                    groups = json.loads(groups)
                except ValueError:
                    groups = re.split(r"[\s,，;；]+", groups)
            self.groups_text.insert("1.0", "\n".join(map(str, groups or [])))
        except Exception:
            pass
        ttk.Button(left, text="保存接收群", style="Accent.TButton", command=self._save_groups).pack(anchor="w", pady=(10, 20))
        ttk.Separator(left).pack(fill="x", pady=(0, 18))
        ttk.Label(left, text="QQ 接收启动器", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(left, text="选择 QQ 安装目录，配置独立接收启动器。启动器保存在本机资料目录；QQ 更新后可重新检查接入。", style="Small.TLabel", wraplength=530).pack(fill="x", pady=(6, 10))
        self.qq_path = tk.StringVar(value=str(self.store.get_setting("qq_root", "") or ""))
        pathrow = ttk.Frame(left)
        pathrow.pack(fill="x")
        ttk.Entry(pathrow, textvariable=self.qq_path).pack(side="left", fill="x", expand=True)
        ttk.Button(pathrow, text="浏览", command=self._browse_qq).pack(side="left", padx=(6, 0))
        controls = ttk.Frame(left)
        controls.pack(fill="x", pady=10)
        ttk.Button(controls, text="自动查找 QQ", command=self._discover_qq).grid(row=0, column=0, sticky="w", padx=(0, 8), pady=(0, 8))
        ttk.Button(controls, text="配置接收启动器", command=lambda: self._receiver_action("install")).grid(row=0, column=1, sticky="w", pady=(0, 8))
        ttk.Button(controls, text="恢复接收配置", command=lambda: self._receiver_action("restore")).grid(row=1, column=0, columnspan=2, sticky="w")
        ttk.Label(left, text="配置后需通过下方按钮重启 QQ。重启将中断当前聊天通话、文件传输和会议，请先处理完毕。", foreground=PALETTE["amber"], wraplength=530).pack(fill="x", pady=8)
        ttk.Button(left, text="重启 QQ 以加载接收模块…", command=lambda: self._receiver_action("restart")).pack(anchor="w")
        ttk.Label(right, text="WPS 登录与执行", style="Heading.TLabel").pack(anchor="w", pady=(4, 10))
        ttk.Label(right, textvariable=self.wps_account_info, wraplength=520, justify="left").pack(fill="x", pady=(0, 12))
        self.wps_info = tk.StringVar(value=self._browser_status)
        ttk.Label(right, textvariable=self.wps_info, wraplength=520).pack(fill="x", pady=(0, 15))
        ttk.Label(right, text="打开登录窗口后，在 Edge 中完成 WPS 登录。程序每 2 秒自动核验，确认后自动恢复后台报名；也可点击顶部刷新立即重查。", style="Small.TLabel", wraplength=520).pack(fill="x", pady=(0, 14))
        ttk.Button(right, text="打开 WPS 登录窗口", style="Accent.TButton", command=lambda: self._browser_action("open_login")).pack(anchor="w", pady=(0, 8))
        ttk.Button(right, text="我已完成登录，继续后台报名", command=lambda: self._browser_action("finish_login")).pack(anchor="w")
        ttk.Separator(right).pack(fill="x", pady=22)
        ttk.Label(right, text="运行说明", font=("Microsoft YaHei UI", 12, "bold")).pack(anchor="w")
        ttk.Label(right, text="• 时间统一使用北京时间（UTC+8）。\n\n• 工具需持续运行，电脑需保持唤醒。\n\n• 接收连接只在收到真实心跳后显示在线。\n\n• 验证码、未知必填项和不明提交结果会停止自动处理，原因显示在报名队列。\n\n• 关闭工具会停止后台接收与执行；重启后会核对持久化队列。", style="Muted.TLabel", wraplength=520, justify="left").pack(fill="x", pady=10)
        ttk.Label(right, text=f"本机资料目录\n{self.data_dir}", style="Small.TLabel", wraplength=520).pack(fill="x", pady=12)
        self._paper_controls(left)
        self._paper_controls(right)

    def _save_groups(self):
        groups = list(dict.fromkeys(filter(None, re.split(r"[\s,，;；]+", self.groups_text.get("1.0", "end").strip()))))
        if any(not re.fullmatch(r"[0-9]{5,12}", group) for group in groups):
            messagebox.showwarning("检查群号", "群号应为 5–12 位数字，多个群号请用空格或换行分隔。", parent=self.root)
            return
        def save():
            self.bridge.set_groups(groups)
            self.store.set_setting("group_ids", groups)
            return "接收群已保存。" if groups else "已清空接收群；不会接收任何群的活动。"
        self._async("保存接收群", save)

    def _browse_qq(self):
        path = filedialog.askdirectory(parent=self.root, title="选择 QQ 安装目录")
        if path:
            self.qq_path.set(path)

    def _discover_qq(self):
        def discover():
            from .qq_setup import discover_qq_root
            result = discover_qq_root()
            self._events.put(("qq_path", str(result) if result else ""))
            return "已找到 QQ 安装目录。" if result else "未自动找到 QQ，请点击浏览选择安装目录。"
        self._async("查找 QQ", discover)

    def _receiver_action(self, action):
        value = self.qq_path.get().strip()
        if not value or not Path(value).is_dir():
            messagebox.showwarning("选择 QQ 目录", "请先选择有效的 QQ 安装目录。", parent=self.root)
            return
        root = Path(value)
        if action == "restart" and not messagebox.askyesno("重启 QQ", "将关闭并重新启动 QQ。正在进行的通话、会议和文件传输会中断。\n\n确认现在重启 QQ？", parent=self.root):
            return
        def execute():
            from . import qq_setup
            if action == "install":
                result = qq_setup.install_receiver(root, self.data_dir)
            elif action == "restore":
                result = qq_setup.restore_receiver(root, self.data_dir)
            else:
                result = qq_setup.restart_qq(root, self.data_dir)
            self.store.set_setting("qq_root", str(root))
            label = {"install": "接收启动器配置", "restore": "接收配置恢复", "restart": "QQ 重启"}[action]
            detail = result.get("message") or result.get("detail") or "" if isinstance(result, dict) else str(result or "")
            return label + "已执行。" + str(detail) + (" 请点击工具中的重启按钮加载接收器，再等待真实心跳。" if action == "install" else "")
        self._async({"install": "配置接收启动器", "restore": "恢复接收配置", "restart": "重启 QQ"}[action], execute)

    def _browser_action(self, action):
        self._async("WPS 登录", lambda: getattr(self.browser, action)())
        self.feedback.set("已请求打开 WPS 登录窗口。" if action == "open_login" else "已请求结束登录并继续后台处理，状态以实际检查为准。")

    def _open_url(self, url):
        parsed = urlparse(str(url))
        if parsed.scheme != "https" or parsed.hostname != "f.wps.cn":
            messagebox.showwarning("检查表单链接", "请先在活动详情中核对 WPS 表单链接。", parent=self.root)
            return
        self._async("打开表单", lambda: webbrowser.open(str(url)))

    def _async(self, label, fn):
        def work():
            try:
                result = fn()
                self._events.put(("feedback", result if isinstance(result, str) else f"{label}请求已处理。"))
            except Exception as exc:
                self._events.put(("error", (label, str(exc))))
        threading.Thread(target=work, name="ui-action", daemon=True).start()

    def post_status(self, text):
        self._events.put(("browser", str(text)))

    def post_event(self, event):
        self._events.put(("bridge", event))

    def _refresh(self, force=False):
        activities = self.store.activities()
        profiles = self.store.profiles()
        jobs = self.store.jobs()
        snapshot = json.dumps((activities, profiles, jobs, [_activity_state(a) for a in activities]), sort_keys=True, ensure_ascii=False, default=str)
        if force or snapshot != self._snapshot:
            self._activities, self._profiles, self._jobs = activities, profiles, jobs
            self._snapshot = snapshot
            self._render_activity_list()
            self._render_profiles()
            self._render_jobs()
            self._render_detail()

    def _refresh_all(self, _event=None):
        if self._closed or self._refresh_busy:
            return
        self._refresh_busy = True
        self.refresh_button.configure(text="刷新中…")
        self.refresh_button.state(["disabled"])
        try:
            self._drain_events()
            self.clock.set(datetime.now(BEIJING).strftime("%Y/%m/%d  %H:%M"))
            self._refresh(force=True)
            self._update_connection_status()
            check = getattr(self.browser, "check_login", None)
            if callable(check):
                check()
            self._update_wps_status()
            inspect = getattr(self.browser, "inspect_activity", None)
            if callable(inspect):
                now = datetime.now(BEIJING)
                for activity in self._activities:
                    if activity.get("form_url") and _activity_state(activity) not in {"已截止", "已取消", "已暂停", "已延期"}:
                        inspect(str(activity["id"]))
                        self._list_checks[str(activity["id"])] = now
            self.feedback.set("页面已刷新；WPS 登录及可报名活动名额正在后台重新核验，结果会自动更新。")
        except Exception as exc:
            self.feedback.set(f"刷新失败：{exc}，请稍后重试。")
        finally:
            def ready():
                self._refresh_busy = False
                self.refresh_button.configure(text="刷新")
                self.refresh_button.state(["!disabled"])
            self.root.after(1000, ready)

    def _update_connection_status(self):
        state = self.bridge.status() or {}
        connected = bool(state.get("connected", False))
        self.connection.set("● QQ 接收在线" if connected else "○ QQ 接收未连接")
        self.connection_label.configure(foreground="#147D4E" if connected else PALETTE["muted"], background="#ECFDF3" if connected else "#F0F4F8")
        groups = state.get("groups") or []
        self.sidebar_groups.set("\n".join(map(str, groups[:2])) + (f"\n等 {len(groups)} 个群" if len(groups) > 2 else "") if groups else "尚未配置")
        detail = ["已收到接收器心跳" if connected else "等待 QQ 接收器心跳；请核对启动器和群号设置。"]
        receiver_states = {"disconnected": "未连接", "waiting_native": "等待 QQ 内部接口", "native_loaded": "内部接口已加载，等待监听", "listening": "正在监听消息", "unsupported": "当前 QQ 版本适配失败", "unknown": "状态尚未确认"}
        receiver_state = state.get("receiver_state") or "未报告"
        detail.append(f"接收状态：{receiver_states.get(receiver_state, receiver_state)}")
        detail.append(f"最近心跳：{_when(state.get('last_heartbeat'))}  ·  最近消息：{_when(state.get('last_message'))}")
        detail.append(f"配置群数：{len(groups)}  ·  本机端口：{state.get('port') or '未启动'}")
        self.receiver_info.set("\n".join(detail))

    def _drain_events(self):
        for _ in range(150):
            try:
                kind, value = self._events.get_nowait()
            except queue.Empty:
                break
            if kind == "browser":
                self._browser_status = _display_text(value)
                self.wps_info.set(_display_text(value))
            elif kind == "feedback":
                self.feedback.set(_display_text(value))
            elif kind == "qq_path":
                if value:
                    self.qq_path.set(value)
            elif kind == "error":
                self.feedback.set(_display_text(f"{value[0]}失败：{value[1]}"))
                messagebox.showerror(_display_text(value[0]) + "失败", _display_text(value[1]), parent=self.root)
            elif kind == "bridge" and isinstance(value, dict):
                # State itself is always fetched from BridgeServer.status().
                message = value.get("message") or value.get("detail")
                if message:
                    self.feedback.set(_display_text(message))
                elif value.get("type") == "activity_change":
                    self.feedback.set("收到活动变更通知，尚未找到唯一对应场次。请点击「变更通知」核对。")

    def _poll(self):
        if self._closed:
            return
        self.clock.set(datetime.now(BEIJING).strftime("%Y/%m/%d  %H:%M"))
        self._drain_events()
        self._update_wps_status()
        try:
            self._refresh()
            self._update_connection_status()
            self._last_poll_error = ""
        except Exception as exc:
            self.connection.set("○ 状态读取失败")
            self.connection_label.configure(foreground=PALETTE["red"])
            if str(exc) != self._last_poll_error:
                self.feedback.set(f"读取本机状态失败：{exc}")
                self._last_poll_error = str(exc)
        if not self._closed:
            self.root.after(1000, self._poll)

    def _error(self, label, exc):
        self.feedback.set(_display_text(f"{label}：{exc}"))
        messagebox.showerror(_display_text(label), _display_text(exc), parent=self.root)

    def _close(self):
        if self._closed:
            return
        self._closed = True
        # Browser stop may need to close a persistent browser; never block Tk.
        def stop_workers():
            for worker in (self.browser, self.bridge):
                try:
                    worker.stop()
                except Exception:
                    pass
        threading.Thread(target=stop_workers, name="ui-shutdown", daemon=True).start()
        # Dialog watchers and coalesced table paints belong to this Tk instance.
        # Cancel them before destroying their Tcl command names.
        for callback in self.root.tk.call("after", "info"):
            try:
                self.root.after_cancel(callback)
            except tk.TclError:
                pass
        self.root.destroy()

    def run(self):
        self.root.mainloop()
