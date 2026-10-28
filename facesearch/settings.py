# -*- coding: utf-8 -*-
"""Чтение и сохранение пользовательских настроек GUI (settings.xml)."""
from __future__ import annotations
import json
import threading
import xml.etree.ElementTree as ET
from pathlib import Path
from typing import Any, Iterable
from .config import (
    APP_DIR, SETTINGS_FILE, LOCAL_DEFAULT_WORKERS, LOCAL_MAX_WORKERS,
    AUTO_WORKERS_DEFAULT, DEFAULT_TOP_K, DEFAULT_SCAN_BATCH_SIZE, MAX_SCAN_BATCH_SIZE,
)

def _xml_bool(value: Any, default: bool = False) -> bool:
    if value is None:
        return default
    return str(value).strip().lower() in ("1", "true", "yes", "on", "да")


def _xml_int(value: Any, default: int) -> int:
    try:
        return int(str(value).strip())
    except (TypeError, ValueError):
        return default


def _xml_float(value: Any, default: float) -> float:
    try:
        return float(str(value).strip())
    except (TypeError, ValueError):
        return default


def _coerce_module_setting(value: Any, default: Any) -> Any:
    """Преобразует значение в тип, заданный значением по умолчанию."""
    if isinstance(default, bool):
        return _xml_bool(value, default)
    if isinstance(default, int) and not isinstance(default, bool):
        return _xml_int(value, default)
    if isinstance(default, float):
        return _xml_float(value, default)
    if isinstance(default, dict):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return dict(default)
        if not isinstance(value, dict):
            return dict(default)
        result = dict(default)
        for key, item in value.items():
            if key in default:
                result[key] = _coerce_module_setting(item, default[key])
            else:
                result[key] = item
        return result
    if isinstance(default, (list, tuple)):
        if isinstance(value, str):
            try:
                value = json.loads(value)
            except (TypeError, ValueError):
                return type(default)()
        if not isinstance(value, (list, tuple)):
            return type(default)()
        converted = list(value)
        if default:
            converted = [
                _coerce_module_setting(item, default[min(i, len(default) - 1)])
                for i, item in enumerate(converted)
            ]
        return tuple(converted) if isinstance(default, tuple) else converted
    if isinstance(default, str):
        return "" if value is None else str(value)
    return default if value is None else value


def _write_module_xml_value(node: ET.Element, value: Any) -> None:
    """Записывает простые и составные значения в типизированный XML-узел."""
    if isinstance(value, dict):
        node.set("type", "dict")
        for key, item_value in value.items():
            child = ET.SubElement(node, "item", {"key": str(key)})
            _write_module_xml_value(child, item_value)
    elif isinstance(value, (list, tuple)):
        node.set("type", "list")
        for item_value in value:
            child = ET.SubElement(node, "item")
            _write_module_xml_value(child, item_value)
    elif isinstance(value, bool):
        node.set("type", "bool")
        node.text = "true" if value else "false"
    elif isinstance(value, int):
        node.set("type", "int")
        node.text = str(value)
    elif isinstance(value, float):
        node.set("type", "float")
        node.text = repr(value)
    elif value is None:
        node.set("type", "none")
        node.text = ""
    else:
        node.set("type", "str")
        node.text = str(value)


def _read_module_xml_value(node: ET.Element, default: Any) -> Any:
    """Читает типизированный XML-узел и поддерживает поля из раннего XML-формата."""
    kind = node.get("type", "")
    children = list(node)
    if kind == "dict" or isinstance(default, dict):
        if children:
            parsed = {}
            for child in children:
                key = child.get("key") or child.tag
                expected = default.get(key) if isinstance(default, dict) else None
                parsed[key] = _read_module_xml_value(child, expected)
            return _coerce_module_setting(parsed, default)
        if node.text:
            return _coerce_module_setting(node.text, default)
        return dict(default) if isinstance(default, dict) else {}
    if kind == "list" or isinstance(default, (list, tuple)):
        if children:
            parsed = [_read_module_xml_value(child, None) for child in children]
            return _coerce_module_setting(parsed, default)
        if node.text:
            return _coerce_module_setting(node.text, default)
        return type(default)() if isinstance(default, (list, tuple)) else []
    raw = node.text or ""
    if kind == "bool":
        return _xml_bool(raw, bool(default) if isinstance(default, bool) else False)
    if kind == "int":
        return _xml_int(raw, int(default) if isinstance(default, int) and not isinstance(default, bool) else 0)
    if kind == "float":
        return _xml_float(raw, float(default) if isinstance(default, float) else 0.0)
    if kind == "none":
        return None
    return _coerce_module_setting(raw, default)


_MODULE_SETTINGS_WRITE_LOCK = threading.RLock()


def save_module_settings(
    path: str | Path,
    module_name: str,
    settings: dict[str, Any],
    *,
    window_keys: set[str] | frozenset[str] = frozenset({"geometry", "maximized", "theme"}),
) -> None:
    """Сохраняет настройки утилиты в общем XML-формате FaceSearch."""
    settings_path = Path(path)
    settings_path.parent.mkdir(parents=True, exist_ok=True)
    root = ET.Element("settings", {"version": "1", "module": str(module_name)})

    window = ET.SubElement(root, "window")
    form = ET.SubElement(root, "form")
    for key, value in settings.items():
        parent = window if key in window_keys else form
        node = ET.SubElement(parent, str(key))
        _write_module_xml_value(node, value)

    tree = ET.ElementTree(root)
    try:
        ET.indent(tree, space="    ")
    except AttributeError:
        pass

    # У каждого потока свой временный файл: в VK смещение поиска может
    # сохраняться из worker-потока одновременно с настройками окна.
    temporary = settings_path.with_name(
        f"{settings_path.name}.{threading.get_ident()}.tmp"
    )
    with _MODULE_SETTINGS_WRITE_LOCK:
        try:
            tree.write(temporary, encoding="utf-8", xml_declaration=True)
            temporary.replace(settings_path)
        finally:
            try:
                temporary.unlink(missing_ok=True)
            except OSError:
                pass


def load_module_settings(
    path: str | Path,
    module_name: str,
    defaults: dict[str, Any],
    *,
    window_keys: set[str] | frozenset[str] = frozenset({"geometry", "maximized", "theme"}),
    legacy_json_paths: Iterable[str | Path] = (),
) -> dict[str, Any]:
    """Загружает XML-настройки утилиты и однократно мигрирует прежние JSON-файлы."""
    settings_path = Path(path)
    legacy_paths = []
    for item in legacy_json_paths:
        candidate = Path(item)
        if candidate != settings_path and candidate not in legacy_paths:
            legacy_paths.append(candidate)

    stored: dict[str, Any] = {}
    loaded_xml = False
    if settings_path.is_file():
        try:
            root = ET.parse(settings_path).getroot()
            root_module = root.get("module")
            if root.tag == "settings" and root_module in (None, str(module_name)):
                for section_name in ("window", "form"):
                    node = root.find(section_name)
                    if node is None:
                        continue
                    for item in list(node):
                        if item.tag in defaults:
                            stored[item.tag] = _read_module_xml_value(item, defaults[item.tag])
                loaded_xml = True
        except (ET.ParseError, OSError, ValueError):
            loaded_xml = False

    legacy_source: Path | None = None
    if not loaded_xml:
        for candidate in legacy_paths:
            if not candidate.is_file():
                continue
            try:
                payload = json.loads(candidate.read_text(encoding="utf-8"))
            except (OSError, ValueError, TypeError):
                continue
            if isinstance(payload, dict):
                stored = {key: value for key, value in payload.items() if key in defaults}
                legacy_source = candidate
                break

    result = dict(defaults)
    for key, default in defaults.items():
        if key in stored:
            result[key] = _coerce_module_setting(stored[key], default)

    # JSON удаляется только после успешного создания XML; при ошибке исходник остаётся.
    if legacy_source is not None:
        try:
            save_module_settings(settings_path, module_name, result, window_keys=window_keys)
        except OSError:
            return result
        for candidate in legacy_paths:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass
    elif loaded_xml:
        # При валидном XML удаляем устаревшие копии от предыдущих версий утилит.
        for candidate in legacy_paths:
            try:
                candidate.unlink(missing_ok=True)
            except OSError:
                pass

    return result


# Новые установки по умолчанию сохраняют прежний вид интерфейса (все группы раскрыты).
COLLAPSIBLE_SECTION_DEFAULTS = {
    "source_images": True,
    "database": True,
    "compute": True,
    "processing": True,
}


def _load_gui_settings() -> dict[str, Any]:
    settings = {
        "window_geometry": "1250x900",
        "window_maximized": True,
        "theme": "light",
        "paned_sash_position": 0,
        "paned_stats_hidden": False,
        "paned_preview_sash_position": 0,
        "selected_db_profile": "",
        "selected_folder": "",
        "query_image": "",
        "backend": "",
        "workers": LOCAL_DEFAULT_WORKERS,
        "scan_batch_size": DEFAULT_SCAN_BATCH_SIZE,
        "auto_workers": AUTO_WORKERS_DEFAULT,
        "delete_processed": False,
        "use_json_metadata": True,
        "create_crop_files": True,
        "overwrite_existing_crops": False,
        "use_local_crops": True,
        "create_crops_on_html": False,
        "top_k": DEFAULT_TOP_K,
        "min_similarity": 43.0,
        "collapsible_sections": dict(COLLAPSIBLE_SECTION_DEFAULTS),
    }

    if not SETTINGS_FILE.is_file():
        return settings

    try:
        root = ET.parse(SETTINGS_FILE).getroot()

        node = root.find("window")
        if node is not None:
            value = node.findtext("geometry")
            if value:
                settings["window_geometry"] = value.strip()
            value = node.findtext("maximized")
            if value is not None:
                settings["window_maximized"] = _xml_bool(
                    value, settings["window_maximized"]
                )
            value = node.findtext("theme")
            if value is not None:
                settings["theme"] = "dark" if value.strip().lower() == "dark" else "light"
            settings["paned_sash_position"] = max(
                0, _xml_int(node.findtext("paned_sash_position"), 0)
            )
            settings["paned_stats_hidden"] = _xml_bool(
                node.findtext("paned_stats_hidden"),
                settings["paned_stats_hidden"],
            )
            settings["paned_preview_sash_position"] = max(
                0, _xml_int(node.findtext("paned_preview_sash_position"), 0)
            )

        node = root.find("sections")
        if node is not None:
            section_states = dict(COLLAPSIBLE_SECTION_DEFAULTS)
            for key, default in COLLAPSIBLE_SECTION_DEFAULTS.items():
                value = node.findtext(key)
                if value is not None:
                    section_states[key] = _xml_bool(value, default)
            settings["collapsible_sections"] = section_states

        node = root.find("database")
        if node is not None:
            value = node.findtext("profile")
            if value:
                settings["selected_db_profile"] = value.strip()

        node = root.find("scan")
        if node is not None:
            value = node.findtext("folder")
            if value is not None:
                settings["selected_folder"] = value.strip()
            settings["workers"] = max(
                1,
                min(
                    LOCAL_MAX_WORKERS,
                    _xml_int(node.findtext("workers"), LOCAL_DEFAULT_WORKERS),
                ),
            )
            settings["scan_batch_size"] = max(
                0,
                min(
                    MAX_SCAN_BATCH_SIZE,
                    _xml_int(node.findtext("batch_size"), DEFAULT_SCAN_BATCH_SIZE),
                ),
            )
            settings["auto_workers"] = _xml_bool(
                node.findtext("auto_workers"),
                settings["auto_workers"],
            )
            settings["delete_processed"] = _xml_bool(
                node.findtext("delete_processed"),
                settings["delete_processed"],
            )
            settings["use_json_metadata"] = _xml_bool(
                node.findtext("use_json_metadata"),
                settings["use_json_metadata"],
            )
            settings["create_crop_files"] = _xml_bool(
                node.findtext("create_crop_files"),
                settings["create_crop_files"],
            )
            settings["overwrite_existing_crops"] = _xml_bool(
                node.findtext("overwrite_existing_crops"),
                settings["overwrite_existing_crops"],
            )

        node = root.find("search")
        if node is not None:
            value = node.findtext("query_image")
            if value is not None:
                settings["query_image"] = value.strip()

            settings["top_k"] = max(
                1,
                min(500, _xml_int(node.findtext("top_k"), DEFAULT_TOP_K)),
            )
            settings["min_similarity"] = max(
                0.0,
                min(
                    100.0,
                    _xml_float(node.findtext("min_similarity"), 43.0),
                ),
            )
            settings["use_local_crops"] = _xml_bool(
                node.findtext("use_local_crops"),
                settings["use_local_crops"],
            )
            settings["create_crops_on_html"] = _xml_bool(
                node.findtext("create_crops_on_html"),
                settings["create_crops_on_html"],
            )

        node = root.find("compute")
        if node is not None:
            value = node.findtext("backend")
            if value:
                settings["backend"] = value.strip().lower()

    except Exception as exc:
        print(f"Предупреждение: не удалось прочитать {SETTINGS_FILE}: {exc}")

    return settings


def _save_gui_settings_data(settings: dict[str, Any]) -> None:
    root = ET.Element("settings", {"version": "1"})

    node = ET.SubElement(root, "window")
    ET.SubElement(node, "geometry").text = str(
        settings.get("window_geometry") or "1250x900"
    )
    ET.SubElement(node, "maximized").text = (
        "true" if bool(settings.get("window_maximized")) else "false"
    )
    theme = str(settings.get("theme") or "light").strip().lower()
    ET.SubElement(node, "theme").text = "dark" if theme == "dark" else "light"
    ET.SubElement(node, "paned_sash_position").text = str(
        max(0, _xml_int(settings.get("paned_sash_position"), 0))
    )
    ET.SubElement(node, "paned_stats_hidden").text = (
        "true" if bool(settings.get("paned_stats_hidden", False)) else "false"
    )
    ET.SubElement(node, "paned_preview_sash_position").text = str(
        max(0, _xml_int(settings.get("paned_preview_sash_position"), 0))
    )

    node = ET.SubElement(root, "sections")
    section_states = settings.get("collapsible_sections", {})
    if not isinstance(section_states, dict):
        section_states = {}
    for key, default in COLLAPSIBLE_SECTION_DEFAULTS.items():
        ET.SubElement(node, key).text = (
            "true" if bool(section_states.get(key, default)) else "false"
        )

    node = ET.SubElement(root, "database")
    ET.SubElement(node, "profile").text = str(
        settings.get("selected_db_profile") or ""
    )

    node = ET.SubElement(root, "scan")
    ET.SubElement(node, "folder").text = str(
        settings.get("selected_folder") or ""
    )
    ET.SubElement(node, "workers").text = str(
        int(settings.get("workers") or LOCAL_DEFAULT_WORKERS)
    )
    try:
        scan_batch_size = int(str(settings.get("scan_batch_size", DEFAULT_SCAN_BATCH_SIZE)).strip() or 0)
    except (TypeError, ValueError):
        scan_batch_size = DEFAULT_SCAN_BATCH_SIZE
    scan_batch_size = max(0, min(MAX_SCAN_BATCH_SIZE, scan_batch_size))
    ET.SubElement(node, "batch_size").text = str(scan_batch_size)
    ET.SubElement(node, "auto_workers").text = (
        "true" if bool(settings.get("auto_workers")) else "false"
    )
    ET.SubElement(node, "delete_processed").text = (
        "true" if bool(settings.get("delete_processed")) else "false"
    )
    ET.SubElement(node, "use_json_metadata").text = (
        "true" if bool(settings.get("use_json_metadata")) else "false"
    )
    ET.SubElement(node, "create_crop_files").text = (
        "true" if bool(settings.get("create_crop_files", True)) else "false"
    )
    ET.SubElement(node, "overwrite_existing_crops").text = (
        "true" if bool(settings.get("overwrite_existing_crops", False)) else "false"
    )

    node = ET.SubElement(root, "search")
    ET.SubElement(node, "query_image").text = str(
        settings.get("query_image") or ""
    )
    ET.SubElement(node, "top_k").text = str(
        int(settings.get("top_k") or DEFAULT_TOP_K)
    )
    ET.SubElement(node, "min_similarity").text = str(
        float(settings.get("min_similarity") or 0.0)
    )
    ET.SubElement(node, "use_local_crops").text = (
        "true" if bool(settings.get("use_local_crops")) else "false"
    )
    ET.SubElement(node, "create_crops_on_html").text = (
        "true" if bool(settings.get("create_crops_on_html", False)) else "false"
    )

    node = ET.SubElement(root, "compute")
    ET.SubElement(node, "backend").text = str(
        settings.get("backend") or ""
    )

    tree = ET.ElementTree(root)
    try:
        ET.indent(tree, space="    ")
    except AttributeError:
        pass

    tmp = SETTINGS_FILE.with_suffix(".tmp")
    tree.write(tmp, encoding="utf-8", xml_declaration=True)
    try:
        tmp.replace(SETTINGS_FILE)
    except PermissionError:
        SETTINGS_FILE.unlink(missing_ok=True)
        tmp.replace(SETTINGS_FILE)
