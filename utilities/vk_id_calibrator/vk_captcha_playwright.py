#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""Постоянная браузерная сессия VK ID Calibrator.

Все restore.getSettings/restore.init выполняются через fetch из одного
persistent context Playwright. Ручная SmartCaptcha и повтор restore.init
происходят в том же браузере и с теми же cookies.
"""

from __future__ import annotations

HELPER_BUILD = "2026-10-31-persistent-browser-session-1"

import json
import sys
import time
from pathlib import Path
from typing import Any
from urllib.parse import parse_qs, urlparse

from playwright.sync_api import sync_playwright


def _write_message(data: dict[str, Any]) -> None:
    sys.stdout.write(json.dumps(data, ensure_ascii=False, separators=(",", ":")) + "\n")
    sys.stdout.flush()


def _find_token(value: Any, depth: int = 0) -> str | None:
    if depth > 10:
        return None
    if isinstance(value, dict):
        for key, item in value.items():
            if key in ("success_token", "successToken") and isinstance(item, str) and len(item) >= 8:
                return item
        for item in value.values():
            found = _find_token(item, depth + 1)
            if found:
                return found
    elif isinstance(value, list):
        for item in value:
            found = _find_token(item, depth + 1)
            if found:
                return found
    return None


def _safe_api_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"api.vk.com", "api.vk.ru"}
        or not parsed.path.startswith("/method/")
    ):
        raise ValueError("Недопустимый адрес VK API.")
    return value


def _captcha_url(value: str) -> str:
    parsed = urlparse(value)
    if (
        parsed.scheme != "https"
        or parsed.hostname not in {"id.vk.ru", "id.vk.com"}
        or parsed.path.rstrip("/") != "/not_robot_captcha"
    ):
        raise RuntimeError("VK вернул неожиданный адрес CAPTCHA.")
    token = (parse_qs(parsed.query).get("session_token") or [""])[0]
    if not token:
        raise RuntimeError("В адресе CAPTCHA отсутствует session_token.")
    return token


def _post_form(page, url: str, params: dict[str, Any]) -> dict[str, Any]:
    """POST из страницы VK ID с browser cookies и сетевым стеком Chromium."""
    result = page.evaluate(
        """async ({url, params}) => {
            const body = new URLSearchParams();
            for (const [key, value] of Object.entries(params || {})) {
                body.append(key, value == null ? '' : String(value));
            }
            const response = await fetch(url, {
                method: 'POST',
                mode: 'cors',
                credentials: 'include',
                cache: 'no-store',
                headers: {'Content-Type': 'application/x-www-form-urlencoded'},
                body: body.toString()
            });
            const raw = await response.text();
            let data;
            try { data = JSON.parse(raw); }
            catch (_) { data = {error: {error_msg: 'VK вернул не-JSON ответ'}}; }
            return {http_status: response.status, http_ok: response.ok, data};
        }""",
        {"url": _safe_api_url(url), "params": params},
    )
    if not isinstance(result, dict) or not isinstance(result.get("data"), dict):
        raise RuntimeError("Браузер вернул ответ VK API неожиданного формата.")
    return result["data"]


def _complete_captcha(page, error: dict[str, Any], retry_url: str,
                      retry_params: dict[str, Any]) -> dict[str, Any]:
    """Открывает SmartCaptcha, ждёт ручную проверку и повторяет исходный init."""
    redirect_uri = str(error.get("redirect_uri") or "")
    challenge_session_token = _captcha_url(redirect_uri)
    captcha_sid = str(error.get("captcha_sid") or "")
    captcha_ts = error.get("captcha_ts")
    if captcha_ts in (None, ""):
        captcha_ts = 0
    captcha_attempt = error.get("captcha_attempt") or 1

    state: dict[str, Any] = {
        "success_token": "",
        "captcha_error": "",
        "end_session_seen": False,
    }

    def on_response(response) -> None:
        try:
            parsed = urlparse(response.url)
            host = (parsed.hostname or "").lower()
            path = parsed.path.lower()
            request = response.request
            form = {}
            try:
                from urllib.parse import parse_qs
                form = {
                    key: values[-1]
                    for key, values in parse_qs(request.post_data or "", keep_blank_values=True).items()
                }
            except Exception:
                pass

            if host == "api.vk.com" and path.endswith("/captchanotrobot.endsession"):
                state["end_session_seen"] = True
                return

            if host != "api.vk.com" or not path.endswith("/captchanotrobot.check"):
                return
            if form.get("session_token") != challenge_session_token:
                return
            data = response.json()
            payload = data.get("response") if isinstance(data, dict) else None
            status = payload.get("status") if isinstance(payload, dict) else None
            token = _find_token(data)
            if token and response.ok and str(status).upper() == "OK":
                state["success_token"] = token
        except Exception:
            # Сетевой callback не должен ломать жизненный цикл страницы.
            return

    page.on("response", on_response)
    try:
        sys.stderr.write("VK запросил SmartCaptcha; ожидается ручная проверка в окне браузера.\n")
        sys.stderr.flush()
        try:
            page.goto(redirect_uri, wait_until="domcontentloaded", timeout=120000)
        except Exception:
            if page.is_closed() or page.url in ("", "about:blank"):
                raise RuntimeError("Не удалось открыть страницу SmartCaptcha.")

        deadline = time.monotonic() + 900.0
        while not state["success_token"] and time.monotonic() < deadline:
            if page.is_closed():
                raise RuntimeError("Окно браузера закрыто до завершения CAPTCHA.")
            page.wait_for_timeout(150)

        if not state["success_token"]:
            raise RuntimeError("Истекло время ожидания ручной CAPTCHA (15 минут).")

        # VK ID отправляет endSession отдельным POST после check.
        end_deadline = time.monotonic() + 6.0
        while not state["end_session_seen"] and time.monotonic() < end_deadline:
            if page.is_closed():
                break
            page.wait_for_timeout(100)
    finally:
        try:
            page.remove_listener("response", on_response)
        except Exception:
            pass

    # Возвращаем страницу в исходный origin, не создавая новый browser context.
    try:
        page.goto("https://id.vk.com/", wait_until="domcontentloaded", timeout=120000)
    except Exception:
        if page.is_closed():
            raise RuntimeError("Браузер был закрыт после прохождения CAPTCHA.")

    replay_params = dict(retry_params)
    replay_params.update({
        "captcha_key": "",
        "captcha_sid": captcha_sid,
        "is_sound_captcha": "0",
        "captcha_ts": captcha_ts,
        "captcha_attempt": captcha_attempt,
        "success_token": state["success_token"],
    })
    return _post_form(page, retry_url, replay_params)


def _handle_request(page, command: dict[str, Any]) -> dict[str, Any]:
    method = str(command.get("method") or "")
    url = _safe_api_url(str(command.get("url") or ""))
    params = command.get("params")
    if not method or not isinstance(params, dict):
        raise ValueError("Неполная команда запроса браузерной сессии.")

    response = _post_form(page, url, params)
    error = response.get("error")
    captcha_performed = False

    if method == "restore.init" and isinstance(error, dict) and error.get("error_code") == 14:
        if not error.get("redirect_uri"):
            raise RuntimeError("VK вернул CAPTCHA без redirect_uri; автоматическое продолжение невозможно.")
        response = _complete_captcha(page, error, url, params)
        captcha_performed = True

    return {"ok": True, "response": response, "captcha_performed": captcha_performed}


def run_server() -> int:
    profile = Path(sys.argv[2] if len(sys.argv) > 2 else "browser_profile").expanduser().resolve()
    browser_executable = sys.argv[3].strip() if len(sys.argv) > 3 else ""
    profile.mkdir(parents=True, exist_ok=True)

    with sync_playwright() as playwright:
        if browser_executable:
            executable = Path(browser_executable).expanduser().resolve()
            if not executable.is_file():
                raise RuntimeError("Указанный файл Chrome/Chromium не найден.")
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                executable_path=str(executable),
                headless=False,
                viewport={"width": 1180, "height": 900},
                timeout=60000,
                args=["--start-maximized"],
            )
        else:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile),
                headless=False,
                viewport={"width": 1180, "height": 900},
                timeout=60000,
                args=["--start-maximized"],
            )

        try:
            page = context.pages[0] if context.pages else context.new_page()
            # Подготавливаем постоянные cookies VK ID один раз при старте.
            for start_url in ("https://vk.com/", "https://id.vk.com/"):
                try:
                    page.goto(start_url, wait_until="domcontentloaded", timeout=120000)
                except Exception:
                    if page.is_closed():
                        raise RuntimeError("Браузерная страница VK неожиданно закрылась.")
            sys.stderr.write("Постоянный контекст VK ID запущен. Сетевые запросы выполняются через браузер.\n")
            sys.stderr.flush()

            for raw_line in sys.stdin:
                try:
                    command = json.loads(raw_line)
                    if not isinstance(command, dict):
                        raise ValueError("Некорректная RPC-команда.")
                    if command.get("cmd") == "shutdown":
                        _write_message({"ok": True, "shutdown": True})
                        break
                    if command.get("cmd") != "request":
                        raise ValueError("Неизвестная RPC-команда.")
                    result = _handle_request(page, command)
                    _write_message(result)
                except Exception as exc:
                    message = f"{type(exc).__name__}: {exc}"
                    # Не включаем URL, параметры запросов, cookies и captcha tokens в IPC.
                    _write_message({"ok": False, "error": message[:1200]})
        finally:
            try:
                context.close()
            except Exception:
                pass
    return 0


def main() -> int:
    if len(sys.argv) < 2 or sys.argv[1] != "--server":
        sys.stderr.write("Этот модуль запускается приложением VK ID Calibrator.\n")
        return 2
    try:
        return run_server()
    except Exception as exc:
        sys.stderr.write(f"Ошибка браузерной сессии: {type(exc).__name__}: {exc}\n")
        sys.stderr.flush()
        return 1


if __name__ == "__main__":
    raise SystemExit(main())
