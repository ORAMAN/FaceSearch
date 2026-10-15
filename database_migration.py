# -*- coding: utf-8 -*-
"""Отдельное окно миграции/слияния баз Face Search.

Можно запускать напрямую: python database_migration.py
Или открывать из основного GUI. Тяжёлый модуль приложения импортируется
только в worker-потоке после нажатия кнопки переноса.
"""
from __future__ import annotations

import hashlib
import json
import os
import queue
import shutil
import sqlite3
import subprocess
import sys
import tempfile
import threading
import time
import traceback
from pathlib import Path
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

APP_DIR = Path(__file__).resolve().parent
PROFILE_FILE = APP_DIR / "database_profiles.json"
BATCH_IMAGES = 250       # не превышаем лимиты параметров SQL Server / SQLite
BATCH_FACES = 2500

DB_LABELS = {
    "SQLite": "sqlite",
    "PostgreSQL": "postgresql",
    "MySQL": "mysql",
    "Microsoft SQL Server": "mssql",
}
DB_LABELS_REVERSE = {v: k for k, v in DB_LABELS.items()}
DEFAULT_PORTS = {"postgresql": 5432, "mysql": 3306, "mssql": 1433}


def _load_profiles_light() -> tuple[list[dict], str]:
    """Читает только JSON профилей — без импорта NumPy/FAISS/InsightFace."""
    try:
        payload = json.loads(PROFILE_FILE.read_text(encoding="utf-8"))
        profiles = [p for p in payload.get("profiles", []) if isinstance(p, dict) and p.get("name")]
        if profiles:
            names = {str(p["name"]) for p in profiles}
            active = str(payload.get("active") or "")
            if active not in names:
                active = str(profiles[0]["name"])
            return profiles, active
    except Exception:
        pass
    default_db = APP_DIR / "faces.db"
    return ([{
        "name": "SQLite — faces", "type": "sqlite", "path": str(default_db),
        "database": default_db.stem, "host": "", "port": "", "user": "",
        "password": "", "driver": "",
        "index_path": str(APP_DIR / "indexes" / "sqlite_faces_pending.faiss"),
        "crop_path": str(APP_DIR / "face_images" / "sqlite_faces_pending"),
    }], "SQLite — faces")


def _profile_description(profile: dict) -> str:
    backend = str(profile.get("type") or "sqlite").lower()
    name = str(profile.get("name") or "")
    if backend == "sqlite":
        return f"{name}  [{DB_LABELS_REVERSE.get(backend, backend)}: {profile.get('path', '')}]"
    return f"{name}  [{DB_LABELS_REVERSE.get(backend, backend)}: {profile.get('host', '')}:{profile.get('port', '')}/{profile.get('database', '')}]"


def _hash_file(path: Path) -> str | None:
    try:
        digest = hashlib.sha256()
        with path.open("rb") as f:
            for block in iter(lambda: f.read(1024 * 1024), b""):
                digest.update(block)
        return digest.hexdigest()
    except OSError:
        return None


def _table_columns(conn, backend: str, table: str) -> set[str]:
    if backend == "sqlite":
        return {str(row[1]).lower() for row in conn.execute(f"PRAGMA table_info({table})").fetchall()}
    if backend == "postgresql":
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ?", (table,),
        ).fetchall()
    elif backend == "mysql":
        rows = conn.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = ?", (table,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS WHERE TABLE_NAME = ?", (table,),
        ).fetchall()
    return {str(row[0]).lower() for row in rows}


def _chunked(items, size):
    for i in range(0, len(items), size):
        yield items[i:i + size]


def _in_clause(values: list) -> str:
    return ",".join("?" for _ in values)


def _db_key(profile: dict, core) -> tuple:
    backend = str(profile.get("type") or core.DB_TYPE_SQLITE).lower()
    if backend == core.DB_TYPE_SQLITE:
        raw_path = str(profile.get("path") or APP_DIR / "faces.db")
        return backend, str(Path(raw_path).expanduser().resolve()).casefold()
    return (
        backend,
        str(profile.get("host") or "localhost").strip().casefold(),
        str(profile.get("port") or DEFAULT_PORTS.get(backend, "")).strip(),
        str(profile.get("database") or "").strip().casefold(),
    )


def _identity_keys(row: dict) -> list[tuple]:
    """Идентичность профиля: VK ID, URL источника, затем сайт+имя."""
    result = []
    source = str(row.get("source") or "").strip().casefold()
    vk_id = row.get("vk_user_id")
    if vk_id not in (None, "", 0, "0"):
        result.append(("vk", str(vk_id)))
    url = str(row.get("profile_url") or "").strip().rstrip("/").casefold()
    if url:
        result.append(("url", url))
    domain = str(row.get("domain") or "").strip().casefold()
    if domain:
        result.append(("domain", source, domain))
    screen = str(row.get("screen_name") or "").strip().lstrip("@").casefold()
    if screen:
        result.append(("screen", source, screen))
    return result


def _copy_profiles(core, source, dest, source_backend: str, dest_backend: str, log, *,
                   deduplicate_existing: bool = True, progress_callback=None) -> tuple[dict, dict]:
    source_cols = _table_columns(source, source_backend, "profiles")
    dest_cols = _table_columns(dest, dest_backend, "profiles")
    if "id" not in source_cols or "id" not in dest_cols:
        raise RuntimeError("В таблице profiles отсутствует обязательный столбец id.")

    src_select = sorted(source_cols)
    # Не вытаскиваем raw_json всех целевых профилей в RAM: в больших базах
    # это поле может содержать десятки/сотни мегабайт данных.
    dst_cache_cols = [c for c in sorted(dest_cols) if c != "raw_json"]
    dst_rows = (
        dest.execute(f"SELECT {', '.join(dst_cache_cols)} FROM profiles").fetchall()
        if deduplicate_existing else []
    )
    targets_by_key: dict[tuple, dict] = {}
    for raw in dst_rows:
        row = dict(zip(dst_cache_cols, raw))
        for key in _identity_keys(row):
            targets_by_key.setdefault(key, row)

    source_to_dest: dict[int, int] = {}
    stats = {"profiles_inserted": 0, "profiles_matched": 0}
    common = [c for c in src_select if c in dest_cols and c != "id"]
    fillable = [c for c in common if c not in {"raw_json"}]
    total_row = source.execute("SELECT COUNT(*) FROM profiles").fetchone()
    total_profiles = int(total_row[0] or 0) if total_row else 0
    processed_profiles = 0
    stream = getattr(source, "execute_streaming", source.execute)
    cursor = stream(f"SELECT {', '.join(src_select)} FROM profiles ORDER BY id")
    try:
        while True:
            raw_batch = cursor.fetchmany(500)
            if not raw_batch:
                break
            for raw in raw_batch:
                processed_profiles += 1
                row = dict(zip(src_select, raw))
                source_id = int(row["id"])
                matched = (
                    next((targets_by_key[k] for k in _identity_keys(row) if k in targets_by_key), None)
                    if deduplicate_existing else None
                )
                if matched is not None:
                    dest_id = int(matched["id"])
                    source_to_dest[source_id] = dest_id
                    stats["profiles_matched"] += 1
                    updates = [(c, row.get(c)) for c in fillable
                               if c in matched and row.get(c) not in (None, "") and matched.get(c) in (None, "")]
                    if updates:
                        set_sql = ", ".join(f"{c} = ?" for c, _ in updates)
                        dest.execute(f"UPDATE profiles SET {set_sql} WHERE id = ?",
                                     tuple(v for _, v in updates) + (dest_id,))
                        for c, v in updates:
                            matched[c] = v
                    continue

                if not common:
                    raise RuntimeError("Нет общих столбцов для переноса profiles.")
                cur = dest.execute(
                    f"INSERT INTO profiles ({', '.join(common)}) VALUES ({_in_clause(common)})",
                    tuple(row.get(c) for c in common),
                )
                dest_id = int(cur.lastrowid)
                inserted = {k: v for k, v in row.items() if k != "raw_json"}
                inserted["id"] = dest_id
                source_to_dest[source_id] = dest_id
                stats["profiles_inserted"] += 1
                if deduplicate_existing:
                    for key in _identity_keys(inserted):
                        targets_by_key.setdefault(key, inserted)
            if progress_callback and (processed_profiles % 500 == 0 or processed_profiles == total_profiles):
                progress_callback(processed_profiles, total_profiles)
        dest.commit()
    finally:
        cursor.close()
    log(f"Профили: добавлено {stats['profiles_inserted']}, сопоставлено существующих {stats['profiles_matched']}.")
    return source_to_dest, stats


def _get_target_images_by_values(dest, dest_backend: str, target_cols: set[str], field: str, values: list[str]) -> list[dict]:
    if not values or field not in target_cols:
        return []
    fields = ["id", "path"]
    for optional in ("sha256", "source_type", "profile_id"):
        if optional in target_cols and optional not in fields:
            fields.append(optional)
    rows_out = []
    # 250 parameters per batch keeps this compatible with SQLite/SQL Server.
    for part in _chunked(list(dict.fromkeys(values)), BATCH_IMAGES):
        rows = dest.execute(
            f"SELECT {', '.join(fields)} FROM images WHERE {field} IN ({_in_clause(part)})", tuple(part),
        ).fetchall()
        rows_out.extend(dict(zip(fields, row)) for row in rows)
    return rows_out


def _get_hash_candidates(dest, dest_backend: str, target_cols: set[str], batch_rows: list[dict], profile_map: dict[int, int]) -> list[dict]:
    """Находит SHA-дубликаты с фильтром по source_type/VK-профилю.

    В VK-данных много профилей может использовать один и тот же аватар-заглушку.
    Поэтому нельзя делать широкий SELECT только по sha256: он способен вернуть
    сотни тысяч строк на один hash. При актуальной схеме используем тип источника
    и profile_id, что также предотвращает ошибочное слияние разных VK-профилей.
    """
    if not {"sha256", "source_type"}.issubset(target_cols):
        return []
    fields = ["id", "path"]
    for optional in ("sha256", "source_type", "profile_id"):
        if optional in target_cols and optional not in fields:
            fields.append(optional)
    result: list[dict] = []

    nonvk_hashes = []
    vk_pairs: set[tuple[str, int | None]] = set()
    for row in batch_rows:
        digest = str(row.get("_effective_sha") or "").strip()
        if not digest:
            continue
        kind = str(row.get("source_type") or "local").strip().lower()
        if kind == "vk":
            if "profile_id" not in target_cols:
                continue
            raw_profile_id = row.get("profile_id")
            mapped_id = profile_map.get(int(raw_profile_id)) if raw_profile_id not in (None, "") and int(raw_profile_id) in profile_map else None
            vk_pairs.add((digest, mapped_id))
        else:
            nonvk_hashes.append(digest)

    for part in _chunked(list(dict.fromkeys(nonvk_hashes)), BATCH_IMAGES):
        rows = dest.execute(
            f"SELECT {', '.join(fields)} FROM images "
            f"WHERE sha256 IN ({_in_clause(part)}) AND (source_type <> 'vk' OR source_type IS NULL)",
            tuple(part),
        ).fetchall()
        result.extend(dict(zip(fields, raw)) for raw in rows)

    nonnull_pairs = [(digest, profile_id) for digest, profile_id in vk_pairs if profile_id is not None]
    for part in _chunked(nonnull_pairs, 100):
        clauses = " OR ".join("(sha256 = ? AND profile_id = ?)" for _ in part)
        params = tuple(value for pair in part for value in pair)
        rows = dest.execute(
            f"SELECT {', '.join(fields)} FROM images WHERE source_type = 'vk' AND ({clauses})",
            params,
        ).fetchall()
        result.extend(dict(zip(fields, raw)) for raw in rows)

    null_profile_hashes = list(dict.fromkeys(digest for digest, profile_id in vk_pairs if profile_id is None))
    for part in _chunked(null_profile_hashes, BATCH_IMAGES):
        rows = dest.execute(
            f"SELECT {', '.join(fields)} FROM images WHERE source_type = 'vk' "
            f"AND profile_id IS NULL AND sha256 IN ({_in_clause(part)})",
            tuple(part),
        ).fetchall()
        result.extend(dict(zip(fields, raw)) for raw in rows)
    return result


def _face_counts(dest, image_ids: list[int]) -> dict[int, int]:
    counts: dict[int, int] = {}
    if not image_ids:
        return counts
    for part in _chunked(list(dict.fromkeys(int(x) for x in image_ids)), BATCH_IMAGES):
        rows = dest.execute(
            f"SELECT image_id, COUNT(*) FROM faces WHERE image_id IN ({_in_clause(part)}) GROUP BY image_id",
            tuple(part),
        ).fetchall()
        counts.update({int(r[0]): int(r[1]) for r in rows})
    return counts


def _source_file_for_image(raw_path, source_profile: dict, core) -> Path | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    p = Path(value).expanduser()
    candidates = []
    if p.is_absolute():
        candidates.append(p)
    else:
        crop_root = Path(str(source_profile.get("crop_path") or core.CROP_DIR)).expanduser().resolve()
        candidates.extend((crop_root / p, core.APP_DIR / p, crop_root / p.name))
    for candidate in candidates:
        try:
            if candidate.is_file():
                return candidate.resolve()
        except OSError:
            continue
    return None


def _target_file_for_path(raw_path: str, target_profile: dict, core) -> Path | None:
    value = str(raw_path or "").strip()
    if not value:
        return None
    p = Path(value)
    if p.is_absolute():
        return p
    crop_root = Path(str(target_profile.get("crop_path") or core.CROP_DIR)).expanduser().resolve()
    return (crop_root / p).resolve()


def migrate_database_data(core, source_profile: dict, target_profile: dict, *, create_target: bool,
                          rebuild_index: bool, log_callback=None) -> dict:
    """Копирует записи profiles/images/faces. CROP-файлы не копируются, images.path сохраняется без изменений."""
    def log(message):
        if log_callback:
            log_callback(str(message))

    def report_progress(percent, message, indeterminate=False):
        if progress_callback:
            try:
                progress_callback(max(0.0, min(100.0, float(percent))), str(message), bool(indeterminate))
            except Exception:
                pass

    report_progress(0, "Подключение к исходной БД...")
    source_profile = core._normalize_profile_storage_paths(dict(source_profile))
    target_profile = core._normalize_profile_storage_paths(dict(target_profile))
    if _db_key(source_profile, core) == _db_key(target_profile, core):
        raise ValueError("Исходная и целевая БД указывают на одно и то же хранилище.")
    source_type = str(source_profile.get("type") or core.DB_TYPE_SQLITE)
    target_type = str(target_profile.get("type") or core.DB_TYPE_SQLITE)
    if source_type == core.DB_TYPE_SQLITE:
        source_path = Path(str(source_profile.get("path") or "")).expanduser().resolve()
        if not source_path.is_file():
            raise FileNotFoundError(f"Файл исходной SQLite-БД не найден: {source_path}")
    if not create_target and target_type == core.DB_TYPE_SQLITE:
        target_path = Path(str(target_profile.get("path") or "")).expanduser().resolve()
        if not target_path.is_file():
            raise FileNotFoundError(f"Файл целевой SQLite-БД не найден: {target_path}")
    if create_target and target_type != core.DB_TYPE_SQLITE:
        # Для серверной СУБД режим «новая БД» означает именно создание новой
        # базы. Чтобы случайно не слить данные в существующую схему без спроса,
        # для слияния используйте режим выбора существующего профиля.
        if core._database_exists(target_profile):
            raise FileExistsError(
                "Целевая серверная БД уже существует. Для слияния сначала добавьте "
                "её как профиль и выберите «Слить с существующей БД», либо укажите "
                "другое имя базы данных."
            )

    src = dst = None
    mapping_path = None
    temp_map = None
    stats = {
        "profiles_inserted": 0, "profiles_matched": 0,
        "images_inserted": 0, "images_deduplicated": 0,
        "image_path_conflicts": 0, "faces_inserted": 0, "faces_skipped": 0,
        "index_built": False,
    }
    created_profile = False
    try:
        report_progress(1, "Подключение к исходной БД...")
        log("Подключение к исходной БД...")
        src = core.create_database_for_profile(
            source_profile, maintenance=False, create_if_missing=False,
            activate_storage=False, initialize=False,
        )
        log("Подключение к целевой БД...")
        dst = core.create_database_for_profile(
            target_profile, maintenance=False, create_if_missing=bool(create_target),
            activate_storage=False, initialize=False,
        )
        if not all(core._table_exists(src, table) for table in ("profiles", "images", "faces")):
            raise RuntimeError("В исходной БД отсутствует одна из таблиц profiles/images/faces.")
        report_progress(3, "Проверка структуры целевой БД...")
        log("Проверка/создание структуры таблиц назначения...")
        core._initialize_schema(dst, maintenance=False)
        dst.commit()

        report_progress(5, "Перенос профилей...")
        source_to_dest_profile, profile_stats = _copy_profiles(
            core, src, dst, source_type, target_type, log,
            deduplicate_existing=not create_target,
            progress_callback=lambda current, total: report_progress(
                5 + 5 * (current / total if total else 1),
                f"Профили: {current:,}/{total:,}"
            ),
        )
        stats.update(profile_stats)
        report_progress(10, "Подготовка переноса изображений...")

        source_image_cols = _table_columns(src, source_type, "images")
        dest_image_cols = _table_columns(dst, target_type, "images")
        source_face_cols = _table_columns(src, source_type, "faces")
        dest_face_cols = _table_columns(dst, target_type, "faces")
        required_image = {"id", "path", "filename"}
        required_face = {"image_id", "x1", "y1", "x2", "y2", "det_score", "embedding", "embedding_dim"}
        if not required_image.issubset(source_image_cols) or not required_image.issubset(dest_image_cols):
            raise RuntimeError("В таблице images отсутствуют обязательные столбцы id/path/filename.")
        if not required_face.issubset(source_face_cols) or not required_face.issubset(dest_face_cols):
            raise RuntimeError("В таблице faces отсутствуют обязательные столбцы embedding/координат.")

        src_image_columns = sorted(source_image_cols)

        temp_file = tempfile.NamedTemporaryFile(prefix="face_migration_", suffix=".sqlite", delete=False)
        mapping_path = Path(temp_file.name)
        temp_file.close()
        temp_map = sqlite3.connect(str(mapping_path))
        temp_map.execute("PRAGMA journal_mode=OFF")
        temp_map.execute("PRAGMA synchronous=OFF")
        temp_map.execute("CREATE TABLE image_map (src_id INTEGER PRIMARY KEY, dst_id INTEGER NOT NULL, copy_faces INTEGER NOT NULL, dst_path TEXT, is_new INTEGER NOT NULL DEFAULT 0)")
        temp_map.execute("CREATE INDEX idx_image_map_dst ON image_map(dst_id)")
        temp_map.execute("CREATE TABLE face_owner (dst_id INTEGER PRIMARY KEY, src_id INTEGER NOT NULL)")
        temp_map.commit()

        total_images_row = src.execute("SELECT COUNT(*) FROM images").fetchone()
        total_images = int(total_images_row[0] or 0) if total_images_row else 0
        log(f"Перенос изображений: {total_images:,} записей, партиями по {BATCH_IMAGES}...")
        img_cursor = getattr(src, "execute_streaming", src.execute)(
            f"SELECT {', '.join(src_image_columns)} FROM images ORDER BY id"
        )
        processed_images = 0
        report_progress(10, f"Изображения: 0/{total_images:,}")
        try:
            while True:
                raw_batch = img_cursor.fetchmany(BATCH_IMAGES)
                if not raw_batch:
                    break
                batch_rows = [dict(zip(src_image_columns, raw)) for raw in raw_batch]
                generated_paths = []
                hash_values = []
                raw_paths = []
                for row in batch_rows:
                    src_id = int(row["id"])
                    raw_path = str(row.get("path") or "")
                    # Сохраняем исходный относительный путь как есть. Не вычисляем
                    # физический путь к CROP и не добавляем служебный префикс migration/.
                    row["_effective_sha"] = str(row.get("sha256") or "").strip()
                    row["_migration_path"] = raw_path
                    generated_paths.append(raw_path)
                    if raw_path:
                        raw_paths.append(raw_path)
                    if row["_effective_sha"]:
                        hash_values.append(row["_effective_sha"])

                # Новая БД: копируем все записи без поиска дублей. Слияние:
                # оставляем сопоставление существующих изображений по пути/SHA.
                if create_target:
                    found_paths = []
                    by_migration_path = {}
                    found_hashes = []
                else:
                    found_paths = _get_target_images_by_values(dst, target_type, dest_image_cols, "path", generated_paths)
                    by_migration_path = {str(r.get("path")): r for r in found_paths}
                    found_hashes = _get_hash_candidates(
                        dst, target_type, dest_image_cols, batch_rows, source_to_dest_profile
                    )
                existing_ids = [int(r["id"]) for r in found_paths + found_hashes]
                counts = _face_counts(dst, existing_ids)
                by_sha: dict[str, list[dict]] = {}
                for target_row in found_hashes:
                    digest = str(target_row.get("sha256") or "")
                    if digest:
                        by_sha.setdefault(digest, []).append(target_row)

                pending = []
                pending_by_key: dict[tuple, dict] = {}
                mapping_actions: list[tuple[int, int | str, int, str, int]] = []
                for row in batch_rows:
                    src_id = int(row["id"])
                    sha = row["_effective_sha"]
                    source_kind = str(row.get("source_type") or "local").strip().lower()
                    source_profile_id = row.get("profile_id")
                    mapped_profile_id = source_to_dest_profile.get(int(source_profile_id)) if source_profile_id not in (None, "") and int(source_profile_id) in source_to_dest_profile else None
                    generated_path = row["_migration_path"]
                    found = None if create_target else by_migration_path.get(generated_path)

                    if not create_target and found is None and sha:
                        candidates = by_sha.get(sha, [])
                        for candidate in candidates:
                            c_kind = str(candidate.get("source_type") or "local").strip().lower()
                            if source_kind == "vk":
                                if not {"source_type", "profile_id"}.issubset(dest_image_cols):
                                    continue
                                if c_kind != "vk" or candidate.get("profile_id") != mapped_profile_id:
                                    continue
                            elif c_kind == "vk":
                                continue
                            found = candidate
                            break

                    if found is not None:
                        dst_id = int(found["id"])
                        found_path = str(found.get("path") or "")
                        copy_faces = 1 if counts.get(dst_id, 0) == 0 else 0
                        mapping_actions.append((src_id, dst_id, copy_faces, found_path, 0))
                        stats["images_deduplicated"] += 1
                        continue

                    # Защищаемся от повторов hash внутри текущей партии, в том числе
                    # для разных source image.id, которые ссылаются на один и тот же файл.
                    key = None
                    if not create_target and sha:
                        key = ("vk", mapped_profile_id, sha) if source_kind == "vk" else ("nonvk", sha)
                    if key is not None and key in pending_by_key:
                        mapping_actions.append((src_id, pending_by_key[key]["_migration_path"], 1,
                                                pending_by_key[key]["_migration_path"], 0))
                        stats["images_deduplicated"] += 1
                        continue

                    row["_mapped_profile_id"] = mapped_profile_id
                    pending.append(row)
                    if key is not None:
                        pending_by_key[key] = row

                # Добавляем новые изображения одним executemany, затем одним IN-запросом
                # получаем назначенные сервером ID и строим отображение source->destination.
                if pending:
                    insert_cols = [c for c in (
                        "path", "filename", "sha256", "width", "height", "processed",
                        "source_type", "source_url", "url_image", "profile_id", "source_updated_at",
                    ) if c in dest_image_cols]
                    payload = []
                    for row in pending:
                        values = []
                        for col in insert_cols:
                            if col == "path":
                                values.append(row["_migration_path"])
                            elif col == "profile_id":
                                values.append(row.get("_mapped_profile_id"))
                            elif col == "sha256":
                                values.append(row.get("_effective_sha") or None)
                            elif col == "source_type":
                                values.append(row.get(col) or "local")
                            elif col == "filename":
                                values.append(row.get("filename") or Path(row["_migration_path"]).name)
                            else:
                                values.append(row.get(col))
                        payload.append(tuple(values))
                    placeholders = _in_clause(insert_cols)
                    dst.executemany(
                        f"INSERT INTO images ({', '.join(insert_cols)}) VALUES ({placeholders})", payload,
                    )
                    dest_new_rows = _get_target_images_by_values(
                        dst, target_type, dest_image_cols, "path", [r["_migration_path"] for r in pending]
                    )
                    by_new_path = {str(r.get("path")): r for r in dest_new_rows}
                    for row in pending:
                        target_row = by_new_path.get(row["_migration_path"])
                        if target_row is None:
                            raise RuntimeError(f"После INSERT не найдена запись images.path={row['_migration_path']}")
                        mapping_actions.append((int(row["id"]), int(target_row["id"]), 1,
                                                str(row["_migration_path"]), 1))
                        stats["images_inserted"] += 1
                    dst.commit()

                # Отложенные ссылки на новый путь из-за дубликатов внутри партии.
                pending_path_to_id = {}
                if pending:
                    for row in pending:
                        target_row = by_new_path.get(row["_migration_path"])
                        if target_row:
                            pending_path_to_id[row["_migration_path"]] = int(target_row["id"])
                fixed_actions = []
                for src_id, dst_or_path, copy_faces, dst_path, is_new in mapping_actions:
                    if isinstance(dst_or_path, str):
                        mapped_id = pending_path_to_id.get(dst_or_path)
                        if mapped_id is None:
                            # Такой путь мог принадлежать представителю-дубликату,
                            # вставленному в одной из предыдущих партий. Найдём через карту.
                            mapped = temp_map.execute(
                                "SELECT dst_id FROM image_map WHERE dst_path = ? ORDER BY src_id LIMIT 1",
                                (dst_or_path,),
                            ).fetchone()
                            if mapped:
                                mapped_id = int(mapped[0])
                        if mapped_id is None:
                            raise RuntimeError(f"Не удалось сопоставить дубликат изображения с путём {dst_or_path}")
                        fixed_actions.append((src_id, mapped_id, copy_faces, dst_path, is_new))
                    else:
                        fixed_actions.append((src_id, int(dst_or_path), copy_faces, dst_path, is_new))
                temp_map.executemany(
                    "INSERT OR REPLACE INTO image_map(src_id,dst_id,copy_faces,dst_path,is_new) VALUES (?,?,?,?,?)",
                    fixed_actions,
                )
                temp_map.commit()

                # Переносим только записи БД. Физические CROP-файлы не копируются.
                processed_images += len(batch_rows)
                report_progress(
                    10 + 40 * (processed_images / total_images if total_images else 1),
                    f"Изображения: {processed_images:,}/{total_images:,}"
                )
                if processed_images % (BATCH_IMAGES * 4) == 0 or processed_images == total_images:
                    log(f"Изображения: {processed_images:,}/{total_images:,}; новых {stats['images_inserted']:,}; дубликатов {stats['images_deduplicated']:,}.")
        finally:
            img_cursor.close()
        dst.commit()
        report_progress(50, f"Изображения завершены: {processed_images:,}/{total_images:,}")

        total_faces_row = src.execute("SELECT COUNT(*) FROM faces").fetchone()
        total_faces = int(total_faces_row[0] or 0) if total_faces_row else 0
        report_progress(50, f"Лица: 0/{total_faces:,}")
        log(f"Перенос лиц: {total_faces:,} строк, партиями по {BATCH_FACES}...")
        face_select = ["image_id", "x1", "y1", "x2", "y2", "det_score", "embedding", "embedding_dim"]
        face_cursor = getattr(src, "execute_streaming", src.execute)(
            "SELECT " + ", ".join(face_select) + " FROM faces ORDER BY image_id"
        )
        processed_faces = 0
        pending_face_rows = []
        try:
            while True:
                raw_batch = face_cursor.fetchmany(BATCH_FACES)
                if not raw_batch:
                    break
                # ORDER BY image_id, поэтому обычный словарь ограничен текущей партией.
                source_image_ids = sorted({int(row[0]) for row in raw_batch})
                source_maps = {}
                for part in _chunked(source_image_ids, BATCH_IMAGES):
                    mapped_rows = temp_map.execute(
                        f"SELECT src_id,dst_id,copy_faces FROM image_map WHERE src_id IN ({_in_clause(part)})", tuple(part),
                    ).fetchall()
                    source_maps.update({int(r[0]): (int(r[1]), bool(r[2])) for r in mapped_rows})

                # Загружаем владельцев только для target image текущей партии.
                # Нельзя выполнять отдельный SQLite-запрос на каждый embedding:
                # при миллионах лиц это стало бы узким местом миграции.
                target_image_ids = sorted({
                    mapping[0] for mapping in source_maps.values() if mapping and mapping[1]
                })
                owner_by_target: dict[int, int] = {}
                for part in _chunked(target_image_ids, BATCH_IMAGES):
                    if not part:
                        continue
                    owner_rows = temp_map.execute(
                        f"SELECT dst_id,src_id FROM face_owner WHERE dst_id IN ({_in_clause(part)})", tuple(part),
                    ).fetchall()
                    owner_by_target.update({int(r[0]): int(r[1]) for r in owner_rows})
                new_owners = []
                for row in raw_batch:
                    src_image_id = int(row[0])
                    mapping = source_maps.get(src_image_id)
                    if not mapping or not mapping[1]:
                        stats["faces_skipped"] += 1
                        continue
                    dst_image_id = mapping[0]
                    owner_src_id = owner_by_target.get(dst_image_id)
                    if owner_src_id is None:
                        # Первая source-картинка с лицами, связанная с этим target image.
                        owner_src_id = src_image_id
                        owner_by_target[dst_image_id] = owner_src_id
                        new_owners.append((dst_image_id, owner_src_id))
                    if owner_src_id != src_image_id:
                        stats["faces_skipped"] += 1
                        continue
                    embedding = row[6]
                    if isinstance(embedding, memoryview):
                        embedding = embedding.tobytes()
                    elif isinstance(embedding, bytearray):
                        embedding = bytes(embedding)
                    pending_face_rows.append((
                        dst_image_id, row[1], row[2], row[3], row[4], row[5], embedding, row[7],
                    ))
                    if len(pending_face_rows) >= BATCH_FACES:
                        dst.executemany(
                            "INSERT INTO faces (image_id,x1,y1,x2,y2,det_score,embedding,embedding_dim) VALUES (?,?,?,?,?,?,?,?)",
                            pending_face_rows,
                        )
                        stats["faces_inserted"] += len(pending_face_rows)
                        dst.commit()
                        pending_face_rows.clear()
                if new_owners:
                    temp_map.executemany(
                        "INSERT OR IGNORE INTO face_owner(dst_id,src_id) VALUES (?,?)", new_owners
                    )
                    temp_map.commit()
                processed_faces += len(raw_batch)
                report_progress(
                    50 + 40 * (processed_faces / total_faces if total_faces else 1),
                    f"Лица: {processed_faces:,}/{total_faces:,}"
                )
                if processed_faces % (BATCH_FACES * 4) == 0 or processed_faces == total_faces:
                    log(f"Лица: прочитано {processed_faces:,}/{total_faces:,}; перенесено {stats['faces_inserted']:,}.")
        finally:
            face_cursor.close()
        if pending_face_rows:
            dst.executemany(
                "INSERT INTO faces (image_id,x1,y1,x2,y2,det_score,embedding,embedding_dim) VALUES (?,?,?,?,?,?,?,?)",
                pending_face_rows,
            )
            stats["faces_inserted"] += len(pending_face_rows)
            dst.commit()
        log(f"Лица: добавлено {stats['faces_inserted']:,}; пропущено как дубликаты/без целевого изображения {stats['faces_skipped']:,}.")
        report_progress(90, f"Лица завершены: {processed_faces:,}/{total_faces:,}")

        if rebuild_index:
            report_progress(92, "Создание FAISS-индекса назначения (прогресс этой операции не измеряется)...", True)
            log("Создание нового FAISS-индекса назначения (CPU). Это может занять время и RAM...")
            old_index = core.INDEX_FILE
            old_db_file = core.DB_FILE
            old_cleanup = core.cleanup_missing_images
            try:
                core.INDEX_FILE = str(Path(str(target_profile["index_path"])).expanduser().resolve())
                core.DB_FILE = str(target_profile.get("path") or target_profile.get("database") or "")
                # Индекс строим по всем перенесённым embedding. Отсутствующий локальный
                # CROP не должен во время миграции удалять запись лица из целевой БД.
                core.cleanup_missing_images = lambda _conn: 0
                target_index_path = Path(core.INDEX_FILE).expanduser().resolve()
                target_faces_count = int(dst.execute("SELECT COUNT(*) FROM faces").fetchone()[0] or 0)
                if target_faces_count <= 0:
                    target_index_path.unlink(missing_ok=True)
                    log("В целевой БД нет лиц: FAISS-индекс не создаётся.")
                    stats["index_built"] = False
                else:
                    core.build_faiss_index(dst, use_gpu_index=False)
                    stats["index_built"] = target_index_path.is_file()
            finally:
                core.cleanup_missing_images = old_cleanup
                core.INDEX_FILE = old_index
                core.DB_FILE = old_db_file

        report_progress(99, "Завершение миграции и фиксация изменений...")
        return stats
    except Exception:
        if dst is not None:
            try:
                dst.rollback()
            except Exception:
                pass
        raise
    finally:
        if src is not None:
            try:
                src.close()
            except Exception:
                pass
        if dst is not None:
            try:
                dst.close()
            except Exception:
                pass
        if temp_map is not None:
            try:
                temp_map.close()
            except Exception:
                pass
        if mapping_path is not None:
            try:
                mapping_path.unlink(missing_ok=True)
            except OSError:
                pass


class DatabaseMigrationWindow:
    def __init__(self, master: tk.Misc | None = None):
        self.root = tk.Toplevel(master) if master is not None else tk.Tk()
        self.root.title("Миграция и слияние баз данных — Face Search")
        self.root.geometry("940x790")
        self.root.minsize(850, 700)
        self.root.protocol("WM_DELETE_WINDOW", self._on_close)
        self.events: queue.Queue = queue.Queue()
        self.running = False
        self.profiles, self.active_name = _load_profiles_light()
        self.profile_by_label = {_profile_description(p): p for p in self.profiles}
        self._build_ui()
        self._on_destination_mode_changed()
        self._on_db_type_changed()
        self.root.after(100, self._poll_events)

    def _build_ui(self):
        main = ttk.Frame(self.root, padding=12)
        main.pack(fill="both", expand=True)
        main.columnconfigure(1, weight=1)
        main.rowconfigure(8, weight=1)

        ttk.Label(main, text="Исходная БД:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=5)
        source_names = [_profile_description(p) for p in self.profiles]
        self.source_var = tk.StringVar(value=next((x for x, p in self.profile_by_label.items() if p.get("name") == self.active_name), source_names[0] if source_names else ""))
        self.source_combo = ttk.Combobox(main, textvariable=self.source_var, values=source_names, state="readonly")
        self.source_combo.grid(row=0, column=1, columnspan=3, sticky="ew", pady=5)

        ttk.Label(main, text="Куда переносить:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=(10, 2))
        self.mode_var = tk.StringVar(value="existing")
        modes = ttk.Frame(main)
        modes.grid(row=1, column=1, columnspan=3, sticky="w", pady=(10, 2))
        ttk.Radiobutton(modes, text="Слить с существующей БД", value="existing", variable=self.mode_var, command=self._on_destination_mode_changed).pack(side="left", padx=(0, 18))
        ttk.Radiobutton(modes, text="Создать / подключить новую БД", value="new", variable=self.mode_var, command=self._on_destination_mode_changed).pack(side="left")

        self.existing_frame = ttk.LabelFrame(main, text="Профиль назначения", padding=8)
        self.existing_frame.grid(row=2, column=0, columnspan=4, sticky="ew", pady=5)
        self.existing_frame.columnconfigure(1, weight=1)
        ttk.Label(self.existing_frame, text="Профиль:").grid(row=0, column=0, sticky="w", padx=(0, 8))
        self.target_var = tk.StringVar()
        self.target_combo = ttk.Combobox(self.existing_frame, textvariable=self.target_var, values=source_names, state="readonly")
        target_default = next((x for x, p in self.profile_by_label.items() if p.get("name") != self.active_name), source_names[0] if source_names else "")
        self.target_var.set(target_default)
        self.target_combo.grid(row=0, column=1, sticky="ew")

        self.new_frame = ttk.LabelFrame(main, text="Параметры новой БД / подключения", padding=8)
        self.new_frame.grid(row=3, column=0, columnspan=4, sticky="ew", pady=5)
        self.new_frame.columnconfigure(1, weight=1)
        self.new_frame.columnconfigure(3, weight=1)

        self.new_name_var = tk.StringVar(value=f"Миграция — {time.strftime('%Y-%m-%d %H-%M')}")
        self.db_type_var = tk.StringVar(value="SQLite")
        self.path_var = tk.StringVar(value=str(APP_DIR / "migrated_faces.db"))
        self.host_var = tk.StringVar(value="localhost")
        self.port_var = tk.StringVar(value="5432")
        self.database_var = tk.StringVar(value="faces_migrated")
        self.user_var = tk.StringVar()
        self.password_var = tk.StringVar()
        self.driver_var = tk.StringVar()

        ttk.Label(self.new_frame, text="Имя профиля:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(self.new_frame, textvariable=self.new_name_var).grid(row=0, column=1, columnspan=3, sticky="ew", pady=4)
        ttk.Label(self.new_frame, text="Тип БД:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=4)
        type_combo = ttk.Combobox(self.new_frame, textvariable=self.db_type_var, values=list(DB_LABELS), state="readonly", width=28)
        type_combo.grid(row=1, column=1, sticky="w", pady=4)
        type_combo.bind("<<ComboboxSelected>>", lambda _e: self._on_db_type_changed())

        self.sqlite_fields = ttk.Frame(self.new_frame)
        self.sqlite_fields.grid(row=2, column=0, columnspan=4, sticky="ew")
        self.sqlite_fields.columnconfigure(1, weight=1)
        ttk.Label(self.sqlite_fields, text="Файл SQLite:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=4)
        ttk.Entry(self.sqlite_fields, textvariable=self.path_var).grid(row=0, column=1, sticky="ew", pady=4)
        ttk.Button(self.sqlite_fields, text="Обзор…", command=self._browse_sqlite).grid(row=0, column=2, padx=(6, 0), pady=4)

        self.server_fields = ttk.Frame(self.new_frame)
        self.server_fields.grid(row=3, column=0, columnspan=4, sticky="ew")
        self.server_fields.columnconfigure(1, weight=1)
        self.server_fields.columnconfigure(3, weight=1)
        labels = [
            ("Сервер / хост:", self.host_var, 0, 0), ("Порт:", self.port_var, 0, 2),
            ("Имя базы:", self.database_var, 1, 0), ("Пользователь:", self.user_var, 1, 2),
            ("Пароль:", self.password_var, 2, 0), ("ODBC-драйвер (SQL Server):", self.driver_var, 2, 2),
        ]
        for label, variable, row, col in labels:
            ttk.Label(self.server_fields, text=label).grid(row=row, column=col, sticky="w", padx=(0, 6), pady=4)
            ent = ttk.Entry(self.server_fields, textvariable=variable, show="*" if label == "Пароль:" else "")
            ent.grid(row=row, column=col + 1, sticky="ew", padx=(0, 10), pady=4)

        self.save_profile_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(self.new_frame, text="Сохранить новое подключение в списке профилей приложения", variable=self.save_profile_var).grid(row=4, column=0, columnspan=4, sticky="w", pady=(6, 2))

        self.rebuild_index_var = tk.BooleanVar(value=True)
        ttk.Checkbutton(main, text="После переноса пересоздать FAISS-индекс назначения (рекомендуется; может потребовать много RAM)", variable=self.rebuild_index_var).grid(row=4, column=0, columnspan=4, sticky="w", pady=(8, 4))
        ttk.Label(main, text="При слиянии существующих БД изображения сопоставляются по SHA-256; при создании новой БД записи копируются без дедупликации. Файлы CROP не копируются, относительный путь Images.path сохраняется.", wraplength=880).grid(row=5, column=0, columnspan=4, sticky="w", pady=(3, 8))

        action_frame = ttk.Frame(main)
        action_frame.grid(row=6, column=0, columnspan=4, sticky="ew", pady=(2, 6))
        self.start_button = ttk.Button(action_frame, text="Начать миграцию", command=self._start_migration)
        self.start_button.pack(side="left")
        self.progress = ttk.Progressbar(action_frame, mode="determinate", maximum=100, value=0)
        self.progress.pack(side="left", fill="x", expand=True, padx=(12, 0))
        self.progress_percent_var = tk.StringVar(value="0%")
        ttk.Label(action_frame, textvariable=self.progress_percent_var, width=6, anchor="e").pack(side="left", padx=(8, 0))

        self.status_var = tk.StringVar(value="Выберите источник и назначение. Перенос выполняется в отдельном потоке.")
        ttk.Label(main, textvariable=self.status_var, wraplength=880).grid(row=7, column=0, columnspan=4, sticky="ew", pady=(4, 6))
        self.log_text = tk.Text(main, height=14, wrap="word", state="disabled")
        scrollbar = ttk.Scrollbar(main, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=scrollbar.set)
        self.log_text.grid(row=8, column=0, columnspan=3, sticky="nsew")
        scrollbar.grid(row=8, column=3, sticky="ns")
        ttk.Label(main, text="После миграции FAISS-индекс исходного профиля не меняется. У назначения будет отдельный индекс.", foreground="#555555", wraplength=880).grid(row=9, column=0, columnspan=4, sticky="w", pady=(8, 0))

    def _browse_sqlite(self):
        chosen = filedialog.asksaveasfilename(
            parent=self.root, title="Файл целевой SQLite-БД", defaultextension=".db",
            filetypes=[("SQLite database", "*.db *.sqlite *.sqlite3"), ("All files", "*.*")],
            initialfile=Path(self.path_var.get() or "migrated_faces.db").name,
        )
        if chosen:
            self.path_var.set(chosen)
            if not self.database_var.get().strip():
                self.database_var.set(Path(chosen).stem)

    def _on_destination_mode_changed(self):
        if self.mode_var.get() == "existing":
            self.existing_frame.grid()
            self.new_frame.grid_remove()
        else:
            self.existing_frame.grid_remove()
            self.new_frame.grid()
        self._on_db_type_changed()

    def _on_db_type_changed(self):
        backend = DB_LABELS.get(self.db_type_var.get(), "sqlite")
        if backend == "sqlite":
            self.sqlite_fields.grid()
            self.server_fields.grid_remove()
        else:
            self.server_fields.grid()
            self.sqlite_fields.grid_remove()
            self.port_var.set(str(DEFAULT_PORTS.get(backend, "")))

    def _set_progress(self, percent: float, message: str, indeterminate: bool = False):
        mode = str(self.progress.cget("mode"))
        if indeterminate:
            if mode != "indeterminate":
                self.progress.stop()
                self.progress.configure(mode="indeterminate")
                self.progress.start(10)
            self.progress_percent_var.set("…")
        else:
            if mode != "determinate":
                self.progress.stop()
                self.progress.configure(mode="determinate", maximum=100)
            value = max(0.0, min(100.0, float(percent)))
            self.progress.configure(value=value)
            self.progress_percent_var.set(f"{value:.0f}%")
        self.status_var.set(str(message)[:220])

    def _set_log(self, message: str):
        self.log_text.configure(state="normal")
        self.log_text.insert("end", message.rstrip() + "\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")
        self.status_var.set(message[:220])

    def _start_migration(self):
        if self.running:
            return
        source_label = self.source_var.get()
        source_profile = self.profile_by_label.get(source_label)
        if not source_profile:
            messagebox.showerror("Миграция БД", "Выберите корректный исходный профиль.", parent=self.root)
            return
        mode = self.mode_var.get()
        if mode == "existing":
            target_profile = self.profile_by_label.get(self.target_var.get())
            if not target_profile:
                messagebox.showerror("Миграция БД", "Выберите профиль назначения.", parent=self.root)
                return
            if str(source_profile.get("name")) == str(target_profile.get("name")):
                messagebox.showerror("Миграция БД", "Исходный и целевой профили совпадают.", parent=self.root)
                return
            create_target = False
        else:
            # Core импортируем в worker; здесь проверяем поля без обращения к ML-стеку.
            backend = DB_LABELS.get(self.db_type_var.get(), "sqlite")
            target_profile = {
                "name": self.new_name_var.get().strip(), "type": backend,
                "path": str(Path(self.path_var.get().strip()).expanduser().resolve()) if backend == "sqlite" and self.path_var.get().strip() else self.path_var.get().strip(),
                "database": Path(self.path_var.get().strip()).stem if backend == "sqlite" and self.path_var.get().strip() else self.database_var.get().strip(),
                "host": self.host_var.get().strip() or "localhost", "port": self.port_var.get().strip() or str(DEFAULT_PORTS.get(backend, "")),
                "user": self.user_var.get().strip(), "password": self.password_var.get(), "driver": self.driver_var.get().strip(),
            }
            if not target_profile["name"]:
                messagebox.showerror("Миграция БД", "Введите имя нового профиля.", parent=self.root)
                return
            if backend == "sqlite" and not target_profile["path"]:
                messagebox.showerror("Миграция БД", "Укажите файл целевой SQLite-БД.", parent=self.root)
                return
            if backend != "sqlite" and not target_profile["database"]:
                messagebox.showerror("Миграция БД", "Введите имя целевой БД.", parent=self.root)
                return
            if backend != "sqlite":
                try:
                    target_profile["port"] = str(int(target_profile["port"]))
                except (TypeError, ValueError):
                    messagebox.showerror("Миграция БД", "Порт должен быть числом.", parent=self.root)
                    return
            if backend == "sqlite" and Path(target_profile["path"]).exists():
                if not messagebox.askyesno("Файл уже существует", "Файл SQLite уже существует. Выполнить слияние с данными внутри него?", parent=self.root):
                    return
                create_target = False
            else:
                create_target = True

        self.running = True
        self.start_button.configure(state="disabled")
        self.progress.stop()
        self.progress.configure(mode="determinate", maximum=100, value=0)
        self.progress_percent_var.set("0%")
        self._set_log("Запуск миграции…")
        rebuild_index = bool(self.rebuild_index_var.get())
        save_profile = bool(self.save_profile_var.get()) if mode == "new" else False

        def worker():
            try:
                self.events.put(("log", "Подготовка модуля приложения и драйверов БД…"))
                import face_search_gui as core
                actual_source = next((p for p in core._load_db_profiles()[0] if str(p.get("name")) == str(source_profile.get("name"))), None)
                if actual_source is None:
                    raise RuntimeError("Исходный профиль исчез из database_profiles.json. Обновите окно миграции.")
                actual_target = target_profile
                if mode == "existing":
                    actual_target = next((p for p in core._load_db_profiles()[0] if str(p.get("name")) == str(target_profile.get("name"))), None)
                    if actual_target is None:
                        raise RuntimeError("Профиль назначения не найден в database_profiles.json.")
                else:
                    # Все значения Tkinter уже скопированы в target_profile в UI-потоке.
                    # В worker читаем только обычный dict: обращаться к StringVar из
                    # фонового потока Tkinter небезопасно.
                    actual_target = core._normalize_profile_storage_paths(dict(target_profile))
                    if _db_key(actual_source, core) == _db_key(actual_target, core):
                        raise ValueError("Исходная и целевая БД указывают на одно хранилище.")
                    if save_profile:
                        profiles, active = core._load_db_profiles()
                        if any(str(p.get("name")) == str(actual_target.get("name")) for p in profiles):
                            raise ValueError("Профиль с таким именем уже существует. Задайте другое имя.")
                        # Сохраняем реквизиты до долгого переноса. Если процесс прервётся,
                        # подключение останется доступно, а частичная миграция допускает повтор.
                        profiles.append(actual_target)
                        core._save_db_profiles(profiles, active)
                        self.events.put(("profiles_saved", None))
                report = migrate_database_data(
                    core, actual_source, actual_target, create_target=create_target,
                    rebuild_index=rebuild_index,
                    log_callback=lambda message: self.events.put(("log", message)),
                    progress_callback=lambda percent, message, indeterminate=False: self.events.put(
                        ("progress", percent, message, indeterminate)
                    ),
                )
                self.events.put(("done", report))
            except Exception as exc:
                details = f"{type(exc).__name__}: {exc}"
                self.events.put(("error", details, traceback.format_exc()))

        threading.Thread(target=worker, daemon=True, name="database-migration-worker").start()

    def _poll_events(self):
        try:
            while True:
                event = self.events.get_nowait()
                kind = event[0]
                if kind == "log":
                    self._set_log(event[1])
                elif kind == "progress":
                    self._set_progress(event[1], event[2], event[3])
                elif kind == "profiles_saved":
                    self._set_log("Новое подключение сохранено в database_profiles.json.")
                elif kind == "done":
                    self.progress.stop()
                    self.progress.configure(mode="determinate", maximum=100, value=100)
                    self.progress_percent_var.set("100%")
                    self.running = False
                    self.start_button.configure(state="normal")
                    stats = event[1]
                    summary = (
                        "Миграция завершена.\n\n"
                        f"Профили добавлено/сопоставлено: {stats['profiles_inserted']}/{stats['profiles_matched']}\n"
                        f"Изображения добавлено/дубликатов: {stats['images_inserted']}/{stats['images_deduplicated']}\n"
                        f"Лиц добавлено/пропущено: {stats['faces_inserted']}/{stats['faces_skipped']}\n"
                        f"FAISS индекс создан: {'да' if stats['index_built'] else 'нет'}"
                    )
                    self._set_log(summary.replace("\n", " | "))
                    messagebox.showinfo("Миграция БД", summary, parent=self.root)
                elif kind == "error":
                    self.progress.stop()
                    self.running = False
                    self.start_button.configure(state="normal")
                    self._set_log("ОШИБКА: " + event[1])
                    self._set_log(event[2])
                    messagebox.showerror("Ошибка миграции", event[1] + "\n\nПодробнее смотрите в журнале окна.", parent=self.root)
        except queue.Empty:
            pass
        try:
            if self.root.winfo_exists():
                self.root.after(100, self._poll_events)
        except tk.TclError:
            pass

    def _on_close(self):
        if self.running:
            messagebox.showwarning("Миграция выполняется", "Не закрывайте окно до завершения переноса.", parent=self.root)
            return
        self.root.destroy()


def main():
    os.chdir(APP_DIR)
    window = DatabaseMigrationWindow()
    window.root.mainloop()


if __name__ == "__main__":
    main()
