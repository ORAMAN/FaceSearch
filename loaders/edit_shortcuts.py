"""Унифицированные горячие клавиши и контекстное меню редактирования Tkinter.

Подключается к корневому окну и автоматически работает в существующих и
создаваемых позднее Entry/Text/Spinbox/Combobox, включая ttk-виджеты.
"""

from __future__ import annotations

import tkinter as tk
from tkinter import ttk


_EDITABLE_CLASSES = (
    tk.Entry,
    tk.Text,
    tk.Spinbox,
    ttk.Entry,
    ttk.Spinbox,
    ttk.Combobox,
)

# Физические VK-коды клавиш помогают распознать Ctrl+C/V/X/A при русской
# раскладке Windows, когда Tk возвращает Cyrillic_* вместо латинских keysyms.
_KEYCODE_ACTIONS = {
    67: "copy",        # C
    86: "paste",       # V
    88: "cut",         # X
    65: "select_all",  # A
    90: "undo",        # Z
    89: "redo",        # Y
}
_KEYSYM_ACTIONS = {
    "c": "copy", "cyrillic_es": "copy",
    "v": "paste", "cyrillic_em": "paste",
    "x": "cut", "cyrillic_che": "cut",
    "a": "select_all", "cyrillic_ef": "select_all",
    "z": "undo", "cyrillic_ya": "undo",
    "y": "redo", "cyrillic_en": "redo",
}
_CHAR_ACTIONS = {
    "c": "copy", "с": "copy",
    "v": "paste", "м": "paste",
    "x": "cut", "ч": "cut",
    "a": "select_all", "ф": "select_all",
    "z": "undo", "я": "undo",
    "y": "redo", "н": "redo",
}
_CYRILLIC_CHARS = set("смчфян")


def _is_editable_widget(widget) -> bool:
    return isinstance(widget, _EDITABLE_CLASSES)


def _is_text_widget(widget) -> bool:
    return isinstance(widget, tk.Text)


def _can_edit(widget) -> bool:
    try:
        state = str(widget.cget("state")).strip().lower()
    except (AttributeError, tk.TclError):
        state = "normal"
    return state not in {"disabled", "readonly"}


def _has_selection(widget) -> bool:
    try:
        if _is_text_widget(widget):
            return bool(widget.tag_ranges("sel"))
        return bool(widget.selection_present())
    except (AttributeError, tk.TclError):
        try:
            widget.index("sel.first")
            widget.index("sel.last")
            return True
        except (AttributeError, tk.TclError):
            return False


def _selected_text(widget) -> str:
    if _is_text_widget(widget):
        return widget.get("sel.first", "sel.last")
    start = widget.index("sel.first")
    end = widget.index("sel.last")
    return widget.get()[start:end]


def _delete_selection(widget) -> bool:
    if not _has_selection(widget):
        return False
    if _is_text_widget(widget):
        widget.delete("sel.first", "sel.last")
    else:
        start = widget.index("sel.first")
        end = widget.index("sel.last")
        widget.delete(start, end)
    return True


def _select_all(widget) -> None:
    if _is_text_widget(widget):
        widget.tag_add("sel", "1.0", "end-1c")
        widget.mark_set("insert", "end-1c")
        widget.see("insert")
    else:
        widget.selection_range(0, "end")
        widget.icursor("end")
        widget.xview("end")


def _copy(root: tk.Misc, widget) -> str:
    try:
        if _has_selection(widget):
            root.clipboard_clear()
            root.clipboard_append(_selected_text(widget))
    except (tk.TclError, AttributeError):
        pass
    return "break"


def _cut(root: tk.Misc, widget) -> str:
    if not _can_edit(widget):
        return "break"
    _copy(root, widget)
    try:
        _delete_selection(widget)
    except (tk.TclError, AttributeError):
        pass
    return "break"


def _paste(root: tk.Misc, widget) -> str:
    if not _can_edit(widget):
        return "break"
    try:
        value = root.clipboard_get()
        _delete_selection(widget)
        widget.insert("insert", value)
        if not _is_text_widget(widget):
            widget.xview("insert")
    except (tk.TclError, AttributeError):
        pass
    return "break"


def install_edit_shortcuts(root: tk.Misc) -> bool:
    """Install layout-aware Ctrl shortcuts and an edit menu on one Tk root.

    Returns False when already installed. Menus and shortcuts are scoped to
    supported text-editable widgets so unrelated controls keep their behavior.
    """
    if getattr(root, "_facesearch_edit_shortcuts_installed", False):
        return False
    try:
        root._facesearch_edit_shortcuts_installed = True
    except Exception:
        pass

    active = {"widget": None}
    menu = tk.Menu(root, tearoff=False)
    menu.add_command(label="Вырезать", command=lambda: _cut(root, active["widget"]) if _is_editable_widget(active["widget"]) else None)
    menu.add_command(label="Копировать", command=lambda: _copy(root, active["widget"]) if _is_editable_widget(active["widget"]) else None)
    menu.add_command(label="Вставить", command=lambda: _paste(root, active["widget"]) if _is_editable_widget(active["widget"]) else None)
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=lambda: _select_active())
    menu.add_separator()
    menu.add_command(label="Отменить", command=lambda: _edit_history("undo"))
    menu.add_command(label="Повторить", command=lambda: _edit_history("redo"))

    def _select_active():
        widget = active.get("widget")
        if _is_editable_widget(widget):
            try:
                _select_all(widget)
            except (tk.TclError, AttributeError):
                pass

    def _edit_history(action: str):
        widget = active.get("widget")
        if not _is_text_widget(widget) or not _can_edit(widget):
            return
        try:
            widget.edit_undo() if action == "undo" else widget.edit_redo()
        except tk.TclError:
            pass

    def _refresh_menu(widget):
        selected = _has_selection(widget)
        editable = _can_edit(widget)
        menu.entryconfigure(0, state="normal" if selected and editable else "disabled")
        menu.entryconfigure(1, state="normal" if selected else "disabled")
        try:
            root.clipboard_get()
            clipboard_has_text = True
        except tk.TclError:
            clipboard_has_text = False
        menu.entryconfigure(2, state="normal" if editable and clipboard_has_text else "disabled")
        try:
            has_text = bool(widget.get("1.0", "end-1c")) if _is_text_widget(widget) else bool(widget.get())
        except (tk.TclError, AttributeError):
            has_text = True
        menu.entryconfigure(4, state="normal" if has_text else "disabled")
        history_state = "normal" if _is_text_widget(widget) and editable else "disabled"
        menu.entryconfigure(6, state=history_state)
        menu.entryconfigure(7, state=history_state)

    def _show_menu(event=None, keyboard=False):
        widget = getattr(event, "widget", None) if event is not None else None
        if not _is_editable_widget(widget):
            try:
                widget = root.focus_get()
            except tk.TclError:
                widget = None
        if not _is_editable_widget(widget):
            return None
        active["widget"] = widget
        try:
            widget.focus_set()
            _refresh_menu(widget)
            if keyboard:
                x = widget.winfo_rootx() + min(max(widget.winfo_width() // 2, 1), 40)
                y = widget.winfo_rooty() + widget.winfo_height()
            else:
                x, y = event.x_root, event.y_root
            menu.tk_popup(x, y)
        except tk.TclError:
            return "break"
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def _handle_shortcut(event):
        widget = getattr(event, "widget", None)
        if not _is_editable_widget(widget):
            return None

        keysym = str(getattr(event, "keysym", "") or "").lower()
        char = str(getattr(event, "char", "") or "").lower()
        try:
            keycode = int(getattr(event, "keycode", 0) or 0)
        except (TypeError, ValueError):
            keycode = 0
        action = _KEYSYM_ACTIONS.get(keysym) or _CHAR_ACTIONS.get(char) or _KEYCODE_ACTIONS.get(keycode)
        if not action:
            return None

        is_cyrillic = keysym.startswith("cyrillic_") or char in _CYRILLIC_CHARS
        if action in {"copy", "cut", "paste"} and not is_cyrillic:
            # Tk's standard class binding has already performed the English-key
            # operation before bind_all. Do not repeat it (especially for paste).
            return "break"

        if action == "select_all":
            try:
                _select_all(widget)
            except (tk.TclError, AttributeError):
                pass
            return "break"

        if action == "copy":
            return _copy(root, widget)
        if action == "cut":
            return _cut(root, widget)
        if action == "paste":
            return _paste(root, widget)
        if action in {"undo", "redo"} and _is_text_widget(widget) and _can_edit(widget):
            try:
                widget.edit_undo() if action == "undo" else widget.edit_redo()
            except tk.TclError:
                pass
            return "break"
        return None

    # A global sequence handler also covers controls created after installation.
    root.bind_all("<Control-KeyPress>", _handle_shortcut, add="+")
    root.bind_all("<Button-3>", _show_menu, add="+")
    root.bind_all("<Shift-F10>", lambda event: _show_menu(event, keyboard=True), add="+")
    root.bind_all("<Menu>", lambda event: _show_menu(event, keyboard=True), add="+")
    try:
        root._facesearch_edit_context_menu = menu
    except Exception:
        pass
    return True
