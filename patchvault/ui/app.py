import os
import queue
import threading
import tkinter as tk
from datetime import datetime
from tkinter import filedialog, ttk
from tkinter import font as tkfont
from typing import Any

from .. import net
from ..config import ART_KINDS, ART_WORKERS, MAX_IMAGE, app_version
from ..names import art_file_name, clean_game_name, format_size
from ..system import can_self_update, open_in_system, restart_program
from .art import Image, ImageTk, compose_banner
from .catalog_manager import CatalogManager
from .dialogs import ask_yes_no, show_message
from .game_row import GameRow, status_text
from .normalize_tab import NormalizeTab
from .theme import COLORS, LIST_COLUMNS
from .widgets import LogPane, ViewSwitch


class App:
    ART_SIZE = (426, 200)
    KEY_WIDTH = 128

    def __init__(self, vault):
        self.vault = vault
        self.root = tk.Tk(className="PatchVault")
        self.root.title(f"PatchVault {app_version()}")
        self.root.geometry("1640x900")
        self.root.minsize(1320, 620)
        self._maximize()
        self.root.configure(bg=COLORS["bg"])
        self.items = {}
        self.shown_ids: Any = []
        self.rows = {}
        self.row_signature = None
        self.selected = None
        self.picked = set()
        self.pick_anchor = None
        self.chosen = []
        self.game_width = 300
        self.detail_id = None
        self.detail_key = None
        self.art_id = None
        self.art_image = None
        self.art_cache = {}
        self.art_signature = None
        self.genres = []
        self.selected_genres = set()
        self.genre_panel = None
        self.genre_panel_anchor = None
        self.prompted_tag = None
        self.reported_batch = 0
        self.catalog_folders = []
        self.manager = None
        self._style()
        self._build()
        self.root.protocol("WM_DELETE_WINDOW", self.close)
        self.refresh()

    def _maximize(self):
        try:
            self.root.state("zoomed")  # Windows and macOS
        except tk.TclError:
            self.root.attributes("-zoomed", True)  # X11

    def _style(self):
        style = ttk.Style(self.root)
        style.theme_use("clam")
        family = tkfont.Font(root=self.root, font="TkDefaultFont").actual("family")
        if "Noto Sans" in tkfont.families(self.root):
            family = "Noto Sans"
        self.family = family
        self.font = (family, 10)
        self.bold = (family, 10, "bold")
        self.chip_font = tkfont.Font(root=self.root, family=family, size=9)
        c = COLORS
        style.configure(
            ".",
            background=c["bg"],
            foreground=c["ink"],
            font=self.font,
            bordercolor=c["line"],
            lightcolor=c["bg"],
            darkcolor=c["bg"],
            troughcolor=c["field"],
        )
        style.configure("Muted.TLabel", foreground=c["muted"])
        style.configure("Title.TLabel", font=(family, 15, "bold"))
        style.configure("Name.TLabel", font=(family, 13, "bold"))
        style.configure("Error.TLabel", foreground=c["bad"])
        style.configure("TButton", background=c["panel"], foreground=c["ink"], padding=(10, 5), relief="flat")
        style.map(
            "TButton", background=[("disabled", c["bg"]), ("active", c["line"])], foreground=[("disabled", c["muted"])]
        )
        style.configure("Accent.TButton", background=c["accent"], foreground=c["accent_ink"], font=self.bold)
        style.map(
            "Accent.TButton",
            background=[("disabled", c["bg"]), ("active", c["accent_hover"])],
            foreground=[("disabled", c["muted"])],
        )
        style.configure("TEntry", fieldbackground=c["field"], foreground=c["ink"], insertcolor=c["ink"], padding=5)
        style.configure(
            "TCombobox",
            fieldbackground=c["field"],
            background=c["panel"],
            foreground=c["ink"],
            arrowcolor=c["muted"],
            padding=4,
        )
        style.map(
            "TCombobox",
            fieldbackground=[("readonly", c["field"])],
            foreground=[("readonly", c["ink"])],
            selectbackground=[("readonly", c["field"])],
            selectforeground=[("readonly", c["ink"])],
        )
        self.root.option_add("*TCombobox*Listbox.background", c["field"])
        self.root.option_add("*TCombobox*Listbox.foreground", c["ink"])
        self.root.option_add("*TCombobox*Listbox.selectBackground", c["select"])
        self.root.option_add("*TCombobox*Listbox.selectForeground", c["ink"])
        style.configure("TProgressbar", background=c["accent"], troughcolor=c["field"], borderwidth=0)
        style.configure("Vertical.TScrollbar", background=c["panel"], arrowcolor=c["muted"])
        style.configure("Horizontal.TScrollbar", background=c["panel"], arrowcolor=c["muted"])
        style.configure("TNotebook", background=c["bg"], borderwidth=0, tabmargins=0)
        style.configure(
            "TNotebook.Tab",
            background=c["bg"],
            foreground=c["muted"],
            padding=(14, 6),
            borderwidth=0,
            bordercolor=c["bg"],
            lightcolor=c["bg"],
            darkcolor=c["bg"],
        )
        # Tabs show where you are by color alone, so they drop the focus ring and the border.
        style.layout(
            "TNotebook.Tab",
            [
                (
                    "Notebook.tab",
                    {
                        "sticky": "nswe",
                        "children": [
                            (
                                "Notebook.padding",
                                {
                                    "side": "top",
                                    "sticky": "nswe",
                                    "children": [("Notebook.label", {"side": "top", "sticky": ""})],
                                },
                            )
                        ],
                    },
                )
            ],
        )
        button_layout: list = [
            (
                "Button.border",
                {
                    "sticky": "nswe",
                    "children": [
                        ("Button.padding", {"sticky": "nswe", "children": [("Button.label", {"sticky": "nswe"})]})
                    ],
                },
            )
        ]
        style.layout("View.TButton", button_layout)
        style.layout("ViewActive.Accent.TButton", button_layout)
        # clam enlarges the selected tab through these two options; keep every tab the same size.
        style.map(
            "TNotebook.Tab",
            background=[("selected", c["accent"]), ("active", c["line"])],
            foreground=[("selected", c["accent_ink"])],
            padding=[("selected", (14, 6))],
            expand=[("selected", (0, 0, 0, 0))],
            bordercolor=[("selected", c["accent"]), ("active", c["line"])],
            lightcolor=[("selected", c["accent"]), ("active", c["line"])],
            darkcolor=[("selected", c["accent"]), ("active", c["line"])],
        )

    def _build(self):
        header = ttk.Frame(self.root, padding=(14, 12, 14, 6))
        header.pack(fill="x")
        ttk.Label(header, text="PatchVault", style="Title.TLabel").pack(side="left")
        self.summary = ttk.Label(header, style="Muted.TLabel")
        self.summary.pack(side="left", padx=14)
        self.mod_name = tk.StringVar(value=self.vault.mod_name)
        self.mod_entry = ttk.Entry(header, textvariable=self.mod_name, width=24)
        self.mod_entry.pack(side="right")
        ttk.Label(header, text="Tên mod", style="Muted.TLabel").pack(side="right", padx=(0, 6))
        self.mod_name.trace_add("write", lambda *_: self.vault.set_mod_name(self.mod_name.get()))
        self.tabs = ttk.Notebook(self.root, takefocus=False)
        self.tabs.pack(fill="both", expand=True, padx=14)
        library = ttk.Frame(self.tabs)
        self.tabs.add(library, text="Tải về")
        normalizer = ttk.Frame(self.tabs, padding=(0, 12, 0, 0))
        self.tabs.add(normalizer, text="Chuẩn hóa")
        self.normalizer = NormalizeTab(self, normalizer)
        top = ttk.Frame(library, padding=(0, 10, 0, 6))
        top.pack(fill="x")
        target = self.library_target = ttk.Frame(library, padding=(0, 0, 0, 8))
        target.pack(fill="x")
        self.overall = ttk.Frame(library, padding=(0, 0, 0, 8))
        self.overall_label = ttk.Label(self.overall, style="Muted.TLabel")
        self.overall_label.pack(side="left", padx=(0, 10))
        self.overall_bar = ttk.Progressbar(self.overall, maximum=1000)
        self.overall_bar.pack(side="left", fill="x", expand=True)
        ttk.Label(target, text="Thư mục đích", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.library_views = ViewSwitch(target, self.show_library_view)
        self.download_button = ttk.Button(target, text="Chọn…", command=self.browse_download)
        self.download_button.pack(side="right", padx=(6, 0))
        self.download_dir = tk.StringVar(value=self.vault.download_dir)
        self.download_entry = ttk.Entry(target, textvariable=self.download_dir)
        self.download_entry.pack(side="left", fill="x", expand=True)
        self.download_dir.trace_add("write", lambda *_: self.vault.set_download_dir(self.download_dir.get()))
        self.open_button = ttk.Button(top, text="Mở thư mục", command=self.open_folder)
        self.open_button.pack(side="right", padx=(6, 0))
        self.check_button = ttk.Button(top, text="Cập nhật catalog", command=self.check)
        self.check_button.pack(side="right", padx=(6, 0))
        self.get_all_button = ttk.Button(top, command=self.get_all)
        self.get_all_button.pack(side="right", padx=(6, 0))
        self.update_all_button = ttk.Button(top, style="Accent.TButton", command=self.update_all)
        self.chosen_button = ttk.Button(top, style="Accent.TButton", command=self.get_chosen)
        self.stop_button = ttk.Button(top, text="Dừng tất cả", command=self.vault.cancel_all)
        self.search = tk.StringVar()
        self.search.trace_add("write", lambda *_: self.rebuild_rows())
        ttk.Label(top, text="Catalog", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.catalog_box = ttk.Combobox(top, state="readonly", width=22)
        self.catalog_box.pack(side="left")
        self.catalog_box.bind("<<ComboboxSelected>>", lambda _: self.catalog_changed())
        self.manage_button = ttk.Button(top, text="Quản lý", command=self.open_manager)
        self.manage_button.pack(side="left", padx=(6, 16))
        ttk.Label(top, text="Tìm", style="Muted.TLabel").pack(side="left", padx=(0, 6))
        self.search_entry = ttk.Entry(top, textvariable=self.search, width=26)
        self.search_entry.pack(side="left")
        self.genre_button = ttk.Button(top, text="Thể loại", command=self.toggle_genre_panel)
        self.genre_button.pack(side="left", padx=(8, 0))
        self.root.bind("<Button-1>", self.close_genre_panel_on_click, add="+")
        self.root.bind("<Configure>", self.close_genre_panel_on_move, add="+")

        body = self.library_body = ttk.Frame(library)
        self.download_log = LogPane(self, library)
        self.library_views.show("list")
        table = tk.Frame(body, bg=COLORS["field"])
        table.pack(side="left", fill="both", expand=True)
        self.list_header = tk.Frame(table, bg=COLORS["panel"])
        self.list_header.pack(fill="x")
        for index, (_, title, width) in enumerate(LIST_COLUMNS):
            self.list_header.columnconfigure(index, minsize=width)
            tk.Label(
                self.list_header,
                text=title,
                anchor="w",
                padx=8,
                pady=7,
                bd=0,
                font=self.font,
                bg=COLORS["panel"],
                fg=COLORS["muted"],
            ).grid(row=0, column=index, sticky="ew")
        holder = tk.Frame(table, bg=COLORS["field"])
        holder.pack(fill="both", expand=True)
        self.list_canvas = tk.Canvas(holder, bg=COLORS["field"], bd=0, highlightthickness=0, takefocus=True)
        scroll = ttk.Scrollbar(holder, orient="vertical", command=self.list_canvas.yview)
        self.list_canvas.configure(yscrollcommand=scroll.set)
        scroll.pack(side="right", fill="y")
        self.list_canvas.pack(side="left", fill="both", expand=True)
        self.list_frame = tk.Frame(self.list_canvas, bg=COLORS["line"])
        self.list_window = self.list_canvas.create_window(0, 0, anchor="nw", window=self.list_frame)
        self.list_frame.bind(
            "<Configure>", lambda _: self.list_canvas.configure(scrollregion=self.list_canvas.bbox("all"))
        )
        self.list_canvas.bind("<Configure>", self.fit_list)
        self.list_canvas.bind("<Button-4>", lambda _: self.scroll_list(-1))
        self.list_canvas.bind("<Button-5>", lambda _: self.scroll_list(1))
        self.list_canvas.bind("<MouseWheel>", lambda event: self.scroll_list(-1 if event.delta > 0 else 1))
        self.list_canvas.bind("<Up>", lambda _: self.move_selection(-1))
        self.list_canvas.bind("<Down>", lambda _: self.move_selection(1))
        self.list_canvas.bind("<Return>", lambda _: self.get_chosen() if len(self.picked) > 1 else self.act())
        self.root.bind("<Control-a>", self.pick_all)
        self.root.bind("<Control-A>", self.pick_all)

        detail = ttk.Frame(body, width=440, padding=(14, 0, 0, 0))
        detail.pack(side="left", fill="y")
        detail.pack_propagate(False)
        detail.bind("<Configure>", self.fit_detail)
        self.art = tk.Label(detail, bg=COLORS["bg"], bd=0)
        self.name = ttk.Label(detail, style="Name.TLabel", justify="left")
        self.name.pack(fill="x", pady=(0, 2))
        self.state_label = ttk.Label(detail, justify="left", font=self.bold)
        self.state_label.pack(fill="x", pady=(2, 6))
        self.progress = ttk.Progressbar(detail, maximum=1000)
        self.action = ttk.Button(detail, command=self.act)
        self.action.pack(fill="x", pady=(0, 8))
        frame = ttk.Frame(detail)
        frame.pack(fill="both", expand=True)
        self.text = tk.Text(
            frame,
            wrap="word",
            bd=0,
            highlightthickness=0,
            padx=0,
            pady=0,
            font=self.font,
            bg=COLORS["bg"],
            fg=COLORS["ink"],
            state="disabled",
            cursor="arrow",
            selectbackground=COLORS["select"],
            inactiveselectbackground=COLORS["select"],
        )
        text_scroll = ttk.Scrollbar(frame, orient="vertical", command=self.text.yview)
        self.text.configure(yscrollcommand=text_scroll.set)
        text_scroll.pack(side="right", fill="y")
        self.text.pack(side="left", fill="both", expand=True, padx=(0, 6))
        self._text_tags()

        foot = ttk.Frame(self.root, padding=(14, 8, 14, 10))
        foot.pack(fill="x")
        self.footer = ttk.Label(foot, style="Muted.TLabel")
        self.footer.pack(side="left")
        ttk.Label(foot, text=app_version(), style="Muted.TLabel").pack(side="right")
        self.app_update_button = ttk.Button(foot, style="Accent.TButton", command=self.start_app_update)

    def show_library_view(self, view):
        shown, hidden = (
            (self.download_log.frame, self.library_body)
            if view == "log"
            else (self.library_body, self.download_log.frame)
        )
        hidden.pack_forget()
        shown.pack(fill="both", expand=True)

    def _text_tags(self):
        c = COLORS
        tag = self.text.tag_configure
        tag("tagline", font=(self.family, 11), spacing3=8)
        tag("chip", background=c["panel"], foreground=c["ink"], font=(self.family, 9))
        tag("chips", spacing3=4, spacing2=6)
        tag("heading", foreground=c["accent"], font=(self.family, 9, "bold"), spacing1=12, spacing3=4)
        tag("body", spacing2=2, spacing3=6)
        tag("row", lmargin2=self.KEY_WIDTH, tabs=(self.KEY_WIDTH,), spacing3=3)
        tag("key", foreground=c["muted"])
        tag("item", lmargin1=0, lmargin2=16, spacing2=2, spacing3=3)
        tag("strong", font=self.bold)
        tag("yes", foreground=c["ok"])
        tag("no", foreground=c["muted"])
        tag("missing_value", foreground=c["miss"])

    def fit_detail(self, event):
        for label in (self.name, self.state_label):
            label.configure(wraplength=event.width - 24)

    def toggle_genre_panel(self):
        if self.genre_panel:
            self.close_genre_panel()
            return
        panel = tk.Toplevel(self.root, bg=COLORS["line"], bd=0)
        panel.overrideredirect(True)
        panel.transient(self.root)
        panel.geometry(
            f"+{self.genre_button.winfo_rootx()}"
            f"+{self.genre_button.winfo_rooty() + self.genre_button.winfo_height() + 4}"
        )
        panel.bind("<Escape>", lambda _: self.close_genre_panel())
        self.genre_panel = panel
        self.genre_panel_anchor = self.root.geometry()
        body = tk.Frame(panel, bg=COLORS["panel"], padx=10, pady=8)
        body.pack(padx=1, pady=1)
        for index, genre in enumerate(self.genres):
            variable = tk.BooleanVar(value=genre in self.selected_genres)
            tk.Checkbutton(
                body,
                text=genre,
                variable=variable,
                anchor="w",
                bd=0,
                highlightthickness=0,
                font=self.font,
                bg=COLORS["panel"],
                fg=COLORS["ink"],
                selectcolor=COLORS["field"],
                activebackground=COLORS["line"],
                activeforeground=COLORS["ink"],
                command=lambda g=genre, v=variable: self.toggle_genre(g, v.get()),
            ).grid(row=index // 2, column=index % 2, sticky="ew", padx=(0, 10), pady=1)
        ttk.Button(body, text="Bỏ chọn tất cả", command=self.clear_genres).grid(
            row=len(self.genres) // 2 + 1, column=0, columnspan=2, sticky="ew", pady=(8, 0)
        )
        panel.focus_set()

    def close_genre_panel(self):
        if self.genre_panel:
            self.genre_panel.destroy()
            self.genre_panel = None

    def close_genre_panel_on_click(self, event):
        if self.genre_panel and event.widget.winfo_toplevel() is self.root and event.widget is not self.genre_button:
            self.close_genre_panel()

    def close_genre_panel_on_move(self, event):
        # The window also reports Configure when it stays put, so compare the real position.
        if self.genre_panel and event.widget is self.root and self.root.geometry() != self.genre_panel_anchor:
            self.close_genre_panel()

    def toggle_genre(self, genre, selected):
        if selected:
            self.selected_genres.add(genre)
        else:
            self.selected_genres.discard(genre)
        self.paint_genre_button()
        self.rebuild_rows()

    def clear_genres(self):
        self.selected_genres.clear()
        self.close_genre_panel()
        self.paint_genre_button()
        self.rebuild_rows()

    def paint_genre_button(self):
        selected = sorted(self.selected_genres, key=str.lower)
        text = (
            "Thể loại"
            if not selected
            else f"Thể loại: {selected[0]}"
            if len(selected) == 1
            else f"Thể loại ({len(selected)})"
        )
        self.genre_button.configure(text=text)

    def matches(self, item):
        query = self.search.get().strip().lower()
        haystack = " ".join(
            [item["name"], clean_game_name(item["name"]), item["developer"], item["translator"], *item["tags"]]
        ).lower()
        return (not query or query in haystack) and (
            not self.selected_genres or not self.selected_genres.isdisjoint(item["tags"])
        )

    def fit_list(self, event):
        self.list_canvas.itemconfigure(self.list_window, width=event.width)
        self.game_width = max(200, event.width - sum(width for _, _, width in LIST_COLUMNS))
        self.list_header.columnconfigure(0, minsize=self.game_width)
        for row in self.rows.values():
            row.fit(self.game_width)

    def scroll_list(self, units):
        if self.list_frame.winfo_height() > self.list_canvas.winfo_height():
            self.list_canvas.yview_scroll(units, "units")

    def select(self, entry_id):
        self.picked = {entry_id}
        self.pick_anchor = entry_id
        self.show_picked(entry_id)

    def show_picked(self, entry_id):
        self.selected = entry_id
        self.list_canvas.focus_set()
        self.paint_rows()
        self.show_detail()
        self.paint_chosen()

    def toggle_pick(self, entry_id):
        if entry_id in self.picked and len(self.picked) > 1:
            self.picked.discard(entry_id)
            entry_id = next(shown for shown in self.shown_ids if shown in self.picked)
        else:
            self.picked.add(entry_id)
        self.pick_anchor = entry_id
        self.show_picked(entry_id)

    def pick_range(self, entry_id):
        if self.pick_anchor not in self.shown_ids:
            self.select(entry_id)
            return
        first, last = sorted((self.shown_ids.index(self.pick_anchor), self.shown_ids.index(entry_id)))
        self.picked = set(self.shown_ids[first : last + 1])
        self.show_picked(entry_id)

    def pick_all(self, _event):
        # Typing fields keep their own meaning of the shortcut.
        if (
            self.tabs.index("current") == 0
            and self.shown_ids
            and not isinstance(self.root.focus_get(), (tk.Entry, tk.Text))
        ):
            self.picked = set(self.shown_ids)
            self.show_picked(self.selected if self.selected in self.picked else self.shown_ids[0])
        return "break"

    def paint_chosen(self):
        items = (self.items.get(entry_id) for entry_id in self.shown_ids or [] if entry_id in self.picked)
        self.chosen = [
            item["id"] for item in items if item and not item["job"] and item["status"] in ("missing", "update")
        ]
        if len(self.picked) > 1:
            self.chosen_button.configure(text=f"Tải đã chọn ({len(self.chosen)})")
            self.chosen_button.state(["!disabled"] if self.chosen else ["disabled"])
            if not self.chosen_button.winfo_manager():
                self.chosen_button.pack(side="right", padx=(6, 0), before=self.get_all_button)
        else:
            self.chosen_button.pack_forget()

    def move_selection(self, step):
        if self.selected in self.shown_ids:
            index = self.shown_ids.index(self.selected) + step
            if 0 <= index < len(self.shown_ids):
                self.select(self.shown_ids[index])
                self.see(self.rows[self.selected].frame)

    def see(self, frame):
        self.root.update_idletasks()
        total = self.list_frame.winfo_height()
        visible = self.list_canvas.winfo_height()
        top = self.list_canvas.canvasy(0)
        if frame.winfo_y() < top:
            self.list_canvas.yview_moveto(frame.winfo_y() / total)
        elif frame.winfo_y() + frame.winfo_height() > top + visible:
            self.list_canvas.yview_moveto((frame.winfo_y() + frame.winfo_height() - visible) / total)

    def build_rows(self):
        signature = (self.vault.root, *((item["id"], tuple(item["tags"])) for item in self.items.values()))
        if signature == self.row_signature:
            return
        self.row_signature = signature
        for row in self.rows.values():
            row.frame.destroy()
        self.rows = {entry_id: GameRow(self, item) for entry_id, item in self.items.items()}
        for row in self.rows.values():
            row.fit(self.game_width)
        self.shown_ids = None
        self.rebuild_rows()

    def rebuild_rows(self):
        ids = [entry_id for entry_id, item in self.items.items() if self.matches(item)]
        if ids != self.shown_ids:
            self.shown_ids = ids
            for row in self.rows.values():
                row.frame.pack_forget()
            for entry_id in ids:
                self.rows[entry_id].frame.pack(fill="x", pady=(0, 1))
            if self.selected not in ids:
                self.selected = ids[0] if ids else None
            self.picked = (self.picked & set(ids)) or ({self.selected} if ids else set())
            self.pick_anchor = self.selected
            self.list_canvas.yview_moveto(0)
        self.paint_rows()

    def paint_rows(self):
        for entry_id in self.shown_ids or []:
            self.rows[entry_id].paint(self.items[entry_id], entry_id in self.picked)

    def show_detail(self):
        entry_id = self.selected
        item = self.items.get(entry_id)
        key = (entry_id, item["sha256"]) if item else None
        if key != self.detail_key:
            self.detail_key = key
            self.detail_id = entry_id
            self.art_id = None
            self.art.pack_forget()
            self.fill_text(item)
        if not item:
            for label in (self.name, self.state_label):
                label.configure(text="")
            self.action.state(["disabled"])
            return
        self.show_art(item)
        self.name.configure(text=clean_game_name(item["name"]))
        self.paint_action(item)

    def fill_text(self, item):
        text = self.text
        text.configure(state="normal")
        text.delete("1.0", "end")
        if item:
            self.write_detail(item)
        text.configure(state="disabled")
        text.yview_moveto(0)

    def write_detail(self, item):
        add = self.text.insert

        def heading(title):
            add("end", title.upper() + "\n", "heading")

        def row(key, value, *tags):
            add("end", key + "\t", ("key", "row"))
            add("end", value + "\n", ("row", *tags))

        def bullets(title, values, mark, style):
            if values:
                heading(title)
                for value in values:
                    add("end", mark + "  ", ("item", style))
                    add("end", value + "\n", "item")

        if item["tagline"]:
            add("end", item["tagline"] + "\n", "tagline")
        if item["tags"]:
            for tag in item["tags"]:
                add("end", " " + tag.replace(" ", " ") + " ", ("chip", "chips"))
                add("end", " ", "chips")
            add("end", "\n", "chips")

        heading("Thông tin")
        for key, value in (
            ("Nhà phát triển", item["developer"]),
            ("Phát hành", item["release"]),
            ("Dịch bởi", item["translator"]),
        ):
            if value:
                row(key, value)
        row("Phiên bản việt hóa", item["version"] or "-")
        if item["game_version"]:
            row("Phiên bản game", item["game_version"])
        else:
            row("Phiên bản game", "Miss", "missing_value")
        row("Title ID", item["title_id"])
        row("Dung lượng tải", format_size(item["size"]))

        if item["description"]:
            heading("Giới thiệu")
            for line in item["description"]:
                add("end", line + "\n", "body")
        if item["highlights"]:
            heading("Điểm nổi bật")
            for entry in item["highlights"]:
                if entry["title"]:
                    add("end", entry["title"] + "\n", "strong")
                if entry["text"]:
                    add("end", entry["text"] + "\n", "body")
        bullets("Đã việt hóa", item["done"], "✓", "yes")
        bullets("Giữ nguyên", item["kept"], "–", "no")
        if item["changelog"]:
            heading("Ghi chú")
            add("end", item["changelog"] + "\n", "body")
        if item["credit"]:
            heading("Credit")
            add("end", item["credit"] + "\n", "body")

    def paint_action(self, item):
        job = item["job"]
        active = bool(job) and job["phase"] != "error"
        self.action.state(["!disabled"])
        if job and job["phase"] == "error":
            message, label, accent = "Lỗi: " + job.get("message", ""), "Thử lại", True
        elif active:
            message, label, accent = status_text(item), "Huỷ", False
        elif item["status"] == "update":
            message, label, accent = status_text(item), "Cập nhật", True
        elif item["status"] == "ok":
            message, label, accent = "Đã tải, mới nhất", "Tải lại", False
        else:
            message, label, accent = "Chưa tải", "Tải về", True
        self.state_label.configure(text=message)
        self.action.configure(text=label, style="Accent.TButton" if accent else "TButton")
        if active:
            self.progress["value"] = 1000 * job["done"] / job["total"] if job["total"] else 0
            if not self.progress.winfo_manager():
                self.progress.pack(fill="x", pady=(0, 6), before=self.action)
        else:
            self.progress.pack_forget()

    def show_art(self, item):
        image = self.art_cache.get((self.vault.root.name, item["id"]))
        if image is None or self.art_id == item["id"]:
            return
        self.art_id = item["id"]
        self.art_image = ImageTk.PhotoImage(image)
        self.art.configure(image=self.art_image)
        self.art.pack(before=self.name, anchor="w", pady=(0, 10))

    def preload_art(self):
        vault = self.vault
        items = [item for item in self.items.values() if item["cover"] or item["background"]]
        signature = (vault.root.name, *((item["cover"], item["background"]) for item in items))
        if Image is None or signature == self.art_signature:
            return
        self.art_signature = signature
        art_dir = vault.art_dir
        art_dir.mkdir(parents=True, exist_ok=True)
        files = {art_file_name(item, kind): item[kind] for item in items for kind in ART_KINDS if item[kind]}
        known = vault._load_json(vault.art_index, {})
        for old in art_dir.iterdir():
            if old.name not in files or known.get(old.name) != files[old.name]:
                old.unlink(missing_ok=True)
        vault._write_json(vault.art_index, files)
        pending = queue.Queue()
        for item in items:
            pending.put(item)
        for _ in range(ART_WORKERS):
            threading.Thread(target=self.art_worker, args=(pending, art_dir), daemon=True).start()

    def art_worker(self, pending, art_dir):
        while True:
            try:
                item = pending.get_nowait()
            except queue.Empty:
                return
            cover = self.load_image(item, "cover", art_dir)
            background = self.load_image(item, "background", art_dir)
            if cover or background:
                try:
                    self.art_cache[(art_dir.name, item["id"])] = compose_banner(cover, background, self.ART_SIZE)
                except Exception:
                    pass

    def load_image(self, item, kind, art_dir):
        url = item[kind]
        if not url:
            return None
        path = art_dir / art_file_name(item, kind)
        try:
            if not path.is_file():
                with net.http_get(url) as response:
                    data = response.read(MAX_IMAGE + 1)
                if len(data) > MAX_IMAGE:
                    return None
                temp = path.with_suffix(".tmp")
                temp.write_bytes(data)
                os.replace(temp, path)
            with Image.open(path) as image:
                return image.convert("RGB")
        except Exception:
            return None

    def refresh(self):
        snapshot = self.vault.snapshot()
        items = {item["id"]: item for item in snapshot["items"]}
        self.items = items
        genres = sorted({tag for item in items.values() for tag in item["tags"]}, key=str.lower)
        if genres != self.genres:
            self.genres = genres
            self.close_genre_panel()
            self.selected_genres &= set(genres)
            self.paint_genre_button()
            self.shown_ids = None
        self.paint_catalogs(snapshot)
        self.build_rows()
        self.paint_rows()
        self.preload_art()
        self.show_detail()
        self.paint_header(snapshot)
        self.download_log.paint(snapshot["log"])
        self.paint_overall(snapshot["batch"])
        self.normalizer.paint(self.vault.normalize_snapshot())
        self.mod_entry.state(["disabled"] if self.vault.normalizing() else ["!disabled"])
        downloading = ["disabled"] if self.vault.busy() else ["!disabled"]
        self.download_entry.state(downloading)
        self.download_button.state(downloading)
        self.paint_app_update(snapshot["app_update"], snapshot["app_update_job"])
        self.root.after(400, self.refresh)

    def paint_overall(self, batch):
        if not batch["active"]:
            self.overall.pack_forget()
            # Numbers, not a seen-it-running flag: a short batch can start and end between repaints.
            if batch["number"] != self.reported_batch:
                self.reported_batch = batch["number"]
                self.report_downloads(batch)
            return
        self.overall_bar["value"] = 1000 * batch["fraction"]
        self.overall_label.configure(
            text=f"Đang tải {batch['finished']}/{batch['count']} mod · {100 * batch['fraction']:.0f}%"
        )
        if not self.overall.winfo_manager():
            self.overall.pack(fill="x", after=self.library_target)

    def report_downloads(self, batch):
        if batch["saved"] or batch["failed"]:
            text = f"Đã tải xong {batch['saved']}/{batch['count']} mod."
            if batch["failed"]:
                text += f" {batch['failed']} mod lỗi."
            if batch["cancelled"]:
                text += f" {batch['cancelled']} mod đã huỷ."
            self.root.after_idle(show_message, "Tải xong", text, self.root)

    def paint_catalogs(self, snapshot):
        folders = [catalog["folder"] for catalog in snapshot["catalogs"]]
        if folders != self.catalog_folders:
            self.catalog_folders = folders
            self.catalog_box.configure(values=[catalog["name"] for catalog in snapshot["catalogs"]])
        if snapshot["active"] in folders:
            self.catalog_box.current(folders.index(snapshot["active"]))
        else:
            self.catalog_box.set("")

    def catalog_changed(self):
        self.catalog_box.selection_clear()
        index = self.catalog_box.current()
        if index < 0:
            return
        if self.vault.busy():
            show_message("Đang tải", "Hãy đợi các file đang tải xong rồi đổi catalog.", parent=self.root)
            return
        self.vault.select_catalog(self.catalog_folders[index])
        self.check()

    def open_manager(self):
        if self.manager and self.manager.window.winfo_exists():
            self.manager.window.lift()
        else:
            self.manager = CatalogManager(self)

    def paint_header(self, snapshot):
        idle = [item for item in self.items.values() if not item["job"]]
        self.updates = [item["id"] for item in idle if item["status"] == "update"]
        missing = [item for item in idle if item["status"] == "missing"]
        self.missing = [item["id"] for item in missing]
        have = sum(item["status"] != "missing" for item in self.items.values())
        parts = [f"{have}/{len(self.items)} đã tải"]
        if self.updates:
            parts.append(f"{len(self.updates)} bản cập nhật mới")
        if snapshot["checked_at"]:
            parts.append("kiểm tra lúc " + datetime.fromtimestamp(snapshot["checked_at"]).strftime("%H:%M:%S"))
        self.summary.configure(text=" · ".join(parts))
        if self.updates:
            self.update_all_button.configure(text=f"Cập nhật tất cả ({len(self.updates)})")
            self.update_all_button.pack(side="right", padx=(6, 0), before=self.get_all_button)
        else:
            self.update_all_button.pack_forget()
        size = format_size(sum(item["size"] for item in missing)) if missing else ""
        self.get_all_button.configure(text=f"Tải tất cả ({size})" if size else "Tải tất cả")
        self.get_all_button.state(["!disabled"] if missing else ["disabled"])
        self.paint_chosen()
        if self.vault.busy():
            if not self.stop_button.winfo_manager():
                self.stop_button.pack(side="right", padx=(6, 0), before=self.get_all_button)
        else:
            self.stop_button.pack_forget()
        self.check_button.configure(text="Đang cập nhật…" if snapshot["checking"] else "Cập nhật catalog")
        self.check_button.state(["disabled"] if snapshot["checking"] else ["!disabled"])
        if not self.vault.catalog:
            self.summary.configure(text="Chưa có catalog")
            self.footer.configure(text="Bấm Quản lý để thêm catalog đầu tiên", style="Muted.TLabel")
        elif snapshot["error"]:
            self.footer.configure(text="Không cập nhật được catalog: " + snapshot["error"], style="Error.TLabel")
        else:
            self.footer.configure(text="Lưu vào: " + snapshot["dir"], style="Muted.TLabel")

    def paint_app_update(self, update, job):
        self.app_update = update
        if not update:
            return
        phase = job["phase"] if job else None
        if phase == "done":
            restart_program()
            self.root.destroy()
            return
        if phase == "error":
            self.vault.app_update_job = None
            self.root.after_idle(self.report_update_error, job["message"])
        if phase == "downloading":
            label = f"Đang cập nhật {100 * job['done'] // job['total']}%"
        elif phase == "installing":
            label = "Đang cài bản mới"
        else:
            label = f"Có bản mới {update['tag']}"
        self.app_update_button.configure(text=label)
        self.app_update_button.state(["disabled"] if phase in ("downloading", "installing") else ["!disabled"])
        if not self.app_update_button.winfo_manager():
            self.app_update_button.pack(side="right", padx=(0, 10))
        if update["tag"] != self.prompted_tag:
            self.prompted_tag = update["tag"]
            self.root.after_idle(self.prompt_app_update)

    def prompt_app_update(self):
        action = (
            "Tải về và cập nhật ngay? Ứng dụng sẽ tự khởi động lại."
            if can_self_update(self.app_update)
            else "Mở trang tải về để cập nhật?"
        )
        if ask_yes_no("Có phiên bản mới", f"Đã có PatchVault {self.app_update['tag']}. {action}", parent=self.root):
            self.start_app_update()

    def start_app_update(self):
        update = self.app_update
        if not can_self_update(update):
            open_in_system(update["url"])
        elif self.vault.normalizing():
            show_message("Đang chuẩn hóa", "Hãy đợi chuẩn hóa xong rồi cập nhật ứng dụng.", parent=self.root)
        elif any(job["phase"] != "error" for job in self.vault.snapshot_jobs()):
            show_message("Đang tải", "Hãy đợi các file đang tải xong rồi cập nhật ứng dụng.", parent=self.root)
        else:
            self.vault.set_update_job(phase="queued", done=0, total=1)
            threading.Thread(target=self.vault.install_app_update, args=(update,), daemon=True).start()

    def report_update_error(self, message):
        text = f"Không cập nhật được: {message}\n\nMở trang tải về để cập nhật thủ công?"
        if ask_yes_no("Lỗi cập nhật", text, parent=self.root):
            open_in_system(self.app_update["url"])

    def has_mod_name(self):
        if self.vault.mod_name:
            return True
        show_message("Thiếu tên mod", "Hãy nhập Tên mod ở góc trên bên phải trước khi tải.", parent=self.root)
        self.mod_entry.focus_set()
        return False

    def act(self):
        item = self.items.get(self.detail_id)
        if not item:
            return
        job = item["job"]
        if job and job["phase"] != "error":
            self.vault.cancel(item["id"])
        elif self.has_mod_name():
            self.vault.clear_error(item["id"])
            self.vault.enqueue([item["id"]])

    def update_all(self):
        if self.has_mod_name():
            self.vault.enqueue(self.updates)

    def get_chosen(self):
        if self.chosen and self.has_mod_name():
            self.vault.enqueue(self.chosen)

    def get_all(self):
        if self.has_mod_name():
            self.vault.enqueue(self.missing)

    def check(self):
        threading.Thread(target=self.vault.check, daemon=True).start()

    def browse_download(self):
        folder = filedialog.askdirectory(parent=self.root, initialdir=self.download_dir.get() or None)
        if folder:
            self.download_dir.set(folder)

    def open_folder(self):
        self.vault.root.mkdir(parents=True, exist_ok=True)
        open_in_system(self.vault.root)

    def close(self):
        active = [job for job in self.vault.snapshot_jobs() if job["phase"] != "error"]
        if self.vault.app_update_job:
            active.append(self.vault.app_update_job)
        if active and not ask_yes_no(
            "Đang tải",
            "Vẫn còn file đang tải. Thoát bây giờ? Lần sau có thể tải tiếp từ phần đã tải.",
            parent=self.root,
        ):
            return
        if self.vault.normalizing() and not ask_yes_no(
            "Đang chuẩn hóa",
            "Vẫn đang chuẩn hóa mod. Thoát bây giờ? File gốc chưa xử lý xong vẫn được giữ.",
            parent=self.root,
        ):
            return
        self.root.destroy()

    def run(self):
        self.root.mainloop()
