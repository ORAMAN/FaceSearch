# -*- coding: utf-8 -*-
"""Профили подключений БД и раздельные пути FAISS/CROP для каждого профиля."""
from __future__ import annotations
import hashlib
import json
import re
from pathlib import Path
from urllib.parse import urlparse
from typing import Any
from .config import APP_DIR, DB_CONFIG_FILE, DB_TYPE_SQLITE, DEFAULT_DB_PROFILE_NAME, DB_DEFAULT_PORTS

def _profile_storage_slug(profile: dict[str, Any]) -> str:
    """Стабильный идентификатор хранилища FAISS/CROP для конкретной БД."""
    backend = str(profile.get("type") or "db").strip().lower()

    if backend == DB_TYPE_SQLITE:
        db_path = Path(
            str(profile.get("path") or APP_DIR / "faces.db")
        ).expanduser().resolve()
        stem = re.sub(r"[^A-Za-z0-9_.-]+", "_", db_path.stem).strip("._-") or "database"
        # Один и тот же stem может находиться по разным путям, поэтому
        # добавляем короткий hash полного пути.
        path_hash = hashlib.sha1(
            str(db_path).encode("utf-8", errors="replace")
        ).hexdigest()[:8]
        return f"sqlite_{stem}_{path_hash}"

    host = str(profile.get("host") or "localhost").strip().lower()
    port = str(profile.get("port") or DB_DEFAULT_PORTS.get(backend, "")).strip()
    database = str(profile.get("database") or "database").strip().lower()

    raw = f"{backend}_{host}_{port}_{database}"
    raw = re.sub(r"[^A-Za-z0-9_.-]+", "_", raw)
    raw = raw.strip("._-") or "database"
    return raw[:180]


def _profile_storage_paths(profile: dict[str, Any]) -> tuple[str, str]:
    """Возвращает единые пути индекса и CROP для любого типа БД."""
    slug = _profile_storage_slug(profile)
    return (
        str(APP_DIR / "indexes" / f"{slug}.faiss"),
        str(APP_DIR / "face_images" / slug),
    )


def _normalize_profile_storage_paths(profile: dict[str, Any]) -> dict[str, Any]:
    """
    Гарантирует наличие отдельных FAISS/CROP для каждой БД, но не
    перезаписывает пользовательские пути.

    Автоматические пути подставляются только если соответствующее поле
    пустое. Поэтому значения, заданные пользователем в редакторе профиля,
    сохраняются в database_profiles.json и повторно загружаются без замены.
    """
    result = dict(profile)
    default_index_path, default_crop_path = _profile_storage_paths(result)

    # Совместимость с ошибочно сохранёнными профилями, где URL ранее
    # мог быть указан в поле локального каталога CROP.
    crop_path_value = str(result.get("crop_path") or "").strip()
    parsed_crop_path = urlparse(crop_path_value)
    if parsed_crop_path.scheme.lower() in {"http", "https"} and parsed_crop_path.netloc:
        if not str(result.get("crop_url") or "").strip():
            result["crop_url"] = crop_path_value.rstrip("/")
        result["crop_path"] = default_crop_path

    if not str(result.get("index_path") or "").strip():
        result["index_path"] = default_index_path

    if not str(result.get("crop_path") or "").strip():
        result["crop_path"] = default_crop_path

    # URL используется только браузером для чтения CROP-файлов.
    # Запись и восстановление всегда направляются в crop_path.
    crop_url = str(result.get("crop_url") or "").strip()
    if crop_url:
        parsed_crop_url = urlparse(crop_url)
        if parsed_crop_url.scheme.lower() not in {"http", "https"} or not parsed_crop_url.netloc:
            # Не ломаем загрузку уже существующих профилей из-за старого
            # ошибочного значения. В GUI новое неверное значение отдельно
            # блокируется проверкой collect_profile().
            crop_url = ""
        else:
            crop_url = crop_url.rstrip("/")
    result["crop_url"] = crop_url

    return result


# Активные пути. Они изменяются при выборе профиля БД.
DB_FILE = str(APP_DIR / "faces.db")
INDEX_FILE = str(APP_DIR / "indexes" / "legacy_unselected.faiss")
CROP_DIR = APP_DIR / "face_images" / "legacy_unselected"


def _default_profile() -> dict[str, Any]:
    db_path = APP_DIR / "faces.db"
    return {
        "name": DEFAULT_DB_PROFILE_NAME,
        "type": DB_TYPE_SQLITE,
        "path": str(db_path),
        "database": db_path.stem,
        "host": "",
        "port": "",
        "user": "",
        "password": "",
        "driver": "",
        "index_path": str(
            APP_DIR / "indexes" / "sqlite_faces_pending.faiss"
        ),
        "crop_path": str(
            APP_DIR / "face_images" / "sqlite_faces_pending"
        ),
        "crop_url": "",
    }


def _load_db_profiles() -> tuple[list[dict[str, Any]], str]:
    default = _default_profile()
    try:
        if DB_CONFIG_FILE.is_file():
            data = json.loads(DB_CONFIG_FILE.read_text(encoding="utf-8"))
            profiles = data.get("profiles", [])
            active = str(data.get("active") or "")
            if isinstance(profiles, list):
                valid = [p for p in profiles if isinstance(p, dict) and p.get("name")]
                if valid:
                    names = {str(p["name"]) for p in valid}
                    if active not in names:
                        active = str(valid[0]["name"])

                    normalized = [
                        _normalize_profile_storage_paths(dict(p))
                        for p in valid
                    ]
                    if normalized != valid:
                        _save_db_profiles(normalized, active)
                    return normalized, active
    except Exception as exc:
        print(f"Предупреждение: не удалось прочитать {DB_CONFIG_FILE}: {exc}")

    return [default], default["name"]


def _save_db_profiles(profiles: list[dict[str, Any]], active: str) -> None:
    payload = {
        "active": active,
        "profiles": profiles,
    }
    tmp = DB_CONFIG_FILE.with_suffix(".tmp")
    tmp.write_text(
        json.dumps(payload, ensure_ascii=False, indent=2),
        encoding="utf-8",
    )
    try:
        tmp.replace(DB_CONFIG_FILE)
    except PermissionError:
        # Windows: если антивирус/индексация держит файл, удаление старого
        # варианта иногда проходит надёжнее, чем replace.
        try:
            DB_CONFIG_FILE.unlink(missing_ok=True)
            tmp.replace(DB_CONFIG_FILE)
        except Exception:
            print(f"Не удалось сохранить конфигурацию БД: {DB_CONFIG_FILE}")
            raise
