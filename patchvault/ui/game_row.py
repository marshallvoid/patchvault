import tkinter as tk

from ..config import MISSING_VERSION
from ..names import clean_game_name, display_version, format_size
from .theme import COLORS, LIST_COLUMNS, ROW_COLORS


def status_text(item):
    job = item["job"]
    if job:
        if job["phase"] == "error":
            return "Lỗi"
        if job["phase"] == "downloading":
            speed = f" · {format_size(job['speed'])}/s" if job["speed"] else ""
            if job["total"]:
                return f"{100 * job['done'] / job['total']:.0f}%{speed}"
            return f"{format_size(job['done'])}{speed}"  # size unknown: show bytes pulled so far
        return {"queued": "Đang chờ", "verifying": "Đang kiểm tra", "repacking": "Đang đóng gói"}[job["phase"]]
    if item["status"] == "update":
        return f"Cập nhật: {item['local_version'] or '?'} → {item['version'] or 'mới'}"
    return "Đã tải" if item["status"] == "ok" else "Chưa tải"


def row_tag(item):
    job = item["job"]
    if job:
        return "error" if job["phase"] == "error" else "active"
    return item["status"]


# A Treeview cannot draw tag chips or rows of different heights.
class GameRow:
    def __init__(self, app, item):
        self.app = app
        self.id = item["id"]
        self.values = None
        self.selected = None
        self.frame = tk.Frame(app.list_frame, bg=COLORS["field"])
        self.cells = {}
        for index, (name, _, width) in enumerate(LIST_COLUMNS):
            self.frame.columnconfigure(index, minsize=width)
            if name == "tags":
                self.tag_cell = tk.Frame(self.frame, bg=COLORS["field"], padx=8, pady=6)
                self.tag_cell.grid(row=0, column=index, sticky="w")
                self.add_chips(item["tags"], width - 16)
            else:
                label = tk.Label(
                    self.frame,
                    anchor="w",
                    justify="left",
                    padx=8,
                    pady=8,
                    bd=0,
                    font=app.font,
                    bg=COLORS["field"],
                    fg=COLORS["ink"],
                )
                label.grid(row=0, column=index, sticky="nsew")
                self.cells[name] = label
        self.bind(self.frame)

    def add_chips(self, tags, limit):
        line, used = None, 0
        for tag in tags:
            width = self.app.chip_font.measure(tag) + 16
            if line is None or used + width > limit:
                line, used = tk.Frame(self.tag_cell, bg=COLORS["field"]), 0
                line.pack(anchor="w", pady=1)
            tk.Label(
                line, text=tag, font=self.app.chip_font, bg=COLORS["panel"], fg=COLORS["ink"], padx=6, pady=1, bd=0
            ).pack(side="left", padx=(0, 4))
            used += width

    def bind(self, widget):
        widget.bind("<Button-1>", lambda _: self.app.select(self.id))
        widget.bind("<Control-Button-1>", lambda _: self.app.toggle_pick(self.id))
        widget.bind("<Shift-Button-1>", lambda _: self.app.pick_range(self.id))
        widget.bind("<Double-1>", lambda _: self.app.act())
        widget.bind("<Button-4>", lambda _: self.app.scroll_list(-1))
        widget.bind("<Button-5>", lambda _: self.app.scroll_list(1))
        widget.bind("<MouseWheel>", lambda event: self.app.scroll_list(-1 if event.delta > 0 else 1))
        for child in widget.winfo_children():
            self.bind(child)

    def fit(self, game_width):
        self.frame.columnconfigure(0, minsize=game_width)
        self.cells["game"].configure(wraplength=game_width - 16)

    def paint(self, item, selected):
        version = display_version(item["game_version"])
        missing = version == MISSING_VERSION
        values = (
            clean_game_name(item["name"]),
            item["version"] or "-",
            "Miss" if missing else version,
            format_size(item["size"]),
            status_text(item),
            row_tag(item),
        )
        if values != self.values:
            self.values = values
            color = COLORS[ROW_COLORS[values[-1]]]
            for name, text in zip(("game", "version", "game_version", "size", "status"), values):
                self.cells[name].configure(text=text, fg=color)
            if missing:
                self.cells["game_version"].configure(fg=COLORS["miss"])
        if selected != self.selected:
            self.selected = selected
            background = COLORS["select"] if selected else COLORS["field"]
            self.frame.configure(bg=background)
            self.tag_cell.configure(bg=background)
            for line in self.tag_cell.winfo_children():
                line.configure(bg=background)  # type: ignore[call-arg]
            for label in self.cells.values():
                label.configure(bg=background)
