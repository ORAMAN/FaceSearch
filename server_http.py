# -*- coding: utf-8 -*-
"""
Многопоточный HTTP-интерфейс FaceSearch.

Запуск из каталога проекта:
    .venv\Scripts\python.exe server_http.py
    .venv\Scripts\python.exe server_http.py --host 0.0.0.0 --port 8765

Активный профиль, backend, процент сходства по умолчанию и top-k читаются из
database_profiles.json / settings.xml так же, как в GUI. Интерфейс не изменяет
settings.xml и не выполняет миграцию схемы БД.
"""
from __future__ import annotations

import argparse
import base64
import binascii
import ipaddress
import json
import mimetypes
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from urllib.parse import parse_qs, urlparse

import face_search_gui as face_search
from facesearch.settings import _load_gui_settings
from facesearch.database_profiles import _load_db_profiles, _default_profile

APP_DIR = Path(__file__).resolve().parent
MAX_BODY_BYTES = 24 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
QUERY_TTL_SECONDS = 30 * 60
MAX_QUERY_SESSIONS = 100

QUERY_SESSIONS: dict[str, dict] = {}
QUERY_SESSIONS_LOCK = threading.RLock()
API_SEMAPHORE = threading.BoundedSemaphore(4)

PROFILE: dict = {}
PROFILE_NAME = ""
BACKEND = face_search.BACKEND_GPU
FACE_APP = None
DEFAULT_TOP_K = 24
DEFAULT_MIN_SIMILARITY = 43.0
DEFAULT_USE_LOCAL_CROPS = True
DEFAULT_THEME = "dark"
CREATE_CROPS_ON_HTML = False
CROP_REPAIR_WORKERS = 4
CROP_REPAIR_AUTO_WORKERS = False
_CROP_REPAIR_LOCK = threading.RLock()
_CROP_REPAIR_ACTIVE_IDS: set[int] = set()
_FACE_IMAGE_CACHE_LOCK = threading.RLock()
_FACE_IMAGE_CACHE: dict[int, tuple] = {}
_FACE_IMAGE_CACHE_MAX = 5000


def _select_profile() -> tuple[dict, dict]:
    """Выбирает профиль так же, как GUI: сохранённый выбор имеет приоритет."""
    gui_settings = _load_gui_settings()
    profiles, active_name = _load_db_profiles()
    selected_name = str(gui_settings.get("selected_db_profile") or "").strip()
    if selected_name and any(str(p.get("name")) == selected_name for p in profiles):
        active_name = selected_name
    profile = next(
        (dict(p) for p in profiles if str(p.get("name")) == str(active_name)),
        None,
    )
    if profile is None:
        profile = _default_profile()
    return profile, gui_settings


def _open_connection(*, activate_storage: bool = False):
    """Создаёт независимое соединение для текущего HTTP-потока."""
    return face_search.create_database_for_profile(
        PROFILE,
        maintenance=False,
        create_if_missing=False,
        activate_storage=activate_storage,
        initialize=False,
        check_crop_migration=False,
    )


def _initialize_runtime() -> None:
    global PROFILE, PROFILE_NAME, BACKEND, FACE_APP
    global DEFAULT_TOP_K, DEFAULT_MIN_SIMILARITY
    global DEFAULT_USE_LOCAL_CROPS, DEFAULT_THEME, CREATE_CROPS_ON_HTML
    global CROP_REPAIR_WORKERS, CROP_REPAIR_AUTO_WORKERS

    PROFILE, gui_settings = _select_profile()
    PROFILE_NAME = str(PROFILE.get("name") or "БД FaceSearch")

    selected_backend = str(gui_settings.get("backend") or "").strip().lower()
    BACKEND = (
        selected_backend
        if selected_backend in (face_search.BACKEND_GPU, face_search.BACKEND_CPU)
        else face_search.BACKEND_GPU
    )
    DEFAULT_TOP_K = max(1, min(500, int(gui_settings.get("top_k") or 24)))
    DEFAULT_MIN_SIMILARITY = max(
        0.0, min(100.0, float(gui_settings.get("min_similarity", 43.0)))
    )
    DEFAULT_USE_LOCAL_CROPS = bool(gui_settings.get("use_local_crops", True))
    DEFAULT_THEME = "dark" if str(gui_settings.get("theme") or "").lower() == "dark" else "light"
    CREATE_CROPS_ON_HTML = bool(gui_settings.get("create_crops_on_html", False))
    try:
        CROP_REPAIR_WORKERS = max(1, min(32, int(gui_settings.get("workers", 4))))
    except (TypeError, ValueError):
        CROP_REPAIR_WORKERS = 4
    CROP_REPAIR_AUTO_WORKERS = bool(gui_settings.get("auto_workers", False))
    print(f"[HTTP] Создавать CROP при поиске: {'да' if CREATE_CROPS_ON_HTML else 'нет'}")

    # Не позволяем sqlite3 незаметно создать пустой файл вместо потерянной БД.
    if str(PROFILE.get("type") or "sqlite").lower() == "sqlite":
        db_path = Path(str(PROFILE.get("path") or face_search.APP_DIR / "faces.db")).expanduser()
        if not db_path.is_absolute():
            db_path = (face_search.APP_DIR / db_path).resolve()
        if not db_path.is_file():
            raise FileNotFoundError(
                f"Файл активной SQLite-базы не найден: {db_path}. "
                "Откройте GUI и проверьте выбранный профиль."
            )

    # Активируем пути профиля в существующем движке FaceSearch, не меняя схему.
    conn = _open_connection(activate_storage=True)
    try:
        # Таблица faces нужна существующей логике поиска. Запрос выполняется
        # один раз при старте и одновременно проверяет выбранную БД.
        conn.execute("SELECT id FROM faces WHERE 1 = 0").fetchall()
    finally:
        conn.close()

    print(f"[HTTP] Профиль БД: {PROFILE_NAME}")
    print(f"[HTTP] СУБД: {PROFILE.get('type', 'неизвестно')}")
    print(f"[HTTP] Backend: {BACKEND.upper()}")
    print(f"[HTTP] FAISS: {face_search.INDEX_FILE}")

    FACE_APP = face_search.create_face_app(BACKEND)

    index_path = Path(face_search.INDEX_FILE)
    if not index_path.is_file():
        print("[HTTP] ПРЕДУПРЕЖДЕНИЕ: FAISS-индекс не найден; поиск будет сообщать об ошибке.")
    elif BACKEND == face_search.BACKEND_GPU:
        # Пытаемся заранее разместить индекс в VRAM. Если CUDA недоступна или
        # памяти недостаточно, штатный search_embedding использует CPU FAISS.
        try:
            loaded = face_search._load_gpu_search_index_from_faiss(
                index_path,
                progress_callback=lambda message: print("[HTTP][FAISS] " + str(message)),
            )
            if not loaded:
                print("[HTTP] GPU-индекс не активирован; будет использован CPU FAISS.")
        except Exception as exc:
            print(f"[HTTP] Не удалось загрузить FAISS в GPU; fallback на CPU: {exc}")


def _validate_remote_image_url(value: str) -> str:
    """Проверяет HTTP(S)-адрес изображения и запрещает обращения к локальным сетям."""
    raw = str(value or "").strip()
    if not raw or len(raw) > 4096:
        raise ValueError("Введите корректный HTTP/HTTPS-адрес изображения.")
    parsed = urlparse(raw)
    if parsed.scheme.lower() not in ("http", "https") or not parsed.hostname:
        raise ValueError("Адрес должен начинаться с http:// или https://.")
    if parsed.username or parsed.password:
        raise ValueError("Адрес изображения не должен содержать логин или пароль.")
    try:
        port = parsed.port or (443 if parsed.scheme.lower() == "https" else 80)
        addresses = socket.getaddrinfo(parsed.hostname, port, type=socket.SOCK_STREAM)
    except (OSError, ValueError) as exc:
        raise ValueError("Не удалось определить адрес сервера изображения.") from exc
    if not addresses:
        raise ValueError("Сервер изображения не найден.")
    for address in addresses:
        try:
            ip = ipaddress.ip_address(address[4][0])
        except ValueError as exc:
            raise ValueError("Адрес сервера изображения некорректен.") from exc
        if not ip.is_global:
            raise ValueError(
                "Для загрузки по адресу разрешены публичные HTTP/HTTPS-серверы. "
                "Локальные изображения выбирайте кнопкой «Выбрать файл»."
            )
    return raw


class _SafeImageRedirectHandler(urllib.request.HTTPRedirectHandler):
    """Повторно проверяет адрес каждого HTTP-перенаправления."""

    def redirect_request(self, req, fp, code, msg, headers, newurl):
        _validate_remote_image_url(newurl)
        return super().redirect_request(req, fp, code, msg, headers, newurl)


def _fetch_remote_image_data_url(value: str) -> str:
    """Загружает публичное изображение по URL с лимитом размера и проверкой декодирования."""
    image_url = _validate_remote_image_url(value)
    opener = urllib.request.build_opener(_SafeImageRedirectHandler())
    request = urllib.request.Request(
        image_url,
        headers={
            "User-Agent": "Mozilla/5.0 (compatible; FaceSearch/1.0)",
            "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.5",
        },
    )
    try:
        with opener.open(request, timeout=15) as response:
            content = response.read(MAX_IMAGE_BYTES + 1)
    except (urllib.error.URLError, TimeoutError, OSError, ValueError) as exc:
        raise ValueError(f"Не удалось загрузить изображение по URL: {exc}") from exc
    if not content:
        raise ValueError("Сервер вернул пустое изображение.")
    if len(content) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение слишком большое. Максимальный размер — 16 МБ.")

    # Декодирование проверяет формат и лимит пикселей до возврата данных браузеру.
    encoded = base64.b64encode(content).decode("ascii")
    image = _decode_image("data:application/octet-stream;base64," + encoded)
    ok, jpg = face_search.cv2.imencode(".jpg", image)
    if not ok:
        raise ValueError("Не удалось подготовить изображение для поиска.")
    return "data:image/jpeg;base64," + base64.b64encode(jpg.tobytes()).decode("ascii")


def _decode_image(data_url: str):
    """Декодирует data URL из браузера с ограничением размера и числа пикселей."""
    if not isinstance(data_url, str) or not data_url:
        raise ValueError("Сначала выберите изображение.")
    if "," in data_url and data_url[:100].lower().startswith("data:"):
        header, encoded = data_url.split(",", 1)
        if ";base64" not in header.lower():
            raise ValueError("Изображение должно быть передано в формате base64.")
    else:
        encoded = data_url

    # Проверяем длину до декодирования, чтобы не выделять неограниченную память.
    if len(encoded) > ((MAX_IMAGE_BYTES + 2) // 3) * 4 + 8:
        raise ValueError("Изображение слишком большое. Максимальный размер — 16 МБ.")
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (binascii.Error, ValueError) as exc:
        raise ValueError("Не удалось прочитать изображение из браузера.") from exc

    if not raw or len(raw) > MAX_IMAGE_BYTES:
        raise ValueError("Изображение пустое или превышает лимит 16 МБ.")

    face_search._ensure_runtime_dependencies(need_numpy=True, need_cv2=True)
    np = face_search.np
    cv2 = face_search.cv2
    image = cv2.imdecode(np.frombuffer(raw, dtype=np.uint8), cv2.IMREAD_COLOR)
    if image is None:
        raise ValueError("Формат изображения не поддерживается или файл повреждён.")
    height, width = image.shape[:2]
    if height <= 0 or width <= 0 or width * height > MAX_IMAGE_PIXELS:
        raise ValueError("Разрешение изображения слишком велико.")
    return image


def _prune_sessions() -> None:
    now = time.monotonic()
    with QUERY_SESSIONS_LOCK:
        expired = [
            token for token, value in QUERY_SESSIONS.items()
            if now - value["created"] > QUERY_TTL_SECONDS
        ]
        for token in expired:
            QUERY_SESSIONS.pop(token, None)
        while len(QUERY_SESSIONS) > MAX_QUERY_SESSIONS:
            oldest = min(
                QUERY_SESSIONS,
                key=lambda token: QUERY_SESSIONS[token]["created"],
            )
            QUERY_SESSIONS.pop(oldest, None)


def _json_bytes(payload: dict) -> bytes:
    return json.dumps(payload, ensure_ascii=False, separators=(",", ":")).encode("utf-8")


def _remember_face_image_rows(rows) -> None:
    """Кэширует связку face.id -> CROP/source, чтобы повторные image-запросы не били по БД."""
    with _FACE_IMAGE_CACHE_LOCK:
        for row in rows:
            # Input layout: face.id, images.id, path, filename, source_url, url_image.
            try:
                face_id = int(row[0])
                cached = (
                    int(row[1]),
                    str(row[2] or ""),
                    str(row[3] or ""),
                    str(row[4] or ""),
                    str(row[5] or ""),
                )
            except (TypeError, ValueError, IndexError):
                continue
            _FACE_IMAGE_CACHE[face_id] = cached
        while len(_FACE_IMAGE_CACHE) > _FACE_IMAGE_CACHE_MAX:
            _FACE_IMAGE_CACHE.pop(next(iter(_FACE_IMAGE_CACHE)))


def _get_face_image_row(face_id: int):
    with _FACE_IMAGE_CACHE_LOCK:
        return _FACE_IMAGE_CACHE.get(int(face_id))


def _start_crop_repair(face_ids) -> None:
    """Восстанавливает отсутствующие CROP для текущей выдачи в фоне, как GUI."""
    if not CREATE_CROPS_ON_HTML or FACE_APP is None:
        return
    try:
        requested = {int(value) for value in face_ids if int(value) > 0}
    except (TypeError, ValueError):
        return
    if not requested:
        return
    with _CROP_REPAIR_LOCK:
        pending = requested - _CROP_REPAIR_ACTIVE_IDS
        if not pending:
            return
        _CROP_REPAIR_ACTIVE_IDS.update(pending)

    def worker():
        conn = None
        try:
            conn = _open_connection()
            result = face_search.repair_missing_crops(
                conn, FACE_APP,
                workers=CROP_REPAIR_WORKERS,
                auto_workers=CROP_REPAIR_AUTO_WORKERS,
                face_ids=pending,
                delete_unavailable_records=False,
            )
            print(
                "[HTTP][CROP] Проверка завершена: "
                f"проверено={result.get('checked', 0)}, "
                f"создано={result.get('repaired', 0)}, "
                f"ошибок={result.get('failed', 0)}"
            )
        except Exception as exc:
            print(f"[HTTP][CROP] Не удалось восстановить CROP: {exc}")
        finally:
            if conn is not None:
                try:
                    conn.close()
                except Exception:
                    pass
            with _CROP_REPAIR_LOCK:
                _CROP_REPAIR_ACTIVE_IDS.difference_update(pending)

    threading.Thread(target=worker, name="facesearch-http-crop-repair", daemon=True).start()


PAGE_TEMPLATE = r"""<!doctype html>
<html lang="ru">
<head>
<meta charset="utf-8">
<meta name="viewport" content="width=device-width,initial-scale=1,viewport-fit=cover">
<title>FaceSearch — поиск лиц</title>
<link rel="icon" href="https://raw.githubusercontent.com/ORAMAN/FaceSearch/refs/heads/main/icons/SearchFace.ico" type="image/x-icon">
<style>
:root{
  color-scheme:dark;
  --bg:#151515;--panel:#242424;--line:#383838;--text:#eeeeee;--muted:#aaaaaa;
  --accent:#167ac6;--field:#1c1c1c;--input-line:#515b67;--drop:#1b1b1b;
  --drop-line:#626b76;--image-bg:#111111;--link:#79bfff;--score:#80bdff;
  --secondary:#333333;--secondary-hover:#444444;--info-bg:#1e1e1e;--info-line:#353535;
  --button-text:#eeeeee;--button-line:#484848;--shadow:0 4px 15px rgba(0,0,0,.22)
}
:root[data-theme="light"]{
  color-scheme:light;
  --bg:#f4f5f7;--panel:#ffffff;--line:#c8cfd8;--text:#202124;--muted:#62666b;
  --accent:#1479c9;--field:#ffffff;--input-line:#b7c0cb;--drop:#ffffff;
  --drop-line:#aab4c0;--image-bg:#e9eaec;--link:#075eb5;--score:#075eb5;
  --secondary:#e5e7eb;--secondary-hover:#d1d5db;--info-bg:#e9edf2;--info-line:#c4ccd5;
  --button-text:#202124;--button-line:#b7c0cb;--shadow:0 4px 15px rgba(30,40,55,.10)
}
*{box-sizing:border-box}
html{min-height:100%;background:var(--bg)}
body{margin:0;background:var(--bg);color:var(--text);font:15px/1.5 system-ui,"Segoe UI",Arial,sans-serif;transition:background-color .18s,color .18s}
main{max-width:1600px;margin:0 auto;padding:28px 22px 60px}
h1{font-size:28px;line-height:1.2;margin:0 0 8px}
.subtitle{color:var(--muted);margin:0 0 24px}
.toolbar{display:flex;gap:14px;align-items:end;flex-wrap:wrap;padding:18px;background:var(--panel);border:1px solid var(--line);border-radius:14px;margin-bottom:18px;box-shadow:var(--shadow)}
.field{display:flex;flex-direction:column;gap:5px;min-width:150px}
.field label,.toggle-label{color:var(--muted);font-size:13px}
.url-field{flex:1 1 100%;min-width:0}
.url-row{display:flex;gap:10px;align-items:stretch;min-width:0}
.url-row input{flex:1 1 auto;min-width:0;width:100%;background:var(--field);color:var(--text);border:1px solid var(--input-line);border-radius:8px;padding:10px;font:inherit}
.url-row button{flex:0 0 auto}
#file-input{display:none!important}
input[type=number]{width:145px;max-width:100%;background:var(--field);color:var(--text);border:1px solid var(--input-line);border-radius:8px;padding:10px;font:inherit}
input:focus-visible,button:focus-visible,a:focus-visible,canvas:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
button{border:0;border-radius:9px;padding:11px 17px;background:var(--accent);color:#fff;font:600 14px system-ui;cursor:pointer;min-height:42px;transition:filter .12s}
button:hover:not(:disabled){filter:brightness(1.08)}
button:disabled{opacity:.42;cursor:not-allowed}
.secondary{background:var(--secondary);color:var(--button-text);border:1px solid var(--button-line)}
.hint{color:var(--muted);font-size:13px}
.toggle-field{display:flex;flex-direction:column;justify-content:flex-end;gap:7px;min-height:42px}
.toggle-label{display:flex;align-items:center;gap:8px;min-height:42px;color:var(--text);font-size:14px;cursor:pointer}
input[type=checkbox]{width:18px;height:18px;margin:0;accent-color:var(--accent);cursor:pointer;flex:0 0 auto}
.dropzone{border:1px dashed var(--drop-line);background:var(--drop);border-radius:14px;min-height:210px;display:flex;align-items:center;justify-content:center;padding:18px;text-align:center;position:relative}
.dropzone.drag{border-color:var(--accent);background:var(--panel)}
.dropzone-content{max-width:650px;width:100%}
.dropzone input{display:block;max-width:100%;margin:13px auto}
#preview{display:none;max-width:100%;max-height:440px;object-fit:contain}
#preview-canvas{display:none;max-width:100%;max-height:440px;width:auto;height:auto;object-fit:contain;cursor:crosshair}
.status{min-height:28px;color:var(--muted);padding:10px 2px;overflow-wrap:anywhere}
.status.error{color:#c83e3e}
#search-button{margin:6px 0 18px}
.section-title{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:23px 0 14px}
.section-title h2{font-size:20px;margin:0}
.section-title span{color:var(--muted);font-size:13px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,265px),1fr));gap:18px}
.card{position:relative;min-width:0;overflow:hidden;border:1px solid var(--line);background:var(--panel);border-radius:12px;padding-bottom:14px;box-shadow:var(--shadow);transition:transform .14s,box-shadow .14s}
.card:hover{transform:translateY(-2px)}
.card-image{height:270px;padding:4px;position:relative;background:var(--image-bg);display:flex;align-items:center;justify-content:center;overflow:hidden}
.card-image img{display:block;width:100%;height:100%;object-fit:contain;border-radius:3px}
.no-image[hidden]{display:none}.no-image{display:flex;align-items:center;justify-content:center;text-align:center;padding:18px;color:var(--muted);font-size:13px}
.rank,.score{position:absolute;top:10px;z-index:2;padding:5px 9px;color:#fff;background:rgba(0,0,0,.82);border-radius:6px;text-shadow:0 1px 2px rgba(0,0,0,.35);font-size:13px}
.rank{left:10px}
.score{right:10px;background:rgba(0,112,205,.92);font-weight:700}
.card-body{padding:0 15px}
.card-title{font-weight:700;line-height:1.35;overflow-wrap:anywhere;padding:10px 0;color:var(--text);border-bottom:1px solid var(--line)}
.card-meta{font-size:12px;color:var(--muted);overflow-wrap:anywhere;margin-top:7px}
.card-meta a,.source-site a{color:var(--link);text-decoration:none}
.card-meta a:hover,.source-site a:hover{text-decoration:underline}
.source-site{display:flex;align-items:center;gap:7px;margin-top:10px;font-size:12px;min-height:22px;min-width:0}
.source-site a{overflow:hidden;text-overflow:ellipsis;white-space:nowrap}
.favicon{width:16px;height:16px;object-fit:contain;flex:0 0 16px}
.card-info{margin:10px 0 12px;padding:10px;background:var(--info-bg);border:1px solid var(--info-line);border-radius:8px;line-height:1.55;font-size:13px;overflow-wrap:anywhere}
.card-info .info-name{color:var(--link);font-size:15px;font-weight:700;margin-bottom:5px}
.card-info-row + .card-info-row{margin-top:3px}
.card-info-row b{font-weight:650}
.card-info-row a{color:var(--link);text-decoration:underline;text-underline-offset:2px}
.card-info-row a:hover{text-decoration-thickness:2px}
.card-buttons{display:flex;gap:7px;margin-top:12px}
.card-button{display:flex;flex:1 1 0;min-width:0;align-items:center;justify-content:center;text-align:center;overflow-wrap:anywhere;padding:8px 10px;background:var(--secondary);color:var(--button-text);text-decoration:none;border:1px solid var(--button-line);border-radius:6px;font-size:13px}
.card-button:hover{background:var(--secondary-hover)}
.empty{color:var(--muted);padding:30px 4px}
.footer{margin-top:38px;color:var(--muted);font-size:12px}
.mobile-only{display:none}
.camera-modal{display:none;position:fixed;inset:0;z-index:9998;align-items:center;justify-content:center;padding:16px;background:rgba(0,0,0,.88)}
.camera-modal.open{display:flex}
.camera-dialog{width:min(720px,100%);max-height:94vh;overflow:auto;background:var(--panel);color:var(--text);border:1px solid var(--line);border-radius:14px;padding:16px;box-shadow:var(--shadow)}
.camera-header{display:flex;align-items:center;justify-content:space-between;gap:12px;margin-bottom:12px;font-size:18px}
.camera-close{width:42px;height:42px;padding:0;font-size:26px}
.camera-view{position:relative;display:flex;align-items:center;justify-content:center;min-height:200px;max-height:68vh;background:#050505;border-radius:10px;overflow:hidden}
#camera-video{display:block;width:100%;max-height:68vh;object-fit:contain}
.camera-placeholder{position:absolute;inset:0;display:flex;align-items:center;justify-content:center;padding:20px;text-align:center;color:#fff;background:#101010}
.camera-placeholder[hidden]{display:none}
.camera-instruction{display:block;position:relative;clear:both;margin:10px 0 14px;line-height:1.45;overflow-wrap:anywhere}
.camera-actions{position:relative;clear:both;display:flex;gap:10px;justify-content:flex-end;margin-top:10px;flex-wrap:wrap}
.camera-actions button{min-width:110px}
.lightbox{display:none;position:fixed;inset:0;z-index:9999;align-items:center;justify-content:center;padding:58px 20px 70px;background:rgba(0,0,0,.94);backdrop-filter:blur(3px);cursor:zoom-out}
.lightbox.open{display:flex}
#lightbox-image{display:block;max-width:96vw;max-height:86vh;width:auto;height:auto;object-fit:contain;border-radius:4px;box-shadow:0 8px 40px rgba(0,0,0,.55);cursor:default}
.lightbox-close{position:fixed;top:14px;right:18px;z-index:2;width:46px;height:46px;padding:0;border:1px solid rgba(255,255,255,.3);border-radius:50%;color:#fff;background:rgba(35,35,35,.9);font-size:32px;line-height:40px;text-align:center}
.lightbox-nav{position:fixed;top:50%;z-index:2;width:46px;height:56px;padding:0;border:1px solid rgba(255,255,255,.25);border-radius:9px;color:#fff;background:rgba(45,45,45,.85);font-size:40px;line-height:44px}
.lightbox-prev{left:12px}.lightbox-next{right:12px}
.lightbox-counter{position:fixed;bottom:22px;left:0;right:0;color:#fff;text-align:center;font-size:14px;pointer-events:none}
@media(max-width:700px){
  main{padding:20px 14px 42px}
  .toolbar{align-items:stretch;padding:14px;gap:12px}
  .url-row{display:flex;flex-direction:column;gap:8px}
  .url-row button{width:100%}
  .toolbar .url-field{flex:1 1 100%;width:100%;min-width:0}
  .toolbar .numeric-field{flex:1 1 100%;width:100%;min-width:0}
  .toolbar .numeric-field input[type=number]{width:100%;min-width:0}
  .toolbar .action-field{flex:1 1 calc(50% - 12px);min-width:0}
  .toolbar .action-field button{width:100%;height:100%;padding:10px 8px;white-space:normal}
  .toggle-field{flex:1 1 100%;align-items:flex-start}
  .toggle-label{min-height:36px}
  .format-hint{display:block;position:relative;clear:both;flex:1 1 100%;width:100%;margin-top:7px;line-height:1.45;overflow-wrap:anywhere}
  .toolbar .mobile-only.action-field{flex:1 1 100%;width:100%;min-width:0}
  .toolbar .mobile-only.action-field button{width:100%;min-height:44px}
  .mobile-only{display:block;width:100%;margin:8px auto 0}
  .gallery{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .card-image{height:clamp(150px,38vw,230px)}
  .card-body{padding:0 10px}
  .card-info{padding:8px;margin:8px 0 10px}
  .card-buttons{flex-direction:column}
  .card-button{flex:1 1 auto}
  .camera-modal{padding:8px}
  .camera-dialog{padding:12px}
  .camera-view{min-height:180px;max-height:62vh}
  #camera-video{max-height:62vh}
  .camera-instruction{margin:8px 0 12px;font-size:13px}
  .camera-actions{display:grid;grid-template-columns:1fr 1fr;position:relative;clear:both;gap:8px}
  .camera-actions button{width:100%;min-width:0}
  .lightbox{padding:64px 10px}
  .lightbox-prev{left:5px}.lightbox-next{right:5px}
  .lightbox-nav{width:36px;height:48px;font-size:34px}
  #lightbox-image{max-width:98vw;max-height:82vh}
}
@media(max-width:380px){
  main{padding:16px 10px 32px}
  h1{font-size:25px}
  .gallery{grid-template-columns:minmax(0,1fr)}
  .card-image{height:230px}
  .toolbar{padding:12px}
}
</style>
</head>
<body>
<main>
  <h1>FaceSearch</h1>
  <p class="subtitle">Загрузите изображение — определение лиц и поиск по первому найденному лицу запустятся автоматически. На групповом фото можно выбрать другое лицо и повторить поиск.</p>
  <section class="toolbar" aria-label="Параметры поиска">
    <div class="field url-field">
      <label for="image-url">Адрес изображения (HTTP/HTTPS)</label>
      <div class="url-row">
        <input id="image-url" type="url" inputmode="url" autocomplete="url" placeholder="https://example.com/photo.jpg">
        <button type="button" id="load-url-button" class="secondary">Загрузить адрес</button>
      </div>
      <span class="hint">Для локального изображения используйте кнопку «Выбрать файл» ниже.</span>
    </div>
    <div class="field numeric-field">
      <label for="similarity">Минимальное сходство, %</label>
      <input id="similarity" class="numeric-input" type="number" min="0" max="100" step="1" value="__DEFAULT_MIN_SIMILARITY__">
    </div>
    <div class="field numeric-field">
      <label for="top-k">Количество результатов</label>
      <input id="top-k" class="numeric-input" type="number" min="1" max="500" step="1" value="__DEFAULT_TOP_K__">
    </div>
    <div class="toggle-field">
      <label class="toggle-label" for="use-crop"><input id="use-crop" type="checkbox" __CROP_CHECKED__> Использовать CROP</label>
      <span class="hint">Показывать локальные вырезки лиц, если они доступны.</span>
    </div>
    <div class="field action-field" style="min-width:0">
      <label>&nbsp;</label>
      <button type="button" id="theme-button" class="secondary" aria-pressed="false">☀️ Дневная тема</button>
    </div>
    <div class="field action-field" style="min-width:0">
      <label>&nbsp;</label>
      <button type="button" id="detect-button" class="secondary">Определить лица</button>
    </div>
    <div class="field action-field">
      <label>&nbsp;</label>
      <button type="button" id="file-button" class="secondary">Выбрать файл</button>
      <input id="file-input" type="file" accept="image/*" hidden>
    </div>
    <div class="field action-field mobile-only">
      <label>&nbsp;</label>
      <button type="button" id="camera-button" class="secondary">📷 Сделать снимок</button>
      <input id="camera-input" type="file" accept="image/*" capture="environment" hidden>
    </div>
    <span class="hint format-hint">Поддерживаются JPG, PNG, WEBP и другие форматы изображений.</span>
  </section>

  <section id="dropzone" class="dropzone">
    <div class="dropzone-content" id="drop-copy">
      <strong>Перетащите фотографию сюда</strong><br>
      <span class="hint">Также можно вставить изображение из буфера обмена.</span>
    </div>
    <canvas id="preview-canvas" aria-label="Предпросмотр. Нажмите на рамку нужного лица, чтобы выбрать его."></canvas>
  </section>
  <div id="camera-modal" class="camera-modal" aria-hidden="true" role="dialog" aria-modal="true" aria-label="Съёмка фотографии">
    <div class="camera-dialog">
      <div class="camera-header">
        <strong>Сделать снимок</strong>
        <button type="button" id="camera-close" class="secondary camera-close" aria-label="Закрыть камеру">×</button>
      </div>
      <div class="camera-view">
        <video id="camera-video" autoplay playsinline muted></video>
        <div id="camera-placeholder" class="camera-placeholder">Подключение к камере…</div>
      </div>
      <p class="hint camera-instruction">Расположите лицо в кадре и нажмите «Снять фото». Снимок автоматически загрузится и запустит поиск.</p>
      <div class="camera-actions">
        <button type="button" id="camera-cancel" class="secondary">Отмена</button>
        <button type="button" id="camera-capture">Снять фото</button>
      </div>
    </div>
  </div>
  <div id="status" class="status" role="status">Выберите изображение, затем нажмите «Определить лица».</div>
  <button type="button" id="search-button" disabled>Искать похожее лицо</button>

  <section>
    <div class="section-title"><h2>Результаты</h2><span id="result-count"></span></div>
    <div id="gallery" class="gallery"><div class="empty">Здесь появятся найденные совпадения.</div></div>
  </section>
  <div id="image-lightbox" class="lightbox" aria-hidden="true" role="dialog" aria-modal="true" aria-label="Просмотр изображения">
    <button type="button" id="lightbox-close" class="lightbox-close" aria-label="Закрыть просмотр">×</button>
    <button type="button" id="lightbox-previous" class="lightbox-nav lightbox-prev" aria-label="Предыдущее изображение">‹</button>
    <img id="lightbox-image" alt="Увеличенное изображение">
    <button type="button" id="lightbox-next" class="lightbox-nav lightbox-next" aria-label="Следующее изображение">›</button>
    <div id="lightbox-counter" class="lightbox-counter"></div>
  </div>
  <div class="footer">Процент сходства — мера визуальной близости embeddings, а не подтверждение личности. Доступ к серверу следует ограничивать доверенной сетью.</div>
</main>
<script>
(function(){
  const fileInput=document.getElementById("file-input");
  const fileButton=document.getElementById("file-button");
  const imageUrlInput=document.getElementById("image-url");
  const loadUrlButton=document.getElementById("load-url-button");
  const cameraInput=document.getElementById("camera-input");
  const cameraButton=document.getElementById("camera-button");
  const cameraModal=document.getElementById("camera-modal");
  const cameraVideo=document.getElementById("camera-video");
  const cameraPlaceholder=document.getElementById("camera-placeholder");
  const cameraCaptureButton=document.getElementById("camera-capture");
  const cameraCloseButton=document.getElementById("camera-close");
  const cameraCancelButton=document.getElementById("camera-cancel");
  const dropzone=document.getElementById("dropzone");
  const dropCopy=document.getElementById("drop-copy");
  const canvas=document.getElementById("preview-canvas");
  const ctx=canvas.getContext("2d");
  const statusBox=document.getElementById("status");
  const detectButton=document.getElementById("detect-button");
  const searchButton=document.getElementById("search-button");
  const gallery=document.getElementById("gallery");
  const resultCount=document.getElementById("result-count");
  const similarityInput=document.getElementById("similarity");
  const topKInput=document.getElementById("top-k");
  const useCropCheckbox=document.getElementById("use-crop");
  const themeButton=document.getElementById("theme-button");
  const lightbox=document.getElementById("image-lightbox");
  const lightboxImage=document.getElementById("lightbox-image");
  const lightboxClose=document.getElementById("lightbox-close");
  const lightboxPrevious=document.getElementById("lightbox-previous");
  const lightboxNext=document.getElementById("lightbox-next");
  const lightboxCounter=document.getElementById("lightbox-counter");
  const CREATE_CROPS_ON_HTML=__CREATE_CROPS_ON_HTML__;
  let dataUrl="";
  let imageObject=null;
  let queryId="";
  let faces=[];
  let selectedFace=-1;
  let busy=false;
  let lightboxItems=[];
  let lightboxIndex=-1;
  let cameraStream=null;
  let cameraOpening=false;

  function applyTheme(theme){
    const value=theme==="light"?"light":"dark";
    document.documentElement.dataset.theme=value;
    document.documentElement.style.colorScheme=value;
    themeButton.textContent=value==="dark"?"☀️ Дневная тема":"🌙 Ночная тема";
    themeButton.setAttribute("aria-pressed",String(value==="light"));
    try{localStorage.setItem("facesearch-http-theme",value);}catch(e){}
  }
  let initialTheme="__DEFAULT_THEME__";
  try{initialTheme=localStorage.getItem("facesearch-http-theme")||initialTheme;}catch(e){}
  applyTheme(initialTheme);
  themeButton.addEventListener("click",function(){
    applyTheme(document.documentElement.dataset.theme==="dark"?"light":"dark");
  });

  function faceImageUrl(faceId,useCrop,refresh){
    return "/api/face/"+encodeURIComponent(faceId)+"/image?crop="+(useCrop?"1":"0")+(refresh?"&refresh="+Date.now():"");
  }
  function showImageFailure(image){
    const box=image.parentElement;
    const placeholder=box&&box.querySelector(".no-image");
    image.style.display="none";
    if(placeholder)placeholder.hidden=false;
  }
  function configureCardImage(image,faceId,useCrop,refresh){
    image.dataset.faceId=String(faceId);
    image.dataset.cropEnabled=String(Boolean(useCrop));
    image.dataset.cropRetryCount="0";
    image.style.display="block";
    const box=image.parentElement;
    const placeholder=box&&box.querySelector(".no-image");
    if(placeholder)placeholder.hidden=true;
    if(image._cropRetryTimer){clearTimeout(image._cropRetryTimer);image._cropRetryTimer=null;}
    image.onload=function(){
      image.style.display="block";
      if(placeholder)placeholder.hidden=true;
      if(image._cropRetryTimer){clearTimeout(image._cropRetryTimer);image._cropRetryTimer=null;}
    };
    image.onerror=function(){
      if(image.dataset.cropEnabled==="true"&&CREATE_CROPS_ON_HTML){
        const retries=Number(image.dataset.cropRetryCount||"0");
        if(retries<180){
          image.dataset.cropRetryCount=String(retries+1);
          if(image._cropRetryTimer)clearTimeout(image._cropRetryTimer);
          image._cropRetryTimer=setTimeout(function(){
            if(image.dataset.cropEnabled!=="true")return;
            image.src=faceImageUrl(image.dataset.faceId,true,true)+"&crop_retry="+Date.now();
          },1500);
          return;
        }
        // Как в HTML-отчёте, после ожидания переключаемся на оригинал.
        image.dataset.cropEnabled="false";
        image.src=faceImageUrl(image.dataset.faceId,false,true);
        return;
      }
      showImageFailure(image);
    };
    image.onclick=function(){openLightboxForImage(image);};
    image.src=faceImageUrl(faceId,useCrop,refresh);
  }
  function refreshCardImages(){
    gallery.querySelectorAll("img[data-face-image='true']").forEach(function(image){
      configureCardImage(image,image.dataset.faceId,useCropCheckbox.checked,true);
    });
  }
  useCropCheckbox.addEventListener("change",refreshCardImages);

  function openLightboxForImage(image){
    lightboxItems=Array.from(gallery.querySelectorAll("img[data-face-image='true']"));
    lightboxIndex=lightboxItems.indexOf(image);
    if(lightboxIndex<0)return;
    displayLightboxImage();
    lightbox.classList.add("open");
    lightbox.setAttribute("aria-hidden","false");
    document.body.style.overflow="hidden";
    lightboxClose.focus();
  }
  function displayLightboxImage(){
    if(lightboxIndex<0||lightboxIndex>=lightboxItems.length)return;
    const current=lightboxItems[lightboxIndex];
    lightboxImage.alt=current.alt||"Увеличенное изображение";
    // В HTML-отчёте просмотр открывает оригинал, а не только локальную вырезку.
    lightboxImage.src=faceImageUrl(current.dataset.faceId,false,true);
    lightboxCounter.textContent=(lightboxIndex+1)+" / "+lightboxItems.length;
    const navigation=lightboxItems.length>1;
    lightboxPrevious.hidden=!navigation;
    lightboxNext.hidden=!navigation;
  }
  function moveLightbox(step){
    if(!lightboxItems.length)return;
    lightboxIndex=(lightboxIndex+step+lightboxItems.length)%lightboxItems.length;
    displayLightboxImage();
  }
  function closeLightbox(){
    lightbox.classList.remove("open");
    lightbox.setAttribute("aria-hidden","true");
    lightboxImage.removeAttribute("src");
    document.body.style.overflow="";
    lightboxIndex=-1;
  }
  lightboxClose.addEventListener("click",closeLightbox);
  lightboxPrevious.addEventListener("click",function(){moveLightbox(-1);});
  lightboxNext.addEventListener("click",function(){moveLightbox(1);});
  lightbox.addEventListener("click",function(event){if(event.target===lightbox)closeLightbox();});
  document.addEventListener("keydown",function(event){
    if(!lightbox.classList.contains("open"))return;
    if(event.key==="Escape")closeLightbox();
    else if(event.key==="ArrowLeft")moveLightbox(-1);
    else if(event.key==="ArrowRight")moveLightbox(1);
  });

  function setStatus(message,isError){
    statusBox.textContent=message;
    statusBox.classList.toggle("error",Boolean(isError));
  }
  function setBusy(value){
    busy=value;
    detectButton.disabled=value||!dataUrl;
    searchButton.disabled=value||!queryId||selectedFace<0||!faces.length;
    fileInput.disabled=value;
    fileButton.disabled=value;
    imageUrlInput.disabled=value;
    loadUrlButton.disabled=value;
    cameraInput.disabled=value;
    cameraButton.disabled=value||cameraOpening;
  }
  function useDataUrl(sourceDataUrl){
    dataUrl=String(sourceDataUrl||"");
    imageObject=new Image();
    imageObject.onload=function(){
      queryId="";faces=[];selectedFace=-1;
      canvas.width=imageObject.naturalWidth;
      canvas.height=imageObject.naturalHeight;
      ctx.clearRect(0,0,canvas.width,canvas.height);
      ctx.drawImage(imageObject,0,0);
      canvas.style.display="block";dropCopy.style.display="none";
      searchButton.disabled=true;
      gallery.replaceChildren();
      const empty=document.createElement("div");empty.className="empty";
      empty.textContent="После поиска здесь появятся совпадения.";
      gallery.appendChild(empty);resultCount.textContent="";
      setStatus("Изображение загружено. Автоматически определяю лица...");
      setBusy(false);
      detectFaces(true);
    };
    imageObject.onerror=function(){setStatus("Не удалось открыть изображение.",true);};
    imageObject.src=dataUrl;
  }
  function useFile(file){
    if(!file||!file.type||!file.type.startsWith("image/")){
      setStatus("Выберите файл изображения.",true);return;
    }
    if(file.size>16*1024*1024){
      setStatus("Файл слишком большой. Максимальный размер — 16 МБ.",true);return;
    }
    const reader=new FileReader();
    reader.onload=function(){useDataUrl(String(reader.result||""));};
    reader.onerror=function(){setStatus("Не удалось прочитать файл.",true);};
    reader.readAsDataURL(file);
  }
  fileButton.addEventListener("click",function(){if(!busy){fileInput.value="";fileInput.click();}});
  fileInput.addEventListener("change",function(){useFile(fileInput.files&&fileInput.files[0]);});
  async function loadImageFromAddress(){
    const address=String(imageUrlInput.value||"").trim();
    if(!address){setStatus("Введите HTTP/HTTPS-адрес изображения.",true);imageUrlInput.focus();return;}
    if(!/^https?:\/\//i.test(address)){
      setStatus("Введите полный адрес, начинающийся с http:// или https://. Для локальных изображений нажмите «Выбрать файл».",true);return;
    }
    setBusy(true);setStatus("Загружаю изображение по адресу...");
    try{
      const response=await postJson("/api/load-image",{url:address});
      if(!response.image)throw new Error("Сервер не вернул изображение.");
      useDataUrl(response.image);
    }catch(error){
      setStatus(error.message||String(error),true);
      setBusy(false);
    }
  }
  loadUrlButton.addEventListener("click",loadImageFromAddress);
  imageUrlInput.addEventListener("keydown",function(event){
    if(event.key==="Enter"){event.preventDefault();loadImageFromAddress();}
  });
  cameraInput.addEventListener("change",function(){
    const file=cameraInput.files&&cameraInput.files[0];
    if(file)useFile(file);
    else setStatus("Съёмка отменена. Можно попробовать ещё раз.");
  });

  function stopCameraStream(){
    if(cameraStream){
      cameraStream.getTracks().forEach(function(track){try{track.stop();}catch(e){}});
      cameraStream=null;
    }
    cameraVideo.srcObject=null;
  }
  function closeCamera(){
    stopCameraStream();
    cameraModal.classList.remove("open");
    cameraModal.setAttribute("aria-hidden","true");
    cameraOpening=false;
    cameraCaptureButton.disabled=false;
    cameraButton.disabled=busy;
    cameraButton.focus();
  }
  async function openCamera(){
    if(busy||cameraOpening)return;
    // getUserMedia is available only in secure contexts (HTTPS or localhost).
    // On a phone opening the server over plain HTTP by LAN IP, use the system
    // camera/file input as a safe fallback; the returned photo still auto-searches.
    if(!window.isSecureContext||!navigator.mediaDevices||!navigator.mediaDevices.getUserMedia){
      setStatus("Для камеры внутри страницы нужен HTTPS. Открываю системную камеру: сделайте снимок и подтвердите его — он автоматически попадёт в поиск.");
      cameraInput.value="";
      cameraInput.click();
      return;
    }
    cameraOpening=true;
    cameraButton.disabled=true;
    cameraPlaceholder.hidden=false;
    cameraPlaceholder.textContent="Подключение к камере…";
    cameraModal.classList.add("open");
    cameraModal.setAttribute("aria-hidden","false");
    try{
      try{
        cameraStream=await navigator.mediaDevices.getUserMedia({
          audio:false,video:{facingMode:{ideal:"environment"}}
        });
      }catch(firstError){
        // Older mobile browsers may not understand facingMode constraints.
        cameraStream=await navigator.mediaDevices.getUserMedia({audio:false,video:true});
      }
      cameraVideo.srcObject=cameraStream;
      await cameraVideo.play();
      cameraPlaceholder.hidden=true;
      cameraCaptureButton.disabled=false;
      cameraCaptureButton.focus();
      setStatus("Камера готова. Нажмите «Снять фото», чтобы передать снимок в поиск.");
    }catch(error){
      closeCamera();
      const reason=error&&error.name==="NotAllowedError"
        ?"Доступ к камере запрещён. Разрешите доступ в настройках браузера."
        :error&&error.name==="NotFoundError"
          ?"Камера не найдена на устройстве."
          :"Не удалось запустить камеру: "+(error&&error.message?error.message:"неизвестная ошибка");
      setStatus(reason,true);
    }
  }
  function captureCameraPhoto(){
    if(!cameraStream||!cameraVideo.videoWidth||!cameraVideo.videoHeight){
      setStatus("Камера ещё не готова. Подождите немного и повторите.",true);return;
    }
    const snapshot=document.createElement("canvas");
    snapshot.width=cameraVideo.videoWidth;
    snapshot.height=cameraVideo.videoHeight;
    const snapshotContext=snapshot.getContext("2d");
    if(!snapshotContext){setStatus("Не удалось подготовить снимок.",true);return;}
    snapshotContext.drawImage(cameraVideo,0,0,snapshot.width,snapshot.height);
    cameraCaptureButton.disabled=true;
    snapshot.toBlob(function(blob){
      if(!blob){
        cameraCaptureButton.disabled=false;
        setStatus("Не удалось получить снимок с камеры.",true);return;
      }
      const photo=new File([blob],"facesearch-camera-"+Date.now()+".jpg",{type:"image/jpeg",lastModified:Date.now()});
      closeCamera();
      useFile(photo);
    },"image/jpeg",0.92);
  }
  cameraButton.addEventListener("click",openCamera);
  cameraCaptureButton.addEventListener("click",captureCameraPhoto);
  cameraCloseButton.addEventListener("click",closeCamera);
  cameraCancelButton.addEventListener("click",closeCamera);
  cameraModal.addEventListener("click",function(event){if(event.target===cameraModal)closeCamera();});
  document.addEventListener("keydown",function(event){
    if(cameraModal.classList.contains("open")&&event.key==="Escape")closeCamera();
  });
  ["dragenter","dragover"].forEach(function(name){
    dropzone.addEventListener(name,function(event){event.preventDefault();dropzone.classList.add("drag");});
  });
  ["dragleave","drop"].forEach(function(name){
    dropzone.addEventListener(name,function(event){event.preventDefault();dropzone.classList.remove("drag");});
  });
  dropzone.addEventListener("drop",function(event){
    const file=event.dataTransfer&&event.dataTransfer.files&&event.dataTransfer.files[0];
    if(file)useFile(file);
  });
  document.addEventListener("paste",function(event){
    const items=event.clipboardData&&event.clipboardData.items;
    if(!items)return;
    for(let i=0;i<items.length;i++){
      if(items[i].type&&items[i].type.startsWith("image/")){
        const file=items[i].getAsFile();if(file)useFile(file);event.preventDefault();return;
      }
    }
  });

  function drawFaces(){
    if(!imageObject)return;
    ctx.clearRect(0,0,canvas.width,canvas.height);
    ctx.drawImage(imageObject,0,0);
    const scale=Math.max(1,canvas.width/450);
    faces.forEach(function(face,index){
      const b=face.bbox;
      ctx.lineWidth=Math.max(2,2.5*scale);
      ctx.strokeStyle=index===selectedFace?"#4dffad":"#4d9cf5";
      ctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);
      const label="Лицо "+(index+1)+" · "+Math.round(face.confidence*100)+"%";
      ctx.font=(14*scale)+"px sans-serif";
      const padding=5*scale, labelHeight=22*scale, labelWidth=ctx.measureText(label).width+padding*2;
      const y=Math.max(0,b[1]-labelHeight);
      ctx.fillStyle=index===selectedFace?"#147a4b":"#1763a5";
      ctx.fillRect(b[0],y,labelWidth,labelHeight);
      ctx.fillStyle="#ffffff";ctx.fillText(label,b[0]+padding,y+16*scale);
    });
  }
  canvas.addEventListener("click",function(event){
    if(!faces.length)return;
    const rect=canvas.getBoundingClientRect();
    const x=(event.clientX-rect.left)*canvas.width/rect.width;
    const y=(event.clientY-rect.top)*canvas.height/rect.height;
    let best=-1,bestArea=Infinity;
    faces.forEach(function(face,index){
      const b=face.bbox;
      if(x>=b[0]&&x<=b[2]&&y>=b[1]&&y<=b[3]){
        const area=(b[2]-b[0])*(b[3]-b[1]);
        if(area<bestArea){best=index;bestArea=area;}
      }
    });
    if(best>=0){
      selectedFace=best;drawFaces();setBusy(false);
      setStatus("Выбрано лицо "+(best+1)+". Можно запускать поиск.");
    }
  });

  async function postJson(url,payload){
    const response=await fetch(url,{method:"POST",headers:{"Content-Type":"application/json"},body:JSON.stringify(payload)});
    let data={};
    try{data=await response.json();}catch(e){throw new Error("Сервер вернул некорректный ответ.");}
    if(!response.ok)throw new Error(data.error||("Ошибка HTTP "+response.status));
    return data;
  }
  async function detectFaces(autoSearch){
    if(!dataUrl||busy)return;
    let detected=false;
    setBusy(true);setStatus("Определение лиц...");
    try{
      const data=await postJson("/api/detect",{image:dataUrl});
      queryId=data.query_id||"";faces=data.faces||[];
      selectedFace=faces.length?0:-1;drawFaces();
      if(!faces.length){
        queryId="";setStatus("Лица не обнаружены. Попробуйте другое изображение.");return;
      }
      detected=true;
      setStatus("Обнаружено лиц: "+faces.length+". "+(autoSearch?"Запускаю поиск по первому лицу...":"Выберите рамку лица и запустите поиск."));
    }catch(error){setStatus(error.message||String(error),true);queryId="";faces=[];selectedFace=-1;}
    finally{setBusy(false);}
    if(detected&&autoSearch)await searchSimilar();
  }
  detectButton.addEventListener("click",function(){detectFaces(false);});

  function safeLink(value){
    if(!value)return "";
    try{const url=new URL(value,window.location.href);return (url.protocol==="http:"||url.protocol==="https:")?url.href:"";}
    catch(e){return "";}
  }
  const LOCAL_INFO_LABELS={
    filename:"Файл",date_taken:"Дата съёмки",date_digitized:"Дата оцифровки",
    file_modified:"Изменён",image_width:"Ширина",image_height:"Высота",
    file_size_bytes:"Размер файла",camera_make:"Производитель камеры",
    camera_model:"Камера",lens_model:"Объектив",exposure_time:"Выдержка",
    f_number:"Диафрагма f/",iso:"ISO",focal_length_mm:"Фокусное расстояние, мм",
    gps_latitude:"Широта",gps_longitude:"Долгота",gps_altitude_m:"Высота над уровнем моря",
    artist:"Автор",copyright:"Авторские права",description:"Описание",software:"ПО"
  };
  function displayValue(value){
    if(value===null||value===undefined)return "";
    if(typeof value==="object"){
      try{return JSON.stringify(value);}catch(e){return String(value);}
    }
    return String(value);
  }
  function normalizedGeoKey(key){
    return String(key||"").toLowerCase().replace(/[^a-z0-9а-яё]/gi,"");
  }
  function geoFieldType(key){
    const normalized=normalizedGeoKey(key);
    if(["gpslatitude","latitude","lat","широта","gpsширота"].includes(normalized))return "latitude";
    if(["gpslongitude","longitude","long","lon","lng","долгота","gpsдолгота"].includes(normalized))return "longitude";
    if([
      "gpsaltitudem","gpsaltitude","altitudem","altitude","elevation","elevationm",
      "heightabovesealevel","высота","gpsвысота","высотанадуровнемморя","высотанадморем"
    ].includes(normalized))return "altitude";
    if([
      "gpscoordinates","gpscoordinate","coordinates","coordinate","coords",
      "координаты","координатыgps","геокоординаты","географическиекоординаты"
    ].includes(normalized))return "coordinates";
    return "";
  }
  function geoNumber(value){
    if(typeof value==="number")return Number.isFinite(value)?value:null;
    if(typeof value!=="string"||!value.trim())return null;
    const number=Number(value.trim().replace(",","."));
    return Number.isFinite(number)?number:null;
  }
  function parseCoordinatePair(value){
    if(Array.isArray(value)&&value.length>=2){
      return {latitude:geoNumber(value[0]),longitude:geoNumber(value[1])};
    }
    if(value&&typeof value==="object"){
      const latitude=value.latitude??value.lat??value.gps_latitude;
      const longitude=value.longitude??value.lon??value.lng??value.gps_longitude;
      return {latitude:geoNumber(latitude),longitude:geoNumber(longitude)};
    }
    const text=String(value??"").trim();
    const semicolonParts=text.split(/[;|]/).map(function(part){return part.trim();});
    if(semicolonParts.length===2){
      const latitude=geoNumber(semicolonParts[0]),longitude=geoNumber(semicolonParts[1]);
      if(latitude!==null&&longitude!==null)return {latitude,longitude};
    }
    const matches=text.match(/[+-]?\d+(?:\.\d+)?/g)||[];
    return {
      latitude:matches.length>=2?geoNumber(matches[0]):null,
      longitude:matches.length>=2?geoNumber(matches[1]):null
    };
  }
  function getGeoInfo(entries){
    let latitude=null,longitude=null,altitude=null;
    let latitudeKey="",longitudeKey="",altitudeKey="";
    const coordinateEntries=[];
    entries.forEach(function(pair){
      const key=pair[0],value=pair[1],type=geoFieldType(key);
      if(type==="latitude"){
        latitude=geoNumber(value);
        latitudeKey=key;
      }else if(type==="longitude"){
        longitude=geoNumber(value);
        longitudeKey=key;
      }else if(type==="altitude"){
        altitude=geoNumber(value);
        altitudeKey=key;
      }else if(type==="coordinates"){
        coordinateEntries.push([key,value]);
      }
    });
    if(latitude===null||longitude===null){
      for(const pair of coordinateEntries){
        const parsed=parseCoordinatePair(pair[1]);
        if(parsed.latitude!==null&&parsed.longitude!==null){
          latitude=parsed.latitude;
          longitude=parsed.longitude;
          break;
        }
      }
    }
    const validCoordinates=latitude!==null&&longitude!==null&&
      latitude>=-90&&latitude<=90&&longitude>=-180&&longitude<=180;
    return {
      latitude,longitude,altitude,latitudeKey,longitudeKey,altitudeKey,
      coordinateKeys:coordinateEntries.map(function(pair){return pair[0];}),
      validCoordinates
    };
  }
  function addInfoBox(body,name,info,isLocal){
    const entries=Object.entries(info&&typeof info==="object"?info:{}).filter(function(pair){
      return pair[1]!==null&&pair[1]!==undefined&&String(displayValue(pair[1])).trim()!=="";
    });
    if(!name&&!entries.length)return;
    const box=document.createElement("div");box.className="card-info";
    if(name){
      const heading=document.createElement("div");heading.className="info-name";
      heading.textContent=name;box.appendChild(heading);
    }
    const geo=getGeoInfo(entries);
    if(geo.validCoordinates){
      const row=document.createElement("div");row.className="card-info-row";
      const label=document.createElement("b");label.textContent="Координаты: ";
      const link=document.createElement("a");
      const lat=geo.latitude.toFixed(6),lon=geo.longitude.toFixed(6);
      link.href="https://www.openstreetmap.org/?mlat="+encodeURIComponent(lat)+
        "&mlon="+encodeURIComponent(lon)+"#map=16/"+lat+"/"+lon;
      link.target="_blank";link.rel="noopener noreferrer";
      link.textContent=lat+", "+lon+" · открыть карту";
      link.title="Показать координаты на карте OpenStreetMap";
      row.append(label,link);box.appendChild(row);
    }
    const ordered=isLocal?entries.sort(function(a,b){
      const ai=Object.keys(LOCAL_INFO_LABELS).indexOf(a[0]);
      const bi=Object.keys(LOCAL_INFO_LABELS).indexOf(b[0]);
      return (ai<0?999:ai)-(bi<0?999:bi);
    }):entries;
    ordered.forEach(function(pair){
      const key=pair[0],value=pair[1],type=geoFieldType(key);
      if(isLocal&&!Object.prototype.hasOwnProperty.call(LOCAL_INFO_LABELS,key))return;
      if(geo.validCoordinates&&(type==="latitude"||type==="longitude"||type==="coordinates"))return;
      const row=document.createElement("div");row.className="card-info-row";
      const label=document.createElement("b");
      const labelText=type==="altitude"?"Высота над уровнем моря":(isLocal?LOCAL_INFO_LABELS[key]:key);
      label.textContent=labelText+": ";
      const text=document.createElement("span");
      let rendered=displayValue(value);
      if(type==="altitude"){
        const altitude=geoNumber(value);
        if(altitude!==null)rendered=String(altitude)+" м";
      }else if(isLocal&&key==="file_size_bytes"){
        const size=Number(value);
        if(Number.isFinite(size))rendered=size>=1048576?(size/1048576).toFixed(2)+" МБ":(size/1024).toFixed(1)+" КБ";
      }else if(isLocal&&(key==="image_width"||key==="image_height")){
        rendered+=" px";
      }
      text.textContent=rendered;row.append(label,text);box.appendChild(row);
    });
    body.appendChild(box);
  }
  function renderResults(results){
    gallery.replaceChildren();
    resultCount.textContent=results.length?"Совпадений: "+results.length:"Совпадений не найдено";
    if(!results.length){
      const empty=document.createElement("div");empty.className="empty";
      empty.textContent="Нет результатов выше заданного порога сходства.";gallery.appendChild(empty);return;
    }
    results.forEach(function(item){
      const faceId=String(item.face_id||"");
      const imported=item.source_type==="import";
      const displayName=String(item.display_name||"");
      const card=document.createElement("article");card.className="card";
      const imageBox=document.createElement("div");imageBox.className="card-image";
      const rank=document.createElement("div");rank.className="rank";rank.textContent="#"+String(item.rank||"");
      const score=document.createElement("div");score.className="score";score.textContent=Number(item.similarity||0).toFixed(2)+"%";
      const image=document.createElement("img");image.loading="lazy";image.dataset.faceImage="true";
      image.alt=displayName||("Лицо ID "+faceId);
      const placeholder=document.createElement("div");placeholder.className="no-image";placeholder.textContent="Изображение недоступно";placeholder.hidden=true;
      imageBox.append(image,placeholder,rank,score);
      configureCardImage(image,faceId,useCropCheckbox.checked,false);
      const body=document.createElement("div");body.className="card-body";
      const title=document.createElement("div");title.className="card-title";
      title.textContent="#"+faceId.padStart(8,"0");
      const meta=document.createElement("div");meta.className="card-meta";
      meta.textContent="Результат №"+item.rank+" · ID лица "+faceId+(!imported&&item.filename?" · "+item.filename:"");
      body.append(title,meta);

      if(imported){
        addInfoBox(body,displayName,item.info||{},false);
      }else{
        addInfoBox(body,item.metadata_name||"",item.metadata||{},true);
      }

      const source=safeLink(item.source_url||item.profile_url);
      if(source){
        const siteRow=document.createElement("div");siteRow.className="source-site";
        let host=source;
        try{host=new URL(source).hostname;}catch(e){}
        const favicon=document.createElement("img");favicon.className="favicon";favicon.alt="";favicon.loading="lazy";
        try{favicon.src=new URL("/favicon.ico",source).href;}catch(e){favicon.style.display="none";}
        favicon.onerror=function(){favicon.style.display="none";};
        const link=document.createElement("a");link.href=source;link.target="_blank";link.rel="noopener noreferrer";
        link.textContent=host;link.title=source;
        siteRow.append(favicon,link);body.appendChild(siteRow);
      }

      const buttons=document.createElement("div");buttons.className="card-buttons";
      if(imported&&source){
        const sourceButton=document.createElement("a");sourceButton.className="card-button";
        sourceButton.href=source;sourceButton.target="_blank";sourceButton.rel="noopener noreferrer";sourceButton.textContent="Источник";
        buttons.appendChild(sourceButton);
      }
      const originalButton=document.createElement("a");originalButton.className="card-button";
      originalButton.href="/api/face/"+encodeURIComponent(faceId)+"/image?crop=0";
      originalButton.target="_blank";originalButton.rel="noopener noreferrer";originalButton.textContent="Открыть оригинал";
      buttons.appendChild(originalButton);body.appendChild(buttons);
      card.append(imageBox,body);gallery.appendChild(card);
    });
  }
  async function searchSimilar(){
    if(!queryId||selectedFace<0||busy)return;
    const minimum=Number(similarityInput.value);
    const topK=Number(topKInput.value);
    if(!Number.isFinite(minimum)||minimum<0||minimum>100){
      setStatus("Минимальное сходство должно быть от 0 до 100%.",true);return;
    }
    if(!Number.isFinite(topK)||topK<1||topK>500){
      setStatus("Количество результатов должно быть от 1 до 500.",true);return;
    }
    setBusy(true);setStatus("Поиск похожих лиц...");
    try{
      const data=await postJson("/api/search",{
        query_id:queryId,face_index:selectedFace,
        min_similarity:minimum,top_k:Math.trunc(topK)
      });
      renderResults(data.results||[]);
      setStatus("Поиск завершён. Найдено результатов: "+(data.results||[]).length+".");
    }catch(error){setStatus(error.message||String(error),true);}
    finally{setBusy(false);}
  }
  searchButton.addEventListener("click",searchSimilar);
})();
</script>
</body>
</html>
"""


class FaceSearchRequestHandler(BaseHTTPRequestHandler):
    server_version = "FaceSearchHTTP/1.0"

    def log_message(self, fmt, *args):
        print("[HTTP] " + (fmt % args))

    def _send(self, status: int, body: bytes, content_type: str, extra_headers=None):
        self.send_response(status)
        self.send_header("Content-Type", content_type)
        self.send_header("Content-Length", str(len(body)))
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("Cache-Control", "no-store")
        self.send_header("Referrer-Policy", "no-referrer")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self'; img-src 'self' data: blob: https:; "
            "style-src 'self' 'unsafe-inline'; script-src 'self' 'unsafe-inline'; "
            "connect-src 'self'; base-uri 'none'; frame-ancestors 'none'",
        )
        if extra_headers:
            for name, value in extra_headers.items():
                self.send_header(name, value)
        self.end_headers()
        if body:
            self.wfile.write(body)

    def _json(self, status: int, payload: dict):
        self._send(status, _json_bytes(payload), "application/json; charset=utf-8")

    def _read_json(self) -> dict:
        try:
            length = int(self.headers.get("Content-Length", "0"))
        except ValueError as exc:
            raise ValueError("Некорректный Content-Length.") from exc
        if length <= 0:
            raise ValueError("Пустое тело запроса.")
        if length > MAX_BODY_BYTES:
            raise OverflowError("Запрос слишком большой.")
        body = self.rfile.read(length)
        try:
            data = json.loads(body.decode("utf-8"))
        except (UnicodeDecodeError, json.JSONDecodeError) as exc:
            raise ValueError("Ожидается корректный JSON.") from exc
        if not isinstance(data, dict):
            raise ValueError("Ожидается JSON-объект.")
        return data

    def do_GET(self):
        path = urlparse(self.path).path
        if path == "/":
            page = PAGE_TEMPLATE.replace(
                "__DEFAULT_TOP_K__", str(DEFAULT_TOP_K)
            ).replace(
                "__DEFAULT_MIN_SIMILARITY__", str(DEFAULT_MIN_SIMILARITY)
            ).replace(
                "__CROP_CHECKED__", "checked" if DEFAULT_USE_LOCAL_CROPS else ""
            ).replace(
                "__CREATE_CROPS_ON_HTML__", "true" if CREATE_CROPS_ON_HTML else "false"
            ).replace(
                "__DEFAULT_THEME__", DEFAULT_THEME
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path.startswith("/api/face/") and path.endswith("/image"):
            query = parse_qs(urlparse(self.path).query)
            use_crop = query.get("crop", ["1"])[0] != "0"
            self._serve_face_image(path, use_crop=use_crop)
            return
        if path == "/api/status":
            self._json(200, {
                "ok": True, "profile": PROFILE_NAME, "backend": BACKEND,
                "index_exists": Path(face_search.INDEX_FILE).is_file(),
                "create_crops_on_html": CREATE_CROPS_ON_HTML,
                "crop_repair_active": bool(_CROP_REPAIR_ACTIVE_IDS),
            })
            return
        self._json(404, {"error": "Страница не найдена."})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/detect", "/api/search", "/api/load-image"):
            self._json(404, {"error": "API-метод не найден."})
            return
        if not API_SEMAPHORE.acquire(timeout=15):
            self._json(503, {"error": "Сервер перегружен. Повторите запрос через несколько секунд."})
            return
        try:
            try:
                data = self._read_json()
            except OverflowError as exc:
                self._json(413, {"error": str(exc)})
                return
            except ValueError as exc:
                self._json(400, {"error": str(exc)})
                return
            if path == "/api/load-image":
                try:
                    image_data_url = _fetch_remote_image_data_url(str(data.get("url") or ""))
                    self._json(200, {"image": image_data_url})
                except ValueError as exc:
                    self._json(400, {"error": str(exc)[:1000]})
                return
            if path == "/api/detect":
                self._detect(data)
            else:
                self._search(data)
        except Exception as exc:
            message = str(exc).strip() or exc.__class__.__name__
            print(f"[HTTP][ERROR] {exc}")
            self._json(500, {"error": message[:1000]})
        finally:
            API_SEMAPHORE.release()

    def _detect(self, data: dict):
        image = _decode_image(data.get("image", ""))
        found = face_search._detect_faces_with_padding(
            FACE_APP, image, padding_ratio=0.40
        )
        faces = []
        embeddings = []
        for face in found:
            if float(face.det_score) < face_search.DET_THRESHOLD:
                continue
            embedding = face_search.np.asarray(
                face.normed_embedding, dtype=face_search.np.float32
            ).copy()
            if float(face_search.np.linalg.norm(embedding)) <= 0:
                continue
            bbox = [float(value) for value in face.bbox.tolist()]
            embeddings.append(embedding)
            faces.append({
                "index": len(faces),
                "bbox": bbox,
                "confidence": float(face.det_score),
            })

        token = secrets.token_urlsafe(24)
        with QUERY_SESSIONS_LOCK:
            QUERY_SESSIONS[token] = {
                "created": time.monotonic(),
                "embeddings": embeddings,
            }
        _prune_sessions()
        self._json(200, {"query_id": token, "faces": faces})

    def _search(self, data: dict):
        token = str(data.get("query_id") or "")
        try:
            face_index = int(data.get("face_index", -1))
            top_k = max(1, min(500, int(data.get("top_k", DEFAULT_TOP_K))))
            minimum_percent = float(data.get("min_similarity", DEFAULT_MIN_SIMILARITY))
        except (TypeError, ValueError) as exc:
            raise ValueError("Параметры поиска имеют неверный формат.") from exc
        if not 0.0 <= minimum_percent <= 100.0:
            raise ValueError("Минимальное сходство должно быть от 0 до 100%.")
        if not token or len(token) > 128:
            raise ValueError("Сессия запроса не найдена. Загрузите изображение ещё раз.")

        with QUERY_SESSIONS_LOCK:
            query = QUERY_SESSIONS.get(token)
            if query is None or time.monotonic() - query["created"] > QUERY_TTL_SECONDS:
                QUERY_SESSIONS.pop(token, None)
                query = None
            if query is not None and 0 <= face_index < len(query["embeddings"]):
                embedding = query["embeddings"][face_index].copy()
            else:
                embedding = None
        if embedding is None:
            raise ValueError("Сессия истекла или лицо не выбрано. Определите лица повторно.")

        conn = _open_connection()
        try:
            results = face_search.search_embedding(
                conn,
                embedding,
                top_k=top_k,
                min_similarity=minimum_percent / 100.0,
                backend=BACKEND,
            )
            # Cache all displayed image rows once per search. The CROP retry script
            # can then poll for several minutes without opening a DB connection for
            # every failed image request (important for PostgreSQL connection limits).
            result_ids = []
            for result in results:
                try:
                    result_id = int(result.get("face_id") or 0)
                except (TypeError, ValueError):
                    continue
                if result_id > 0:
                    result_ids.append(result_id)
            result_ids = list(dict.fromkeys(result_ids))
            for offset in range(0, len(result_ids), 500):
                chunk = result_ids[offset:offset + 500]
                if not chunk:
                    continue
                placeholders = ",".join("?" for _ in chunk)
                rows = conn.execute(
                    "SELECT f.id, i.id, i.path, i.filename, i.source_url, i.url_image "
                    "FROM faces f JOIN images i ON i.id = f.image_id "
                    f"WHERE f.id IN ({placeholders})",
                    tuple(chunk),
                ).fetchall()
                _remember_face_image_rows(rows)
        finally:
            conn.close()

        cards = []
        for result in results:
            face_id = int(result.get("face_id") or 0)
            source_type = str(result.get("source_type") or "local")
            source_url = str(result.get("source_url") or result.get("profile_url") or "")
            url_image = str(result.get("url_image") or "").strip()
            raw_meta = {}
            metadata_doc = {}
            metadata_info = {}
            metadata_name = ""
            try:
                value = json.loads(str(result.get("raw_json") or ""))
                if isinstance(value, dict):
                    raw_meta = value
            except (TypeError, ValueError):
                pass
            if not source_url:
                source_url = str(raw_meta.get("url_source") or "")
            if not url_image:
                url_image = str(raw_meta.get("url_image") or "").strip()

            display_name = str(raw_meta.get("name") or result.get("first_name") or "").strip()
            profile_info = raw_meta.get("info") if isinstance(raw_meta.get("info"), dict) else {}
            try:
                value = json.loads(str(result.get("metadata_json") or ""))
                if isinstance(value, dict):
                    metadata_doc = value
            except (TypeError, ValueError):
                pass
            if (
                set(metadata_doc.keys()) == {"url_source", "name", "url_image", "info"}
                and isinstance(metadata_doc.get("info"), dict)
            ):
                metadata_info = metadata_doc["info"]
                metadata_name = str(metadata_doc.get("name") or "")
            else:
                metadata_info = metadata_doc
                metadata_name = str(metadata_doc.get("filename") or "")
            if source_type == "import":
                if not display_name:
                    display_name = "Без имени"
            else:
                display_name = ""

            cards.append({
                "rank": int(result.get("rank") or len(cards) + 1),
                "face_id": face_id,
                "filename": str(result.get("filename") or ""),
                "similarity": round(float(result.get("score") or 0.0) * 100.0, 2),
                "source_url": source_url,
                "profile_url": str(result.get("profile_url") or ""),
                "url_image": url_image,
                "source_type": source_type,
                "display_name": display_name,
                "info": profile_info,
                "metadata_name": metadata_name,
                "metadata": metadata_info,
            })
        if CREATE_CROPS_ON_HTML and cards:
            _start_crop_repair(card["face_id"] for card in cards)
        self._json(200, {"results": cards, "count": len(cards)})

    def _serve_face_image(self, route: str, *, use_crop: bool = True):
        parts = route.strip("/").split("/")
        try:
            face_id = int(parts[2])
        except (IndexError, ValueError):
            self._json(400, {"error": "Некорректный ID лица."})
            return
        if face_id <= 0:
            self._json(400, {"error": "Некорректный ID лица."})
            return

        row = _get_face_image_row(face_id)
        if row is None:
            conn = _open_connection()
            try:
                db_row = conn.execute(
                    """
                    SELECT f.id, i.id, i.path, i.filename, i.source_url, i.url_image
                    FROM faces f
                    JOIN images i ON i.id = f.image_id
                    WHERE f.id = ?
                    """,
                    (face_id,),
                ).fetchone()
                if db_row:
                    _remember_face_image_rows([db_row])
                    row = _get_face_image_row(face_id)
            finally:
                conn.close()

        if not row:
            self._json(404, {"error": "Изображение не найдено."})
            return
        image_id, stored_path, filename, source_url, url_image = row
        crop_path = face_search._resolve_db_crop_path(
            stored_path, image_id=int(image_id)
        )
        if use_crop and crop_path.is_file():
            try:
                body = crop_path.read_bytes()
                content_type = mimetypes.guess_type(str(crop_path))[0] or "image/jpeg"
                self._send(200, body, content_type)
                return
            except OSError:
                pass
        elif use_crop and CREATE_CROPS_ON_HTML:
            # Браузерный скрипт будет повторять запрос, пока CROP формируется.
            self._json(404, {"error": "CROP ещё формируется."})
            return

        # Если CROP не создавался/утерян, показываем сохранённый оригинал.
        local_source = face_search._source_url_to_local_path(source_url)
        if local_source is not None and local_source.is_file():
            try:
                self._send(
                    200, local_source.read_bytes(),
                    mimetypes.guess_type(str(local_source))[0] or "application/octet-stream",
                )
                return
            except OSError:
                pass

        remote_url = str(url_image or "").strip()
        if remote_url.lower().startswith(("http://", "https://")):
            self._send(302, b"", "text/plain; charset=utf-8", {"Location": remote_url})
            return
        self._json(404, {"error": "CROP отсутствует, исходное изображение недоступно."})


class FaceSearchHTTPServer(ThreadingHTTPServer):
    daemon_threads = True
    request_queue_size = 64
    allow_reuse_address = True


def main() -> None:
    parser = argparse.ArgumentParser(description="Многопоточный HTTP-сервер поиска лиц FaceSearch")
    parser.add_argument("--host", default="127.0.0.1", help="Адрес привязки; по умолчанию только локальный компьютер")
    parser.add_argument("--port", type=int, default=8765, help="HTTP-порт (по умолчанию 8765)")
    parser.add_argument("--no-browser", action="store_true", help="Не открывать браузер автоматически")
    args = parser.parse_args()

    if not 1 <= args.port <= 65535:
        parser.error("Порт должен быть в диапазоне 1–65535.")

    _initialize_runtime()
    server = FaceSearchHTTPServer((args.host, args.port), FaceSearchRequestHandler)
    display_host = "127.0.0.1" if args.host in ("0.0.0.0", "::") else args.host
    url = f"http://{display_host}:{args.port}/"

    print()
    print("FaceSearch HTTP-сервер запущен.")
    print(f"Локальный адрес: {url}")
    print(f"Профиль БД: {PROFILE_NAME}")
    if args.host in ("0.0.0.0", "::"):
        print("ВНИМАНИЕ: сервер доступен по сети и не имеет встроенной авторизации.")
        print("Не публикуйте порт в Интернет; ограничьте доступ правилами firewall.")
    print("Для остановки нажмите Ctrl+C.")
    if not args.no_browser:
        try:
            webbrowser.open(url)
        except Exception:
            pass

    try:
        server.serve_forever(poll_interval=0.5)
    except KeyboardInterrupt:
        print("\nОстановка HTTP-сервера...")
    finally:
        server.server_close()
        with QUERY_SESSIONS_LOCK:
            QUERY_SESSIONS.clear()


if __name__ == "__main__":
    main()
