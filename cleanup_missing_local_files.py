# -*- coding: utf-8 -*-
"""
Очистка FaceSearch: удаление записей локальных файлов, которых больше нет на диске.

Запуск из корня проекта:
    uv run python cleanup_missing_local_files.py
или
    python cleanup_missing_local_files.py

Проверяется оригинал по images.source_url только для локальных записей без url_image.
В локальных записях source_url может иметь вид file:///...; images.path хранит путь CROP.
При удалении удаляются записи faces/images, профили без ссылок из images и связанный CROP-файл, если он существует.
"""
from __future__ import annotations

import json
import os
import sqlite3
import sys
import threading
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


def fetch_missing(conn) -> list[tuple[int, str, str, str]]:
    """
    Находит локальные оригиналы, которых больше нет на диске.

    Включаются только записи source_type='local' и url_image IS NULL/пустой.
    Путь оригинала читается из source_url (в проекте обычно file:///...);
    images.path сохраняется отдельно и используется как путь CROP.
    """
    rows = execute(
        conn,
        """
        SELECT id, path, filename, source_url
        FROM images
        WHERE LOWER(COALESCE(source_type, 'local')) = 'local'
          AND (url_image IS NULL OR TRIM(url_image) = '')
        ORDER BY id
        """,
    ).fetchall()

    missing = []
    for row in rows:
        image_id, crop_path_raw, filename, source_raw = row[0], row[1], row[2], row[3]
        source_text = str(source_raw or "").strip()
        if not source_text:
            continue

        source_path = resolve_original_path(source_text)
        if source_path is None:
            continue
        try:
            original_exists = source_path.is_file()
        except OSError:
            # Ошибка доступа не доказывает, что оригинал был удалён.
            continue

        if not original_exists:
            missing.append((
                int(image_id),
                str(crop_path_raw or "").strip(),
                str(filename or source_path.name),
                source_text,
            ))
    return missing


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


def delete_missing(
    conn,
    missing: list[tuple[int, str, str, str]],
    crop_base: Path,
) -> tuple[int, int, list[str], int]:
    """Удаляет faces/images, очищает профили без ссылок и удаляет CROP-файлы."""
    ids = [item[0] for item in missing]
    crop_paths = []
    for _image_id, crop_text, _filename, _source_text in missing:
        if crop_text:
            crop_paths.append(resolve_crop_path(crop_text, crop_base))

    try:
        for start in range(0, len(ids), 400):
            batch = ids[start:start + 400]
            marks = ",".join("?" for _ in batch)
            execute(conn, f"DELETE FROM faces WHERE image_id IN ({marks})", batch)
            execute(conn, f"DELETE FROM images WHERE id IN ({marks})", batch)

        # После удаления изображений удаляем все profiles, на которые больше
        # не ссылается ни одна строка images. Условие переносимо между СУБД.
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

    # Физические CROP-файлы удаляем только после успешной фиксации транзакции.
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
        self.title("FaceSearch — очистка отсутствующих локальных файлов")
        self.geometry("980x620")
        self.minsize(760, 440)
        self.profiles, active = load_profiles()
        self.profile_by_name = {str(p["name"]): p for p in self.profiles}
        self.profile_var = tk.StringVar(
            value=active if active in self.profile_by_name else str(self.profiles[0]["name"])
        )
        self.status_var = tk.StringVar(value="Выберите профиль и нажмите «Проверить».")
        self.missing: list[tuple[int, str, str, str]] = []
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
            row, textvariable=self.profile_var, state="readonly",
            values=list(self.profile_by_name), width=42,
        )
        self.profile_combo.pack(side="left", padx=(0, 8))
        ttk.Button(row, text="Проверить", command=self.start_check).pack(side="left", padx=4)
        self.delete_button = ttk.Button(
            row, text="Удалить найденные записи", command=self.confirm_delete, state="disabled"
        )
        self.delete_button.pack(side="left", padx=4)

        ttk.Label(
            outer,
            text=(
                "Проверяются только локальные строки images, где url_image пустой или NULL. "
                "Оригинал определяется по source_url (в том числе file:///...), а images.path "
                "считается путём CROP. При очистке также удаляются профили, на которые не "
                "ссылается ни одна запись images."
            ),
            wraplength=920,
        ).pack(fill="x", pady=(0, 8))

        columns = ("id", "filename", "original", "crop")
        self.tree = ttk.Treeview(outer, columns=columns, show="headings", height=18)
        self.tree.heading("id", text="ID записи")
        self.tree.heading("filename", text="Файл")
        self.tree.heading("original", text="Путь оригинала (source_url)")
        self.tree.heading("crop", text="Путь CROP (path)")
        self.tree.column("id", width=85, anchor="e", stretch=False)
        self.tree.column("filename", width=180, stretch=False)
        self.tree.column("original", width=430)
        self.tree.column("crop", width=430)
        yscroll = ttk.Scrollbar(outer, orient="vertical", command=self.tree.yview)
        xscroll = ttk.Scrollbar(outer, orient="horizontal", command=self.tree.xview)
        self.tree.configure(yscrollcommand=yscroll.set, xscrollcommand=xscroll.set)
        self.tree.pack(side="top", fill="both", expand=True)
        yscroll.place(relx=1.0, rely=0.23, relheight=0.62, anchor="ne")
        xscroll.pack(fill="x")

        ttk.Label(outer, textvariable=self.status_var).pack(fill="x", pady=(8, 0))

    def start_check(self):
        if self._busy:
            return
        self._busy = True
        self.delete_button.configure(state="disabled")
        self.status_var.set("Подключение к БД и проверка файлов…")
        self.tree.delete(*self.tree.get_children())
        threading.Thread(target=self._check_worker, daemon=True).start()

    def _check_worker(self):
        conn = None
        try:
            profile = self.profile_by_name[self.profile_var.get()]
            conn, _backend = open_database(profile)
            missing = fetch_missing(conn)
            orphan_profiles = fetch_orphan_profile_ids(conn)
            self.after(0, lambda: self._show_results(missing, orphan_profiles))
        except Exception as exc:
            self.after(0, lambda err=str(exc): self._show_error(err))
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            self.after(0, self._finish_busy)

    def _show_results(self, missing, orphan_profiles):
        self.missing = missing
        self.orphan_profiles = orphan_profiles
        for image_id, crop_path, filename, source_path in missing:
            self.tree.insert("", "end", values=(image_id, filename, source_path, crop_path))
        has_cleanup = bool(missing or orphan_profiles)
        self.delete_button.configure(state="normal" if has_cleanup else "disabled")
        if has_cleanup:
            self.status_var.set(
                f"Проверка завершена. Отсутствующих локальных файлов: {len(missing)}; "
                f"профилей без ссылок из images: {len(orphan_profiles)}."
            )
        else:
            self.status_var.set(
                "Проверка завершена: отсутствующих локальных файлов и профилей без ссылок не найдено."
            )

    def confirm_delete(self):
        if not self.missing and not self.orphan_profiles:
            return
        count = len(self.missing)
        orphan_count = len(self.orphan_profiles)
        if not messagebox.askyesno(
            "Подтверждение очистки",
            f"Отсутствующих локальных изображений найдено: {count}.\n"
            f"Профилей без ссылок из images на момент проверки: {orphan_count}.\n\n"
            "Будут удалены отсутствующие изображения, связанные записи faces и CROP-файлы. "
            "Также будут удалены все профили, на которые после очистки не останется ссылок "
            "из таблицы images. Оригиналы не удаляются.",
            icon="warning",
            parent=self,
        ):
            return
        self._busy = True
        self.delete_button.configure(state="disabled")
        self.status_var.set("Удаление записей БД и CROP-файлов…")
        snapshot = list(self.missing)
        threading.Thread(target=self._delete_worker, args=(snapshot,), daemon=True).start()

    def _delete_worker(self, snapshot):
        conn = None
        try:
            profile = self.profile_by_name[self.profile_var.get()]
            conn, _backend = open_database(profile)
            crop_base = profile_crop_dir(profile)
            # Повторная проверка непосредственно перед удалением: файл мог
            # появиться снова после предварительного просмотра.
            still_missing = fetch_missing(conn)
            missing_by_id = {row[0]: row for row in still_missing}
            confirmed = [missing_by_id[item[0]] for item in snapshot if item[0] in missing_by_id]
            removed, crops_deleted, crop_errors, profiles_deleted = delete_missing(
                conn, confirmed, crop_base
            )
            self.after(
                0,
                lambda: self._delete_done(
                    removed, len(snapshot) - len(confirmed), crops_deleted,
                    crop_errors, profiles_deleted
                ),
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
        self.missing = []
        self.orphan_profiles = []
        self.tree.delete(*self.tree.get_children())
        self.delete_button.configure(state="disabled")
        self.status_var.set(
            f"Готово. Удалено images: {removed}; профилей: {profiles_deleted}; "
            f"CROP-файлов: {crops_deleted}; пропущено: {skipped}; "
            f"ошибок удаления CROP: {len(crop_errors)}."
        )
        details = (
            f"Удалено записей images: {removed}\n"
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
        self._busy = False


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
