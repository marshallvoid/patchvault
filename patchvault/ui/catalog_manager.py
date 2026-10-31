import http.client
import threading
import tkinter as tk
from tkinter import ttk

from ..catalog import MAPPABLE_FIELDS
from .dialogs import ask_yes_no
from .theme import COLORS


class CatalogManager:
    def __init__(self, app):
        self.app = app
        self.vault = app.vault
        self.result = None
        self.window = tk.Toplevel(app.root, bg=COLORS["bg"])
        self.window.title("Quản lý catalog")
        self.window.geometry("640x430")
        self.window.minsize(520, 360)
        self.window.transient(app.root)
        frame = ttk.Frame(self.window, padding=14)
        frame.pack(fill="both", expand=True)
        frame.columnconfigure(1, weight=1)
        frame.rowconfigure(0, weight=1)
        self.listbox = tk.Listbox(
            frame,
            bg=COLORS["field"],
            fg=COLORS["ink"],
            bd=0,
            highlightthickness=0,
            selectbackground=COLORS["select"],
            selectforeground=COLORS["ink"],
            activestyle="none",
            font=app.font,
        )
        self.listbox.grid(row=0, column=0, columnspan=2, sticky="nsew")
        self.listbox.bind("<<ListboxSelect>>", lambda _: self.paint_buttons())
        self.remove_button = ttk.Button(frame, text="Xoá khỏi danh sách", command=self.remove)
        self.remove_button.grid(row=1, column=0, columnspan=2, sticky="e", pady=(6, 12))
        self.name = tk.StringVar()
        self.url = tk.StringVar()
        ttk.Label(frame, text="Tên", style="Muted.TLabel").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
        ttk.Entry(frame, textvariable=self.name).grid(row=2, column=1, sticky="ew", pady=3)
        ttk.Label(frame, text="URL", style="Muted.TLabel").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
        url_entry = ttk.Entry(frame, textvariable=self.url)
        url_entry.grid(row=3, column=1, sticky="ew", pady=3)
        url_entry.bind("<Return>", lambda _: self.add())

        # Advanced panel: an optional map from PatchVault's fields to this catalog's own keys,
        # for a JSON shape the heuristics do not recognise. Blank = pure auto-detect.
        self.advanced = tk.BooleanVar(value=False)
        self.toggle = tk.Checkbutton(
            frame,
            variable=self.advanced,
            command=self.toggle_advanced,
            bg=COLORS["bg"],
            fg=COLORS["muted"],
            activebackground=COLORS["bg"],
            activeforeground=COLORS["ink"],
            selectcolor=COLORS["field"],
            bd=0,
            highlightthickness=0,
            font=app.font,
            anchor="w",
            text="Ánh xạ trường (nâng cao)",
        )
        self.toggle.grid(row=4, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.fields = {}
        self.panel = ttk.Frame(frame)
        self.panel.grid(row=5, column=0, columnspan=2, sticky="ew", pady=(2, 0))
        self.panel.columnconfigure(1, weight=1)
        self.panel.columnconfigure(3, weight=1)
        ttk.Label(
            self.panel,
            wraplength=560,
            justify="left",
            style="Muted.TLabel",
            text="Để trống = tự dò. Điền tên khoá trong JSON của catalog; có thể dùng dấu "
            "chấm để vào mảng con, ví dụ files.0.url.",
        ).grid(row=0, column=0, columnspan=4, sticky="w", pady=(0, 6))
        for index, (field, label) in enumerate(MAPPABLE_FIELDS):
            var = tk.StringVar()
            self.fields[field] = var
            row, column = 1 + index // 2, (index % 2) * 2
            ttk.Label(self.panel, text=label, style="Muted.TLabel").grid(
                row=row, column=column, sticky="w", padx=(0, 8), pady=2
            )
            ttk.Entry(self.panel, textvariable=var, width=16).grid(
                row=row, column=column + 1, sticky="ew", padx=(0, 12), pady=2
            )
        self.panel.grid_remove()

        self.status = ttk.Label(frame, style="Muted.TLabel", wraplength=560, justify="left")
        self.status.grid(row=6, column=0, columnspan=2, sticky="w", pady=(8, 0))
        self.add_button = ttk.Button(frame, text="Thêm catalog", style="Accent.TButton", command=self.add)
        self.add_button.grid(row=7, column=0, columnspan=2, sticky="e", pady=(8, 0))
        self.fill_list()
        url_entry.focus_set()

    def toggle_advanced(self):
        if self.advanced.get():
            self.panel.grid()
        else:
            self.panel.grid_remove()

    def current_mapping(self):
        return {field: var.get() for field, var in self.fields.items() if var.get().strip()}

    def fill_list(self):
        self.listbox.delete(0, "end")
        for catalog in self.vault.catalogs:
            marker = "  ⚙" if catalog.get("mapping") else ""
            self.listbox.insert("end", f"{catalog['name']}  ·  {catalog['url']}{marker}")
        self.paint_buttons()

    def paint_buttons(self):
        self.remove_button.state(["!disabled"] if self.listbox.curselection() else ["disabled"])

    def add(self):
        if self.result is not None or "disabled" in self.add_button.state():
            return
        self.add_button.state(["disabled"])
        self.status.configure(text="Đang đọc catalog…", style="Muted.TLabel")
        threading.Thread(
            target=self.run_add, args=(self.name.get(), self.url.get(), self.current_mapping()), daemon=True
        ).start()
        self.window.after(150, self.wait_for_add)

    def run_add(self, name, url, mapping):
        try:
            catalog = self.vault.add_catalog(name, url, mapping)
            self.result = ("ok", f"Đã thêm {catalog['name']} ({len(self.vault.items)} game).")
        except (OSError, ValueError, http.client.HTTPException) as error:
            self.result = ("error", str(error) or type(error).__name__)

    def wait_for_add(self):
        if not self.window.winfo_exists():
            return
        if self.result is None:
            self.window.after(150, self.wait_for_add)
            return
        state, message = self.result
        self.result = None
        self.add_button.state(["!disabled"])
        self.status.configure(text=message, style="Muted.TLabel" if state == "ok" else "Error.TLabel")
        if state == "ok":
            self.name.set("")
            self.url.set("")
            for var in self.fields.values():
                var.set("")
            self.fill_list()

    def remove(self):
        selected = self.listbox.curselection()
        if not selected:
            return
        catalog = self.vault.catalogs[selected[0]]
        if self.vault.busy() and catalog is self.vault.catalog:
            self.status.configure(text="Hãy đợi các file đang tải xong.", style="Error.TLabel")
            return
        if ask_yes_no(
            "Xoá catalog", f"Xoá {catalog['name']} khỏi danh sách? File đã tải vẫn được giữ lại.", parent=self.window
        ):
            self.vault.remove_catalog(catalog["folder"])
            self.fill_list()
