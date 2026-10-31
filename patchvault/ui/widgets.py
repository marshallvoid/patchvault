import tkinter as tk
from tkinter import ttk

from .theme import COLORS


class LogPane:
    FONT_SIZE = 12

    def __init__(self, app, parent):
        self.frame = ttk.Frame(parent)
        self.lines = 0
        self.text = tk.Text(
            self.frame,
            wrap="word",
            bd=0,
            highlightthickness=0,
            padx=8,
            pady=6,
            spacing1=2,
            font=(app.family, self.FONT_SIZE),
            bg=COLORS["field"],
            fg=COLORS["ink"],
            state="disabled",
            selectbackground=COLORS["select"],
            inactiveselectbackground=COLORS["select"],
        )
        self.text.tag_configure("info", foreground=COLORS["muted"])
        self.text.tag_configure("ok", foreground=COLORS["ok"])
        self.text.tag_configure("warn", foreground=COLORS["accent"])
        self.text.tag_configure("error", foreground=COLORS["bad"], font=(app.family, self.FONT_SIZE, "bold"))
        scroll = ttk.Scrollbar(self.frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True)

    def paint(self, lines):
        if len(lines) == self.lines:
            return
        self.text.configure(state="normal")
        if len(lines) < self.lines:
            self.text.delete("1.0", "end")
            self.lines = 0
        following = self.text.yview()[1] >= 1
        for level, line in lines[self.lines :]:
            self.text.insert("end", line + "\n", level)
        self.lines = len(lines)
        self.text.configure(state="disabled")
        if following:
            self.text.see("end")


class ViewSwitch:
    def __init__(self, toolbar, swap):
        self.swap = swap
        self.log_tab = ttk.Button(toolbar, text="Log", takefocus=False, command=lambda: self.show("log"))
        self.log_tab.pack(side="right")
        self.list_tab = ttk.Button(toolbar, text="Danh sách", takefocus=False, command=lambda: self.show("list"))
        self.list_tab.pack(side="right", padx=(12, 6))

    def show(self, view):
        self.swap(view)
        self.log_tab.configure(style="ViewActive.Accent.TButton" if view == "log" else "View.TButton")
        self.list_tab.configure(style="View.TButton" if view == "log" else "ViewActive.Accent.TButton")
