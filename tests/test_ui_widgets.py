# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Isolated, real-Tk checks for the reusable desktop table."""
import tkinter as tk
import unittest

try:
    from qq_activity.ui_widgets import DataTable
except ImportError:
    DataTable = None


class DataTableTests(unittest.TestCase):
    def setUp(self):
        self.assertIsNotNone(DataTable, "The canvas-backed DataTable is not implemented")
        self.root = tk.Tk()
        self.root.geometry("850x400+80+80")
        self.addCleanup(self.root.destroy)
        self.table = DataTable(self.root, ("title", "date", "remaining", "state"),
                               ("活动名称", "活动时间", "剩余名额", "状态"),
                               (300, 180, 100, 120), height=4)
        self.table.pack(fill="both", expand=True)
        self.root.update()

    def add_rows(self, count=3):
        for index in range(count):
            self.table.insert("", "end", iid=str(index),
                              values=(f"活动 {index}", "10-28\n14:30", "8", "报名中"),
                              tags=("available",))

    def test_insert_order_and_item_updates_preserve_multiline_data(self):
        self.add_rows()
        self.table.insert("", 1, iid="middle", values=("插入活动", "10-29\n10:00", "0", "已满"))
        self.assertEqual(self.table.get_children(), ("0", "middle", "1", "2"))
        self.assertEqual(self.table.item("0", "values"), ("活动 0", "10-28\n14:30", "8", "报名中"))
        self.table.item("middle", values=("新名称", "10-30\n18:00", "2", "未开始"), tags=("upcoming",))
        self.assertEqual(self.table.item("middle")["tags"], ("upcoming",))
        self.assertEqual(self.table.item("middle", "values")[1], "10-30\n18:00")

    def test_selection_changes_dispatch_once_and_delete_clears_selection(self):
        self.add_rows()
        changes = []
        self.table.bind("<<TreeviewSelect>>", lambda _event: changes.append(self.table.selection()))
        self.table.selection_set("1")
        self.root.update()
        self.table.selection_set(("1",))
        self.root.update()
        self.assertEqual(changes, [("1",)])
        self.table.delete("0", "1")
        self.root.update()
        self.assertEqual(self.table.selection(), ())
        self.assertEqual(self.table.get_children(), ("2",))
        self.assertFalse(self.table.exists("1"))
        self.assertEqual(changes, [("1",), ()])

    def test_selection_remove_leaves_unrelated_selection_alone(self):
        self.add_rows()
        self.table.selection_set("1")
        self.table.selection_remove("0")
        self.assertEqual(self.table.selection(), ("1",))
        self.table.selection_remove(("1",))
        self.assertEqual(self.table.selection(), ())

    def test_keyboard_moves_selection_and_return_keeps_callback(self):
        self.add_rows(20)
        opened = []
        self.table.bind("<Return>", lambda _event: opened.append(self.table.selection()))
        self.table.focus_set()
        self.table.canvas.focus_force()
        self.root.update()
        for key, wanted in (("Down", "0"), ("Down", "1"), ("End", "19"),
                            ("Up", "18"), ("Home", "0")):
            self.table.canvas.event_generate(f"<KeyPress-{key}>")
            self.root.update()
            self.assertEqual(self.table.selection(), (wanted,))
        self.table.canvas.event_generate("<KeyPress-Next>")
        self.root.update()
        self.assertGreater(int(self.table.selection()[0]), 0)
        self.table.canvas.event_generate("<KeyPress-Prior>")
        self.table.canvas.event_generate("<KeyPress-Return>")
        self.root.update()
        self.assertEqual(opened, [("0",)])

    def test_click_selects_visible_row_and_wheel_scrolls(self):
        self.add_rows(30)
        self.root.update()
        self.table.canvas.event_generate("<Button-1>", x=30, y=10)
        self.root.update()
        self.assertEqual(self.table.selection(), ("0",))
        self.table.canvas.event_generate("<MouseWheel>", delta=-120)
        self.root.update()
        self.assertGreater(self.table.yview()[0], 0)

    def test_large_list_only_draws_visible_rows_and_see_reveals_last(self):
        self.add_rows(500)
        self.root.update()
        self.assertLess(len(self.table.canvas.find_all()), 200)
        drawn_text = [self.table.canvas.itemcget(item, "text")
                      for item in self.table.canvas.find_all() if self.table.canvas.type(item) == "text"]
        self.assertIn("10-28\n14:30", drawn_text)
        self.assertNotIn("活动 499", drawn_text)
        self.table.see("499")
        self.root.update()
        self.assertGreater(self.table.yview()[0], 0.9)
        drawn_text = [self.table.canvas.itemcget(item, "text")
                      for item in self.table.canvas.find_all() if self.table.canvas.type(item) == "text"]
        self.assertIn("活动 499", drawn_text)
        self.assertLess(len(self.table.canvas.find_all()), 200)

    def test_status_badges_keep_ordinary_text_dark(self):
        self.add_rows(1)
        self.table.tag_configure("available", foreground="#15803D")
        self.root.update()
        colors = {self.table.canvas.itemcget(item, "text"): self.table.canvas.itemcget(item, "fill")
                  for item in self.table.canvas.find_all() if self.table.canvas.type(item) == "text"}
        self.assertEqual(colors["活动 0"].lower(), "#1e293b")
        self.assertEqual(colors["8"].lower(), "#15803d")
        self.assertEqual(colors["报名中"].lower(), "#15803d")

    def test_column_heading_and_narrow_resize_keep_data_accessible(self):
        self.add_rows()
        self.table.heading("title", text="名称")
        self.table.column("title", width=400, minwidth=350)
        self.table.column("date", minwidth=250)
        self.root.geometry("420x400")
        self.root.update()
        self.assertEqual(self.table.heading("title", "text"), "名称")
        self.assertEqual(self.table.column("title", "minwidth"), 350)
        self.assertLess(self.table.xview()[1], 1)
        self.table.xview_moveto(1)
        self.root.update()
        self.assertGreater(self.table.xview()[0], 0)


if __name__ == "__main__":
    unittest.main()
