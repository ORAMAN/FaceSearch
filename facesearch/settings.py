# -*- coding: utf-8 -*-
"""Чтение и сохранение пользовательских настроек GUI (settings.xml)."""
from __future__ import annotations
import xml.etree.ElementTree as ET
from typing import Any
from .config import (
    APP_DIR, SETTINGS_FILE, LOCAL_DEFAULT_WORKERS, LOCAL_MAX_WORKERS,
    AUTO_WORKERS_DEFAULT, DEFAULT_TOP_K,
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


def _load_gui_settings() -> dict[str, Any]:
    settings = {
        "window_geometry": "1250x900",
        "window_maximized": True,
        "theme": "light",
        "paned_sash_position": 0,
        "paned_preview_sash_position": 0,
        "selected_db_profile": "",
        "selected_folder": "",
        "query_image": "",
        "backend": "",
        "workers": LOCAL_DEFAULT_WORKERS,
        "auto_workers": AUTO_WORKERS_DEFAULT,
        "delete_processed": False,
        "use_json_metadata": True,
        "create_crop_files": True,
        "overwrite_existing_crops": False,
        "use_local_crops": True,
        "create_crops_on_html": False,
        "top_k": DEFAULT_TOP_K,
        "min_similarity": 43.0,
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
            settings["paned_preview_sash_position"] = max(
                0, _xml_int(node.findtext("paned_preview_sash_position"), 0)
            )

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
    ET.SubElement(node, "paned_preview_sash_position").text = str(
        max(0, _xml_int(settings.get("paned_preview_sash_position"), 0))
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
