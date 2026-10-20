"""Shared light/dark Tkinter theme matching the VK downloader UI."""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk
from typing import Callable


THEME_PALETTES = {
    "light": {
        "app": "#F2F5F9", "panel": "#F8FAFD", "white": "#FFFFFF",
        "text": "#202B3C", "muted": "#627187", "border": "#D7E0EB",
        "accent": "#0077FF", "accent_active": "#0066DE",
        "accent_disabled": "#9ABFF0", "accent_text": "#FFFFFF",
        "danger_bg": "#FDECEC", "danger_text": "#B42318", "danger_border": "#F5C5C5",
        "selection": "#C9E1FF", "log_green": "#16803C", "log_gray": "#687386",
        "log_amber": "#9A6700", "log_blue": "#175CD3", "button": "#E5ECF5",
        "button_active": "#DCE8F6", "button_pressed": "#D5E1EF", "button_disabled": "#E9EEF5",
        "disabled_text": "#99A4B4", "progress_track": "#DFE7F1", "scrollbar": "#D7E0EB",
        "danger_active": "#F9DEDE", "danger_pressed": "#F5D0D0", "danger_disabled": "#F7F2F2",
    },
    "dark": {
        "app": "#1C2026", "panel": "#252A31", "white": "#30363F",
        "text": "#E7EAF0", "muted": "#A8B0BC", "border": "#424A55",
        "accent": "#2D77D4", "accent_active": "#2465BA",
        "accent_disabled": "#3E5876", "accent_text": "#FFFFFF",
        "danger_bg": "#482D32", "danger_text": "#FFB4AB", "danger_border": "#704148",
        "selection": "#3C5879", "log_green": "#7EE0A3", "log_gray": "#A4ACB8",
        "log_amber": "#F2C66D", "log_blue": "#91B8FF", "button": "#363D47",
        "button_active": "#454E5B", "button_pressed": "#2E343D", "button_disabled": "#2B3037",
        "disabled_text": "#77818E", "progress_track": "#343B45", "scrollbar": "#454E5B",
        "danger_active": "#613B42", "danger_pressed": "#542F37", "danger_disabled": "#332E32",
    },
}


class ThemeManager:
    """Applies the VK downloader's light/dark palette to a Tkinter window."""

    def __init__(self, root: tk.Misc, theme: str = "light"):
        self.root = root
        self.style = ttk.Style(root)
        self.current = "light"
        self.palette = dict(THEME_PALETTES["light"])
        self.theme_button = None
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self.apply(theme, recolor=False)

    def set_button(self, button) -> None:
        self.theme_button = button
        self._update_button_text()

    def configure_styles(self) -> None:
        p = self.palette
        style = self.style
        style.configure(".", font=("Segoe UI", 10), foreground=p["text"], background=p["panel"])
        style.configure("TFrame", background=p["panel"])
        style.configure("App.TFrame", background=p["app"])
        style.configure(
            "TLabelframe", background=p["panel"], bordercolor=p["border"],
            lightcolor=p["border"], darkcolor=p["border"], relief="solid", borderwidth=1,
        )
        style.configure(
            "TLabelframe.Label", background=p["panel"], foreground=p["accent"],
            font=("Segoe UI", 10, "bold"), padding=(4, 0),
        )
        style.configure("TLabel", background=p["panel"], foreground=p["text"])
        style.configure(
            "Header.TLabel", background=p["app"], foreground=p["text"],
            font=("Segoe UI", 19, "bold"),
        )
        style.configure(
            "Muted.TLabel", background=p["app"], foreground=p["muted"],
            font=("Segoe UI", 9),
        )
        style.configure("TCheckbutton", background=p["panel"], foreground=p["text"], padding=(3, 3))
        style.map(
            "TCheckbutton", background=[("active", p["panel"])],
            foreground=[("disabled", p["disabled_text"])],
        )
        style.configure("TRadiobutton", background=p["panel"], foreground=p["text"], padding=(3, 3))
        style.map("TRadiobutton", background=[("active", p["panel"])])
        style.configure(
            "TEntry", fieldbackground=p["white"], foreground=p["text"],
            insertcolor=p["text"], bordercolor=p["border"],
            lightcolor=p["border"], darkcolor=p["border"], padding=5,
        )
        style.map(
            "TEntry",
            fieldbackground=[("disabled", p["panel"]), ("!disabled", p["white"])],
            foreground=[("disabled", p["disabled_text"])],
        )
        style.configure(
            "TSpinbox", fieldbackground=p["white"], foreground=p["text"],
            bordercolor=p["border"], padding=4, arrowsize=12,
        )
        style.configure(
            "TCombobox", fieldbackground=p["white"], foreground=p["text"],
            background=p["button"], arrowcolor=p["text"], bordercolor=p["border"],
        )
        style.map(
            "TCombobox", fieldbackground=[("readonly", p["white"])],
            foreground=[("disabled", p["disabled_text"])],
        )
        style.configure(
            "TButton", background=p["button"], foreground=p["text"],
            bordercolor=p["border"], padding=(11, 7), relief="flat",
        )
        style.map(
            "TButton",
            background=[
                ("disabled", p["button_disabled"]),
                ("pressed", p["button_pressed"]),
                ("active", p["button_active"]),
            ],
            foreground=[("disabled", p["disabled_text"])],
        )
        style.configure(
            "Accent.TButton", background=p["accent"], foreground=p["accent_text"],
            bordercolor=p["accent"], padding=(14, 8),
            font=("Segoe UI", 10, "bold"), relief="flat",
        )
        style.map(
            "Accent.TButton",
            background=[
                ("disabled", p["accent_disabled"]),
                ("pressed", p["accent_active"]),
                ("active", p["accent_active"]),
            ],
            foreground=[("disabled", p["accent_text"]), ("!disabled", p["accent_text"])],
        )
        style.configure(
            "Danger.TButton", background=p["danger_bg"], foreground=p["danger_text"],
            bordercolor=p["danger_border"], padding=(14, 8), relief="flat",
        )
        style.map(
            "Danger.TButton",
            background=[
                ("disabled", p["danger_disabled"]),
                ("pressed", p["danger_pressed"]),
                ("active", p["danger_active"]),
            ],
            foreground=[("disabled", p["disabled_text"])],
        )
        style.configure(
            "TProgressbar", troughcolor=p["progress_track"], background=p["accent"],
            bordercolor=p["progress_track"],
        )
        style.configure(
            "TScrollbar", background=p["scrollbar"], troughcolor=p["app"],
            bordercolor=p["app"], arrowsize=12, arrowcolor=p["text"],
        )

    def _recolor_widget(self, widget) -> None:
        p = self.palette
        try:
            if isinstance(widget, (tk.Tk, tk.Toplevel)):
                widget.configure(bg=p["app"])
            elif isinstance(widget, tk.Canvas):
                widget.configure(bg=p["app"], highlightbackground=p["app"])
            elif isinstance(widget, tk.Text):
                widget.configure(
                    bg=p["white"], fg=p["text"], insertbackground=p["text"],
                    selectbackground=p["selection"], highlightbackground=p["border"],
                    highlightcolor=p["accent"],
                )
            elif isinstance(widget, (tk.Frame, tk.LabelFrame)):
                widget.configure(bg=p["panel"])
            elif isinstance(widget, tk.Entry):
                widget.configure(
                    bg=p["white"], fg=p["text"], insertbackground=p["text"],
                    selectbackground=p["selection"], disabledbackground=p["panel"],
                    disabledforeground=p["disabled_text"], highlightbackground=p["border"],
                )
            elif isinstance(widget, tk.Listbox):
                widget.configure(
                    bg=p["white"], fg=p["text"], selectbackground=p["selection"],
                    selectforeground=p["text"], highlightbackground=p["border"],
                )
            elif isinstance(widget, (tk.Label, tk.Button, tk.Checkbutton, tk.Radiobutton, tk.Spinbox, tk.Menubutton)):
                widget.configure(
                    bg=p["panel"], fg=p["text"], activebackground=p["button_active"],
                    activeforeground=p["text"], disabledforeground=p["disabled_text"],
                    highlightbackground=p["border"],
                )
            for child in widget.winfo_children():
                self._recolor_widget(child)
        except (tk.TclError, AttributeError):
            pass

    def _update_button_text(self) -> None:
        if self.theme_button is None:
            return
        try:
            self.theme_button.configure(
                text="☀ Светлая тема" if self.current == "dark" else "☾ Тёмная тема"
            )
        except tk.TclError:
            pass

    def apply(
        self,
        theme: str = "light",
        *,
        recolor: bool = True,
        on_change: Callable[[], None] | None = None,
    ) -> None:
        selected = "dark" if str(theme or "").strip().lower() in {
            "dark", "night", "тёмная", "темная",
        } else "light"
        self.current = selected
        self.palette.clear()
        self.palette.update(THEME_PALETTES[selected])
        try:
            self.root.configure(bg=self.palette["app"])
        except tk.TclError:
            pass
        self.configure_styles()
        if recolor:
            self._recolor_widget(self.root)
        self._update_button_text()
        if on_change is not None:
            on_change()

    def toggle(self, on_change: Callable[[], None] | None = None) -> None:
        self.apply(
            "dark" if self.current == "light" else "light",
            on_change=on_change,
        )
