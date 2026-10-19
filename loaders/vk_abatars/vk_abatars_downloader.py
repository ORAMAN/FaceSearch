# VK ID: OAuth 2.1 Authorization Code Flow с PKCE.
# Старые access_token по-прежнему можно вставить вручную.

import os
import re
import sys
import json
import time
import queue
import threading
import webbrowser
import importlib
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urlparse, urlsplit, parse_qs, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from playwright.sync_api import sync_playwright
except ImportError:
    sync_playwright = None

# === ПАРАМЕТРЫ === #
APP_DIR = Path(__file__).resolve().parent
VK_TOKEN_FILE = APP_DIR.parent / "vk" / "vk_token.txt"
LOG_FILE = APP_DIR / "Log.txt"
DEFAULT_OUTPUT_DIR = APP_DIR / "VK_Avatars_Downloads"
APP_SETTINGS_PATH = APP_DIR / "vk_abatars_settings.json"
DEFAULT_FACESEARCH_DIR = Path(r"C:\FaceSearch")
DEFAULT_BROWSER_PROFILE_DIR = APP_DIR.parent / "vk" / "VK_Browser_Profile"
VK_SERVICE_TOKEN_FILE = APP_DIR.parent / "vk" / "vk_service_token.txt"
VK_SILENT_TOKEN_FILE = APP_DIR.parent / "vk" / "vk_silent_token.json"
_META_RESOLUTION = "_url_resolution"
_META_VERSION = "_resolution_version"
_META_DUPLICATE_OF = "_duplicate_of"
_META_DUPLICATE_REASON = "_duplicate_reason"
API_VERSION = "5.199"
BASE_URL = "https://api.vk.ru/method"
FRIENDS_PAGE_SIZE = 5000
USER_BATCH_SIZE = 1000  # users.get поддерживает не более 1000 ID в запросе.
AUTH_STOP_REQUESTED = threading.Event()
# ID пользователей, чьи профили уже заведены в активной БД FaceSearch.
EXISTING_PROFILE_IDS: set[int] = set()
EXISTING_PROFILE_IDS_LOCK = threading.Lock()
_AUTH_THREAD = None

# VK ID flow для загрузчика: silent_token -> auth.exchangeSilentAuthToken.
# OAuth 2.1 Authorization Code Flow выдаёт vk2.a токены, несовместимые с
# используемыми загрузчиком методами photos.get/friends.get.
VKID_CLIENT_ID = "8101047"
VKID_REDIRECT_URI = "https://oauth.vk.ru/blank.html"
VKID_SILENT_AUTH_URL = (
    "https://id.vk.ru/auth?return_auth_hash=1f3f97d3e974a20acf"
    "&redirect_uri=https%3A%2F%2Foauth.vk.ru%2Fblank.html"
    "&redirect_uri_hash=96188d4fed90d04a34&force_hash=1&app_id=8101047"
    "&response_type=token&code_challenge=&code_challenge_method=&scope=327684&state="
)
UI_EVENTS = None
STOP_REQUESTED = threading.Event()
_TOKEN_FILE_LOCK = threading.Lock()
# === ========= === #

# ============================================================
# ACCESS_TOKENS
# ============================================================

count_tokens = 0
current_token = 0
_TOKEN_LIST = []
_TOKEN_LOCK = threading.Lock()


def _read_all_token_file() -> list[str]:
    """Читает все сохранённые строки токенов, включая несовместимые токены VK ID."""
    if not VK_TOKEN_FILE.exists():
        return []
    tokens = []
    seen = set()
    for line in VK_TOKEN_FILE.read_text(encoding="utf-8-sig").splitlines():
        token = line.strip()
        if not token or token.startswith("#") or token in seen:
            continue
        seen.add(token)
        tokens.append(token)
    return tokens


def _read_token_file() -> list[str]:
    """Возвращает токены, пригодные для legacy-методов загрузчика (photos.get/friends.get)."""
    # vk2.a.* — токены нового OAuth VK ID. Они могут успешно проходить вход,
    # но не подходят для требуемых загрузчику методов VK API.
    return [token for token in _read_all_token_file() if not token.lower().startswith("vk2.")]


def reload_token_cache() -> int:
    """Сбрасывает кэш токенов, например после добавления нового токена."""
    global _TOKEN_LIST, count_tokens, current_token
    tokens = _read_token_file()
    with _TOKEN_LOCK:
        _TOKEN_LIST = tokens
        count_tokens = len(tokens)
        current_token = 0
    return len(tokens)


def _load_tokens_if_needed():
    """Загружает токены из файла только тогда, когда кэш пуст."""
    global _TOKEN_LIST, count_tokens, current_token
    if _TOKEN_LIST:
        return
    tokens = _read_token_file()
    if not tokens:
        raise RuntimeError(
            f"В {VK_TOKEN_FILE.name} нет токенов. Добавьте токен через кнопку авторизации."
        )
    with _TOKEN_LOCK:
        if not _TOKEN_LIST:
            _TOKEN_LIST = tokens
            count_tokens = len(tokens)
            current_token = 0


def reload_tokens():
    """Возвращает следующий токен из vk_token.txt по кругу."""
    global current_token
    _load_tokens_if_needed()
    with _TOKEN_LOCK:
        token = _TOKEN_LIST[current_token]
        current_token = (current_token + 1) % len(_TOKEN_LIST)
        return token


def extract_vk_auth_payload(value: str) -> dict:
    """Разбирает callback VK ID query/fragment, включая legacy #payload={...}."""
    value = (value or "").strip()
    if not value:
        return {}

    try:
        parsed = urlsplit(value)
        query = parse_qs(parsed.query, keep_blank_values=True)

        # Современный VK ID может вернуть ошибку в query-параметрах.
        if query.get("error"):
            return {
                "type": "authorization_error",
                "error": (query.get("error") or [""])[0],
                "error_description": (query.get("error_description") or [""])[0],
            }

        code = (query.get("code") or [""])[0].strip()
        callback_type = str((query.get("type") or [""])[0]).strip().lower()
        if code:
            if callback_type == "code_v2" and code.lower().startswith("vk2."):
                return {
                    "type": "access_token",
                    "access_token": code,
                    "token_format": "code_v2",
                }
            return {
                "type": "authorization_code",
                "code": code,
                "state": (query.get("state") or [""])[0],
                "device_id": (query.get("device_id") or [""])[0],
                "code_format": callback_type,
            }

        fragment = parse_qs(parsed.fragment, keep_blank_values=True)

        # Некоторые callbacks помещают payload во fragment, другие — в query.
        # Берём сырую строку параметра и используем unquote, а не unquote_plus:
        # символ '+' в JSON-поле не должен превращаться в пробел.
        raw_payload = None
        for component in (parsed.fragment, parsed.query):
            for part in component.split("&"):
                key, separator, raw_value = part.partition("=")
                if separator and unquote(key) == "payload":
                    raw_payload = unquote(raw_value)
                    break
            if raw_payload is not None:
                break

        if raw_payload is not None:
            candidate = raw_payload
            last_error = None
            for _ in range(3):
                try:
                    data = json.loads(candidate)
                    if isinstance(data, dict):
                        return data
                    return {
                        "type": "authorization_error",
                        "error": "invalid_payload",
                        "error_description": "VK ID callback payload имеет неожиданный формат.",
                    }
                except (json.JSONDecodeError, TypeError) as exc:
                    last_error = exc
                    decoded = unquote(candidate)
                    if decoded == candidate:
                        break
                    candidate = decoded
            return {
                "type": "authorization_error",
                "error": "invalid_payload",
                "error_description": (
                    "Callback VK ID содержит параметр payload, но JSON не удалось разобрать "
                    f"({type(last_error).__name__ if last_error else 'ошибка формата'})."
                ),
            }

        # Старый implicit flow: #access_token=...&user_id=...
        for params in (fragment, query):
            token = (params.get("access_token") or [""])[0].strip()
            if token:
                return {
                    "type": "access_token",
                    "access_token": token,
                    "user_id": (params.get("user_id") or [""])[0],
                }
    except Exception as exc:
        return {
            "type": "authorization_error",
            "error": "callback_parse_error",
            "error_description": f"Не удалось разобрать URL ответа VK ID ({type(exc).__name__}).",
        }

    match = re.search(r"(?:^|[&#?\s])access_token(?:_[^=&#\s]+)?=([^&#\s]+)", value)
    if match:
        return {"type": "access_token", "access_token": unquote(match.group(1)).strip()}

    # Ручной ввод ранее полученного токена по-прежнему поддерживается.
    if " " not in value and "://" not in value and "access_token=" not in value:
        return {"type": "access_token", "access_token": value}
    return {}


def extract_access_token(value: str) -> str:
    """Извлекает API access_token; silent_token VK ID нельзя использовать напрямую."""
    payload = extract_vk_auth_payload(value)
    token_type = str(payload.get("type") or "").lower()
    if token_type == "silent_token":
        return ""
    token = payload.get("access_token")
    if token:
        return str(token).strip()
    if token_type == "access_token":
        return str(payload.get("token") or "").strip()
    return ""

def validate_access_token(token: str) -> str:
    """Проверяет совместимый API-токен через users.get и возвращает имя пользователя."""
    token = (token or "").strip()
    if token.lower().startswith("vk2."):
        raise ValueError(
            "Получен токен нового формата VK ID (vk2.a.…). Для загрузчика он не подходит: "
            "методы photos.get и friends.get требуют совместимый пользовательский API-токен. "
            "Токен vk2 нельзя превратить в vk1 заменой префикса. Он не сохранён. "
            "Используйте ранее полученный рабочий vk1.a токен либо другой поддерживаемый "
            "VK API OAuth-поток, который выдаёт токен для этих методов."
        )
    response = requests.get(
        f"{BASE_URL}/users.get",
        params={"access_token": token, "v": API_VERSION},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    error = payload.get("error")
    if error:
        raise RuntimeError(
            f"VK API отклонил токен: {error.get('error_msg', error)} "
            f"(код {error.get('error_code', '?')})."
        )
    users = payload.get("response") or []
    if not users:
        raise RuntimeError("VK API не вернул пользователя для проверки токена.")
    user = users[0]
    return f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or str(user.get("id", ""))


def save_access_token(token: str) -> tuple[bool, int]:
    """Добавляет совместимый API-токен в vk_token.txt, не создавая повторов."""
    token = (token or "").strip()
    if not token:
        raise ValueError("Токен пустой.")
    if token.lower().startswith("vk2."):
        raise ValueError(
            "Токен vk2.a… не подходит для photos.get/friends.get и не может быть сохранён "
            "как рабочий токен загрузчика. Нужен совместимый пользовательский API-токен."
        )
    with _TOKEN_FILE_LOCK:
        existing_all = _read_all_token_file()
        if token in existing_all:
            return False, reload_token_cache()
        VK_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        temp_path = VK_TOKEN_FILE.with_suffix(VK_TOKEN_FILE.suffix + ".tmp")
        temp_path.write_text("\n".join(existing_all + [token]) + "\n", encoding="utf-8")
        temp_path.replace(VK_TOKEN_FILE)
    count = reload_token_cache()
    return True, count


def read_service_token() -> str:
    """Читает service token из переменной окружения либо локального файла."""
    value = os.environ.get("VK_SERVICE_TOKEN", "").strip()
    if value:
        return value
    try:
        if VK_SERVICE_TOKEN_FILE.is_file():
            return VK_SERVICE_TOKEN_FILE.read_text(encoding="utf-8-sig").strip()
    except OSError:
        pass
    return ""


def default_chrome_executable() -> str:
    """Находит стандартную установку Chrome в Windows."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES") or "C:/Program Files") / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)") or "C:/Program Files (x86)") / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[0])


def _save_pending_silent_token(payload: dict) -> None:
    """Сохраняет только необходимые поля silent_token для последующего обмена."""
    user = payload.get("user") if isinstance(payload.get("user"), dict) else {}
    safe_payload = {
        "type": "silent_token",
        "token": str(payload.get("token") or ""),
        "uuid": str(payload.get("uuid") or ""),
        "hash": str(payload.get("hash") or ""),
        "user_id": user.get("id"),
        "first_name": user.get("first_name", ""),
        "last_name": user.get("last_name", ""),
        "ttl": max(1, int(payload.get("ttl") or 600)),
        "saved_at": int(time.time()),
    }
    VK_SILENT_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
    temp_path = VK_SILENT_TOKEN_FILE.with_suffix(".tmp")
    temp_path.write_text(json.dumps(safe_payload, ensure_ascii=False, indent=2), encoding="utf-8")
    temp_path.replace(VK_SILENT_TOKEN_FILE)


def load_pending_silent_token() -> dict:
    """Загружает silent_token, сохранённый после автоматического браузерного входа."""
    if not VK_SILENT_TOKEN_FILE.is_file():
        raise FileNotFoundError(
            f"Файл {VK_SILENT_TOKEN_FILE.name} не найден. Сначала выполните вход VK ID через Chrome."
        )
    payload = json.loads(VK_SILENT_TOKEN_FILE.read_text(encoding="utf-8-sig"))
    if not isinstance(payload, dict) or not payload.get("token"):
        raise ValueError(f"В {VK_SILENT_TOKEN_FILE.name} нет корректного silent_token.")
    saved_at = int(payload.get("saved_at") or 0)
    ttl = max(1, int(payload.get("ttl") or 600))
    if not saved_at or time.time() - saved_at > ttl:
        try:
            VK_SILENT_TOKEN_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        raise ValueError(
            "Срок действия silent_token истёк. Нажмите «Войти через Chrome автоматически» и выполните вход повторно."
        )
    return payload


def exchange_silent_token(silent_token: str, service_token: str, uuid: str = "") -> dict:
    """Обменивает VK ID silent_token на API access_token с помощью service token."""
    if not silent_token:
        raise ValueError("VK ID не вернул silent_token.")
    if not service_token:
        raise ValueError(
            "Не указан service token приложения. Silent token VK ID не является API access_token."
        )
    response = requests.post(
        f"{BASE_URL}/auth.exchangeSilentAuthToken",
        data={
            "v": API_VERSION,
            "token": silent_token,
            "uuid": uuid or "",
            "access_token": service_token,
        },
        timeout=30,
    )
    response.raise_for_status()
    data = response.json()
    error = data.get("error")
    if error:
        raise RuntimeError(
            "VK не смог обменять silent_token: "
            f"{error.get('error_msg', error)} (код {error.get('error_code', '?')}). "
            "Проверьте service token и права приложения."
        )
    result = data.get("response") or {}
    if not isinstance(result, dict) or not result.get("access_token"):
        raise RuntimeError(f"VK не вернул access_token при обмене: {data}")
    return result


def _browser_session_from_context(context) -> tuple[requests.Session, int]:
    """Создаёт requests-сессию с cookies авторизованного браузерного контекста."""
    session = requests.Session()
    session.headers.update({
        "User-Agent": (
            "Mozilla/5.0 (Windows NT 10.0; Win64; x64) "
            "AppleWebKit/537.36 (KHTML, like Gecko) "
            "Chrome/150.0.0.0 Safari/537.36"
        )
    })
    copied = 0
    for cookie in context.cookies():
        name = str(cookie.get("name") or "").strip()
        value = cookie.get("value")
        if not name or value is None:
            continue
        options = {
            "path": str(cookie.get("path") or "/"),
            "secure": bool(cookie.get("secure", False)),
        }
        domain = cookie.get("domain")
        if domain:
            options["domain"] = str(domain)
        session.cookies.set(name, str(value), **options)
        copied += 1
    return session, copied


def _extract_access_token_from_oauth_url(url: str) -> str:
    """Извлекает access_token из URL OAuth, включая вложенный authorize_url."""
    candidates = [url]
    parsed = urlsplit(url or "")
    query = parse_qs(parsed.query, keep_blank_values=True)
    nested = (query.get("authorize_url") or [""])[0]
    if nested:
        if nested.startswith("https%3A") or "%3A%2F%2F" in nested:
            nested = unquote(nested)
        candidates.insert(0, nested)

    for candidate in candidates:
        parsed_candidate = urlsplit(candidate)
        for params in (
            parse_qs(parsed_candidate.query, keep_blank_values=True),
            parse_qs(parsed_candidate.fragment, keep_blank_values=True),
        ):
            token = str((params.get("access_token") or [""])[0]).strip()
            if token:
                return token
    return ""


def exchange_browser_session_for_legacy_user_token(
    context, payload: dict | None = None, app_id: str = VKID_CLIENT_ID, scope: int = 327684
) -> str:
    """Получает legacy user API token из авторизованной браузерной сессии.

    Алгоритм основан на рабочем auth-потоке vk_api: oauth.vk.ru/authorize,
    извлечение data.hash.return_auth из window.init, connect_internal и
    auth.getOauthToken. Callback silent_token используется как сигнал, что
    пользователь завершил подтверждение, но его hash не считается return_auth_hash.
    """
    session, cookie_count = _browser_session_from_context(context)
    if cookie_count == 0:
        raise RuntimeError(
            "VK вернул страницу callback, но браузер не передал cookies в обмен. "
            "Повторите вход через окно Chrome, открытое загрузчиком."
        )

    response = session.get(
        "https://oauth.vk.ru/authorize",
        params={
            "client_id": app_id,
            "scope": scope,
            "response_type": "token",
        },
        timeout=30,
        allow_redirects=True,
    )
    response.raise_for_status()
    if "act=blocked" in response.url:
        raise RuntimeError("VK заблокировал legacy OAuth для этого приложения или аккаунта.")

    token = _extract_access_token_from_oauth_url(response.url)
    if token:
        if token.lower().startswith("vk2."):
            raise RuntimeError(
                "OAuth вернул токен vk2.a…, несовместимый с photos.get/friends.get. "
                "VK не выдал legacy user API token для этого приложения."
            )
        return token

    # В некоторых ответах OAuth сначала отдаёт URL, на который перенаправляет JS.
    redirect_match = re.search(r'location\.href\s*=\s*"([^"]+)"\s*\+\s*addr;', response.text)
    if redirect_match:
        redirect_url = redirect_match.group(1)
        if not redirect_url.startswith(("http://", "https://")):
            from urllib.parse import urljoin
            redirect_url = urljoin(response.url, redirect_url)
        response = session.get(redirect_url, timeout=30, allow_redirects=True)
        response.raise_for_status()
        token = _extract_access_token_from_oauth_url(response.url)
        if token:
            if token.lower().startswith("vk2."):
                raise RuntimeError("VK вернул vk2-токен вместо legacy user API token.")
            return token

    # Рабочий legacy-поток возвращает hash внутри window.init, именно
    # data.hash.return_auth (а не поле hash из payload silent_token).
    return_auth_hash = ""
    auth_json_match = re.search(r"window\.init\s*=\s*(\{.*?\});", response.text, re.DOTALL)
    if auth_json_match:
        try:
            auth_json = json.loads(auth_json_match.group(1))
            return_auth_hash = str(
                (((auth_json.get("data") or {}).get("hash") or {}).get("return_auth")) or ""
            ).strip()
        except (json.JSONDecodeError, AttributeError, TypeError):
            return_auth_hash = ""

    # Fallback для callback-реализаций, где поле hash действительно содержит return_auth.
    # Основной путь выше использует значение из OAuth HTML, как в vk_api.
    if not return_auth_hash and isinstance(payload, dict):
        return_auth_hash = str(payload.get("hash") or "").strip()

    if not return_auth_hash:
        location = urlsplit(response.url)
        raise RuntimeError(
            "VK не выдал return_auth_hash в ответе oauth.vk.ru/authorize. "
            f"Ответ: {location.scheme}://{location.netloc}{location.path}; "
            "доступность legacy OAuth для приложения могла быть ограничена."
        )

    connect_response = session.post(
        "https://login.vk.ru/?act=connect_internal",
        data={
            "uuid": "",
            "service_group": "",
            "return_auth_hash": return_auth_hash,
            "version": 1,
            "app_id": app_id,
        },
        headers={"Origin": "https://id.vk.ru", "Referer": "https://id.vk.ru/"},
        timeout=30,
    )
    connect_response.raise_for_status()
    try:
        connect_data = connect_response.json()
    except ValueError as exc:
        raise RuntimeError("VK вернул некорректный JSON из connect_internal.") from exc

    if not isinstance(connect_data, dict) or connect_data.get("type") != "okay":
        reason = ""
        if isinstance(connect_data, dict):
            reason = str(connect_data.get("error") or connect_data.get("message") or connect_data.get("type") or "")
        raise RuntimeError(
            "VK отклонил connect_internal."
            + (f" Ответ VK: {reason}" if reason else "")
        )

    data = connect_data.get("data") or {}
    temporary_token = str(data.get("access_token") or "").strip()
    auth_user_hash = str(data.get("auth_user_hash") or "").strip()
    if not temporary_token or not auth_user_hash:
        raise RuntimeError("connect_internal не вернул access_token/auth_user_hash.")

    oauth_response = session.post(
        "https://api.vk.ru/method/auth.getOauthToken",
        data={
            "hash": return_auth_hash,
            "auth_user_hash": auth_user_hash,
            "app_id": app_id,
            "client_id": app_id,
            "scope": scope,
            "access_token": temporary_token,
            "is_seamless_auth": 1,
            "v": "5.207",
        },
        headers={"Origin": "https://id.vk.ru"},
        timeout=30,
    )
    oauth_response.raise_for_status()
    try:
        oauth_data = oauth_response.json()
    except ValueError as exc:
        raise RuntimeError("VK вернул некорректный JSON из auth.getOauthToken.") from exc

    error = oauth_data.get("error") if isinstance(oauth_data, dict) else None
    if error:
        raise RuntimeError(
            "VK не выдал пользовательский API-токен: "
            f"{error.get('error_msg', error)} (код {error.get('error_code', '?')})."
        )
    result = oauth_data.get("response") if isinstance(oauth_data, dict) else None
    if isinstance(result, str):
        token = result.strip()
    elif isinstance(result, dict):
        token = str(result.get("access_token") or "").strip()
    else:
        token = ""
    if not token:
        raise RuntimeError("VK не вернул access_token из auth.getOauthToken.")
    if token.lower().startswith("vk2."):
        raise RuntimeError(
            "VK снова вернул токен vk2.a…, несовместимый с photos.get/friends.get."
        )
    return token


def _validate_and_save_access_token(token: str) -> tuple[str, bool, int]:
    """Проверяет API access_token и сохраняет его в общий список токенов."""
    user_name = validate_access_token(token)
    added, count = save_access_token(token)
    return user_name, added, count


def _exchange_and_store_pending_token(service_token: str) -> None:
    """Повторно обменивает сохранённый silent_token в фоновом потоке."""
    try:
        pending = load_pending_silent_token()
        result = exchange_silent_token(
            str(pending.get("token") or ""),
            service_token,
            str(pending.get("uuid") or ""),
        )
        user_name, added, count = _validate_and_save_access_token(str(result.get("access_token") or ""))
        try:
            VK_SILENT_TOKEN_FILE.unlink(missing_ok=True)
        except OSError:
            pass
        post_ui_event("token_saved", True, user_name, added, count, "")
        post_ui_event("auth_status", f"Авторизация VK завершена: {user_name}. API access_token сохранён.")
    except Exception as exc:
        post_ui_event("auth_error", str(exc))


def _browser_auth_worker(profile_directory: str, chrome_executable: str, service_token: str = "") -> None:
    """Получает VK ID silent_token и обменивает его на API access_token для загрузчика."""
    if sync_playwright is None:
        post_ui_event(
            "auth_error",
            "Для автоматической авторизации установите Playwright: python -m pip install playwright.",
        )
        return

    context = None
    try:
        profile_path = Path(profile_directory).expanduser()
        profile_path.mkdir(parents=True, exist_ok=True)
        executable = Path(chrome_executable).expanduser()
        if not executable.is_file():
            raise FileNotFoundError(f"Не найден chrome.exe: {executable}")

        post_ui_event(
            "auth_status",
            "Открываю VK ID; после возврата blank.html получу API-токен через OAuth-сессию…",
        )
        with sync_playwright() as playwright:
            context = playwright.chromium.launch_persistent_context(
                user_data_dir=str(profile_path.resolve()),
                executable_path=str(executable.resolve()),
                headless=False,
                viewport=None,
                no_viewport=True,
                locale="ru-RU",
                accept_downloads=True,
                args=["--start-maximized", "--window-size=1920,1080", "--window-position=0,0"],
            )
            page = context.pages[0] if context.pages else context.new_page()
            try:
                page.goto(VKID_SILENT_AUTH_URL, wait_until="domcontentloaded", timeout=45000)
            except Exception:
                # VK ID может перенаправить на blank.html до истечения стандартного timeout.
                pass

            deadline = time.monotonic() + 600
            last_callback_status = None
            last_seen_urls = {}
            while not AUTH_STOP_REQUESTED.is_set() and time.monotonic() < deadline:
                pages = list(context.pages)
                if not pages:
                    if last_callback_status != "pages_missing":
                        post_ui_event(
                            "auth_status",
                            "Chrome запущен, но Playwright сейчас не видит ни одной вкладки. "
                            "Закройте авторизацию и запустите её повторно из загрузчика.",
                        )
                        last_callback_status = "pages_missing"
                    time.sleep(0.25)
                    continue

                for page_index, current_page in enumerate(pages):
                    current_url = current_page.url or ""
                    parsed_current = urlsplit(current_url)
                    current_location = f"{parsed_current.scheme}://{parsed_current.netloc}{parsed_current.path}"

                    # Не ждём, пока fragment #payload будет распознан. Сам callback
                    # blank.html означает, что VK завершил экран согласия; дальше
                    # токен запрашивается через cookies авторизованного браузера.
                    is_vk_callback = (
                        parsed_current.netloc.lower() in {"oauth.vk.ru", "oauth.vk.com"}
                        and parsed_current.path.rstrip("/") == "/blank.html"
                    )
                    if is_vk_callback:
                        if last_seen_urls.get(page_index) != current_url:
                            last_seen_urls[page_index] = current_url
                            print(
                                f"[VK AUTH] Callback обнаружен: {current_location}. "
                                "Запускаю получение legacy user API token через OAuth-сессию."
                            )
                        post_ui_event(
                            "auth_status",
                            "VK ID вернул callback. Получаю пользовательский API-токен через OAuth-сессию…",
                        )
                        payload = extract_vk_auth_payload(current_url)
                        if str(payload.get("type") or "").lower() == "authorization_error":
                            # Невалидный payload не блокирует попытку, если cookies уже авторизованы.
                            print("[VK AUTH] Payload callback не распознан; продолжаю обмен по cookies браузера.")
                            payload = {}
                        token = exchange_browser_session_for_legacy_user_token(context, payload)
                        user_name, added, count = _validate_and_save_access_token(token)
                        try:
                            VK_SILENT_TOKEN_FILE.unlink(missing_ok=True)
                        except OSError:
                            pass
                        post_ui_event("token_saved", True, user_name, added, count, "")
                        post_ui_event(
                            "auth_status",
                            f"Авторизация VK завершена: {user_name}. Совместимый API access_token сохранён.",
                        )
                        return

                    payload = extract_vk_auth_payload(current_url)
                    if last_seen_urls.get(page_index) != current_url:
                        last_seen_urls[page_index] = current_url
                        fragment_keys = sorted(
                            key for key in parse_qs(parsed_current.fragment, keep_blank_values=True).keys()
                        )
                        query_keys = sorted(
                            key for key in parse_qs(parsed_current.query, keep_blank_values=True).keys()
                        )
                        payload_type_for_log = str(payload.get("type") or "") if payload else "unparsed"
                        # Намеренно не записываем значения URL, token, hash или payload в журнал.
                        diagnostic = (
                            f"[VK AUTH] Tab {page_index + 1}: {current_location}; "
                            f"query_keys={query_keys}; fragment_keys={fragment_keys}; "
                            f"parsed_type={payload_type_for_log or 'none'}"
                        )
                        print(diagnostic)
                        if "blank.html" in parsed_current.path and "payload" in (
                            fragment_keys + query_keys
                        ) and payload.get("type") == "authorization_error":
                            raise RuntimeError(str(payload.get("error_description") or "Не удалось разобрать payload VK ID."))

                    if not payload:
                        if (
                            parsed_current.netloc.lower() in {"oauth.vk.ru", "oauth.vk.com"}
                            and parsed_current.path.rstrip("/") == "/blank.html"
                        ):
                            callback_status = (
                                "VK открыл blank.html, но URL не содержит распознанного payload/code. "
                                "В журнал добавлен безопасный диагноз без токена: проверьте строку [VK AUTH]."
                            )
                            if last_callback_status != callback_status:
                                post_ui_event("auth_status", callback_status)
                                last_callback_status = callback_status
                        continue

                    payload_type = str(payload.get("type") or "").lower()
                    if payload_type == "authorization_error":
                        description = payload.get("error_description") or payload.get("error") or "авторизация отклонена"
                        raise RuntimeError(f"VK ID не завершил авторизацию: {description}")

                    if payload_type == "silent_token":
                        if not payload.get("token"):
                            raise RuntimeError("VK ID вернул payload silent_token без поля token.")
                        if int(payload.get("auth") or 0) != 1:
                            raise RuntimeError("VK ID не подтвердил успешную авторизацию (auth != 1).")

                        _save_pending_silent_token(payload)
                        post_ui_event(
                            "auth_status",
                            "VK ID подтвердил вход. Получаю пользовательский API-токен через браузерную сессию…",
                        )
                        token = exchange_browser_session_for_legacy_user_token(context, payload)
                        user_name, added, count = _validate_and_save_access_token(token)
                        try:
                            VK_SILENT_TOKEN_FILE.unlink(missing_ok=True)
                        except OSError:
                            pass
                        post_ui_event("token_saved", True, user_name, added, count, "")
                        post_ui_event(
                            "auth_status",
                            f"Авторизация VK завершена: {user_name}. Совместимый API access_token сохранён.",
                        )
                        return

                    token = str(payload.get("access_token") or "")
                    if token:
                        if token.lower().startswith("vk2."):
                            raise RuntimeError(
                                "VK ID вернул токен vk2.a… в параметре code (type=code_v2), а не silent_token. "
                                "Загрузчик распознал ответ и остановил ожидание. Этот токен не поддерживает требуемые "
                                "photos.get/friends.get (ошибка VK API 1051); обменивать его через "
                                "auth.exchangeSilentAuthToken нельзя. Для текущего приложения VK ID не выдал "
                                "совместимый пользовательский API-токен vk1.a…. Нужен поддерживаемый VK способ "
                                "выдачи legacy user token или помощь поддержки VK; заменой префикса это не исправить."
                            )
                        user_name, added, count = _validate_and_save_access_token(token)
                        post_ui_event("token_saved", True, user_name, added, count, "")
                        post_ui_event("auth_status", f"Совместимый API access_token сохранён: {user_name}.")
                        return

                time.sleep(0.25)

            if AUTH_STOP_REQUESTED.is_set():
                post_ui_event("auth_status", "Ожидание авторизации остановлено.")
            else:
                raise RuntimeError(
                    "За 10 минут VK ID не вернул silent_token. Возможно, старый silent-auth flow "
                    "отключён для приложения; проверьте актуальные возможности приложения VK ID."
                )
    except Exception as exc:
        post_ui_event("auth_error", f"Ошибка автоматической авторизации VK ID: {exc}")
    finally:
        if context is not None:
            try:
                context.close()
            except Exception:
                pass


def post_ui_event(*event):
    """Передаёт событие в GUI-поток, если интерфейс запущен."""
    if UI_EVENTS is not None:
        UI_EVENTS.put(event)


# ============================================================
# УТИЛИТЫ
# ============================================================

def sanitize_filename(name: str) -> str:
    """Очистка строки от недопустимых символов (Windows)."""
    name = re.sub(r'[<>:"/\\|?*]', '_', name)
    name = name.strip().strip('.')
    return name[:150]


def write_json(path: str, data: dict) -> None:
    """Атомарно записывает JSON-файл в UTF-8."""
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        json.dump(data, f, ensure_ascii=False, indent=2)
    os.replace(tmp, path)


def build_entity_json(entity: dict, url_image: str,
                    album_title: str = None,
                    album_id: int = None,
                    is_group: bool = False) -> dict:
    """
    Собирает JSON-структуру для пользователя или группы.
    Структура: url_source, name, url_image, info.
    """
    if is_group:
        gid = abs(int(entity.get("id", 0)))
        url_source = f"https://vk.com/club{gid}"
        name = entity.get("name", "") or ""

        info = {}
        if entity.get("screen_name"):
            info["Короткое имя"] = entity["screen_name"]
        if entity.get("members_count") is not None:
            info["Участников"] = entity["members_count"]
        if entity.get("status"):
            info["Статус"] = entity["status"]
        if entity.get("description"):
            info["Описание"] = entity["description"]
    else:
        uid = entity.get("id")
        url_source = f"https://vk.com/id{uid}"
        first = entity.get("first_name", "")
        last = entity.get("last_name", "")
        name = f"{first} {last}".strip()

        info = {}
        bdate = entity.get("bdate")
        if bdate:
            info["Дата рождения"] = bdate

        city = entity.get("city", {})
        if city and city.get("title"):
            info["Город"] = city["title"]

        home_town = entity.get("home_town")
        if home_town:
            info["Страна"] = home_town

        sex = entity.get("sex")
        if sex == 1:
            info["Пол"] = "Ж"
        elif sex == 2:
            info["Пол"] = "М"

        is_closed = entity.get("is_closed")
        deactivated = entity.get("deactivated")
        if deactivated == "deleted":
            info["Статус"] = "Удалён"
        elif deactivated == "banned":
            info["Статус"] = "Заблокирован"
        elif is_closed:
            info["Статус"] = "Закрыт"
        else:
            info["Статус"] = "Открыт"

    if album_title is not None:
        info["Альбом"] = album_title
    if album_id is not None:
        info["ID альбома"] = album_id

    return {
        "url_source": url_source,
        "name": name,
        "url_image": url_image.replace("&cs=240x240",""),
        "info": info,
    }


# ============================================================
# VK API
# ============================================================

def get_group_info(group_id_or_screen_name: str, session: requests.Session):
    url = (
        f"https://api.vk.ru/method/groups.getById"
        f"?group_ids={group_id_or_screen_name}"
        f"&fields=photo_max_orig,screen_name,description,members_count,status"
        f"&access_token={reload_tokens()}"
        f"&v={API_VERSION}"
    )
    time.sleep(0.35)
    resp = session.post(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()

    if "error" in data:
        raise RuntimeError(
            f"API error: {data['error']['error_msg']} "
            f"(code {data['error']['error_code']})"
        )

    response = data.get("response", {})

    if isinstance(response, dict) and "groups" in response:
        groups = response["groups"]
    elif isinstance(response, list):
        groups = response
    else:
        groups = []

    if not groups:
        raise ValueError(
            f"Group '{group_id_or_screen_name}' not found, private, or deleted"
        )

    return groups[0]


def get_user_info(user_ids: str, session: requests.Session):
    """Получает подробные данные пачки пользователей одним POST-запросом users.get."""
    params = {
        "user_ids": user_ids,
        "fields": "photo_max_orig,city,home_town,sex,bdate,screen_name,deactivated,is_closed",
        "access_token": reload_tokens(),
        "v": API_VERSION,
    }
    time.sleep(0.35)
    resp = session.post(f"{BASE_URL}/users.get", data=params, timeout=30)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        error = data["error"]
        raise RuntimeError(
            f"users.get: {error.get('error_msg', error)} "
            f"(код {error.get('error_code', 'неизвестен')})"
        )
    response = data.get("response", [])
    if not isinstance(response, list):
        raise RuntimeError("VK API вернул неожиданный ответ users.get.")
    return response


def get_user_friends(user_id: int, session: requests.Session):
    """Возвращает доступных друзей пользователя постранично через friends.get.

    Метод friends.get с параметром fields возвращает объекты пользователей.
    Поля выбраны из поддерживаемого friends.get списка. Список страниц
    читается через offset, а при ответе только с ID карточки запрашиваются
    пачками через users.get.
    """
    all_items = []
    offset = 0
    fields = "bdate,city,sex,status,photo_200_orig,photo_200,photo_50,screen_name"

    while True:
        params = {
            "user_id": int(user_id),
            "offset": offset,
            "count": FRIENDS_PAGE_SIZE,
            "fields": fields,
            "access_token": reload_tokens(),
            "v": API_VERSION,
        }

        page_data = None
        for attempt in range(3):
            if attempt:
                time.sleep(0.8 * attempt)
                params["access_token"] = reload_tokens()
            else:
                time.sleep(0.35)
            resp = session.get(f"{BASE_URL}/friends.get", params=params, timeout=25)
            resp.raise_for_status()
            payload = resp.json()
            error = payload.get("error")
            if error:
                code = error.get("error_code")
                if code in (5, 6, 29) and attempt < 2:
                    print(f"⚠️ friends.get({user_id}): {error.get('error_msg', error)}; повтор {attempt + 1}/2")
                    continue
                raise RuntimeError(
                    f"friends.get для ID {user_id}: "
                    f"{error.get('error_msg', error)} (код {code})"
                )
            page_data = payload.get("response", {})
            break

        if page_data is None:
            break

        # Обычно response — объект {count, items}. Оставляем поддержку
        # упрощённого ответа со списком на случай отличий API/моков.
        if isinstance(page_data, list):
            items = page_data
            total = len(items)
        else:
            items = page_data.get("items", []) or []
            total = int(page_data.get("count", 0) or 0)

        if not items:
            break

        if isinstance(items[0], dict):
            all_items.extend(item for item in items if item.get("id") is not None)
        else:
            ids = [str(item) for item in items if str(item).isdigit()]
            for start in range(0, len(ids), 1000):
                batch = ids[start:start + 1000]
                if batch:
                    all_items.extend(get_user_info(",".join(batch), session))

        offset += len(items)
        if (total and offset >= total) or len(items) < FRIENDS_PAGE_SIZE:
            break

    return all_items


def get_albums(owner_id: int, session: requests.Session):
    url = (
        f"https://api.vk.ru/method/photos.getAlbums"
        f"?owner_id={owner_id}"
        f"&need_system=1"
        f"&access_token={reload_tokens()}"
        f"&v={API_VERSION}"
    )
    time.sleep(0.35)
    resp = session.get(url, timeout=15)
    resp.raise_for_status()
    data = resp.json()
    if "error" in data:
        raise RuntimeError(f"Albums error: {data['error']}")
    return data["response"]["items"]


def get_user_photos(owner_id: int, session: requests.Session):
    all_items = []
    offset = 0
    while True:
        url = (
            f"https://api.vk.ru/method/photos.getUserPhotos"
            f"?user_id={owner_id}"
            f"&count=1000"
            f"&offset={offset}"
            f"&extended=0"
            f"&access_token={reload_tokens()}"
            f"&v={API_VERSION}"
        )
        time.sleep(0.35)
        resp = session.get(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            print(f"⚠️ getUserPhotos error: {data['error']}")
            break
        items = data["response"]["items"]
        all_items.extend(items)
        if len(items) < 1000:
            break
        offset += 1000
    return all_items


def get_photos_by_album(owner_id: int, album_id: int, session: requests.Session):
    all_items = []
    offset = 0
    while True:
        url = (
            f"https://api.vk.ru/method/photos.get"
            f"?owner_id={owner_id}"
            f"&album_id={album_id}"
            f"&count=200"
            f"&offset={offset}"
            f"&rev=0"
            f"&extended=0"
            f"&photo_sizes=1"
            f"&access_token={reload_tokens()}"
            f"&v={API_VERSION}"
        )
        time.sleep(0.35)
        resp = session.post(url, timeout=20)
        resp.raise_for_status()
        data = resp.json()
        if "error" in data:
            print(f"⚠️ Album {album_id} error: {data['error']}")
            break
        items = data["response"]["items"]
        all_items.extend(items)
        if len(items) < 200:
            break
        offset += 200
    return all_items


def extract_best_url(photo: dict) -> str:
    if "orig_photo" in photo and "url" in photo["orig_photo"]:
        return photo["orig_photo"]["url"]
    if "sizes" in photo and photo["sizes"]:
        return photo["sizes"][-1]["url"]
    raise ValueError("No photo URL found")


# ============================================================
# СКАЧИВАНИЕ
# ============================================================

def download_image(args):
    """
    args = (NameUser, user_id, album_name, photo_id, url, base_dir, json_data)
    Скачивает фото и кладёт рядом одноимённый .json.
    Возвращает (успех, ошибка, путь_изображения, путь_json).
    """
    NameUser, user_id, album_name, photo_id, url, base_dir, json_data = args
    try:
        folder = os.path.join(base_dir, album_name)
        os.makedirs(folder, exist_ok=True)

        parsed = urlparse(url)
        orig_name = os.path.basename(parsed.path)
        if not re.search(r"\.(jpg|jpeg|png)$", orig_name, re.I):
            ext = ".jpg"
        else:
            ext = ""
        safe_name = sanitize_filename(f"{photo_id}_{orig_name}{ext}")
        filepath = os.path.join(folder, safe_name)
        json_path = os.path.splitext(filepath)[0] + ".json"

        # Защита от дублей: файл уже есть — не качаем, но JSON допишем.
        if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            if json_data is not None:
                try:
                    write_json(json_path, json_data)
                except Exception as e:
                    return False, f"JSON write error: {e}", filepath, json_path
            return True, None, filepath, json_path

        resp = requests.get(url, timeout=25, stream=True)
        if resp.status_code != 200:
            return False, f"HTTP {resp.status_code}", filepath, json_path
        ct = resp.headers.get("content-type", "").lower()
        if "image" not in ct and "octet-stream" not in ct:
            return False, f"Invalid content-type: {ct}", filepath, json_path

        with open(filepath, "wb") as f:
            for chunk in resp.iter_content(65536):
                f.write(chunk)

        # JSON рядом с картинкой
        if json_data is not None:
            try:
                write_json(json_path, json_data)
            except Exception as e:
                return False, f"JSON write error: {e}", filepath, json_path

        return True, None, filepath, json_path

    except Exception as e:
        return False, str(e), None, None


def save_avatar_to_root(entity: dict, base_dir: str, is_group: bool = False) -> bool:
    """
    Сохраняет аватар в корень папки как Avatar.jpg
    и рядом кладёт Avatar.json.
    """
    avatar_url = (
        entity.get("photo_max_orig") or
        entity.get("photo_400_orig") or
        entity.get("photo_200_orig") or
        entity.get("photo_512") or
        entity.get("photo_200") or
        entity.get("photo_100") or
        ""
    ).strip()

    if " " in avatar_url:
        avatar_url = avatar_url.split(" ")[0].strip()

    stubs = [
        "https://vk.com/images/camera_200.png",
        "https://vk.com/images/camera_100.png",
        "https://vk.com/images/deactivated_200.png",
        "https://vk.com/images/deactivated_cropped_200.png",
        "https://vk.com/images/question_200.png",
        "https://vk.com/images/community_200.png",
    ]

    if not avatar_url or any(avatar_url.startswith(stub) for stub in stubs):
        return False

    avatar_path = os.path.join(base_dir, "Avatar.jpg")
    try:
        resp = requests.get(avatar_url, timeout=20, stream=True)
        if resp.status_code == 200:
            ct = resp.headers.get("content-type", "").lower()
            if "image" in ct or "octet-stream" in ct:
                with open(avatar_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        if chunk:
                            f.write(chunk)

                # JSON для аватара
                avatar_json = build_entity_json(
                    entity,
                    avatar_url,
                    album_title="Аватар",
                    album_id=-6,
                    is_group=is_group,
                )
                json_path = os.path.join(base_dir, "Avatar.json")
                write_json(json_path, avatar_json)
                return True
    except Exception as e:
        print(f"⚠️ Ошибка сохранения аватара: {e}")
    return False


def log_user_processed(NameUser: str, user_id: str, groupe: bool = False):
    if groupe:
        user_id = user_id.replace("-", "")
    log_file = str(LOG_FILE)
    try:
        if os.path.exists(log_file):
            with open(log_file, "r", encoding="utf-8") as f:
                if groupe:
                    if any(f"http://vk.com/club{user_id}" in line for line in f):
                        return
                else:
                    if any(f"http://vk.com/id{user_id}" in line for line in f):
                        return
        with open(log_file, "a", encoding="utf-8") as f:
            if groupe:
                f.write(f"{NameUser}, http://vk.com/club{user_id}\n")
            else:
                f.write(f"{NameUser}, http://vk.com/id{user_id}\n")
    except Exception as e:
        print(f"⚠️ Log error: {e}")


# ============================================================
# ОПРЕДЕЛЕНИЕ ТИПА СУЩНОСТИ
# ============================================================

def get_vk_entity(identifier):
    """
    Определяет тип сущности по ID (число или строка) или screen_name.
    Возвращает dict: {'type': 'user'|'group'|'unknown', 'id': ..., 'name': ...}
    """
    headers = {'User-Agent': 'VKEntityResolver/1.0'}

    # --- Шаг 1: попытка как число ---
    try:
        user_or_group_id = int(identifier)
        if user_or_group_id < 0:
            resp = requests.get(f'{BASE_URL}/groups.getById', params={
                'group_ids': str(-user_or_group_id),
                'v': API_VERSION,
                'access_token': reload_tokens(),
                'fields': 'screen_name'
            }, headers=headers).json()
            if 'response' in resp and resp['response']:
                group = resp['response'][0]
                return {
                    'type': 'group',
                    'id': -group['id'],
                    'name': group.get('name') or group.get('screen_name')
                }
            return {'type': 'unknown', 'id': user_or_group_id, 'name': None}
        else:
            resp = requests.get(f'{BASE_URL}/users.get', params={
                'user_ids': str(user_or_group_id),
                'v': API_VERSION,
                'access_token': reload_tokens(),
                'fields': 'screen_name'
            }, headers=headers).json()
            if 'response' in resp and resp['response']:
                user = resp['response'][0]
                return {
                    'type': 'user',
                    'id': user['id'],
                    'name': f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('screen_name')
                }
            resp = requests.get(f'{BASE_URL}/groups.getById', params={
                'group_ids': str(user_or_group_id),
                'v': API_VERSION,
                'access_token': reload_tokens(),
                'fields': 'screen_name'
            }, headers=headers).json()
            if 'response' in resp and resp['response']:
                group = resp['response'][0]
                return {
                    'type': 'group',
                    'id': -group['id'],
                    'name': group.get('name') or group.get('screen_name')
                }
            return {'type': 'unknown', 'id': user_or_group_id, 'name': None}
    except (ValueError, TypeError):
        pass

    # --- Шаг 2: resolveScreenName ---
    resolve_resp = requests.get(f'{BASE_URL}/utils.resolveScreenName', params={
        'screen_name': str(identifier),
        'v': API_VERSION,
        'access_token': reload_tokens()
    }, headers=headers).json()

    obj = resolve_resp.get('response', {})
    if obj:
        obj_type = obj.get('type')
        obj_id = obj.get('object_id')
        if obj_type == 'user':
            return {'type': 'user', 'id': obj_id, 'name': str(identifier)}
        elif obj_type == 'group':
            return {'type': 'group', 'id': -obj_id, 'name': str(identifier)}
        elif obj_type == 'application':
            return {'type': 'application', 'id': obj_id, 'name': str(identifier)}
        else:
            return {'type': 'unknown', 'id': None, 'name': str(identifier)}

    # --- Шаг 3: явно как группа ---
    group_resp = requests.get(f'{BASE_URL}/groups.getById', params={
        'group_ids': str(identifier),
        'v': API_VERSION,
        'access_token': reload_tokens(),
        'fields': 'screen_name'
    }, headers=headers).json()
    if 'response' in group_resp and group_resp['response']:
        group = group_resp['response'][0]
        return {
            'type': 'group',
            'id': -group['id'],
            'name': group.get('name') or group.get('screen_name')
        }

    # --- Шаг 4: явно как пользователь ---
    user_resp = requests.get(f'{BASE_URL}/users.get', params={
        'user_ids': str(identifier),
        'v': API_VERSION,
        'access_token': reload_tokens(),
        'fields': 'screen_name'
    }, headers=headers).json()
    if 'response' in user_resp and user_resp['response']:
        user = user_resp['response'][0]
        return {
            'type': 'user',
            'id': user['id'],
            'name': f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or user.get('screen_name')
        }

    return {'type': 'unknown', 'id': None, 'name': str(identifier)}


# ============================================================
# СБОР И СКАЧИВАНИЕ ФОТО (для пользователя или группы)
# ============================================================

def process_entity(
    entity: dict, base_dir: str, is_group: bool, MAX_WORKERS: int,
    importer=None, delete_processed_files: bool = False, import_to_database: bool = True,
):
    """Скачивает только аватар и возвращает пути для импорта из рабочего потока."""
    raw_id = entity.get("id")
    if is_group:
        uid = -abs(int(raw_id))
        name = str(entity.get("name") or f"group_{abs(uid)}").strip()
        display_id = str(uid)
    else:
        uid = int(raw_id)
        name = f"{entity.get('last_name', '')} {entity.get('first_name', '')}".strip()
        name = name or entity.get("screen_name") or f"user_{uid}"
        display_id = str(uid)

    with EXISTING_PROFILE_IDS_LOCK:
        already_in_db = abs(uid) in EXISTING_PROFILE_IDS
    if not is_group and already_in_db:
        print(f"[DB-SKIP] Профиль VK ID {abs(uid)} уже есть в profiles.profile_url активной БД; аватар пропущен.")
        return

    print(f"\n{'=' * 60}")
    print(f"👤 {name} | ID: {display_id}")
    post_ui_event("status", f"Скачивание аватара VK ID {display_id}…")

    if not save_avatar_to_root(entity, base_dir, is_group=is_group):
        print("🖼️ Аватар отсутствует или VK не предоставил доступ к изображению.")
        log_user_processed(name, display_id, is_group)
        post_ui_event("status", f"VK ID {display_id}: аватар отсутствует")
        return

    avatar_path = Path(base_dir) / "Avatar.jpg"
    avatar_json = Path(base_dir) / "Avatar.json"
    print(f"🖼️ Аватар сохранён: {avatar_path}")
    # Импорт в БД выполняется вызывающим потоком-владельцем соединения.
    # Это важно для SQLite: worker-потоки занимаются сетью и записью файлов, но не DB connection.
    post_ui_event("status", f"Аватар VK ID {display_id} скачан; передача в FaceSearch…")
    return (avatar_path, avatar_json, abs(uid), name)


def process_users_with_friends(
    root_users: list, depth: int, MAX_WORKERS: int,
    base_output_dir: str | Path | None = None, importer=None,
    delete_processed_files: bool = False, import_to_database: bool = True,
):
    """Скачивает корневых пользователей и их друзей до заданной глубины.

    depth=0 — только введённые ID; depth=1 — также друзья этих пользователей;
    depth=2 — дополнительно друзья друзей и т. д. Каждый ID обрабатывается один раз.
    """
    output_root = Path(base_output_dir or DEFAULT_OUTPUT_DIR).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    visited = set()
    current_level = []
    root_ids = set()
    for user in root_users:
        try:
            uid = int(user.get("id"))
        except (TypeError, ValueError):
            continue
        if uid > 0 and uid not in root_ids:
            user["_friend_depth"] = 0
            current_level.append(user)
            root_ids.add(uid)

    for level in range(depth + 1):
        if STOP_REQUESTED.is_set():
            print("🛑 Остановка запрошена; обход друзей прекращается.")
            break
        # Убираем повторы на текущем уровне и профили, уже встреченные выше.
        unique_level = []
        level_ids = set()
        for user in current_level:
            try:
                uid = int(user.get("id"))
            except (TypeError, ValueError):
                continue
            if uid <= 0 or uid in visited or uid in level_ids:
                continue
            if uid in EXISTING_PROFILE_IDS:
                print(f"[DB-SKIP] ID {uid} уже есть в profiles.profile_url активной БД; пропускаем.")
                continue
            level_ids.add(uid)
            visited.add(uid)
            unique_level.append(user)

        if not unique_level:
            print(f"\nℹ️ На уровне {level} новых пользователей нет.")
            break

        print("\n" + "=" * 68)
        print(f"👥 Уровень вложенности {level}: пользователей к обработке — {len(unique_level)}")
        print("=" * 68)

        for index, user in enumerate(unique_level, start=1):
            if STOP_REQUESTED.is_set():
                print("🛑 Обработка уровня остановлена по запросу пользователя.")
                break
            uid = int(user["id"])
            user["_friend_depth"] = level
            name = f"{user.get('last_name', '')} {user.get('first_name', '')}".strip()
            name = name or user.get("screen_name") or f"user_{uid}"
            folder_name = sanitize_filename(f"{name} ({uid})")
            base_dir = str(output_root / folder_name)
            os.makedirs(base_dir, exist_ok=True)
            print(f"\n[{index}/{len(unique_level)}] Уровень {level}: {name} (ID {uid})")
            try:
                process_entity(
                    user, base_dir, is_group=False, MAX_WORKERS=MAX_WORKERS,
                    importer=importer, delete_processed_files=delete_processed_files,
                    import_to_database=import_to_database,
                )
            except Exception as e:
                print(f"💥 Ошибка обработки пользователя {uid}: {e}")

        if level >= depth:
            break

        next_level = {}
        print(f"\n🔎 Получаем друзей пользователей уровня {level}...")
        for index, user in enumerate(unique_level, start=1):
            if STOP_REQUESTED.is_set():
                print("🛑 Обработка уровня остановлена по запросу пользователя.")
                break
            uid = int(user["id"])
            try:
                with requests.Session() as session:
                    friends = get_user_friends(uid, session)
                added = 0
                for friend in friends:
                    try:
                        friend_id = int(friend.get("id"))
                    except (AttributeError, TypeError, ValueError):
                        continue
                    if (friend_id > 0 and friend_id not in visited
                            and friend_id not in next_level
                            and friend_id not in EXISTING_PROFILE_IDS):
                        friend["_friend_depth"] = level + 1
                        next_level[friend_id] = friend
                        added += 1
                print(f"   [{index}/{len(unique_level)}] ID {uid}: друзей получено {len(friends)}, новых {added}")
            except Exception as e:
                print(f"⚠️ Не удалось получить друзей ID {uid}: {e}")

        print(f"✅ Уровень {level + 1}: уникальных пользователей найдено — {len(next_level)}")
        current_level = list(next_level.values())

    print(f"\n🏁 Обход завершён. Уникальных пользователей обработано: {len(visited)}")


# ============================================================
# ИНТЕГРАЦИЯ С FACESEARCH (необязательная)
# ============================================================


def load_face_search_config(settings_xml: str | Path, profiles_json: str | Path) -> dict:
    """Определяет активный профиль FaceSearch по settings.xml и database_profiles.json."""
    settings_path = Path(settings_xml).expanduser().resolve()
    profiles_path = Path(profiles_json).expanduser().resolve()
    if not settings_path.is_file():
        raise FileNotFoundError(f"Не найден settings.xml: {settings_path}")
    if not profiles_path.is_file():
        raise FileNotFoundError(f"Не найден database_profiles.json: {profiles_path}")
    xml_root = ET.parse(settings_path).getroot()
    data = json.loads(profiles_path.read_text(encoding="utf-8-sig"))
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, list):
        raise ValueError("В database_profiles.json отсутствует список profiles.")
    profiles = [item for item in profiles if isinstance(item, dict) and str(item.get("name") or "").strip()]
    if not profiles:
        raise ValueError("В database_profiles.json нет профилей.")
    selected = str(xml_root.findtext("database/profile") or "").strip()
    active = str(data.get("active") or "").strip()
    by_name = {str(item.get("name")): item for item in profiles}
    if active not in by_name:
        active = selected if selected in by_name else str(profiles[0]["name"])
    backend = str(xml_root.findtext("compute/backend") or "gpu").strip().lower()
    root_dir = find_face_search_root(settings_path, profiles_path)
    return {
        "settings_path": settings_path,
        "profiles_path": profiles_path,
        "active_name": active,
        "profile": dict(by_name[active]),
        "backend": backend,
        "root": root_dir,
    }


def find_face_search_root(settings_path: Path, profiles_path: Path) -> Path:
    candidates = []
    for base in (profiles_path.parent, settings_path.parent, APP_DIR):
        for candidate in (base, *base.parents):
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        if (candidate / "face_search_gui.py").is_file() and (candidate / "facesearch").is_dir():
            return candidate.resolve()
    raise FileNotFoundError(
        "Не найден корневой каталог FaceSearch (нужны face_search_gui.py и папка facesearch)."
    )


class FaceSearchImporter:
    """Импортирует скачанные изображения через штатную функцию FaceSearch."""
    def __init__(
        self, facesearch_dir: str | Path, status_callback,
        create_crop_files: bool = True, check_only: bool = False,
    ):
        self.status_callback = status_callback
        self.create_crop_files = bool(create_crop_files)
        self.check_only = bool(check_only)
        directory = Path(facesearch_dir).expanduser()
        self.config = load_face_search_config(directory / "settings.xml", directory / "database_profiles.json")
        self.module = self._load_module(self.config["root"])
        self.conn = None
        self.app = None
        self.imported_count = 0
        self._import_lock = threading.RLock()
        profile = self.config["profile"]
        profile_type = str(profile.get("type") or "sqlite").strip().lower()
        if profile_type == "sqlite":
            database_path = Path(str(profile.get("path") or self.config["root"] / "faces.db")).expanduser()
            if not database_path.is_absolute():
                database_path = self.config["root"] / database_path
            if not database_path.is_file():
                raise FileNotFoundError(f"Активная SQLite-база FaceSearch не найдена: {database_path}")
            profile["path"] = str(database_path.resolve())
        target = str(profile.get("database") or profile.get("path") or "")
        self.status_callback(f"Подключение к FaceSearch: {self.config['active_name']} ({profile_type}: {target})…")
        try:
            self.conn = self.module.create_database_for_profile(
                profile,
                maintenance=False,
                create_if_missing=False,
                activate_storage=True,
                initialize=False,
                check_crop_migration=False,
            )
            if not self.check_only:
                ensure_columns = getattr(self.module, "_ensure_columns", None)
                if callable(ensure_columns):
                    ensure_columns(self.conn, "images", {"source_key": "VARCHAR(255)"})
                    self.conn.commit()
                self.conn.execute("SELECT id, source_type, url_image, source_key FROM images WHERE 1 = 0").fetchall()
            else:
                # При одной проверке профилей не меняем схему БД.
                self.conn.execute("SELECT profile_url FROM profiles WHERE 1 = 0").fetchall()
        except Exception:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
                self.conn = None
            raise

    @staticmethod
    def _load_module(root: Path):
        root = root.resolve()
        existing = sys.modules.get("face_search_gui")
        if existing is not None and Path(getattr(existing, "__file__", "")).resolve().parent != root:
            raise RuntimeError("Уже загружен другой face_search_gui.py. Перезапустите загрузчик.")
        if existing is None:
            root_text = str(root)
            if root_text not in sys.path:
                sys.path.insert(0, root_text)
            existing = importlib.import_module("face_search_gui")
        if Path(existing.__file__).resolve().parent != root:
            raise RuntimeError("FaceSearch загружен не из выбранного каталога.")
        return existing

    def import_image(self, image_path: Path, json_path: Path, source_key: str):
        """Сериализует доступ к общей модели и соединению с БД между потоками загрузки."""
        with self._import_lock:
            return self._import_image_locked(image_path, json_path, source_key)

    def _import_image_locked(self, image_path: Path, json_path: Path, source_key: str):
        if self.conn is None:
            raise RuntimeError("Соединение с базой FaceSearch закрыто.")
        if not image_path.is_file() or not json_path.is_file():
            raise FileNotFoundError(f"Не найдена пара изображения и JSON: {image_path}")
        metadata = json.loads(json_path.read_text(encoding="utf-8-sig"))
        if not isinstance(metadata, dict):
            raise ValueError(f"Некорректные метаданные: {json_path.name}")
        if self.app is None:
            self.status_callback("Инициализация модели распознавания FaceSearch…")
            backend_value = self.config.get("backend", "gpu")
            backend = self.module.BACKEND_CPU if backend_value in {"cpu", "processor", "процессор"} else self.module.BACKEND_GPU
            self.app = self.module.create_face_app(backend=backend)
        self.status_callback(f"Импорт в FaceSearch: {image_path.name}…")
        clean = dict(metadata)
        info = dict(metadata.get("info") or {}) if isinstance(metadata.get("info"), dict) else {}
        for key in (_META_RESOLUTION, _META_VERSION):
            info.pop(key, None)
        clean["info"] = info
        result = self.module.import_image_bundle(
            self.conn,
            self.app,
            image_path,
            json_path,
            delete_source=False,
            create_crop_files=self.create_crop_files,
            overwrite_existing_crops=False,
            metadata=clean,
            source_key=source_key,
        )
        self.imported_count += 1
        return result

    def build_index(self):
        if self.conn is None:
            raise RuntimeError("Соединение с базой FaceSearch закрыто.")
        builder = getattr(self.module, "build_faiss_index", None)
        if not callable(builder):
            raise RuntimeError("В загруженном FaceSearch нет build_faiss_index().")
        self.status_callback("Создание индекса FAISS…")
        builder(self.conn)
        self.status_callback("Индекс FAISS создан.")

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


# ============================================================
# GUI + ОРКЕСТРАЦИЯ ЗАДАНИЯ
# ============================================================


def normalize_vk_identifier(value: str) -> str:
    """Нормализует VK URL, короткое имя или ID для последующего разрешения сущности."""
    value = (value or "").strip().strip(",; ")
    if not value:
        return ""
    if value.lower().startswith(("http://", "https://")):
        parsed = urlparse(value)
        path = unquote(parsed.path).strip("/")
        parts = [part for part in path.split("/") if part]
        if not parts:
            return ""
        item = parts[0]
        if item.lower().startswith("id") and item[2:].isdigit():
            return item[2:]
        for prefix in ("club", "public", "group"):
            if item.lower().startswith(prefix) and item[len(prefix):].isdigit():
                return str(-int(item[len(prefix):]))
        if item.isdigit():
            return item
        return item
    low = value.lower()
    if low.startswith("id") and value[2:].isdigit():
        return value[2:]
    for prefix in ("club", "public", "group"):
        if low.startswith(prefix) and value[len(prefix):].isdigit():
            return str(-int(value[len(prefix):]))
    return value


def vk_user_id_from_profile_url(value: str) -> int | None:
    """Извлекает ID из URL профиля VK, учитывая домены vk.com и vk.ru."""
    raw = str(value or "").strip()
    if not raw:
        return None
    try:
        parsed = urlparse(raw if "://" in raw else "https://" + raw.lstrip("/"))
    except Exception:
        return None
    host = (parsed.hostname or "").lower()
    if host.startswith("www."):
        host = host[4:]
    if host not in {"vk.com", "vk.ru"}:
        return None
    path = unquote(parsed.path or "").strip("/")
    match = re.fullmatch(r"id(\d+)", path, flags=re.IGNORECASE)
    if match:
        return int(match.group(1))
    return None


def vk_user_id_from_identifier(value: str) -> int | None:
    """Возвращает числовой ID из непосредственной ссылки/идентификатора VK."""
    normalized = normalize_vk_identifier(value)
    if normalized.isdigit():
        return int(normalized)
    return None


def load_existing_vk_profile_ids(importer) -> set[int]:
    """Читает profile_url из БД активного профиля и нормализует ID пользователей VK."""
    if importer is None or getattr(importer, "conn", None) is None:
        return set()
    try:
        rows = importer.conn.execute(
            "SELECT profile_url FROM profiles WHERE profile_url IS NOT NULL"
        ).fetchall()
    except Exception as exc:
        raise RuntimeError(
            "Не удалось проверить profiles.profile_url в активной базе FaceSearch. "
            "Загрузка остановлена, чтобы не обрабатывать профили повторно."
        ) from exc
    result: set[int] = set()
    for row in rows:
        try:
            profile_id = vk_user_id_from_profile_url(row[0])
        except (IndexError, TypeError, KeyError):
            profile_id = None
        if profile_id is not None:
            result.add(profile_id)
    return result


def run_download_job(
    start_id: int, max_workers: int, output_dir: str,
    importer=None, delete_processed_files: bool = False, import_to_database: bool = True,
):
    """Непрерывно обрабатывает числовые ID по возрастанию пакетами users.get."""
    global EXISTING_PROFILE_IDS
    start_id = int(start_id)
    if start_id < 1:
        raise ValueError("Стартовый ID должен быть положительным числом.")

    EXISTING_PROFILE_IDS = load_existing_vk_profile_ids(importer)
    if importer is not None:
        print(
            f"[DB-CHECK] Активный профиль FaceSearch: {importer.config['active_name']}; "
            f"загружено ID для пропуска: {len(EXISTING_PROFILE_IDS)}."
        )

    _load_tokens_if_needed()
    output_root = Path(output_dir).expanduser().resolve()
    output_root.mkdir(parents=True, exist_ok=True)
    worker_count = max(1, min(64, int(max_workers)))
    current_id = start_id
    checked_total = 0
    skipped_total = 0
    returned_total = 0
    avatars_saved_total = 0
    errors_total = 0

    print(f"🖼️ VK Avatar Downloader — каталог: {output_root}")
    print(
        f"▶ Начальный ID: {start_id}; потоков загрузки: {worker_count}; "
        f"пачка users.get: до {USER_BATCH_SIZE} ID."
    )
    print("ℹ️ Перебор непрерывный и работает до нажатия «Остановить».")

    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="VKAvatar") as executor:
            while not STOP_REQUESTED.is_set():
                batch_start = current_id
                batch_end = batch_start + USER_BATCH_SIZE
                batch_ids = list(range(batch_start, batch_end))

                # Существующие профили исключаем ещё до обращения к VK API.
                with EXISTING_PROFILE_IDS_LOCK:
                    request_ids = [
                        uid for uid in batch_ids
                        if uid not in EXISTING_PROFILE_IDS
                    ]
                skipped = len(batch_ids) - len(request_ids)
                skipped_total += skipped
                checked_total += len(batch_ids)

                post_ui_event(
                    "status",
                    f"Проверка ID {batch_start}–{batch_end - 1}; "
                    f"найдено в БД для пропуска: {skipped}."
                )
                post_ui_event(
                    "progress", 0, 0,
                    f"ID {batch_start}–{batch_end - 1} | "
                    f"проверено: {checked_total:,} | пропущено по БД: {skipped_total:,}"
                )

                returned_users = []
                if request_ids:
                    # POST позволяет передать полный пакет ID без чрезмерно длинного URL.
                    returned_users = get_user_info(",".join(map(str, request_ids)), session)

                users_by_id = {}
                for user in returned_users:
                    try:
                        uid = int(user.get("id"))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if uid in request_ids:
                        users_by_id[uid] = user
                returned_total += len(users_by_id)
                missing_count = len(request_ids) - len(users_by_id)
                print(
                    f"[BATCH] ID {batch_start}–{batch_end - 1}: "
                    f"профилей в БД пропущено {skipped}; "
                    f"запрошено через VK {len(request_ids)}; "
                    f"получено пользователей {len(users_by_id)}; "
                    f"не найдено VK {max(0, missing_count)}."
                )

                tasks = []
                # Список задач упорядочен по ID; одновременно в работу отправляется только число потоков.
                for uid in request_ids:
                    user = users_by_id.get(uid)
                    if user is None:
                        continue
                    if user.get("deactivated"):
                        print(f"[VK-SKIP] ID {uid}: профиль деактивирован ({user.get('deactivated')}).")
                        continue
                    display_name = (
                        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                        or user.get("screen_name")
                        or f"user_{uid}"
                    )
                    folder_name = sanitize_filename(f"{display_name} ({uid})")
                    base_dir = str(output_root / folder_name)
                    os.makedirs(base_dir, exist_ok=True)
                    tasks.append((uid, user, base_dir))

                next_task_index = 0
                pending = {}
                batch_errors = 0

                def submit_next_tasks():
                    nonlocal next_task_index
                    while (
                        not STOP_REQUESTED.is_set()
                        and next_task_index < len(tasks)
                        and len(pending) < worker_count
                    ):
                        uid, user, base_dir = tasks[next_task_index]
                        next_task_index += 1
                        future = executor.submit(
                            process_entity,
                            user,
                            base_dir,
                            False,
                            worker_count,
                            None,   # соединение FaceSearch не передаётся в worker-потоки
                            False,
                            False,  # только скачивание; импорт выполняет текущий поток
                        )
                        pending[future] = uid

                submit_next_tasks()
                while pending:
                    completed_futures, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed_futures:
                        uid = pending.pop(future)
                        try:
                            download_result = future.result()
                            if download_result:
                                image_path, json_path, downloaded_uid, downloaded_name = download_result
                                try:
                                    if importer is not None and import_to_database:
                                        # Импортируем по готовности файлов, в потоке, где создано соединение с БД.
                                        import_result = importer.import_image(
                                            image_path,
                                            json_path,
                                            source_key=f"vk:{downloaded_uid}:avatar",
                                        )
                                        with EXISTING_PROFILE_IDS_LOCK:
                                            EXISTING_PROFILE_IDS.add(abs(int(downloaded_uid)))
                                        print(
                                            f"[DB-IMPORT] Аватар {downloaded_uid}: "
                                            f"импортирован; лиц: {import_result.get('faces', 0)}."
                                        )
                                        if delete_processed_files:
                                            image_path.unlink(missing_ok=True)
                                            json_path.unlink(missing_ok=True)
                                except Exception as exc:
                                    batch_errors += 1
                                    errors_total += 1
                                    print(
                                        f"[DB-ERROR] ID {downloaded_uid}: аватар скачан, "
                                        f"но не удалось записать в FaceSearch: {exc}"
                                    )
                                    continue
                                # Отмечаем ID завершённым только после успешного импорта
                                # (или после скачивания, если импорт выключен).
                                log_user_processed(downloaded_name, str(downloaded_uid), False)
                        except Exception as exc:
                            batch_errors += 1
                            errors_total += 1
                            print(f"[ERROR] ID {uid}: {exc}")
                    # При нажатии «Остановить» новые задания не добавляются в пул.
                    submit_next_tasks()

                if STOP_REQUESTED.is_set():
                    # Если часть задач пачки ещё не была отправлена, повторяем её с начала.
                    # Успешно импортированные ID при повторном запуске будут пропущены по БД.
                    current_id = batch_start if (next_task_index < len(tasks) or batch_errors) else batch_end
                    break

                avatars_saved_total += len(tasks) - batch_errors
                print(
                    f"[TOTAL] проверено ID: {checked_total:,}; "
                    f"пропущено по БД: {skipped_total:,}; "
                    f"профилей получено: {returned_total:,}; "
                    f"пользователей обработано без исключений: {avatars_saved_total:,}; "
                    f"ошибок обработки: {errors_total:,}."
                )
                current_id = batch_end

    print(
        f"🏁 Остановлено на границе пачки. Следующий стартовый ID: {current_id}. "
        f"Проверено ID: {checked_total:,}; пропущено по БД: {skipped_total:,}; "
        f"пользователей получено от VK: {returned_total:,}; ошибок: {errors_total:,}."
    )
    return current_id


class QueueStream:
    """Перенаправляет stdout/stderr в очередь GUI без обращения к Tkinter из потоков."""
    def __init__(self, events, kind="log"):
        self.events = events
        self.kind = kind
        self.local = threading.local()

    def write(self, text):
        if not text:
            return 0
        buffer = getattr(self.local, "buffer", "") + str(text)
        chunks = re.split(r"(?<=\n)", buffer)
        if chunks and not chunks[-1].endswith("\n"):
            self.local.buffer = chunks.pop()
        else:
            self.local.buffer = ""
        for chunk in chunks:
            if chunk:
                self.events.put((self.kind, chunk))
        return len(text)

    def flush(self):
        buffer = getattr(self.local, "buffer", "")
        if buffer:
            self.events.put((self.kind, buffer))
            self.local.buffer = ""

    def isatty(self):
        return False


def main():
    global UI_EVENTS
    events = queue.Queue()
    UI_EVENTS = events

    root = tk.Tk()
    root.title("VK.ru — загрузчик аватарок")
    root.geometry("1100x960")
    root.minsize(900, 700)

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = QueueStream(events, "log")
    sys.stderr = QueueStream(events, "log")

    # Верхняя часть формы прокручивается; выполнение, журнал и кнопки закреплены снизу.
    main_frame = ttk.Frame(root, padding=8)
    main_frame.pack(fill="both", expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(0, weight=3)
    main_frame.rowconfigure(1, weight=2)

    form_host = ttk.Frame(main_frame)
    form_host.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
    form_host.columnconfigure(0, weight=1)
    form_host.rowconfigure(0, weight=1)

    form_canvas = tk.Canvas(form_host, highlightthickness=0, borderwidth=0)
    form_scrollbar = ttk.Scrollbar(form_host, orient="vertical", command=form_canvas.yview)
    form_canvas.configure(yscrollcommand=form_scrollbar.set)
    form_canvas.grid(row=0, column=0, sticky="nsew")
    form_scrollbar.grid(row=0, column=1, sticky="ns")

    outer = ttk.Frame(form_canvas, padding=4)
    outer.columnconfigure(0, weight=1)
    form_window_id = form_canvas.create_window((0, 0), window=outer, anchor="nw")

    def update_form_scrollregion(_event=None):
        form_canvas.configure(scrollregion=form_canvas.bbox("all"))

    def resize_form_content(event):
        form_canvas.itemconfigure(form_window_id, width=event.width)

    outer.bind("<Configure>", update_form_scrollregion)
    form_canvas.bind("<Configure>", resize_form_content)

    def scroll_form_mousewheel(event):
        # Не перехватываем прокрутку журнала и других элементов нижней панели.
        widget = root.winfo_containing(event.x_root, event.y_root)
        while widget is not None and widget is not form_canvas:
            widget = getattr(widget, "master", None)
        if widget is not form_canvas:
            return None
        delta = getattr(event, "delta", 0)
        if delta:
            units = int(-delta / 120)
            if units == 0:
                units = -1 if delta > 0 else 1
            form_canvas.yview_scroll(units, "units")
        elif getattr(event, "num", None) == 4:
            form_canvas.yview_scroll(-1, "units")
        elif getattr(event, "num", None) == 5:
            form_canvas.yview_scroll(1, "units")
        return "break"

    root.bind_all("<MouseWheel>", scroll_form_mousewheel, add="+")
    root.bind_all("<Button-4>", scroll_form_mousewheel, add="+")
    root.bind_all("<Button-5>", scroll_form_mousewheel, add="+")

    ttk.Label(
        outer,
        text="Загрузка аватарок по последовательным ID VK. Существующие в активной базе FaceSearch ID пропускаются; остановить бесконечный перебор можно кнопкой «Остановить».",
        wraplength=980,
    ).grid(row=0, column=0, sticky="w", pady=(0, 8))

    source_frame = ttk.LabelFrame(outer, text="Последовательный обход ID", padding=8)
    source_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    source_frame.columnconfigure(1, weight=1)
    ttk.Label(source_frame, text="Стартовый ID пользователя:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
    start_id_var = tk.StringVar(value="")
    ttk.Entry(source_frame, textvariable=start_id_var, width=20).grid(row=0, column=1, sticky="w", pady=3)
    ttk.Label(
        source_frame,
        text="Сканирование продолжается по возрастанию ID до остановки. Данные запрашиваются пакетами по 1000 ID; существующие в БД не отправляются в VK API.",
        wraplength=980,
    ).grid(row=1, column=0, columnspan=2, sticky="w", pady=(4, 0))

    settings_frame = ttk.LabelFrame(outer, text="Параметры загрузки", padding=8)
    settings_frame.grid(row=2, column=0, sticky="ew", pady=(0, 8))
    settings_frame.columnconfigure(2, weight=1)
    ttk.Label(settings_frame, text="Потоков загрузки:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
    workers_var = tk.StringVar(value="8")
    ttk.Spinbox(settings_frame, from_=1, to=64, textvariable=workers_var, width=8).grid(row=0, column=1, sticky="w", pady=3)
    ttk.Label(
        settings_frame,
        text="ID передаются на обработку по порядку, скачивание аватаров выполняется параллельно.",
        wraplength=850,
    ).grid(row=0, column=2, sticky="w", padx=(12, 0), pady=3)

    output_frame = ttk.LabelFrame(outer, text="Каталог сохранения", padding=8)
    output_frame.grid(row=3, column=0, sticky="ew", pady=(0, 8))
    output_frame.columnconfigure(0, weight=1)
    output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
    ttk.Entry(output_frame, textvariable=output_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))

    def browse_output():
        current = output_var.get().strip()
        selected = filedialog.askdirectory(
            parent=root,
            title="Выберите каталог для скачанных аватарок",
            initialdir=current if current and Path(current).expanduser().is_dir() else str(APP_DIR),
            mustexist=False,
        )
        if selected:
            output_var.set(selected)

    ttk.Button(output_frame, text="Обзор…", command=browse_output).grid(row=0, column=1)

    face_frame = ttk.LabelFrame(outer, text="Интеграция с FaceSearch", padding=8)
    face_frame.grid(row=4, column=0, sticky="ew", pady=(0, 8))
    face_frame.columnconfigure(1, weight=1)
    facesearch_dir_var = tk.StringVar(value=str(DEFAULT_FACESEARCH_DIR))
    auto_import_var = tk.BooleanVar(value=True)
    create_crop_var = tk.BooleanVar(value=True)
    build_index_var = tk.BooleanVar(value=False)
    delete_processed_var = tk.BooleanVar(value=False)
    ttk.Label(face_frame, text="Каталог FaceSearch:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(face_frame, textvariable=facesearch_dir_var).grid(row=0, column=1, sticky="ew", pady=3)

    def browse_facesearch():
        current = facesearch_dir_var.get().strip()
        selected = filedialog.askdirectory(
            parent=root,
            title="Выберите каталог FaceSearch",
            initialdir=current if current and Path(current).expanduser().is_dir() else str(APP_DIR.parent.parent),
            mustexist=True,
        )
        if selected:
            facesearch_dir_var.set(selected)
            save_gui_settings()

    ttk.Button(face_frame, text="Обзор…", command=browse_facesearch).grid(row=0, column=2, padx=(8, 0), pady=3)
    auto_import_check = ttk.Checkbutton(
        face_frame,
        text="Автоматически импортировать загруженные аватарки в активный профиль FaceSearch",
        variable=auto_import_var,
        command=lambda: (update_import_state(), save_gui_settings()),
    )
    auto_import_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=(4, 2))
    create_crop_check = ttk.Checkbutton(
        face_frame, text="Создавать CROP-файлы", variable=create_crop_var,
        command=lambda: save_gui_settings(),
    )
    create_crop_check.grid(row=2, column=0, columnspan=3, sticky="w", pady=1)
    build_index_check = ttk.Checkbutton(
        face_frame, text="Построить индекс FAISS после завершения импорта", variable=build_index_var,
        command=lambda: save_gui_settings(),
    )
    build_index_check.grid(row=3, column=0, columnspan=3, sticky="w", pady=1)
    delete_processed_check = ttk.Checkbutton(
        face_frame, text="Удалять исходные изображения и JSON после успешного импорта", variable=delete_processed_var,
        command=lambda: save_gui_settings(),
    )
    delete_processed_check.grid(row=4, column=0, columnspan=3, sticky="w", pady=1)

    auth_frame = ttk.LabelFrame(outer, text="Авторизация VK", padding=8)
    auth_frame.grid(row=5, column=0, sticky="ew", pady=(0, 8))
    auth_frame.columnconfigure(1, weight=1)
    browser_profile_var = tk.StringVar(value=str(DEFAULT_BROWSER_PROFILE_DIR))
    chrome_exe_var = tk.StringVar(value=default_chrome_executable())
    service_token_var = tk.StringVar(value=read_service_token())
    auth_status_var = tk.StringVar(value="Готово к автоматической авторизации.")
    token_count_var = tk.StringVar(value=f"Токенов в {VK_TOKEN_FILE.name}: {len(_read_token_file())}")
    ttk.Label(auth_frame, textvariable=token_count_var).grid(row=0, column=0, sticky="w", pady=(0, 4))
    auth_buttons = ttk.Frame(auth_frame)
    auth_buttons.grid(row=1, column=0, columnspan=3, sticky="ew", pady=(0, 8))

    def load_gui_settings():
        defaults = {
            "output_dir": str(DEFAULT_OUTPUT_DIR),
            "facesearch_dir": str(DEFAULT_FACESEARCH_DIR),
            "auto_import": True,
            "create_crop_files": True,
            "build_index_after": False,
            "delete_processed_files": False,
            "start_id": "",
            "workers": 8,
            "browser_profile_dir": str(DEFAULT_BROWSER_PROFILE_DIR),
            "chrome_exe_path": default_chrome_executable(),
        }
        try:
            if APP_SETTINGS_PATH.is_file():
                saved = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
                if isinstance(saved, dict):
                    for key in defaults:
                        if key in saved:
                            defaults[key] = saved[key]
        except Exception as exc:
            print(f"[WARN] Не удалось прочитать настройки загрузчика: {exc}")
        output_var.set(str(defaults["output_dir"]))
        facesearch_dir_var.set(str(defaults["facesearch_dir"]))
        auto_import_var.set(bool(defaults["auto_import"]))
        create_crop_var.set(bool(defaults["create_crop_files"]))
        build_index_var.set(bool(defaults["build_index_after"]))
        delete_processed_var.set(bool(defaults["delete_processed_files"]))
        start_id_var.set(str(defaults["start_id"]))
        workers_var.set(str(defaults["workers"]))
        browser_profile_var.set(str(defaults["browser_profile_dir"]))
        chrome_exe_var.set(str(defaults["chrome_exe_path"]))
        if not service_token_var.get().strip():
            service_token_var.set(read_service_token())

    def save_gui_settings():
        payload = {
            "output_dir": output_var.get().strip(),
            "facesearch_dir": facesearch_dir_var.get().strip(),
            "auto_import": bool(auto_import_var.get()),
            "create_crop_files": bool(create_crop_var.get()),
            "build_index_after": bool(build_index_var.get()),
            "delete_processed_files": bool(delete_processed_var.get()),
            "start_id": start_id_var.get().strip(),
            "workers": workers_var.get().strip(),
            "browser_profile_dir": browser_profile_var.get().strip(),
            "chrome_exe_path": chrome_exe_var.get().strip(),
        }
        try:
            temp_path = APP_SETTINGS_PATH.with_suffix(".tmp")
            temp_path.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
            temp_path.replace(APP_SETTINGS_PATH)
        except Exception as exc:
            print(f"[WARN] Не удалось сохранить настройки загрузчика: {exc}")

    def update_import_state():
        state = "normal" if auto_import_var.get() else "disabled"
        for control in (create_crop_check, build_index_check, delete_processed_check):
            control.configure(state=state)

    load_gui_settings()
    update_import_state()
    for variable in (output_var, facesearch_dir_var, start_id_var, workers_var):
        variable.trace_add("write", lambda *_args: save_gui_settings())

    def refresh_token_count():
        count = reload_token_cache()
        token_count_var.set(f"Токенов в {VK_TOKEN_FILE.name}: {count}")

    def open_token_file():
        VK_TOKEN_FILE.parent.mkdir(parents=True, exist_ok=True)
        if not VK_TOKEN_FILE.exists():
            VK_TOKEN_FILE.write_text("", encoding="utf-8")
        try:
            if os.name == "nt":
                os.startfile(str(VK_TOKEN_FILE))
            else:
                webbrowser.open(VK_TOKEN_FILE.as_uri())
        except Exception as exc:
            messagebox.showerror("Файл токенов", f"Не удалось открыть файл:\n{exc}", parent=root)

    ttk.Button(auth_buttons, text="Вставить старый access_token…", command=lambda: open_auth_dialog()).pack(side="left", padx=(0, 6))
    ttk.Button(auth_buttons, text="Открыть vk_token.txt", command=open_token_file).pack(side="left", padx=(0, 6))
    ttk.Button(auth_buttons, text="Обновить список токенов", command=refresh_token_count).pack(side="left")

    ttk.Label(
        auth_frame,
        text="После входа VK ID загрузчик получает пользовательский API-токен через браузерную сессию. Если VK не разрешит этот способ для приложения, причина отобразится здесь; code-flow с токеном vk2 не подходит для photos.get/friends.get.",
        wraplength=980,
    ).grid(row=2, column=0, columnspan=3, sticky="ew", pady=(0, 6))

    ttk.Label(auth_frame, text="Профиль Chrome:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(auth_frame, textvariable=browser_profile_var).grid(row=3, column=1, sticky="ew", pady=3)

    def browse_browser_profile():
        current = browser_profile_var.get().strip()
        selected = filedialog.askdirectory(
            parent=root,
            title="Выберите постоянный профиль браузера для VK",
            initialdir=current if current and Path(current).expanduser().is_dir() else str(APP_DIR),
            mustexist=False,
        )
        if selected:
            browser_profile_var.set(selected)
            save_gui_settings()

    ttk.Button(auth_frame, text="Обзор…", command=browse_browser_profile).grid(row=3, column=2, padx=(8, 0), pady=3)

    ttk.Label(auth_frame, text="Путь к chrome.exe:").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(auth_frame, textvariable=chrome_exe_var).grid(row=4, column=1, sticky="ew", pady=3)

    def browse_chrome_executable():
        current = chrome_exe_var.get().strip()
        initial_dir = str(Path(current).expanduser().parent) if current and Path(current).expanduser().parent.is_dir() else str(Path.home())
        selected = filedialog.askopenfilename(
            parent=root,
            title="Выберите Chrome",
            initialdir=initial_dir,
            filetypes=[("Google Chrome", "chrome.exe"), ("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")],
        )
        if selected:
            chrome_exe_var.set(selected)
            save_gui_settings()

    ttk.Button(auth_frame, text="Обзор…", command=browse_chrome_executable).grid(row=4, column=2, padx=(8, 0), pady=3)

    ttk.Label(auth_frame, text="Service token приложения:").grid(row=5, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(auth_frame, textvariable=service_token_var, show="*").grid(row=5, column=1, sticky="ew", pady=3)

    def save_service_token(show_message=True):
        token = service_token_var.get().strip()
        if not token:
            try:
                VK_SERVICE_TOKEN_FILE.unlink(missing_ok=True)
            except OSError:
                pass
            auth_status_var.set("Service token не задан.")
            if show_message:
                messagebox.showinfo("Service token", "Поле очищено. Для обмена silent_token нужен service token приложения.", parent=root)
            return
        VK_SERVICE_TOKEN_FILE.write_text(token + "\n", encoding="utf-8")
        auth_status_var.set(f"Service token сохранён локально в {VK_SERVICE_TOKEN_FILE.name}.")
        if show_message:
            messagebox.showinfo("Service token", f"Ключ сохранён в {VK_SERVICE_TOKEN_FILE.name}; он не записывается в JSON-настройки.", parent=root)

    ttk.Button(auth_frame, text="Сохранить", command=save_service_token).grid(row=5, column=2, padx=(8, 0), pady=3)

    auth_action_frame = ttk.Frame(auth_frame)
    auth_action_frame.grid(row=6, column=0, columnspan=3, sticky="ew", pady=(6, 2))
    for column in range(3):
        auth_action_frame.columnconfigure(column, weight=1)

    def start_vk_browser_auth():
        global _AUTH_THREAD
        if sync_playwright is None:
            messagebox.showerror("Playwright не установлен", "Для автоматической авторизации установите пакет:\\npython -m pip install playwright", parent=root)
            return
        executable = Path(chrome_exe_var.get().strip()).expanduser()
        if not executable.is_file():
            messagebox.showerror("Chrome не найден", f"Проверьте путь к chrome.exe:\\n{executable}", parent=root)
            return
        profile = browser_profile_var.get().strip()
        if not profile:
            messagebox.showerror("Профиль Chrome", "Укажите каталог постоянного профиля Chrome.", parent=root)
            return
        if _AUTH_THREAD is not None and _AUTH_THREAD.is_alive():
            messagebox.showinfo("Авторизация VK", "Ожидание авторизации уже выполняется.", parent=root)
            return
        if service_token_var.get().strip():
            try:
                save_service_token(show_message=False)
            except Exception as exc:
                messagebox.showerror("Service token", f"Не удалось сохранить service token: {exc}", parent=root)
                return
        save_gui_settings()
        AUTH_STOP_REQUESTED.clear()
        auth_status_var.set("Запускаю Chrome и ожидаю ответ VK ID…")
        _AUTH_THREAD = threading.Thread(
            target=_browser_auth_worker,
            args=(profile, str(executable), service_token_var.get().strip()),
            daemon=True,
            name="VKBrowserAuthWorker",
        )
        _AUTH_THREAD.start()

    def stop_vk_browser_auth():
        AUTH_STOP_REQUESTED.set()
        auth_status_var.set("Запрошена остановка ожидания авторизации…")

    def exchange_pending_silent_token():
        service_token = service_token_var.get().strip() or read_service_token()
        if not service_token:
            messagebox.showwarning("Нужен service token", "Silent token нельзя использовать для photos.get/friends.get как API access_token. Укажите service token приложения и сохраните его.", parent=root)
            return
        if service_token_var.get().strip():
            try:
                save_service_token(show_message=False)
            except Exception as exc:
                messagebox.showerror("Service token", str(exc), parent=root)
                return
        try:
            load_pending_silent_token()
        except Exception as exc:
            messagebox.showerror("Нет сохранённого silent_token", str(exc), parent=root)
            return
        threading.Thread(
            target=_exchange_and_store_pending_token,
            args=(service_token,),
            daemon=True,
            name="VKSilentTokenExchangeWorker",
        ).start()
        auth_status_var.set("Обмениваю сохранённый silent_token на API access_token…")

    ttk.Button(auth_action_frame, text="Войти через Chrome автоматически", command=start_vk_browser_auth).grid(row=0, column=0, sticky="ew", padx=(0, 5))
    ttk.Button(auth_action_frame, text="Обменять silent_token", command=exchange_pending_silent_token).grid(row=0, column=1, sticky="ew", padx=5)
    ttk.Button(auth_action_frame, text="Остановить ожидание", command=stop_vk_browser_auth).grid(row=0, column=2, sticky="ew", padx=(5, 0))
    ttk.Label(auth_frame, textvariable=auth_status_var, wraplength=980).grid(row=7, column=0, columnspan=3, sticky="ew", pady=(4, 0))

    def open_auth_dialog():
        dialog = tk.Toplevel(root)
        dialog.title("Добавить токен VK")
        dialog.transient(root)
        dialog.geometry("720x390")
        dialog.minsize(620, 330)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text="Вставьте ранее полученный API access_token. Значение code=... из URL VK ID — временный код, его нельзя использовать как токен:",
            wraplength=680,
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        token_input = tk.Text(dialog, height=6, wrap="word", undo=True)
        token_input.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, sticky="e", padx=10, pady=(0, 10))
        save_button = ttk.Button(buttons, text="Проверить и сохранить")
        save_button.pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right")

        def submit_token():
            value = token_input.get("1.0", "end").strip()
            token = extract_access_token(value)
            if not token:
                messagebox.showerror(
                    "Токен VK",
                    "Вставьте ранее полученный API access_token или URL, содержащий access_token=. "
                    "Код code=...&type=code_v2 не является access_token; используйте «Войти через Chrome автоматически».",
                    parent=dialog,
                )
                return

            save_button.configure(state="disabled", text="Проверка…")

            def validate_worker():
                try:
                    user_name = validate_access_token(token)
                    added, count = save_access_token(token)
                    events.put(("token_saved", True, user_name, added, count, ""))
                except Exception as exc:
                    events.put(("token_saved", False, "", False, len(_read_token_file()), str(exc)))

            threading.Thread(target=validate_worker, daemon=True).start()

        save_button.configure(command=submit_token)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        # Окно не блокирует основную форму; этот диалог предназначен только для
        # добавления ранее полученного API access_token.
        dialog.focus_set()
        root._vk_token_dialog = dialog
        root._vk_token_save_button = save_button

    footer = ttk.Frame(main_frame)
    footer.grid(row=1, column=0, sticky="nsew")
    footer.columnconfigure(0, weight=1)
    footer.rowconfigure(1, weight=1)

    progress_frame = ttk.LabelFrame(footer, text="Выполнение", padding=8)
    progress_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
    progress_frame.columnconfigure(0, weight=1)
    status_var = tk.StringVar(value="Готово к запуску.")
    ttk.Label(progress_frame, textvariable=status_var, wraplength=980).grid(row=0, column=0, sticky="ew", pady=(0, 4))
    progress_text_var = tk.StringVar(value="Ожидание")
    ttk.Label(progress_frame, textvariable=progress_text_var).grid(row=1, column=0, sticky="w", pady=(0, 3))
    progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100, value=0)
    progress_bar.grid(row=2, column=0, sticky="ew")

    log_frame = ttk.LabelFrame(footer, text="Журнал", padding=6)
    log_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)
    log_text = tk.Text(log_frame, height=8, wrap="word", state="disabled", undo=True)
    log_text.grid(row=0, column=0, sticky="nsew")
    scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=scroll.set)

    def install_edit_shortcuts():
        editable_classes = (tk.Text, tk.Entry, tk.Spinbox, ttk.Entry, ttk.Spinbox, ttk.Combobox)
        shortcuts = {
            "c": "copy", "cyrillic_es": "copy", "с": "copy",
            "v": "paste", "cyrillic_em": "paste", "м": "paste",
            "x": "cut", "cyrillic_che": "cut", "ч": "cut",
            "a": "select_all", "cyrillic_ef": "select_all", "ф": "select_all",
            "z": "undo", "cyrillic_ya": "undo", "я": "undo",
            "y": "redo", "cyrillic_en": "redo", "н": "redo",
        }

        def selection_exists(widget):
            if isinstance(widget, tk.Text):
                return bool(widget.tag_ranges("sel"))
            try:
                return bool(widget.selection_present())
            except (AttributeError, tk.TclError):
                return False

        def selected_text(widget):
            if isinstance(widget, tk.Text):
                return widget.get("sel.first", "sel.last")
            return widget.selection_get()

        def delete_selection(widget):
            if not selection_exists(widget):
                return False
            if isinstance(widget, tk.Text):
                widget.delete("sel.first", "sel.last")
            else:
                widget.delete("sel.first", "sel.last")
            return True

        def handle_ctrl_shortcut(event):
            widget = event.widget
            if not isinstance(widget, editable_classes):
                return None
            # keysym при русской раскладке отличается, поэтому поддерживаем оба варианта.
            action = shortcuts.get(str(event.keysym).lower())
            if not action:
                return None

            is_text = isinstance(widget, tk.Text)
            try:
                state = str(widget.cget("state")).lower()
            except (tk.TclError, AttributeError):
                state = "normal"
            can_edit = state not in {"disabled", "readonly"}

            try:
                if action == "select_all":
                    if is_text:
                        widget.tag_add("sel", "1.0", "end-1c")
                        widget.mark_set("insert", "end-1c")
                    else:
                        widget.selection_range(0, "end")
                        widget.icursor("end")
                    return "break"

                if action == "copy":
                    if selection_exists(widget):
                        text_value = selected_text(widget)
                        root.clipboard_clear()
                        root.clipboard_append(text_value)
                    return "break"

                if action == "cut":
                    if not can_edit:
                        return "break"
                    if selection_exists(widget):
                        text_value = selected_text(widget)
                        root.clipboard_clear()
                        root.clipboard_append(text_value)
                        delete_selection(widget)
                    return "break"

                if action == "paste":
                    if not can_edit:
                        return "break"
                    try:
                        value = root.clipboard_get()
                    except tk.TclError:
                        return "break"
                    if is_text:
                        delete_selection(widget)
                        widget.insert("insert", value)
                    else:
                        delete_selection(widget)
                        widget.insert("insert", value)
                    return "break"

                if action == "undo":
                    if is_text and can_edit:
                        widget.edit_undo()
                        return "break"
                    return None

                if action == "redo":
                    if is_text and can_edit:
                        widget.edit_redo()
                        return "break"
                    return None
            except tk.TclError:
                # Например, история Undo ещё пуста или фокус находится в readonly-поле.
                return "break"
            return None

        root.bind_all("<Control-KeyPress>", handle_ctrl_shortcut, add="+")
        # Дополнительные стандартные варианты, распространённые в Windows.
        root.bind_all("<Control-Insert>", lambda event: handle_ctrl_shortcut(
            type("ShortcutEvent", (), {"widget": event.widget, "keysym": "c"})()
        ), add="+")
        root.bind_all("<Shift-Insert>", lambda event: handle_ctrl_shortcut(
            type("ShortcutEvent", (), {"widget": event.widget, "keysym": "v"})()
        ), add="+")
        root.bind_all("<Shift-Delete>", lambda event: handle_ctrl_shortcut(
            type("ShortcutEvent", (), {"widget": event.widget, "keysym": "x"})()
        ), add="+")

    install_edit_shortcuts()

    action_frame = ttk.Frame(footer)
    action_frame.grid(row=2, column=0, sticky="ew")
    action_frame.columnconfigure(0, weight=1)
    running = {"value": False}
    window_closing = threading.Event()

    def set_running(active: bool):
        running["value"] = active
        start_button.configure(state="disabled" if active else "normal")
        stop_button.configure(state="normal" if active else "disabled")

    def start_job():
        try:
            raw_start_id = start_id_var.get().strip()
            if not raw_start_id:
                raise ValueError("Укажите стартовый ID пользователя VK.")
            start_id = int(raw_start_id)
            workers = int(workers_var.get().strip())
            if start_id < 1:
                raise ValueError("Стартовый ID должен быть положительным числом.")
            if workers < 1 or workers > 64:
                raise ValueError("Количество потоков должно быть от 1 до 64.")
        except ValueError as exc:
            messagebox.showerror("Параметры", str(exc) or "Проверьте стартовый ID и число потоков.", parent=root)
            return
        token_count = len(_read_token_file())
        if token_count == 0:
            unsupported_count = sum(
                1 for saved_token in _read_all_token_file()
                if saved_token.lower().startswith("vk2.")
            )
            message = (
                f"В {VK_TOKEN_FILE.name} нет совместимых API-токенов для users.get/friends.get/groups.getById. "
                "Нужен рабочий пользовательский токен старого совместимого формата vk1.a… ."
            )
            if unsupported_count:
                message += (
                    f"\n\nНайдено токенов vk2.a…: {unsupported_count}. "
                    "Это токены нового VK ID; загрузчик пропускает их и не использует."
                )
            messagebox.showwarning("Токены VK", message, parent=root)
            return
        reload_token_cache()
        auto_import = bool(auto_import_var.get())
        create_crop_files = bool(create_crop_var.get())
        build_index_after = bool(build_index_var.get())
        delete_processed_files = bool(delete_processed_var.get())
        facesearch_dir = facesearch_dir_var.get().strip()
        output_dir = output_var.get().strip() or str(DEFAULT_OUTPUT_DIR)
        STOP_REQUESTED.clear()
        set_running(True)
        progress_bar.configure(mode="indeterminate")
        progress_bar.start(10)
        progress_text_var.set("Подготовка задания…")
        status_var.set("Задание выполняется в фоновом потоке; интерфейс остаётся доступен.")
        log_text.configure(state="normal")
        log_text.insert("end", "\n" + "=" * 68 + "\nНовое задание\n")
        log_text.see("end")
        log_text.configure(state="disabled")

        def worker():
            importer = None
            try:
                importer = FaceSearchImporter(
                    facesearch_dir,
                    status_callback=lambda message: events.put(("status", message)),
                    create_crop_files=create_crop_files,
                    check_only=not auto_import,
                )
                mode_text = "готов к импорту" if auto_import else "подключён только для проверки уже существующих профилей"
                print(f"✅ FaceSearch: активный профиль «{importer.config['active_name']}» {mode_text}.")
                next_start_id = run_download_job(
                    start_id, workers,
                    output_dir,
                    importer=importer,
                    delete_processed_files=delete_processed_files and importer is not None and auto_import,
                    import_to_database=auto_import,
                )
                events.put(("start_id_updated", next_start_id))
                if importer is not None and auto_import and build_index_after and not window_closing.is_set():
                    importer.build_index()
                if importer is not None and auto_import:
                    print(f"✅ FaceSearch: импортировано файлов — {importer.imported_count}.")
                elif importer is not None:
                    print("✅ FaceSearch: проверка существующих VK-профилей выполнена; импорт выключен.")
            except Exception as exc:
                print(f"💥 Критическая ошибка задания: {exc}")
                import traceback
                traceback.print_exc()
            finally:
                if importer is not None:
                    try:
                        importer.close()
                    except Exception as exc:
                        print(f"⚠️ Ошибка закрытия базы FaceSearch: {exc}")
                events.put(("job_finished", STOP_REQUESTED.is_set()))

        threading.Thread(target=worker, daemon=True, name="VKDownloaderWorker").start()

    def stop_job():
        if running["value"]:
            STOP_REQUESTED.set()
            status_var.set("Запрошена остановка. Текущий сетевой запрос/загрузка может завершиться перед остановкой.")
            stop_button.configure(state="disabled")

    start_button = ttk.Button(action_frame, text="Начать загрузку", command=start_job)
    start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=5)
    stop_button = ttk.Button(action_frame, text="Остановить", command=stop_job, state="disabled")
    stop_button.grid(row=0, column=1, sticky="ew", ipady=5)

    def drain_events():
        try:
            while True:
                event = events.get_nowait()
                kind = event[0]
                if kind == "log":
                    log_text.configure(state="normal")
                    log_text.insert("end", event[1])
                    # Не допускаем бесконечного роста виджета на очень больших обходах.
                    try:
                        line_count = int(log_text.index("end-1c").split(".")[0])
                        if line_count > 12000:
                            log_text.delete("1.0", f"{line_count - 10000}.0")
                    except Exception:
                        pass
                    log_text.see("end")
                    log_text.configure(state="disabled")
                elif kind == "progress":
                    current, total, message = event[1], event[2], event[3]
                    progress_text_var.set(message)
                    if total and total > 0:
                        if str(progress_bar.cget("mode")) != "determinate":
                            progress_bar.stop()
                            progress_bar.configure(mode="determinate", maximum=total)
                        else:
                            progress_bar.configure(maximum=total)
                        progress_bar["value"] = min(current, total)
                    else:
                        if str(progress_bar.cget("mode")) != "indeterminate":
                            progress_bar.configure(mode="indeterminate")
                            progress_bar.start(10)
                elif kind == "start_id_updated":
                    start_id_var.set(str(event[1]))
                elif kind == "status":
                    status_var.set(event[1])
                elif kind == "auth_status":
                    auth_status_var.set(event[1])
                elif kind == "auth_needs_service_token":
                    auth_status_var.set(event[1])
                    messagebox.showwarning("VK ID: требуется обмен токена", event[1], parent=root)
                elif kind == "auth_error":
                    auth_status_var.set(event[1])
                    messagebox.showerror("Авторизация VK", event[1], parent=root)
                elif kind == "token_saved":
                    success, user_name, added, count, error = event[1:]
                    token_count_var.set(f"Токенов в {VK_TOKEN_FILE.name}: {count}")
                    if hasattr(root, "_vk_token_save_button"):
                        try:
                            root._vk_token_save_button.configure(state="normal", text="Проверить и сохранить")
                        except tk.TclError:
                            pass
                    if success:
                        dialog = getattr(root, "_vk_token_dialog", None)
                        if dialog is not None:
                            try:
                                if dialog.winfo_exists():
                                    dialog.destroy()
                            except tk.TclError:
                                pass
                        if added:
                            messagebox.showinfo("Токен VK сохранён", f"Авторизация проверена. Токен добавлен в {VK_TOKEN_FILE.name}.\nПользователь: {user_name}\nВсего токенов: {count}", parent=root)
                        else:
                            messagebox.showinfo("Токен уже есть", f"Этот токен уже присутствует в {VK_TOKEN_FILE.name}.\nПользователь: {user_name}\nВсего токенов: {count}", parent=root)
                    else:
                        error_parent = getattr(root, "_vk_token_dialog", None)
                        try:
                            if error_parent is None or not error_parent.winfo_exists():
                                error_parent = root
                        except tk.TclError:
                            error_parent = root
                        messagebox.showerror("Не удалось добавить токен", error, parent=error_parent)
                elif kind == "job_finished":
                    progress_bar.stop()
                    progress_bar.configure(mode="determinate")
                    set_running(False)
                    progress_text_var.set("Задание остановлено" if event[1] else "Задание завершено")
                    status_var.set("Задание остановлено. Скачанные файлы сохранены." if event[1] else "Задание завершено. Проверьте журнал для итогов.")
                    progress_bar["value"] = 0 if event[1] else progress_bar["maximum"]
        except queue.Empty:
            pass
        except tk.TclError:
            return
        try:
            root.after(100, drain_events)
        except tk.TclError:
            pass

    def on_close():
        if running["value"]:
            if not messagebox.askyesno("Выход", "Задание ещё выполняется. Запросить остановку и закрыть окно?", parent=root):
                return
            STOP_REQUESTED.set()
        window_closing.set()
        AUTH_STOP_REQUESTED.set()
        sys.stdout = original_stdout
        sys.stderr = original_stderr
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    refresh_token_count()
    drain_events()
    root.mainloop()
    sys.stdout = original_stdout
    sys.stderr = original_stderr
    UI_EVENTS = None


if __name__ == "__main__":
    main()
