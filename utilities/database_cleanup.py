# -*- coding: utf-8 -*-
"""FaceSearch — поиск и удаление профилей с зависимыми фотографиями/лицами.

Файл находится в папке utilities проекта FaceSearch.
Запуск: .venv\\Scripts\\python.exe utilities\\database_cleanup.py

Удаление затрагивает только выбранные profiles, их связанные images и faces.
Локальные CROP удаляются после успешного COMMIT и только из CROP-каталогов.
Исходные оригиналы не удаляются. После удаления требуется перестроить FAISS.
"""
from __future__ import annotations

import json
import os
import re
import sqlite3
import io
import sys
import threading
import time
import webbrowser
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
from pathlib import Path
from typing import Any, Iterable
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
import tkinter as tk
from tkinter import ttk, messagebox, simpledialog

SCRIPT_DIR = Path(__file__).resolve().parent
APP_DIR = SCRIPT_DIR.parent  # корень проекта FaceSearch
if str(APP_DIR) not in sys.path:
    sys.path.insert(0, str(APP_DIR))

try:
    from facesearch.config import (
        APP_DIR as PROJECT_DIR,
        DB_CONFIG_FILE,
        DB_TYPE_SQLITE,
        DB_TYPE_POSTGRESQL,
        DB_TYPE_MYSQL,
        DB_TYPE_MSSQL,
        DB_DEFAULT_PORTS,
    )
    from facesearch.database.connection import DatabaseConnection
    from facesearch.settings import load_module_settings, save_module_settings
except Exception as exc:
    raise SystemExit(
        "Не удалось импортировать конфигурацию FaceSearch.\n"
        "Скрипт должен находиться в папке utilities внутри проекта FaceSearch.\n\n"
        f"Подробности: {exc}"
    )

PAGE_SIZE = 250
SQL_BATCH_SIZE = 300  # безопасно для SQL Server (лимит 2100 параметров)
FETCH_SIZE = 1000

def _search_tokens(query: str) -> list[str]:
    """Разбивает запрос на слова; фрагмент в двойных кавычках ищется как фраза."""
    value = str(query or "").strip().casefold()
    if not value:
        return []
    tokens = [quoted if quoted else plain for quoted, plain in re.findall(r'"([^"]+)"|(\S+)', value)]
    # Ограничение предохраняет запросы с большим вставленным текстом от превышения
    # лимита параметров SQL Server и от неоправданно тяжёлых SQL-запросов.
    return [token.strip() for token in tokens if token.strip()][:30]


def _like_pattern(token: str) -> str:
    """Готовит фрагмент для LIKE, трактуя %, _ и ! как обычные символы."""
    escaped = token.replace("!", "!!").replace("%", "!%").replace("_", "!_")
    return f"%{escaped}%"


def _cast_type(conn) -> str:
    """Возвращает переносимый тип текстового CAST для текущей СУБД."""
    if conn.backend == DB_TYPE_MYSQL:
        return "CHAR"
    if conn.backend == DB_TYPE_MSSQL:
        return "NVARCHAR(MAX)"
    return "TEXT"


def _lower_function(conn) -> str:
    """Возвращает функцию Unicode-регистра, включая кириллицу в SQLite."""
    if conn.backend == DB_TYPE_SQLITE:
        raw = getattr(conn, "raw", None)
        if raw is not None and hasattr(raw, "create_function"):
            raw.create_function(
                "UNICODE_LOWER", 1,
                lambda value: str(value or "").casefold(),
            )
        return "UNICODE_LOWER"
    return "LOWER"


def load_profiles() -> tuple[list[dict[str, Any]], str]:
    """Загружает ровно те же DB-профили, которые использует основное приложение."""
    if not DB_CONFIG_FILE.is_file():
        raise FileNotFoundError(
            f"Не найден файл профилей БД:\n{DB_CONFIG_FILE}\n"
            "Сначала запустите FaceSearch и настройте подключение к базе."
        )
    data = json.loads(DB_CONFIG_FILE.read_text(encoding="utf-8"))
    profiles = [
        p for p in data.get("profiles", [])
        if isinstance(p, dict) and p.get("name")
    ]
    if not profiles:
        raise ValueError(f"В {DB_CONFIG_FILE} нет профилей подключения.")
    active = str(data.get("active") or profiles[0]["name"])
    names = {str(p["name"]) for p in profiles}
    if active not in names:
        active = str(profiles[0]["name"])
    return profiles, active


def open_database(profile: dict[str, Any]):
    """Открывает SQLite, PostgreSQL, MySQL или MS SQL Server тем же способом, что проект."""
    backend = str(profile.get("type") or DB_TYPE_SQLITE).lower()
    if backend == DB_TYPE_SQLITE:
        path = Path(str(profile.get("path") or PROJECT_DIR / "faces.db")).expanduser()
        if not path.is_absolute():
            path = PROJECT_DIR / path
        if not path.is_file():
            raise FileNotFoundError(f"Файл SQLite не найден:\n{path}")
        raw = sqlite3.connect(str(path), timeout=30)
        raw.execute("PRAGMA foreign_keys = ON")
    elif backend == DB_TYPE_POSTGRESQL:
        import psycopg
        raw = psycopg.connect(
            host=profile.get("host") or "localhost",
            port=int(profile.get("port") or DB_DEFAULT_PORTS[backend]),
            dbname=profile.get("database") or "",
            user=profile.get("user") or "",
            password=profile.get("password") or "",
            connect_timeout=10,
        )
    elif backend == DB_TYPE_MYSQL:
        import pymysql
        raw = pymysql.connect(
            host=profile.get("host") or "localhost",
            port=int(profile.get("port") or DB_DEFAULT_PORTS[backend]),
            database=profile.get("database") or "",
            user=profile.get("user") or "",
            password=profile.get("password") or "",
            connect_timeout=10,
            charset="utf8mb4",
            autocommit=False,
        )
    elif backend == DB_TYPE_MSSQL:
        import pyodbc
        driver = profile.get("driver") or "ODBC Driver 18 for SQL Server"
        server = profile.get("host") or "localhost"
        port = str(profile.get("port") or DB_DEFAULT_PORTS[backend])
        database = profile.get("database") or ""
        user = profile.get("user") or ""
        password = profile.get("password") or ""
        if user:
            conn_str = (
                f"DRIVER={{{driver}}};SERVER={server},{port};DATABASE={database};"
                f"UID={user};PWD={password};TrustServerCertificate=yes;"
            )
        else:
            conn_str = (
                f"DRIVER={{{driver}}};SERVER={server},{port};DATABASE={database};"
                "Trusted_Connection=yes;TrustServerCertificate=yes;"
            )
        raw = pyodbc.connect(conn_str, timeout=10)
    else:
        raise ValueError(f"Неизвестный тип БД в профиле: {backend}")

    return DatabaseConnection(raw, backend), backend


def get_columns(conn, table: str) -> list[str]:
    """Возвращает столбцы таблицы с учётом типа БД."""
    backend = conn.backend
    if backend == DB_TYPE_SQLITE:
        rows = conn.execute(f"PRAGMA table_info({table})").fetchall()
        return [str(row[1]) for row in rows]
    if backend == DB_TYPE_POSTGRESQL:
        rows = conn.execute(
            "SELECT column_name FROM information_schema.columns "
            "WHERE table_schema = current_schema() AND table_name = ? ORDER BY ordinal_position",
            (table,),
        ).fetchall()
    elif backend == DB_TYPE_MYSQL:
        rows = conn.execute(
            "SELECT COLUMN_NAME FROM information_schema.columns "
            "WHERE table_schema = DATABASE() AND table_name = ? ORDER BY ORDINAL_POSITION",
            (table,),
        ).fetchall()
    else:
        rows = conn.execute(
            "SELECT COLUMN_NAME FROM INFORMATION_SCHEMA.COLUMNS "
            "WHERE TABLE_NAME = ? AND TABLE_SCHEMA = SCHEMA_NAME() ORDER BY ORDINAL_POSITION",
            (table,),
        ).fetchall()
    return [str(row[0]) for row in rows]

def validate_schema(conn) -> tuple[set[str], set[str]]:
    pcols = set(get_columns(conn, "profiles"))
    icols = set(get_columns(conn, "images"))
    fcols = set(get_columns(conn, "faces"))
    if not pcols or not {"id", "profile_id"}.issubset(icols) or not {"image_id"}.issubset(fcols):
        raise RuntimeError(
            "В выбранной БД не найдена ожидаемая схема FaceSearch: profiles, images, faces. "
            "Проверьте тип и профиль базы данных."
        )
    if "id" not in pcols:
        raise RuntimeError("В таблице profiles отсутствует поле id.")
    return pcols, icols


def _make_filter(conn, query: str, pcols: set[str], icols: set[str]) -> tuple[str, list[Any]]:
    """Строит универсальный поиск подстрок по всем полям профиля и его изображений.

    Каждый не заключённый в кавычки фрагмент должен встретиться хотя бы в одном
    поле профиля или связанного изображения. Разные фрагменты могут находиться
    в разных полях. Строка в двойных кавычках ищется как единая подстрока.
    Имена столбцов берутся из проверенной схемы и дополнительно валидируются.
    """
    tokens = _search_tokens(query)
    if not tokens:
        return "1=1", []

    # Берём фактические столбцы схемы, а не фиксированный набор полей: так поиск
    # автоматически охватывает новые поля, добавленные будущими версиями FaceSearch.
    valid_name = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")
    profile_columns = sorted(c for c in pcols if valid_name.fullmatch(str(c)))
    image_columns = sorted(c for c in icols if valid_name.fullmatch(str(c)))
    cast_type = _cast_type(conn)
    lower_fn = _lower_function(conn)
    token_clauses: list[str] = []
    params: list[Any] = []

    for token in tokens:
        pattern = _like_pattern(token)
        alternatives: list[str] = []
        for column in profile_columns:
            alternatives.append(
                f"{lower_fn}(COALESCE(CAST(p.{column} AS {cast_type}), '')) LIKE ? ESCAPE '!'"
            )
            params.append(pattern)

        if image_columns:
            image_alternatives: list[str] = []
            for column in image_columns:
                image_alternatives.append(
                    f"{lower_fn}(COALESCE(CAST(i.{column} AS {cast_type}), '')) LIKE ? ESCAPE '!'"
                )
                params.append(pattern)
            alternatives.append(
                "EXISTS (SELECT 1 FROM images i WHERE i.profile_id = p.id AND ("
                + " OR ".join(image_alternatives)
                + "))"
            )

        if not alternatives:
            return "1=0", []
        # AND между фрагментами, OR между всеми доступными полями.
        token_clauses.append("(" + " OR ".join(alternatives) + ")")

    return "(" + " AND ".join(token_clauses) + ")", params


def count_matches(conn, where_sql: str, params: list[Any]) -> int:
    row = conn.execute(f"SELECT COUNT(*) FROM profiles p WHERE {where_sql}", tuple(params)).fetchone()
    return int(row[0] or 0)


def fetch_page(conn, where_sql: str, params: list[Any], offset: int, limit: int, pcols: set[str]):
    """Читает одну страницу результатов, не загружая всю выборку в GUI."""
    def expr(column: str) -> str:
        return f"COALESCE(p.{column}, '')" if column in pcols else "''"

    fields = [
        "p.id",
        f"{expr('source')} AS source",
        f"{expr('first_name')} AS first_name",
        f"{expr('last_name')} AS last_name",
        f"{expr('screen_name')} AS screen_name",
        f"{expr('domain')} AS domain",
        f"{expr('profile_url')} AS profile_url",
        f"{expr('city_title')} AS city_title",
        f"{expr('status')} AS status",
        "(SELECT COUNT(*) FROM images i WHERE i.profile_id = p.id) AS image_count",
        "(SELECT COUNT(*) FROM faces f JOIN images i ON i.id = f.image_id WHERE i.profile_id = p.id) AS face_count",
    ]
    sql = f"SELECT {', '.join(fields)} FROM profiles p WHERE {where_sql} ORDER BY p.id"
    if conn.backend == DB_TYPE_MSSQL:
        sql += " OFFSET ? ROWS FETCH NEXT ? ROWS ONLY"
        page_params = [*params, int(offset), int(limit)]
    else:
        sql += " LIMIT ? OFFSET ?"
        page_params = [*params, int(limit), int(offset)]
    return conn.execute(sql, tuple(page_params)).fetchall()


def fetch_matching_ids(conn, where_sql: str, params: list[Any]) -> list[int]:
    """Читает только ID профилей; используется лишь по явной команде массового выбора."""
    if hasattr(conn, "execute_streaming"):
        cursor = conn.execute_streaming(
            f"SELECT p.id FROM profiles p WHERE {where_sql} ORDER BY p.id", tuple(params)
        )
    else:
        cursor = conn.execute(f"SELECT p.id FROM profiles p WHERE {where_sql} ORDER BY p.id", tuple(params))
    ids: list[int] = []
    try:
        while True:
            rows = cursor.fetchmany(FETCH_SIZE)
            if not rows:
                break
            ids.extend(int(row[0]) for row in rows)
    finally:
        try:
            cursor.close()
        except Exception:
            pass
    return ids


def _chunks(items: list[int], size: int = SQL_BATCH_SIZE) -> Iterable[list[int]]:
    for start in range(0, len(items), size):
        yield items[start:start + size]


def _resolve_project_path(raw_path: str) -> Path:
    path = Path(str(raw_path or "")).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def profile_crop_dir(profile: dict[str, Any]) -> Path:
    value = str(profile.get("crop_path") or "").strip()
    if value:
        return _resolve_project_path(value).resolve()
    return (PROJECT_DIR / "face_images").resolve()


def resolve_crop_path(raw_path: str, crop_base: Path) -> Path | None:
    """Разрешает images.path, но не разрешает удалять файлы вне известных CROP-каталогов."""
    raw = str(raw_path or "").strip()
    if not raw:
        return None
    path = Path(raw).expanduser()
    if path.is_absolute():
        candidate = path.resolve(strict=False)
    else:
        preferred = (crop_base / path).resolve(strict=False)
        if preferred.is_file():
            candidate = preferred
        else:
            candidate = (PROJECT_DIR / path).resolve(strict=False)

    roots = [crop_base.resolve(strict=False), (PROJECT_DIR / "face_images").resolve(strict=False)]
    for root in roots:
        try:
            candidate.relative_to(root)
            return candidate
        except ValueError:
            continue
    # Не удаляем путь, похожий на оригинал/произвольный файл вне CROP.
    return None


def _count_images_and_faces(conn, ids: list[int]) -> tuple[int, int]:
    image_total = face_total = 0
    for batch in _chunks(ids):
        marks = ",".join("?" for _ in batch)
        row = conn.execute(
            f"SELECT COUNT(*) FROM images WHERE profile_id IN ({marks})", tuple(batch)
        ).fetchone()
        image_total += int(row[0] or 0)
        row = conn.execute(
            f"SELECT COUNT(*) FROM faces f JOIN images i ON i.id = f.image_id "
            f"WHERE i.profile_id IN ({marks})", tuple(batch)
        ).fetchone()
        face_total += int(row[0] or 0)
    return image_total, face_total


def delete_profile_records(conn, profile_ids: list[int], crop_base: Path) -> dict[str, Any]:
    """Транзакционно удаляет profiles -> images -> faces; CROP очищает только после COMMIT."""
    ids = sorted({int(value) for value in profile_ids})
    if not ids:
        return {"profiles": 0, "images": 0, "faces": 0, "crops": 0, "errors": []}

    images_total, faces_total = _count_images_and_faces(conn, ids)
    crop_by_raw: dict[str, Path] = {}
    deleted_profiles = 0
    try:
        for batch in _chunks(ids):
            marks = ",".join("?" for _ in batch)
            # Путь сохраняем до удаления изображений; fetchmany не держит все строки сразу.
            if hasattr(conn, "execute_streaming"):
                cursor = conn.execute_streaming(
                    f"SELECT path FROM images WHERE profile_id IN ({marks})", tuple(batch)
                )
            else:
                cursor = conn.execute(
                    f"SELECT path FROM images WHERE profile_id IN ({marks})", tuple(batch)
                )
            try:
                while True:
                    rows = cursor.fetchmany(FETCH_SIZE)
                    if not rows:
                        break
                    for row in rows:
                        raw_path = str(row[0] or "").strip()
                        if raw_path and raw_path not in crop_by_raw:
                            resolved = resolve_crop_path(raw_path, crop_base)
                            if resolved is not None:
                                crop_by_raw[raw_path] = resolved
            finally:
                try:
                    cursor.close()
                except Exception:
                    pass

            conn.execute(
                f"DELETE FROM faces WHERE image_id IN "
                f"(SELECT id FROM images WHERE profile_id IN ({marks}))",
                tuple(batch),
            )
            conn.execute(f"DELETE FROM images WHERE profile_id IN ({marks})", tuple(batch))
            result = conn.execute(f"DELETE FROM profiles WHERE id IN ({marks})", tuple(batch))
            try:
                affected = int(result.rowcount)
            except (TypeError, ValueError):
                affected = -1
            deleted_profiles += affected if affected >= 0 else len(batch)
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    # После успешного COMMIT убираем CROP, если путь больше не используется таблицей images.
    crop_deleted = 0
    file_errors: list[str] = []
    for raw_path, file_path in crop_by_raw.items():
        try:
            ref_row = conn.execute("SELECT COUNT(*) FROM images WHERE path = ?", (raw_path,)).fetchone()
            if int(ref_row[0] or 0) > 0:
                continue
            if file_path.is_file():
                file_path.unlink()
                crop_deleted += 1
        except OSError as exc:
            file_errors.append(f"{file_path}: {exc}")
        except Exception as exc:
            file_errors.append(f"{raw_path}: не удалось проверить/удалить CROP: {exc}")

    return {
        "profiles": min(deleted_profiles, len(ids)),
        "images": images_total,
        "faces": faces_total,
        "crops": crop_deleted,
        "errors": file_errors,
    }




def resolve_original_path(raw_source: str) -> Path | None:
    """Преобразует source_url локального оригинала, включая Windows/file:// URL."""
    raw = str(raw_source or "").strip()
    if not raw:
        return None
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\"):
        path = Path(raw).expanduser()
    else:
        parsed = urlparse(raw)
        scheme = parsed.scheme.lower()
        if scheme == "file":
            path_text = unquote(parsed.path or "")
            if os.name == "nt":
                if re.match(r"^/[A-Za-z]:/", path_text):
                    path_text = path_text[1:]
                path_text = url2pathname(path_text)
                if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
                    path_text = f"\\\\{parsed.netloc}{path_text}"
            else:
                if parsed.netloc and parsed.netloc.lower() not in ("", "localhost"):
                    path_text = f"//{parsed.netloc}{path_text}"
            path = Path(path_text).expanduser()
        elif not scheme:
            path = Path(raw).expanduser()
        else:
            return None
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def fetch_missing_local(conn) -> list[tuple[int, str, str, str, str]]:
    """Возвращает (image_id, filename, source_url, crop_path, original_path) для пропавших файлов."""
    rows = conn.execute(
        """
        SELECT id, filename, source_url, path
        FROM images
        WHERE LOWER(COALESCE(source_type, 'local')) = 'local'
          AND (url_image IS NULL OR TRIM(url_image) = '')
        ORDER BY id
        """
    ).fetchall()
    missing = []
    for image_id, filename, source_url, crop_path in rows:
        source_text = str(source_url or '').strip()
        original = resolve_original_path(source_text)
        if original is None:
            continue
        try:
            exists = original.is_file()
        except OSError:
            # Ошибка доступа — не доказательство отсутствия файла.
            continue
        if not exists:
            missing.append((int(image_id), str(filename or ''), source_text,
                            str(crop_path or ''), str(original)))
    return missing


def fetch_remote_candidates(conn) -> list[tuple[int, str, str, str, str, str]]:
    """Сетевые изображения: оригинал проверяем по url_image, не по URL профиля."""
    rows = conn.execute(
        """
        SELECT id, path, filename, source_type, url_image, source_url
        FROM images
        WHERE LOWER(COALESCE(source_type, 'local')) <> 'local'
          AND url_image IS NOT NULL AND TRIM(url_image) <> ''
        ORDER BY id
        """
    ).fetchall()
    return [(int(r[0]), str(r[1] or '').strip(), str(r[2] or '').strip(),
             str(r[3] or '').strip(), str(r[4] or '').strip(), str(r[5] or '').strip())
            for r in rows]


def check_remote_url(url: str) -> dict[str, Any]:
    """Два контрольных GET: удалять разрешается только при двух HTTP 404/410."""
    value = str(url or '').strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc:
        return {"label": "Пропущено: URL не HTTP(S)", "dead": False,
                "kind": "skip", "detail": "Некорректный URL"}
    confirmed = []
    last_note = ''
    last_code = None
    for attempt in range(2):
        sample = b''
        content_type = ''
        try:
            request = Request(value, headers={
                "User-Agent": ("Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                               "AppleWebKit/537.36 (KHTML, like Gecko) "
                               "Chrome/134.0.0.0 Safari/537.36"),
                "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                "Range": "bytes=0-1023",
                "Connection": "close",
            }, method="GET")
            try:
                with urlopen(request, timeout=15) as response:
                    code = int(response.getcode() or 0)
                    content_type = str(response.headers.get('Content-Type') or '').lower()
                    sample = response.read(1024)
            except HTTPError as exc:
                code = int(exc.code or 0)
                content_type = str(exc.headers.get('Content-Type') or '').lower() if exc.headers else ''
                exc.close()
            last_code = code
            if 200 <= code < 300:
                if not sample or 'text/html' in content_type or 'application/json' in content_type:
                    return {"label": f"Проверить вручную: HTTP {code}, ответ не похож на изображение",
                            "dead": False, "kind": "warning", "detail": content_type or "Пустой ответ"}
                return {"label": f"Доступен (HTTP {code})", "dead": False,
                        "kind": "ok", "detail": content_type or "Content-Type не указан"}
            if code in (404, 410):
                confirmed.append(code)
                if len(confirmed) >= 2:
                    return {"label": f"Оригинал отсутствует (HTTP {code})", "dead": True,
                            "kind": "dead", "detail": "404/410 повторился при двух запросах"}
                last_note = f"HTTP {code}; выполняется контрольный запрос"
                time.sleep(0.35)
                continue
            if code in (401, 403):
                return {"label": f"Нет доступа (HTTP {code})", "dead": False,
                        "kind": "warning", "detail": "Запрет доступа не доказывает удаление файла"}
            last_note = f"HTTP {code}"
            if (code in (408, 429) or 500 <= code < 600) and attempt == 0:
                time.sleep(0.6)
                continue
            return {"label": f"Ошибка/ограничение (HTTP {code})", "dead": False,
                    "kind": "warning", "detail": last_note}
        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_note = str(getattr(exc, 'reason', exc))[:220]
            if attempt == 0:
                time.sleep(0.6)
                continue
    if confirmed:
        return {"label": "Не подтверждено: повторный запрос не подтвердил отсутствие",
                "dead": False, "kind": "warning", "detail": last_note or str(last_code)}
    return {"label": "Ошибка сети; не удалять автоматически", "dead": False,
            "kind": "warning", "detail": last_note or f"Последний статус: {last_code}"}


def check_remote_urls(urls: list[str], progress_callback=None) -> dict[str, dict[str, Any]]:
    unique = list(dict.fromkeys(str(u or '').strip() for u in urls if str(u or '').strip()))
    results: dict[str, dict[str, Any]] = {}
    valid = []
    for url in unique:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ('http', 'https') or not parsed.netloc:
            results[url] = check_remote_url(url)
        else:
            valid.append(url)
    total = len(valid)
    if total == 0:
        if progress_callback:
            progress_callback(0, 0)
        return results
    done = 0
    with ThreadPoolExecutor(max_workers=min(16, total), thread_name_prefix='url-check') as pool:
        futures = {pool.submit(check_remote_url, url): url for url in valid}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as exc:
                results[url] = {"label": "Ошибка проверки; не удалять автоматически",
                                "dead": False, "kind": "warning", "detail": str(exc)[:220]}
            done += 1
            if progress_callback and (done % 5 == 0 or done == total):
                progress_callback(done, total)
    return results


def fetch_orphan_profile_ids(conn) -> list[int]:
    rows = conn.execute(
        "SELECT p.id FROM profiles p WHERE NOT EXISTS "
        "(SELECT 1 FROM images i WHERE i.profile_id = p.id) ORDER BY p.id"
    ).fetchall()
    return [int(r[0]) for r in rows]


def delete_images_and_orphans(conn, image_ids: list[int], crop_base: Path,
                              delete_orphan_profiles: bool = True) -> dict[str, Any]:
    """Удаляет faces/images; CROP после COMMIT и только в каталогах CROP."""
    ids = sorted({int(x) for x in image_ids})
    paths_by_raw: dict[str, Path] = {}
    images_count = faces_count = 0
    try:
        for start in range(0, len(ids), SQL_BATCH_SIZE):
            batch = ids[start:start + SQL_BATCH_SIZE]
            marks = ','.join('?' for _ in batch)
            # Путь сохраняется заранее, до удаления images.
            rows = conn.execute(f"SELECT id, path FROM images WHERE id IN ({marks})", tuple(batch)).fetchall()
            actual_ids = [int(r[0]) for r in rows]
            if not actual_ids:
                continue
            for _, raw_path in rows:
                raw = str(raw_path or '').strip()
                if raw and raw not in paths_by_raw:
                    resolved = resolve_crop_path(raw, crop_base)
                    if resolved is not None:
                        paths_by_raw[raw] = resolved
            id_marks = ','.join('?' for _ in actual_ids)
            fresult = conn.execute(f"DELETE FROM faces WHERE image_id IN ({id_marks})", tuple(actual_ids))
            try: faces_count += max(0, int(fresult.rowcount or 0))
            except Exception: pass
            iresult = conn.execute(f"DELETE FROM images WHERE id IN ({id_marks})", tuple(actual_ids))
            try: images_count += max(0, int(iresult.rowcount or 0))
            except Exception: images_count += len(actual_ids)
        orphan_count = 0
        if delete_orphan_profiles:
            result = conn.execute(
                "DELETE FROM profiles WHERE NOT EXISTS "
                "(SELECT 1 FROM images WHERE images.profile_id = profiles.id)"
            )
            try: orphan_count = max(0, int(result.rowcount or 0))
            except Exception: orphan_count = 0
        conn.commit()
    except Exception:
        try: conn.rollback()
        except Exception: pass
        raise
    crops_deleted = 0
    errors = []
    for raw, path in paths_by_raw.items():
        try:
            ref = conn.execute("SELECT COUNT(*) FROM images WHERE path = ?", (raw,)).fetchone()
            if int(ref[0] or 0) > 0:
                continue
            if path.is_file():
                path.unlink()
                crops_deleted += 1
        except Exception as exc:
            errors.append(f"{path}: {exc}")
    return {"images": images_count, "faces": faces_count, "profiles": orphan_count,
            "crops": crops_deleted, "errors": errors}


def make_scrolled_tree(parent, columns, headings, widths, selectmode='extended'):
    """Создаёт Treeview с вертикальной и горизонтальной прокруткой."""
    frame = ttk.Frame(parent)
    frame.pack(fill='both', expand=True)
    tree = ttk.Treeview(frame, columns=columns, show='headings', selectmode=selectmode)
    for col in columns:
        tree.heading(col, text=headings.get(col, col))
        tree.column(col, width=widths.get(col, 140), minwidth=60,
                     stretch=col in {'url', 'original', 'crop', 'details'})
    yscroll = ttk.Scrollbar(frame, orient='vertical', command=tree.yview)
    xscroll = ttk.Scrollbar(parent, orient='horizontal', command=tree.xview)
    tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
    tree.grid(row=0, column=0, sticky='nsew')
    yscroll.grid(row=0, column=1, sticky='ns')
    frame.rowconfigure(0, weight=1)
    frame.columnconfigure(0, weight=1)
    xscroll.pack(fill='x')
    return tree



def _popup_menu(menu, event):
    try:
        menu.tk_popup(event.x_root, event.y_root)
    finally:
        try:
            menu.grab_release()
        except tk.TclError:
            pass
    return "break"


def _copy_text_selection(widget):
    try:
        _clipboard_copy(widget, widget.get("sel.first", "sel.last"))
    except tk.TclError:
        pass


def _clipboard_copy(widget, value: str):
    """Копирует строку в системный буфер обмена."""
    widget.clipboard_clear()
    widget.clipboard_append(str(value))
    try:
        widget.update_idletasks()
    except tk.TclError:
        pass


def _attach_text_context_menu(widget, *, readonly: bool = False):
    """Контекстное меню и Ctrl+C/V/X/A для Entry/Text, включая русскую раскладку."""
    menu = tk.Menu(widget, tearoff=False)

    def copy():
        try:
            selected = widget.selection_get()
        except tk.TclError:
            return
        _clipboard_copy(widget, selected)

    def cut():
        if readonly:
            copy()
            return
        try:
            selected = widget.selection_get()
        except tk.TclError:
            return
        _clipboard_copy(widget, selected)
        try:
            widget.delete("sel.first", "sel.last")
        except tk.TclError:
            pass

    def paste():
        if readonly:
            return
        try:
            value = widget.clipboard_get()
            if isinstance(widget, tk.Entry):
                widget.insert(tk.INSERT, value)
            else:
                widget.insert(tk.INSERT, value)
        except tk.TclError:
            pass

    def select_all():
        try:
            if isinstance(widget, tk.Entry):
                widget.selection_range(0, tk.END)
                widget.icursor(tk.END)
            else:
                widget.tag_add("sel", "1.0", "end-1c")
        except tk.TclError:
            pass
        return "break"

    menu.add_command(label="Копировать", command=copy, accelerator="Ctrl+C")
    if isinstance(widget, tk.Text):
        menu.add_command(label="Копировать всё", command=lambda: _clipboard_copy(widget, widget.get("1.0", "end-1c")))
    menu.add_command(label="Вырезать", command=cut, accelerator="Ctrl+X", state="disabled" if readonly else "normal")
    menu.add_command(label="Вставить", command=paste, accelerator="Ctrl+V", state="disabled" if readonly else "normal")
    menu.add_separator()
    menu.add_command(label="Выделить всё", command=select_all, accelerator="Ctrl+A")

    def popup(event):
        try:
            if isinstance(widget, tk.Entry):
                index = widget.index(f"@{event.x}")
                if not widget.selection_present():
                    widget.icursor(index)
            elif isinstance(widget, tk.Text):
                widget.mark_set(tk.INSERT, f"@{event.x},{event.y}")
            menu.tk_popup(event.x_root, event.y_root)
        finally:
            try:
                menu.grab_release()
            except tk.TclError:
                pass
        return "break"

    def shortcuts(event):
        if not (event.state & 0x4):  # Control; keycode проверяет физическую клавишу и не зависит от раскладки
            return None
        key = event.keycode
        if key == 67:  # C
            copy()
            return "break"
        if key == 88:  # X
            cut()
            return "break"
        if key == 86:  # V
            paste()
            return "break"
        if key == 65:  # A
            return select_all()
        return None

    widget.bind("<Button-3>", popup, add="+")
    widget.bind("<Shift-F10>", lambda event: popup(event), add="+")
    widget.bind("<Control-KeyPress>", shortcuts, add="+")


def _profile_url(value: str) -> str:
    """Возвращает пригодный для браузера HTTP(S)-адрес, не подменяя домен."""
    url = str(value or "").strip()
    if not url:
        return ""
    if url.startswith("//"):
        url = "https:" + url
    elif not re.match(r"^https?://", url, flags=re.IGNORECASE):
        url = "https://" + url.lstrip("/")
    parsed = urlparse(url)
    return url if parsed.scheme in {"http", "https"} and parsed.netloc else ""


class ProfileSearchTab(ttk.Frame):
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.profiles, active = load_profiles()
        self.profile_by_name = {str(p["name"]): p for p in self.profiles}
        settings = getattr(self.winfo_toplevel(), "_saved_settings", {})
        saved_profile = str(settings.get("database_profile") or "")
        profile_value = (
            saved_profile if saved_profile in self.profile_by_name
            else active if active in self.profile_by_name
            else str(self.profiles[0]["name"])
        )
        self.profile_var = tk.StringVar(value=profile_value)
        self.query_var = tk.StringVar(value=str(settings.get("query") or ""))
        self.profile_var.trace_add("write", self._schedule_settings_save)
        self.query_var.trace_add("write", self._schedule_settings_save)
        self.status_var = tk.StringVar(
            value="Введите часть значения и нажмите «Найти». Поиск охватывает все поля профиля и связанные изображения."
        )
        self.page_var = tk.StringVar(value="Страница —")
        self.result_summary_var = tk.StringVar(value="")

        self._busy = False
        self._building_tree = False
        self._visible_ids: set[int] = set()
        self.selected_ids: set[int] = set()
        self.current_page = 0
        self.total_matches = 0
        self.last_query: str | None = None
        self.last_profile_name: str | None = None
        self._details_window = None
        self._details_body = None
        self._detail_profile_id = None
        self._detail_profile_name = None
        self._detail_query = ""
        self._detail_prev_pid = None
        self._detail_next_pid = None
        self._detail_prev_button = None
        self._detail_next_button = None
        self._detail_nav_label = None
        self._busy_widget_states = []
        self._busy_menu_states = []
        self._busy_indicator_active = False
        self._detail_busy_frame = None
        self._detail_busy_var = None
        self._detail_busy_progress = None
        self._detail_busy_indicator_active = False
        self._build_ui()
        # Поиск не запускается автоматически: пользователь сам вводит запрос
        # и нажимает «Найти», чтобы не блокировать выбор профиля на больших БД.

    def _schedule_settings_save(self, *_args):
        try:
            root = self.winfo_toplevel()
            if hasattr(root, "schedule_settings_save"):
                root.schedule_settings_save()
        except tk.TclError:
            pass

    def _build_ui(self):
        outer = ttk.Frame(self, padding=10)
        outer.pack(fill="both", expand=True)

        # Контрастная полоса заметно показывает, что запрос ещё выполняется.
        self.busy_message_var = tk.StringVar(value="Выполняется операция…")
        self.busy_frame = ttk.Frame(outer, style="Busy.TFrame", padding=(12, 8))
        self.busy_message = ttk.Label(
            self.busy_frame, textvariable=self.busy_message_var, style="Busy.TLabel"
        )
        self.busy_message.pack(side="left", fill="x", expand=True)
        self.busy_progress = ttk.Progressbar(
            self.busy_frame, mode="indeterminate", length=260,
            style="Busy.Horizontal.TProgressbar",
        )
        self.busy_progress.pack(side="right", padx=(12, 0))
        self.busy_frame.pack(fill="x", pady=(0, 7))
        self.busy_frame.pack_forget()

        self.controls_frame = ttk.Frame(outer)
        controls = self.controls_frame
        controls.pack(fill="x", pady=(0, 7))
        ttk.Label(controls, text="Профиль БД:").pack(side="left", padx=(0, 5))
        self.profile_combo = ttk.Combobox(
            controls, textvariable=self.profile_var, state="readonly",
            values=list(self.profile_by_name), width=34,
        )
        self.profile_combo.pack(side="left", padx=(0, 12))
        self.profile_combo.bind("<<ComboboxSelected>>", self._on_profile_changed)

        ttk.Label(controls, text="Поиск по полям профиля и фотографий:").pack(side="left", padx=(0, 5))
        self.query_entry = ttk.Entry(controls, textvariable=self.query_var, width=35)
        self.query_entry.pack(side="left", padx=(0, 5))
        self.query_entry.bind("<Return>", lambda _event: self.start_search())
        _attach_text_context_menu(self.query_entry)
        self.search_button = ttk.Button(controls, text="Найти", command=self.start_search)
        self.search_button.pack(side="left", padx=4)
        self.clear_query_button = ttk.Button(controls, text="✕ Очистить", command=self.clear_query)
        self.clear_query_button.pack(side="left", padx=4)

        note = ttk.Label(
            outer,
            text=(
                "Поиск подстроки по всем полям профиля и связанным фотографиям, включая числовые поля "
                "и JSON. Регистр не учитывается; слова без кавычек могут находиться в разных полях. "
                "Фразу в двойных кавычках ищет целиком как подстроку. Двойной щелчок по строке показывает "
                "все поля профиля и связанные изображения."
            ),
            wraplength=1360,
        )
        note.pack(fill="x", pady=(0, 7))

        table_frame = ttk.Frame(outer)
        table_frame.pack(fill="both", expand=True)
        columns = ("id", "source", "name", "screen", "domain", "url", "city", "status", "images", "faces")
        self.tree = ttk.Treeview(table_frame, columns=columns, show="headings", selectmode="extended")
        headings = {
            "id": "ID", "source": "Источник", "name": "Имя / фамилия", "screen": "Screen name",
            "domain": "Домен", "url": "URL профиля", "city": "Город", "status": "Статус",
            "images": "Фото", "faces": "Лица",
        }
        widths = {
            "id": 70, "source": 90, "name": 180, "screen": 130, "domain": 140,
            "url": 360, "city": 130, "status": 180, "images": 70, "faces": 70,
        }
        for col in columns:
            self.tree.heading(col, text=headings[col])
            self.tree.column(col, width=widths[col], minwidth=55, stretch=col in {"url", "status"})
        yscroll = ttk.Scrollbar(table_frame, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(outer, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.grid(row=0, column=0, sticky="nsew")
        yscroll.grid(row=0, column=1, sticky="ns")
        table_frame.rowconfigure(0, weight=1)
        table_frame.columnconfigure(0, weight=1)
        xscroll.pack(fill="x")
        self.tree.bind("<<TreeviewSelect>>", self._on_tree_select)
        self.tree.bind("<Double-1>", self.show_details)
        self.tree.bind("<Button-3>", self._profile_tree_context_menu)
        self.tree.bind("<Shift-F10>", self._profile_tree_keyboard_menu)
        self.tree.bind("<Control-KeyPress>", self._profile_tree_shortcuts, add="+")

        self.tree_menu = tk.Menu(self.tree, tearoff=False)
        self.tree_menu.add_command(label="Открыть профиль во внешнем браузере", command=self.open_selected_profile)
        self.tree_menu.add_command(label="Копировать URL профиля", command=self.copy_selected_url)
        self.tree_menu.add_command(label="Копировать выбранные строки", command=self.copy_selected_rows)
        self.tree_menu.add_command(label="Копировать значение ячейки", command=self.copy_focused_cell)

        actions = ttk.Frame(outer)
        actions.pack(fill="x", pady=(8, 4))
        self.select_page_button = ttk.Button(actions, text="Выделить страницу", command=self.select_page)
        self.select_page_button.pack(side="left", padx=(0, 5))
        self.deselect_page_button = ttk.Button(actions, text="Снять выделение страницы", command=self.deselect_page)
        self.deselect_page_button.pack(side="left", padx=5)
        self.open_profile_button = ttk.Button(actions, text="Открыть профиль в браузере", command=self.open_selected_profile)
        self.open_profile_button.pack(side="left", padx=5)
        self.delete_selected_button = ttk.Button(
            actions, text="Удалить выбранные", command=self.confirm_delete_selected
        )
        self.delete_selected_button.pack(side="left", padx=(16, 5))
        self.delete_all_button = ttk.Button(
            actions, text="Удалить ВСЕ результаты поиска…", command=self.confirm_delete_all
        )
        self.delete_all_button.pack(side="left", padx=5)
        ttk.Label(actions, textvariable=self.result_summary_var).pack(side="left", padx=15)

        paging = ttk.Frame(outer)
        paging.pack(fill="x", pady=(2, 0))
        self.prev_button = ttk.Button(paging, text="◀ Назад", command=lambda: self.change_page(-1), state="disabled")
        self.prev_button.pack(side="left")
        ttk.Label(paging, textvariable=self.page_var).pack(side="left", padx=10)
        self.next_button = ttk.Button(paging, text="Вперёд ▶", command=lambda: self.change_page(1), state="disabled")
        self.next_button.pack(side="left")
        self.status_label = ttk.Label(outer, textvariable=self.status_var, style="Status.TLabel", wraplength=1400)
        self.status_label.pack(fill="x", pady=(8, 0))

        self._busy_controls = [
            self.profile_combo, self.query_entry, self.search_button, self.clear_query_button,
            self.select_page_button, self.deselect_page_button, self.open_profile_button,
            self.delete_selected_button, self.delete_all_button, self.prev_button, self.next_button,
        ]
        self._busy_tree = self.tree

    def _profile_tree_context_menu(self, event):
        if self._busy:
            return "break"
        row = self.tree.identify_row(event.y)
        if row and row not in self.tree.selection():
            self.tree.selection_set(row)
            self.tree.focus(row)
        if row:
            self.tree_menu.tk_popup(event.x_root, event.y_root)
        return "break"

    def _profile_tree_keyboard_menu(self, event=None):
        if self._busy:
            return "break"
        row = self.tree.focus()
        if not row:
            selection = self.tree.selection()
            row = selection[0] if selection else ""
        if row:
            bbox = self.tree.bbox(row)
            x = self.tree.winfo_rootx() + (bbox[0] if bbox else 20)
            y = self.tree.winfo_rooty() + (bbox[1] + (bbox[3] if bbox else 20))
            self.tree_menu.tk_popup(x, y)
        return "break"

    def _profile_tree_shortcuts(self, event):
        if self._busy:
            return "break"
        if not (event.state & 0x4):
            return None
        if event.keycode == 67:
            self.copy_selected_rows()
            return "break"
        if event.keycode == 65:
            self.tree.selection_set(self.tree.get_children())
            return "break"
        return None

    def _selected_profile_values(self):
        selection = self.tree.selection()
        return self.tree.item(selection[0], "values") if selection else ()

    def open_selected_profile(self):
        values = self._selected_profile_values()
        if not values:
            messagebox.showinfo("Открытие профиля", "Выберите профиль в результатах поиска.", parent=self)
            return
        url = _profile_url(values[5] if len(values) > 5 else "")
        if not url:
            # Если URL отсутствует, пробуем домен из соседнего столбца.
            url = _profile_url(values[4] if len(values) > 4 else "")
        if not url:
            messagebox.showwarning("Открытие профиля", "У выбранного профиля нет корректного URL или домена.", parent=self)
            return
        try:
            webbrowser.open(url, new=2)
        except Exception as exc:
            messagebox.showerror("Открытие профиля", f"Не удалось открыть браузер:\n{exc}", parent=self)

    def copy_selected_url(self):
        values = self._selected_profile_values()
        if not values:
            return
        _clipboard_copy(self, values[5] if len(values) > 5 else "")

    def copy_selected_rows(self):
        rows = [self.tree.item(iid, "values") for iid in self.tree.selection()]
        if not rows:
            rows = [self._selected_profile_values()]
        rows = [row for row in rows if row]
        if rows:
            _clipboard_copy(self, "\n".join("\t".join(str(v or "") for v in row) for row in rows))

    def copy_focused_cell(self):
        row = self.tree.focus()
        if not row:
            selection = self.tree.selection()
            row = selection[0] if selection else ""
        if not row:
            return
        column = self.tree.identify_column(self.tree.winfo_pointerx() - self.tree.winfo_rootx())
        try:
            index = int(column[1:]) - 1
            values = self.tree.item(row, "values")
            if 0 <= index < len(values):
                _clipboard_copy(self, values[index])
                return
        except (ValueError, IndexError):
            pass
        self.copy_selected_rows()

    def _set_busy(self, busy: bool, message: str | None = None):
        if busy:
            if self._busy:
                if message:
                    self.status_var.set(message)
                    self.busy_message_var.set(message)
                    self._set_detail_busy_indicator(True, message)
                return
            self._busy = True
            if message:
                self.status_var.set(message)
            self.busy_message_var.set(self.status_var.get() or "Выполняется операция…")
            try:
                self.busy_frame.pack(fill="x", pady=(0, 7), before=self.controls_frame)
            except tk.TclError:
                self.busy_frame.pack(fill="x", pady=(0, 7))
            self.busy_progress.start(12)
            self._busy_indicator_active = True

            self._busy_widget_states = []
            for widget in self._busy_controls:
                try:
                    old_state = widget.cget("state")
                    self._busy_widget_states.append((widget, old_state))
                    widget.configure(state="disabled")
                except (tk.TclError, AttributeError):
                    pass
            try:
                self._busy_tree.state(["disabled"])
            except tk.TclError:
                pass

            self._busy_menu_states = []
            try:
                end_index = self.tree_menu.index("end")
                if end_index is not None:
                    for index in range(end_index + 1):
                        try:
                            if self.tree_menu.type(index) == "command":
                                old_state = self.tree_menu.entrycget(index, "state")
                                self._busy_menu_states.append((index, old_state))
                                self.tree_menu.entryconfigure(index, state="disabled")
                        except tk.TclError:
                            pass
            except tk.TclError:
                pass
        else:
            self._busy = False
            if self._busy_indicator_active:
                try:
                    self.busy_progress.stop()
                except tk.TclError:
                    pass
                self._busy_indicator_active = False
            try:
                self.busy_frame.pack_forget()
            except tk.TclError:
                pass
            for widget, old_state in self._busy_widget_states:
                try:
                    widget.configure(state=old_state)
                except (tk.TclError, AttributeError):
                    pass
            self._busy_widget_states.clear()
            try:
                self._busy_tree.state(["!disabled"])
            except tk.TclError:
                pass
            for index, old_state in self._busy_menu_states:
                try:
                    self.tree_menu.entryconfigure(index, state=old_state)
                except tk.TclError:
                    pass
            self._busy_menu_states.clear()
            self._refresh_paging_buttons()
        self._refresh_detail_navigation()
        self._set_detail_busy_indicator(busy, message or self.status_var.get())
        try:
            root = self.winfo_toplevel()
            if hasattr(root, "set_operation_busy"):
                root.set_operation_busy(busy)
        except tk.TclError:
            pass

    def _set_detail_busy_indicator(self, busy: bool, message: str = ""):
        frame = self._detail_busy_frame
        progress = self._detail_busy_progress
        if frame is None or progress is None:
            return
        try:
            if not frame.winfo_exists():
                return
            if busy:
                if self._detail_busy_var is not None:
                    self._detail_busy_var.set(message or "Загрузка профиля…")
                if not frame.winfo_manager():
                    frame.pack(fill="x", padx=10, pady=(8, 0), before=self._details_body)
                if not self._detail_busy_indicator_active:
                    progress.start(12)
                    self._detail_busy_indicator_active = True
            else:
                if self._detail_busy_indicator_active:
                    progress.stop()
                    self._detail_busy_indicator_active = False
                frame.pack_forget()
        except tk.TclError:
            self._detail_busy_indicator_active = False

    def _refresh_paging_buttons(self):
        if self._busy:
            return
        self.prev_button.configure(state="normal" if self.current_page > 0 else "disabled")
        max_page = max(0, (self.total_matches - 1) // PAGE_SIZE)
        self.next_button.configure(state="normal" if self.current_page < max_page else "disabled")
        self.delete_all_button.configure(
            state="normal" if self.total_matches > 0 and bool((self.last_query or "").strip()) and self.last_query == self.query_var.get().strip() and self.last_profile_name == self.profile_var.get() else "disabled"
        )
        self.delete_selected_button.configure(state="normal" if self.selected_ids else "disabled")

    def _refresh_detail_navigation(self):
        if self._detail_prev_button is None or self._detail_next_button is None:
            return
        state = "disabled" if self._busy else "normal"
        self._detail_prev_button.configure(
            state=state if self._detail_prev_pid is not None else "disabled"
        )
        self._detail_next_button.configure(
            state=state if self._detail_next_pid is not None else "disabled"
        )

    def _on_profile_changed(self, _event=None):
        # Сбрасываем старые результаты, чтобы нельзя было принять их за выборку
        # из только что выбранной БД или случайно удалить записи не из того профиля.
        self.selected_ids.clear()
        self._visible_ids.clear()
        self.current_page = 0
        self.total_matches = 0
        self.last_query = None
        self.last_profile_name = None
        self.tree.delete(*self.tree.get_children())
        self.page_var.set("Страница —")
        self.result_summary_var.set("")
        self.status_var.set("Профиль БД изменён. Введите поисковый запрос и нажмите «Найти».")
        self._refresh_paging_buttons()

    def clear_query(self):
        self.query_var.set("")
        self.selected_ids.clear()
        self._visible_ids.clear()
        self.current_page = 0
        self.total_matches = 0
        self.last_query = None
        self.last_profile_name = None
        self.tree.delete(*self.tree.get_children())
        self.page_var.set("Страница —")
        self.result_summary_var.set("")
        self.status_var.set("Поиск очищен. Введите текст или часть значения и нажмите «Найти».")
        self._refresh_paging_buttons()

    def _on_tree_select(self, _event=None):
        if self._building_tree:
            return
        visible_selected = {int(self.tree.item(iid, "values")[0]) for iid in self.tree.selection()}
        self.selected_ids.difference_update(self._visible_ids)
        self.selected_ids.update(visible_selected)
        self.result_summary_var.set(
            f"Найдено: {self.total_matches:,}; выделено: {len(self.selected_ids):,}".replace(",", " ")
        )
        self._refresh_paging_buttons()

    def _sync_selection_from_tree(self):
        if self._building_tree:
            return
        selected_now = {int(self.tree.item(iid, "values")[0]) for iid in self.tree.selection()}
        self.selected_ids.difference_update(self._visible_ids)
        self.selected_ids.update(selected_now)

    def start_search(self):
        if self._busy:
            return
        query = self.query_var.get().strip()
        if not query:
            messagebox.showinfo(
                "Поиск профилей",
                "Введите текст или часть значения для поиска. Пустой запрос не запускается, "
                "чтобы случайно не загрузить всю таблицу profiles.",
                parent=self,
            )
            self.status_var.set("Ожидается поисковый запрос.")
            return
        self.selected_ids.clear()
        self.current_page = 0
        profile_name = self.profile_var.get()
        self._set_busy(True, "⏳ Поиск профилей в базе данных. Чтение совпадений…")
        threading.Thread(target=self._search_worker, args=(profile_name, query, 0), daemon=True).start()

    def _search_worker(self, profile_name: str, query: str, offset: int):
        conn = None
        try:
            profile = self.profile_by_name[profile_name]
            conn, _backend = open_database(profile)
            pcols, icols = validate_schema(conn)
            where_sql, params = _make_filter(conn, query, pcols, icols)
            total = count_matches(conn, where_sql, params)
            rows = fetch_page(conn, where_sql, params, offset, PAGE_SIZE, pcols) if total else []
            self.after(0, lambda: self._show_page(rows, total, query, profile_name, offset))
        except Exception as exc:
            self.after(0, lambda err=str(exc): self._show_error("Ошибка поиска", err))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.after(0, lambda: self._set_busy(False))

    def _show_page(self, rows, total: int, query: str, profile_name: str, offset: int):
        self._building_tree = True
        try:
            self.tree.delete(*self.tree.get_children())
            self._visible_ids.clear()
            for row in rows:
                (pid, source, first_name, last_name, screen_name, domain, url, city, status, image_count, face_count) = row
                pid = int(pid)
                name = " ".join(str(v or "").strip() for v in (first_name, last_name)).strip()
                self.tree.insert(
                    "", "end", iid=str(pid), values=(
                        pid, source or "", name, screen_name or "", domain or "", url or "",
                        city or "", status or "", int(image_count or 0), int(face_count or 0),
                    ),
                )
                self._visible_ids.add(pid)
        finally:
            self._building_tree = False
        self.total_matches = total
        self.current_page = offset // PAGE_SIZE
        self.last_query = query
        self.last_profile_name = profile_name
        self.page_var.set(
            f"Страница {self.current_page + 1} из {max(1, (total + PAGE_SIZE - 1) // PAGE_SIZE)} · по {PAGE_SIZE} записей"
        )
        self.result_summary_var.set(
            f"Найдено: {total:,}; выделено: {len(self.selected_ids):,}".replace(",", " ")
        )
        if total:
            self.status_var.set(f"Показаны записи {offset + 1}–{offset + len(rows)} из {total}. Двойной щелчок — подробности.")
        else:
            self.status_var.set("Совпадений не найдено.")
        self._refresh_paging_buttons()

    def change_page(self, delta: int):
        if self._busy:
            return
        self._sync_selection_from_tree()
        max_page = max(0, (self.total_matches - 1) // PAGE_SIZE)
        new_page = max(0, min(max_page, self.current_page + delta))
        if new_page == self.current_page:
            return
        profile_name = self.profile_var.get()
        query = self.last_query or ""
        self._set_busy(True, f"⏳ Загрузка страницы {new_page + 1} результатов поиска…")
        threading.Thread(
            target=self._page_worker,
            args=(profile_name, query, new_page * PAGE_SIZE),
            daemon=True,
        ).start()

    def _page_worker(self, profile_name: str, query: str, offset: int):
        conn = None
        try:
            conn, _backend = open_database(self.profile_by_name[profile_name])
            pcols, icols = validate_schema(conn)
            where_sql, params = _make_filter(conn, query, pcols, icols)
            total = count_matches(conn, where_sql, params)
            rows = fetch_page(conn, where_sql, params, offset, PAGE_SIZE, pcols) if total else []
            self.after(0, lambda: self._show_page(rows, total, query, profile_name, offset))
        except Exception as exc:
            self.after(0, lambda err=str(exc): self._show_error("Ошибка чтения страницы", err))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.after(0, lambda: self._set_busy(False))

    def select_page(self):
        if self._busy:
            return
        self.tree.selection_set(self.tree.get_children())

    def deselect_page(self):
        if self._busy:
            return
        self.tree.selection_remove(self.tree.selection())

    def show_details(self, _event=None):
        selection = self.tree.selection()
        if not selection or self._busy:
            return
        pid = int(self.tree.item(selection[0], "values")[0])
        profile_name = self.profile_var.get()
        query = self.last_query if self.last_profile_name == profile_name else ""
        self._set_busy(True, f"⏳ Загрузка окна профиля ID {pid}. Чтение данных и фотографий…")
        threading.Thread(
            target=self._details_worker, args=(profile_name, pid, query or ""), daemon=True
        ).start()

    def _details_worker(self, profile_name: str, pid: int, query: str = ""):
        conn = None
        try:
            conn, _backend = open_database(self.profile_by_name[profile_name])
            pcols = get_columns(conn, "profiles")
            icols = get_columns(conn, "images")
            row = conn.execute("SELECT * FROM profiles WHERE id = ?", (pid,)).fetchone()
            profile_data = []
            image_data = []
            error = ""
            if row is None:
                error = "Профиль уже отсутствует в базе."
            else:
                # Для драйверов DB-API порядок SELECT * соответствует порядку метаданных таблицы.
                profile_data = list(zip(pcols, row))
                image_sql = (
                    "SELECT TOP 100 id, filename, source_type, source_url, url_image, path, source_key "
                    "FROM images WHERE profile_id = ? ORDER BY id"
                    if conn.backend == DB_TYPE_MSSQL else
                    "SELECT id, filename, source_type, source_url, url_image, path, source_key "
                    "FROM images WHERE profile_id = ? ORDER BY id LIMIT 100"
                )
                img_rows = conn.execute(image_sql, (pid,)).fetchall()
                images_cols = ["id", "filename", "source_type", "source_url", "url_image", "path", "source_key"]
                image_data = [list(zip(images_cols, values)) for values in img_rows]

            prev_pid = next_pid = None
            if not error and query:
                try:
                    where_sql, params = _make_filter(conn, query, set(pcols), set(icols))
                    def find_neighbor(operator: str, ordering: str):
                        if conn.backend == DB_TYPE_MSSQL:
                            sql = (
                                f"SELECT TOP 1 p.id FROM profiles p WHERE ({where_sql}) "
                                f"AND p.id {operator} ? ORDER BY p.id {ordering}"
                            )
                        else:
                            sql = (
                                f"SELECT p.id FROM profiles p WHERE ({where_sql}) "
                                f"AND p.id {operator} ? ORDER BY p.id {ordering} LIMIT 1"
                            )
                        found = conn.execute(sql, tuple(params) + (pid,)).fetchone()
                        return int(found[0]) if found else None
                    prev_pid = find_neighbor("<", "DESC")
                    next_pid = find_neighbor(">", "ASC")
                except Exception:
                    # Подробности остаются доступны, даже если нельзя вычислить соседей.
                    prev_pid = next_pid = None

            result = (pid, profile_data, image_data, error, profile_name, query, prev_pid, next_pid)
            self.after(0, lambda data=result: self._show_details_window(data))
        except Exception as exc:
            self.after(0, lambda err=str(exc): self._show_error("Ошибка чтения профиля", err))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.after(0, lambda: self._set_busy(False))

    def _navigate_details(self, direction: int):
        if self._busy or self._details_window is None:
            return
        target = self._detail_prev_pid if direction < 0 else self._detail_next_pid
        if target is None or self._detail_profile_name not in self.profile_by_name:
            return
        profile_name = self._detail_profile_name
        query = self._detail_query
        self._set_busy(True, f"⏳ Загрузка профиля ID {target}…")
        threading.Thread(
            target=self._details_worker,
            args=(profile_name, target, query),
            daemon=True,
        ).start()

    def _show_details_window(self, data):
        pid, profile_data, image_data, error, profile_name, query, prev_pid, next_pid = data
        win = self._details_window
        if win is None or not win.winfo_exists():
            win = tk.Toplevel(self)
            self._details_window = win
            win.geometry("1180x760")
            win.minsize(760, 500)

            navigation = ttk.Frame(win, padding=(10, 10, 10, 0))
            navigation.pack(fill="x")
            self._detail_prev_button = ttk.Button(
                navigation, text="◀ Назад", command=lambda: self._navigate_details(-1)
            )
            self._detail_prev_button.pack(side="left")
            self._detail_nav_label = ttk.Label(navigation, text="", anchor="center")
            self._detail_nav_label.pack(side="left", fill="x", expand=True, padx=10)
            self._detail_next_button = ttk.Button(
                navigation, text="Вперёд ▶", command=lambda: self._navigate_details(1)
            )
            self._detail_next_button.pack(side="right")

            self._detail_busy_var = tk.StringVar(master=win, value="Загрузка профиля…")
            self._detail_busy_frame = ttk.Frame(win, style="Busy.TFrame", padding=(12, 6))
            ttk.Label(
                self._detail_busy_frame, textvariable=self._detail_busy_var, style="Busy.TLabel"
            ).pack(side="left", fill="x", expand=True)
            self._detail_busy_progress = ttk.Progressbar(
                self._detail_busy_frame, mode="indeterminate", length=220,
                style="Busy.Horizontal.TProgressbar",
            )
            self._detail_busy_progress.pack(side="right", padx=(12, 0))
            self._detail_busy_frame.pack(fill="x", padx=10, pady=(8, 0))
            self._detail_busy_frame.pack_forget()

            self._details_body = ttk.Frame(win)
            self._details_body.pack(fill="both", expand=True)
            win.protocol("WM_DELETE_WINDOW", lambda w=win: self._close_details_window(w))
        else:
            for child in self._details_body.winfo_children():
                child.destroy()

        win.title(f"FaceSearch — подробности профиля ID {pid}")
        self._detail_profile_id = int(pid)
        self._detail_profile_name = profile_name
        self._detail_query = query or ""
        self._detail_prev_pid = prev_pid
        self._detail_next_pid = next_pid
        self._detail_nav_label.configure(text=f"Профиль ID {pid} · навигация по результатам поиска")
        self._refresh_detail_navigation()
        self._set_detail_busy_indicator(self._busy, self.status_var.get())
        outer = ttk.Frame(self._details_body, padding=10)
        outer.pack(fill="both", expand=True)
        win.lift()
        panes = ttk.Panedwindow(outer, orient="horizontal")
        panes.pack(fill="both", expand=True)

        details_frame = ttk.Frame(panes)
        gallery_frame = ttk.Frame(panes, padding=(8, 0, 0, 0))
        panes.add(details_frame, weight=3)
        panes.add(gallery_frame, weight=2)

        ttk.Label(details_frame, text="Данные профиля и фотографий", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 5))
        text_frame = ttk.Frame(details_frame)
        text_frame.pack(fill="both", expand=True)
        text = tk.Text(text_frame, wrap="word", undo=False)
        scroll = ttk.Scrollbar(text_frame, orient="vertical", command=text.yview)
        text.configure(yscrollcommand=scroll.set)
        text.pack(side="left", fill="both", expand=True)
        scroll.pack(side="right", fill="y")
        if error:
            text.insert("end", error)
        else:
            text.insert("end", f"ПРОФИЛЬ ID {pid}\n" + "=" * 78 + "\n")
            for key, value in profile_data:
                if value is None or value == "":
                    continue
                text.insert("end", f"{key}: {value}\n")
            text.insert("end", "\nСВЯЗАННЫЕ ФОТОГРАФИИ (максимум 100)\n" + "=" * 78 + "\n")
            if not image_data:
                text.insert("end", "Связанных изображений нет.\n")
            for i, image in enumerate(image_data, 1):
                text.insert("end", f"\n[{i}]\n")
                for key, value in image:
                    if value not in (None, ""):
                        text.insert("end", f"{key}: {value}\n")
        text.configure(state="disabled")
        _attach_text_context_menu(text, readonly=True)
        text.bind("<Control-KeyPress>", lambda event: "break" if event.state & 0x4 and event.keycode in (65, 86, 88) else None, add="+")

        ttk.Label(gallery_frame, text=f"Быстрый просмотр · {len(image_data)} фото", font=("Segoe UI", 10, "bold")).pack(anchor="w", pady=(0, 5))
        ttk.Label(gallery_frame, text="Нажмите на миниатюру, чтобы открыть увеличенный просмотр.", wraplength=300).pack(anchor="w", pady=(0, 6))
        gallery_canvas = tk.Canvas(gallery_frame, highlightthickness=0, width=340)
        gallery_scroll = ttk.Scrollbar(gallery_frame, orient="vertical", command=gallery_canvas.yview)
        gallery_inner = ttk.Frame(gallery_canvas)
        gallery_window = gallery_canvas.create_window((0, 0), window=gallery_inner, anchor="nw")
        gallery_canvas.configure(yscrollcommand=gallery_scroll.set)
        gallery_canvas.pack(side="left", fill="both", expand=True)
        gallery_scroll.pack(side="right", fill="y")
        gallery_inner.bind("<Configure>", lambda _e: gallery_canvas.configure(scrollregion=gallery_canvas.bbox("all")))
        gallery_canvas.bind("<Configure>", lambda e: gallery_canvas.itemconfigure(gallery_window, width=e.width))

        try:
            preview_crop_base = profile_crop_dir(self.profile_by_name[profile_name])
        except Exception:
            preview_crop_base = (PROJECT_DIR / "face_images").resolve()

        win._preview_images = []
        for index, image_data_item in enumerate(image_data, 1):
            image_values = dict(image_data_item)
            card = ttk.Frame(gallery_inner, padding=6)
            card.pack(fill="x", pady=4)
            filename = str(image_values.get("filename") or f"Изображение {index}")
            ttk.Label(card, text=f"{index}. {filename}", wraplength=300).pack(anchor="w")
            preview_label = ttk.Label(card, text="Загрузка миниатюры…", anchor="center")
            preview_label.pack(fill="x", pady=(4, 2))
            ttk.Label(card, text=f"Image ID: {image_values.get('id', '—')}").pack(anchor="w")
            preview_label.configure(cursor="hand2")

            def load_preview(item=dict(image_values), label=preview_label, num=index, parent=win):
                try:
                    from PIL import Image, ImageTk
                    raw_path = str(item.get("path") or "").strip()
                    source_url = str(item.get("url_image") or "").strip()
                    original_url = str(item.get("source_url") or "").strip()
                    image = None
                    candidates = []
                    if raw_path:
                        candidate = Path(raw_path).expanduser()
                        if not candidate.is_absolute():
                            candidates.append(preview_crop_base / candidate)
                            candidate = PROJECT_DIR / candidate
                        candidates.append(candidate)
                        if item.get("path"):
                            candidates.append(PROJECT_DIR / "face_images" / str(item["path"]))
                    for candidate in candidates:
                        try:
                            if candidate.is_file():
                                image = Image.open(candidate)
                                image.load()
                                break
                        except Exception:
                            continue
                    if image is None:
                        for url in (source_url, original_url):
                            if not url.lower().startswith(("http://", "https://")):
                                continue
                            try:
                                request = Request(url, headers={"User-Agent": "FaceSearch/1.0"})
                                with urlopen(request, timeout=8) as response:
                                    payload = response.read(12 * 1024 * 1024 + 1)
                                if len(payload) > 12 * 1024 * 1024:
                                    continue
                                image = Image.open(io.BytesIO(payload))
                                image.load()
                                break
                            except Exception:
                                continue
                    if image is None:
                        raise OSError("Файл недоступен и загрузить изображение по URL не удалось.")
                    image = image.convert("RGB")
                    original_size = image.size
                    image.thumbnail((300, 190), Image.Resampling.LANCZOS)
                    def show_thumb():
                        try:
                            thumb = ImageTk.PhotoImage(image.copy(), master=parent)
                            parent._preview_images.append(thumb)
                            label.configure(image=thumb, text="", compound="center")
                            label.bind(
                                "<Button-1>",
                                lambda _event, item=dict(item), db_name=profile_name:
                                    self._open_large_preview(parent, item, db_name),
                            )
                            label.configure(cursor="hand2")
                            label.image = thumb
                            label._original_size = original_size
                        except tk.TclError:
                            pass
                    parent.after(0, show_thumb)
                except Exception as exc:
                    parent.after(0, lambda e=str(exc): label.configure(text=f"Предпросмотр недоступен\n{e[:120]}", wraplength=300))

            threading.Thread(target=load_preview, daemon=True, name=f"ProfilePreview-{pid}-{index}").start()

        try:
            self.winfo_toplevel().apply_theme(self.winfo_toplevel().theme_state["value"], persist=False)
        except Exception:
            pass

    def _close_details_window(self, win):
        if self._details_window is win:
            self._details_window = None
            self._details_body = None
            self._detail_prev_button = None
            self._detail_next_button = None
            self._detail_nav_label = None
            if self._detail_busy_indicator_active and self._detail_busy_progress is not None:
                try:
                    self._detail_busy_progress.stop()
                except tk.TclError:
                    pass
            self._detail_busy_frame = None
            self._detail_busy_var = None
            self._detail_busy_progress = None
            self._detail_busy_indicator_active = False
        try:
            win.destroy()
        except tk.TclError:
            pass

    def _open_large_preview(self, parent, image_item, profile_name=None):
        """Открывает изображение в отдельном окне; сетевые данные загружаются в фоне."""
        if self._busy:
            return
        win = tk.Toplevel(parent)
        win.title(f"FaceSearch — просмотр изображения {image_item.get('id', '')}")
        win.geometry("1000x760")
        win.minsize(480, 360)
        body = ttk.Frame(win, padding=8)
        body.pack(fill="both", expand=True)
        image_label = ttk.Label(body, text="Загрузка изображения…", anchor="center")
        image_label.pack(fill="both", expand=True)
        caption = ttk.Label(body, text=str(image_item.get("filename") or image_item.get("url_image") or "Изображение"), wraplength=900)
        caption.pack(fill="x", pady=(6, 0))
        win._full_preview_images = []

        # preview_crop_base внутри метода должен вычисляться заново:
        # локальная переменная из метода, открывшего галерею, сюда не передаётся.
        try:
            profile = self.profile_by_name.get(profile_name or self._detail_profile_name or self.profile_var.get())
            preview_crop_base = profile_crop_dir(profile) if profile else (PROJECT_DIR / "face_images").resolve()
        except Exception:
            preview_crop_base = (PROJECT_DIR / "face_images").resolve()

        def worker():
            try:
                from PIL import Image, ImageTk
                image = None
                raw_path = str(image_item.get("path") or "").strip()
                candidates = []
                if raw_path:
                    path = Path(raw_path).expanduser()
                    if not path.is_absolute():
                        candidates.append(preview_crop_base / path)
                        path = PROJECT_DIR / path
                    candidates.append(path)
                    candidates.append(PROJECT_DIR / "face_images" / raw_path)
                for path in candidates:
                    try:
                        if path.is_file():
                            image = Image.open(path)
                            image.load()
                            break
                    except Exception:
                        continue
                if image is None:
                    for url in (str(image_item.get("url_image") or ""), str(image_item.get("source_url") or "")):
                        if not url.lower().startswith(("http://", "https://")):
                            continue
                        try:
                            request = Request(url, headers={"User-Agent": "FaceSearch/1.0"})
                            with urlopen(request, timeout=10) as response:
                                payload = response.read(20 * 1024 * 1024 + 1)
                            if len(payload) > 20 * 1024 * 1024:
                                continue
                            image = Image.open(io.BytesIO(payload))
                            image.load()
                            break
                        except Exception:
                            continue
                if image is None:
                    raise OSError("Не удалось прочитать локальный файл или загрузить изображение.")
                original_size = image.size
                image = image.convert("RGB")
                image.thumbnail((1500, 1100), Image.Resampling.LANCZOS)
                def show():
                    try:
                        photo = ImageTk.PhotoImage(image, master=win)
                        win._full_preview_images.append(photo)
                        image_label.configure(image=photo, text="")
                        caption.configure(text=f"{image_item.get('filename') or 'Изображение'} · исходный размер {original_size[0]} × {original_size[1]}")
                    except tk.TclError:
                        pass
                win.after(0, show)
            except Exception as exc:
                win.after(0, lambda e=str(exc): image_label.configure(text=f"Предпросмотр недоступен:\n{e}", wraplength=700))
        threading.Thread(target=worker, daemon=True, name="FullImagePreview").start()
        try:
            self.winfo_toplevel().apply_theme(self.winfo_toplevel().theme_state["value"], persist=False)
        except Exception:
            pass

    def confirm_delete_selected(self):
        if self._busy:
            return
        self._sync_selection_from_tree()
        ids = sorted(self.selected_ids)
        if not ids:
            messagebox.showinfo("Удаление профилей", "Сначала выделите одну или несколько строк.", parent=self)
            return
        if not messagebox.askyesno(
            "Подтверждение удаления",
            f"Будет удалено выбранных профилей: {len(ids):,}.\n"
            "Также будут удалены связанные строки images и faces.\n"
            "Локальные CROP-файлы удаляются только из каталогов CROP; оригиналы не удаляются.\n\n"
            "Операция необратима. Продолжить?".replace(",", " "),
            icon="warning", parent=self,
        ):
            return
        self._start_delete(ids=ids, query=None)

    def confirm_delete_all(self):
        if self._busy:
            return
        query = self.query_var.get().strip()
        if not query or self.last_query != query or self.last_profile_name != self.profile_var.get():
            messagebox.showwarning(
                "Требуется поиск", "Сначала выполните поиск и убедитесь, что отображается актуальная выборка.", parent=self
            )
            return
        if not self.total_matches:
            return
        answer = messagebox.askyesno(
            "Массовое удаление профилей",
            f"Текущий запрос: {query}\n"
            f"Будет удалено ВСЕХ совпадающих профилей: {self.total_matches:,}.\n\n"
            "Для каждого профиля будут удалены связанные images и faces. Локальные CROP-файлы "
            "будут удалены только внутри CROP-каталогов. Исходные оригиналы не удаляются.\n\n"
            "Операция необратима. Перейти к дополнительному подтверждению?".replace(",", " "),
            icon="warning", parent=self,
        )
        if not answer:
            return
        typed = simpledialog.askstring(
            "Дополнительное подтверждение",
            f"Чтобы удалить {self.total_matches} профилей, введите слово УДАЛИТЬ:",
            parent=self,
        )
        if typed is None or typed.strip().upper() != "УДАЛИТЬ":
            messagebox.showinfo("Удаление отменено", "Подтверждение не совпало. Данные не изменены.", parent=self)
            return
        self._start_delete(ids=None, query=query)

    def _start_delete(self, ids: list[int] | None, query: str | None):
        profile_name = self.profile_var.get()
        profile = self.profile_by_name[profile_name]
        self._set_busy(True)
        self.status_var.set("Удаление записей в транзакции БД…")
        threading.Thread(
            target=self._delete_worker, args=(profile_name, ids, query), daemon=True
        ).start()

    def _delete_worker(self, profile_name: str, ids: list[int] | None, query: str | None):
        conn = None
        try:
            profile = self.profile_by_name[profile_name]
            conn, _backend = open_database(profile)
            pcols, icols = validate_schema(conn)
            if ids is None:
                where_sql, params = _make_filter(conn, str(query or ""), pcols, icols)
                target_ids = fetch_matching_ids(conn, where_sql, params)
            else:
                target_ids = list(ids)
            crop_base = profile_crop_dir(profile)
            result = delete_profile_records(conn, target_ids, crop_base)
            index_path = str(profile.get("index_path") or "").strip()
            self.after(0, lambda r=result, q=query, ip=index_path: self._delete_finished(r, q, ip))
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            self.after(0, lambda err=str(exc): self._show_error("Ошибка удаления", err))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.after(0, lambda: self._set_busy(False))

    def _delete_finished(self, result: dict[str, Any], old_query: str | None, index_path: str):
        self.selected_ids.clear()
        summary = (
            "Удаление завершено.\n\n"
            f"Профилей удалено: {result['profiles']}\n"
            f"Записей images удалено: {result['images']}\n"
            f"Записей faces удалено: {result['faces']}\n"
            f"Локальных CROP-файлов удалено: {result['crops']}\n\n"
            "Важно: индекс FAISS содержит прежние векторы и должен быть пересоздан "
            "в FaceSearch кнопкой «Обновить индекс» до продолжения поиска."
        )
        if index_path:
            summary += f"\n\nПуть индекса профиля:\n{index_path}"
        if result["errors"]:
            summary += "\n\nОшибки удаления отдельных CROP-файлов:\n" + "\n".join(result["errors"][:20])
            if len(result["errors"]) > 20:
                summary += f"\n… и ещё {len(result['errors']) - 20} ошибок."
        messagebox.showinfo("Результат удаления", summary, parent=self)
        self.current_page = 0
        # Worker posts this callback before its finally-block unlocks the window.
        # Defer refresh so _set_busy(False) has already run.
        self.after(100, self.start_search)

    def _show_error(self, title: str, error: str):
        self.status_var.set(f"Ошибка: {error}")
        messagebox.showerror(title, error, parent=self)


class LocalCleanupTab(ttk.Frame):
    """Вкладка поиска локальных оригиналов, отсутствующих на диске."""
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.profiles, active = load_profiles()
        self.profile_by_name = {str(p['name']): p for p in self.profiles}
        self.profile_var = tk.StringVar(value=active if active in self.profile_by_name else str(self.profiles[0]['name']))
        self.delete_orphans_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value='Проверьте локальные оригиналы, которых больше нет на диске.')
        self.missing: list[tuple[int, str, str, str, str]] = []
        self.busy = False
        self._build_ui()

    def _build_ui(self):
        controls = ttk.Frame(self); controls.pack(fill='x', pady=(0, 8))
        ttk.Label(controls, text='Профиль БД:').pack(side='left')
        self.combo = ttk.Combobox(controls, textvariable=self.profile_var, values=list(self.profile_by_name), state='readonly', width=34)
        self.combo.pack(side='left', padx=6)
        self.check_btn = ttk.Button(controls, text='Проверить локальные файлы', command=self.start_check)
        self.check_btn.pack(side='left', padx=4)
        self.delete_btn = ttk.Button(controls, text='Удалить выбранные', command=self.delete_selected, state='disabled')
        self.delete_btn.pack(side='left', padx=4)
        ttk.Checkbutton(controls, text='Также удалить профили без фотографий', variable=self.delete_orphans_var).pack(side='left', padx=10)
        ttk.Label(self, text='Проверяется только source_type=local при пустом url_image. Исходный файл читается из source_url, CROP — из path. Недоступные пути с ошибками доступа пропускаются.', wraplength=1200).pack(fill='x', pady=(0, 6))
        self.tree = make_scrolled_tree(self,
            ('id','filename','original','crop'),
            {'id':'Image ID','filename':'Файл','original':'Путь оригинала (source_url)','crop':'Путь CROP (images.path)'},
            {'id':75,'filename':200,'original':440,'crop':360})
        self.status = ttk.Label(self, textvariable=self.status_var); self.status.pack(fill='x', pady=(8, 0))

    def start_check(self):
        if self.busy: return
        self.busy = True; self.check_btn.configure(state='disabled'); self.delete_btn.configure(state='disabled')
        self.status_var.set('Подключение к БД и проверка локальных файлов…')
        self.tree.delete(*self.tree.get_children())
        name = self.profile_var.get()
        include_orphans = self.delete_orphans_var.get()
        threading.Thread(target=self._check_worker, args=(name, include_orphans), daemon=True).start()

    def _check_worker(self, name, include_orphans):
        conn = None
        try:
            conn, _ = open_database(self.profile_by_name[name])
            missing = fetch_missing_local(conn)
            orphans = fetch_orphan_profile_ids(conn) if include_orphans else []
            self.after(0, lambda: self._show_results(missing, orphans))
        except Exception as exc:
            self.after(0, lambda e=str(exc): messagebox.showerror('Ошибка проверки', e, parent=self))
            self.after(0, lambda e=str(exc): self.status_var.set(f'Ошибка: {e}'))
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
            self.after(0, self._finish_check)

    def _show_results(self, missing, orphans):
        self.missing = missing
        for image_id, filename, source, crop, original in missing:
            self.tree.insert('', 'end', iid=str(image_id), values=(image_id, filename, original, crop))
        self.status_var.set(f'Найдено отсутствующих локальных оригиналов: {len(missing):,}; профилей без изображений: {len(orphans):,}.'.replace(',', ' '))
        self.delete_btn.configure(state='normal' if missing or orphans else 'disabled')

    def _finish_check(self):
        self.busy = False; self.check_btn.configure(state='normal'); self.combo.configure(state='readonly')

    def delete_selected(self):
        if self.busy: return
        selected = [int(iid) for iid in self.tree.selection()]
        if not selected and not self.delete_orphans_var.get():
            messagebox.showinfo('Очистка', 'Выделите хотя бы одну запись.', parent=self); return
        if not messagebox.askyesno('Подтверждение очистки',
            f'Будут удалены выбранные отсутствующие изображения: {len(selected)}.\n'
            'Также будут удалены связанные faces и локальные CROP-файлы, если CROP больше не используется.\n'
            + ('Профили без оставшихся изображений также будут удалены.\n' if self.delete_orphans_var.get() else '')
            + '\nОригиналы не удаляются. Продолжить?', icon='warning', parent=self): return
        self.busy = True; self.check_btn.configure(state='disabled'); self.delete_btn.configure(state='disabled')
        name = self.profile_var.get(); ids = selected; orphan = self.delete_orphans_var.get()
        threading.Thread(target=self._delete_worker, args=(name, ids, orphan), daemon=True).start()
        self.status_var.set('Удаление связанных записей…')

    def _delete_worker(self, name, ids, orphan):
        conn = None
        try:
            conn, _ = open_database(self.profile_by_name[name])
            # Повторно убеждаемся, что удаляются только оригиналы, которые по-прежнему отсутствуют.
            still_missing = {r[0] for r in fetch_missing_local(conn)}
            ids = [i for i in ids if i in still_missing]
            result = delete_images_and_orphans(conn, ids, profile_crop_dir(self.profile_by_name[name]), orphan)
            self.after(0, lambda r=result: self._delete_done(r))
        except Exception as exc:
            if conn is not None:
                try: conn.rollback()
                except Exception: pass
            self.after(0, lambda e=str(exc): messagebox.showerror('Ошибка удаления', e, parent=self))
            self.after(0, lambda e=str(exc): self.status_var.set(f'Ошибка: {e}'))
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
            self.after(0, self._finish_check)

    def _delete_done(self, r):
        text = (f"Удаление завершено.\n\nImages: {r['images']}\nFaces: {r['faces']}\n"
                f"Профилей без фотографий: {r['profiles']}\nCROP-файлов удалено: {r['crops']}\n\n"
                "Если удалены лица, перестройте индекс FAISS в FaceSearch.")
        if r['errors']: text += '\n\nОшибки CROP:\n' + '\n'.join(r['errors'][:10])
        messagebox.showinfo('Результат очистки', text, parent=self)
        self.start_check()


class RemoteCleanupTab(ttk.Frame):
    """Вкладка проверки URL сетевых изображений; удаляются только подтверждённые 404/410."""
    def __init__(self, master):
        super().__init__(master, padding=10)
        self.profiles, active = load_profiles()
        self.profile_by_name = {str(p['name']): p for p in self.profiles}
        self.profile_var = tk.StringVar(value=active if active in self.profile_by_name else str(self.profiles[0]['name']))
        self.delete_orphans_var = tk.BooleanVar(value=True)
        self.status_var = tk.StringVar(value='Проверьте URL оригиналов сетевых изображений.')
        self.progress_var = tk.StringVar(value='')
        self.candidates: list[tuple[int, str, str, str, str, str]] = []
        self.results_by_id: dict[int, dict[str, Any]] = {}
        self.busy = False
        self._build_ui()

    def _build_ui(self):
        controls = ttk.Frame(self); controls.pack(fill='x', pady=(0, 8))
        ttk.Label(controls, text='Профиль БД:').pack(side='left')
        self.combo = ttk.Combobox(controls, textvariable=self.profile_var, values=list(self.profile_by_name), state='readonly', width=34)
        self.combo.pack(side='left', padx=6)
        self.check_btn = ttk.Button(controls, text='Проверить URL', command=self.start_check)
        self.check_btn.pack(side='left', padx=4)
        self.delete_btn = ttk.Button(controls, text='Удалить выбранные 404/410', command=self.delete_selected, state='disabled')
        self.delete_btn.pack(side='left', padx=4)
        self.delete_all_btn = ttk.Button(controls, text='Удалить ВСЕ подтверждённые…', command=self.delete_all_dead, state='disabled')
        self.delete_all_btn.pack(side='left', padx=4)
        ttk.Checkbutton(controls, text='Также удалить профили без фотографий', variable=self.delete_orphans_var).pack(side='left', padx=8)
        ttk.Label(self, text='Проверяется прямой URL оригинала images.url_image. Запрет доступа, HTML-ответы, лимиты и сетевые ошибки не считаются доказательством удаления. Удаляются только URL, для которых два запроса подряд вернули HTTP 404 или 410.', wraplength=1300).pack(fill='x', pady=(0, 6))
        self.tree = make_scrolled_tree(self,
            ('id','filename','source_type','url','profile_url','result','details'),
            {'id':'Image ID','filename':'Файл','source_type':'Источник','url':'URL оригинала','profile_url':'URL страницы','result':'Результат проверки','details':'Подробности'},
            {'id':75,'filename':180,'source_type':100,'url':420,'profile_url':280,'result':260,'details':300})
        foot = ttk.Frame(self); foot.pack(fill='x', pady=(6, 0))
        ttk.Label(foot, textvariable=self.progress_var).pack(side='left')
        ttk.Label(foot, textvariable=self.status_var).pack(side='left', padx=14)

    def start_check(self):
        if self.busy: return
        self.busy = True; self.check_btn.configure(state='disabled'); self.delete_btn.configure(state='disabled'); self.delete_all_btn.configure(state='disabled')
        self.tree.delete(*self.tree.get_children()); self.results_by_id.clear(); self.candidates.clear()
        self.status_var.set('Чтение URL из БД…'); self.progress_var.set('')
        name = self.profile_var.get()
        threading.Thread(target=self._check_worker, args=(name,), daemon=True).start()

    def _check_worker(self, name):
        conn = None
        try:
            conn, _ = open_database(self.profile_by_name[name])
            candidates = fetch_remote_candidates(conn)
            self.after(0, lambda c=candidates: self._candidates_loaded(c))
            def progress(done, total):
                self.after(0, lambda d=done, t=total: self.progress_var.set(f'Проверено URL: {d:,} / {t:,}'.replace(',', ' ')))
            by_url = check_remote_urls([r[4] for r in candidates], progress)
            results = {r[0]: by_url.get(r[4], {"label":"Не проверен", "dead":False, "kind":"warning", "detail":"Нет результата"}) for r in candidates}
            self.after(0, lambda c=candidates, rs=results: self._show_results(c, rs))
        except Exception as exc:
            self.after(0, lambda e=str(exc): messagebox.showerror('Ошибка проверки URL', e, parent=self))
            self.after(0, lambda e=str(exc): self.status_var.set(f'Ошибка: {e}'))
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
            self.after(0, self._finish_check)

    def _candidates_loaded(self, candidates):
        self.candidates = candidates
        self.status_var.set(f'Загружено сетевых изображений: {len(candidates):,}. Проверка URL…'.replace(',', ' '))

    def _show_results(self, candidates, results):
        self.candidates = candidates; self.results_by_id = results
        for row in candidates:
            iid, crop, filename, source_type, url, profile_url = row
            result = results.get(iid, {})
            self.tree.insert('', 'end', iid=str(iid), values=(iid, filename, source_type, url, profile_url,
                              result.get('label','Не проверен'), result.get('detail','')))
        dead = sum(1 for v in results.values() if v.get('dead'))
        ok = sum(1 for v in results.values() if v.get('kind') == 'ok')
        self.status_var.set(f'Завершено. Всего: {len(candidates):,}; доступны: {ok:,}; подтверждённо отсутствуют: {dead:,}.'.replace(',', ' '))
        self.delete_btn.configure(state='normal' if dead else 'disabled')
        self.delete_all_btn.configure(state='normal' if dead else 'disabled')

    def _finish_check(self):
        self.busy = False; self.check_btn.configure(state='normal'); self.combo.configure(state='readonly')

    def _selected_dead_ids(self):
        selected = [int(iid) for iid in self.tree.selection()]
        dead = [i for i in selected if self.results_by_id.get(i, {}).get('dead')]
        nondead = len(selected) - len(dead)
        if nondead:
            messagebox.showwarning('Выборка', f'Пропущено строк без подтверждённого 404/410: {nondead}.', parent=self)
        return dead

    def delete_selected(self):
        ids = self._selected_dead_ids()
        if not ids:
            messagebox.showinfo('Очистка URL', 'Выделите строки со статусом «Оригинал отсутствует (HTTP 404/410)».', parent=self)
            return
        if not messagebox.askyesno('Подтверждение удаления',
                f'Будут удалены подтверждённо отсутствующие оригиналы: {len(ids)}.\n'
                'Также будут удалены связанные faces, images и неиспользуемые CROP-файлы.\n'
                + ('Профили без оставшихся изображений также будут удалены.\n' if self.delete_orphans_var.get() else '')
                + '\nИсходники и профили на сайтах не удаляются. Продолжить?', icon='warning', parent=self): return
        self._start_delete(ids)

    def delete_all_dead(self):
        ids = [iid for iid, result in self.results_by_id.items() if result.get('dead')]
        if not ids: return
        if not messagebox.askyesno('Массовая очистка URL',
                f'Найдено подтверждённо отсутствующих оригиналов: {len(ids)}.\n\nУдалить все эти записи вместе с faces и images?',
                icon='warning', parent=self): return
        typed = simpledialog.askstring('Дополнительное подтверждение',
                f'Для удаления {len(ids)} записей введите УДАЛИТЬ:', parent=self)
        if typed is None or typed.strip().upper() != 'УДАЛИТЬ':
            messagebox.showinfo('Отменено', 'Подтверждение не совпало. Данные не изменены.', parent=self); return
        self._start_delete(ids)

    def _start_delete(self, ids):
        if self.busy: return
        self.busy = True; self.check_btn.configure(state='disabled'); self.delete_btn.configure(state='disabled'); self.delete_all_btn.configure(state='disabled')
        self.status_var.set('Удаление подтверждённых записей…')
        name = self.profile_var.get(); ids = list(ids); orphan = self.delete_orphans_var.get()
        threading.Thread(target=self._delete_worker, args=(name, ids, orphan), daemon=True).start()

    def _delete_worker(self, name, ids, orphan):
        conn = None
        try:
            conn, _ = open_database(self.profile_by_name[name])
            # Не удаляем строку, если URL после проверки изменился или запись уже исчезла.
            current = {}
            for start in range(0, len(ids), SQL_BATCH_SIZE):
                batch = ids[start:start+SQL_BATCH_SIZE]; marks = ','.join('?' for _ in batch)
                rows = conn.execute(f'SELECT id, url_image FROM images WHERE id IN ({marks})', tuple(batch)).fetchall()
                current.update({int(r[0]): str(r[1] or '').strip() for r in rows})
            candidate_by_id = {r[0]: r[4] for r in self.candidates}
            valid_ids = [i for i in ids if i in current and current[i] == candidate_by_id.get(i)
                         and self.results_by_id.get(i, {}).get('dead')]
            result = delete_images_and_orphans(conn, valid_ids, profile_crop_dir(self.profile_by_name[name]), orphan)
            self.after(0, lambda r=result, n=len(valid_ids): self._delete_done(r, n))
        except Exception as exc:
            if conn is not None:
                try: conn.rollback()
                except Exception: pass
            self.after(0, lambda e=str(exc): messagebox.showerror('Ошибка удаления', e, parent=self))
            self.after(0, lambda e=str(exc): self.status_var.set(f'Ошибка: {e}'))
        finally:
            if conn is not None:
                try: conn.close()
                except Exception: pass
            self.after(0, self._finish_check)

    def _delete_done(self, r, checked_count):
        text = (f'Удаление завершено.\n\nПодтверждённых строк к удалению: {checked_count}\n'
                f"Images удалено: {r['images']}\nFaces удалено: {r['faces']}\n"
                f"Профилей без изображений удалено: {r['profiles']}\nCROP-файлов удалено: {r['crops']}\n\n"
                'Перед дальнейшим поиском перестройте индекс FAISS.')
        if r['errors']: text += '\n\nОшибки CROP:\n' + '\n'.join(r['errors'][:10])
        messagebox.showinfo('Результат очистки URL', text, parent=self)
        self.after(100, self.start_check)


class CleanupSuite(tk.Tk):
    """Главное окно и единая дневная/ночная тема, как в загрузчике VK."""
    SETTINGS_PATH = SCRIPT_DIR / "database_cleanup_settings.xml"

    PALETTES = {
        "light": {
            "app": "#F4F5F7", "panel": "#FFFFFF", "field": "#FFFFFF",
            "text": "#202124", "muted": "#62666B", "border": "#C8CFD8",
            "accent": "#1479C9", "accent_active": "#0F6DB6",
            "button": "#E5E7EB", "button_active": "#D1D5DB",
            "selection": "#D6E8F8", "disabled": "#8A9098",
        },
        "dark": {
            "app": "#151515", "panel": "#242424", "field": "#1C1C1C",
            "text": "#EEEEEE", "muted": "#AAAAAA", "border": "#383838",
            "accent": "#167AC6", "accent_active": "#1989DC",
            "button": "#333333", "button_active": "#444444",
            "selection": "#244A68", "disabled": "#777777",
        },
    }

    def __init__(self):
        super().__init__()
        self.title("FaceSearch — инструменты очистки базы")
        self.minsize(1120, 640)
        self._settings_after_id = None
        self._settings_defaults = {
            "theme": "dark",
            "geometry": "1500x820",
            "maximized": False,
            "query": "",
            "database_profile": "",
        }
        self._saved_settings = load_module_settings(
            self.SETTINGS_PATH,
            "database_cleanup",
            self._settings_defaults,
            window_keys={"theme", "geometry", "maximized"},
            legacy_json_paths=(
                SCRIPT_DIR / "database_cleanup_settings.json",
                APP_DIR / "database_cleanup_settings.json",
            ),
        )

        saved_theme = self._saved_settings.get("theme")
        self.theme_state = {"value": saved_theme if saved_theme in self.PALETTES else "dark"}
        saved_geometry = self._saved_settings.get("geometry")
        if isinstance(saved_geometry, str) and re.fullmatch(r"\d+x\d+[+-]\d+[+-]\d+", saved_geometry):
            self.geometry(saved_geometry)
            self._last_normal_geometry = saved_geometry
        else:
            self.geometry("1500x820")
            self._last_normal_geometry = self.geometry()
        self._restore_maximized = bool(self._saved_settings.get("maximized", False))
        self.palette = dict(self.PALETTES[self.theme_state["value"]])
        self.style = ttk.Style(self)
        try:
            self.style.theme_use("clam")
        except tk.TclError:
            pass
        self._configure_theme_styles()

        header = ttk.Frame(self, padding=(10, 8, 10, 4), style="App.TFrame")
        header.pack(fill="x")
        ttk.Label(header, text="FaceSearch · Инструменты очистки", style="Header.TLabel").pack(side="left")
        ttk.Label(header, text="Профили · локальные файлы · сетевые URL", style="Muted.TLabel").pack(side="left", padx=14)
        self.theme_button = ttk.Button(header, command=self.toggle_theme, style="Accent.TButton")
        self.theme_button.pack(side="right")
        self._update_theme_button()

        self.notebook = ttk.Notebook(self)
        notebook = self.notebook
        notebook.pack(fill="both", expand=True, padx=8, pady=(0, 8))
        self.profile_tab = ProfileSearchTab(notebook)
        self.local_tab = LocalCleanupTab(notebook)
        self.remote_tab = RemoteCleanupTab(notebook)
        notebook.add(self.profile_tab, text="  Профили и связанные данные  ")
        notebook.add(self.local_tab, text="  Отсутствующие локальные файлы  ")
        notebook.add(self.remote_tab, text="  Недоступные URL  ")
        footer = ttk.Frame(self, padding=(10, 0, 10, 8), style="App.TFrame")
        footer.pack(fill="x")
        ttk.Label(footer, text="Перед массовым удалением сделайте резервную копию БД. После удаления лиц перестройте FAISS.", style="Muted.TLabel").pack(side="left")
        self.apply_theme(self.theme_state["value"], persist=False)
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Configure>", self._on_configure, add="+")
        self.after(180, self._restore_window_state)

    def _restore_window_state(self):
        if self._restore_maximized:
            try:
                self.state("zoomed")
            except tk.TclError:
                pass
        self.schedule_settings_save()

    def _on_configure(self, event=None):
        if event is not None and event.widget is not self:
            return
        try:
            if self.state() == "normal":
                self._last_normal_geometry = self.geometry()
        except tk.TclError:
            pass
        self.schedule_settings_save()

    def schedule_settings_save(self):
        try:
            if self._settings_after_id is not None:
                self.after_cancel(self._settings_after_id)
            self._settings_after_id = self.after(350, self._save_settings)
        except tk.TclError:
            self._settings_after_id = None

    def _save_settings(self):
        self._settings_after_id = None
        try:
            state = self.state()
            maximized = state in {"zoomed", "maximized"}
            geometry = self.geometry() if state == "normal" else self._last_normal_geometry
            profile_tab = getattr(self, "profile_tab", None)
            saved = {
                "theme": self.theme_state["value"],
                "geometry": str(geometry or "1500x820"),
                "maximized": maximized,
                "query": profile_tab.query_var.get() if profile_tab is not None else str(self._saved_settings.get("query") or ""),
                "database_profile": profile_tab.profile_var.get() if profile_tab is not None else str(self._saved_settings.get("database_profile") or ""),
            }
            save_module_settings(
                self.SETTINGS_PATH,
                "database_cleanup",
                saved,
                window_keys={"theme", "geometry", "maximized"},
            )
            self._saved_settings = saved
        except (OSError, tk.TclError, AttributeError, TypeError):
            pass

    def _on_close(self):
        try:
            if self._settings_after_id is not None:
                self.after_cancel(self._settings_after_id)
                self._settings_after_id = None
            self._save_settings()
        finally:
            self.destroy()

    def set_operation_busy(self, busy: bool):
        """Блокирует переключение на другие вкладки и тему, пока поиск/загрузка профиля активны."""
        try:
            self.theme_button.configure(state="disabled" if busy else "normal")
            # Текущая вкладка поиска остаётся активной, остальные нельзя запускать параллельно.
            self.notebook.tab(self.local_tab, state="disabled" if busy else "normal")
            self.notebook.tab(self.remote_tab, state="disabled" if busy else "normal")
        except (tk.TclError, AttributeError):
            pass

    def _configure_theme_styles(self):
        p = self.palette
        style = self.style
        style.configure(".", font=("Segoe UI", 10), foreground=p["text"], background=p["panel"])
        style.configure("TFrame", background=p["panel"])
        style.configure("App.TFrame", background=p["app"])
        style.configure("TLabelframe", background=p["panel"], bordercolor=p["border"], lightcolor=p["border"], darkcolor=p["border"])
        style.configure("TLabelframe.Label", background=p["panel"], foreground=p["accent"], font=("Segoe UI", 10, "bold"))
        style.configure("TLabel", background=p["panel"], foreground=p["text"])
        style.configure("Header.TLabel", background=p["app"], foreground=p["text"], font=("Segoe UI", 15, "bold"))
        style.configure("Muted.TLabel", background=p["app"], foreground=p["muted"], font=("Segoe UI", 9))
        style.configure("Status.TLabel", background=p["panel"], foreground=p["accent"], font=("Segoe UI", 10, "bold"))
        style.configure("Busy.TFrame", background=p["accent"], bordercolor=p["accent"], relief="solid", borderwidth=1)
        style.configure("Busy.TLabel", background=p["accent"], foreground="#FFFFFF", font=("Segoe UI", 10, "bold"))
        style.configure("Busy.Horizontal.TProgressbar", troughcolor=p["panel"], background="#FFFFFF", bordercolor=p["accent"], lightcolor="#FFFFFF", darkcolor="#FFFFFF", thickness=10)
        style.configure("TCheckbutton", background=p["panel"], foreground=p["text"], padding=3)
        style.map("TCheckbutton", background=[("active", p["panel"])], foreground=[("disabled", p["disabled"])])
        style.configure("TRadiobutton", background=p["panel"], foreground=p["text"], padding=3)
        style.map("TRadiobutton", background=[("active", p["panel"])])
        style.configure("TEntry", fieldbackground=p["field"], foreground=p["text"], insertcolor=p["text"], bordercolor=p["border"], padding=5)
        style.configure("TCombobox", fieldbackground=p["field"], foreground=p["text"], background=p["button"], arrowcolor=p["text"], bordercolor=p["border"])
        style.map("TCombobox", fieldbackground=[("readonly", p["field"])], foreground=[("disabled", p["disabled"])])
        style.configure("TButton", background=p["button"], foreground=p["text"], bordercolor=p["border"], padding=(10, 6), relief="flat")
        style.map("TButton", background=[("pressed", p["button_active"]), ("active", p["button_active"]), ("disabled", p["panel"])], foreground=[("disabled", p["disabled"])])
        style.configure("Accent.TButton", background=p["accent"], foreground="#FFFFFF", bordercolor=p["accent"], padding=(12, 7), font=("Segoe UI", 10, "bold"))
        style.map("Accent.TButton", background=[("pressed", p["accent_active"]), ("active", p["accent_active"])], foreground=[("!disabled", "#FFFFFF")])
        style.configure("TNotebook", background=p["app"], bordercolor=p["border"])
        style.configure("TNotebook.Tab", background=p["button"], foreground=p["text"], padding=(12, 7))
        style.map("TNotebook.Tab", background=[("selected", p["panel"]), ("active", p["button_active"])], foreground=[("selected", p["accent"])])
        style.configure("Treeview", background=p["field"], fieldbackground=p["field"], foreground=p["text"], rowheight=25, bordercolor=p["border"])
        style.configure("Treeview.Heading", background=p["button"], foreground=p["text"], font=("Segoe UI", 9, "bold"), relief="flat")
        style.map("Treeview", background=[("selected", p["selection"])], foreground=[("selected", p["text"])])
        style.map("Treeview.Heading", background=[("active", p["button_active"])])
        style.configure("TScrollbar", background=p["button"], troughcolor=p["app"], bordercolor=p["app"], arrowcolor=p["text"])

    def _update_theme_button(self):
        self.theme_button.configure(text="☀ Дневная тема" if self.theme_state["value"] == "dark" else "☾ Ночная тема")

    def toggle_theme(self):
        self.apply_theme("light" if self.theme_state["value"] == "dark" else "dark", persist=True)

    def apply_theme(self, theme_name, persist=False):
        selected = theme_name if theme_name in self.PALETTES else "dark"
        self.theme_state["value"] = selected
        self.palette.clear()
        self.palette.update(self.PALETTES[selected])
        self.configure(bg=self.palette["app"])
        self._configure_theme_styles()

        def recolor(widget):
            try:
                if isinstance(widget, tk.Toplevel):
                    widget.configure(bg=self.palette["app"])
                elif isinstance(widget, tk.Canvas):
                    widget.configure(bg=self.palette["field"], highlightbackground=self.palette["border"])
                elif isinstance(widget, tk.Text):
                    widget.configure(bg=self.palette["field"], fg=self.palette["text"], insertbackground=self.palette["text"], selectbackground=self.palette["selection"], highlightbackground=self.palette["border"], highlightcolor=self.palette["accent"])
                elif isinstance(widget, tk.Entry):
                    widget.configure(bg=self.palette["field"], fg=self.palette["text"], insertbackground=self.palette["text"], selectbackground=self.palette["selection"], highlightbackground=self.palette["border"])
                elif isinstance(widget, (tk.Frame, tk.LabelFrame)):
                    widget.configure(bg=self.palette["panel"])
                elif isinstance(widget, (tk.Label, tk.Button, tk.Checkbutton, tk.Radiobutton, tk.Menubutton)):
                    widget.configure(bg=self.palette["panel"], fg=self.palette["text"], activebackground=self.palette["button_active"], activeforeground=self.palette["text"], disabledforeground=self.palette["disabled"])
                for child in widget.winfo_children():
                    recolor(child)
            except (tk.TclError, AttributeError):
                pass
        recolor(self)
        self._update_theme_button()
        if persist:
            self._save_settings()


if __name__ == '__main__':
    try:
        app = CleanupSuite()
        app.mainloop()
    except Exception as exc:
        root = tk.Tk(); root.withdraw()
        messagebox.showerror('FaceSearch — ошибка запуска', str(exc), parent=root)
        root.destroy()
        raise