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
import os
import secrets
import socket
import threading
import time
import urllib.error
import urllib.request
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from datetime import datetime
from pathlib import Path
from contextlib import contextmanager
from urllib.parse import parse_qs, urlparse

import face_search_gui as face_search
from facesearch import group_analysis
from facesearch.settings import _load_gui_settings
from facesearch.database_profiles import _load_db_profiles, _default_profile
from facesearch.card_text_filter import (
    MAX_CARD_TEXT_FILTER_LENGTH,
    candidate_count_for_text_filter,
    filter_results_by_card_text,
)

APP_DIR = Path(__file__).resolve().parent

# REPAIR_HTTP_RETRIES — число повторов после первой попытки в repair_missing_crops.
# Значение 1 ограничивает суммарное число сетевых попыток двумя.
if hasattr(face_search, "REPAIR_HTTP_RETRIES"):
    face_search.REPAIR_HTTP_RETRIES = 1

MAX_BODY_BYTES = 24 * 1024 * 1024
MAX_IMAGE_BYTES = 16 * 1024 * 1024
MAX_IMAGE_PIXELS = 40_000_000
QUERY_TTL_SECONDS = 30 * 60
MAX_QUERY_SESSIONS = 100
GROUP_ANALYSIS_TTL_SECONDS = 30 * 60
MAX_GROUP_ANALYSIS_JOBS = 100
MAX_ACTIVE_GROUP_ANALYSES = 2

QUERY_SESSIONS: dict[str, dict] = {}
QUERY_SESSIONS_LOCK = threading.RLock()
GROUP_ANALYSIS_JOBS: dict[str, dict] = {}
GROUP_ANALYSIS_JOBS_LOCK = threading.RLock()
API_SEMAPHORE = threading.BoundedSemaphore(4)

PROFILE: dict = {}
PROFILE_NAME = ""
BACKEND = face_search.BACKEND_GPU
FACE_APP = None
DEFAULT_TOP_K = 24
DEFAULT_MIN_SIMILARITY = 43.0
DEFAULT_USE_LOCAL_CROPS = True
DEFAULT_THEME = "dark"
DEFAULT_GROUP_ANALYSIS_OPTIONS = dict(group_analysis.DEFAULTS)
CREATE_CROPS_ON_HTML = False
CROP_REPAIR_WORKERS = 4
CROP_REPAIR_AUTO_WORKERS = False
_CROP_REPAIR_LOCK = threading.RLock()
_CROP_REPAIR_ACTIVE_IDS: set[int] = set()
_CROP_REPAIR_LAST_START: dict[int, float] = {}
_CROP_REPAIR_RETRY_INTERVAL = 10.0
_CROP_REPAIR_LAST_START_MAX = 5000
_FACE_IMAGE_CACHE_LOCK = threading.RLock()
_FACE_IMAGE_CACHE: dict[int, tuple] = {}
_FACE_IMAGE_CACHE_MAX = 5000
_FACE_GENDER_CACHE_LOCK = threading.RLock()
_FACE_GENDER_CACHE: dict[int, dict] = {}
_FACE_GENDER_CACHE_MAX = 20000
_REMOTE_IMAGE_CHECK_LOCK = threading.RLock()
_REMOTE_IMAGE_CHECK_CACHE: dict[str, tuple[float, bool]] = {}
_REMOTE_IMAGE_CHECK_CACHE_TTL = 20.0
_REMOTE_IMAGE_CHECK_CACHE_MAX = 2000


# Автоматическое обнаружение обновления FAISS.
# Перезагрузка начинается только после нескольких одинаковых проверок stat(),
# чтобы не читать индекс во время его записи.
_INDEX_CHECK_INTERVAL = 5.0
_INDEX_STABLE_POLLS = 3
_INDEX_RETRY_INTERVAL = 60.0
_INDEX_WATCH_STOP = threading.Event()
_INDEX_STATE_LOCK = threading.RLock()
_INDEX_ACTIVE_SIGNATURE = None
_INDEX_RELOAD_STATE = "starting"  # starting | ready | pending | loading | error | missing | fallback
_INDEX_RELOAD_MESSAGE = "Инициализация индекса..."
_INDEX_GENERATION = 0
_INDEX_LAST_UPDATED_AT = None
_INDEX_LAST_ATTEMPT_SIGNATURE = object()
_INDEX_LAST_ATTEMPT_AT = 0.0


class _IndexReadWriteLock:
    """Разрешает параллельные поиски, но исключает поиск во время смены индекса."""

    def __init__(self):
        self._condition = threading.Condition(threading.RLock())
        self._readers = 0
        self._waiting_writers = 0
        self._writer_active = False

    @contextmanager
    def read_lock(self):
        with self._condition:
            while self._writer_active or self._waiting_writers:
                self._condition.wait()
            self._readers += 1
        try:
            yield
        finally:
            with self._condition:
                self._readers -= 1
                if self._readers == 0:
                    self._condition.notify_all()

    @contextmanager
    def write_lock(self, timeout: float | None = None):
        deadline = time.monotonic() + timeout if timeout is not None else None
        with self._condition:
            self._waiting_writers += 1
            try:
                while self._writer_active or self._readers:
                    remaining = None if deadline is None else deadline - time.monotonic()
                    if remaining is not None and remaining <= 0:
                        raise TimeoutError(
                            f"не завершились активные поисковые запросы за {timeout:.0f} секунд"
                        )
                    self._condition.wait(remaining)
                self._writer_active = True
            finally:
                self._waiting_writers -= 1
                self._condition.notify_all()
        try:
            yield
        finally:
            with self._condition:
                self._writer_active = False
                self._condition.notify_all()

    def get_status(self) -> dict:
        with self._condition:
            return {
                "active_searches": int(self._readers),
                "waiting_writers": int(self._waiting_writers),
                "writer_active": bool(self._writer_active),
            }


_INDEX_USE_LOCK = _IndexReadWriteLock()
_GPU_STATE_FIELDS = (
    "_GPU_EMBEDDINGS", "_GPU_FACE_IDS", "_GPU_INDEX_DIM", "_GPU_INDEX_COUNT",
    "_GPU_INDEX_DTYPE", "_GPU_INDEX_ERROR", "_GPU_INDEX_WARNING", "_GPU_INDEX_MODE",
    "_GPU_STREAM_CPU_INDEX", "_GPU_STREAM_FACE_IDS", "_GPU_STREAM_DIM", "_GPU_STREAM_COUNT",
)
_CPU_CACHE_FIELDS = (
    "_CPU_FAISS_CACHE_INDEX", "_CPU_FAISS_CACHE_PATH",
    "_CPU_FAISS_CACHE_MTIME_NS", "_CPU_FAISS_CACHE_SIZE",
)


def _index_signature(index_path_value=None):
    """Снимок файла индекса: путь, mtime, размер, ctime и inode."""
    try:
        path = Path(index_path_value or face_search.INDEX_FILE).expanduser().resolve()
        stat = path.stat()
        if not path.is_file():
            return None
        return (
            str(path),
            int(stat.st_mtime_ns),
            int(stat.st_size),
            int(stat.st_ctime_ns),
            int(getattr(stat, "st_ino", 0)),
        )
    except (OSError, TypeError, ValueError):
        return None


def _set_index_state(state, message):
    global _INDEX_RELOAD_STATE, _INDEX_RELOAD_MESSAGE
    with _INDEX_STATE_LOCK:
        _INDEX_RELOAD_STATE = str(state)
        _INDEX_RELOAD_MESSAGE = str(message)


def _snapshot_gpu_index():
    lock = getattr(face_search, "_GPU_INDEX_LOCK", None)
    if lock is None:
        return {}
    with lock:
        return {name: getattr(face_search, name, None) for name in _GPU_STATE_FIELDS}


def _restore_gpu_index(snapshot):
    if not snapshot:
        return
    lock = getattr(face_search, "_GPU_INDEX_LOCK", None)
    if lock is None:
        return
    with lock:
        for name, value in snapshot.items():
            setattr(face_search, name, value)


def _snapshot_cpu_index_cache():
    lock = getattr(face_search, "_CPU_FAISS_CACHE_LOCK", None)
    if lock is None:
        return {}
    with lock:
        return {name: getattr(face_search, name, None) for name in _CPU_CACHE_FIELDS}


def _restore_cpu_index_cache(snapshot, signature_hint=None):
    """Восстанавливает старый объект после неудачной загрузки новой версии."""
    if not snapshot:
        return
    lock = getattr(face_search, "_CPU_FAISS_CACHE_LOCK", None)
    if lock is None:
        return
    with lock:
        for name, value in snapshot.items():
            setattr(face_search, name, value)
        cached_index = snapshot.get("_CPU_FAISS_CACHE_INDEX")
        cached_path = snapshot.get("_CPU_FAISS_CACHE_PATH")
        if cached_index is not None and signature_hint is not None:
            try:
                current_path = str(Path(face_search.INDEX_FILE).expanduser().resolve())
                if cached_path == current_path and signature_hint[0] == current_path:
                    # Не даём CPU fallback автоматически заменить рабочий старый индекс
                    # потенциально неполным файлом. Следующая попытка обновления
                    # предварительно очистит кэш и перечитает файл заново.
                    face_search._CPU_FAISS_CACHE_MTIME_NS = int(signature_hint[1])
                    face_search._CPU_FAISS_CACHE_SIZE = int(signature_hint[2])
            except (OSError, TypeError, ValueError, IndexError):
                pass


def _validate_cpu_faiss_index(index):
    """Проверяет структуру загруженного индекса до переключения на него."""
    base_index = index.index if hasattr(index, "index") else index
    count = int(getattr(base_index, "ntotal", 0) or 0)
    dimension = int(getattr(base_index, "d", 0) or 0)
    if count <= 0 or dimension <= 0:
        raise RuntimeError("Индекс пуст или имеет некорректную размерность.")
    if not hasattr(index, "id_map"):
        raise RuntimeError("Индекс не содержит карту face.id; требуется пересоздание FAISS.")
    face_ids = face_search.np.asarray(
        face_search.faiss.vector_to_array(index.id_map), dtype=face_search.np.int64
    )
    if int(face_ids.shape[0]) != count:
        raise RuntimeError(
            "Число face.id не совпадает с числом embeddings "
            + "(" + str(face_ids.shape[0]) + " != " + str(count) + ")."
        )
    return count, dimension


def _reload_index(target_signature):
    """Перечитывает FAISS, не прерывая HTTP-сервер и сохраняя старый индекс при ошибке."""
    global _INDEX_ACTIVE_SIGNATURE, _INDEX_GENERATION, _INDEX_LAST_UPDATED_AT

    wait_started = time.monotonic()
    _set_index_state(
        "waiting",
        "Новая версия FAISS обнаружена. Ожидание завершения текущих поисковых запросов...",
    )
    print(
        "[HTTP][FAISS] Обнаружена новая версия индекса. Ожидаю завершения активных поисков.",
        flush=True,
    )

    try:
        with _INDEX_USE_LOCK.write_lock(timeout=180.0):
            waited = time.monotonic() - wait_started
            # Во время ожидания завершения поисков файл мог обновиться ещё раз.
            if _index_signature() != target_signature:
                _set_index_state(
                    "pending",
                    "Файл FAISS снова меняется; ожидается стабильная версия.",
                )
                print(
                    "[HTTP][FAISS] Файл снова изменился во время ожидания. "
                    "Перезапуск проверки через следующий цикл.",
                    flush=True,
                )
                return False

            _set_index_state(
                "loading",
                "Активные поиски завершены. Загружается новая версия FAISS...",
            )
            print(
                "[HTTP][FAISS] Начинаю загрузку новой версии "
                f"(ожидание блокировки: {waited:.1f} с).",
                flush=True,
            )

            old_gpu = _snapshot_gpu_index()
            old_cpu = _snapshot_cpu_index_cache()
            index_path = Path(face_search.INDEX_FILE).expanduser().resolve()
            try:
                # Принудительно перечитываем именно файл на диске.
                # Старые объекты сохраняются в old_gpu/old_cpu для отката.
                face_search._clear_cpu_faiss_cache()

                if BACKEND == face_search.BACKEND_GPU:
                    loaded = face_search._load_gpu_search_index_from_faiss(
                        index_path,
                        progress_callback=lambda message: print(
                            "[HTTP][FAISS] " + str(message), flush=True
                        ),
                    )
                    if not loaded:
                        gpu_status = face_search.get_gpu_search_status()
                        reason = gpu_status.get("error") or "GPU-индекс не активирован"
                        raise RuntimeError(str(reason))
                else:
                    cpu_index = face_search._get_cpu_faiss_index(index_path)
                    count, dimension = _validate_cpu_faiss_index(cpu_index)
                    print(
                        "[HTTP][FAISS] CPU-индекс проверен: "
                        + format(count, ",").replace(",", " ")
                        + " лиц, dim=" + str(dimension),
                        flush=True,
                    )

                if _index_signature() != target_signature:
                    raise RuntimeError("Файл FAISS изменился непосредственно во время загрузки.")

                with _INDEX_STATE_LOCK:
                    _INDEX_ACTIVE_SIGNATURE = target_signature
                    _INDEX_GENERATION += 1
                    _INDEX_LAST_UPDATED_AT = datetime.now().isoformat(timespec="seconds")
                    _INDEX_RELOAD_STATE = "ready"
                    _INDEX_RELOAD_MESSAGE = (
                        "FAISS-индекс обновлён. Новые поисковые запросы используют актуальную версию."
                    )
                print(
                    "[HTTP][FAISS] Индекс успешно обновлён без перезапуска HTTP-сервера: "
                    + index_path.name,
                    flush=True,
                )
                return True

            except Exception as exc:
                # GPU-загрузчик может очистить старый глобальный индекс при OOM.
                # Восстанавливаем ссылки до снятия эксклюзивной блокировки.
                _restore_gpu_index(old_gpu)
                _restore_cpu_index_cache(old_cpu, signature_hint=target_signature)
                try:
                    import gc
                    gc.collect()
                except Exception:
                    pass
                message = (
                    "Не удалось загрузить новую версию FAISS: " + str(exc)
                    + ". Предыдущая версия сохранена, если была успешно загружена."
                )
                print("[HTTP][FAISS][ERROR] " + message, flush=True)
                _set_index_state("error", message)
                return False

    except TimeoutError as exc:
        message = (
            "Перезагрузка FAISS отменена: " + str(exc)
            + ". Старый индекс не изменён; повторная попытка будет выполнена автоматически."
        )
        print("[HTTP][FAISS][WARNING] " + message, flush=True)
        _set_index_state("error", message)
        return False

def _watch_index_loop():
    """Периодически обнаруживает стабильное обновление файла FAISS и применяет его."""
    global _INDEX_LAST_ATTEMPT_SIGNATURE, _INDEX_LAST_ATTEMPT_AT

    print(
        f"[HTTP][FAISS] Фоновая проверка индекса запущена; интервал {_INDEX_CHECK_INTERVAL:.0f} с.",
        flush=True,
    )
    observed_signature = object()
    stable_polls = 0

    while not _INDEX_WATCH_STOP.wait(_INDEX_CHECK_INTERVAL):
        signature = _index_signature()
        now = time.monotonic()

        with _INDEX_STATE_LOCK:
            active_signature = _INDEX_ACTIVE_SIGNATURE
            current_state = _INDEX_RELOAD_STATE

        if signature == active_signature:
            observed_signature = object()
            stable_polls = 0
            # Не затираем fallback/error/missing: это полезная диагностическая информация.
            if current_state not in ("fallback", "error", "missing", "loading"):
                _set_index_state("ready", "FAISS-индекс актуален.")
            continue

        # Не маскируем уведомление об ошибке ожиданием стабильности повторно.
        # Та же версия файла будет повторно проверена через минуту.
        if (
            signature == _INDEX_LAST_ATTEMPT_SIGNATURE
            and current_state == "error"
            and now - _INDEX_LAST_ATTEMPT_AT < _INDEX_RETRY_INTERVAL
        ):
            continue

        if signature != observed_signature:
            observed_signature = signature
            stable_polls = 1
        else:
            stable_polls += 1

        if signature is None:
            _set_index_state(
                "missing",
                "Файл FAISS временно отсутствует. Если старая версия загружена, она остаётся в памяти.",
            )
        else:
            _set_index_state(
                "pending",
                "Обнаружены изменения FAISS. Ожидание завершения записи файла перед переключением индекса...",
            )

        if stable_polls < _INDEX_STABLE_POLLS:
            continue

        if _INDEX_LAST_ATTEMPT_SIGNATURE == signature:
            # Для отсутствующего файла повторная попытка не нужна; ошибку загрузки
            # повторяем не чаще раза в минуту, не засоряя консоль одинаковыми сообщениями.
            if signature is None or now - _INDEX_LAST_ATTEMPT_AT < _INDEX_RETRY_INTERVAL:
                continue

        _INDEX_LAST_ATTEMPT_SIGNATURE = signature
        _INDEX_LAST_ATTEMPT_AT = now

        if signature is None:
            _set_index_state(
                "missing",
                "Файл FAISS не найден. Сервер не остановлен; доступность поиска зависит от загруженной версии индекса.",
            )
            print("[HTTP][FAISS][WARNING] Файл индекса отсутствует; сервер продолжает работать.")
            continue

        file_path = Path(face_search.INDEX_FILE)
        try:
            stat = file_path.stat()
            size_mb = stat.st_size / (1024 ** 2)
            created_timestamp = _index_created_timestamp(stat)
            created_at = (
                datetime.fromtimestamp(created_timestamp).astimezone().strftime("%d.%m.%Y %H:%M:%S")
                if created_timestamp is not None else "н/д"
            )
            print(
                f"[HTTP][FAISS] Новая версия подтверждена: {file_path.name}, "
                f"{size_mb:.1f} MiB, создан {created_at}.",
                flush=True,
            )
        except OSError:
            pass
        _set_index_state(
            "waiting",
            "Новая версия FAISS обнаружена. Ожидание завершения текущих поисковых запросов...",
        )
        _reload_index(signature)


def _index_watcher_runner():
    """Перезапускает проверяющий поток, если непредвиденная ошибка завершила цикл."""
    while not _INDEX_WATCH_STOP.is_set():
        try:
            _watch_index_loop()
            return
        except Exception as exc:
            message = "Фоновая проверка FAISS аварийно завершилась: " + str(exc)
            print("[HTTP][FAISS][ERROR] " + message, flush=True)
            _set_index_state("error", message)
            if _INDEX_WATCH_STOP.wait(_INDEX_CHECK_INTERVAL):
                return


def _index_created_timestamp(stat):
    """Возвращает время создания файла, если ОС его предоставляет."""
    created_timestamp = getattr(stat, "st_birthtime", None)
    # На Windows st_ctime означает время создания; на POSIX это время изменения метаданных.
    if created_timestamp is None and os.name == "nt":
        created_timestamp = getattr(stat, "st_ctime", None)
    return created_timestamp


def _index_status_payload():
    with _INDEX_STATE_LOCK:
        state = _INDEX_RELOAD_STATE
        message = _INDEX_RELOAD_MESSAGE
        generation = _INDEX_GENERATION
        updated_at = _INDEX_LAST_UPDATED_AT
        active_signature = _INDEX_ACTIVE_SIGNATURE

    path = Path(face_search.INDEX_FILE).expanduser().resolve()
    try:
        stat = path.stat()
        index_exists = path.is_file()
        size_bytes = int(stat.st_size)
        created_timestamp = _index_created_timestamp(stat)
        created_at = (
            datetime.fromtimestamp(created_timestamp).astimezone().isoformat(timespec="seconds")
            if created_timestamp is not None else None
        )
    except (OSError, ValueError):
        index_exists = False
        size_bytes = 0
        created_at = None

    status = _INDEX_USE_LOCK.get_status()
    current_signature = _index_signature()
    # Защита от застрявшего статуса после завершённой замены индекса.
    # Во время фактической загрузки writer_active=True, поэтому статус не сбрасывается.
    if (
        state in ("pending", "waiting", "loading")
        and not status["writer_active"]
        and active_signature is not None
        and current_signature == active_signature
    ):
        state = "ready"
        message = "FAISS-индекс актуален."
        _set_index_state(state, message)

    return {
        "reload_state": state,
        "reload_message": message,
        "generation": generation,
        "updated_at": updated_at,
        "index_exists": index_exists,
        "index_name": path.name,
        "index_size_bytes": size_bytes,
        "index_created_at": created_at,
        "index_changed_on_disk": current_signature != active_signature,
        **status,
    }


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
    global DEFAULT_GROUP_ANALYSIS_OPTIONS
    global CROP_REPAIR_WORKERS, CROP_REPAIR_AUTO_WORKERS
    global _INDEX_ACTIVE_SIGNATURE, _INDEX_RELOAD_STATE, _INDEX_RELOAD_MESSAGE

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
    DEFAULT_GROUP_ANALYSIS_OPTIONS = group_analysis.normalize_options({
        "min_similarity": gui_settings.get("group_min_similarity", 43.0),
        "candidates_per_face": gui_settings.get("group_candidates_per_face", 5),
        "friend_weight": gui_settings.get("group_friend_weight", 8.0),
        "common_friend_weight": gui_settings.get("group_common_friend_weight", 2.0),
        "city_weight": gui_settings.get("group_city_weight", 1.5),
        "check_vk_friends": gui_settings.get("group_check_vk_friends", True),
        "check_gender": gui_settings.get("group_check_gender", True),
        "gender_weight": gui_settings.get("group_gender_weight", 4.0),
        "profile_gender_weight": gui_settings.get("group_profile_gender_weight", 16.0),
    })
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
    initial_signature = _index_signature(index_path)
    with _INDEX_STATE_LOCK:
        _INDEX_ACTIVE_SIGNATURE = initial_signature
        if initial_signature is None:
            _INDEX_RELOAD_STATE = "missing"
            _INDEX_RELOAD_MESSAGE = "FAISS-индекс не найден. Поиск станет доступен после появления файла индекса."
        else:
            _INDEX_RELOAD_STATE = "ready"
            _INDEX_RELOAD_MESSAGE = "FAISS-индекс актуален."
    if initial_signature is None:
        print("[HTTP] ПРЕДУПРЕЖДЕНИЕ: FAISS-индекс не найден; фоновая проверка продолжится.")
    elif BACKEND == face_search.BACKEND_GPU:
        # Первичная загрузка выполняется до старта HTTP; последующие обновления
        # контролирует фоновый поток, не останавливая сервер.
        try:
            loaded = face_search._load_gpu_search_index_from_faiss(
                index_path,
                progress_callback=lambda message: print("[HTTP][FAISS] " + str(message)),
            )
            if not loaded:
                message = "GPU-индекс не активирован; поиск будет использовать CPU FAISS."
                print("[HTTP] " + message)
                _set_index_state("fallback", message)
        except Exception as exc:
            message = "Не удалось загрузить FAISS в GPU; будет использован CPU fallback: " + str(exc)
            print("[HTTP] " + message)
            _set_index_state("fallback", message)


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


def _prune_group_analysis_jobs() -> None:
    now = time.monotonic()
    with GROUP_ANALYSIS_JOBS_LOCK:
        expired = [
            job_id for job_id, job in GROUP_ANALYSIS_JOBS.items()
            if now - job.get("created", now) > GROUP_ANALYSIS_TTL_SECONDS
            and job.get("state") not in ("queued", "running")
        ]
        for job_id in expired:
            GROUP_ANALYSIS_JOBS.pop(job_id, None)
        while len(GROUP_ANALYSIS_JOBS) > MAX_GROUP_ANALYSIS_JOBS:
            finished = [
                job_id for job_id, job in GROUP_ANALYSIS_JOBS.items()
                if job.get("state") not in ("queued", "running")
            ]
            if not finished:
                break
            oldest = min(finished, key=lambda job_id: GROUP_ANALYSIS_JOBS[job_id].get("created", 0))
            GROUP_ANALYSIS_JOBS.pop(oldest, None)


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



def _gender_model():
    """Возвращает уже загруженную модель genderage из набора InsightFace."""
    app = FACE_APP
    models = getattr(app, "models", {}) if app is not None else {}
    if not isinstance(models, dict):
        return None
    model = models.get("genderage")
    if model is not None:
        return model
    return next(
        (value for value in models.values()
         if str(getattr(value, "taskname", "")).casefold() == "genderage"),
        None,
    )


def _predict_face_gender(image, face) -> dict:
    """Возвращает осторожную оценку видимого пола по genderage-модели InsightFace."""
    unknown = {"gender": "unknown", "confidence": 0.0}
    model = _gender_model()
    if model is None:
        return unknown

    # Повторно запускаем только маленькую genderage ONNX-модель, чтобы получить
    # оценку уверенности, а не использовать только жёсткий argmax из face.gender.
    try:
        import numpy as np
        from insightface.utils import face_align

        bbox = np.asarray(face.bbox, dtype=np.float32)
        width = float(bbox[2] - bbox[0])
        height = float(bbox[3] - bbox[1])
        if width < 2.0 or height < 2.0:
            return unknown
        center = ((float(bbox[2]) + float(bbox[0])) / 2.0,
                  (float(bbox[3]) + float(bbox[1])) / 2.0)
        input_side = int(model.input_size[0])
        scale = input_side / (max(width, height) * 1.5)
        aligned, _transform = face_align.transform(
            image, center, input_side, scale, 0
        )
        input_size = tuple(aligned.shape[0:2][::-1])
        mean = float(model.input_mean)
        std = float(model.input_std)
        if std == 0:
            return unknown
        blob = face_search.cv2.dnn.blobFromImage(
            aligned, 1.0 / std, input_size,
            (mean, mean, mean), swapRB=True,
        )
        raw = model.session.run(
            model.output_names,
            {model.input_name: blob},
        )[0][0]
        scores = np.asarray(raw[:2], dtype=np.float64)
        if scores.shape[0] != 2 or not np.all(np.isfinite(scores)):
            return unknown

        # Некоторые сборки возвращают нормированные вероятности, другие —
        # оценки до softmax. Поддерживаем оба варианта.
        total = float(scores.sum())
        if np.all(scores >= 0.0) and np.all(scores <= 1.0) and 0.90 <= total <= 1.10:
            probabilities = scores / max(total, 1e-12)
        else:
            shifted = scores - float(np.max(scores))
            exponentials = np.exp(shifted)
            probabilities = exponentials / max(float(exponentials.sum()), 1e-12)

        label_index = int(np.argmax(probabilities))
        # InsightFace genderage class order: 0 = female, 1 = male.
        return {
            "gender": "female" if label_index == 0 else "male",
            "confidence": max(0.0, min(1.0, float(probabilities[label_index]))),
            "gender_model": "insightface_genderage",
        }
    except Exception:
        # Совместимость с версиями InsightFace, в которых результат genderage
        # доступен лишь через face.gender / face.sex, но внутренний session отличается.
        raw_gender = getattr(face, "gender", None)
        if raw_gender is None:
            raw_gender = getattr(face, "sex", None)
        if isinstance(raw_gender, bool):
            label = "unknown"
        elif isinstance(raw_gender, (int, float)):
            label = "female" if int(raw_gender) == 0 else ("male" if int(raw_gender) == 1 else "unknown")
        else:
            text = str(raw_gender or "").strip().casefold()
            label = (
                "male" if text in ("m", "male", "1", "м", "мужской")
                else "female" if text in ("f", "female", "0", "ж", "женский")
                else "unknown"
            )
        return {
            "gender": label,
            # Без доступа к исходной оценке модели задаём только слабую уверенность.
            "confidence": 0.65 if label != "unknown" else 0.0,
            "gender_model": "insightface_genderage_fallback",
        }


def _classify_candidate_face_gender(candidate: dict) -> dict:
    """Классифицирует сохранённый face crop кандидата с кэшем по faces.id."""
    try:
        face_id = int(candidate.get("face_id") or 0)
    except (TypeError, ValueError):
        return {"gender": "unknown", "confidence": 0.0}
    if face_id <= 0 or FACE_APP is None:
        return {"gender": "unknown", "confidence": 0.0}

    with _FACE_GENDER_CACHE_LOCK:
        cached = _FACE_GENDER_CACHE.get(face_id)
        if cached is not None:
            return dict(cached)

    crop_path = None
    raw_path = str(candidate.get("path") or "").strip()
    if raw_path:
        try:
            resolved = face_search._resolve_db_crop_path(raw_path)
            if resolved.is_file():
                crop_path = resolved
        except (OSError, TypeError, ValueError):
            pass

    # Если путь не удалось разрешить напрямую (например, pending_ path),
    # читаем точный путь CROP по связке faces.id -> images.id.
    if crop_path is None:
        connection = None
        try:
            connection = _open_connection()
            row = connection.execute(
                "SELECT i.id, i.path FROM faces f JOIN images i ON i.id = f.image_id WHERE f.id = ?",
                (face_id,),
            ).fetchone()
            if row:
                resolved = face_search._resolve_db_crop_path(row[1], image_id=int(row[0]))
                if resolved.is_file():
                    crop_path = resolved
        except Exception as exc:
            print(f"[HTTP][GENDER] Не удалось найти CROP для face_id={face_id}: {exc}", flush=True)
        finally:
            if connection is not None:
                try:
                    connection.close()
                except Exception:
                    pass

    prediction = {"gender": "unknown", "confidence": 0.0}
    if crop_path is not None:
        try:
            image = face_search.cv2.imread(str(crop_path))
            if image is not None:
                # CROP может содержать небольшой фон. Берём самый крупный
                # уверенно обнаруженный face, а не угадываем по данным профиля.
                found = face_search._detect_faces_with_padding(
                    FACE_APP, image, padding_ratio=0.40
                )
                if found:
                    height, width = image.shape[:2]
                    def face_size(face):
                        box = face.bbox
                        area = max(0.0, float(box[2] - box[0])) * max(0.0, float(box[3] - box[1]))
                        return area * max(0.0, float(getattr(face, "det_score", 0.0)))
                    selected = max(found, key=face_size)
                    prediction = _predict_face_gender(image, selected)
        except Exception as exc:
            print(f"[HTTP][GENDER] Не удалось определить атрибут face_id={face_id}: {exc}", flush=True)

    # Кэшируем только валидную классификацию. Если CROP пока отсутствует,
    # неизвестный результат не должен блокировать повторную попытку после ремонта файла.
    if prediction.get("gender") in ("male", "female"):
        with _FACE_GENDER_CACHE_LOCK:
            _FACE_GENDER_CACHE[face_id] = dict(prediction)
            while len(_FACE_GENDER_CACHE) > _FACE_GENDER_CACHE_MAX:
                _FACE_GENDER_CACHE.pop(next(iter(_FACE_GENDER_CACHE)))
    return prediction

def _looks_like_image_content_type(value: str) -> bool:
    content_type = str(value or "").split(";", 1)[0].strip().lower()
    return (
        not content_type
        or content_type.startswith("image/")
        or content_type in ("application/octet-stream", "binary/octet-stream")
    )


def _remote_image_url_available(value: str) -> bool:
    """Проверяет доступность удалённого оригинала; результат ненадолго кэшируется."""
    image_url = str(value or "").strip()
    if not image_url.lower().startswith(("http://", "https://")):
        return False

    now = time.monotonic()
    with _REMOTE_IMAGE_CHECK_LOCK:
        cached = _REMOTE_IMAGE_CHECK_CACHE.get(image_url)
        if cached and now - cached[0] < _REMOTE_IMAGE_CHECK_CACHE_TTL:
            return cached[1]

    available = False
    retry_with_get = False
    try:
        image_url = _validate_remote_image_url(image_url)
        opener = urllib.request.build_opener(_SafeImageRedirectHandler())
        headers = {
            "User-Agent": "Mozilla/5.0 (compatible; FaceSearch/1.0)",
            "Accept": "image/avif,image/webp,image/*,*/*;q=0.5",
        }
        request = urllib.request.Request(image_url, headers=headers, method="HEAD")
        try:
            with opener.open(request, timeout=4) as response:
                available = (
                    200 <= int(getattr(response, "status", 200)) < 400
                    and _looks_like_image_content_type(response.headers.get("Content-Type", ""))
                )
        except urllib.error.HTTPError as exc:
            retry_with_get = exc.code in (403, 405, 501)
            try:
                exc.close()
            except Exception:
                pass

        # Некоторые хостинги запрещают HEAD, хотя обычная загрузка изображения работает.
        if not available and retry_with_get:
            request = urllib.request.Request(
                image_url,
                headers={**headers, "Range": "bytes=0-0"},
                method="GET",
            )
            with opener.open(request, timeout=4) as response:
                first_byte = response.read(1)
                available = (
                    bool(first_byte)
                    and 200 <= int(getattr(response, "status", 200)) < 400
                    and _looks_like_image_content_type(response.headers.get("Content-Type", ""))
                )
    except (urllib.error.URLError, TimeoutError, OSError, ValueError):
        available = False
    except Exception:
        available = False

    with _REMOTE_IMAGE_CHECK_LOCK:
        _REMOTE_IMAGE_CHECK_CACHE[image_url] = (now, available)
        while len(_REMOTE_IMAGE_CHECK_CACHE) > _REMOTE_IMAGE_CHECK_CACHE_MAX:
            _REMOTE_IMAGE_CHECK_CACHE.pop(next(iter(_REMOTE_IMAGE_CHECK_CACHE)))
    return available


def _original_image_available(source_url: str, url_image: str) -> bool:
    """True только если можно найти локальный оригинал или доступное изображение по URL."""
    try:
        local_source = face_search._source_url_to_local_path(source_url)
        if local_source is not None and local_source.is_file():
            return True
    except (OSError, TypeError, ValueError):
        pass
    return _remote_image_url_available(url_image)


def _start_crop_repair(face_ids, *, force: bool = False) -> None:
    """Восстанавливает отсутствующие CROP в фоне; force используется веб-запросами."""
    if (not force and not CREATE_CROPS_ON_HTML) or FACE_APP is None:
        return
    try:
        requested = {int(value) for value in face_ids if int(value) > 0}
    except (TypeError, ValueError):
        return
    if not requested:
        return
    now = time.monotonic()
    with _CROP_REPAIR_LOCK:
        pending = {
            face_id for face_id in requested
            if face_id not in _CROP_REPAIR_ACTIVE_IDS
            and (
                face_id not in _CROP_REPAIR_LAST_START
                or now - _CROP_REPAIR_LAST_START[face_id] >= _CROP_REPAIR_RETRY_INTERVAL
            )
        }
        if not pending:
            return
        _CROP_REPAIR_ACTIVE_IDS.update(pending)
        for face_id in pending:
            _CROP_REPAIR_LAST_START[face_id] = now
        while len(_CROP_REPAIR_LAST_START) > _CROP_REPAIR_LAST_START_MAX:
            oldest_id = min(_CROP_REPAIR_LAST_START, key=_CROP_REPAIR_LAST_START.get)
            _CROP_REPAIR_LAST_START.pop(oldest_id, None)

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
.page-header{display:flex;align-items:center;justify-content:space-between;gap:16px;margin-bottom:8px}
.page-header h1{font-size:28px;line-height:1.2;margin:0;min-width:0}
.page-header #theme-button{flex:0 0 auto;margin-left:auto;white-space:nowrap}
.subtitle{color:var(--muted);margin:0 0 24px}
.toolbar{display:flex;gap:14px;align-items:end;flex-wrap:wrap;padding:18px;background:var(--panel);border:1px solid var(--line);border-radius:14px;margin-bottom:18px;box-shadow:var(--shadow)}
.field{display:flex;flex-direction:column;gap:5px;min-width:150px}
.field label,.toggle-label{color:var(--muted);font-size:13px}
.url-field{flex:1 1 100%;min-width:0}
.url-row{display:flex;gap:10px;align-items:stretch;min-width:0}
.url-field .toggle-options-row{flex:0 0 auto;width:auto;align-self:flex-end;justify-content:flex-end;margin:0;gap:18px}
.url-row input{flex:1 1 auto;min-width:0;width:100%;background:var(--field);color:var(--text);border:1px solid var(--input-line);border-radius:8px;padding:10px;font:inherit}
.url-row button{flex:0 0 auto}
#file-input{display:none!important}
input[type=number]{width:145px;max-width:100%;background:var(--field);color:var(--text);border:1px solid var(--input-line);border-radius:8px;padding:10px;font:inherit}
.numeric-action-field{min-width:190px;gap:6px}
.numeric-action-field input[type=number]{width:100%}
.numeric-action-button{width:100%;margin-top:2px;white-space:normal}
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
#preview-canvas{display:block;max-width:none;max-height:none;width:auto;height:auto;object-fit:contain;cursor:crosshair;margin:0 auto}
.dropzone.preview-active{display:block;padding:10px;text-align:left}
.preview-controls{display:grid;grid-template-columns:minmax(0,1fr) auto minmax(0,1fr);align-items:center;width:100%;gap:8px;margin:0 0 9px}
.preview-control-buttons{grid-column:2;display:flex;align-items:center;justify-content:center;flex-wrap:wrap;gap:8px;min-width:0}
.preview-controls button{min-height:34px;min-width:38px;padding:6px 12px}
.preview-controls #identify-faces-button{min-width:112px;font-weight:650;margin-right:4px}
.preview-zoom-value{min-width:54px;text-align:center;font-size:12px;font-variant-numeric:tabular-nums;color:var(--text)}
.preview-zoom-hint{grid-column:3;justify-self:end;color:var(--muted);font-size:12px;margin-left:0;text-align:right}
  .gender-legend{grid-column:1/-1;justify-self:start;display:flex;flex-wrap:wrap;gap:10px;margin-top:3px}
  .gender-legend-item{display:inline-flex;align-items:center;gap:5px}
  .gender-legend-dot{width:9px;height:9px;display:inline-block;border-radius:50%;border:1px solid rgba(255,255,255,.72)}
  .gender-legend-dot.female{background:#f472b6}
  .gender-legend-dot.male{background:#38bdf8}
  .gender-legend-dot.unknown{background:#94a3b8}
.preview-stage{position:relative;display:block;flex:0 0 auto;margin:0 auto;line-height:0;max-width:none}
#preview-canvas{margin:0}
.face-link-overlay{position:absolute;inset:0;z-index:4;pointer-events:none;overflow:hidden}
.face-link-overlay a{position:absolute;display:block;pointer-events:auto;overflow:hidden;color:transparent;background:transparent;font-size:0;text-decoration:none;border-radius:2px}
.face-link-overlay a:focus-visible{outline:2px solid #fff;outline-offset:1px;box-shadow:0 0 0 3px rgba(22,122,198,.8)}
.preview-controls[hidden],.preview-viewport[hidden]{display:none!important}
.preview-viewport{width:100%;height:min(68vh,640px);min-height:180px;overflow:auto;overscroll-behavior:contain;background:var(--image-bg);border:1px solid var(--line);border-radius:10px}
.preview-viewport:focus-visible{outline:2px solid var(--accent);outline-offset:2px}
.status{min-height:28px;color:var(--muted);padding:10px 2px;overflow-wrap:anywhere;scroll-margin-top:10px}
.status.error{color:#c83e3e}
.group-options{margin:0 0 16px}
.group-options details,.group-panel{border:1px solid var(--line);background:var(--panel);border-radius:12px;padding:14px 16px;box-shadow:var(--shadow)}
.group-options summary{cursor:pointer;font-weight:650}
.group-options-note{color:var(--muted);font-size:12px;margin:5px 0 12px}
.group-options-grid{display:grid;grid-template-columns:repeat(auto-fit,minmax(155px,1fr));gap:12px;align-items:end}
.group-options-grid .field{min-width:0}
.group-options-grid input[type=number]{width:100%;min-width:0}
.group-options-grid .toggle-label{white-space:normal}
.group-panel{margin:18px 0 24px}
.group-panel[hidden]{display:none!important}
.group-panel-header{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;flex-wrap:wrap}
.group-panel h2{font-size:20px;margin:0}
.group-progress{height:10px;overflow:hidden;border-radius:99px;background:var(--secondary);margin:12px 0 5px}
.group-progress-bar{height:100%;width:0;background:var(--accent);transition:width .18s}
.group-progress-message{font-size:13px;color:var(--muted);overflow-wrap:anywhere}
.group-graph-wrap{margin-top:14px;border:1px solid var(--line);border-radius:10px;overflow:hidden;background:var(--drop)}
.group-graph{display:block;width:100%;height:auto;max-height:440px}
.group-gender-legend{display:flex;flex-wrap:wrap;align-items:center;gap:8px 16px;padding:10px 14px 0;font-size:12px;color:var(--muted)}
.group-gender-legend-item{display:inline-flex;align-items:center;gap:6px}
.group-gender-swatch{display:inline-block;width:11px;height:11px;border-radius:50%;border:2px solid transparent;flex:0 0 auto}
.group-gender-swatch.female{background:#fce7f3;border-color:#db2777}
.group-gender-swatch.male{background:#dbeafe;border-color:#0284c7}
.group-gender-swatch.unknown{background:#e5e7eb;border-color:#9ca3af}
.group-gender-swatch.mismatch{background:#dbeafe;border:3px solid #dc2626}
.group-gender-swatch.unmatched{background:#e5e7eb;border:2px dashed #6b7280}
.group-combinations{display:grid;gap:8px;margin-top:14px}
.group-combination{display:flex;align-items:flex-start;justify-content:space-between;gap:12px;text-align:left;width:100%;min-height:auto;background:var(--secondary);color:var(--button-text);border:1px solid var(--button-line);padding:11px 13px}
.group-combination[aria-pressed=true]{border-color:var(--accent);box-shadow:inset 3px 0 var(--accent)}
.group-combination small{display:block;color:var(--muted);font-weight:400;margin-top:3px}
.group-profile-list{display:grid;grid-template-columns:repeat(auto-fit,minmax(min(100%,190px),1fr));gap:12px;margin-top:14px}
.group-profile-card{border:1px solid var(--line);border-radius:10px;padding:10px;min-width:0;background:var(--panel)}
.group-profile-card img{display:block;width:100%;height:150px;object-fit:contain;background:var(--image-bg);border-radius:6px;margin-bottom:8px}
.group-profile-card .group-profile-name{font-weight:650;overflow-wrap:anywhere}
.group-profile-card .group-profile-meta{font-size:12px;color:var(--muted);margin-top:4px;overflow-wrap:anywhere}
.group-profile-card a{color:var(--link);overflow-wrap:anywhere}
.group-notice{font-size:12px;color:var(--muted);margin-top:12px;line-height:1.45}
.group-empty{padding:14px;border:1px dashed var(--line);border-radius:8px;color:var(--muted)}
@media(max-width:700px){.group-panel{padding:12px}.group-graph{min-height:190px}.group-combination{padding:10px}.group-profile-list{grid-template-columns:repeat(2,minmax(0,1fr))}}
@media(max-width:380px){.group-profile-list{grid-template-columns:minmax(0,1fr)}}
.index-meta{margin:4px 0 9px;color:var(--muted);font-size:12px;line-height:1.35;overflow-wrap:anywhere}
.index-status{margin:8px 0 12px;padding:11px 14px;border:1px solid var(--line);border-radius:9px;font-size:14px;line-height:1.45;overflow-wrap:anywhere}
.index-status[hidden]{display:none}
.index-status.warning{border-color:#d49a32;background:rgba(212,154,50,.12);color:var(--text)}
.index-status.error{border-color:#c83e3e;background:rgba(200,62,62,.10);color:var(--text)}
.index-status.success{border-color:#359b67;background:rgba(53,155,103,.10);color:var(--text)}
#search-button{margin:6px 0 18px}
.section-title{display:flex;align-items:baseline;gap:12px;flex-wrap:wrap;margin:23px 0 14px}
.section-title h2{font-size:20px;margin:0}
.section-title span{color:var(--muted);font-size:13px}
.gallery{display:grid;grid-template-columns:repeat(auto-fill,minmax(min(100%,265px),1fr));gap:18px}
.description-filter-field{flex:1 1 260px;width:auto;min-width:260px}
.description-filter-field input{width:100%;min-width:0}
.toggle-options-row{flex:0 0 auto;width:auto;display:flex;align-items:center;align-self:stretch;justify-content:flex-end;margin-left:auto;gap:18px;flex-wrap:nowrap;min-width:0}
.toggle-options-row .toggle-field{display:flex;flex:0 0 auto;flex-direction:row;align-items:center;min-width:0;min-height:42px;gap:6px}
.toggle-options-row .toggle-label{min-height:36px;white-space:nowrap;font-size:14px;color:var(--text)}
.card{position:relative;min-width:0;overflow:hidden;border:1px solid var(--line);background:var(--panel);border-radius:12px;padding-bottom:14px;box-shadow:var(--shadow);transition:transform .14s,box-shadow .14s}
.card:hover{transform:translateY(-2px)}
.card-image{height:270px;padding:4px;position:relative;background:var(--image-bg);display:flex;align-items:center;justify-content:center;overflow:hidden}
.card-image img{display:block;width:100%;height:100%;object-fit:contain;border-radius:3px}
.card-images{height:270px;padding:4px;position:relative;display:grid;grid-template-columns:repeat(2,minmax(0,1fr));gap:4px;overflow:hidden;background:var(--image-bg)}
.card-image-cell{display:flex;min-width:0;min-height:0;overflow:hidden;background:var(--panel2);border-radius:3px}
.card-image-label{position:absolute;left:50%;bottom:7px;z-index:3;transform:translateX(-50%);max-width:calc(100% - 12px);padding:3px 7px;text-align:center;color:#fff;background:rgba(0,0,0,.78);border:1px solid rgba(255,255,255,.18);border-radius:5px;font-size:10px;line-height:1.15;white-space:nowrap;pointer-events:none}
.card-image-view{flex:1;min-width:0;min-height:0;position:relative;display:flex;align-items:center;justify-content:center;overflow:hidden}
.card-image-view img{display:block;width:100%;height:100%;object-fit:contain;border-radius:2px}
.no-image[hidden]{display:none}.no-image{display:flex;align-items:center;justify-content:center;text-align:center;padding:18px;color:var(--muted);font-size:13px}
.no-image.unavailable{gap:7px}
.no-image.unavailable::before{content:"▧";display:inline-block;font-size:19px;line-height:1;color:var(--muted)}
.no-image.creating{gap:9px}
.no-image.creating::after{content:"";display:inline-block;width:12px;height:12px;flex:0 0 12px;border:2px solid currentColor;border-top-color:var(--accent);border-radius:50%;animation:thumbnail-spin .8s linear infinite}
@keyframes thumbnail-spin{to{transform:rotate(360deg)}}
@media(prefers-reduced-motion:reduce){.no-image.creating::after{animation-duration:2.5s}}
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
  .url-field .toggle-options-row{flex:0 0 auto;width:auto;align-self:flex-end;justify-content:flex-end;margin:0;gap:10px}
  .url-field .toggle-options-row .toggle-label{white-space:normal}
  .toolbar .numeric-field{flex:1 1 100%;width:100%;min-width:0}
  .toolbar .numeric-field input[type=number]{width:100%;min-width:0}
  .toolbar .numeric-action-button{width:100%}
  .page-header{gap:10px}
  .page-header h1{font-size:25px}
  .page-header #theme-button{padding:9px 11px;font-size:12px}
  .preview-controls{grid-template-columns:minmax(0,1fr);align-items:center;gap:6px}
  .preview-control-buttons{grid-column:1;justify-self:center;max-width:100%}
  .preview-controls #identify-faces-button{min-width:100px}
  .preview-zoom-hint{grid-column:1;justify-self:end;text-align:right;margin-left:0}
  .toolbar .action-field{flex:1 1 calc(50% - 12px);min-width:0}
  .toolbar .action-field button{width:100%;height:100%;padding:10px 8px;white-space:normal}
  .toggle-field{flex:1 1 100%;align-items:flex-start}
  .toggle-label{min-height:36px}
  .format-hint{display:block;position:relative;clear:both;flex:1 1 100%;width:100%;margin-top:7px;line-height:1.45;overflow-wrap:anywhere}
  .toolbar .mobile-only.action-field{flex:1 1 100%;width:100%;min-width:0}
  .toolbar .mobile-only.action-field button{width:100%;min-height:44px}
  .description-filter-field{flex:1 1 100%;width:100%}
  .toggle-options-row{flex:1 1 100%;width:100%;align-self:auto;justify-content:flex-end;margin-left:0;flex-wrap:wrap;gap:8px}
  .toggle-options-row .toggle-field{flex:0 1 auto;min-height:36px}
  .toggle-options-row .toggle-label{white-space:normal}
  .mobile-only{display:block;width:100%;margin:8px auto 0}
  .gallery{grid-template-columns:repeat(2,minmax(0,1fr));gap:12px}
  .card-image{height:clamp(150px,38vw,230px)}
  .card-images{height:clamp(150px,38vw,230px)}
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
  .card-images{height:230px}
  .toolbar{padding:12px}
}
</style>
</head>
<body>
<main>
  <header class="page-header">
    <h1>FaceSearch</h1>
    <button type="button" id="theme-button" class="secondary" aria-pressed="false">☀️ Дневная тема</button>
  </header>
  <p class="subtitle">Загрузите изображение — определение лиц и поиск по первому найденному лицу запустятся автоматически. На групповом фото можно выбрать другое лицо и повторить поиск.</p>
  <div id="index-meta" class="index-meta" aria-label="Информация о FAISS-индексе">FAISS: получение информации об индексе…</div>
  <div id="index-status" class="index-status" role="status" aria-live="polite" hidden></div>
  <section class="toolbar" aria-label="Параметры поиска">
    <div class="field url-field">
      <label for="image-url">Адрес изображения (HTTP/HTTPS)</label>
      <div class="url-row">
        <input id="image-url" type="url" inputmode="url" autocomplete="url" placeholder="https://example.com/photo.jpg">
        <button type="button" id="load-url-button" class="secondary">Загрузить адрес</button>
      </div>
      <div class="toggle-options-row" aria-label="Режим отображения изображений">
        <div class="toggle-field" title="При отсутствии CROP веб-интерфейс создаст его автоматически.">
          <label class="toggle-label" for="use-crop"><input id="use-crop" type="checkbox" __CROP_CHECKED__> Использовать CROP</label>
        </div>
        <div class="toggle-field" title="Показывать CROP и оригинал рядом в одной карточке.">
          <label class="toggle-label" for="double-images"><input id="double-images" type="checkbox"> Двойные изображения</label>
        </div>
      </div>
      <span class="hint">Для локального изображения используйте кнопку «Выбрать файл» ниже.</span>
    </div>
    <div class="field numeric-field numeric-action-field">
      <label for="similarity">Минимальное сходство, %</label>
      <input id="similarity" class="numeric-input" type="number" min="0" max="100" step="1" value="__DEFAULT_MIN_SIMILARITY__">
      <button type="button" id="detect-button" class="secondary numeric-action-button">Определить лица</button>
    </div>
    <div class="field numeric-field numeric-action-field">
      <label for="top-k">Количество результатов</label>
      <input id="top-k" class="numeric-input" type="number" min="1" max="500" step="1" value="__DEFAULT_TOP_K__">
      <button type="button" id="file-button" class="secondary numeric-action-button">Выбрать файл</button>
      <input id="file-input" type="file" accept="image/*" hidden>
    </div>
    <div class="field description-filter-field">
      <label for="description-filter">Фильтр описания карточки</label>
      <input id="description-filter" type="text" maxlength="500" autocomplete="off" placeholder="Например: Москва или katerina">
      <span class="hint">Частичное совпадение без учёта регистра во всех полях и вложенных метаданных карточки.</span>
    </div>
    <div class="field action-field mobile-only">
      <label>&nbsp;</label>
      <button type="button" id="camera-button" class="secondary">📷 Сделать снимок</button>
      <input id="camera-input" type="file" accept="image/*" capture="environment" hidden>
    </div>
    <span class="hint format-hint">Поддерживаются JPG, PNG, WEBP и другие форматы изображений.</span>
  </section>

  <section class="group-options" aria-label="Параметры группового анализа">
    <details>
      <summary>Параметры анализа связей группы — только для текущего сеанса</summary>
      <div class="group-options-note">Эти изменения не записываются в settings.xml. Постоянные значения задаются в окне FaceSearch → «Параметры группового анализа». Токены добавляются только через загрузчик VK; на этой странице их вводить не нужно.</div>
      <div class="group-options-grid">
        <div class="field">
          <label for="group-min-similarity">Минимальное сходство, %</label>
          <input id="group-min-similarity" type="number" min="0" max="100" step="1" value="__GROUP_MIN_SIMILARITY__">
        </div>
        <div class="field">
          <label for="group-candidates">Кандидатов на лицо</label>
          <input id="group-candidates" type="number" min="1" max="10" step="1" value="__GROUP_CANDIDATES_PER_FACE__">
        </div>
        <div class="field">
          <label for="group-friend-weight">Вес дружбы</label>
          <input id="group-friend-weight" type="number" min="0" max="20" step="0.5" value="__GROUP_FRIEND_WEIGHT__">
        </div>
        <div class="field">
          <label for="group-common-weight">Вес общих друзей</label>
          <input id="group-common-weight" type="number" min="0" max="10" step="0.5" value="__GROUP_COMMON_FRIEND_WEIGHT__">
        </div>
        <div class="field">
          <label for="group-city-weight">Вес совпадения города</label>
          <input id="group-city-weight" type="number" min="0" max="10" step="0.5" value="__GROUP_CITY_WEIGHT__">
        </div>
        <div class="field">
          <label for="group-gender-weight">Штраф за несовпадение пола лица по фото</label>
          <input id="group-gender-weight" type="number" min="0" max="10" step="0.5" value="__GROUP_GENDER_WEIGHT__">
        </div>
        <div class="field">
          <label for="group-profile-gender-weight">Штраф за несовпадение пола профиля</label>
          <input id="group-profile-gender-weight" type="number" min="0" max="30" step="1" value="__GROUP_PROFILE_GENDER_WEIGHT__">
        </div>
        <div class="field toggle-field">
          <label class="toggle-label" for="group-check-vk-friends"><input id="group-check-vk-friends" type="checkbox" __GROUP_CHECK_VK_FRIENDS__> Проверять друзей VK</label>
          <span class="hint">Используются токены, настроенные в загрузчике VK.</span>
        </div>
        <div class="field toggle-field">
          <label class="toggle-label" for="group-check-gender"><input id="group-check-gender" type="checkbox" __GROUP_CHECK_GENDER__> Учитывать пол лица и пол профиля</label>
          <span class="hint">Сопоставляются оценка InsightFace и поле пола в карточке профиля. Пол профиля учитывается отдельно и сильнее; это мягкий штраф, не жёсткий фильтр.</span>
        </div>
      </div>
    </details>
  </section>

  <section id="dropzone" class="dropzone">
    <div class="dropzone-content" id="drop-copy">
      <strong>Перетащите фотографию сюда</strong><br>
      <span class="hint">Также можно вставить изображение из буфера обмена.</span>
    </div>
    <div id="preview-controls" class="preview-controls" hidden aria-label="Определение лиц и управление масштабом">
      <div class="preview-control-buttons">
        <button type="button" id="identify-faces-button" class="secondary" disabled>Определить</button>
        <button type="button" id="group-analysis-button" class="secondary" disabled title="Активируется, когда обнаружено не менее двух лиц">Анализ связей группы</button>
        <button type="button" id="zoom-out-button" class="secondary" aria-label="Уменьшить масштаб">−</button>
        <span id="preview-zoom-value" class="preview-zoom-value">100%</span>
        <button type="button" id="zoom-in-button" class="secondary" aria-label="Увеличить масштаб">+</button>
        <button type="button" id="zoom-fit-button" class="secondary">Вписать</button>
      </div>
      <span class="preview-zoom-hint">Ctrl — масштаб · Shift — вверх/вниз · Alt — влево/вправо · колесо — страница</span>
      <span class="preview-zoom-hint gender-legend" aria-label="Цвет обнаруженного лица по оценке модели">
        <span class="gender-legend-item"><span class="gender-legend-dot female"></span>Женский класс</span>
        <span class="gender-legend-item"><span class="gender-legend-dot male"></span>Мужской класс</span>
        <span class="gender-legend-item"><span class="gender-legend-dot unknown"></span>Не определён / низкая уверенность</span>
      </span>
    </div>
    <div id="preview-viewport" class="preview-viewport" hidden>
      <div id="preview-stage" class="preview-stage">
        <canvas id="preview-canvas" aria-label="Предпросмотр. Нажмите на рамку нужного лица, чтобы выбрать его." tabindex="0"></canvas>
        <div id="face-link-overlay" class="face-link-overlay" aria-label="Ссылки на профили найденных лиц"></div>
      </div>
    </div>
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

  <section id="group-analysis-panel" class="group-panel" hidden aria-live="polite">
    <div class="group-panel-header">
      <div>
        <h2>Анализ связей группы</h2>
        <div class="hint">Поиск согласованного набора профилей с учётом сходства лиц, дружбы и города.</div>
      </div>
      <button type="button" id="group-analysis-hide" class="secondary">Скрыть</button>
    </div>
    <div class="group-progress" id="group-progress" role="progressbar" aria-label="Ход анализа" aria-valuemin="0" aria-valuemax="100" aria-valuenow="0">
      <div class="group-progress-bar" id="group-progress-bar"></div>
    </div>
    <div id="group-progress-message" class="group-progress-message">Ожидание запуска анализа.</div>
    <div id="group-analysis-summary"></div>
    <div id="group-graph-wrap" class="group-graph-wrap" hidden>
      <div class="group-gender-legend" aria-label="Обозначения пола на графе">
        <span class="group-gender-legend-item"><span class="group-gender-swatch female" aria-hidden="true"></span>Женский класс исходного лица</span>
        <span class="group-gender-legend-item"><span class="group-gender-swatch male" aria-hidden="true"></span>Мужской класс исходного лица</span>
        <span class="group-gender-legend-item"><span class="group-gender-swatch unknown" aria-hidden="true"></span>Пол не определён</span>
        <span class="group-gender-legend-item"><span class="group-gender-swatch mismatch" aria-hidden="true"></span>Пол кандидата не совпадает</span>
        <span class="group-gender-legend-item"><span class="group-gender-swatch unmatched" aria-hidden="true"></span>Пунктир — профиль не назначен</span>
      </div>
      <svg id="group-graph" class="group-graph" viewBox="0 0 600 360" role="img" aria-label="Граф связей выбранной комбинации; цвет узла отражает оценку пола исходного лица"></svg>
    </div>
    <div id="group-profile-list" class="group-profile-list"></div>
    <div id="group-combinations" class="group-combinations"></div>
    <div id="group-analysis-notice" class="group-notice"></div>
  </section>

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
  const previewViewport=document.getElementById("preview-viewport");
  const previewStage=document.getElementById("preview-stage");
  const faceLinkOverlay=document.getElementById("face-link-overlay");
  const previewControls=document.getElementById("preview-controls");
  const identifyFacesButton=document.getElementById("identify-faces-button");
  const groupAnalysisButton=document.getElementById("group-analysis-button");
  const previewZoomValue=document.getElementById("preview-zoom-value");
  const zoomOutButton=document.getElementById("zoom-out-button");
  const zoomInButton=document.getElementById("zoom-in-button");
  const zoomFitButton=document.getElementById("zoom-fit-button");
  let previewZoom=1;
  const statusBox=document.getElementById("status");
  const detectButton=document.getElementById("detect-button");
  const searchButton=document.getElementById("search-button");
  const gallery=document.getElementById("gallery");
  const resultCount=document.getElementById("result-count");
  const similarityInput=document.getElementById("similarity");
  const topKInput=document.getElementById("top-k");
  const descriptionFilterInput=document.getElementById("description-filter");
  const useCropCheckbox=document.getElementById("use-crop");
  const doubleImagesCheckbox=document.getElementById("double-images");
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
  let faceMatchResults=[];
  let faceIdentificationRun=0;
  let identificationInProgress=false;
  let lightboxItems=[];
  let lightboxIndex=-1;
  let cameraStream=null;
  let cameraOpening=false;
  let lastResults=[];
  let groupAnalysisMatches=null;

  const indexStatusBox=document.getElementById("index-status");
  const indexMetaBox=document.getElementById("index-meta");
  let knownIndexGeneration=null;
  let indexNoticeUntil=0;
  let indexStatusPollInFlight=false;
  async function pollIndexStatus(){
    // Не допускаем параллельные опросы: старый ответ не должен затирать новый статус.
    if(indexStatusPollInFlight)return;
    indexStatusPollInFlight=true;
    try{
      const response=await fetch("/api/status?refresh="+Date.now(),{cache:"no-store"});
      if(!response.ok)return;
      const info=await response.json();
      const state=String(info.reload_state||"");
      function formatIndexSize(value){
        const bytes=Number(value||0);
        if(!Number.isFinite(bytes)||bytes<=0)return "размер неизвестен";
        const units=["Б","КБ","МБ","ГБ","ТБ"];
        let size=bytes,unit=0;
        while(size>=1024&&unit<units.length-1){size/=1024;unit++;}
        return (unit===0?String(Math.round(size)):size.toFixed(2).replace(".",","))+" "+units[unit];
      }
      function formatIndexDate(value){
        if(!value)return "н/д";
        const date=new Date(value);
        if(Number.isNaN(date.getTime()))return "н/д";
        return date.toLocaleString("ru-RU",{day:"2-digit",month:"2-digit",year:"numeric",hour:"2-digit",minute:"2-digit",second:"2-digit"});
      }
      indexMetaBox.textContent=info.index_exists
        ? "FAISS: "+formatIndexSize(info.index_size_bytes)
          +" · создан: "+formatIndexDate(info.index_created_at)
        : "FAISS: файл индекса не найден";
      const waitingSearches=Number(info.active_searches||0);
      let message="";
      let className="warning";
      if(state==="pending"){
        message=info.reload_message||"Обнаружено изменение FAISS-индекса. Ожидание завершения записи.";
      }else if(state==="waiting"){
        message=(info.reload_message||"Ожидание завершения поисковых запросов перед переключением индекса.")
          +(waitingSearches>0?" Сейчас выполняется поисков: "+waitingSearches+".":"");
      }else if(state==="loading"){
        message=info.reload_message||"Загружается новая версия FAISS. Запросы поиска временно ожидают переключения.";
      }else if(state==="error"){
        message=info.reload_message||"Не удалось обновить FAISS-индекс. Сервер продолжает использовать прежнюю версию, если она была загружена.";
        className="error";
        indexNoticeUntil=0;
      }else if(state==="missing"){
        message=info.reload_message||"Файл FAISS-индекса не найден.";
        className="error";
        indexNoticeUntil=0;
      }else if(state==="fallback"){
        message=info.reload_message||"GPU-индекс недоступен; поиск выполняется с CPU.";
        className="warning";
        indexNoticeUntil=0;
      }else if(knownIndexGeneration!==null&&Number(info.generation)!==knownIndexGeneration){
        message="FAISS-индекс обновлён. Новые поисковые запросы используют актуальную версию.";
        className="success";
        indexNoticeUntil=Date.now()+8000;
      }
      knownIndexGeneration=Number(info.generation||0);
      if(message){
        indexStatusBox.className="index-status "+className;
        indexStatusBox.textContent=message;
        indexStatusBox.hidden=false;
      }else if(Date.now()<indexNoticeUntil){
        indexStatusBox.className="index-status success";
        indexStatusBox.hidden=false;
      }else{
        indexStatusBox.hidden=true;
      }
    }catch(e){}finally{
      indexStatusPollInFlight=false;
    }
  }
  pollIndexStatus();
  setInterval(pollIndexStatus,4000);

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
  function showImageFailure(image,message){
    const box=image.parentElement;
    const placeholder=box&&box.querySelector(".no-image");
    image.style.display="none";
    if(placeholder){
      placeholder.classList.remove("creating");
      placeholder.classList.add("unavailable");
      placeholder.textContent=message||"Нет изображения";
      placeholder.hidden=false;
    }
  }
  function clearImageRetryTimers(image){
    if(image._cropRetryTimer){clearTimeout(image._cropRetryTimer);image._cropRetryTimer=null;}
    if(image._cropStatusTimer){clearTimeout(image._cropStatusTimer);image._cropStatusTimer=null;}
  }
  async function pollCropStatus(image,requestId){
    if(image.dataset.imageRequestId!==requestId||image.dataset.cropEnabled!=="true")return;
    const box=image.parentElement;
    const placeholder=box&&box.querySelector(".no-image");
    image.style.display="none";
    if(placeholder){
      // Текст и DOM не меняются во время опроса: анимация показывает,
      // что проверка продолжается, не создавая визуального подёргивания.
      if(!placeholder.classList.contains("creating")){
        placeholder.classList.remove("unavailable");
        placeholder.textContent="Идёт создание миниатюры";
        placeholder.classList.add("creating");
      }
      placeholder.hidden=false;
    }
    try{
      const response=await fetch(
        "/api/face/"+encodeURIComponent(image.dataset.faceId)+"/image-status?refresh="+Date.now(),
        {cache:"no-store"}
      );
      let data={};
      try{data=await response.json();}catch(e){}
      if(image.dataset.imageRequestId!==requestId||image.dataset.cropEnabled!=="true")return;
      if(!response.ok||data.state==="missing"){
        showImageFailure(image,"Нет изображения");
        return;
      }
      if(data.state==="ready"){
        if(placeholder){
          placeholder.classList.remove("creating");
          placeholder.hidden=true;
        }
        image.style.display="block";
        image.src=faceImageUrl(image.dataset.faceId,true,true);
        return;
      }
      if(data.state==="creating"){
        if(placeholder)placeholder.hidden=false;
        image._cropStatusTimer=setTimeout(function(){
          pollCropStatus(image,requestId);
        },1500);
        return;
      }
      showImageFailure(image,"Нет изображения");
    }catch(error){
      if(image.dataset.imageRequestId===requestId){
        showImageFailure(image,"Нет изображения");
      }
    }
  }
  function configureCardImage(image,faceId,useCrop,refresh){
    image.dataset.faceId=String(faceId);
    image.dataset.cropEnabled=String(Boolean(useCrop));
    image.dataset.imageRequestId=String(Number(image.dataset.imageRequestId||"0")+1);
    const requestId=image.dataset.imageRequestId;
    image.style.display="block";
    const box=image.parentElement;
    const placeholder=box&&box.querySelector(".no-image");
    if(placeholder){
      placeholder.classList.remove("creating","unavailable");
      placeholder.textContent="Нет изображения";
      placeholder.hidden=true;
    }
    clearImageRetryTimers(image);
    image.onload=function(){
      if(image.dataset.imageRequestId!==requestId)return;
      image.style.display="block";
      if(placeholder){
        placeholder.classList.remove("creating");
        placeholder.hidden=true;
      }
      clearImageRetryTimers(image);
    };
    image.onerror=function(){
      if(image.dataset.imageRequestId!==requestId)return;
      if(image.dataset.cropEnabled==="true"){
        pollCropStatus(image,requestId);
        return;
      }
      showImageFailure(image,"Нет изображения");
    };
    image.onclick=function(){openLightboxForImage(image);};
    image.src=faceImageUrl(faceId,useCrop,refresh);
  }
  function refreshCardImages(){
    gallery.querySelectorAll("img[data-face-image='true']").forEach(function(image){
      const useCrop=image.dataset.fixedCrop!==undefined
        ?image.dataset.fixedCrop==="true"
        :useCropCheckbox.checked;
      configureCardImage(image,image.dataset.faceId,useCrop,true);
    });
  }
  useCropCheckbox.addEventListener("change",refreshCardImages);
  function syncDoubleImageControls(){
    if(doubleImagesCheckbox.checked){
      useCropCheckbox.checked=true;
      useCropCheckbox.disabled=true;
    }else{
      useCropCheckbox.disabled=false;
    }
  }
  doubleImagesCheckbox.addEventListener("change",function(){
    syncDoubleImageControls();
    renderResults(lastResults);
  });
  syncDoubleImageControls();

  function openLightboxForImage(image){
    const selectedCard=image.closest(".card");
    if(!selectedCard)return;
    // В двойном режиме в карточке два <img>, но это один результат поиска.
    // Карусель хранит ровно одну позицию на карточку, чтобы оригиналы не дублировались.
    lightboxItems=Array.from(gallery.querySelectorAll(".card")).map(function(card){
      const cardImage=card.querySelector("img[data-face-image='true']");
      if(!cardImage)return null;
      const title=card.querySelector(".card-title");
      return {
        card:card,
        faceId:cardImage.dataset.faceId,
        alt:(title&&title.textContent)||cardImage.alt||"Увеличенное изображение"
      };
    }).filter(Boolean);
    lightboxIndex=lightboxItems.findIndex(function(item){return item.card===selectedCard;});
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
    // Просмотр всегда показывает оригинал; двойные CROP/оригинал одной карточки
    // не образуют две отдельные позиции в карусели.
    lightboxImage.src=faceImageUrl(current.faceId,false,true);
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
    descriptionFilterInput.disabled=value;
    identifyFacesButton.disabled=value||!queryId||!faces.length||identificationInProgress;
    if(window.FaceSearchGroupUI)window.FaceSearchGroupUI.sync();
  }
  function useDataUrl(sourceDataUrl){
    dataUrl=String(sourceDataUrl||"");
    previewZoom=1;
    faceIdentificationRun++;
    faceMatchResults=[];
    groupAnalysisMatches=null;
    identificationInProgress=false;
    faceLinkOverlay.replaceChildren();
    identifyFacesButton.textContent="Определить";
    if(window.FaceSearchGroupUI)window.FaceSearchGroupUI.reset();
    imageObject=new Image();
    imageObject.onload=function(){
      queryId="";faces=[];selectedFace=-1;
      canvas.width=imageObject.naturalWidth;
      canvas.height=imageObject.naturalHeight;
      ctx.clearRect(0,0,canvas.width,canvas.height);
      ctx.drawImage(imageObject,0,0);
      canvas.style.display="block";
      previewViewport.hidden=false;
      previewControls.hidden=false;
      dropzone.classList.add("preview-active");
      dropCopy.style.display="none";
      applyPreviewZoom(true);
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
    previewStage.style.width=canvas.style.width||canvas.width+"px";
    previewStage.style.height=canvas.style.height||canvas.height+"px";
    faceLinkOverlay.replaceChildren();
    // Рамки и метки задаются в экранных пикселях с учётом масштаба canvas.
    const displayScale=Math.max(0.01,canvas.getBoundingClientRect().width/canvas.width||1);
    const lineWidth=1.2/displayScale;
    const selectedLineWidth=2/displayScale;
    const fontSize=11/displayScale;
    const padding=4/displayScale;
    const labelHeight=17/displayScale;
    const anchors=[];
    const drawOrder=faces.map(function(face,index){return {face:face,index:index};})
      .sort(function(a,b){return (a.index===selectedFace?1:0)-(b.index===selectedFace?1:0);});
    function markerStyle(face,match){
      function normalizeGender(value){
        const gender=String(value||"").trim().toLowerCase();
        if(gender==="female"||gender==="f"||gender==="женский")return "female";
        if(gender==="male"||gender==="m"||gender==="мужской")return "male";
        return "unknown";
      }
      function readConfidence(value){
        if(value===undefined||value===null||value==="")return null;
        const number=Number(value);
        return Number.isFinite(number)?number:null;
      }

      // Цвет относится к полу обнаруженного лица на исходном фото.
      // Имя/совпадение из поиска не должно затирать его значением "unknown".
      const detectedGender=normalizeGender(face&&face.gender);
      const matchedGender=normalizeGender(match&&match.gender);
      const gender=detectedGender!=="unknown"?detectedGender:matchedGender;
      const detectedConfidence=readConfidence(face&&face.gender_confidence);
      const matchedConfidence=readConfidence(
        match&&(match.genderConfidence!==undefined?match.genderConfidence:match.gender_confidence)
      );
      const confidence=detectedGender!=="unknown"
        ?(detectedConfidence!==null?detectedConfidence:matchedConfidence)
        :(matchedConfidence!==null?matchedConfidence:detectedConfidence);
      if(gender==="unknown"||(confidence!==null&&confidence<0.65))
        return {stroke:"#94a3b8",fill:"#475569",gender:"unknown"};
      if(gender==="female")return {stroke:"#f472b6",fill:"#9d174d",gender:"female"};
      if(gender==="male")return {stroke:"#38bdf8",fill:"#075985",gender:"male"};
      return {stroke:"#94a3b8",fill:"#475569",gender:"unknown"};
    }
    drawOrder.forEach(function(entry){
      const face=entry.face,index=entry.index,b=face.bbox;
      const match=faceMatchResults[index]||null;
      const style=markerStyle(face,match);
      ctx.lineWidth=index===selectedFace?selectedLineWidth:lineWidth;
      ctx.strokeStyle=style.stroke;
      ctx.strokeRect(b[0],b[1],b[2]-b[0],b[3]-b[1]);
      // Круг на рамке помогает распознать класс даже при короткой подписи.
      const radius=5/displayScale;
      const cx=Math.min(canvas.width-radius,Math.max(radius,b[2]));
      const cy=Math.min(canvas.height-radius,Math.max(radius,b[1]));
      ctx.beginPath();
      ctx.arc(cx,cy,radius,0,Math.PI*2);
      ctx.fillStyle=style.stroke;
      ctx.fill();
      ctx.lineWidth=1.5/displayScale;
      ctx.strokeStyle="#ffffff";
      ctx.stroke();
    });
    drawOrder.forEach(function(entry){
      const face=entry.face,index=entry.index,b=face.bbox;
      const match=faceMatchResults[index]||null;
      const style=markerStyle(face,match);
      const fullName=String(match&&match.name||"").trim();
      const labelSource=fullName||"#"+(index+1);
      ctx.font="600 "+fontSize+"px sans-serif";
      const maxLabelWidth=Math.min(220/displayScale,Math.max(72/displayScale,(b[2]-b[0])+44/displayScale));
      let label=labelSource;
      while(label.length>4&&ctx.measureText(label).width+padding*2>maxLabelWidth){
        label=label.slice(0,-2).trimEnd()+"…";
      }
      const labelWidth=Math.min(maxLabelWidth,ctx.measureText(label).width+padding*2);
      const labelX=Math.max(0,Math.min(canvas.width-labelWidth,b[0]));
      let labelY=b[1]-labelHeight-2/displayScale;
      if(labelY<0)labelY=Math.min(canvas.height-labelHeight,b[3]+2/displayScale);
      ctx.globalAlpha=index===selectedFace?0.84:0.58;
      ctx.fillStyle=style.fill;
      if(index===selectedFace)ctx.globalAlpha=0.94;
      ctx.fillRect(labelX,labelY,labelWidth,labelHeight);
      ctx.globalAlpha=1;
      ctx.fillStyle="#ffffff";
      ctx.fillText(label,labelX+padding,labelY+labelHeight*0.72);
      const profileUrl=fullName&&match?safeLink(match.profileUrl):"";
      if(profileUrl){
        anchors.push({
          url:profileUrl,
          name:fullName,
          score:match.similarity,
          x:labelX*displayScale,
          y:labelY*displayScale,
          width:labelWidth*displayScale,
          height:labelHeight*displayScale
        });
      }
    });
    anchors.forEach(function(item){
      const link=document.createElement("a");
      link.href=item.url;
      link.target="_blank";
      link.rel="noopener noreferrer";
      link.title=item.name+(Number.isFinite(item.score)?" · сходство "+item.score.toFixed(2)+"%":"");
      link.setAttribute("aria-label","Открыть профиль: "+item.name);
      link.style.left=item.x+"px";
      link.style.top=item.y+"px";
      link.style.width=item.width+"px";
      link.style.height=item.height+"px";
      link.textContent=item.name;
      faceLinkOverlay.appendChild(link);
    });
  }
  function previewFitScale(){
    if(!imageObject)return 1;
    const width=Math.max(1,previewViewport.clientWidth-18);
    const height=Math.max(1,previewViewport.clientHeight-18);
    return Math.min(width/imageObject.naturalWidth,height/imageObject.naturalHeight,1);
  }
  function applyPreviewZoom(reset,anchorX,anchorY){
    if(!imageObject)return;
    if(reset)previewZoom=1;
    const viewportRect=previewViewport.getBoundingClientRect();
    const oldRect=canvas.getBoundingClientRect();
    const clientX=Number.isFinite(anchorX)?anchorX:viewportRect.left+previewViewport.clientWidth/2;
    const clientY=Number.isFinite(anchorY)?anchorY:viewportRect.top+previewViewport.clientHeight/2;
    const ratioX=oldRect.width>0?Math.max(0,Math.min(1,(clientX-oldRect.left)/oldRect.width)):0.5;
    const ratioY=oldRect.height>0?Math.max(0,Math.min(1,(clientY-oldRect.top)/oldRect.height)):0.5;
    const fit=previewFitScale();
    canvas.style.width=Math.max(1,Math.round(imageObject.naturalWidth*fit*previewZoom))+"px";
    canvas.style.height=Math.max(1,Math.round(imageObject.naturalHeight*fit*previewZoom))+"px";
    previewStage.style.width=canvas.style.width;
    previewStage.style.height=canvas.style.height;
    previewZoomValue.textContent=Math.round(previewZoom*100)+"%";
    zoomOutButton.disabled=previewZoom<=0.25;
    zoomInButton.disabled=previewZoom>=8;
    if(reset){
      previewViewport.scrollLeft=0;
      previewViewport.scrollTop=0;
      drawFaces();
      return;
    }
    requestAnimationFrame(function(){
      const nextRect=canvas.getBoundingClientRect();
      previewViewport.scrollLeft+=nextRect.left+ratioX*nextRect.width-clientX;
      previewViewport.scrollTop+=nextRect.top+ratioY*nextRect.height-clientY;
      drawFaces();
    });
  }
  function changePreviewZoom(direction,anchorX,anchorY){
    previewZoom=Math.max(0.25,Math.min(8,previewZoom*(direction>0?1.25:1/1.25)));
    applyPreviewZoom(false,anchorX,anchorY);
  }
  zoomInButton.addEventListener("click",function(){changePreviewZoom(1);});
  zoomOutButton.addEventListener("click",function(){changePreviewZoom(-1);});
  zoomFitButton.addEventListener("click",function(){applyPreviewZoom(true);});
  function wheelDeltaPixels(event,axis){
    let delta=axis==="x"?event.deltaX:event.deltaY;
    if(event.deltaMode===1)delta*=16;
    else if(event.deltaMode===2){
      delta*=Math.max(100,axis==="x"?previewViewport.clientWidth:previewViewport.clientHeight);
    }
    return delta;
  }
  previewViewport.addEventListener("wheel",function(event){
    if(!imageObject||previewViewport.hidden)return;
    const deltaX=wheelDeltaPixels(event,"x");
    const deltaY=wheelDeltaPixels(event,"y");
    if(event.ctrlKey){
      // Ctrl + колесо меняет масштаб с фокусом на участке под курсором.
      event.preventDefault();
      if(deltaY!==0)changePreviewZoom(deltaY<0?1:-1,event.clientX,event.clientY);
      return;
    }
    if(event.shiftKey){
      // Shift + колесо двигает увеличенное изображение вверх/вниз.
      event.preventDefault();
      previewViewport.scrollTop+=deltaY||deltaX;
      return;
    }
    if(event.altKey){
      // Alt + колесо двигает изображение влево/вправо.
      event.preventDefault();
      previewViewport.scrollLeft+=deltaX||deltaY;
      return;
    }
    // Без модификаторов колесо прокручивает страницу, а не внутреннюю область изображения.
    event.preventDefault();
    window.scrollBy({top:deltaY,left:deltaX,behavior:"auto"});
  },{passive:false});
  window.addEventListener("resize",function(){
    if(imageObject&&!previewViewport.hidden)applyPreviewZoom(false);
  });
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
    faceIdentificationRun++;
    faceMatchResults=[];
    groupAnalysisMatches=null;
    if(window.FaceSearchGroupUI)window.FaceSearchGroupUI.reset();
    identificationInProgress=false;
    faceLinkOverlay.replaceChildren();
    identifyFacesButton.textContent="Определить";
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
    finally{
      if(window.FaceSearchGroupUI)window.FaceSearchGroupUI.sync();
      setBusy(false);
    }
    if(detected&&autoSearch)await searchSimilar();
  }
  detectButton.addEventListener("click",function(){detectFaces(false);});

  async function identifyAllFaces(){
    if(!queryId||!faces.length||busy||identificationInProgress)return;
    if(groupAnalysisMatches&&groupAnalysisMatches.queryId===queryId){
      faceIdentificationRun++;
      faceMatchResults=groupAnalysisMatches.matches.map(function(match){
        return match?Object.assign({},match):null;
      });
      drawFaces();
      const identified=faceMatchResults.filter(function(item){
        return item&&item.name&&item.name!=="Нет совпадения";
      }).length;
      setStatus(
        "Использованы имена из выбранного варианта группового анализа: "+
        identified+" из "+faces.length+" лиц. Повторный поиск не выполнялся."
      );
      return;
    }
    const runId=++faceIdentificationRun;
    const activeQueryId=queryId;
    const faceCount=faces.length;
    identificationInProgress=true;
    faceMatchResults=Array(faceCount).fill(null);
    identifyFacesButton.textContent="Определение…";
    setBusy(true);
    drawFaces();
    setStatus("Ищу наиболее похожие профили для всех лиц: 0 / "+faceCount+"…");
    let nextIndex=0,completed=0,failed=0;
    async function worker(){
      while(true){
        const faceIndex=nextIndex++;
        if(faceIndex>=faceCount)return;
        try{
          const response=await postJson("/api/search",{
            query_id:activeQueryId,
            face_index:faceIndex,
            min_similarity:0,
            top_k:1,
            text_filter:""
          });
          const results=Array.isArray(response.results)?response.results:[];
          let best=null;
          results.forEach(function(item){
            if(!best||Number(item.similarity||0)>Number(best.similarity||0))best=item;
          });
          if(best){
            const profileName=String(best.profile_name||"").replace(/\s+/g," ").trim();
            const profileUrl=safeLink(best.profile_url||"");
            if(profileName){
              faceMatchResults[faceIndex]={
                name:profileName,
                profileUrl:profileUrl,
                similarity:Number(best.similarity)
              };
            }
          }
        }catch(error){
          failed++;
          console.warn("Не удалось определить профиль для лица "+(faceIndex+1)+":",error);
        }
        completed++;
        if(runId!==faceIdentificationRun)return;
        drawFaces();
        setStatus("Поиск профилей: "+completed+" / "+faceCount+"…");
      }
    }
    try{
      // До трёх запросов одновременно: параллельный поиск, но оставляем один
      // слот API-семафора свободным для обычных действий пользователя.
      await Promise.all(Array.from({length:Math.min(3,faceCount)},worker));
    }finally{
      if(runId===faceIdentificationRun){
        identificationInProgress=false;
        identifyFacesButton.textContent="Определить";
        setBusy(false);
        drawFaces();
        const identified=faceMatchResults.filter(function(item){return item&&item.name;}).length;
        setStatus(
          "Определение завершено: "+identified+" из "+faceCount+" лиц сопоставлено"+
          (failed?"; ошибок запросов: "+failed:"")+".",
          failed>0
        );
      }
    }
  }
  identifyFacesButton.addEventListener("click",identifyAllFaces);

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
      "height","heightm","heightabovesealevel","gpsвысота","высота","высотанадуровнемморя","высотанадморем"
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
    let mapUrl="";
    if(geo.validCoordinates){
      const row=document.createElement("div");row.className="card-info-row";
      const label=document.createElement("b");label.textContent="Координаты: ";
      const link=document.createElement("a");
      const lat=geo.latitude.toFixed(6),lon=geo.longitude.toFixed(6);
      mapUrl="https://www.openstreetmap.org/?mlat="+encodeURIComponent(lat)+
        "&mlon="+encodeURIComponent(lon)+"#map=16/"+lat+"/"+lon;
      link.href=mapUrl;
      link.target="_blank";link.rel="noopener noreferrer";
      link.textContent=lat+", "+lon;
      link.title="Показать координаты на карте OpenStreetMap";
      row.append(label,link);box.appendChild(row);
      if(geo.altitude!==null){
        const altitudeRow=document.createElement("div");altitudeRow.className="card-info-row";
        const altitudeLabel=document.createElement("b");altitudeLabel.textContent="Высота над уровнем моря: ";
        const altitudeLink=document.createElement("a");
        altitudeLink.href=mapUrl;altitudeLink.target="_blank";altitudeLink.rel="noopener noreferrer";
        altitudeLink.textContent=String(geo.altitude)+" м";
        altitudeLink.title="Показать место съёмки на карте OpenStreetMap";
        altitudeRow.append(altitudeLabel,altitudeLink);box.appendChild(altitudeRow);
      }
    }
    const ordered=isLocal?entries.sort(function(a,b){
      const ai=Object.keys(LOCAL_INFO_LABELS).indexOf(a[0]);
      const bi=Object.keys(LOCAL_INFO_LABELS).indexOf(b[0]);
      return (ai<0?999:ai)-(bi<0?999:bi);
    }):entries;
    ordered.forEach(function(pair){
      const key=pair[0],value=pair[1],type=geoFieldType(key);
      if(isLocal&&!Object.prototype.hasOwnProperty.call(LOCAL_INFO_LABELS,key)&&type!=="altitude")return;
      if(geo.validCoordinates&&(type==="latitude"||type==="longitude"||type==="coordinates"||type==="altitude"))return;
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
      text.textContent=rendered;row.append(label,text);
      box.appendChild(row);
    });
    body.appendChild(box);
  }
  function createDualImageCell(faceId,useCrop,labelText,altText){
    const cell=document.createElement("div");cell.className="card-image-cell";
    const view=document.createElement("div");view.className="card-image-view";
    const image=document.createElement("img");image.loading="lazy";image.dataset.faceImage="true";
    image.dataset.fixedCrop=String(Boolean(useCrop));image.alt=altText;
    const placeholder=document.createElement("div");placeholder.className="no-image";
    placeholder.textContent=useCrop?"Идёт создание миниатюры":"Нет изображения";placeholder.hidden=true;
    const label=document.createElement("div");label.className="card-image-label";label.textContent=labelText;
    view.append(image,placeholder,label);cell.append(view);
    configureCardImage(image,faceId,useCrop,false);
    return cell;
  }
  function renderResults(results){
    lastResults=Array.isArray(results)?results:[];
    results=lastResults;
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
      let imageBox;
      const rank=document.createElement("div");rank.className="rank";rank.textContent="#"+String(item.rank||"");
      const score=document.createElement("div");score.className="score";score.textContent=Number(item.similarity||0).toFixed(2)+"%";
      if(doubleImagesCheckbox.checked){
        imageBox=document.createElement("div");imageBox.className="card-images";
        const alt=displayName||("Лицо ID "+faceId);
        imageBox.append(
          createDualImageCell(faceId,true,"CROP",alt+" — CROP"),
          createDualImageCell(faceId,false,"Оригинал",alt+" — оригинал"),
          rank,score
        );
      }else{
        imageBox=document.createElement("div");imageBox.className="card-image";
        const image=document.createElement("img");image.loading="lazy";image.dataset.faceImage="true";
        image.alt=displayName||("Лицо ID "+faceId);
        const placeholder=document.createElement("div");placeholder.className="no-image";placeholder.textContent="Нет изображения";placeholder.hidden=true;
        imageBox.append(image,placeholder,rank,score);
        configureCardImage(image,faceId,useCropCheckbox.checked,false);
      }
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
        try{
          const sourceUrl=new URL(source);
          const sourceHost=sourceUrl.hostname.toLowerCase().replace(/\.$/,"");
          const isInstagram=sourceHost==="instagram.com"||sourceHost.endsWith(".instagram.com");
          favicon.src=isInstagram
            ?"https://static.cdninstagram.com/rsrc.php/y4/r/QaBlI0OZiks.ico"
            :new URL("/favicon.ico",sourceUrl).href;
        }catch(e){favicon.style.display="none";}
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
        min_similarity:minimum,top_k:Math.trunc(topK),
        text_filter:String(descriptionFilterInput.value||"").trim()
      });
      renderResults(data.results||[]);
      setStatus("Поиск завершён. Найдено результатов: "+(data.results||[]).length+".");
      requestAnimationFrame(function(){
        const resultsSection=gallery.closest("section");
        const resultsHeader=resultsSection&&resultsSection.querySelector(".section-title");
        if(resultsHeader)resultsHeader.scrollIntoView({behavior:"smooth",block:"start"});
      });
    }catch(error){setStatus(error.message||String(error),true);}
    finally{setBusy(false);}
  }
  searchButton.addEventListener("click",searchSimilar);
  descriptionFilterInput.addEventListener("keydown",function(event){
    if(event.key==="Enter"){event.preventDefault();searchSimilar();}
  });
  document.addEventListener("keydown",function(event){
    if(event.key!=="Enter"||event.repeat||event.isComposing)return;
    const target=event.target;
    if(target===imageUrlInput||target===descriptionFilterInput)return;
    if(target&&target.closest&&target.closest("button,a,textarea,select,[contenteditable='true']"))return;
    if(target&&target.matches&&target.matches('input[type="file"],input[type="checkbox"],input[type="radio"]'))return;
    if(!queryId||selectedFace<0||busy)return;
    event.preventDefault();
    searchSimilar();
  });
  window.FaceSearchGroupCore={
    getQueryId:function(){return queryId;},
    getFaceCount:function(){return faces.length;},
    isBusy:function(){return busy;},
    postJson:postJson,
    setStatus:setStatus,
    setGroupAnalysisMatches:function(resultQueryId,profiles){
      if(!resultQueryId||resultQueryId!==queryId){
        groupAnalysisMatches=null;
        faceMatchResults=[];
        drawFaces();
        return;
      }
      const mapped=Array(faces.length).fill(null);
      (Array.isArray(profiles)?profiles:[]).forEach(function(profile){
        const index=Number(profile&&profile.face_index);
        if(!Number.isInteger(index)||index<0||index>=mapped.length)return;
        const queryGender=profile&&profile.query_gender&&typeof profile.query_gender==="object"
          ?profile.query_gender:{};
        const unmatched=Boolean(profile&&profile.unmatched);
        mapped[index]={
          name:unmatched?"Нет совпадения":String(profile.name||"").trim(),
          profileUrl:unmatched?"":safeLink(profile.profile_url||""),
          similarity:Number(profile&&profile.similarity||0),
          gender:String(queryGender.gender||"unknown"),
          genderConfidence:Number(queryGender.confidence||0),
          unmatched:unmatched
        };
      });
      groupAnalysisMatches={queryId:resultQueryId,matches:mapped};
      // Обновляем подписи сразу после выбора варианта группы; кнопка «Определить»
      // затем лишь подтверждает эти имена и не делает отдельный top_k=1 поиск.
      faceMatchResults=mapped.map(function(match){
        return match?Object.assign({},match):null;
      });
      drawFaces();
    }
  };
})();
</script>
<script src="/group-analysis.js"></script>
</body>
</html>
"""


class FaceSearchRequestHandler(BaseHTTPRequestHandler):
    server_version = "FaceSearchHTTP/1.0"

    def log_message(self, fmt, *args):
        # Успешные фоновые запросы браузера создают много шума, но не несут диагностической ценности.
        try:
            status_code = int(args[1]) if len(args) > 1 else None
            request_path = urlparse(getattr(self, "path", "")).path
            routine_get = (
                self.command == "GET"
                and (
                    request_path == "/api/status"
                    or (request_path.startswith("/api/face/")
                        and request_path.endswith(("/image", "/image-status")))
                )
            )
            if routine_get and status_code is not None and 200 <= status_code < 400:
                return
        except (TypeError, ValueError, IndexError):
            pass

        # Остальные запросы по-прежнему пишутся с локальным временем и IP TCP-клиента.
        timestamp = datetime.now().astimezone().isoformat(timespec="seconds")
        try:
            client_ip = str(self.client_address[0])
        except (IndexError, TypeError):
            client_ip = "unknown"
        print(f"[HTTP] {timestamp} IP={client_ip} " + (fmt % args), flush=True)

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
            ).replace(
                "__GROUP_MIN_SIMILARITY__", str(DEFAULT_GROUP_ANALYSIS_OPTIONS["min_similarity"])
            ).replace(
                "__GROUP_CANDIDATES_PER_FACE__", str(DEFAULT_GROUP_ANALYSIS_OPTIONS["candidates_per_face"])
            ).replace(
                "__GROUP_FRIEND_WEIGHT__", str(DEFAULT_GROUP_ANALYSIS_OPTIONS["friend_weight"])
            ).replace(
                "__GROUP_COMMON_FRIEND_WEIGHT__", str(DEFAULT_GROUP_ANALYSIS_OPTIONS["common_friend_weight"])
            ).replace(
                "__GROUP_CITY_WEIGHT__", str(DEFAULT_GROUP_ANALYSIS_OPTIONS["city_weight"])
            ).replace(
                "__GROUP_CHECK_VK_FRIENDS__", "checked" if DEFAULT_GROUP_ANALYSIS_OPTIONS["check_vk_friends"] else ""
            ).replace(
                "__GROUP_CHECK_GENDER__", "checked" if DEFAULT_GROUP_ANALYSIS_OPTIONS["check_gender"] else ""
            ).replace(
                "__GROUP_GENDER_WEIGHT__", str(DEFAULT_GROUP_ANALYSIS_OPTIONS["gender_weight"])
            ).replace(
                "__GROUP_PROFILE_GENDER_WEIGHT__", str(DEFAULT_GROUP_ANALYSIS_OPTIONS["profile_gender_weight"])
            )
            self._send(200, page.encode("utf-8"), "text/html; charset=utf-8")
            return
        if path == "/group-analysis.js":
            try:
                script = (APP_DIR / "group_analysis_ui.js").read_bytes()
            except OSError:
                self._send(404, b"// group analysis UI is unavailable", "application/javascript; charset=utf-8")
            else:
                self._send(200, script, "application/javascript; charset=utf-8")
            return
        if path == "/favicon.ico":
            self._send(204, b"", "image/x-icon")
            return
        if path.startswith("/api/face/") and path.endswith("/image-status"):
            self._serve_face_image_status(path)
            return
        if path.startswith("/api/face/") and path.endswith("/image"):
            query = parse_qs(urlparse(self.path).query)
            use_crop = query.get("crop", ["1"])[0] != "0"
            self._serve_face_image(path, use_crop=use_crop)
            return
        if path == "/api/group-analysis/status":
            query = parse_qs(urlparse(self.path).query)
            job_id = str(query.get("job_id", [""])[0] or "")
            query_id = str(query.get("query_id", [""])[0] or "")
            if not job_id or len(job_id) > 128:
                self._json(400, {"error": "Не указан идентификатор задачи анализа."})
                return
            with GROUP_ANALYSIS_JOBS_LOCK:
                job = GROUP_ANALYSIS_JOBS.get(job_id)
                if job is None or not secrets.compare_digest(str(job.get("query_id") or ""), query_id):
                    job = None
                else:
                    payload = {
                        "ok": True,
                        "job_id": job_id,
                        "state": job.get("state"),
                        "progress": int(job.get("progress") or 0),
                        "message": str(job.get("message") or ""),
                    }
                    if job.get("state") == "done":
                        payload["result"] = job.get("result")
                    if job.get("state") == "error":
                        payload["error"] = str(job.get("error") or "Ошибка группового анализа.")
            if job is None:
                self._json(404, {"error": "Задача анализа не найдена или сессия не совпадает."})
                return
            self._json(200, payload)
            return
        if path == "/api/status":
            payload = _index_status_payload()
            payload.update({
                "ok": True,
                "profile": PROFILE_NAME,
                "backend": BACKEND,
                "create_crops_on_html": CREATE_CROPS_ON_HTML,
                "crop_repair_active": bool(_CROP_REPAIR_ACTIVE_IDS),
            })
            self._json(200, payload)
            return
        self._json(404, {"error": "Страница не найдена."})

    def do_POST(self):
        path = urlparse(self.path).path
        if path not in ("/api/detect", "/api/search", "/api/load-image", "/api/group-analysis/start"):
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
            if path == "/api/group-analysis/start":
                self._start_group_analysis(data)
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

    def _start_group_analysis(self, data: dict):
        """Запускает анализ в отдельном потоке; HTTP-запрос сразу возвращает job_id."""
        token = str(data.get("query_id") or "")
        if not token or len(token) > 128:
            raise ValueError("Сессия запроса не найдена. Загрузите изображение ещё раз.")
        with QUERY_SESSIONS_LOCK:
            query = QUERY_SESSIONS.get(token)
            if query is None or time.monotonic() - query["created"] > QUERY_TTL_SECONDS:
                QUERY_SESSIONS.pop(token, None)
                raise ValueError("Сессия истекла. Определите лица на фотографии повторно.")
            embeddings = [embedding.copy() for embedding in query.get("embeddings", [])]
            query_genders = [dict(value) for value in query.get("gender_predictions", [])]
        if len(embeddings) < 2:
            raise ValueError("Для группового анализа нужно обнаружить не менее двух лиц.")

        options = group_analysis.normalize_options(data.get("options"))
        with GROUP_ANALYSIS_JOBS_LOCK:
            for existing_id, job in GROUP_ANALYSIS_JOBS.items():
                if (
                    job.get("query_id") == token
                    and job.get("state") in ("queued", "running")
                ):
                    self._json(409, {
                        "error": "Для этой фотографии групповой анализ уже выполняется.",
                        "job_id": existing_id,
                    })
                    return
            active_jobs = sum(
                1 for job in GROUP_ANALYSIS_JOBS.values()
                if job.get("state") in ("queued", "running")
            )
            if active_jobs >= MAX_ACTIVE_GROUP_ANALYSES:
                self._json(429, {
                    "error": (
                        "Уже выполняются две задачи группового анализа. "
                        "Повторите запуск после завершения одной из них."
                    )
                })
                return
            job_id = secrets.token_urlsafe(18)
            GROUP_ANALYSIS_JOBS[job_id] = {
                "created": time.monotonic(),
                "query_id": token,
                "state": "queued",
                "progress": 0,
                "message": "Задача поставлена в очередь.",
                "result": None,
                "error": "",
            }
        _prune_group_analysis_jobs()

        def update_job(progress, message, state=None):
            with GROUP_ANALYSIS_JOBS_LOCK:
                job = GROUP_ANALYSIS_JOBS.get(job_id)
                if job is None:
                    return
                job["progress"] = max(0, min(100, int(progress)))
                job["message"] = str(message or "")
                if state:
                    job["state"] = state

        def worker():
            update_job(1, "Подготовка поисковых кандидатов…", "running")
            connection = None
            try:
                candidate_count = int(options["candidates_per_face"])
                # Поиск FAISS сначала возвращает отдельные строки лиц, включая локальные
                # изображения без интернет-профиля. Расширяем top-k, пока не наберём
                # требуемое число уникальных интернет-профилей или пока не закончатся
                # результаты выше минимального порога сходства.
                search_top_k = min(500, max(candidate_count * 40, candidate_count + 50))
                max_search_top_k = min(10000, max(search_top_k, candidate_count * 400))
                candidates_by_face = []

                def online_profile_count(search_results):
                    unique_profiles = set()
                    for result in search_results:
                        raw = {}
                        try:
                            decoded = json.loads(str(result.get("raw_json") or ""))
                            if isinstance(decoded, dict):
                                raw = decoded
                        except (TypeError, ValueError):
                            pass
                        url = str(result.get("profile_url") or raw.get("url_source") or "").strip()
                        try:
                            user_id = int(result.get("vk_user_id") or 0)
                        except (TypeError, ValueError):
                            user_id = 0
                        if user_id > 0:
                            unique_profiles.add("vk:" + str(user_id))
                            continue
                        if not url:
                            continue
                        try:
                            parsed = urlparse(url if "://" in url else "https://" + url)
                            host = (parsed.hostname or "").strip(".").lower()
                            if parsed.scheme.lower() not in ("http", "https") or not host:
                                continue
                            if host in {"localhost", "localhost.localdomain"} or host.endswith(".local"):
                                continue
                        except (TypeError, ValueError):
                            continue
                        try:
                            profile_id = int(result.get("profile_id") or 0)
                        except (TypeError, ValueError):
                            profile_id = 0
                        if profile_id > 0:
                            unique_profiles.add("profile:" + str(profile_id))
                        else:
                            unique_profiles.add((host + parsed.path.rstrip("/")).lower())
                    return len(unique_profiles)
                connection = _open_connection()
                for face_index, embedding in enumerate(embeddings):
                    update_job(
                        5 + int(25 * face_index / max(1, len(embeddings))),
                        "Поиск кандидатов для лица " + str(face_index + 1)
                        + " / " + str(len(embeddings)) + "…",
                    )
                    while True:
                        with _INDEX_USE_LOCK.read_lock():
                            candidates = face_search.search_embedding(
                                connection,
                                embedding,
                                top_k=search_top_k,
                                min_similarity=float(options["min_similarity"]) / 100.0,
                                backend=BACKEND,
                            )
                        online_count = online_profile_count(candidates)
                        if (
                            online_count >= candidate_count
                            or len(candidates) < search_top_k
                            or search_top_k >= max_search_top_k
                        ):
                            break
                        search_top_k = min(max_search_top_k, search_top_k * 2)
                        update_job(
                            5 + int(25 * face_index / max(1, len(embeddings))),
                            "Для лица " + str(face_index + 1)
                            + ": расширяю поиск до " + str(search_top_k)
                            + " результатов, интернет-профилей пока " + str(online_count) + "…",
                        )
                    face_candidates = []
                    for candidate in candidates:
                        try:
                            score = max(0.0, min(100.0, float(candidate.get("score") or 0.0) * 100.0))
                            face_id = int(candidate.get("face_id") or 0)
                        except (TypeError, ValueError):
                            continue
                        raw_meta = {}
                        try:
                            raw_value = json.loads(str(candidate.get("raw_json") or ""))
                            if isinstance(raw_value, dict):
                                raw_meta = raw_value
                        except (TypeError, ValueError):
                            pass
                        vk_id = candidate.get("vk_user_id")
                        try:
                            vk_id = int(vk_id) if vk_id is not None and int(vk_id) > 0 else None
                        except (TypeError, ValueError):
                            vk_id = None
                        profile_sex = candidate.get("sex")
                        if profile_sex in (None, "", 0, "0"):
                            profile_sources = [raw_meta]
                            raw_info = raw_meta.get("info")
                            if isinstance(raw_info, dict):
                                profile_sources.append(raw_info)
                            for profile_source in profile_sources:
                                for field_name in ("sex", "gender", "Пол", "пол", "profile_gender"):
                                    value = profile_source.get(field_name)
                                    if value not in (None, "") and str(value).strip() not in ("", "0"):
                                        profile_sex = value
                                        break
                                if profile_sex not in (None, "", 0, "0"):
                                    break
                        profile_url = str(
                            candidate.get("profile_url")
                            or raw_meta.get("url_source")
                            or ""
                        ).strip()
                        if not profile_url and vk_id:
                            profile_url = "https://vk.com/id" + str(vk_id)
                        full_name = " ".join(
                            part for part in (
                                str(candidate.get("first_name") or "").strip(),
                                str(candidate.get("last_name") or "").strip(),
                            ) if part
                        ).strip()
                        if not full_name:
                            full_name = str(
                                candidate.get("screen_name")
                                or candidate.get("domain")
                                or profile_url
                                or ("Профиль #" + str(face_id))
                            ).strip()
                        face_candidates.append({
                            "profile_id": candidate.get("profile_id"),
                            "face_id": face_id,
                            "name": full_name,
                            "profile_url": profile_url,
                            "source_url": str(candidate.get("source_url") or ""),
                            "city": str(candidate.get("city_title") or "").strip(),
                            "path": str(candidate.get("path") or ""),
                            "similarity": score,
                            "vk_user_id": vk_id,
                            "profile_sex": profile_sex,
                        })
                    candidates_by_face.append(face_candidates)
                    update_job(
                        5 + int(25 * (face_index + 1) / max(1, len(embeddings))),
                        "Поиск кандидатов завершён для лица "
                        + str(face_index + 1) + " / " + str(len(embeddings)),
                    )
                connection.close()
                connection = None

                def progress_callback(percent, message):
                    update_job(30 + int(max(0, min(100, int(percent))) * 0.7), message)

                result = group_analysis.analyze_group(
                    candidates_by_face,
                    options,
                    APP_DIR,
                    progress=progress_callback,
                    query_genders=query_genders,
                    gender_classifier=_classify_candidate_face_gender,
                )
                # Сначала публикуем результат и только затем помечаем задачу завершённой,
                # чтобы клиент не успел получить state=done без поля result.
                with GROUP_ANALYSIS_JOBS_LOCK:
                    job = GROUP_ANALYSIS_JOBS.get(job_id)
                    if job is not None:
                        job["result"] = result
                        job["progress"] = 100
                        job["message"] = "Групповой анализ завершён."
                        job["state"] = "done"
            except Exception as exc:
                message = str(exc).strip() or exc.__class__.__name__
                print("[HTTP][GROUP-ANALYSIS][ERROR] " + message, flush=True)
                with GROUP_ANALYSIS_JOBS_LOCK:
                    job = GROUP_ANALYSIS_JOBS.get(job_id)
                    if job is not None:
                        job["error"] = message[:1000]
                        job["progress"] = 100
                        job["message"] = "Анализ завершился с ошибкой."
                        job["state"] = "error"
            finally:
                if connection is not None:
                    try:
                        connection.close()
                    except Exception:
                        pass

        threading.Thread(
            target=worker,
            name="FaceSearch-GroupAnalysis-" + job_id[:8],
            daemon=True,
        ).start()
        self._json(202, {"ok": True, "job_id": job_id, "state": "queued"})

    def _detect(self, data: dict):
        image = _decode_image(data.get("image", ""))
        found = face_search._detect_faces_with_padding(
            FACE_APP, image, padding_ratio=0.40
        )
        faces = []
        embeddings = []
        gender_predictions = []
        for face in found:
            if float(face.det_score) < face_search.DET_THRESHOLD:
                continue
            embedding = face_search.np.asarray(
                face.normed_embedding, dtype=face_search.np.float32
            ).copy()
            if float(face_search.np.linalg.norm(embedding)) <= 0:
                continue
            bbox = [float(value) for value in face.bbox.tolist()]
            gender_prediction = _predict_face_gender(image, face)
            gender_predictions.append(gender_prediction)
            embeddings.append(embedding)
            faces.append({
                "index": len(faces),
                "bbox": bbox,
                "confidence": float(face.det_score),
                "gender": gender_prediction.get("gender", "unknown"),
                "gender_confidence": float(gender_prediction.get("confidence") or 0.0),
            })

        token = secrets.token_urlsafe(24)
        with QUERY_SESSIONS_LOCK:
            QUERY_SESSIONS[token] = {
                "created": time.monotonic(),
                "embeddings": embeddings,
                "gender_predictions": gender_predictions,
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
        text_filter = str(data.get("text_filter") or "").strip()
        if len(text_filter) > MAX_CARD_TEXT_FILTER_LENGTH:
            raise ValueError(
                f"Длина текстового фильтра не должна превышать {MAX_CARD_TEXT_FILTER_LENGTH} символов."
            )
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
            candidate_top_k = candidate_count_for_text_filter(top_k, text_filter)
            with _INDEX_USE_LOCK.read_lock():
                current_signature = _index_signature()
                with _INDEX_STATE_LOCK:
                    active_signature = _INDEX_ACTIVE_SIGNATURE
                    reload_state = _INDEX_RELOAD_STATE
                # При CPU backend движок умеет лениво перечитывать файл. Не даём ему
                # читать индекс, который ещё меняется; дождёмся стабильной версии и
                # проверим её в фоновом потоке. При ошибке используем сохранённый кэш.
                if (
                    current_signature != active_signature
                    and reload_state not in ("error", "missing", "fallback")
                    and (BACKEND == face_search.BACKEND_CPU
                         or face_search.get_gpu_search_status().get("mode") == "none")
                ):
                    self._json(
                        503,
                        {"error": "Файл FAISS обновляется. Повторите поиск после завершения обновления индекса."},
                    )
                    return
                results = face_search.search_embedding(
                    conn,
                    embedding,
                    top_k=candidate_top_k,
                    min_similarity=minimum_percent / 100.0,
                    backend=BACKEND,
                )
            if text_filter:
                results = filter_results_by_card_text(
                    results, text_filter, limit=top_k
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
            is_per_image_metadata = (
                set(metadata_doc.keys()) == {"url_source", "name", "url_image", "info"}
                and isinstance(metadata_doc.get("info"), dict)
            )
            if is_per_image_metadata:
                metadata_info = metadata_doc["info"]
                metadata_name = str(metadata_doc.get("name") or "")
            else:
                metadata_info = metadata_doc
                metadata_name = str(metadata_doc.get("filename") or "")
            if source_type == "import":
                # Instagram's raw_json belongs to the whole account/profile; use
                # images.metadata_json when it contains the metadata for this post.
                if is_per_image_metadata:
                    profile_info = metadata_info
                    display_name = metadata_name or display_name
                    source_url = str(metadata_doc.get("url_source") or source_url)
                    url_image = str(metadata_doc.get("url_image") or url_image).strip()
                if not display_name:
                    display_name = "Без имени"
            else:
                display_name = ""

            profile_first_name = str(result.get("first_name") or "").strip()
            profile_last_name = str(result.get("last_name") or "").strip()
            profile_name = " ".join(
                part for part in (profile_first_name, profile_last_name) if part
            ).strip()
            if not profile_name:
                profile_name = str(
                    result.get("screen_name") or result.get("domain") or raw_meta.get("name") or ""
                ).strip()
            profile_url = str(result.get("profile_url") or "").strip()

            cards.append({
                "rank": int(result.get("rank") or len(cards) + 1),
                "face_id": face_id,
                "filename": str(result.get("filename") or ""),
                "similarity": round(float(result.get("score") or 0.0) * 100.0, 2),
                "source_url": source_url,
                "profile_name": profile_name,
                "profile_url": profile_url,
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

    def _serve_face_image_status(self, route: str):
        """Сообщает браузеру, ждать ли CROP или показать заглушку «Нет изображения»."""
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
            self._json(404, {"state": "missing", "error": "Изображение не найдено."})
            return

        image_id, stored_path, _filename, source_url, url_image = row
        crop_path = face_search._resolve_db_crop_path(
            stored_path, image_id=int(image_id)
        )
        try:
            if crop_path.is_file() and crop_path.stat().st_size > 0:
                self._json(200, {"state": "ready"})
                return
        except OSError:
            pass

        if _original_image_available(source_url, url_image):
            # В веб-интерфейсе CROP создаётся по запросу независимо от настройки GUI.
            # Cooldown и список активных задач защищают от повторного запуска.
            _start_crop_repair([face_id], force=True)
            self._json(200, {"state": "creating"})
            return

        self._json(200, {"state": "missing"})

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
        if use_crop:
            if crop_path.is_file():
                try:
                    body = crop_path.read_bytes()
                    content_type = mimetypes.guess_type(str(crop_path))[0] or "image/jpeg"
                    self._send(200, body, content_type)
                    return
                except OSError:
                    pass
            if _original_image_available(source_url, url_image):
                # Просим браузер ждать готовности CROP; фоновая задача запускается
                # даже при выключенной настройке create_crops_on_html в основной форме.
                _start_crop_repair([face_id], force=True)
                self._json(404, {"error": "CROP ещё формируется."})
                return

        # Если CROP создать невозможно, показываем сохранённый оригинал.
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
    _INDEX_WATCH_STOP.clear()
    index_watcher = threading.Thread(
        target=_index_watcher_runner,
        name="FaceSearch-FAISS-Watcher",
        daemon=True,
    )
    index_watcher.start()
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
        _INDEX_WATCH_STOP.set()
        server.server_close()
        with QUERY_SESSIONS_LOCK:
            QUERY_SESSIONS.clear()


if __name__ == "__main__":
    main()
