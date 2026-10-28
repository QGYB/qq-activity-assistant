# SPDX-License-Identifier: AGPL-3.0-only
# SPDX-FileCopyrightText: 2026 QGYB and contributors
"""Native desktop typography and flat, rounded ttk controls (no asset files)."""
from __future__ import annotations

import base64
import math
import struct
import tkinter as tk
from tkinter import font as tkfont, ttk
import zlib


PALETTE = {
    "canvas": "#F8FAFC", "paper": "#FFFFFF", "ink": "#1E293B",
    "muted": "#64748B", "line": "#E2E8F0", "teal": "#2563EB",
    "blue": "#2563EB", "accent": "#2563EB", "pale": "#EFF6FF",
    "amber": "#92400E", "red": "#B91C1C", "nav": "#FFFFFF",
    "green": "#166534", "green_pale": "#F0FDF4", "red_pale": "#FEF2F2",
}


def _rounded_image(root, fill, outline, scale, focused=False):
    """Make an antialiased, transparent nine-slice button using Tk PhotoImage."""
    width, height = round(44 * scale), round(36 * scale)
    rgb = lambda color: tuple(int(color[i:i + 2], 16) for i in (1, 3, 5))
    fill_rgb, edge_rgb, focus_rgb = rgb(fill), rgb(outline), rgb(PALETTE["blue"])

    def inside(x, y, inset, radius):
        dx = max(inset + radius - x, x - (width - inset - radius), 0)
        dy = max(inset + radius - y, y - (height - inset - radius), 0)
        return math.hypot(dx, dy) <= radius

    rows = bytearray()
    for y in range(height):
        rows.append(0)
        for x in range(width):
            samples = []
            for ox, oy in ((.25, .25), (.75, .25), (.25, .75), (.75, .75)):
                px, py = x + ox, y + oy
                if inside(px, py, 4 * scale, 6 * scale):
                    samples.append(fill_rgb)
                elif inside(px, py, 3 * scale, 7 * scale):
                    samples.append(edge_rgb)
                elif focused and inside(px, py, .5 * scale, 9.5 * scale) and not inside(px, py, 2 * scale, 8 * scale):
                    samples.append(focus_rgb)
            if samples:
                rows.extend(round(sum(c[i] for c in samples) / len(samples)) for i in range(3))
                rows.append(round(255 * len(samples) / 4))
            else:
                rows.extend((0, 0, 0, 0))

    def chunk(kind, data):
        return struct.pack("!I", len(data)) + kind + data + struct.pack("!I", zlib.crc32(kind + data) & 0xffffffff)

    png = b"\x89PNG\r\n\x1a\n" + chunk(b"IHDR", struct.pack("!2I5B", width, height, 8, 6, 0, 0, 0))
    png += chunk(b"IDAT", zlib.compress(rows)) + chunk(b"IEND", b"")
    return tk.PhotoImage(master=root, data=base64.b64encode(png), format="png")


def apply_theme(root) -> dict[str, str]:
    """Apply the workspace theme and retain generated images for its lifetime."""
    p = PALETTE
    scale = max(.85, min(2.25, float(root.tk.call("tk", "scaling")) / (96 / 72)))
    root._ui_scale = scale
    px = lambda value: max(1, round(value * scale))
    families = set(tkfont.families(root))
    family = next((name for name in ("Microsoft YaHei UI", "Segoe UI", "Microsoft YaHei") if name in families), "TkDefaultFont")
    root._ui_font_family = family
    for name in ("TkDefaultFont", "TkTextFont", "TkMenuFont", "TkHeadingFont", "TkCaptionFont"):
        tkfont.nametofont(name, root).configure(family=family, size=10)
    root.configure(background=p["canvas"])
    root.option_add("*Listbox.background", p["paper"])
    root.option_add("*Listbox.foreground", p["ink"])
    root.option_add("*Listbox.selectBackground", p["pale"])
    root.option_add("*Listbox.selectForeground", p["ink"])
    style = ttk.Style(root)
    style.theme_use("clam")
    style.configure(".", font=(family, 10), background=p["canvas"], foreground=p["ink"], borderwidth=0)
    for name, color in (("TFrame", p["canvas"]), ("Paper.TFrame", p["paper"]),
                        ("Card.TFrame", p["paper"]), ("Header.TFrame", p["canvas"]), ("Nav.TFrame", p["nav"])):
        style.configure(name, background=color)
    style.configure("TLabel", background=p["canvas"], foreground=p["ink"])
    for name in ("Paper.TLabel", "Card.TLabel"):
        style.configure(name, background=p["paper"])
    style.configure("Muted.TLabel", foreground=p["muted"])
    style.configure("Small.TLabel", font=(family, 9), foreground=p["muted"])
    style.configure("Title.TLabel", font=(family, 18, "bold"))
    style.configure("Heading.TLabel", font=(family, 11, "bold"))
    style.configure("Card.Heading.TLabel", background=p["paper"], font=(family, 11, "bold"))
    style.configure("Nav.TLabel", background=p["nav"], foreground=p["muted"], font=(family, 9))

    # Each element has its own state images; transparent corners blend on any panel.
    images = []
    button_colors = {
        "TButton": ("#FFFFFF", "#F1F5F9", "#E2E8F0", p["line"], p["ink"]),
        "Accent.TButton": (p["blue"], "#1D4ED8", "#1E40AF", p["blue"], "#FFFFFF"),
        "Danger.TButton": ("#FEF2F2", "#FEE2E2", "#FECACA", "#FECACA", p["red"]),
        "Quiet.TButton": (p["canvas"], "#F1F5F9", "#E2E8F0", p["canvas"], p["muted"]),
        "Nav.TButton": ("#FFFFFF", "#F1F5F9", "#E2E8F0", "#FFFFFF", p["ink"]),
        "Active.Nav.TButton": (p["pale"], "#DBEAFE", "#BFDBFE", p["pale"], "#1D4ED8"),
    }
    generation = getattr(root, "_theme_generation", 0) + 1
    root._theme_generation = generation
    for name, (normal, active, pressed, outline, foreground) in button_colors.items():
        variants = [(normal, False), ("#F1F5F9", False), (pressed, True), (active, True),
                    (normal, True), (pressed, False), (active, False)]
        states = [_rounded_image(root, color, outline if i != 1 else p["line"], scale, focus)
                  for i, (color, focus) in enumerate(variants)]
        images.extend(states)
        element = f"Workspace{generation}.{name}.rounded"
        style.element_create(element, "image", states[0], ("disabled", states[1]),
                             ("pressed", "focus", states[2]), ("active", "focus", states[3]),
                             ("focus", states[4]), ("pressed", states[5]), ("active", states[6]),
                             border=px(11), padding=0, sticky="nsew")
        style.layout(name, [(element, {"sticky": "nsew", "children": [
            ("Button.padding", {"sticky": "nsew", "children": [("Button.label", {"sticky": "nsew"})]})]})])
        surface = p["paper"] if name in ("Nav.TButton", "Active.Nav.TButton") else p["canvas"]
        style.configure(name, font=(family, 10), foreground=foreground, background=surface,
                        padding=(px(14), px(7)), borderwidth=0, relief="flat", anchor="center")
        style.map(name, foreground=[("disabled", p["muted"])],
                  background=[("disabled", surface), ("!disabled", surface)])
    for name in ("Nav.TButton", "Active.Nav.TButton"):
        style.configure(name, anchor="w", padding=(px(14), px(10)))
    style.configure("Active.Nav.TButton", font=(family, 10, "bold"))
    root._theme_images = images

    for name in ("TEntry", "TCombobox", "TSpinbox"):
        style.configure(name, padding=px(6), fieldbackground=p["paper"], background=p["paper"],
                        borderwidth=1, relief="flat", bordercolor=p["line"], lightcolor=p["line"],
                        darkcolor=p["line"], arrowcolor=p["muted"], foreground=p["ink"],
                        selectbackground="#DBEAFE", selectforeground=p["ink"], insertcolor=p["ink"])
        style.map(name, bordercolor=[("focus", p["blue"]), ("!focus", p["line"])],
                  lightcolor=[("focus", p["blue"]), ("!focus", p["line"])],
                  darkcolor=[("focus", p["blue"]), ("!focus", p["line"])],
                  fieldbackground=[("disabled", "#F1F5F9"), ("readonly", p["paper"])],
                  foreground=[("disabled", p["muted"])],
                  background=[("active", "#F1F5F9"), ("readonly", p["paper"])])
    notebook_options = dict(borderwidth=0, padding=0, background=p["canvas"],
                            bordercolor=p["canvas"], lightcolor=p["canvas"], darkcolor=p["canvas"], relief="flat")
    style.configure("TNotebook", **notebook_options)
    style.configure("Workspace.TNotebook", **notebook_options, tabmargins=0)
    style.layout("Workspace.TNotebook.Tab", [])
    style.configure("TNotebook.Tab", padding=(px(16), px(8)), background=p["canvas"], foreground=p["muted"])
    style.map("TNotebook.Tab", background=[("selected", p["paper"])], foreground=[("selected", p["blue"])])
    style.configure("TLabelframe", background=p["paper"], bordercolor=p["line"], borderwidth=1, relief="solid")
    style.configure("TLabelframe.Label", background=p["paper"], foreground=p["ink"], font=(family, 10, "bold"))
    style.configure("TCheckbutton", background=p["paper"], padding=(0, px(4)), focuscolor=p["blue"])
    style.map("TCheckbutton", background=[("active", p["paper"])], foreground=[("disabled", p["muted"])])
    style.configure("TRadiobutton", background=p["paper"], padding=(0, px(4)), focuscolor=p["blue"])
    style.map("TRadiobutton", background=[("active", p["paper"])])
    style.configure("TPanedwindow", background=p["canvas"])
    style.configure("TSeparator", background=p["line"])
    for direction in ("Vertical", "Horizontal"):
        name = f"{direction}.TScrollbar"
        style.configure(name, troughcolor=p["canvas"], background="#CBD5E1", bordercolor=p["canvas"],
                        lightcolor="#CBD5E1", darkcolor="#CBD5E1", arrowcolor=p["muted"], borderwidth=0,
                        gripcount=0, arrowsize=px(12), width=px(10))
        style.map(name, background=[("active", "#94A3B8"), ("pressed", p["muted"])])
    style.configure("Treeview", rowheight=px(42), background=p["paper"], fieldbackground=p["paper"],
                    borderwidth=0, font=(family, 10), foreground=p["ink"], lightcolor=p["paper"], darkcolor=p["paper"])
    style.configure("Treeview.Heading", font=(family, 9, "bold"), padding=(px(8), px(10)),
                    background=p["canvas"], foreground=p["muted"], borderwidth=0, relief="flat")
    style.map("Treeview", background=[("selected", "#DBEAFE")], foreground=[("selected", p["ink"])])
    style.map("Treeview.Heading", background=[("active", "#F1F5F9")])
    return p
