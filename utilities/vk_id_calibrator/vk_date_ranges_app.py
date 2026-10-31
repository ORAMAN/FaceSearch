#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
VK ID Calibrator — настольное приложение для построения опорных точек
«ID пользователя → дата регистрации → год» и приблизительных диапазонов ID.

Зависимости: только стандартная библиотека Python 3.10+.
Запуск: python vk_date_ranges_app.py

ВНИМАНИЕ: используются недокументированные методы restore.getSettings и
restore.init. VK может изменить или ограничить их работу в любой момент.
Полученные диапазоны являются оценкой и зависят от предположения, что дата
регистрации в целом возрастает вместе с числовым ID.
"""

from __future__ import annotations

import csv
import json
import base64
import gzip
import os
import queue
import re
import subprocess
import sys
import tempfile
import threading
import time
import urllib.error
import urllib.parse
import urllib.request
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse
import tkinter as tk
from tkinter import filedialog, messagebox, ttk

APP_DIR = Path(__file__).resolve().parent
PROJECT_DIR = APP_DIR.parent.parent
if str(PROJECT_DIR) not in sys.path:
    sys.path.insert(0, str(PROJECT_DIR))

from facesearch.settings import load_module_settings, save_module_settings

_REQUIRED_CAPTCHA_HELPER_BUILD = "2026-10-31-persistent-browser-session-1"

def ensure_captcha_helper() -> Path:
    """Проверяет наличие совместимого постоянного браузерного helper-модуля."""
    helper = APP_DIR / "vk_captcha_playwright.py"
    try:
        head = helper.read_text(encoding="utf-8", errors="replace")[:4000]
    except OSError as exc:
        raise RuntimeError(f"Не удалось прочитать модуль браузерной сессии: {exc}") from exc
    if f'HELPER_BUILD = "{_REQUIRED_CAPTCHA_HELPER_BUILD}"' not in head:
        raise RuntimeError(
            "Модуль vk_captcha_playwright.py отсутствует или несовместим. "
            "Обновите оба файла приложения из одного релиза."
        )
    return helper

CACHE_FILE = APP_DIR / "vk_dates_cache.json"
APP_SETTINGS_FILE = APP_DIR / "vk_id_calibrator_settings.xml"
LEGACY_APP_SETTINGS_FILE = APP_DIR / "vk_calibrator_settings.json"
SESSION_FILE = APP_DIR / "vk_restore_session.json"
RESULTS_FILE = APP_DIR / "vk_calibration_results.json"
SINGLE_RESULTS_FILE = APP_DIR / "vk_single_id_history.json"
DEFAULT_API_URL = "https://api.vk.com/method/"
DEFAULT_BROWSER_PROFILE = str((APP_DIR / "browser_profile").resolve())
DEFAULT_BROWSER_EXECUTABLE = r"C:/Program Files/Google/Chrome/Application/chrome.exe"
MSK = timezone(timedelta(hours=3))


def default_app_settings() -> dict[str, Any]:
    """Значения формы по умолчанию для общего механизма настроек FaceSearch."""
    return {
        "max_id": "950000000",
        "points": "140",
        "pause": "2.0",
        "retries": "4",
        "api_url": DEFAULT_API_URL,
        "build_ranges": True,
        "browser_profile": DEFAULT_BROWSER_PROFILE,
        "browser_executable": DEFAULT_BROWSER_EXECUTABLE,
        "single_id": "",
        "single_use_cache": True,
        "geometry": "1160x820",
        "maximized": False,
    }


# ---------------------------------------------------------------------------
# Хранение данных
# ---------------------------------------------------------------------------

def read_json(path: Path, default: Any) -> Any:
    try:
        with path.open("r", encoding="utf-8") as f:
            return json.load(f)
    except (OSError, ValueError, TypeError):
        return default


def write_json(path: Path, value: Any) -> None:
    tmp = path.with_suffix(path.suffix + ".tmp")
    with tmp.open("w", encoding="utf-8") as f:
        json.dump(value, f, ensure_ascii=False, indent=2)
    tmp.replace(path)


@dataclass
class ProbeResult:
    uid: int
    timestamp: int | None
    status: str
    detail: str = ""
    from_cache: bool = False

    @property
    def year(self) -> int | None:
        if self.timestamp is None:
            return None
        return datetime.fromtimestamp(self.timestamp, MSK).year

    @property
    def date_text(self) -> str:
        if self.timestamp is None:
            return "—"
        return datetime.fromtimestamp(self.timestamp, MSK).strftime("%d.%m.%Y %H:%M")


class VKRestoreClient:
    """Клиент VK через один постоянный браузерный контекст Playwright."""

    def __init__(self, api_url: str, pause: float, retries: int,
                 log: Callable[[str], None], stop_event: threading.Event,
                 browser_profile: str, browser_executable: str):
        self.api_url = api_url.rstrip("/") + "/"
        self.pause = max(0.2, pause)
        self.retries = max(1, retries)
        self.log = log
        self.stop_event = stop_event
        self.browser_profile = browser_profile
        self.browser_executable = browser_executable

        self.session = read_json(SESSION_FILE, {})
        if not isinstance(self.session, dict):
            self.session = {}
        self.cache = read_json(CACHE_FILE, {})
        if not isinstance(self.cache, dict):
            self.cache = {}

        self._cache_lock = threading.Lock()
        self._session_lock = threading.Lock()
        self._rpc_lock = threading.Lock()
        self._process_lock = threading.Lock()
        self._helper_process: subprocess.Popen[str] | None = None
        self._helper_output: queue.Queue[str | None] = queue.Queue()
        self._helper_reader: threading.Thread | None = None
        self._helper_stderr_file = None
        self._post_captcha_pause = 0.0
        self._closed = False

    def _wait(self, seconds: float) -> None:
        if self.stop_event.wait(seconds):
            raise InterruptedError("Операция остановлена пользователем")

    def _wait_after_request(self) -> None:
        delay = max(self.pause, self._post_captcha_pause)
        self._post_captcha_pause = 0.0
        self._wait(delay)

    @staticmethod
    def _read_helper_stream(stream, output_queue: queue.Queue) -> None:
        try:
            if stream is not None:
                for line in stream:
                    output_queue.put(line)
        except (OSError, ValueError):
            pass
        finally:
            output_queue.put(None)

    def _ensure_helper(self) -> None:
        with self._process_lock:
            if self._closed:
                raise RuntimeError("Браузерная сессия уже закрыта.")
            if self._helper_process is not None and self._helper_process.poll() is None:
                return

            helper = ensure_captcha_helper()
            self._helper_output = queue.Queue()
            log_path = APP_DIR / "vk_browser_session.log"
            self._helper_stderr_file = log_path.open("a", encoding="utf-8", errors="replace")
            self.log("Запускаю постоянную браузерную сессию Playwright.")
            self.log("Все запросы restore.getSettings и restore.init будут выполняться из одного контекста Chrome.")
            try:
                process = subprocess.Popen(
                    [sys.executable, str(helper), "--server", self.browser_profile, self.browser_executable],
                    stdin=subprocess.PIPE,
                    stdout=subprocess.PIPE,
                    stderr=self._helper_stderr_file,
                    text=True,
                    encoding="utf-8",
                    errors="replace",
                    bufsize=1,
                    cwd=str(APP_DIR),
                    creationflags=getattr(subprocess, "CREATE_NO_WINDOW", 0),
                )
            except OSError as exc:
                try:
                    self._helper_stderr_file.close()
                except OSError:
                    pass
                self._helper_stderr_file = None
                raise RuntimeError(
                    f"Не удалось запустить постоянную браузерную сессию: {type(exc).__name__}: {exc}"
                ) from exc

            self._helper_process = process
            output_queue = self._helper_output
            self._helper_reader = threading.Thread(
                target=self._read_helper_stream,
                args=(process.stdout, output_queue),
                name="VKBrowserSessionReader",
                daemon=True,
            )
            self._helper_reader.start()

    def _terminate_helper(self) -> None:
        with self._process_lock:
            process = self._helper_process
            self._helper_process = None
            if process is not None:
                try:
                    if process.poll() is None:
                        process.terminate()
                        process.wait(timeout=3)
                except (OSError, subprocess.TimeoutExpired):
                    try:
                        process.kill()
                        process.wait(timeout=2)
                    except (OSError, subprocess.TimeoutExpired):
                        pass
                for stream in (process.stdin, process.stdout):
                    if stream is not None:
                        try:
                            stream.close()
                        except OSError:
                            pass
            if self._helper_stderr_file is not None:
                try:
                    self._helper_stderr_file.close()
                except OSError:
                    pass
                self._helper_stderr_file = None

    def _browser_request(self, method: str, params: dict[str, Any]) -> dict[str, Any]:
        """Отправляет RPC постоянному браузеру; сетевой запрос выполняется только через fetch."""
        with self._rpc_lock:
            self._ensure_helper()
            process = self._helper_process
            output_queue = self._helper_output
            if process is None or process.stdin is None:
                raise RuntimeError("Постоянный браузер не запущен.")

            command = {"cmd": "request", "method": method, "url": self.api_url + method, "params": params}
            try:
                process.stdin.write(json.dumps(command, ensure_ascii=False) + "\n")
                process.stdin.flush()
            except (OSError, BrokenPipeError) as exc:
                self._terminate_helper()
                raise RuntimeError("Потеряно соединение с процессом браузерной сессии.") from exc

            deadline = time.monotonic() + 930.0
            while True:
                if self.stop_event.wait(0.2):
                    self._terminate_helper()
                    raise InterruptedError("Операция остановлена пользователем")
                if time.monotonic() >= deadline:
                    self._terminate_helper()
                    raise RuntimeError("Истекло время ожидания ответа браузерной сессии (15 минут).")
                try:
                    line = output_queue.get(timeout=0.2)
                except queue.Empty:
                    if process.poll() is not None:
                        self._terminate_helper()
                        raise RuntimeError(
                            f"Процесс браузерной сессии завершился с кодом {process.returncode}. "
                            f"Диагностика: {APP_DIR / 'vk_browser_session.log'}"
                        )
                    continue

                if line is None:
                    self._terminate_helper()
                    raise RuntimeError(
                        "Процесс браузерной сессии закрыл канал связи. "
                        f"Диагностика: {APP_DIR / 'vk_browser_session.log'}"
                    )
                try:
                    result = json.loads(line)
                except (TypeError, ValueError) as exc:
                    self._terminate_helper()
                    raise RuntimeError("Браузерная сессия вернула некорректный служебный ответ.") from exc

                if not isinstance(result, dict) or not result.get("ok"):
                    detail = str(result.get("error") if isinstance(result, dict) else "неизвестная ошибка")
                    raise RuntimeError(detail or "Ошибка браузерной сессии.")

                self._post_captcha_pause = (
                    max(self.pause, 5.0) if result.get("captcha_performed") else 0.0
                )
                response = result.get("response")
                if not isinstance(response, dict):
                    raise RuntimeError("VK вернул ответ неожиданного формата.")
                return response

    def get_session(self, force: bool = False) -> dict[str, Any]:
        with self._session_lock:
            if not force and self.session.get("restore_session_id"):
                return self.session

            response = self._browser_request("restore.getSettings", {
                "type": "restore",
                "vkui": 1,
                "platform": "vkcom",
                "access_token": "",
                "v": "5.83",
            })
            data = response.get("response") or {}
            if not data.get("restore_session_id"):
                error = response.get("error") or {}
                raise RuntimeError(
                    "restore.getSettings не вернул restore_session_id: "
                    + str(error.get("error_msg") or response)
                )
            self.session = {
                "restore_session_id": data["restore_session_id"],
                "unauth_id": data.get("unauth_id", ""),
                "saved_at": int(time.time()),
            }
            write_json(SESSION_FILE, self.session)
            return self.session

    @staticmethod
    def _parse_date(response: dict[str, Any]) -> int | None:
        value = response.get("response")
        data: Any = None
        if isinstance(value, list) and len(value) > 1:
            data = value[1]
        elif isinstance(value, dict):
            data = value
        if not isinstance(data, dict):
            return None
        user = data.get("user")
        raw = user.get("date_created") if isinstance(user, dict) else data.get("date_created")
        try:
            stamp = int(raw)
            return stamp if stamp > 0 else None
        except (TypeError, ValueError):
            return None

    def get_date(self, uid: int, use_cache: bool = True) -> ProbeResult:
        key = str(uid)
        if use_cache:
            with self._cache_lock:
                cached = self.cache.get(key)
            if cached:
                try:
                    return ProbeResult(uid, int(cached), "ok", from_cache=True)
                except (TypeError, ValueError):
                    pass

        last_error = ""
        for attempt in range(self.retries):
            if self.stop_event.is_set():
                raise InterruptedError("Операция остановлена пользователем")
            try:
                session = self.get_session()
                request_params = {
                    "uid": uid,
                    "access_token": "",
                    "restore_session_id": session["restore_session_id"],
                    "unauth_id": session.get("unauth_id", ""),
                    "v": "5.223",
                }
                # При необходимости helper сам откроет CAPTCHA, дождётся ручной
                # проверки и повторит этот же запрос в том же браузерном контексте.
                response = self._browser_request("restore.init", request_params)
                error = response.get("error")
                if error:
                    code = error.get("error_code")
                    msg = str(error.get("error_msg", "ошибка API"))
                    if code in (15, 100) or "not found" in msg.lower() or "не найден" in msg.lower():
                        self._wait_after_request()
                        return ProbeResult(uid, None, "unavailable", f"API {code}: {msg}")
                    if code == 14:
                        raise RuntimeError(
                            "VK повторно запросил CAPTCHA после проверки. "
                            "Калибровка приостановлена для этого ID; проверьте журнал браузерной сессии."
                        )
                    last_error = f"API {code}: {msg}"
                    if attempt == 0:
                        self.get_session(force=True)
                    self._wait(min(3, attempt + 1))
                    continue

                stamp = self._parse_date(response)
                if stamp is None:
                    last_error = f"Неожиданный ответ: {str(response)[:250]}"
                    self._wait_after_request()
                    continue

                with self._cache_lock:
                    self.cache[key] = stamp
                    write_json(CACHE_FILE, self.cache)
                self._wait_after_request()
                return ProbeResult(uid, stamp, "ok")
            except InterruptedError:
                raise
            except Exception as exc:
                last_error = str(exc)
                if "CAPTCHA" in last_error or "captcha" in last_error.lower():
                    return ProbeResult(uid, None, "error", last_error)
                if attempt + 1 < self.retries:
                    self.log(f"ID {uid:,}: {last_error}. Повтор {attempt + 1}/{self.retries}.")
                    self._wait(min(4, attempt + 1))

        return ProbeResult(uid, None, "error", last_error or "Не удалось получить дату")

    def close(self) -> None:
        """Корректно закрывает постоянный браузер при выходе из приложения."""
        self._closed = True
        if not self._rpc_lock.acquire(timeout=3):
            self._terminate_helper()
            return
        try:
            process = self._helper_process
            if process is None:
                return
            try:
                if process.poll() is None and process.stdin is not None:
                    process.stdin.write(json.dumps({"cmd": "shutdown"}) + "\n")
                    process.stdin.flush()
                    try:
                        self._helper_output.get(timeout=8)
                    except queue.Empty:
                        pass
                    process.wait(timeout=8)
            except (OSError, subprocess.TimeoutExpired):
                self._terminate_helper()
            finally:
                if self._helper_process is not None:
                    self._terminate_helper()
        finally:
            self._rpc_lock.release()


# ---------------------------------------------------------------------------
# Калибровка
# ---------------------------------------------------------------------------

class CalibrationWorker:
    def __init__(self, config: dict[str, Any], events: queue.Queue,
                 stop_event: threading.Event, client: VKRestoreClient | None = None):
        self.config = config
        self.events = events
        self.stop_event = stop_event
        self.client = client or VKRestoreClient(
            config["api_url"], config["pause"], config["retries"],
            lambda message: self.emit("log", message), stop_event, config["browser_profile"], config["browser_executable"],
        )
        self.anchors: list[ProbeResult] = []
        self.ranges: list[dict[str, Any]] = []
        self.previous_results = read_json(RESULTS_FILE, {})
        if not isinstance(self.previous_results, dict):
            self.previous_results = {}
        saved_plan = self.previous_results.get("calibration", {})
        self.saved_calibration = saved_plan if isinstance(saved_plan, dict) else {}
        self.calibration_state: dict[str, Any] = {}
        self.saved_anchors: dict[int, ProbeResult] = {}
        anchor_priority = {"error": 0, "unavailable": 1, "ok": 2}
        for item in self.previous_results.get("anchors", []):
            if not isinstance(item, dict):
                continue
            try:
                uid = int(item["id"])
                status = str(item.get("status", "ok"))
                raw_timestamp = item.get("timestamp")
                timestamp = int(raw_timestamp) if raw_timestamp is not None else None
                if uid < 1 or status not in {"ok", "unavailable", "error"}:
                    continue
                if status == "ok" and timestamp is None:
                    continue
                candidate = ProbeResult(uid, timestamp, status, str(item.get("detail", "")))
                current = self.saved_anchors.get(uid)
                if current is None or anchor_priority.get(status, 0) > anchor_priority.get(current.status, 0):
                    self.saved_anchors[uid] = candidate
            except (KeyError, TypeError, ValueError, OverflowError):
                continue

    def emit(self, kind: str, data: Any = None) -> None:
        self.events.put((kind, data))

    def check_stop(self) -> None:
        if self.stop_event.is_set():
            raise InterruptedError("Операция остановлена пользователем")

    def find_top(self, max_id: int) -> ProbeResult:
        uid = max_id
        attempts = 0
        while uid >= 1:
            self.check_stop()
            attempts += 1
            self.emit("status", f"Поиск верхней опорной точки: проверяется ID {uid:,}")
            self.emit("log", f"Проверка верхней границы: ID {uid:,}")
            result = self.client.get_date(uid)
            if result.status == "ok":
                return result
            if result.status == "error":
                raise RuntimeError(f"Не удалось проверить верхнюю границу ID {uid:,}: {result.detail}")
            uid = int(uid * 0.85)
            if attempts > 100:
                break
        raise RuntimeError("Не удалось найти доступный ID. Проверьте работу restore.init и адрес API.")

    def build_anchors(self, top: ProbeResult, target_ids: list[int]) -> None:
        """Продолжает сохранённую сетку, запрашивая только ещё не полученные ID."""
        ordered_ids = sorted({int(uid) for uid in target_ids if int(uid) > 0} | {top.uid})
        completed: dict[int, ProbeResult] = {}
        for uid in ordered_ids:
            if uid == top.uid:
                completed[uid] = top
                continue
            saved = self.saved_anchors.get(uid)
            # Успешные ответы и подтверждённое отсутствие профиля уже обработаны.
            # Ошибки остаются в очереди для повторной попытки.
            if saved is not None and saved.status in {"ok", "unavailable"}:
                completed[uid] = saved

        self.anchors = sorted(completed.values(), key=lambda item: item.uid)
        total = len(ordered_ids)
        self.emit("progress_init", total)
        ready = len(completed)
        remaining = total - ready
        if remaining:
            self.emit("log", f"Продолжение калибровки: уже получено {ready} из {total} опорных точек; осталось {remaining}.")
        else:
            self.emit("log", f"Все {total} опорных точек уже сохранены. Повторные запросы пропущены.")

        # Фиксируем восстановленные точки и верхнюю границу до новых запросов.
        for item in self.anchors:
            self.emit("anchor", item)
        self.save_results()
        self.emit("progress", (ready, total, f"Восстановлено точек: {ready}/{total}"))

        processed = set(completed)
        for index, uid in enumerate(ordered_ids, 1):
            self.check_stop()
            if uid in processed:
                self.emit("progress", (index, total, f"Опорные точки: {index}/{total} (уже сохранено)"))
                continue

            result = self.client.get_date(uid)
            self.anchors.append(result)
            self.anchors.sort(key=lambda item: item.uid)
            processed.add(uid)
            self.emit("anchor", result)

            # Сохраняем каждую точку сразу: аварийное закрытие не сбросит прогресс.
            self.save_results()
            if result.status == "error":
                self.emit("log", f"ID {uid:,}: ошибка запроса — {result.detail}")
            elif result.status != "ok":
                self.emit("log", f"ID {uid:,}: дата недоступна ({result.detail})")
            else:
                self.emit("log", f"ID {uid:,} → {result.date_text} (год {result.year})" + (" [кэш]" if result.from_cache else ""))
            self.emit("progress", (index, total, f"Опорные точки: {index}/{total}"))

    @staticmethod
    def anchors_monotonic(anchors: list[ProbeResult]) -> tuple[bool, str]:
        valid = [item for item in anchors if item.timestamp is not None]
        problems = []
        for left, right in zip(valid, valid[1:]):
            if right.timestamp < left.timestamp:
                problems.append(f"{left.uid:,} ({left.date_text}) → {right.uid:,} ({right.date_text})")
                if len(problems) == 3:
                    break
        return not problems, "; ".join(problems)

    def probe_near(self, middle: int, low: int, high: int) -> ProbeResult | None:
        """При дырке проверяем соседние ID, не делая вывод по пустому ответу."""
        offsets = (1, -1, 2, -2, 4, -4, 8, -8, 16, -16)
        for offset in offsets:
            candidate = middle + offset
            if low < candidate < high:
                self.check_stop()
                result = self.client.get_date(candidate)
                if result.status == "ok":
                    self.emit("log", f"ID {middle:,} недоступен; использована соседняя точка {candidate:,}")
                    return result
                if result.status == "error":
                    raise RuntimeError(f"Ошибка при проверке ID {candidate:,}: {result.detail}")
        return None

    def find_year_boundary(self, year: int, anchors: list[ProbeResult]) -> tuple[int | None, str]:
        target = int(datetime(year, 1, 1, tzinfo=MSK).timestamp())
        valid = sorted((a for a in anchors if a.timestamp is not None), key=lambda a: a.uid)
        before = [a for a in valid if a.timestamp < target]
        after = [a for a in valid if a.timestamp >= target]
        if not after:
            return None, "нет верхней опорной точки"
        low = before[-1].uid if before else 0
        high = after[0].uid
        if high <= low:
            return None, "не удалось сформировать интервал"

        used_neighbour = False
        max_steps = max(1, (high - low).bit_length() + 4)
        for _ in range(max_steps):
            self.check_stop()
            if high - low <= 1:
                # Если в календарном году не было регистраций, бинарный поиск
                # упрётся в первую точку следующего года. Проверяем саму дату.
                end_probe = self.client.get_date(high)
                if end_probe.status != "ok":
                    return None, f"не удалось подтвердить дату граничного ID {high:,}"
                if end_probe.year != year:
                    return None, f"нет подтверждённой регистрации в {year} году; ближайшая дата — {end_probe.date_text}"
                return high, "уточнено бинарным поиском" + (" с обходом пропусков" if used_neighbour else "")
            middle = (low + high) // 2
            result = self.client.get_date(middle)
            if result.status != "ok":
                result = self.probe_near(middle, low, high)
                if result is None:
                    return None, f"нет данных около ID {middle:,}; граница не подтверждена"
                used_neighbour = True
            if result.timestamp >= target:
                high = result.uid
            else:
                low = result.uid
        return None, "достигнут предел уточнения"

    def build_ranges(self) -> None:
        valid = [a for a in self.anchors if a.timestamp is not None]
        if len(valid) < 2:
            self.emit("log", "Недостаточно успешных опорных точек для расчёта диапазонов.")
            return
        monotonic, details = self.anchors_monotonic(self.anchors)
        if not monotonic:
            self.emit("warning", "Даты опорных точек не монотонны по ID. Диапазоны не рассчитывались. Примеры: " + details)
            return

        years = range(min(a.year for a in valid), max(a.year for a in valid) + 1)
        years = list(years)
        self.emit("progress_init", len(years))
        boundaries: dict[int, int] = {}
        statuses: dict[int, str] = {}
        for index, year in enumerate(years, 1):
            self.check_stop()
            self.emit("status", f"Поиск первой регистрации за {year} год")
            boundary, info = self.find_year_boundary(year, self.anchors)
            statuses[year] = info
            if boundary is not None:
                boundaries[year] = boundary
                self.emit("log", f"{year}: первый ID ≈ {boundary:,} ({info})")
            else:
                self.emit("log", f"{year}: диапазон не определён — {info}")
            self.emit("progress", (index, len(years), f"Границы годов: {index}/{len(years)}"))

        sorted_bounds = sorted(boundaries.items())
        for i, (year, start_id) in enumerate(sorted_bounds):
            next_start = sorted_bounds[i + 1][1] if i + 1 < len(sorted_bounds) else max(a.uid for a in valid) + 1
            row = {
                "year": year,
                "id_from": start_id,
                "id_to": max(start_id, next_start - 1),
                "status": statuses.get(year, ""),
            }
            self.ranges.append(row)
            self.emit("range", row)

    def run(self) -> None:
        try:
            max_id = int(self.config["max_id"])
            count = int(self.config["points"])
            api_url = str(self.config.get("api_url") or DEFAULT_API_URL)
            self.emit("status", "Получение сессии VK…")
            self.client.get_session()
            self.emit("log", "Сессия VK получена.")

            # Для возобновления используем прежнюю сетку. Иначе новый верхний
            # ID сместил бы интервалы и заставил повторять уже пройденные точки.
            plan = self.saved_calibration
            try:
                same_plan = (
                    int(plan.get("max_id")) == max_id
                    and int(plan.get("points")) == count
                    and str(plan.get("api_url") or DEFAULT_API_URL) == api_url
                    and isinstance(plan.get("target_ids"), list)
                    and bool(plan.get("target_ids"))
                )
            except (TypeError, ValueError):
                same_plan = False

            top: ProbeResult | None = None
            target_ids: list[int] = []
            if same_plan:
                try:
                    saved_ids = sorted({int(value) for value in plan["target_ids"] if int(value) > 0})
                    saved_top_id = int(plan.get("top_id") or 0)
                    candidate = self.saved_anchors.get(saved_top_id)
                    if candidate is not None and candidate.status == "ok" and candidate.timestamp is not None:
                        top = candidate
                    elif saved_top_id > 0:
                        candidate = self.client.get_date(saved_top_id)
                        if candidate.status == "ok":
                            top = candidate
                    if top is not None and saved_ids:
                        target_ids = sorted(set(saved_ids) | {top.uid})
                        self.emit("log", f"Возобновляю прежнюю сетку опорных точек (верхний ID {top.uid:,}; запрошено {len(target_ids)} точек).")
                except (TypeError, ValueError):
                    top = None
                    target_ids = []

            if top is None:
                self.emit("status", "Поиск верхнего существующего ID…")
                top = self.find_top(max_id)
                target_ids = {
                    max(1, int(round(1 + (top.uid - 1) * i / max(1, count - 1))))
                    for i in range(count)
                }
                target_ids.add(top.uid)
                target_ids = sorted(target_ids)
                self.emit("log", f"Верхняя опорная точка: ID {top.uid:,} → {top.date_text}")
                self.emit("log", f"Создана новая сетка из {len(target_ids)} опорных точек.")

            self.calibration_state = {
                "max_id": max_id,
                "points": count,
                "api_url": api_url,
                "top_id": top.uid,
                "target_ids": target_ids,
                "updated_at": datetime.now().isoformat(timespec="seconds"),
            }
            self.emit("top", top)
            self.save_results()
            self.build_anchors(top, target_ids)
            if self.config["build_ranges"]:
                self.build_ranges()
            self.save_results()
            self.emit("done", {"anchors": len(self.anchors), "ranges": len(self.ranges), "stopped": False})
        except InterruptedError:
            self.save_results()
            self.emit("done", {"anchors": len(self.anchors), "ranges": len(self.ranges), "stopped": True})
        except Exception as exc:
            self.save_results()
            self.emit("fatal", str(exc))

    def save_results(self) -> None:
        data = {
            "saved_at": datetime.now().isoformat(timespec="seconds"),
            "timezone": "Europe/Moscow (UTC+03:00)",
            "anchors": [
                {"id": a.uid, "timestamp": a.timestamp, "date": a.date_text,
                 "year": a.year, "status": a.status, "detail": a.detail}
                for a in self.anchors
            ],
            "ranges": self.ranges,
        }
        previous = read_json(RESULTS_FILE, {})
        if not isinstance(previous, dict):
            previous = {}
        if self.calibration_state:
            data["calibration"] = dict(self.calibration_state)
        elif isinstance(previous.get("calibration"), dict):
            data["calibration"] = previous["calibration"]

        def merge_rows(old_rows: Any, new_rows: list[dict[str, Any]],
                       key_fields: tuple[str, ...]) -> list[dict[str, Any]]:
            merged: list[dict[str, Any]] = []
            seen: set[tuple[str, ...]] = set()
            old = old_rows if isinstance(old_rows, list) else []
            for item in [*old, *new_rows]:
                if not isinstance(item, dict):
                    continue
                key = tuple(
                    json.dumps(item.get(field), ensure_ascii=False, sort_keys=True)
                    for field in key_fields
                )
                if key in seen:
                    continue
                seen.add(key)
                merged.append(item)
            return merged

        # Не заменяем предыдущую калибровку: сохраняем все разные варианты
        # опорных точек и диапазонов, добавляя только действительно новые записи.
        data["anchors"] = merge_rows(
            previous.get("anchors", []), data["anchors"],
            ("id", "timestamp", "status", "detail"),
        )
        data["ranges"] = merge_rows(
            previous.get("ranges", []), data["ranges"],
            ("year", "id_from", "id_to", "status"),
        )
        for key, value in previous.items():
            if key not in data:
                data[key] = value
        write_json(RESULTS_FILE, data)


class SingleProbeWorker:
    """Асинхронно получает дату регистрации одного заданного VK ID."""

    def __init__(self, uid: int, use_cache: bool, client: VKRestoreClient,
                 events: queue.Queue, stop_event: threading.Event):
        self.uid = uid
        self.use_cache = use_cache
        self.client = client
        self.events = events
        self.stop_event = stop_event

    def emit(self, kind: str, data: Any = None) -> None:
        self.events.put((kind, data))

    def run(self) -> None:
        try:
            self.emit("status", f"Получение даты регистрации для ID {self.uid:,}…")
            result = self.client.get_date(self.uid, use_cache=self.use_cache)
            self.emit("single_result", result)
            self.emit("single_done", {"stopped": False})
        except InterruptedError:
            self.emit("single_done", {"stopped": True})
        except Exception as exc:
            self.emit("single_failure", str(exc))
            self.emit("single_done", {"stopped": False})


# ---------------------------------------------------------------------------
# Интерфейс
# ---------------------------------------------------------------------------

class VKCalibratorApp(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title("VK ID Calibrator — опорные ID и годы регистрации")
        self.minsize(930, 650)
        self._settings_after_id = None
        self._settings_defaults = default_app_settings()
        self._saved_settings = load_module_settings(
            APP_SETTINGS_FILE,
            "vk_id_calibrator",
            self._settings_defaults,
            window_keys={"geometry", "maximized"},
            legacy_json_paths=(LEGACY_APP_SETTINGS_FILE,),
        )
        saved_geometry = str(self._saved_settings.get("geometry") or "1160x820")
        if re.fullmatch(r"\d+x\d+(?:[+-]\d+[+-]\d+)?", saved_geometry):
            self.geometry(saved_geometry)
            self._last_normal_geometry = saved_geometry
        else:
            self.geometry("1160x820")
            self._last_normal_geometry = self.geometry()
        self._restore_maximized = bool(self._saved_settings.get("maximized", False))

        self.events: queue.Queue = queue.Queue()
        self.stop_event = threading.Event()
        self.worker_thread: threading.Thread | None = None
        self.worker: CalibrationWorker | SingleProbeWorker | None = None
        self.current_task: str | None = None
        self.shared_client: VKRestoreClient | None = None
        self.shared_client_signature: tuple[str, str, str] | None = None
        self.anchor_count = 0
        self.range_count = 0
        self._anchor_rows_seen: set[tuple[Any, ...]] = set()
        self._range_rows_seen: set[tuple[Any, ...]] = set()
        self.single_history: list[dict[str, Any]] = []

        self.max_id_var = tk.StringVar(value=str(self._saved_settings["max_id"]))
        self.points_var = tk.StringVar(value=str(self._saved_settings["points"]))
        self.pause_var = tk.StringVar(value=str(self._saved_settings["pause"]))
        self.retries_var = tk.StringVar(value=str(self._saved_settings["retries"]))
        self.api_var = tk.StringVar(value=str(self._saved_settings["api_url"]))
        self.browser_profile_var = tk.StringVar(value=str(self._saved_settings["browser_profile"]))
        self.browser_executable_var = tk.StringVar(value=str(self._saved_settings["browser_executable"]))
        self.ranges_var = tk.BooleanVar(value=bool(self._saved_settings["build_ranges"]))
        self.single_id_var = tk.StringVar(value=str(self._saved_settings["single_id"]))
        self.single_use_cache_var = tk.BooleanVar(value=bool(self._saved_settings["single_use_cache"]))
        self.status_var = tk.StringVar(value="Готово к работе")
        self.progress_var = tk.DoubleVar(value=0)

        self._build_ui()
        self._load_previous_results()
        self._load_single_history()
        for variable in (
            self.max_id_var, self.points_var, self.pause_var, self.retries_var,
            self.api_var, self.browser_profile_var, self.browser_executable_var,
            self.ranges_var, self.single_id_var, self.single_use_cache_var,
        ):
            variable.trace_add("write", self.schedule_settings_save)
        self.bind("<Configure>", self._on_configure, add="+")
        self.after(180, self._restore_window_state)
        self.after(100, self._poll_events)
        self.protocol("WM_DELETE_WINDOW", self._on_close)

    def _build_ui(self) -> None:
        style = ttk.Style(self)
        try:
            style.theme_use("vista")
        except tk.TclError:
            try:
                style.theme_use("clam")
            except tk.TclError:
                pass
        style.configure("Treeview", rowheight=25)
        style.configure("Treeview.Heading", font=("Segoe UI", 9, "bold"))

        root = ttk.Frame(self, padding=10)
        root.pack(fill="both", expand=True)

        settings = ttk.LabelFrame(root, text="Параметры калибровки", padding=8)
        settings.pack(fill="x")
        for col in range(10):
            settings.columnconfigure(col, weight=0)
        settings.columnconfigure(9, weight=1)

        ttk.Label(settings, text="Максимальный ID для поиска:").grid(row=0, column=0, sticky="w", padx=(0, 5), pady=3)
        self.max_id_entry = ttk.Entry(settings, textvariable=self.max_id_var, width=13)
        self.max_id_entry.grid(row=0, column=1, sticky="w", padx=(0, 14))
        self._attach_entry_clipboard(self.max_id_entry)
        ttk.Label(settings, text="Опорных точек:").grid(row=0, column=2, sticky="w", padx=(0, 5))
        ttk.Entry(settings, textvariable=self.points_var, width=7).grid(row=0, column=3, sticky="w", padx=(0, 14))
        ttk.Label(settings, text="Пауза между запросами, сек:").grid(row=0, column=4, sticky="w", padx=(0, 5))
        ttk.Entry(settings, textvariable=self.pause_var, width=6).grid(row=0, column=5, sticky="w", padx=(0, 14))
        ttk.Label(settings, text="Повторов:").grid(row=0, column=6, sticky="w", padx=(0, 5))
        ttk.Entry(settings, textvariable=self.retries_var, width=5).grid(row=0, column=7, sticky="w")

        ttk.Label(settings, text="Адрес API:").grid(row=1, column=0, sticky="w", padx=(0, 5), pady=(7, 2))
        ttk.Entry(settings, textvariable=self.api_var).grid(row=1, column=1, columnspan=7, sticky="ew", pady=(7, 2))
        ttk.Checkbutton(settings, text="После опорных точек рассчитать границы диапазонов по годам",
                        variable=self.ranges_var).grid(row=2, column=0, columnspan=8, sticky="w", pady=(7, 0))
        ttk.Label(settings, text="Профиль браузера CAPTCHA:").grid(row=3, column=0, sticky="w", padx=(0, 5), pady=(7, 2))
        ttk.Entry(settings, textvariable=self.browser_profile_var).grid(row=3, column=1, columnspan=6, sticky="ew", pady=(7, 2))
        ttk.Button(settings, text="Обзор…", command=self._choose_browser_profile).grid(row=3, column=7, sticky="w", padx=(6, 0), pady=(7, 2))
        ttk.Label(settings, text="Исполняемый файл браузера CAPTCHA:").grid(row=4, column=0, sticky="w", padx=(0, 5), pady=(7, 2))
        ttk.Entry(settings, textvariable=self.browser_executable_var).grid(row=4, column=1, columnspan=6, sticky="ew", pady=(7, 2))
        ttk.Button(settings, text="Обзор…", command=self._choose_browser_executable).grid(row=4, column=7, sticky="w", padx=(6, 0), pady=(7, 2))
        ttk.Label(settings, text="Профиль сохраняет cookies и вход VK; для браузера можно указать установленный Chrome или оставить поле пустым для Chromium Playwright.",
                  foreground="#666666").grid(row=5, column=1, columnspan=7, sticky="w", pady=(0, 2))

        single_frame = ttk.LabelFrame(root, text="Получить дату регистрации одного ID", padding=7)
        single_frame.pack(fill="x", pady=(7, 0))
        ttk.Label(single_frame, text="VK ID:").pack(side="left")
        self.single_id_entry = ttk.Entry(single_frame, textvariable=self.single_id_var, width=18)
        self.single_id_entry.pack(side="left", padx=(6, 8))
        self._attach_entry_clipboard(self.single_id_entry)
        self.single_lookup_button = ttk.Button(single_frame, text="Получить дату", command=self.start_single_lookup)
        self.single_lookup_button.pack(side="left")
        ttk.Checkbutton(single_frame, text="Использовать кэш (если есть)",
                        variable=self.single_use_cache_var).pack(side="left", padx=(12, 0))
        self.single_id_entry.bind("<Return>", lambda _event: self.start_single_lookup())

        buttons = ttk.Frame(root)
        buttons.pack(fill="x", pady=(9, 7))
        self.start_button = ttk.Button(buttons, text="▶ Начать калибровку", command=self.start_calibration)
        self.start_button.pack(side="left")
        self.stop_button = ttk.Button(buttons, text="■ Остановить", command=self.stop_calibration, state="disabled")
        self.stop_button.pack(side="left", padx=(7, 0))
        ttk.Button(buttons, text="Экспорт опорных ID…", command=self.export_anchors).pack(side="left", padx=(18, 0))
        ttk.Button(buttons, text="Экспорт диапазонов…", command=self.export_ranges).pack(side="left", padx=(7, 0))
        ttk.Button(buttons, text="Открыть папку результатов", command=self.open_results_folder).pack(side="right")

        progress_frame = ttk.Frame(root)
        progress_frame.pack(fill="x", pady=(0, 8))
        ttk.Progressbar(progress_frame, variable=self.progress_var, maximum=100).pack(fill="x")
        ttk.Label(progress_frame, textvariable=self.status_var).pack(anchor="w", pady=(3, 0))

        self.notebook = ttk.Notebook(root)
        self.notebook.pack(fill="both", expand=True)
        anchors_tab = ttk.Frame(self.notebook, padding=5)
        ranges_tab = ttk.Frame(self.notebook, padding=5)
        log_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(anchors_tab, text="Опорные ID и годы")
        self.notebook.add(ranges_tab, text="Диапазоны по годам")

        self.anchor_tree = self._make_tree(anchors_tab, (
            ("id", "ID пользователя", 145, "e"),
            ("date", "Дата регистрации (МСК)", 190, "center"),
            ("year", "Год", 75, "center"),
            ("status", "Результат", 135, "center"),
            ("detail", "Комментарий", 430, "w"),
        ))
        self._attach_tree_copy_menu(self.anchor_tree, (0,))
        self.range_tree = self._make_tree(ranges_tab, (
            ("year", "Год", 85, "center"),
            ("from", "ID от", 170, "e"),
            ("to", "ID до", 170, "e"),
            ("status", "Способ / статус определения", 500, "w"),
        ))
        self._attach_tree_copy_menu(self.range_tree, (1, 2))

        single_tab = ttk.Frame(self.notebook, padding=5)
        self.notebook.add(single_tab, text="Одиночные ID")
        self.single_tree = self._make_tree(single_tab, (
            ("id", "ID пользователя", 130, "e"),
            ("date", "Дата регистрации (МСК)", 180, "center"),
            ("year", "Год", 65, "center"),
            ("status", "Результат", 110, "center"),
            ("source", "Источник", 110, "center"),
            ("checked", "Проверено", 145, "center"),
            ("detail", "Комментарий", 330, "w"),
        ))
        self._attach_tree_copy_menu(self.single_tree, (0,))
        self.single_tab = single_tab
        self.notebook.add(log_tab, text="Журнал")

        log_controls = ttk.Frame(log_tab)
        log_controls.pack(fill="x", pady=(0, 4))
        ttk.Button(log_controls, text="Копировать весь журнал", command=self.copy_log_all).pack(side="left")
        ttk.Button(log_controls, text="Копировать выделенное", command=self.copy_log_selection).pack(side="left", padx=(6, 0))
        ttk.Button(log_controls, text="Сохранить журнал…", command=self.save_log).pack(side="left", padx=(6, 0))
        ttk.Button(log_controls, text="Очистить журнал", command=self.clear_log).pack(side="right")
        self.log_text = tk.Text(log_tab, wrap="word", height=12, font=("Consolas", 9), state="disabled",
                                undo=False, exportselection=True)
        log_scroll = ttk.Scrollbar(log_tab, orient="vertical", command=self.log_text.yview)
        self.log_text.configure(yscrollcommand=log_scroll.set)
        self.log_text.pack(side="left", fill="both", expand=True)
        log_scroll.pack(side="right", fill="y")
        self._build_log_context_menu()

        note = ttk.Label(root, text=(
            "При checkbox-CAPTCHA откроется отдельное окно Chrome без DevTools: отметьте «Я не робот» вручную. "
            "Для окна проверки требуется Playwright/Chromium. В полях ID работают Ctrl+C/Ctrl+V и меню по правой кнопке; "
            "в таблицах Ctrl+C копирует ID выбранной строки. В журнале: Ctrl+C — выделенное, "
            "Ctrl+Shift+C — всё, Ctrl+A — выделить всё; правый клик открывает меню. "
            "restore.getSettings / restore.init — недокументированные методы VK; диапазоны являются оценкой."
        ), foreground="#8a5a00", wraplength=1100)
        note.pack(fill="x", pady=(8, 0))

    @staticmethod
    def _make_tree(parent: ttk.Frame, columns: tuple) -> ttk.Treeview:
        keys = [column[0] for column in columns]
        tree = ttk.Treeview(parent, columns=keys, show="headings", selectmode="browse")
        for key, heading, width, anchor in columns:
            tree.heading(key, text=heading)
            tree.column(key, width=width, minwidth=55, anchor=anchor, stretch=True)
        y_scroll = ttk.Scrollbar(parent, orient="vertical", command=tree.yview)
        x_scroll = ttk.Scrollbar(parent, orient="horizontal", command=tree.xview)
        tree.configure(yscrollcommand=y_scroll.set, xscrollcommand=x_scroll.set)
        parent.rowconfigure(0, weight=1)
        parent.columnconfigure(0, weight=1)
        tree.grid(row=0, column=0, sticky="nsew")
        y_scroll.grid(row=0, column=1, sticky="ns")
        x_scroll.grid(row=1, column=0, sticky="ew")
        return tree

    def _log(self, message: str) -> None:
        stamp = datetime.now().strftime("%H:%M:%S")
        self.log_text.configure(state="normal")
        self.log_text.insert("end", f"[{stamp}] {message}\n")
        self.log_text.see("end")
        self.log_text.configure(state="disabled")

    def clear_log(self) -> None:
        self.log_text.configure(state="normal")
        self.log_text.delete("1.0", "end")
        self.log_text.configure(state="disabled")

    def _attach_entry_clipboard(self, entry: ttk.Entry) -> None:
        """Добавляет явные сочетания клавиш и контекстное меню буфера обмена."""
        if not hasattr(self, "entry_context_menu"):
            self.entry_context_menu = tk.Menu(self, tearoff=False)
            self.entry_context_menu.add_command(
                label="Вырезать", command=lambda: self._entry_menu_action("<<Cut>>"))
            self.entry_context_menu.add_command(
                label="Копировать", command=lambda: self._entry_menu_action("<<Copy>>"))
            self.entry_context_menu.add_command(
                label="Вставить", command=lambda: self._entry_menu_action("<<Paste>>"))
            self.entry_context_menu.add_separator()
            self.entry_context_menu.add_command(
                label="Выделить всё", command=self._select_all_entry_text)

        entry.bind("<Button-3>", self._show_entry_context_menu, add="+")
        entry.bind("<Control-c>", lambda event: self._entry_virtual_event(event, "<<Copy>>"), add="+")
        entry.bind("<Control-C>", lambda event: self._entry_virtual_event(event, "<<Copy>>"), add="+")
        entry.bind("<Control-v>", lambda event: self._entry_virtual_event(event, "<<Paste>>"), add="+")
        entry.bind("<Control-V>", lambda event: self._entry_virtual_event(event, "<<Paste>>"), add="+")
        entry.bind("<Control-x>", lambda event: self._entry_virtual_event(event, "<<Cut>>"), add="+")
        entry.bind("<Control-X>", lambda event: self._entry_virtual_event(event, "<<Cut>>"), add="+")
        entry.bind("<Control-a>", self._select_all_entry_shortcut, add="+")
        entry.bind("<Control-A>", self._select_all_entry_shortcut, add="+")
        entry.bind("<Control-Insert>", lambda event: self._entry_virtual_event(event, "<<Copy>>"), add="+")
        entry.bind("<Shift-Insert>", lambda event: self._entry_virtual_event(event, "<<Paste>>"), add="+")
        entry.bind("<Shift-Delete>", lambda event: self._entry_virtual_event(event, "<<Cut>>"), add="+")

    @staticmethod
    def _entry_virtual_event(event: tk.Event, virtual_event: str) -> str:
        try:
            event.widget.event_generate(virtual_event)
        except tk.TclError:
            pass
        return "break"

    def _select_all_entry_shortcut(self, event: tk.Event | None = None) -> str:
        self._select_all_entry_text(event.widget if event is not None else None)
        return "break"

    def _select_all_entry_text(self, entry=None) -> None:
        entry = entry or getattr(self, "_context_entry", None) or self.focus_get()
        if entry is None:
            return
        try:
            entry.selection_range(0, "end")
            entry.icursor("end")
            entry.focus_set()
        except (tk.TclError, AttributeError):
            pass

    def _entry_menu_action(self, virtual_event: str) -> None:
        entry = getattr(self, "_context_entry", None) or self.focus_get()
        if entry is None:
            return
        try:
            entry.focus_set()
            entry.event_generate(virtual_event)
        except (tk.TclError, AttributeError):
            pass

    def _show_entry_context_menu(self, event: tk.Event) -> str:
        self._context_entry = event.widget
        try:
            self.entry_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.entry_context_menu.grab_release()
        return "break"

    def _attach_tree_copy_menu(self, tree: ttk.Treeview, id_columns: tuple[int, ...]) -> None:
        """Включает копирование ID из строки таблицы по Ctrl+C и правому клику."""
        if not hasattr(self, "tree_context_menu"):
            self.tree_context_menu = tk.Menu(self, tearoff=False)
            self.tree_context_menu.add_command(
                label="Копировать ID", command=self._copy_context_tree_ids)
            self.tree_context_menu.add_command(
                label="Копировать всю строку", command=self._copy_context_tree_row)

        tree.bind(
            "<Control-c>",
            lambda event, target=tree, columns=id_columns: self._copy_tree_ids(target, columns),
            add="+",
        )
        tree.bind(
            "<Control-C>",
            lambda event, target=tree, columns=id_columns: self._copy_tree_ids(target, columns),
            add="+",
        )
        tree.bind(
            "<Button-3>",
            lambda event, target=tree, columns=id_columns: self._show_tree_context_menu(
                event, target, columns),
            add="+",
        )

    def _show_tree_context_menu(
        self, event: tk.Event, tree: ttk.Treeview, id_columns: tuple[int, ...]
    ) -> str:
        item = tree.identify_row(event.y)
        if item:
            tree.selection_set(item)
            tree.focus(item)
        self._context_tree = tree
        self._context_tree_item = item or (tree.selection()[0] if tree.selection() else "")
        self._context_tree_id_columns = id_columns
        try:
            self.tree_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.tree_context_menu.grab_release()
        return "break"

    def _copy_tree_ids(self, tree: ttk.Treeview, id_columns: tuple[int, ...]) -> str:
        selection = tree.selection()
        item = selection[0] if selection else tree.focus()
        if not item:
            return "break"
        values = tree.item(item, "values")
        ids = [
            re.sub(r"[\s,]", "", str(values[index]))
            for index in id_columns
            if index < len(values) and str(values[index]).strip()
        ]
        if ids:
            self.clipboard_clear()
            self.clipboard_append("\t".join(ids))
            self.update_idletasks()
            self.status_var.set("ID скопирован в буфер обмена.")
        return "break"

    def _copy_context_tree_ids(self) -> None:
        tree = getattr(self, "_context_tree", None)
        if tree is not None:
            self._copy_tree_ids(tree, self._context_tree_id_columns)

    def _copy_context_tree_row(self) -> None:
        tree = getattr(self, "_context_tree", None)
        item = getattr(self, "_context_tree_item", "")
        if tree is None or not item:
            return
        values = tree.item(item, "values")
        if values:
            self.clipboard_clear()
            self.clipboard_append("\t".join(str(value) for value in values))
            self.update_idletasks()
            self.status_var.set("Строка таблицы скопирована в буфер обмена.")

    def _build_log_context_menu(self) -> None:
        self.log_context_menu = tk.Menu(self, tearoff=False)
        self.log_context_menu.add_command(label="Копировать выделенное", command=self.copy_log_selection)
        self.log_context_menu.add_command(label="Копировать весь журнал", command=self.copy_log_all)
        self.log_context_menu.add_command(label="Выделить всё", command=self.select_all_log)
        self.log_context_menu.add_separator()
        self.log_context_menu.add_command(label="Сохранить журнал…", command=self.save_log)
        self.log_context_menu.add_command(label="Очистить журнал", command=self.clear_log)
        self.log_text.bind("<Button-3>", self._show_log_context_menu)
        self.log_text.bind("<Control-c>", self._copy_log_shortcut)
        self.log_text.bind("<Control-C>", self._copy_log_shortcut)
        self.log_text.bind("<Control-a>", self._select_all_log_shortcut)
        self.log_text.bind("<Control-A>", self._select_all_log_shortcut)
        self.log_text.bind("<Control-Shift-c>", self._copy_all_log_shortcut)
        self.log_text.bind("<Control-Shift-C>", self._copy_all_log_shortcut)
        self.log_text.bind("<Shift-F10>", self._show_log_context_menu_keyboard)

    def _show_log_context_menu(self, event: tk.Event) -> str:
        try:
            self.log_context_menu.tk_popup(event.x_root, event.y_root)
        finally:
            self.log_context_menu.grab_release()
        return "break"

    def _show_log_context_menu_keyboard(self, event: tk.Event) -> str:
        try:
            box = self.log_text.bbox("insert") or (8, 8, 0, 0)
            self.log_context_menu.tk_popup(self.log_text.winfo_rootx() + box[0],
                                           self.log_text.winfo_rooty() + box[1])
        finally:
            self.log_context_menu.grab_release()
        return "break"

    def _copy_log_shortcut(self, event: tk.Event | None = None) -> str:
        self.copy_log_selection()
        return "break"

    def _copy_all_log_shortcut(self, event: tk.Event | None = None) -> str:
        self.copy_log_all()
        return "break"

    def _select_all_log_shortcut(self, event: tk.Event | None = None) -> str:
        self.select_all_log()
        return "break"

    def select_all_log(self) -> None:
        self.log_text.tag_remove("sel", "1.0", "end")
        self.log_text.tag_add("sel", "1.0", "end-1c")
        self.log_text.mark_set("insert", "end-1c")
        self.log_text.see("insert")
        self.log_text.focus_set()

    def copy_log_selection(self) -> None:
        try:
            selected = self.log_text.get("sel.first", "sel.last")
        except tk.TclError:
            self.status_var.set("Нет выделенного текста. Используйте «Копировать весь журнал».")
            return
        self.clipboard_clear()
        self.clipboard_append(selected)
        self.update_idletasks()
        self.status_var.set("Выделенный фрагмент журнала скопирован в буфер обмена.")

    def copy_log_all(self) -> None:
        content = self.log_text.get("1.0", "end-1c")
        if not content:
            self.status_var.set("Журнал пуст.")
            return
        self.clipboard_clear()
        self.clipboard_append(content)
        self.update_idletasks()
        self.status_var.set("Весь журнал скопирован в буфер обмена.")

    def save_log(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Сохранить журнал", defaultextension=".txt",
            initialfile="vk_calibrator_log.txt",
            filetypes=[("Текстовые файлы", "*.txt"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        try:
            Path(path).write_text(self.log_text.get("1.0", "end-1c"), encoding="utf-8-sig")
            self.status_var.set(f"Журнал сохранён: {path}")
        except OSError as exc:
            messagebox.showerror("Ошибка сохранения журнала", str(exc), parent=self)

    def _choose_browser_profile(self) -> None:
        current = self.browser_profile_var.get().strip()
        selected = filedialog.askdirectory(parent=self, title="Выберите папку постоянного профиля Chromium",
                                           initialdir=current if Path(current).is_dir() else str(APP_DIR))
        if selected:
            self.browser_profile_var.set(selected)

    def _choose_browser_executable(self) -> None:
        current = self.browser_executable_var.get().strip()
        selected = filedialog.askopenfilename(
            parent=self, title="Выберите исполняемый файл браузера",
            initialdir=str(Path(current).parent) if current and Path(current).parent.is_dir() else r"C:\Program Files\Google\Chrome\Application",
            filetypes=[("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")]
        )
        if selected:
            self.browser_executable_var.set(selected)

    def _validate_runtime_config(self) -> dict[str, Any] | None:
        """Проверяет общие настройки соединения для калибровки и одиночного запроса."""
        try:
            pause = float(self.pause_var.get().replace(",", "."))
            retries = int(self.retries_var.get())
            api_url = self.api_var.get().strip()
            browser_profile = self.browser_profile_var.get().strip()
            browser_executable = self.browser_executable_var.get().strip()
            if not browser_profile:
                raise ValueError("Укажите папку профиля браузера для CAPTCHA.")
            browser_profile = str(Path(browser_profile).expanduser().resolve())
            if browser_executable:
                browser_executable = str(Path(browser_executable).expanduser().resolve())
                if not Path(browser_executable).is_file():
                    raise ValueError(f"Файл браузера не найден: {browser_executable}")
                if Path(browser_executable).suffix.lower() != ".exe":
                    raise ValueError("Укажите путь именно к исполняемому файлу .exe")
            if pause < 0.2:
                raise ValueError("Пауза не может быть меньше 0,2 секунды.")
            if not 1 <= retries <= 10:
                raise ValueError("Количество повторов должно быть от 1 до 10.")
            if not api_url.startswith(("https://", "http://")):
                raise ValueError("Адрес API должен начинаться с https:// или http://.")
            if not api_url.endswith("/"):
                api_url += "/"
            return {"pause": pause, "retries": retries, "api_url": api_url,
                    "browser_profile": browser_profile,
                    "browser_executable": browser_executable}
        except ValueError as exc:
            messagebox.showerror("Проверьте параметры", str(exc), parent=self)
            return None

    def _validate_config(self) -> dict[str, Any] | None:
        common = self._validate_runtime_config()
        if common is None:
            return None
        try:
            max_id = int(self.max_id_var.get().replace(",", "").replace(" ", ""))
            points = int(self.points_var.get())
            if max_id < 10:
                raise ValueError("Максимальный ID должен быть не меньше 10.")
            if not 2 <= points <= 5000:
                raise ValueError("Количество опорных точек должно быть от 2 до 5000.")
            return {**common, "max_id": max_id, "points": points,
                    "build_ranges": bool(self.ranges_var.get())}
        except ValueError as exc:
            messagebox.showerror("Проверьте параметры", str(exc), parent=self)
            return None

    def _get_shared_client(self, config: dict[str, Any]) -> VKRestoreClient:
        """Повторно использует cookie-контекст после CAPTCHA между разными задачами."""
        signature = (config["api_url"], config["browser_profile"], config["browser_executable"])
        if self.shared_client is None or self.shared_client_signature != signature:
            if self.shared_client is not None:
                self.shared_client.close()
            self.shared_client = VKRestoreClient(
                config["api_url"], config["pause"], config["retries"],
                lambda message: self.events.put(("log", message)),
                self.stop_event, config["browser_profile"], config["browser_executable"],
            )
            self.shared_client_signature = signature
        else:
            self.shared_client.pause = max(0.2, config["pause"])
            self.shared_client.retries = max(1, config["retries"])
        return self.shared_client

    def start_calibration(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        config = self._validate_config()
        if config is None:
            return
        self._save_settings()
        # Существующие строки таблиц не очищаем: новые результаты дополняют историю.
        self.progress_var.set(0)
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.single_lookup_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.current_task = "calibration"
        self._log("Запуск калибровки. Кэшированные даты повторно запрашиваться не будут.")
        self._log("При CAPTCHA откроется Chrome без отладочных инструментов. Пройдите проверку вручную; контекст VK будет использоваться последующими запросами.")
        self._log(f"Каталог профиля браузера: {config['browser_profile']}")
        self._log(f"Исполняемый файл браузера: {config['browser_executable'] or 'Chromium Playwright (автовыбор)'}")
        client = self._get_shared_client(config)
        self.worker = CalibrationWorker(config, self.events, self.stop_event, client=client)
        self.worker_thread = threading.Thread(target=self.worker.run, name="VKCalibrator", daemon=True)
        self.worker_thread.start()

    def start_single_lookup(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            return
        raw_uid = self.single_id_var.get().strip().replace(",", "").replace(" ", "")
        try:
            uid = int(raw_uid)
            if uid < 1 or uid > 2_147_483_647:
                raise ValueError
        except ValueError:
            messagebox.showerror("Некорректный VK ID", "Введите целочисленный VK ID от 1 до 2 147 483 647.", parent=self)
            self.single_id_entry.focus_set()
            self.single_id_entry.selection_range(0, "end")
            return

        config = self._validate_runtime_config()
        if config is None:
            return
        self._save_settings()
        self.stop_event.clear()
        self.start_button.configure(state="disabled")
        self.single_lookup_button.configure(state="disabled")
        self.stop_button.configure(state="normal")
        self.current_task = "single"
        self.progress_var.set(0)
        use_cache = bool(self.single_use_cache_var.get())
        self._log(f"Одиночная проверка ID {uid:,}.")
        self._log("Источник: кэш (при наличии)." if use_cache else "Кэш отключён: запрашиваю дату непосредственно у VK.")
        self._log(f"Каталог профиля браузера: {config['browser_profile']}")
        client = self._get_shared_client(config)
        self.worker = SingleProbeWorker(uid, use_cache, client, self.events, self.stop_event)
        self.worker_thread = threading.Thread(target=self.worker.run, name="VKSingleProbe", daemon=True)
        self.worker_thread.start()

    def stop_calibration(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            self.stop_event.set()
            self.stop_button.configure(state="disabled")
            self.status_var.set("Останавливаю после текущего сетевого запроса…")
            if self.current_task == "single":
                self._log("Получена команда остановки одиночной проверки.")
            else:
                self._log("Получена команда остановки калибровки.")

    def _poll_events(self) -> None:
        try:
            while True:
                kind, data = self.events.get_nowait()
                if kind == "log":
                    self._log(str(data))
                elif kind == "status":
                    self.status_var.set(str(data))
                elif kind == "progress_init":
                    self.progress_var.set(0)
                elif kind == "progress":
                    current, total, text_value = data
                    self.progress_var.set(current / max(1, total) * 100)
                    self.status_var.set(text_value)
                elif kind == "top":
                    self._log(f"Верхняя граница: ID {data.uid:,}, год {data.year}.")
                elif kind == "anchor":
                    self._insert_anchor(data)
                elif kind == "range":
                    self._insert_range(data)
                elif kind == "single_result":
                    self._insert_single_result(data)
                elif kind == "single_failure":
                    self._log("ОШИБКА одиночного запроса: " + str(data))
                    self.status_var.set("Одиночная проверка завершилась с ошибкой")
                elif kind == "single_done":
                    if data.get("stopped"):
                        self.status_var.set("Одиночная проверка остановлена")
                        self._log("Одиночная проверка остановлена пользователем.")
                    self._finish_worker()
                elif kind == "warning":
                    self._log("ПРЕДУПРЕЖДЕНИЕ: " + str(data))
                    messagebox.showwarning("Проверка монотонности", str(data), parent=self)
                elif kind == "fatal":
                    self._log("ОШИБКА: " + str(data))
                    self.status_var.set("Калибровка завершилась с ошибкой")
                    messagebox.showerror("Ошибка калибровки", str(data), parent=self)
                    self._finish_worker()
                elif kind == "done":
                    if data.get("stopped"):
                        self.status_var.set("Остановлено. Частичные результаты сохранены.")
                        self._log("Калибровка остановлена; частичные результаты сохранены.")
                    else:
                        self.status_var.set("Калибровка завершена")
                        self._log(f"Готово. Опорных точек: {data['anchors']}; диапазонов: {data['ranges']}.")
                    self._finish_worker()
        except queue.Empty:
            pass
        self.after(100, self._poll_events)

    def _finish_worker(self) -> None:
        self.start_button.configure(state="normal")
        self.single_lookup_button.configure(state="normal")
        self.stop_button.configure(state="disabled")
        self.worker_thread = None
        self.current_task = None

    @staticmethod
    def _scroll_to_latest(tree: ttk.Treeview, item_id: str) -> None:
        # see() гарантирует видимость конкретной строки; moveto(1.0) дополнительно
        # прижимает список к самому низу, когда записей уже больше высоты окна.
        tree.see(item_id)
        tree.yview_moveto(1.0)

    def _insert_anchor(self, item: ProbeResult) -> None:
        row_key = (item.uid, item.timestamp, item.status, item.detail)
        if row_key in self._anchor_rows_seen:
            return
        self._anchor_rows_seen.add(row_key)
        result_label = "Получена" if item.status == "ok" else ("Недоступна" if item.status == "unavailable" else "Ошибка")
        iid = self.anchor_tree.insert("", "end", values=(
            f"{item.uid:,}", item.date_text,
            item.year if item.year is not None else "—",
            result_label, item.detail or ("Из кэша" if item.from_cache else ""),
        ))
        self._scroll_to_latest(self.anchor_tree, iid)
        self.anchor_count += 1

    def _insert_range(self, item: dict[str, Any]) -> None:
        row_key = (item.get("year"), item.get("id_from"), item.get("id_to"), item.get("status", ""))
        if row_key in self._range_rows_seen:
            return
        self._range_rows_seen.add(row_key)
        iid = self.range_tree.insert("", "end", values=(
            item["year"], f"{item['id_from']:,}", f"{item['id_to']:,}", item["status"]
        ))
        self._scroll_to_latest(self.range_tree, iid)
        self.range_count += 1

    @staticmethod
    def _single_status_label(status: str) -> str:
        return "Получена" if status == "ok" else ("Недоступна" if status == "unavailable" else "Ошибка")

    def _insert_single_history_row(self, item: dict[str, Any]) -> None:
        timestamp = item.get("timestamp")
        try:
            date_text = datetime.fromtimestamp(int(timestamp), MSK).strftime("%d.%m.%Y %H:%M") if timestamp else "—"
        except (TypeError, ValueError, OSError, OverflowError):
            date_text = "—"
        year = item.get("year") or "—"
        source = "Кэш" if item.get("from_cache") else "VK API"
        iid = self.single_tree.insert("", "end", values=(
            f"{int(item.get('id', 0)):,}", date_text, year,
            self._single_status_label(str(item.get("status", "error"))),
            source, item.get("checked_at", ""), item.get("detail", ""),
        ))
        self._scroll_to_latest(self.single_tree, iid)

    def _insert_single_result(self, result: ProbeResult) -> None:
        entry = {
            "id": result.uid,
            "timestamp": result.timestamp,
            "year": result.year,
            "status": result.status,
            "detail": result.detail,
            "from_cache": result.from_cache,
            "checked_at": datetime.now().strftime("%d.%m.%Y %H:%M:%S"),
        }
        self.single_history.append(entry)
        self.single_history = self.single_history[-2000:]
        try:
            write_json(SINGLE_RESULTS_FILE, self.single_history)
        except OSError as exc:
            self._log(f"Не удалось сохранить историю одиночных ID: {exc}")
        self._insert_single_history_row(entry)
        self.notebook.select(self.single_tab)
        if result.status == "ok":
            source = " [кэш]" if result.from_cache else ""
            message = f"ID {result.uid:,} → {result.date_text} (год {result.year}){source}"
            self._log(message)
            self.status_var.set(f"Дата регистрации ID {result.uid:,}: {result.date_text}")
        elif result.status == "unavailable":
            message = f"ID {result.uid:,}: дата недоступна ({result.detail})"
            self._log(message)
            self.status_var.set(f"Для ID {result.uid:,} дата недоступна")
        else:
            message = f"ID {result.uid:,}: ошибка — {result.detail}"
            self._log(message)
            self.status_var.set("Одиночная проверка завершилась с ошибкой")

    def _load_single_history(self) -> None:
        data = read_json(SINGLE_RESULTS_FILE, [])
        if not isinstance(data, list):
            return
        self.single_history = [item for item in data if isinstance(item, dict)][-2000:]
        for item in self.single_history:
            try:
                self._insert_single_history_row(item)
            except (TypeError, ValueError, tk.TclError):
                continue
        if self.single_history:
            self._log(f"Загружена история одиночных проверок: {len(self.single_history)} записей.")

    def _load_previous_results(self) -> None:
        data = read_json(RESULTS_FILE, {})
        if not isinstance(data, dict):
            return
        for item in data.get("anchors", []):
            try:
                probe = ProbeResult(int(item["id"]),
                                    int(item["timestamp"]) if item.get("timestamp") is not None else None,
                                    str(item.get("status", "ok")), str(item.get("detail", "")))
                self._insert_anchor(probe)
            except (KeyError, TypeError, ValueError):
                continue
        for item in data.get("ranges", []):
            self._insert_range(item)
        if self.anchor_count or self.range_count:
            self._log(f"Загружены ранее сохранённые результаты от {data.get('saved_at', 'неизвестно')}.")

    def export_anchors(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Сохранить опорные ID", defaultextension=".csv",
            initialfile="vk_id_anchors.csv", filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        data = read_json(RESULTS_FILE, {})
        rows = data.get("anchors", []) if isinstance(data, dict) else []
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file, delimiter=";")
                writer.writerow(["ID", "Дата регистрации (МСК)", "Год", "Unix timestamp", "Статус", "Комментарий"])
                for item in rows:
                    writer.writerow([item.get("id"), item.get("date"), item.get("year"),
                                     item.get("timestamp"), item.get("status"), item.get("detail")])
            messagebox.showinfo("Экспорт", f"Опорные точки сохранены:\n{path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Ошибка записи", str(exc), parent=self)

    def export_ranges(self) -> None:
        path = filedialog.asksaveasfilename(
            parent=self, title="Сохранить диапазоны по годам", defaultextension=".csv",
            initialfile="vk_id_ranges.csv", filetypes=[("CSV", "*.csv"), ("Все файлы", "*.*")]
        )
        if not path:
            return
        data = read_json(RESULTS_FILE, {})
        rows = data.get("ranges", []) if isinstance(data, dict) else []
        try:
            with open(path, "w", newline="", encoding="utf-8-sig") as file:
                writer = csv.writer(file, delimiter=";")
                writer.writerow(["Год", "ID от", "ID до", "Статус определения"])
                for item in rows:
                    writer.writerow([item.get("year"), item.get("id_from"),
                                     item.get("id_to"), item.get("status")])
            messagebox.showinfo("Экспорт", f"Диапазоны сохранены:\n{path}", parent=self)
        except OSError as exc:
            messagebox.showerror("Ошибка записи", str(exc), parent=self)

    def open_results_folder(self) -> None:
        try:
            import os
            os.startfile(str(APP_DIR))
        except Exception as exc:
            messagebox.showerror("Не удалось открыть папку", str(exc), parent=self)

    def _restore_window_state(self) -> None:
        if self._restore_maximized:
            try:
                self.state("zoomed")
            except tk.TclError:
                pass
        self.schedule_settings_save()

    def _on_configure(self, event=None) -> None:
        if event is not None and event.widget is not self:
            return
        try:
            if self.state() == "normal":
                self._last_normal_geometry = self.geometry()
        except tk.TclError:
            pass
        self.schedule_settings_save()

    def schedule_settings_save(self, *_args) -> None:
        try:
            if self._settings_after_id is not None:
                self.after_cancel(self._settings_after_id)
            self._settings_after_id = self.after(400, self._save_settings)
        except tk.TclError:
            self._settings_after_id = None

    def _save_settings(self) -> None:
        self._settings_after_id = None
        try:
            state = self.state()
            maximized = state in {"zoomed", "maximized"}
            geometry = self.geometry() if state == "normal" else self._last_normal_geometry
            settings = {
                "max_id": self.max_id_var.get(),
                "points": self.points_var.get(),
                "pause": self.pause_var.get(),
                "retries": self.retries_var.get(),
                "api_url": self.api_var.get(),
                "build_ranges": bool(self.ranges_var.get()),
                "browser_profile": self.browser_profile_var.get(),
                "browser_executable": self.browser_executable_var.get(),
                "single_id": self.single_id_var.get(),
                "single_use_cache": bool(self.single_use_cache_var.get()),
                "geometry": str(geometry or "1160x820"),
                "maximized": maximized,
            }
            save_module_settings(
                APP_SETTINGS_FILE,
                "vk_id_calibrator",
                settings,
                window_keys={"geometry", "maximized"},
            )
            self._saved_settings = settings
        except (OSError, tk.TclError, AttributeError, TypeError):
            pass

    def _on_close(self) -> None:
        if self.worker_thread and self.worker_thread.is_alive():
            if not messagebox.askyesno("Выход", "Калибровка ещё выполняется. Остановить её и закрыть приложение?", parent=self):
                return
            self.stop_event.set()
        try:
            if self._settings_after_id is not None:
                self.after_cancel(self._settings_after_id)
                self._settings_after_id = None
            self._save_settings()
            if self.shared_client is not None:
                self.shared_client.close()
        finally:
            self.destroy()


def main() -> None:
    app = VKCalibratorApp()
    app.mainloop()


if __name__ == "__main__":
    main()
