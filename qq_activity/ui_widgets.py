# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Small native-Tk controls shared by the desktop presentation layer.

DataTable keeps the application's flat Treeview contract while drawing only
the rows currently in view. Row records never create widgets or canvas items.
"""
from __future__ import annotations

import math
import tkinter as tk
from tkinter import font as tkfont, ttk


_COLORS = {
    "paper": "#FFFFFF", "ink": "#1E293B", "muted": "#64748B",
    "line": "#E2E8F0", "hover": "#F8FAFC", "selected": "#EFF6FF",
    "blue": "#2563EB", "green": "#15803D", "red": "#B91C1C",
    "amber": "#A16207",
}
_BADGES = {
    "expired": ("#FEF2F2", "red"), "upcoming": ("#FEFCE8", "amber"),
    "available": ("#F0FDF4", "green"), "success": ("#F0FDF4", "green"),
    "warning": ("#FEFCE8", "amber"), "muted": ("#F1F5F9", "muted"),
}


class DataTable(ttk.Frame):
    """A single-selection, flat table with Treeview-compatible data methods."""

    def __init__(self, parent, columns, labels, widths, height=8):
        super().__init__(parent, style="Paper.TFrame")
        self.columns = tuple(str(column) for column in columns)
        self._scale = float(getattr(self.winfo_toplevel(), "_ui_scale", 1.0))
        self._font = tkfont.nametofont("TkDefaultFont").copy()
        self._bold_font = self._font.copy()
        self._bold_font.configure(weight="bold")
        self._heading_font = tkfont.nametofont("TkHeadingFont").copy()
        self._pad = max(8, round(14 * self._scale))
        self.row_height = max(round(64 * self._scale), self._font.metrics("linespace") * 2 + self._pad)
        self.header_height = max(round(44 * self._scale), self._heading_font.metrics("linespace") + self._pad)
        self._headings = {column: {"text": str(label), "anchor": "w", "command": None}
                          for column, label in zip(self.columns, labels)}
        self._column_options = {
            column: {"width": max(40, round(width * self._scale)),
                     "minwidth": min(round(width * self._scale), round(78 * self._scale)),
                     "anchor": "w", "stretch": True}
            for column, width in zip(self.columns, widths)
        }
        self._rows = {}
        self._order = []
        self._selected = ()
        self._focused = ""
        self._hover = ""
        self._tag_options = {}
        self._next_id = 1
        self._draw_pending = None
        self._actual_widths = []
        self._content_width = 1
        self._region = None
        self._xscroll_visible = False
        self._status_columns = {index for index, column in enumerate(self.columns)
                                if column.lower() in {"state", "status"}}

        self.header = tk.Canvas(self, height=self.header_height, background=_COLORS["paper"],
                                highlightthickness=0, borderwidth=0, takefocus=0)
        self.canvas = tk.Canvas(self, height=self.row_height * height, background=_COLORS["paper"],
                                highlightthickness=0, borderwidth=0, takefocus=1,
                                yscrollincrement=self.row_height)
        self._yscroll = ttk.Scrollbar(self, orient="vertical", command=self.yview)
        self._xscroll = ttk.Scrollbar(self, orient="horizontal", command=self.xview)
        self.canvas.configure(yscrollcommand=self._on_yscroll, xscrollcommand=self._on_xscroll)
        self.header.grid(row=0, column=0, sticky="ew")
        self.canvas.grid(row=1, column=0, sticky="nsew")
        self._yscroll.grid(row=1, column=1, sticky="ns")
        self._xscroll.grid(row=2, column=0, sticky="ew")
        self._xscroll.grid_remove()
        self.rowconfigure(1, weight=1)
        self.columnconfigure(0, weight=1)
        self.canvas.bind("<Configure>", self._queue_draw)
        self.canvas.bind("<Button-1>", self._click)
        self.canvas.bind("<Motion>", self._motion)
        self.canvas.bind("<Leave>", self._leave)
        self.canvas.bind("<FocusIn>", self._queue_draw)
        self.canvas.bind("<FocusOut>", self._queue_draw)
        self.canvas.bind("<MouseWheel>", self._wheel)
        self.canvas.bind("<Shift-MouseWheel>", self._wheel_horizontal)
        self.canvas.bind("<Button-4>", lambda _e: self._scroll_rows(-1))
        self.canvas.bind("<Button-5>", lambda _e: self._scroll_rows(1))
        for key in ("Up", "Down", "Home", "End", "Prior", "Next"):
            self.canvas.bind(f"<KeyPress-{key}>", self._key)
        self.header.bind("<Button-1>", self._header_click)
        ttk.Frame.bind(self, "<Destroy>", self._destroyed, add="+")
        self._queue_draw()

    # Bind application callbacks to the focusable body canvas, where mouse and
    # keyboard events occur. This also keeps Return/double-click bindings native.
    def bind(self, sequence=None, func=None, add=None):
        return self.canvas.bind(sequence, func, add)

    def event_generate(self, sequence, **kwargs):
        return self.canvas.event_generate(sequence, **kwargs)

    def focus_set(self):
        self.canvas.focus_set()

    @staticmethod
    def _ids(ids):
        if len(ids) == 1 and isinstance(ids[0], (tuple, list)):
            ids = ids[0]
        return tuple(str(iid) for iid in ids)

    def selection(self):
        return self._selected

    def selection_set(self, *ids):
        ids = self._ids(ids)
        if ids:
            self._require(ids[0])
        self._change_selection(ids[:1])
        if ids:
            self._focused = ids[0]

    def selection_remove(self, *ids):
        removed = set(self._ids(ids))
        self._change_selection(tuple(iid for iid in self._selected if iid not in removed))

    def _change_selection(self, selected):
        if selected != self._selected:
            self._selected = selected
            self._queue_draw()
            self.canvas.event_generate("<<TreeviewSelect>>", when="tail")

    def get_children(self, item=""):
        return tuple(self._order) if not item else ()

    def insert(self, parent, index, iid=None, values=(), tags=(), **kwargs):
        if parent:
            raise tk.TclError("DataTable supports flat rows only")
        if iid is None:
            while f"I{self._next_id:03X}" in self._rows:
                self._next_id += 1
            iid = f"I{self._next_id:03X}"
            self._next_id += 1
        iid = str(iid)
        if iid in self._rows:
            raise tk.TclError(f"Item {iid} already exists")
        self._rows[iid] = {"values": tuple(values), "tags": (tags,) if isinstance(tags, str) else tuple(tags),
                           "text": kwargs.get("text", ""), "image": kwargs.get("image", ""), "open": False}
        position = len(self._order) if index == "end" else max(0, min(int(index), len(self._order)))
        self._order.insert(position, iid)
        self._queue_draw()
        return iid

    def delete(self, *ids):
        ids = set(self._ids(ids))
        for iid in ids:
            self._require(iid)
        for iid in ids:
            del self._rows[iid]
        self._order = [iid for iid in self._order if iid not in ids]
        if self._focused in ids:
            self._focused = ""
        if self._hover in ids:
            self._hover = ""
        self._change_selection(tuple(iid for iid in self._selected if iid not in ids))
        self._queue_draw()

    def exists(self, iid):
        return str(iid) in self._rows

    def _require(self, iid):
        if str(iid) not in self._rows:
            raise tk.TclError(f"Item {iid} not found")
        return self._rows[str(iid)]

    def item(self, iid, option=None, **changes):
        row = self._require(iid)
        if changes:
            for name, value in changes.items():
                row[name] = (value,) if name == "tags" and isinstance(value, str) else (
                    tuple(value) if name in {"values", "tags"} else value)
            self._queue_draw()
        return row.get(option, "") if option else dict(row)

    def tag_configure(self, tag, option=None, **kwargs):
        options = self._tag_options.setdefault(tag, {})
        if kwargs:
            options.update(kwargs)
            self._queue_draw()
        return options.get(option, "") if option else dict(options)

    def _column_key(self, column):
        if isinstance(column, int):
            return self.columns[column]
        if str(column).startswith("#"):
            return self.columns[int(str(column)[1:]) - 1]
        if column not in self.columns:
            raise tk.TclError(f"Invalid column {column}")
        return column

    def heading(self, column, option=None, **kwargs):
        options = self._headings[self._column_key(column)]
        if kwargs:
            options.update(kwargs)
            self._queue_draw()
        return options.get(option, "") if option else dict(options)

    def column(self, column, option=None, **kwargs):
        options = self._column_options[self._column_key(column)]
        if kwargs:
            options.update(kwargs)
            self._queue_draw()
        return options.get(option, "") if option else dict(options)

    def focus(self, iid=None):
        if iid is not None:
            if iid:
                self._require(iid)
            self._focused = str(iid)
            self._queue_draw()
        return self._focused

    def see(self, iid):
        self._require(iid)
        # Geometry changes are coalesced, but see() must work immediately after
        # an insert, before Tk has had a chance to draw those new records.
        self._layout()
        top = self._order.index(str(iid)) * self.row_height
        visible_top = self.canvas.canvasy(0)
        visible_height = self.canvas.winfo_height()
        if top < visible_top:
            self.canvas.yview_moveto(top / max(1, len(self._order) * self.row_height))
        elif top + self.row_height > visible_top + visible_height:
            self.canvas.yview_moveto((top + self.row_height - visible_height) /
                                     max(1, len(self._order) * self.row_height))
        self._queue_draw()

    def yview(self, *args):
        return self.canvas.yview(*args)

    def yview_moveto(self, fraction):
        self.canvas.yview_moveto(fraction)

    def yview_scroll(self, number, what):
        self.canvas.yview_scroll(number, what)

    def xview(self, *args):
        return self.canvas.xview(*args)

    def xview_moveto(self, fraction):
        self.canvas.xview_moveto(fraction)

    def _on_yscroll(self, first, last):
        self._yscroll.set(first, last)
        self._queue_draw()

    def _on_xscroll(self, first, last):
        self._xscroll.set(first, last)
        self._queue_draw()

    def _queue_draw(self, _event=None):
        if self._draw_pending is None:
            self._draw_pending = self.after_idle(self._draw)

    def _destroyed(self, event):
        if event.widget is self and self._draw_pending is not None:
            self.after_cancel(self._draw_pending)
            self._draw_pending = None

    def _layout(self):
        available = max(1, self.canvas.winfo_width())
        options = [self._column_options[column] for column in self.columns]
        preferred = [max(float(option["minwidth"]), float(option["width"])) for option in options]
        minimum = [float(option["minwidth"]) for option in options]
        if sum(preferred) > available:
            shortage = sum(preferred) - available
            room = sum(width - low for width, low in zip(preferred, minimum))
            fraction = min(1, shortage / room) if room else 1
            widths = [width - (width - low) * fraction for width, low in zip(preferred, minimum)]
        else:
            flex_count = sum(bool(option["stretch"]) for option in options)
            extra = (available - sum(preferred)) / max(1, flex_count)
            widths = [width + (extra if option["stretch"] else 0) for width, option in zip(preferred, options)]
        self._actual_widths = widths
        self._content_width = max(available, round(sum(widths)))
        needs_horizontal = sum(minimum) > available
        if needs_horizontal != self._xscroll_visible:
            self._xscroll_visible = needs_horizontal
            if needs_horizontal:
                self._xscroll.grid()
            else:
                self._xscroll.grid_remove()
                self.canvas.xview_moveto(0)
        region = (0, 0, self._content_width, max(1, len(self._order) * self.row_height))
        if region != self._region:
            self._region = region
            self.canvas.configure(scrollregion=region)

    @staticmethod
    def _ellipsize(text, width, font):
        if width <= 0:
            return ""
        lines = str(text).splitlines() or [""]
        result = []
        for line in lines[:2]:
            if font.measure(line) <= width:
                result.append(line)
                continue
            left, right = 0, len(line)
            while left < right:
                middle = (left + right + 1) // 2
                if font.measure(line[:middle] + "…") <= width:
                    left = middle
                else:
                    right = middle - 1
            result.append(line[:left] + "…")
        if len(lines) > 2:
            result[-1] = DataTable._ellipsize(result[-1] + "…", width, font)
        return "\n".join(result)

    def _badge_style(self, tags):
        for tag in tags:
            if tag in _BADGES:
                background, color_key = _BADGES[tag]
                foreground = self._tag_options.get(tag, {}).get("foreground", _COLORS[color_key])
                return background, foreground
        return "#F1F5F9", _COLORS["muted"]

    def _draw(self):
        self._draw_pending = None
        self._layout()
        self.canvas.delete("all")
        self.header.delete("all")
        xoffset = self.canvas.canvasx(0)
        x = -xoffset
        for column, width in zip(self.columns, self._actual_widths):
            self.header.create_text(x + self._pad, self.header_height / 2,
                                    text=self._ellipsize(self._headings[column]["text"], width - self._pad * 2, self._heading_font),
                                    font=self._heading_font, fill=_COLORS["muted"], anchor="w")
            x += width
        self.header.create_line(0, self.header_height - 1, self.header.winfo_width(),
                                self.header_height - 1, fill=_COLORS["line"])
        if not self._order:
            self.canvas.create_text(max(120, self.canvas.winfo_width() / 2),
                                    self.row_height, text="暂无记录", font=self._font, fill=_COLORS["muted"])
            return
        top = max(0, self.canvas.canvasy(0))
        start = max(0, int(top // self.row_height))
        stop = min(len(self._order), math.ceil((top + self.canvas.winfo_height()) / self.row_height) + 1)
        for index in range(start, stop):
            iid = self._order[index]
            row = self._rows[iid]
            y = index * self.row_height
            selected = iid in self._selected
            background = _COLORS["selected"] if selected else (
                _COLORS["hover"] if iid == self._hover else _COLORS["paper"])
            self.canvas.create_rectangle(0, y, self._content_width, y + self.row_height,
                                          fill=background, outline="")
            if selected:
                self.canvas.create_rectangle(0, y + 1, 3 * self._scale, y + self.row_height - 1,
                                              fill=_COLORS["blue"], outline="")
            badge_background, badge_foreground = self._badge_style(row["tags"])
            x = 0
            for column_index, (column, width) in enumerate(zip(self.columns, self._actual_widths)):
                value = row["values"][column_index] if column_index < len(row["values"]) else ""
                text = "" if value is None else str(value)
                font = self._bold_font if column_index == 0 else self._font
                foreground = _COLORS["ink"]
                if column_index in self._status_columns and text:
                    text = self._ellipsize(text, width - self._pad * 2 - 16 * self._scale, self._font)
                    badge_width = min(width - self._pad * 2, self._font.measure(text) + 16 * self._scale)
                    badge_height = self._font.metrics("linespace") + 9 * self._scale
                    self._rounded_badge(x + self._pad, y + (self.row_height - badge_height) / 2,
                                        badge_width, badge_height, badge_background)
                    self.canvas.create_text(x + self._pad + badge_width / 2, y + self.row_height / 2,
                                            text=text, font=self._font, fill=badge_foreground, anchor="center")
                else:
                    if column.lower() in {"remaining", "places"} and "available" in row["tags"] and text.isdecimal():
                        foreground = self._tag_options.get("available", {}).get("foreground", _COLORS["green"])
                    anchor = self._column_options[column].get("anchor", "w")
                    text_x = x + width / 2 if anchor in {"center", "c"} else (
                        x + width - self._pad if anchor in {"e", "right"} else x + self._pad)
                    self.canvas.create_text(text_x, y + self.row_height / 2,
                                            text=self._ellipsize(text, width - self._pad * 2, font),
                                            font=font, fill=foreground, anchor=anchor, justify="left")
                x += width
            self.canvas.create_line(0, y + self.row_height - 1, self._content_width,
                                    y + self.row_height - 1, fill=_COLORS["line"])
            if iid == self._focused and self.canvas.focus_get() == self.canvas:
                self.canvas.create_rectangle(1, y + 1, self._content_width - 1, y + self.row_height - 2,
                                              outline=_COLORS["blue"], dash=(2, 3))

    def _rounded_badge(self, x, y, width, height, color):
        radius = min(6 * self._scale, height / 2, width / 2)
        self.canvas.create_polygon(x + radius, y, x + width - radius, y,
                                   x + width, y, x + width, y + radius,
                                   x + width, y + height - radius, x + width, y + height,
                                   x + width - radius, y + height, x + radius, y + height,
                                   x, y + height, x, y + height - radius, x, y + radius,
                                   x, y, smooth=True, splinesteps=12, fill=color, outline="")

    def _row_at(self, y):
        index = int(self.canvas.canvasy(y) // self.row_height)
        return self._order[index] if 0 <= index < len(self._order) else ""

    def _click(self, event):
        self.canvas.focus_set()
        iid = self._row_at(event.y)
        if iid:
            self.selection_set(iid)
        return "break"

    def _motion(self, event):
        hovered = self._row_at(event.y)
        if hovered != self._hover:
            self._hover = hovered
            self._queue_draw()

    def _leave(self, _event):
        if self._hover:
            self._hover = ""
            self._queue_draw()

    def _header_click(self, event):
        x = event.x + self.canvas.canvasx(0)
        for column, width in zip(self.columns, self._actual_widths):
            if x < width:
                command = self._headings[column].get("command")
                if callable(command):
                    command()
                break
            x -= width

    def _key(self, event):
        if not self._order:
            return "break"
        current = self._focused if self._focused in self._rows else (
            self._selected[0] if self._selected else "")
        index = self._order.index(current) if current else -1
        page = max(1, self.canvas.winfo_height() // self.row_height)
        if event.keysym == "Home":
            index = 0
        elif event.keysym == "End":
            index = len(self._order) - 1
        elif event.keysym in {"Next", "Prior"}:
            index = max(0, index) + (page if event.keysym == "Next" else -page)
        else:
            index += 1 if event.keysym == "Down" else -1
        iid = self._order[max(0, min(index, len(self._order) - 1))]
        self.selection_set(iid)
        self.see(iid)
        return "break"

    def _scroll_rows(self, units):
        self.canvas.yview_scroll(units, "units")
        return "break"

    def _wheel(self, event):
        units = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        return self._scroll_rows(units * 2)

    def _wheel_horizontal(self, event):
        units = -int(event.delta / 120) if abs(event.delta) >= 120 else (-1 if event.delta > 0 else 1)
        self.canvas.xview_scroll(units * 2, "units")
        return "break"
