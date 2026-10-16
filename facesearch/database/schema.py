# -*- coding: utf-8 -*-
"""Создание и обновление схемы FaceSearch для поддерживаемых СУБД."""
from __future__ import annotations

import json
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname

from .connection import DatabaseConnection
from ..metadata import (
    is_standard_metadata_document,
    normalize_legacy_metadata_document,
)
from ..config import (
    DB_TYPE_SQLITE, DB_TYPE_POSTGRESQL, DB_TYPE_MYSQL, DB_TYPE_MSSQL,
    PROFILE_COLUMNS, IMAGE_COLUMNS,
)

def ensure_columns(conn: DatabaseConnection, table: str, columns: dict[str, str]):
    if conn.backend == DB_TYPE_SQLITE:
        existing = {
            row[1]
            for row in conn.execute(f"PRAGMA table_info({table})").fetchall()
        }
    elif conn.backend == DB_TYPE_POSTGRESQL:
        existing = {
            row[0]
            for row in conn.execute(
                """
                SELECT column_name
                FROM information_schema.columns
                WHERE table_name = ?
                """,
                (table,),
            ).fetchall()
        }
    elif conn.backend == DB_TYPE_MYSQL:
        existing = {
            row[0]
            for row in conn.execute(
                """
                SELECT COLUMN_NAME
                FROM information_schema.columns
                WHERE table_schema = DATABASE()
                  AND table_name = ?
                """,
                (table,),
            ).fetchall()
        }
    else:
        existing = {
            row[0]
            for row in conn.execute(
                """
                SELECT COLUMN_NAME
                FROM INFORMATION_SCHEMA.COLUMNS
                WHERE TABLE_NAME = ?
                """,
                (table,),
            ).fetchall()
        }

    for name, definition in columns.items():
        if name not in existing:
            conn.execute(
                f"ALTER TABLE {table} ADD COLUMN {name} {definition}"
            )


def backfill_import_image_urls(conn: DatabaseConnection) -> int:
    rows = conn.execute(
        """
        SELECT i.id, p.raw_json
        FROM images i
        JOIN profiles p ON p.id = i.profile_id
        WHERE i.source_type = 'import'
          AND (i.url_image IS NULL OR TRIM(i.url_image) = '')
          AND p.raw_json IS NOT NULL
          AND TRIM(p.raw_json) != ''
        """
    ).fetchall()

    updated = 0
    for image_id, raw_json in rows:
        try:
            metadata = json.loads(raw_json)
        except Exception:
            continue
        if not isinstance(metadata, dict):
            continue
        url_image = str(metadata.get("url_image") or "").strip()
        if not url_image:
            continue
        conn.execute(
            "UPDATE images SET url_image = ? WHERE id = ?",
            (url_image, int(image_id)),
        )
        updated += 1

    if updated:
        conn.commit()
    return updated


def table_exists(conn: DatabaseConnection, table: str) -> bool:
    backend = conn.backend
    if backend == DB_TYPE_SQLITE:
        return conn.execute(
            "SELECT 1 FROM sqlite_master WHERE type='table' AND name = ?",
            (table,),
        ).fetchone() is not None
    if backend == DB_TYPE_POSTGRESQL:
        return conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = current_schema() AND table_name = ?",
            (table,),
        ).fetchone() is not None
    if backend == DB_TYPE_MYSQL:
        return conn.execute(
            "SELECT 1 FROM information_schema.tables "
            "WHERE table_schema = DATABASE() AND table_name = ?",
            (table,),
        ).fetchone() is not None
    return conn.execute(
        "SELECT 1 FROM INFORMATION_SCHEMA.TABLES "
        "WHERE TABLE_NAME = ?",
        (table,),
    ).fetchone() is not None


def _local_source_has_sidecar_json(source_url: str | None) -> bool:
    """Checks whether the local source has a same-name JSON sidecar."""
    value = str(source_url or "").strip()
    if not value:
        return False
    try:
        parsed = urlparse(value)
        if parsed.scheme.lower() == "file":
            source_path = Path(url2pathname(unquote(parsed.path)))
            if parsed.netloc and parsed.netloc.lower() not in {"", "localhost"}:
                source_path = Path(f"//{parsed.netloc}{source_path}")
        elif not parsed.scheme:
            source_path = Path(value)
        else:
            return False
        return source_path.with_suffix(".json").is_file()
    except (OSError, TypeError, ValueError):
        return False


def _apply_metadata_json_updates(conn: DatabaseConnection, updates) -> None:
    """Applies a bounded batch of id -> JSON/NULL changes with one SQL statement."""
    if not updates:
        return
    # Three parameters per row (CASE id WHEN id THEN value, plus WHERE id IN id).
    # 250 rows stays safely below SQL Server's 2100-parameter limit.
    for offset in range(0, len(updates), 250):
        batch = updates[offset:offset + 250]
        case_sql = " ".join("WHEN ? THEN ?" for _ in batch)
        where_sql = ",".join("?" for _ in batch)
        params = []
        for image_id, value in batch:
            params.extend((int(image_id), value))
        params.extend(int(image_id) for image_id, _value in batch)
        conn.execute(
            f"UPDATE images SET metadata_json = CASE id {case_sql} "
            f"ELSE metadata_json END WHERE id IN ({where_sql})",
            tuple(params),
        )


def _migrate_metadata_json_to_strict_envelope(conn: DatabaseConnection) -> None:
    """One-time migration of legacy flat EXIF JSON values.

    Local files without JSON get wrapped as {url_source, name, url_image, info}.
    Imported records and local files with a JSON sidecar have metadata_json cleared:
    their real metadata belongs in the original profile JSON, not the EXIF column.
    """
    migration_key = "metadata_json_strict_envelope_v2"
    if not table_exists(conn, "face_search_migrations"):
        try:
            conn.execute(
                "CREATE TABLE face_search_migrations ("
                "migration_key VARCHAR(128) NOT NULL PRIMARY KEY, "
                "applied_at VARCHAR(40))"
            )
            conn.commit()
        except Exception:
            # Another process may have created the table concurrently.
            if not table_exists(conn, "face_search_migrations"):
                raise

    done = conn.execute(
        "SELECT migration_key FROM face_search_migrations WHERE migration_key = ?",
        (migration_key,),
    ).fetchone()
    if done:
        return

    last_id = -1
    while True:
        if conn.backend == DB_TYPE_MSSQL:
            sql = (
                "SELECT TOP 250 id, source_type, source_url, metadata_json "
                "FROM images WHERE id > ? AND metadata_json IS NOT NULL "
                "ORDER BY id"
            )
        else:
            sql = (
                "SELECT id, source_type, source_url, metadata_json "
                "FROM images WHERE id > ? AND metadata_json IS NOT NULL "
                "ORDER BY id LIMIT 250"
            )
        rows = conn.execute(sql, (last_id,)).fetchall()
        if not rows:
            break

        updates = []
        for image_id, source_type, source_url, raw_json in rows:
            last_id = max(last_id, int(image_id))
            if str(source_type or "local").lower() != "local":
                # Older builds accidentally stored flat EXIF objects for imports.
                updates.append((int(image_id), None))
                continue

            if _local_source_has_sidecar_json(source_url):
                # A JSON sidecar is authoritative; do not retain EXIF-derived data.
                updates.append((int(image_id), None))
                continue

            try:
                value = json.loads(str(raw_json or ""))
            except Exception:
                value = None

            if is_standard_metadata_document(value):
                continue

            if isinstance(value, dict):
                normalized = normalize_legacy_metadata_document(value, source_url)
                encoded = json.dumps(
                    normalized, ensure_ascii=False, separators=(",", ":")
                )
            else:
                encoded = None
            updates.append((int(image_id), encoded))

        _apply_metadata_json_updates(conn, updates)
        conn.commit()

    conn.execute(
        "INSERT INTO face_search_migrations (migration_key, applied_at) VALUES (?, ?)",
        (migration_key, __import__("datetime").datetime.now().isoformat(timespec="seconds")),
    )
    conn.commit()


def initialize_schema(
    conn: DatabaseConnection, *, maintenance: bool = True, cleanup_duplicate_images_fn=None
):
    backend = conn.backend

    if backend == DB_TYPE_SQLITE:
        id_type = "INTEGER PRIMARY KEY AUTOINCREMENT"
        text_type = "TEXT"
        short_type = "TEXT"
        path_type = "TEXT"
        json_type = "TEXT"
        blob_type = "BLOB"
        real_type = "REAL"
    elif backend == DB_TYPE_POSTGRESQL:
        id_type = "BIGSERIAL PRIMARY KEY"
        text_type = "TEXT"
        short_type = "VARCHAR(255)"
        path_type = "VARCHAR(700)"
        json_type = "TEXT"
        blob_type = "BYTEA"
        real_type = "DOUBLE PRECISION"
    elif backend == DB_TYPE_MYSQL:
        id_type = "BIGINT PRIMARY KEY AUTO_INCREMENT"
        text_type = "LONGTEXT"
        short_type = "VARCHAR(255)"
        path_type = "VARCHAR(700)"
        json_type = "LONGTEXT"
        blob_type = "LONGBLOB"
        real_type = "DOUBLE"
    else:
        id_type = "BIGINT IDENTITY(1,1) PRIMARY KEY"
        text_type = "NVARCHAR(MAX)"
        short_type = "NVARCHAR(255)"
        path_type = "NVARCHAR(2048)"
        json_type = "NVARCHAR(MAX)"
        blob_type = "VARBINARY(MAX)"
        real_type = "FLOAT"

    if not table_exists(conn, "profiles"):
        conn.execute(f"""
            CREATE TABLE profiles (
                id {id_type},
                source {short_type} NOT NULL DEFAULT 'vk',
                vk_user_id BIGINT,
                screen_name {short_type},
                domain {short_type},
                first_name {text_type},
                last_name {text_type},
                maiden_name {text_type},
                bdate {text_type},
                site {short_type},
                status {short_type},
                followers_count BIGINT,
                online INTEGER,
                last_seen_time BIGINT,
                last_seen_platform INTEGER,
                verified INTEGER,
                is_verified INTEGER,
                occupation_type {short_type},
                occupation_id BIGINT,
                occupation_name {short_type},
                city_id BIGINT,
                city_title {short_type},
                home_town {short_type},
                sex INTEGER,
                has_photo INTEGER NOT NULL DEFAULT 0,
                has_mobile INTEGER,
                can_access_closed INTEGER,
                is_closed INTEGER,
                deactivated {short_type},
                profile_url {text_type},
                raw_json {json_type},
                scanned_at {text_type}
            )
        """)

    if not table_exists(conn, "images"):
        conn.execute(f"""
            CREATE TABLE images (
                id {id_type},
                path {path_type} NOT NULL UNIQUE,
                filename {short_type} NOT NULL,
                sha256 {short_type},
                width INTEGER,
                height INTEGER,
                processed INTEGER NOT NULL DEFAULT 0,
                source_type {short_type} NOT NULL DEFAULT 'local',
                source_url {path_type},
                url_image {path_type},
                profile_id BIGINT,
                source_updated_at {short_type},
                metadata_json {json_type},
                FOREIGN KEY(profile_id)
                    REFERENCES profiles(id)
                    ON DELETE SET NULL
            )
        """)

    if not table_exists(conn, "faces"):
        conn.execute(f"""
            CREATE TABLE faces (
                id {id_type},
                image_id BIGINT NOT NULL,
                x1 {real_type} NOT NULL,
                y1 {real_type} NOT NULL,
                x2 {real_type} NOT NULL,
                y2 {real_type} NOT NULL,
                det_score {real_type} NOT NULL,
                embedding {blob_type} NOT NULL,
                embedding_dim INTEGER NOT NULL,
                FOREIGN KEY(image_id)
                    REFERENCES images(id)
                    ON DELETE CASCADE
            )
        """)

    if backend == DB_TYPE_SQLITE:
        ensure_columns(conn, "profiles", PROFILE_COLUMNS)
        ensure_columns(conn, "images", IMAGE_COLUMNS)
        backfill_import_image_urls(conn)

    # Метаданные изображения добавляются и в уже существующие базы всех СУБД.
    ensure_columns(conn, "images", {"metadata_json": json_type})

    # Приводим ранее записанные плоские EXIF-объекты к единой оболочке JSON.
    # Миграция помечается как выполненная и не повторяется при каждом запуске.
    _migrate_metadata_json_to_strict_envelope(conn)

    # Имена столбцов и таблиц в этом списке заданы самим приложением.
    # Для MySQL CREATE INDEX IF NOT EXISTS не является переносимым синтаксисом:
    # запрос завершался ошибкой, которая подавлялась обработчиком ниже, и
    # индексы фактически не создавались. На больших удалённых БД это превращало
    # проверки дубликатов/профиля в полные сканирования таблиц.
    index_definitions = [
        ("idx_faces_image_id", "faces", "image_id"),
        ("idx_images_profile_id", "images", "profile_id"),
        ("idx_images_source_type", "images", "source_type"),
        ("idx_profiles_vk_user_id", "profiles", "vk_user_id"),
        ("idx_profiles_city", "profiles", "city_title"),
    ]

    if backend == DB_TYPE_MYSQL:
        # Хеш crop участвует в запросе на каждый найденный face.
        index_definitions.extend([
            ("idx_images_sha256", "images", "sha256(64)"),
            # Для потенциально LONGTEXT/VARCHAR(700) URL используем индексный
            # префикс. MySQL затем проверяет полное равенство по строке-кандидату.
            ("idx_profiles_source_profile_url", "profiles", "source(16), profile_url(150)"),
            ("idx_images_local_source_url", "images", "source_type(16), source_url(150)"),
            ("idx_images_import_profile_url", "images", "source_type(16), profile_id, url_image(150)"),
            ("idx_images_import_profile_source", "images", "source_type(16), profile_id, source_url(150)"),
        ])

    for name, table, columns in index_definitions:
        try:
            if backend == DB_TYPE_MSSQL:
                exists = conn.execute(
                    "SELECT 1 FROM sys.indexes "
                    "WHERE name = ? AND object_id = OBJECT_ID(?)",
                    (name, table),
                ).fetchone()
                if not exists:
                    conn.execute(f"CREATE INDEX {name} ON {table} ({columns})")
            elif backend == DB_TYPE_MYSQL:
                exists = conn.execute(
                    "SELECT 1 FROM information_schema.statistics "
                    "WHERE table_schema = DATABASE() "
                    "AND table_name = ? AND index_name = ? LIMIT 1",
                    (table, name),
                ).fetchone()
                if not exists:
                    conn.execute(
                        f"CREATE INDEX `{name}` ON `{table}` ({columns})"
                    )
            else:
                conn.execute(
                    f"CREATE INDEX IF NOT EXISTS {name} ON {table} ({columns})"
                )
        except Exception as exc:
            print(f"Предупреждение создания индекса {name}: {exc}")

    if maintenance:
        cleanup_duplicate_images_fn(conn) if cleanup_duplicate_images_fn else None

    # SQLite/PostgreSQL поддерживают partial unique indexes.
    # Для MySQL/MS SQL дедупликация выполняется приложением по SHA-256,
    # поэтому эти индексы не создаём.
    if backend in (DB_TYPE_SQLITE, DB_TYPE_POSTGRESQL):
        for sql in (
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_images_sha_local
            ON images(sha256)
            WHERE sha256 IS NOT NULL
              AND sha256 != ''
              AND source_type != 'vk'
            """,
            """
            CREATE UNIQUE INDEX IF NOT EXISTS ux_images_sha_vk_profile
            ON images(sha256, profile_id)
            WHERE sha256 IS NOT NULL
              AND sha256 != ''
              AND source_type = 'vk'
              AND profile_id IS NOT NULL
            """,
        ):
            try:
                conn.execute(sql)
            except Exception as exc:
                print(f"Предупреждение создания уникального индекса: {exc}")

    # Важно для массовой перезаписи CROP: до обработки каждого исходника
    # программа ищет все его существующие faces в images. Без этих индексов
    # SQLite просматривал сотни тысяч строк для каждого файла (N×M поиск).
    # Частичные индексы компактны и подходят под точные WHERE-запросы проекта.
    if backend == DB_TYPE_SQLITE:
        source_lookup_indexes = (
            """
            CREATE INDEX IF NOT EXISTS idx_images_local_source_url
            ON images(source_url)
            WHERE source_type = 'local'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_images_import_profile_url
            ON images(profile_id, url_image)
            WHERE source_type = 'import'
            """,
            """
            CREATE INDEX IF NOT EXISTS idx_images_import_profile_source
            ON images(profile_id, source_url)
            WHERE source_type = 'import'
            """,
        )
        for sql in source_lookup_indexes:
            try:
                conn.execute(sql)
            except Exception as exc:
                print(f"Предупреждение создания индекса поиска CROP: {exc}")

    conn.commit()
