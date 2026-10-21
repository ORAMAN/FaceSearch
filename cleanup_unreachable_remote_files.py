# -*- coding: utf-8 -*-
"""
FaceSearch: проверка доступности сетевых URL оригиналов.

Запуск из корня проекта:
    uv run python cleanup_missing_local_files.py
или
    python cleanup_missing_local_files.py

Проверяются URL оригиналов из images.url_image для записей с source_type != 'local'.
images.source_url обычно хранит URL страницы профиля; images.path хранит путь CROP.
Удаление разрешено только после двух HTTP-ответов 404/410; ошибки доступа и временные сбои не удаляются автоматически.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from urllib.error import HTTPError, URLError
from urllib.request import Request, urlopen
import tkinter as tk
from tkinter import ttk, messagebox
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
from urllib.request import url2pathname
import re

APP_DIR = Path(__file__).resolve().parent
sys.path.insert(0, str(APP_DIR))

try:
    from facesearch.config import (
        APP_DIR as PROJECT_DIR, DB_CONFIG_FILE,
        DB_TYPE_SQLITE, DB_TYPE_POSTGRESQL, DB_TYPE_MYSQL, DB_TYPE_MSSQL,
        DB_DEFAULT_PORTS,
    )
except Exception as exc:
    raise SystemExit(
        "Не удалось импортировать конфигурацию FaceSearch. "
        "Положите файл в корень проекта рядом с face_search_gui.py.\n"
        f"Подробности: {exc}"
    )


def load_profiles() -> tuple[list[dict[str, Any]], str]:
    """Читает те же database_profiles.json, что и основное приложение."""
    if not DB_CONFIG_FILE.is_file():
        raise FileNotFoundError(
            f"Не найден файл профилей БД:\n{DB_CONFIG_FILE}\n"
            "Сначала запустите FaceSearch и создайте профиль подключения."
        )
    data = json.loads(DB_CONFIG_FILE.read_text(encoding="utf-8"))
    profiles = data.get("profiles", [])
    profiles = [p for p in profiles if isinstance(p, dict) and p.get("name")]
    if not profiles:
        raise ValueError(f"В {DB_CONFIG_FILE} нет профилей подключения.")
    active = str(data.get("active") or profiles[0]["name"])
    return profiles, active


def open_database(profile: dict[str, Any]):
    """Открывает выбранный профиль; использует те же драйверы, что FaceSearch."""
    backend = str(profile.get("type") or DB_TYPE_SQLITE).lower()

    if backend == DB_TYPE_SQLITE:
        db_path = Path(str(profile.get("path") or PROJECT_DIR / "faces.db")).expanduser()
        if not db_path.is_absolute():
            db_path = PROJECT_DIR / db_path
        if not db_path.is_file():
            raise FileNotFoundError(f"Файл SQLite не найден:\n{db_path}")
        raw = sqlite3.connect(str(db_path), timeout=30)
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

    # Слой проекта нормализует placeholders для PostgreSQL/MySQL и другие
    # различия SQL Server. Для очистки используем его, если доступен.
    try:
        from facesearch.database.connection import DatabaseConnection
        return DatabaseConnection(raw, backend), backend
    except Exception:
        return raw, backend


def execute(conn, sql: str, params=()):
    """Совместим с DatabaseConnection и обычными DB-API соединениями."""
    result = conn.execute(sql, params)
    return result


def resolve_project_path(raw_path: str) -> Path:
    """Разрешает обычный абсолютный/относительный путь относительно корня проекта."""
    path = Path(raw_path).expanduser()
    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def resolve_original_path(raw_source: str) -> Path | None:
    """Разрешает source_url локального оригинала, включая file:// URL."""
    raw = str(raw_source or "").strip()
    if not raw:
        return None

    # Windows-диск (например C:\\photos\\1.jpg) urlparse ошибочно
    # воспринимает как URI scheme "c", поэтому проверяем его первым.
    if re.match(r"^[A-Za-z]:[\\/]", raw) or raw.startswith("\\\\"):
        path = Path(raw).expanduser()
    else:
        parsed = urlparse(raw)
        scheme = parsed.scheme.lower()
        if scheme == "file":
            path_text = unquote(parsed.path or "")
            if os.name == "nt":
                # file:///C:/dir/file.jpg -> C:\\dir\\file.jpg
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
            # http(s), ftp и прочие внешние адреса не являются локальными оригиналами.
            return None

    if not path.is_absolute():
        path = PROJECT_DIR / path
    return path


def resolve_crop_path(raw_path: str, crop_base: Path) -> Path:
    """Разрешает images.path через CROP_DIR выбранного профиля, с legacy fallback."""
    path = Path(str(raw_path or "")).expanduser()
    if path.is_absolute():
        return path
    crop_candidate = (crop_base / path).resolve()
    if crop_candidate.is_file():
        return crop_candidate
    # Обратная совместимость со старыми БД, где path был относительно APP_DIR.
    return (PROJECT_DIR / path).resolve()


def profile_crop_dir(profile: dict[str, Any]) -> Path:
    """Возвращает CROP_DIR выбранного профиля БД."""
    raw = str(profile.get("crop_path") or "").strip()
    if raw:
        return resolve_project_path(raw).resolve()
    # Совместимость с профилями старого формата без crop_path.
    return (PROJECT_DIR / "face_images").resolve()


def fetch_remote_candidates(conn) -> list[tuple[int, str, str, str, str, str]]:
    """
    Получает сетевые изображения. Оригинал для проверки берётся из images.url_image;
    images.source_url обычно указывает на страницу профиля, а images.path — на CROP.
    """
    rows = execute(
        conn,
        """
        SELECT id, path, filename, source_type, url_image, source_url
        FROM images
        WHERE LOWER(COALESCE(source_type, 'local')) <> 'local'
          AND url_image IS NOT NULL
          AND TRIM(url_image) <> ''
        ORDER BY id
        """,
    ).fetchall()
    return [
        (
            int(row[0]),
            str(row[1] or "").strip(),
            str(row[2] or "").strip(),
            str(row[3] or "").strip(),
            str(row[4] or "").strip(),
            str(row[5] or "").strip(),
        )
        for row in rows
    ]


def check_remote_url(url: str) -> dict[str, Any]:
    """
    Проверяет прямой URL оригинала GET-запросом с Range.
    Только два последовательных ответа HTTP 404/410 считаются подтверждённым
    отсутствием оригинала. 401/403/429/5xx и сетевые ошибки не удаляются автоматически.
    """
    value = str(url or "").strip()
    parsed = urlparse(value)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
        return {
            "label": "Пропущено: URL не HTTP(S)",
            "dead": False,
            "kind": "skip",
            "detail": "",
        }

    confirmed_missing: list[int] = []
    last_note = ""
    last_code = None

    for attempt in range(2):
        try:
            request = Request(
                value,
                headers={
                    "User-Agent": (
                        "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
                        "AppleWebKit/537.36 (KHTML, like Gecko) "
                        "Chrome/134.0.0.0 Safari/537.36"
                    ),
                    "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
                    "Range": "bytes=0-1023",
                    "Connection": "close",
                },
                method="GET",
            )
            try:
                with urlopen(request, timeout=15) as response:
                    code = int(response.getcode() or 0)
                    content_type = str(response.headers.get("Content-Type") or "").lower()
                    # Читаем только небольшой фрагмент, не скачиваем оригинал целиком.
                    response.read(1024)
            except HTTPError as exc:
                code = int(exc.code or 0)
                content_type = str(exc.headers.get("Content-Type") or "").lower() if exc.headers else ""
                try:
                    exc.close()
                except Exception:
                    pass

            last_code = code
            if 200 <= code < 300:
                if "text/html" in content_type or "application/json" in content_type:
                    return {
                        "label": f"Проверить вручную: HTTP {code}, ответ не изображение",
                        "dead": False,
                        "kind": "warning",
                        "detail": content_type,
                    }
                return {
                    "label": f"Доступен (HTTP {code})",
                    "dead": False,
                    "kind": "ok",
                    "detail": content_type or "Content-Type не указан",
                }

            if code in (404, 410):
                confirmed_missing.append(code)
                if len(confirmed_missing) >= 2:
                    return {
                        "label": f"Оригинал отсутствует (HTTP {code})",
                        "dead": True,
                        "kind": "dead",
                        "detail": "Ошибка повторилась при двух запросах",
                    }
                last_note = f"HTTP {code}; выполняется контрольный запрос"
                time.sleep(0.35)
                continue

            if code in (401, 403):
                return {
                    "label": f"Нет доступа (HTTP {code})",
                    "dead": False,
                    "kind": "warning",
                    "detail": "Запрет доступа не доказывает, что файл удалён",
                }

            last_note = f"HTTP {code}"
            if code == 429 or code == 408 or 500 <= code < 600:
                if attempt == 0:
                    time.sleep(0.6)
                    continue
            return {
                "label": f"Ошибка/ограничение (HTTP {code})",
                "dead": False,
                "kind": "warning",
                "detail": last_note,
            }

        except (URLError, TimeoutError, OSError, ValueError) as exc:
            last_note = str(getattr(exc, "reason", exc))[:220]
            if attempt == 0:
                time.sleep(0.6)
                continue

    if confirmed_missing and last_code not in (404, 410):
        return {
            "label": f"Не подтверждено: был HTTP {confirmed_missing[0]}",
            "dead": False,
            "kind": "warning",
            "detail": last_note or "Повторный запрос не подтвердил отсутствие",
        }

    return {
        "label": "Ошибка сети; не удалять автоматически",
        "dead": False,
        "kind": "warning",
        "detail": last_note or f"Последний статус: {last_code}",
    }


def check_remote_urls(
    urls: list[str],
    progress_callback=None,
) -> dict[str, dict[str, Any]]:
    """Проверяет каждый уникальный URL параллельно и возвращает URL -> результат."""
    unique_urls = list(dict.fromkeys(str(url or "").strip() for url in urls if str(url or "").strip()))
    results: dict[str, dict[str, Any]] = {}
    valid_urls = []

    for url in unique_urls:
        parsed = urlparse(url)
        if parsed.scheme.lower() not in ("http", "https") or not parsed.netloc:
            results[url] = check_remote_url(url)
        else:
            valid_urls.append(url)

    total = len(valid_urls)
    if total == 0:
        if progress_callback:
            progress_callback(0, 0)
        return results

    workers = min(16, total)
    done = 0
    with ThreadPoolExecutor(max_workers=workers, thread_name_prefix="url-check") as pool:
        futures = {pool.submit(check_remote_url, url): url for url in valid_urls}
        for future in as_completed(futures):
            url = futures[future]
            try:
                results[url] = future.result()
            except Exception as exc:
                results[url] = {
                    "label": "Ошибка проверки; не удалять автоматически",
                    "dead": False,
                    "kind": "warning",
                    "detail": str(exc)[:220],
                }
            done += 1
            if progress_callback and (done % 5 == 0 or done == total):
                progress_callback(done, total)

    return results


def fetch_orphan_profile_ids(conn) -> list[int]:
    """Возвращает ID профилей, на которые не ссылается ни одна запись images."""
    rows = execute(
        conn,
        """
        SELECT p.id
        FROM profiles p
        WHERE NOT EXISTS (
            SELECT 1
            FROM images
            WHERE images.profile_id = p.id
        )
        ORDER BY p.id
        """,
    ).fetchall()
    return [int(row[0]) for row in rows]


def delete_remote_rows(
    conn,
    rows: list[tuple[int, str, str, str, str, str]],
    crop_base: Path,
) -> tuple[int, int, list[str], int]:
    """Удаляет подтверждённо отсутствующие URL-оригиналы, faces, CROP и orphan profiles."""
    if not rows:
        crop_paths = []
    else:
        crop_paths = [
            resolve_crop_path(row[1], crop_base)
            for row in rows
            if row[1]
        ]

    ids = [int(row[0]) for row in rows]
    try:
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            execute(conn, f"DELETE FROM faces WHERE image_id IN ({marks})", batch)
            execute(conn, f"DELETE FROM images WHERE id IN ({marks})", batch)

        profiles_result = execute(
            conn,
            """
            DELETE FROM profiles
            WHERE NOT EXISTS (
                SELECT 1
                FROM images
                WHERE images.profile_id = profiles.id
            )
            """,
        )
        profiles_deleted = max(0, int(profiles_result.rowcount or 0))
        conn.commit()
    except Exception:
        try:
            conn.rollback()
        except Exception:
            pass
        raise

    crop_deleted = 0
    crop_errors: list[str] = []
    seen: set[str] = set()
    for crop_path in crop_paths:
        key = str(crop_path.resolve(strict=False)).casefold()
        if key in seen:
            continue
        seen.add(key)
        try:
            if crop_path.is_file():
                crop_path.unlink()
                crop_deleted += 1
        except OSError as exc:
            crop_errors.append(f"{crop_path}: {exc}")

    return len(ids), crop_deleted, crop_errors, profiles_deleted


class CleanupWindow(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("FaceSearch — проверка URL оригиналов")
        self.geometry("1180x700")
        self.minsize(820, 480)
        self.profiles, active = load_profiles()
        self.profile_by_name = {str(p["name"]): p for p in self.profiles}
        self.profile_var = tk.StringVar(
            value=active if active in self.profile_by_name else str(self.profiles[0]["name"])
        )
        self.status_var = tk.StringVar(value="Выберите профиль БД и нажмите «Проверить URL».")
        self.progress_var = tk.StringVar(value="")
        self.candidates: list[tuple[int, str, str, str, str, str]] = []
        self.results_by_id: dict[int, dict[str, Any]] = {}
        self.orphan_profiles: list[int] = []
        self._busy = False
        self._build_ui()

    def _build_ui(self):
        outer = ttk.Frame(self, padding=12)
        outer.pack(fill="both", expand=True)

        row = ttk.Frame(outer)
        row.pack(fill="x", pady=(0, 8))
        ttk.Label(row, text="Профиль БД:").pack(side="left", padx=(0, 8))
        self.profile_combo = ttk.Combobox(
            row,
            textvariable=self.profile_var,
            state="readonly",
            values=list(self.profile_by_name),
            width=38,
        )
        self.profile_combo.pack(side="left", padx=(0, 8))
        self.check_button = ttk.Button(row, text="Проверить URL", command=self.start_check)
        self.check_button.pack(side="left", padx=4)
        self.delete_button = ttk.Button(
            row,
            text="Удалить отсутствующие оригиналы",
            command=self.confirm_delete,
            state="disabled",
        )
        self.delete_button.pack(side="left", padx=4)

        ttk.Label(
            outer,
            text=(
                "Проверяются сетевые оригиналы из images.url_image у записей, где source_type не local. "
                "images.source_url обычно является адресом профиля, а images.path — путём CROP. "
                "Автоматически удаляются только URL, дважды ответившие HTTP 404/410. "
                "403, 429, ошибки сервера, тайм-ауты и ответы HTML требуют ручной проверки."
            ),
            wraplength=1120,
        ).pack(fill="x", pady=(0, 8))

        columns = ("id", "filename", "source_type", "status", "url", "crop")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", height=20)
        self.tree.heading("id", text="ID")
        self.tree.heading("filename", text="Файл")
        self.tree.heading("source_type", text="Источник")
        self.tree.heading("status", text="Результат проверки")
        self.tree.heading("url", text="URL оригинала (url_image)")
        self.tree.heading("crop", text="Путь CROP")
        self.tree.column("id", width=65, anchor="e", stretch=False)
        self.tree.column("filename", width=160, stretch=False)
        self.tree.column("source_type", width=95, stretch=False)
        self.tree.column("status", width=255, stretch=False)
        self.tree.column("url", width=480, stretch=True)
        self.tree.column("crop", width=220, stretch=False)
        self.tree.tag_configure("ok", foreground="#237a3b")
        self.tree.tag_configure("dead", foreground="#b42318")
        self.tree.tag_configure("warning", foreground="#9a6700")
        self.tree.tag_configure("skip", foreground="#6b7280")

        yscroll = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(outer, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.pack(side="top", fill="both", expand=True)
        yscroll.pack(side="right", fill="y")
        xscroll.pack(fill="x")

        ttk.Label(outer, textvariable=self.progress_var).pack(fill="x", pady=(8, 0))
        ttk.Label(outer, textvariable=self.status_var).pack(fill="x", pady=(4, 0))

    def _set_busy(self, busy: bool):
        self._busy = busy
        self.profile_combo.configure(state="disabled" if busy else "readonly")
        self.check_button.configure(state="disabled" if busy else "normal")
        if busy:
            self.delete_button.configure(state="disabled")

    def start_check(self):
        if self._busy:
            return
        self._set_busy(True)
        self.tree.delete(*self.tree.get_children())
        self.results_by_id.clear()
        self.progress_var.set("")
        self.status_var.set("Подключение к БД и получение списка URL…")
        threading.Thread(
            target=self._check_worker,
            args=(self.profile_var.get(),),
            daemon=True,
        ).start()

    def _check_progress(self, done: int, total: int, prefix: str = "Проверено URL"):
        self.after(0, lambda: self.progress_var.set(f"{prefix}: {done} из {total}."))

    def _check_worker(self, profile_name: str):
        conn = None
        try:
            profile = self.profile_by_name[profile_name]
            conn, _backend = open_database(profile)
            candidates = fetch_remote_candidates(conn)
            orphan_profiles = fetch_orphan_profile_ids(conn)
            conn.close()
            conn = None

            urls = [item[4] for item in candidates]
            status_map = check_remote_urls(
                urls,
                progress_callback=lambda done, total: self._check_progress(done, total),
            )
            self.after(0, self._show_results, candidates, status_map, orphan_profiles)
        except Exception as exc:
            self.after(0, lambda err=str(exc): self._show_error(err))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.after(0, self._finish_busy)

    def _show_results(self, candidates, status_map, orphan_profiles):
        self.candidates = candidates
        self.orphan_profiles = orphan_profiles
        self.results_by_id.clear()
        self.tree.delete(*self.tree.get_children())

        counts = {"ok": 0, "dead": 0, "warning": 0, "skip": 0}
        for item in candidates:
            image_id, crop_path, filename, source_type, url, _source_url = item
            result = status_map.get(url) or {
                "label": "Нет результата проверки",
                "dead": False,
                "kind": "warning",
                "detail": "",
            }
            self.results_by_id[image_id] = {"candidate": item, "result": result}
            counts[result.get("kind", "warning")] = counts.get(result.get("kind", "warning"), 0) + 1
            self.tree.insert(
                "",
                "end",
                iid=str(image_id),
                values=(image_id, filename, source_type, result["label"], url, crop_path),
                tags=(result.get("kind", "warning"),),
            )

        dead_count = counts["dead"]
        self.delete_button.configure(
            state="normal" if dead_count or orphan_profiles else "disabled"
        )
        self.status_var.set(
            f"Проверка завершена. URL: {len(candidates)}; доступно: {counts['ok']}; "
            f"подтверждённо отсутствуют (404/410): {dead_count}; "
            f"ошибки/ограничения: {counts['warning']}; пропущено: {counts['skip']}; "
            f"профилей без ссылок: {len(orphan_profiles)}."
        )
        self.progress_var.set("")

    def confirm_delete(self):
        if self._busy:
            return
        snapshot = [
            data["candidate"]
            for data in self.results_by_id.values()
            if data["result"].get("dead")
        ]
        if not snapshot and not self.orphan_profiles:
            return

        if not messagebox.askyesno(
            "Подтверждение очистки",
            f"Подтверждённо отсутствующих URL-оригиналов: {len(snapshot)}.\n"
            f"Профилей без ссылок из images на момент проверки: {len(self.orphan_profiles)}.\n\n"
            "Перед удалением URL будут проверены повторно. Будут удалены записи faces/images "
            "для URL, которые снова подтвердят HTTP 404/410, соответствующие CROP-файлы и "
            "профили, на которые не останется ссылок из images. "
            "Оригиналы в сети не изменяются. Ошибки доступа и временные сбои не удаляются.",
            icon="warning",
            parent=self,
        ):
            return

        self._set_busy(True)
        self.progress_var.set("Повторная проверка URL перед удалением…")
        self.status_var.set("Повторная проверка и удаление записей…")
        threading.Thread(
            target=self._delete_worker,
            args=(snapshot, self.profile_var.get()),
            daemon=True,
        ).start()

    def _delete_worker(self, snapshot, profile_name: str):
        conn = None
        try:
            urls = [item[4] for item in snapshot]
            status_map = check_remote_urls(
                urls,
                progress_callback=lambda done, total: self._check_progress(
                    done, total, "Повторно проверено URL"
                ),
            )
            still_dead = {
                url for url, result in status_map.items()
                if result.get("dead")
            }

            profile = self.profile_by_name[profile_name]
            crop_base = profile_crop_dir(profile)
            conn, _backend = open_database(profile)

            # Проверяем, что URL и источник записи не изменились после первичного списка.
            confirmed = []
            for old in snapshot:
                image_id, _old_crop, _filename, _source_type, old_url, _old_source_url = old
                if old_url not in still_dead:
                    continue
                current = execute(
                    conn,
                    """
                    SELECT id, path, filename, source_type, url_image, source_url
                    FROM images
                    WHERE id = ?
                    """,
                    (int(image_id),),
                ).fetchone()
                if not current:
                    continue
                current_source_type = str(current[3] or "").strip().lower()
                current_url = str(current[4] or "").strip()
                if current_source_type == "local" or current_url != old_url:
                    continue
                confirmed.append((
                    int(current[0]),
                    str(current[1] or "").strip(),
                    str(current[2] or "").strip(),
                    str(current[3] or "").strip(),
                    current_url,
                    str(current[5] or "").strip(),
                ))

            removed, crops_deleted, crop_errors, profiles_deleted = delete_remote_rows(
                conn, confirmed, crop_base
            )
            skipped = len(snapshot) - len(confirmed)
            self.after(
                0,
                self._delete_done,
                removed,
                skipped,
                crops_deleted,
                crop_errors,
                profiles_deleted,
            )
        except Exception as exc:
            if conn is not None:
                try:
                    conn.rollback()
                except Exception:
                    pass
            self.after(0, lambda err=str(exc): self._show_error(err))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.after(0, self._finish_busy)

    def _delete_done(self, removed, skipped, crops_deleted, crop_errors, profiles_deleted):
        self.results_by_id.clear()
        self.candidates = []
        self.orphan_profiles = []
        self.tree.delete(*self.tree.get_children())
        self.delete_button.configure(state="disabled")
        self.progress_var.set("")
        self.status_var.set(
            f"Готово. Удалено images: {removed}; профилей: {profiles_deleted}; "
            f"CROP-файлов: {crops_deleted}; пропущено после повторной проверки: {skipped}; "
            f"ошибок удаления CROP: {len(crop_errors)}."
        )
        details = (
            f"Удалено записей images: {removed}\n"
            f"Удалено связанных записей faces: для этих изображений\n"
            f"Удалено профилей без ссылок: {profiles_deleted}\n"
            f"Удалено CROP-файлов: {crops_deleted}\n"
            f"Пропущено после повторной проверки: {skipped}\n"
        )
        if crop_errors:
            details += "\nНе удалось удалить некоторые CROP-файлы:\n" + "\n".join(crop_errors[:10])
            if len(crop_errors) > 10:
                details += f"\n…и ещё {len(crop_errors) - 10}."
        details += "\n\nДля синхронизации поиска перестройте FAISS-индекс выбранного профиля."
        messagebox.showinfo("Очистка завершена", details, parent=self)

    def _show_error(self, error):
        self.status_var.set("Ошибка операции.")
        messagebox.showerror("Ошибка очистки", error, parent=self)

    def _finish_busy(self):
        self._set_busy(False)


if __name__ == "__main__":
    try:
        app = CleanupWindow()
        app.mainloop()
    except Exception as exc:
        try:
            root = tk.Tk()
            root.withdraw()
            messagebox.showerror("FaceSearch — очистка", str(exc))
            root.destroy()
        except Exception:
            print(f"Ошибка: {exc}", file=sys.stderr)
        raise
