import threading
import tkinter as tk
from tkinter import filedialog, ttk
from tkinter import font as tkfont

from ..config import BACKUP_DIR, INTERNAL_VERSION, MISSING_VERSION
from ..names import clean_game_name, display_version, format_size
from .dialogs import ask_yes_no, show_message
from .theme import COLORS
from .widgets import LogPane, ViewSwitch


# A Treeview cell cannot wrap long text or hold a real progress bar.
class ModRow:
    COLORS = {"ready": "ink", "working": "accent", "done": "ok", "conflict": "muted", "error": "bad"}

    def __init__(self, tab):
        self.tab = tab
        self.painted = None
        self.frame = tk.Frame(tab.rows_frame, bg=COLORS["field"])
        self.labels = {}
        for index, (name, _, _) in enumerate(tab.COLUMNS):
            if name == "progress":
                self.progress_cell = tk.Frame(self.frame, bg=COLORS["field"], padx=8, pady=9)
                self.progress_cell.grid(row=0, column=index, sticky="new")
                self.bar = ttk.Progressbar(self.progress_cell, maximum=1000)
                self.percent = tk.Label(
                    self.progress_cell,
                    width=5,
                    anchor="e",
                    bd=0,
                    font=tab.app.font,
                    bg=COLORS["field"],
                    fg=COLORS["ink"],
                )
                self.percent.pack(side="right")
                self.bar.pack(side="left", fill="x", expand=True)
            else:
                label = tk.Label(
                    self.frame,
                    anchor="nw",
                    justify="left",
                    padx=8,
                    pady=8,
                    bd=0,
                    font=tab.app.font,
                    bg=COLORS["field"],
                    fg=COLORS["ink"],
                )
                label.grid(row=0, column=index, sticky="nsew")
                self.labels[name] = label
        for widget in (self.frame, self.progress_cell, self.bar, self.percent, *self.labels.values()):
            tab.bind_scroll(widget)

    def fit(self, widths):
        for index, ((name, _, _), width) in enumerate(zip(self.tab.COLUMNS, widths)):
            self.frame.columnconfigure(index, minsize=width)
            if name in self.labels and name not in self.tab.FITTED:
                self.labels[name].configure(wraplength=width - 16)

    def paint(self, cells, status, fraction):
        key = (cells, status, fraction)
        if key == self.painted:
            return
        self.painted = key
        color = COLORS[self.COLORS[status]]
        for name, label in self.labels.items():
            label.configure(text=cells[name], fg=color)
        if fraction is None:
            self.bar.pack_forget()
            self.percent.configure(text="-", fg=color)
        else:
            if not self.bar.winfo_manager():
                self.bar.pack(side="left", fill="x", expand=True)
            self.bar["value"] = 1000 * fraction
            self.percent.configure(text=f"{100 * fraction:.0f}%", fg=color)


class NormalizeTab:
    # Width in pixels, or a share of what the fixed columns leave. Columns in FITTED grow to
    # their content and never wrap; when they need more room, the table scrolls sideways.
    COLUMNS = (
        ("source", "Nguồn", 0.4),
        ("title", "Title ID", 200),
        ("name", "Tên game", 0.25),
        ("mod", "Tên mod", 140),
        ("version", "Phiên bản game", 125),
        ("size", "Dung lượng", 100),
        ("progress", "Tiến độ", 200),
        ("output", "Tên sau chuẩn hóa", 300),
        ("status", "Trạng thái", 0.35),
    )
    FITTED = ("version", "output")
    MIN_SHARED = 760
    CELL_PADDING = 18
    STATUS = {
        "ready": "Sẵn sàng",
        "working": "Đang đóng gói",
        "done": "Đã chuẩn hóa",
        "conflict": "Bỏ qua",
        "error": "Lỗi",
    }

    def __init__(self, app, parent):
        self.app = app
        self.vault = app.vault
        self.painted = None
        self.row_keys = []
        self.reported_runs = 0
        parent.columnconfigure(1, weight=1)
        parent.rowconfigure(4, weight=1)
        self.source = tk.StringVar()
        self.dest = tk.StringVar(value=self.vault.normalize_dest)
        self.dest.trace_add("write", lambda *_: self.vault.set_normalize_dest(self.dest.get()))
        for row, (label, variable) in enumerate((("Thư mục nguồn", self.source), ("Thư mục đích", self.dest))):
            ttk.Label(parent, text=label, style="Muted.TLabel").grid(
                row=row, column=0, sticky="w", padx=(0, 10), pady=3
            )
            ttk.Entry(parent, textvariable=variable).grid(row=row, column=1, sticky="ew", pady=3)
            ttk.Button(parent, text="Chọn…", command=lambda v=variable: self.browse(v)).grid(
                row=row, column=2, padx=(6, 0), pady=3
            )
        self.dest_entry = parent.grid_slaves(row=1, column=1)[0]
        actions = ttk.Frame(parent)
        actions.grid(row=2, column=0, columnspan=3, sticky="ew", pady=(8, 8))
        self.scan_button = ttk.Button(actions, text="Quét", command=self.scan)
        self.scan_button.pack(side="left")
        self.run_button = ttk.Button(actions, text="Chuẩn hóa", style="Accent.TButton", command=self.run)
        self.run_button.pack(side="left", padx=(6, 0))
        self.cancel_button = ttk.Button(actions, text="Huỷ", command=self.vault.normalize_cancel.set)
        self.status = ttk.Label(actions, style="Muted.TLabel")
        self.status.pack(side="left", padx=(12, 0))
        self.views = ViewSwitch(actions, self.show_view)
        self.progress = ttk.Progressbar(parent, maximum=1000)
        self.table = tk.Frame(parent, bg=COLORS["panel"])
        self.header_canvas = tk.Canvas(self.table, bg=COLORS["panel"], bd=0, highlightthickness=0, height=1)
        self.header_canvas.pack(fill="x")
        self.header = tk.Frame(self.header_canvas, bg=COLORS["panel"])
        self.header_window = self.header_canvas.create_window(0, 0, anchor="nw", window=self.header)
        self.header.bind(
            "<Configure>",
            lambda event: self.header_canvas.configure(
                height=event.height, scrollregion=self.header_canvas.bbox("all")
            ),
        )
        for index, (_, title, _) in enumerate(self.COLUMNS):
            tk.Label(
                self.header,
                text=title,
                anchor="w",
                padx=8,
                pady=7,
                bd=0,
                font=app.font,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
            ).grid(row=0, column=index, sticky="ew")
        self.holder = tk.Frame(self.table, bg=COLORS["field"])
        self.holder.pack(fill="both", expand=True)
        holder = self.holder
        self.canvas = tk.Canvas(holder, bg=COLORS["field"], bd=0, highlightthickness=0)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.canvas.yview)
        # The header stops where the rows do, or the two would scroll by different amounts.
        self.header_canvas.pack_configure(padx=(0, scroll.winfo_reqwidth()))
        self.side_scroll = ttk.Scrollbar(self.table, orient="horizontal", command=self.scroll_sideways)
        self.canvas.configure(yscrollcommand=scroll.set, xscrollcommand=self.follow_sideways)
        scroll.pack(side="right", fill="y")
        self.canvas.pack(side="left", fill="both", expand=True)
        self.rows_frame = tk.Frame(self.canvas, bg=COLORS["line"])
        self.rows_window = self.canvas.create_window(0, 0, anchor="nw", window=self.rows_frame)
        self.rows_frame.bind("<Configure>", lambda _: self.canvas.configure(scrollregion=self.canvas.bbox("all")))
        self.canvas.bind("<Configure>", self.fit)
        self.bind_scroll(self.canvas)
        self.rows = []
        self.widths = [width if width > 1 else 200 for _, _, width in self.COLUMNS]
        self.working = None
        self.measure = tkfont.Font(root=app.root, font=app.font)
        self.canvas_width = 0
        self.fitted = {
            name: self.measure.measure(title) + self.CELL_PADDING
            for name, title, _ in self.COLUMNS
            if name in self.FITTED
        }
        self.log = LogPane(app, parent)
        self.views.show("list")

    def bind_scroll(self, widget):
        widget.bind("<Button-4>", lambda _: self.scroll(-1))
        widget.bind("<Button-5>", lambda _: self.scroll(1))
        widget.bind("<MouseWheel>", lambda event: self.scroll(-1 if event.delta > 0 else 1))
        widget.bind("<Shift-Button-4>", lambda _: self.scroll_sideways("scroll", -3, "units"))
        widget.bind("<Shift-Button-5>", lambda _: self.scroll_sideways("scroll", 3, "units"))
        widget.bind(
            "<Shift-MouseWheel>", lambda event: self.scroll_sideways("scroll", -3 if event.delta > 0 else 3, "units")
        )

    def scroll_sideways(self, *args):
        self.canvas.xview(*args)

    def follow_sideways(self, first, last):
        self.side_scroll.set(first, last)
        self.header_canvas.xview_moveto(first)
        if float(last) - float(first) >= 1:
            self.side_scroll.pack_forget()
        elif not self.side_scroll.winfo_manager():
            self.side_scroll.pack(side="bottom", fill="x", before=self.holder)

    def scroll(self, units):
        if self.rows_frame.winfo_height() > self.canvas.winfo_height():
            self.canvas.yview_scroll(units, "units")

    def fit(self, event):
        self.canvas_width = event.width
        self.apply_widths()

    def apply_widths(self):
        widths = [self.fitted.get(name, width) for name, _, width in self.COLUMNS]
        spare = max(self.MIN_SHARED, self.canvas_width - sum(width for width in widths if width > 1))
        self.widths = [width if width > 1 else int(spare * width) for width in widths]
        total = max(self.canvas_width, sum(self.widths))
        self.canvas.itemconfigure(self.rows_window, width=total)
        self.header_canvas.itemconfigure(self.header_window, width=total)
        for index, width in enumerate(self.widths):
            self.header.columnconfigure(index, minsize=width)
        for row in self.rows:
            row.fit(self.widths)

    def show_view(self, view):
        shown, hidden = (self.log.frame, self.table) if view == "log" else (self.table, self.log.frame)
        hidden.grid_remove()
        shown.grid(row=4, column=0, columnspan=3, sticky="nsew")

    def browse(self, variable):
        folder = filedialog.askdirectory(parent=self.app.root, initialdir=variable.get() or None)
        if folder:
            variable.set(folder)

    def scan(self):
        if not self.vault.normalizing():
            threading.Thread(
                target=self.vault.scan_mods, args=(self.source.get(), self.dest.get()), daemon=True
            ).start()

    def run(self):
        count = sum(plan["status"] == "ready" for plan in self.vault.normalize_snapshot()["plans"])
        if (
            count
            and not self.vault.normalizing()
            and ask_yes_no(
                "Chuẩn hóa mod",
                f"Chuẩn hóa {count} mod? Sau khi lưu xong, file gốc và cả thư mục ngoài cùng chứa nó "
                f"trong Thư mục nguồn sẽ được chuyển vào thư mục {BACKUP_DIR} trong Thư mục đích.",
                parent=self.app.root,
            )
        ):
            threading.Thread(target=self.vault.normalize_mods, daemon=True).start()

    def cells(self, plan, root):
        source = plan["source"]
        shown = source.relative_to(root).as_posix() if root else source.name
        status = self.STATUS[plan["status"]]
        if plan["message"]:
            status += ": " + plan["message"]
        title = plan["title_id"] or "-"
        if plan["extra_titles"] > 0:
            title += f" +{plan['extra_titles']}"
        version = display_version(plan["version"])
        return {
            "source": shown,
            "title": title,
            "name": clean_game_name(plan["name"]),
            "mod": plan["mod"],
            "version": "Miss"
            if version == MISSING_VERSION
            else (version if plan["internal"] == INTERNAL_VERSION else f"{version}-{plan['internal']}"),
            "status": status,
            "size": format_size(plan["stored"]) if plan["stored"] else "-",
            "output": "-" if plan["status"] == "error" else f"{plan['target'].parent.name}/{plan['target'].name}",
        }

    @staticmethod
    def fraction(plan):
        if plan["status"] not in ("ready", "working", "done"):
            return None
        return 1 if plan["status"] == "done" else min(1, plan["written"] / max(1, plan["size"]))

    def paint_rows(self, plans, root):
        keys = [(plan["source"], plan["title_id"]) for plan in plans]
        if keys != self.row_keys:
            self.row_keys = keys
            for row in self.rows:
                row.frame.destroy()
            self.rows = [ModRow(self) for _ in plans]
            for row in self.rows:
                row.fit(self.widths)
                row.frame.pack(fill="x", pady=(0, 1))
            self.canvas.yview_moveto(0)
        cells = [self.cells(plan, root) for plan in plans]
        wanted = {
            name: max([self.measure.measure(title), *(self.measure.measure(cell[name]) for cell in cells)])
            + self.CELL_PADDING
            for name, title, _ in self.COLUMNS
            if name in self.FITTED
        }
        if wanted != self.fitted:
            self.fitted = wanted
            self.apply_widths()
        for row, plan, cell in zip(self.rows, plans, cells):
            row.paint(cell, plan["status"], self.fraction(plan))
        working = next((index for index, plan in enumerate(plans) if plan["status"] == "working"), None)
        if working != self.working:
            self.working = working
            if working is not None:
                self.see(self.rows[working].frame)

    def see(self, frame):
        self.app.root.update_idletasks()
        total = self.rows_frame.winfo_height()
        visible = self.canvas.winfo_height()
        top = self.canvas.canvasy(0)
        if frame.winfo_y() < top:
            self.canvas.yview_moveto(frame.winfo_y() / total)
        elif frame.winfo_y() + frame.winfo_height() > top + visible:
            self.canvas.yview_moveto((frame.winfo_y() + frame.winfo_height() - visible) / total)

    def paint(self, job):
        plans = job["plans"]
        key = (
            job["phase"],
            job["runs"],
            job["done"],
            job["message"],
            job["bytes_done"],
            len(job["log"]),
            *((plan["status"], plan["message"], plan["written"]) for plan in plans),
        )
        if key == self.painted:
            return
        self.painted = key
        busy = job["phase"] in ("scanning", "working")
        ready = sum(plan["status"] == "ready" for plan in plans)
        self.scan_button.state(["disabled"] if busy else ["!disabled"])
        self.dest_entry.state(["disabled"] if busy else ["!disabled"])
        self.run_button.state(["!disabled"] if ready and not busy else ["disabled"])
        self.run_button.configure(text=f"Chuẩn hóa ({ready})" if ready else "Chuẩn hóa")
        if job["phase"] == "working":
            fraction = min(1, job["bytes_done"] / max(1, job["bytes_total"]))
            self.cancel_button.pack(side="left", padx=(6, 0), after=self.run_button)
            self.progress.configure(value=1000 * fraction)
            self.progress.grid(row=3, column=0, columnspan=3, sticky="ew", pady=(0, 8))
            text = f"Đang chuẩn hóa {job['done']}/{job['total']} mod · {100 * fraction:.0f}%"
        else:
            self.cancel_button.pack_forget()
            self.progress.grid_remove()
            text = (
                "Đang quét… Lần đầu cần tải danh sách tên game nên sẽ lâu hơn."
                if job["phase"] == "scanning"
                else job["message"]
            )
        self.status.configure(text=text, style="Error.TLabel" if job["failed"] else "Muted.TLabel")
        self.paint_rows(plans, job.get("root"))
        self.log.paint(job["log"])
        if job["runs"] != self.reported_runs:
            self.reported_runs = job["runs"]
            if job["done"] or any(plan["status"] == "error" for plan in plans):
                self.app.root.after_idle(show_message, "Chuẩn hóa xong", job["message"], self.app.root)
