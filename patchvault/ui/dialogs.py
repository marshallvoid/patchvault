import tkinter as tk
from tkinter import ttk

from .theme import COLORS


def popup(title, message, parent, buttons):
    # Native message boxes cannot center their text.
    window = tk.Toplevel(parent, bg=COLORS["bg"])
    window.withdraw()
    window.title(title)
    window.resizable(False, False)
    window.transient(parent.winfo_toplevel())
    chosen = [len(buttons) - 1]

    def choose(index):
        chosen[0] = index
        window.destroy()

    frame = ttk.Frame(window, padding=(28, 22, 28, 18))
    frame.pack(fill="both", expand=True)
    ttk.Label(frame, text=message, justify="center", anchor="center", wraplength=460).pack(fill="x", pady=(0, 18))
    row = ttk.Frame(frame)
    row.pack()
    for index, label in enumerate(buttons):
        ttk.Button(
            row,
            text=label,
            width=10,
            style="TButton" if index else "Accent.TButton",
            command=lambda value=index: choose(value),
        ).pack(side="left", padx=4)
    window.bind("<Return>", lambda _: choose(0))
    window.bind("<Escape>", lambda _: choose(len(buttons) - 1))
    window.protocol("WM_DELETE_WINDOW", lambda: choose(len(buttons) - 1))
    parent.update_idletasks()
    window.update_idletasks()
    x = parent.winfo_rootx() + (parent.winfo_width() - window.winfo_reqwidth()) // 2
    y = parent.winfo_rooty() + (parent.winfo_height() - window.winfo_reqheight()) // 2
    window.geometry(f"+{max(0, x)}+{max(0, y)}")
    window.deiconify()
    window.wait_visibility()
    window.grab_set()
    window.focus_set()
    window.wait_window()
    return chosen[0]


def show_message(title, message, parent):
    popup(title, message, parent, ("OK",))


def ask_yes_no(title, message, parent):
    return popup(title, message, parent, ("Có", "Không")) == 0
