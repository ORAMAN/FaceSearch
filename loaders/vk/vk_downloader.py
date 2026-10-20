# Загрузчик изображений VK: ручное добавление пользовательских API-токенов v1.

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
from urllib.parse import urlparse, unquote
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# === ПАРАМЕТРЫ === #
APP_DIR = Path(__file__).resolve().parent

# Общие горячие клавиши и контекстное меню для полей ввода.
_LOADER_SHARED_DIR = str(APP_DIR.parent)
if _LOADER_SHARED_DIR not in sys.path:
    sys.path.insert(0, _LOADER_SHARED_DIR)
from edit_shortcuts import install_edit_shortcuts

# Адаптивное управление потоками использует тот же контроллер, что и основная форма.
_REPOSITORY_ROOT = str(APP_DIR.parent.parent)
if _REPOSITORY_ROOT not in sys.path:
    sys.path.insert(0, _REPOSITORY_ROOT)
from facesearch.config import AUTO_WORKERS_DEFAULT
from facesearch.processing.telemetry import AdaptiveWorkerController
VK_TOKEN_FILE = APP_DIR / "vk_token.txt"
# VKHost-style OAuth implicit flow: app 8101047, friends + photos + status + offline.
# Bitmask: friends=2, photos=4, status=1024, offline=65536; total=66566.
VK_AUTH_URL = (
    "https://oauth.vk.com/authorize?client_id=8101047&scope=66566&"
    "redirect_uri=https%3A%2F%2Foauth.vk.com%2Fblank.html&"
    "display=page&response_type=token&revoke=1"
)
LOG_FILE = APP_DIR / "log.txt"
DEFAULT_OUTPUT_DIR = APP_DIR / "vk_downloads"
APP_SETTINGS_PATH = APP_DIR / "vk_settings.json"
DEFAULT_FACESEARCH_DIR = Path(r"C:\FaceSearch")
_META_RESOLUTION = "_url_resolution"
_META_VERSION = "_resolution_version"
_META_DUPLICATE_OF = "_duplicate_of"
_META_DUPLICATE_REASON = "_duplicate_reason"
API_VERSION = "5.199"
BASE_URL = "https://api.vk.ru/method"
FRIENDS_PAGE_SIZE = 5000
USER_BATCH_SIZE = 1000
# Обычные системные альбомы VK. photos.get использует для них строковые
# album_id: profile, wall и saved; остальные значения остаются числовыми.
SYSTEM_ALBUM_OPTIONS = (
    (-6, "Аватары профиля (−6)"),
    (-7, "Фотографии со страницы (−7)"),
    (-15, "Сохранённые фотографии (−15)"),
    (-17, "Обложки (−17)"),
)
DEFAULT_SYSTEM_ALBUM_IDS = tuple(album_id for album_id, _label in SYSTEM_ALBUM_OPTIONS)
EXISTING_PROFILE_IDS: set[int] = set()
EXISTING_PROFILE_IDS_LOCK = threading.Lock()
UI_EVENTS = None
STOP_REQUESTED = threading.Event()
_TOKEN_FILE_LOCK = threading.Lock()

# ============================================================
# ACCESS_TOKENS
# ============================================================

count_tokens = 0
current_token = 0
_TOKEN_LIST = []
_TOKEN_LOCK = threading.Lock()


def _read_all_token_file() -> list[str]:
    """Читает все непустые строки файла, чтобы избежать повторного добавления токенов."""
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


def _is_v1_token(token: str) -> bool:
    """Принимает пользовательские access_token v1 с префиксом vk1.a."""
    return (token or "").strip().lower().startswith("vk1.a")


def _read_token_file() -> list[str]:
    """Возвращает только токены v1, совместимые с используемыми методами VK API."""
    return [token for token in _read_all_token_file() if _is_v1_token(token)]


def reload_token_cache() -> int:
    """Перечитывает список токенов из vk_token.txt."""
    global _TOKEN_LIST, count_tokens, current_token
    tokens = _read_token_file()
    with _TOKEN_LOCK:
        _TOKEN_LIST = tokens
        count_tokens = len(tokens)
        current_token = 0
    return len(tokens)


def _load_tokens_if_needed():
    """Загружает кэш токенов, если он пуст."""
    global _TOKEN_LIST, count_tokens, current_token
    if _TOKEN_LIST:
        return
    tokens = _read_token_file()
    if not tokens:
        raise RuntimeError(
            f"В {VK_TOKEN_FILE.name} нет токенов v1 формата vk1.a… . "
            "Добавьте рабочий токен через кнопку «Добавить токен v1»."
        )
    with _TOKEN_LOCK:
        if not _TOKEN_LIST:
            _TOKEN_LIST = tokens
            count_tokens = len(tokens)
            current_token = 0


def reload_tokens():
    """Возвращает следующий сохранённый токен v1 по кругу."""
    global current_token
    _load_tokens_if_needed()
    with _TOKEN_LOCK:
        token = _TOKEN_LIST[current_token]
        current_token = (current_token + 1) % len(_TOKEN_LIST)
        return token


def extract_v1_token_input(value: str) -> str:
    """Extract a v1 token from a raw token or the blank.html redirect URL."""
    value = (value or "").strip()
    if _is_v1_token(value):
        return value
    match = re.search(r"(?:^|[?#&])access_token=([^&#\s]+)", value)
    if not match:
        return ""
    token = unquote(match.group(1)).strip()
    return token if _is_v1_token(token) else ""


def validate_access_token(token: str) -> str:
    """Проверяет пользовательский API-токен v1 через users.get и возвращает имя владельца."""
    token = (token or "").strip()
    if not _is_v1_token(token):
        raise ValueError(
            "Поддерживаются только пользовательские API-токены v1 с префиксом vk1.a… . "
            "Вставьте сам токен, без URL и кода авторизации."
        )
    if any(character.isspace() for character in token):
        raise ValueError("Токен не должен содержать пробелы или переносы строк.")
    response = requests.get(
        f"{BASE_URL}/users.get",
        params={"access_token": token, "v": API_VERSION},
        timeout=20,
    )
    response.raise_for_status()
    payload = response.json()
    error = payload.get("error")
    if error:
        error_message = str(error.get("error_msg", error))
        error_code = error.get("error_code", "?")
        if error_code == 5 and "another ip address" in error_message.lower():
            raise RuntimeError(
                "VK отклонил токен из-за привязки к IP-адресу. Получите новый токен через кнопку "
                "«Получить токен через VK ID»: обновлённая ссылка включает право «Доступ в любое время» "
                "(offline). Старый токен не получает это право автоматически. "
                "Можно вставить часть от access_token= до &expires_in либо полный URL blank.html."
            )
        raise RuntimeError(
            f"VK API отклонил токен: {error_message} (код {error_code})."
        )
    users = payload.get("response") or []
    if not users:
        raise RuntimeError("VK API не вернул пользователя для проверки токена.")
    user = users[0]
    return f"{user.get('first_name', '')} {user.get('last_name', '')}".strip() or str(user.get("id", ""))


def save_access_token(token: str) -> tuple[bool, int]:
    """Добавляет только токен v1 в vk_token.txt и не создаёт дубликатов."""
    token = (token or "").strip()
    if not _is_v1_token(token):
        raise ValueError("Можно сохранить только API-токен v1 с префиксом vk1.a… .")
    if any(character.isspace() for character in token):
        raise ValueError("Токен не должен содержать пробелы или переносы строк.")
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


def post_ui_event(*event):
    if UI_EVENTS is not None:
        UI_EVENTS.put(event)


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


def _format_vk_timestamp(value):
    """Преобразует Unix timestamp VK в локальное время для карточки/JSON."""
    try:
        timestamp = int(value)
        if timestamp <= 0:
            return ""
        return time.strftime("%Y-%m-%d %H:%M:%S", time.localtime(timestamp))
    except (TypeError, ValueError, OverflowError, OSError):
        return ""


def _photo_size_dimensions(photo: dict) -> tuple[int | None, int | None]:
    """Выбирает самое большое доступное разрешение фотографии."""
    sizes = photo.get("sizes") or []
    best = None
    best_area = -1
    for size in sizes:
        if not isinstance(size, dict):
            continue
        try:
            width = int(size.get("width") or 0)
            height = int(size.get("height") or 0)
        except (TypeError, ValueError):
            width = height = 0
        area = width * height
        if size.get("url") and area >= best_area:
            best = size
            best_area = area
    if best:
        try:
            return int(best.get("width") or 0) or None, int(best.get("height") or 0) or None
        except (TypeError, ValueError):
            pass
    original = photo.get("orig_photo") or {}
    try:
        width = int(original.get("width") or 0) or None
        height = int(original.get("height") or 0) or None
        return width, height
    except (TypeError, ValueError, AttributeError):
        return None, None


def build_entity_json(
    entity: dict,
    url_image: str,
    album_title: str | None = None,
    album_id: int | str | None = None,
    is_group: bool = False,
    photo: dict | None = None,
    album: dict | None = None,
    source_kind: str | None = None,
) -> dict:
    """Формирует совместимые с FaceSearch метаданные пользователя и конкретного фото.

    Базовые ключи url_source/name/url_image/info сохранены. Дополнительные поля
    photo и album необязательны и содержат идентификаторы/характеристики снимка.
    """
    if is_group:
        gid = abs(int(entity.get("id", 0)))
        url_source = f"https://vk.com/club{gid}"
        name = entity.get("name", "") or ""
        info = {}
        for field, label in (
            ("screen_name", "Короткое имя"),
            ("members_count", "Участников"),
            ("status", "Статус"),
            ("description", "Описание"),
        ):
            value = entity.get(field)
            if value not in (None, "", [], {}):
                info[label] = value
    else:
        uid = entity.get("id")
        url_source = f"https://vk.com/id{uid}"
        first = str(entity.get("first_name") or "").strip()
        last = str(entity.get("last_name") or "").strip()
        name = f"{first} {last}".strip() or str(entity.get("screen_name") or f"user_{uid}")
        info = {}

        bdate = entity.get("bdate")
        if bdate:
            info["Дата рождения"] = bdate
        city = entity.get("city") or {}
        if isinstance(city, dict) and city.get("title"):
            info["Город"] = city["title"]
        country = entity.get("country") or {}
        if isinstance(country, dict) and country.get("title"):
            info["Страна"] = country["title"]
        home_town = entity.get("home_town")
        if home_town:
            info["Родной город"] = home_town

        sex = entity.get("sex")
        if sex == 1:
            info["Пол"] = "Ж"
        elif sex == 2:
            info["Пол"] = "М"
        elif sex == 0:
            info["Пол"] = "Не указан"

        deactivated = entity.get("deactivated")
        if deactivated == "deleted":
            info["Статус"] = "Удалён"
        elif deactivated == "banned":
            info["Статус"] = "Заблокирован"
        elif entity.get("is_closed"):
            info["Статус"] = "Закрыт"
        else:
            info["Статус"] = "Открыт"

        profile_fields = (
            ("screen_name", "Короткое имя"),
            ("status", "Текст статуса"),
            ("about", "О себе"),
            ("activities", "Деятельность"),
            ("interests", "Интересы"),
            ("site", "Сайт"),
        )
        for field, label in profile_fields:
            value = entity.get(field)
            if value not in (None, "", [], {}):
                info[label] = value

        occupation = entity.get("occupation")
        if isinstance(occupation, dict):
            occupation_name = occupation.get("name")
            occupation_type = occupation.get("type")
            if occupation_name:
                if occupation_type:
                    info["Место работы / учёбы"] = f"{occupation_name} ({occupation_type})"
                else:
                    info["Место работы / учёбы"] = occupation_name
        elif occupation:
            info["Место работы / учёбы"] = occupation

        universities = entity.get("universities") or []
        if isinstance(universities, list):
            education_names = [
                str(item.get("name")).strip()
                for item in universities
                if isinstance(item, dict) and item.get("name")
            ]
            if education_names:
                info["Образование"] = "; ".join(dict.fromkeys(education_names))
        education = entity.get("education") or {}
        if isinstance(education, dict):
            for field, label in (("university_name", "Вуз"), ("faculty_name", "Факультет"), ("graduation", "Год окончания")):
                value = education.get(field)
                if value not in (None, "", 0):
                    info[label] = value

        followers = entity.get("followers_count")
        if followers is not None:
            info["Подписчиков"] = followers
        verified = entity.get("verified")
        if verified is not None:
            info["Подтверждённый профиль"] = "Да" if bool(verified) else "Нет"
        has_photo = entity.get("has_photo")
        if has_photo is not None:
            info["Фото профиля"] = "Да" if bool(has_photo) else "Нет"
        online = entity.get("online")
        if online is not None:
            info["Онлайн"] = "Да" if bool(online) else "Нет"
        last_seen = entity.get("last_seen") or {}
        if isinstance(last_seen, dict):
            last_seen_text = _format_vk_timestamp(last_seen.get("time"))
            platform = last_seen.get("platform")
            platform_names = {
                1: "мобильная версия сайта", 2: "iPhone", 3: "iPad",
                4: "Android", 5: "Windows Phone", 6: "Windows 10",
                7: "полная версия сайта",
            }
            if last_seen_text:
                if platform:
                    try:
                        platform_label = platform_names.get(int(platform), f"платформа {platform}")
                    except (TypeError, ValueError):
                        platform_label = str(platform)
                    last_seen_text += f" ({platform_label})"
                info["Последний визит"] = last_seen_text
        counters = entity.get("counters") or {}
        if isinstance(counters, dict):
            for field, label in (
                ("friends", "Друзей"), ("photos", "Фотографий в профиле"),
                ("albums", "Альбомов"), ("videos", "Видеозаписей"),
            ):
                if counters.get(field) is not None:
                    info[label] = counters[field]

    if album_title is not None:
        info["Альбом"] = album_title
    effective_album_id = album_id
    if effective_album_id is None and isinstance(photo, dict):
        effective_album_id = photo.get("album_id")
    if effective_album_id is not None:
        info["ID альбома"] = effective_album_id

    result = {
        "url_source": url_source,
        "name": name,
        "url_image": str(url_image or "").replace("&cs=240x240", ""),
        "info": info,
    }
    # FaceSearch принимает ровно четыре корневых поля. Дополнительные
    # метаданные хранятся только внутри info.
    if source_kind:
        info["Тип источника"] = source_kind

    if isinstance(photo, dict):
        photo_id = photo.get("id")
        owner_id = photo.get("owner_id") or entity.get("id")
        try:
            photo_url = f"https://vk.com/photo{int(owner_id)}_{int(photo_id)}"
        except (TypeError, ValueError):
            photo_url = ""
        width, height = _photo_size_dimensions(photo)
        date_text = _format_vk_timestamp(photo.get("date"))
        likes = photo.get("likes") if isinstance(photo.get("likes"), dict) else {}
        comments = photo.get("comments") if isinstance(photo.get("comments"), dict) else {}
        reposts = photo.get("reposts") if isinstance(photo.get("reposts"), dict) else {}
        views = photo.get("views") if isinstance(photo.get("views"), dict) else {}
        text_value = str(photo.get("text") or "").strip()
        if photo_id is not None:
            info["ID фотографии"] = photo_id
        if date_text:
            info["Дата фотографии"] = date_text
        if width and height:
            info["Размер фотографии"] = f"{width} × {height}"
        if text_value:
            info["Описание фотографии"] = text_value
        if likes.get("count") is not None:
            info["Лайки"] = likes.get("count")
        if comments.get("count") is not None:
            info["Комментарии"] = comments.get("count")
        if reposts.get("count") is not None:
            info["Репосты"] = reposts.get("count")
        if views.get("count") is not None:
            info["Просмотры"] = views.get("count")
        if owner_id is not None:
            info["ID владельца фотографии"] = owner_id
        if photo_url:
            info["Ссылка на фотографию VK"] = photo_url
        tags = photo.get("tags") or []
        if isinstance(tags, list):
            info["Количество отметок"] = len(tags)

    if isinstance(album, dict):
        album_data = {
            "id": album.get("id", effective_album_id),
            "title": album.get("title") or album_title or "",
            "description": album.get("description") or "",
            "size": album.get("size"),
            "created": album.get("created"),
            "updated": album.get("updated"),
        }
        if album_data["description"]:
            info["Описание альбома"] = album_data["description"]
        if album_data["size"] is not None:
            info["Фотографий в альбоме"] = album_data["size"]
        created_text = _format_vk_timestamp(album_data["created"])
        if created_text:
            info["Дата создания альбома"] = created_text
        updated_text = _format_vk_timestamp(album_data["updated"])
        if updated_text:
            info["Дата обновления альбома"] = updated_text
    # Не добавлять пользовательские поля в корень JSON: импортёр FaceSearch
    # строго ожидает url_source, name, url_image и info.
    return result


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


def get_user_info(user_ids: str | list[int] | tuple[int, ...], session: requests.Session):
    """Запрашивает пользователей, принимая только отдельные ID, перечисленные через запятую."""
    if isinstance(user_ids, str):
        raw_ids = [part.strip() for part in user_ids.split(",") if part.strip()]
    else:
        raw_ids = [str(part).strip() for part in user_ids]

    if not raw_ids:
        raise ValueError("users.get: список ID пуст.")

    normalized_ids = []
    for raw_id in raw_ids:
        if not re.fullmatch(r"\d+", raw_id):
            raise ValueError(
                f"users.get ожидает отдельные числовые ID через запятую; "
                f"диапазоны и другие значения запрещены: {raw_id!r}"
            )
        user_id = int(raw_id)
        if user_id < 1:
            raise ValueError(f"users.get ожидает положительный ID: {raw_id!r}")
        normalized_ids.append(str(user_id))

    user_ids_param = ",".join(normalized_ids)
    params = {
        "user_ids": user_ids_param,
        "fields": (
            "photo_max_orig,city,country,home_town,sex,bdate,screen_name,deactivated,is_closed,"
            "status,site,about,activities,interests,education,universities,occupation,"
            "followers_count,verified,has_photo,last_seen,online,counters"
        ),
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


def _response_items(response_data):
    """Нормализует стандартные VK-ответы {count, items} и простые списки."""
    if isinstance(response_data, list):
        return response_data, len(response_data)
    if isinstance(response_data, dict):
        items = response_data.get("items") or []
        try:
            total = int(response_data.get("count") or 0)
        except (TypeError, ValueError):
            total = 0
        return items, total
    return [], 0


def _vk_get_page(method: str, params: dict, session: requests.Session, timeout: int = 20):
    """Вызов VK API с повторами для временных ограничений и ротацией токена."""
    last_error = None
    for attempt in range(3):
        request_params = dict(params)
        request_params["access_token"] = reload_tokens()
        request_params["v"] = API_VERSION
        if attempt:
            time.sleep(0.75 * attempt)
        else:
            time.sleep(0.35)
        response = session.get(f"{BASE_URL}/{method}", params=request_params, timeout=timeout)
        response.raise_for_status()
        payload = response.json()
        error = payload.get("error") if isinstance(payload, dict) else None
        if error:
            code = error.get("error_code")
            error_text = error.get("error_msg", error)
            last_error = RuntimeError(f"{method}: {error_text} (код {code or 'неизвестен'})")
            if code in (5, 6, 29) and attempt < 2:
                print(f"⚠️ {method}: {error_text}; повтор {attempt + 1}/2.")
                continue
            raise last_error
        return payload.get("response", {})
    if last_error:
        raise last_error
    raise RuntimeError(f"{method}: VK API не вернул ответ.")


def get_albums(owner_id: int, session: requests.Session):
    """Возвращает системные и пользовательские альбомы с постраничной загрузкой."""
    all_items = []
    offset = 0
    page_size = 100
    while True:
        response_data = _vk_get_page(
            "photos.getAlbums",
            {
                "owner_id": int(owner_id),
                "need_system": 1,
                "need_covers": 1,
                "count": page_size,
                "offset": offset,
            },
            session,
            timeout=20,
        )
        items, total = _response_items(response_data)
        if not items:
            break
        all_items.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        if (total and offset >= total) or (not total and len(items) < page_size):
            break
    return all_items


def get_user_photos(owner_id: int, session: requests.Session, limit: int = 0):
    """Получает фотографии, на которых отмечен пользователь; 0 означает все."""
    all_items = []
    offset = 0
    limit = max(0, int(limit or 0))
    page_size = 100
    while not STOP_REQUESTED.is_set():
        remaining = limit - len(all_items) if limit else page_size
        if limit and remaining <= 0:
            break
        count = min(page_size, remaining) if limit else page_size
        response_data = _vk_get_page(
            "photos.getUserPhotos",
            {
                "user_id": int(owner_id),
                "count": count,
                "offset": offset,
                "extended": 1,
            },
            session,
            timeout=25,
        )
        items, total = _response_items(response_data)
        if not items:
            break
        all_items.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        if (limit and len(all_items) >= limit) or (total and offset >= total) or (not total and len(items) < count):
            break
    return all_items[:limit] if limit else all_items


def _api_album_id(album_id: int | str) -> str | int:
    """VK рекомендует строковые идентификаторы для основных системных альбомов."""
    try:
        numeric_id = int(album_id)
    except (TypeError, ValueError):
        return album_id
    return {-6: "profile", -7: "wall", -15: "saved"}.get(numeric_id, numeric_id)


def get_photos_by_album(
    owner_id: int,
    album_id: int | str,
    session: requests.Session,
    limit: int = 0,
):
    """Получает фотографии альбома постранично; limit=0 — без ограничения."""
    all_items = []
    offset = 0
    limit = max(0, int(limit or 0))
    page_size = 200
    while not STOP_REQUESTED.is_set():
        remaining = limit - len(all_items) if limit else page_size
        if limit and remaining <= 0:
            break
        count = min(page_size, remaining) if limit else page_size
        response_data = _vk_get_page(
            "photos.get",
            {
                "owner_id": int(owner_id),
                "album_id": _api_album_id(album_id),
                "count": count,
                "offset": offset,
                "rev": 0,
                "extended": 1,
                "photo_sizes": 1,
            },
            session,
            timeout=25,
        )
        items, total = _response_items(response_data)
        if not items:
            break
        all_items.extend(item for item in items if isinstance(item, dict))
        offset += len(items)
        if (limit and len(all_items) >= limit) or (total and offset >= total) or (not total and len(items) < count):
            break
    return all_items[:limit] if limit else all_items


def extract_best_url(photo: dict) -> str:
    """Возвращает URL самого большого доступного варианта фотографии VK.

    Не полагается на порядок полей ответа API и не отдаёт приоритет orig_photo
    вслепую: у разных старых/новых ответов VK этот объект может уступать более
    крупному элементу sizes или photo_2560/photo_1280.
    """
    candidates: list[tuple[int, int, str, str]] = []
    sequence = 0

    def add_candidate(url, width=None, height=None, fallback_area=0, label=""):
        nonlocal sequence
        if not isinstance(url, str):
            return
        url = url.strip()
        if not url or not url.lower().startswith(("http://", "https://")):
            return
        try:
            w = int(width or 0)
        except (TypeError, ValueError):
            w = 0
        try:
            h = int(height or 0)
        except (TypeError, ValueError):
            h = 0
        area = w * h if w > 0 and h > 0 else int(fallback_area or 0)
        # Последний элемент выигрывает только при равной площади: это важно для
        # ответов VK, где позднее может идти оригинальный URL с теми же размерами.
        candidates.append((area, sequence, url, label))
        sequence += 1

    # 1. Вариант orig_photo — это ещё один кандидат, а не безусловный приоритет.
    original = photo.get("orig_photo")
    if isinstance(original, dict):
        add_candidate(
            original.get("url") or original.get("src"),
            original.get("width"), original.get("height"),
            label="orig_photo",
        )

    # 2. Все размеры из photos.get с учётом URL как url, так и src.
    # Типы размеров VK используются как резервная оценка, если API не вернул
    # ширину/высоту для конкретного варианта.
    type_edge = {
        "temp": 1, "s": 75, "m": 130, "o": 130, "p": 200, "q": 320,
        "r": 510, "x": 604, "y": 807, "z": 1080, "w": 2560,
        "j": 256,
    }
    for size in (photo.get("sizes") or []):
        if not isinstance(size, dict):
            continue
        size_type = str(size.get("type") or "").lower()
        edge = type_edge.get(size_type, 0)
        add_candidate(
            size.get("url") or size.get("src"),
            size.get("width"), size.get("height"),
            fallback_area=edge * edge,
            label=f"sizes:{size_type}",
        )

    # 3. Старые поля API. Оценка соответствует максимально возможному размеру
    # соответствующего варианта; не предполагает, что поле обязательно есть.
    legacy_fields = (
        ("photo_2560", 2560, 2048),
        ("photo_1280", 1280, 1024),
        ("photo_807", 807, 807),
        ("photo_604", 604, 604),
        ("photo_400_orig", 400, 400),
        ("photo_200_orig", 200, 200),
        ("photo_200", 200, 200),
        ("photo_130", 130, 130),
        ("photo_100", 100, 100),
        ("photo_75", 75, 75),
    )
    for field, width, height in legacy_fields:
        value = photo.get(field)
        if value:
            add_candidate(value, width, height, label=field)

    if not candidates:
        raise ValueError("VK не вернул URL фотографии ни для одного доступного размера")

    # Максимальная доступная площадь — первичный критерий. Порядок служит лишь
    # разрешением равенства и не приводит к выбору первой миниатюры.
    return max(candidates, key=lambda item: (item[0], item[1]))[2]



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

        # Не переиспользуем файл только по совпадению имени. В URL разных размеров
        # VK иногда отличается лишь query-параметр, а имя пути остаётся тем же.
        # Поэтому ранее загруженная миниатюра могла блокировать скачивание оригинала.
        if os.path.exists(filepath) and os.path.getsize(filepath) > 1024:
            previous_url = None
            if os.path.isfile(json_path):
                try:
                    with open(json_path, "r", encoding="utf-8") as json_file:
                        old_metadata = json.load(json_file)
                    if isinstance(old_metadata, dict):
                        previous_url = old_metadata.get("url_image")
                except (OSError, ValueError, TypeError):
                    previous_url = None
            if previous_url == url:
                if json_data is not None:
                    try:
                        write_json(json_path, json_data)
                    except Exception as e:
                        return False, f"JSON write error: {e}", filepath, json_path
                return True, None, filepath, json_path

        # Скачиваем во временный файл: ошибка сети не должна оставить обрезанное
        # изображение под именем ранее существовавшего файла.
        temp_path = filepath + ".part"
        try:
            with requests.get(url, timeout=25, stream=True) as resp:
                if resp.status_code != 200:
                    return False, f"HTTP {resp.status_code}", filepath, json_path
                ct = resp.headers.get("content-type", "").lower()
                if "image" not in ct and "octet-stream" not in ct:
                    return False, f"Invalid content-type: {ct}", filepath, json_path

                with open(temp_path, "wb") as f:
                    for chunk in resp.iter_content(65536):
                        if chunk:
                            f.write(chunk)

            if not os.path.isfile(temp_path) or os.path.getsize(temp_path) == 0:
                return False, "Пустой файл изображения", filepath, json_path
            os.replace(temp_path, filepath)
        finally:
            try:
                if os.path.exists(temp_path):
                    os.remove(temp_path)
            except OSError:
                pass

        # JSON рядом с картинкой хранит тот же URL, по которому реально скачали файл.
        if json_data is not None:
            try:
                write_json(json_path, json_data)
            except Exception as e:
                return False, f"JSON write error: {e}", filepath, json_path

        return True, None, filepath, json_path

    except Exception as e:
        return False, str(e), None, None


def _remove_dir_if_empty(path: str | Path) -> bool:
    """Удаляет только пустой каталог; возвращает, удалось ли убрать его."""
    target = Path(path)
    if not target.exists():
        return True
    try:
        target.rmdir()
        return True
    except FileNotFoundError:
        return True
    except (OSError, TypeError, ValueError):
        return False


def save_avatar_to_root(
    entity: dict, base_dir: str, is_group: bool = False
) -> tuple[bool, str | None, str]:
    """
    Сохраняет Avatar.jpg и Avatar.json.
    Возвращает (успех, причина, тип результата): ok, no_avatar или download_error.
    Каталог создаётся только после проверки URL и HTTP-ответа.
    """
    avatar_url = (
        entity.get("photo_max_orig")
        or entity.get("photo_400_orig")
        or entity.get("photo_200_orig")
        or entity.get("photo_512")
        or entity.get("photo_200")
        or entity.get("photo_100")
        or ""
    ).strip()

    if " " in avatar_url:
        avatar_url = avatar_url.split(" ")[0].strip()

    stubs = (
        "https://vk.com/images/camera_200.png",
        "https://vk.com/images/camera_100.png",
        "https://vk.com/images/deactivated_200.png",
        "https://vk.com/images/deactivated_cropped_200.png",
        "https://vk.com/images/question_200.png",
        "https://vk.com/images/community_200.png",
    )
    if not avatar_url or any(avatar_url.startswith(stub) for stub in stubs):
        return False, "у профиля нет доступной фотографии (пустой URL или стандартная заглушка VK)", "no_avatar"

    base_path = Path(base_dir)
    avatar_path = base_path / "Avatar.jpg"
    json_path = base_path / "Avatar.json"
    temp_path = base_path / ".Avatar.jpg.part"
    image_created = False

    try:
        with requests.get(avatar_url, timeout=20, stream=True) as resp:
            if resp.status_code != 200:
                return False, f"HTTP {resp.status_code} при загрузке фотографии", "download_error"
            content_type = resp.headers.get("content-type", "").lower()
            if "image" not in content_type and "octet-stream" not in content_type:
                return False, f"неожиданный Content-Type: {content_type or '(пустой)'}", "download_error"

            base_path.mkdir(parents=True, exist_ok=True)
            with open(temp_path, "wb") as file:
                for chunk in resp.iter_content(65536):
                    if chunk:
                        file.write(chunk)

        if not temp_path.is_file() or temp_path.stat().st_size == 0:
            raise RuntimeError("VK вернул пустой файл изображения")

        os.replace(str(temp_path), str(avatar_path))
        image_created = True

        avatar_json = build_entity_json(
            entity,
            avatar_url,
            album_title="Аватар",
            album_id=-6,
            is_group=is_group,
        )
        write_json(str(json_path), avatar_json)
        return True, None, "ok"

    except Exception as exc:
        try:
            temp_path.unlink(missing_ok=True)
        except OSError:
            pass
        if image_created:
            try:
                avatar_path.unlink(missing_ok=True)
            except OSError:
                pass
        try:
            json_path.unlink(missing_ok=True)
        except OSError:
            pass
        _remove_dir_if_empty(base_path)
        return False, f"{type(exc).__name__}: {exc}", "download_error"


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

def _is_system_album(album: dict) -> bool:
    """Определяет системный альбом по полю API и отрицательному ID."""
    value = album.get("is_system")
    if isinstance(value, str):
        value = value.strip().lower() in {"1", "true", "yes"}
    if value is not None and bool(value):
        return True
    try:
        return int(album.get("id")) < 0
    except (TypeError, ValueError):
        return False


def _album_title(album: dict) -> str:
    """Название для стандартных альбомов VK, даже если поле title пустое."""
    try:
        album_id = int(album.get("id"))
    except (TypeError, ValueError):
        album_id = 0
    defaults = {
        -6: "Аватары",
        -7: "Фотографии со страницы",
        -15: "Сохранённые фотографии",
        -17: "Обложки",
        -9000: "Фото с отметками",
    }
    return str(album.get("title") or defaults.get(album_id) or f"Альбом {album_id}").strip()


def _remove_empty_directories_upwards(start_path: str | Path, stop_at: str | Path) -> None:
    """Удаляет пустые каталоги снизу вверх до output-каталога, не считая непустоту ошибкой."""
    current = Path(start_path)
    stop = Path(stop_at)
    try:
        stop_resolved = stop.resolve()
    except OSError:
        return
    while True:
        try:
            current_resolved = current.resolve()
            current_resolved.relative_to(stop_resolved)
        except (OSError, ValueError):
            return
        if current_resolved == stop_resolved:
            return
        try:
            current.rmdir()
        except (FileNotFoundError, OSError):
            return
        current = current.parent


def _remove_empty_directories_recursively(root_path: str | Path) -> int:
    """Удаляет все пустые вложенные каталоги под output-root, сам root не удаляет."""
    try:
        root = Path(root_path).resolve()
    except (OSError, RuntimeError, TypeError, ValueError):
        return 0
    if not root.is_dir():
        return 0

    removed = 0
    # Bottom-up: сначала пустые подкаталоги альбомов, затем папки пользователей.
    for current_text, dirnames, _filenames in os.walk(root, topdown=False, followlinks=False):
        current = Path(current_text)
        for dirname in dirnames:
            candidate = current / dirname
            try:
                resolved = candidate.resolve()
                resolved.relative_to(root)
                # Не затрагиваем symlinks/junctions: их цель может быть вне downloads.
                if candidate.is_symlink():
                    continue
                candidate.rmdir()
                removed += 1
            except (FileNotFoundError, OSError, RuntimeError, ValueError):
                # Каталог непуст, отсутствует или его путь выходит за пределы root.
                continue
    return removed


def process_entity(
    entity: dict, base_dir: str, is_group: bool, MAX_WORKERS: int,
    importer=None, delete_processed_files: bool = False, import_to_database: bool = True,
    download_albums: bool = False, all_albums: bool = False, album_photo_limit: int = 0,
    selected_system_albums: list[int] | tuple[int, ...] | set[int] | None = None,
    download_tagged_photos: bool = False,
):
    """Скачивает аватар и выбранные системные/пользовательские альбомы; возвращает пары файлов."""
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
    if not is_group and already_in_db and not download_albums:
        print(f"[DB-SKIP] ID {abs(uid)} | {name}: профиль уже есть в активной БД FaceSearch.")
        return {"status": "already_in_db", "uid": abs(uid), "name": name}

    print(f"\n{'=' * 60}")
    print(f"👤 {name} | ID: {display_id}")
    items = []
    # Если загружаем ещё и фотографии из альбомов, профиль не отбрасываем по ID:
    # заново скачиваем и аватар, а штатный импортёр FaceSearch сам определит,
    # нужно ли обновить источник или это уже известное изображение.
    avatar_result_kind = None
    avatar_reason = None

    if not already_in_db or download_albums or is_group:
        post_ui_event("status", f"Загрузка аватарки VK ID {display_id} ({name})…")
        success, reason, result_kind = save_avatar_to_root(entity, base_dir, is_group=is_group)
        avatar_result_kind = result_kind
        avatar_reason = reason
        if success:
            avatar_path = Path(base_dir) / "Avatar.jpg"
            avatar_json = Path(base_dir) / "Avatar.json"
            items.append({
                "image_path": avatar_path,
                "json_path": avatar_json,
                "source_key": f"vk:{abs(uid)}:avatar",
                "kind": "avatar",
            })
            print(f"[AVATAR-OK] ID {display_id} | {name}: аватар скачан.")
        elif result_kind == "no_avatar":
            print(f"[NO-AVATAR] ID {display_id} | {name}: {reason}.")
        else:
            print(f"[AVATAR-ERROR] ID {display_id} | {name}: {reason}.")
    else:
        print(f"[DB-ALBUMS] ID {display_id} | {name}: профиль уже в БД; аватар повторно не скачивается, проверяются альбомы.")

    album_photos_downloaded = 0
    album_errors = 0
    if download_albums and not is_group and not STOP_REQUESTED.is_set():
        try:
            limit = max(0, int(album_photo_limit or 0))
        except (TypeError, ValueError):
            limit = 0
        try:
            selected_album_ids = {
                int(value) for value in (
                    DEFAULT_SYSTEM_ALBUM_IDS if selected_system_albums is None else selected_system_albums
                )
            }
        except (TypeError, ValueError):
            selected_album_ids = set(DEFAULT_SYSTEM_ALBUM_IDS)
        download_tagged_photos = bool(download_tagged_photos) or bool(all_albums)
        selected_names = [label for album_id, label in SYSTEM_ALBUM_OPTIONS if album_id in selected_album_ids]
        print(
            f"[ALBUMS] ID {display_id}: режим={'все альбомы' if all_albums else 'выбранные системные альбомы'}; "
            f"выбрано: {'все альбомы' if all_albums else (', '.join(selected_names) if selected_names else 'нет системных альбомов')}; "
            f"фото с отметками={'да' if download_tagged_photos else 'нет'}; "
            f"лимит фото на альбом: {'все' if limit == 0 else limit}."
        )
        try:
            with requests.Session() as api_session:
                try:
                    albums = get_albums(abs(uid), api_session)
                except Exception as exc:
                    album_errors += 1
                    albums = []
                    print(f"[ALBUMS-ERROR] ID {display_id}: не удалось получить список альбомов: {type(exc).__name__}: {exc}")

                def is_selected_system_album(album):
                    if not isinstance(album, dict) or not _is_system_album(album):
                        return False
                    try:
                        album_id = int(album.get("id"))
                    except (TypeError, ValueError):
                        return False
                    return album_id in selected_album_ids

                selected_albums = [
                    album for album in albums
                    if isinstance(album, dict) and (all_albums or is_selected_system_album(album))
                ]
                if not all_albums:
                    selected_albums.sort(key=lambda album: (int(album.get("id") or 0) >= 0, int(album.get("id") or 0)))
                print(f"[ALBUMS] ID {display_id}: доступно альбомов {len(albums)}, к обработке {len(selected_albums)}.")
                seen_photo_ids = set()

                def save_album_photos(photo_records, title, album_data=None, album_id_value=None):
                    nonlocal album_photos_downloaded, album_errors
                    # Список фотографий текущего альбома уже получен — завершаем
                    # обработку всех его элементов, включая импорт в БД и очистку.
                    # Stop проверяется между альбомами/профилями, не внутри начатой задачи.
                    for photo in photo_records:
                        if not isinstance(photo, dict):
                            continue
                        photo_id = photo.get("id")
                        owner_id = photo.get("owner_id") or abs(uid)
                        if photo_id is None:
                            continue
                        try:
                            dedupe_key = (int(owner_id), int(photo_id))
                        except (TypeError, ValueError):
                            dedupe_key = (str(owner_id), str(photo_id))
                        if dedupe_key in seen_photo_ids:
                            continue
                        seen_photo_ids.add(dedupe_key)
                        try:
                            image_url = extract_best_url(photo)
                            metadata = build_entity_json(
                                entity,
                                image_url,
                                album_title=title,
                                album_id=album_id_value,
                                photo=photo,
                                album=album_data,
                                source_kind="photo",
                            )
                            ok, error, image_path, json_path = download_image((
                                name, display_id, sanitize_filename(title), str(photo_id),
                                image_url, base_dir, metadata,
                            ))
                            if not ok or not image_path or not json_path:
                                album_errors += 1
                                print(f"[PHOTO-ERROR] ID {display_id}, фото {photo_id}, альбом «{title}»: {error or 'не удалось сохранить файл'}")
                                continue
                            photo_owner = metadata.get("photo", {}).get("owner_id", abs(uid))
                            items.append({
                                "image_path": Path(image_path),
                                "json_path": Path(json_path),
                                "source_key": f"vk:{abs(uid)}:photo:{photo_owner}_{photo_id}",
                                "kind": "photo",
                            })
                            album_photos_downloaded += 1
                        except Exception as exc:
                            album_errors += 1
                            print(f"[PHOTO-ERROR] ID {display_id}, фото {photo_id}, альбом «{title}»: {type(exc).__name__}: {exc}")

                for album_data in selected_albums:
                    if STOP_REQUESTED.is_set():
                        break
                    raw_album_id = album_data.get("id")
                    try:
                        current_album_id = int(raw_album_id)
                    except (TypeError, ValueError):
                        print(f"[ALBUM-SKIP] ID {display_id}: пропущен альбом с некорректным ID {raw_album_id!r}.")
                        continue
                    title = _album_title(album_data)
                    try:
                        photos = get_photos_by_album(abs(uid), current_album_id, api_session, limit=limit)
                    except Exception as exc:
                        album_errors += 1
                        print(f"[ALBUM-ERROR] ID {display_id}, альбом «{title}» ({current_album_id}): {type(exc).__name__}: {exc}")
                        continue
                    print(f"[ALBUM] ID {display_id} | «{title}»: фотографий получено {len(photos)}.")
                    save_album_photos(photos, title, album_data, current_album_id)

                # Фото с отметками — отдельная лента photos.getUserPhotos, а не обычный
                # альбом photos.getAlbums. «Все альбомы» включает её автоматически.
                if download_tagged_photos and not STOP_REQUESTED.is_set():
                    try:
                        tagged_photos = get_user_photos(abs(uid), api_session, limit=limit)
                        print(f"[TAGGED-PHOTOS] ID {display_id}: фото с отметками получено {len(tagged_photos)}.")
                        save_album_photos(tagged_photos, "Фото с отметками", None, None)
                    except Exception as exc:
                        album_errors += 1
                        print(f"[TAGGED-PHOTOS-ERROR] ID {display_id}: {type(exc).__name__}: {exc}")
        except Exception as exc:
            album_errors += 1
            print(f"[ALBUMS-ERROR] ID {display_id}: {type(exc).__name__}: {exc}")

    if album_photos_downloaded:
        print(f"[ALBUMS-OK] ID {display_id} | {name}: скачано фотографий альбомов/отметок: {album_photos_downloaded}.")
    if album_errors:
        print(f"[ALBUMS-WARN] ID {display_id} | {name}: ошибок/пропусков фотографий и альбомов: {album_errors}.")

    if items:
        post_ui_event(
            "status",
            f"VK ID {display_id}: подготовлено файлов — {len(items)} "
            f"(аватар: {'да' if any(item['kind'] == 'avatar' for item in items) else 'нет'}, фото из альбомов: {album_photos_downloaded}).",
        )
        return {
            "status": "downloaded",
            "items": items,
            "image_path": next((item["image_path"] for item in items if item["kind"] == "avatar"), items[0]["image_path"]),
            "json_path": next((item["json_path"] for item in items if item["kind"] == "avatar"), items[0]["json_path"]),
            "uid": abs(uid),
            "name": name,
            "base_dir": str(base_dir),
            "avatar_result_kind": avatar_result_kind,
            "avatar_reason": avatar_reason,
            "album_photos_downloaded": album_photos_downloaded,
            "album_errors": album_errors,
        }

    _remove_dir_if_empty(base_dir)
    if avatar_result_kind == "no_avatar":
        post_ui_event("status", f"VK ID {display_id}: нет доступной аватарки и пригодных фотографий.")
        return {
            "status": "no_avatar", "uid": abs(uid), "name": name,
            "reason": avatar_reason or "Нет доступных фотографий", "base_dir": str(base_dir),
            "album_errors": album_errors,
        }
    if avatar_result_kind == "download_error":
        post_ui_event("status", f"VK ID {display_id}: ошибка загрузки аватара; фотографии альбомов не сохранены.")
        return {
            "status": "download_error", "uid": abs(uid), "name": name,
            "reason": avatar_reason or "Ошибка загрузки", "base_dir": str(base_dir),
            "album_errors": album_errors,
        }
    return {
        "status": "no_media", "uid": abs(uid), "name": name,
        "base_dir": str(base_dir), "avatar_result_kind": avatar_result_kind,
        "album_photos_downloaded": 0, "album_errors": album_errors,
    }



def process_users_with_friends(
    root_users: list, depth: int, MAX_WORKERS: int,
    base_output_dir: str | Path | None = None, importer=None,
    delete_processed_files: bool = False, import_to_database: bool = True,
    download_albums: bool = False,
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
            if uid in EXISTING_PROFILE_IDS and not download_albums:
                print(f"[DB-SKIP] ID {uid} уже есть в profiles.profile_url активной БД; пропускаем в режиме только аватарок.")
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
            print(f"\n[{index}/{len(unique_level)}] Уровень {level}: {name} (ID {uid})")
            try:
                process_entity(
                    user, base_dir, is_group=False, MAX_WORKERS=MAX_WORKERS,
                    importer=importer, delete_processed_files=delete_processed_files,
                    import_to_database=import_to_database,
                    download_albums=download_albums,
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
                            and (download_albums or friend_id not in EXISTING_PROFILE_IDS)):
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
        # Учитываем сохранённый параметр основного приложения FaceSearch
        # «Обновлять CROP и метаданные», чтобы повторный импорт имел ту же семантику.
        self.face_search_gui_settings = {}
        settings_loader = getattr(self.module, "_load_gui_settings", None)
        if callable(settings_loader):
            try:
                loaded_settings = settings_loader()
                if isinstance(loaded_settings, dict):
                    self.face_search_gui_settings = loaded_settings
            except Exception as exc:
                print(f"[DB-SETTINGS-WARN] Не удалось прочитать настройки FaceSearch GUI: {exc}")
        self.overwrite_existing_crops_requested = bool(
            self.face_search_gui_settings.get("overwrite_existing_crops", False)
        )
        # В FaceSearch обновление существующих CROP завершает работу очисткой
        # прежних JPEG. При create_crop_files=False новые JPEG не записываются,
        # поэтому не разрешаем этот режим в загрузчике: иначе можно оставить
        # действующие записи images/faces без файлов CROP.
        self.overwrite_existing_crops = bool(
            self.overwrite_existing_crops_requested and self.create_crop_files
        )
        if self.overwrite_existing_crops_requested and not self.create_crop_files:
            print(
                "[DB-SETTINGS-WARN] В настройках FaceSearch включено обновление существующих CROP, "
                "но создание CROP-файлов в загрузчике отключено. Для защиты существующих CROP "
                "обновление существующих CROP на этот запуск отключено; метаданные профиля "
                "и ссылки источников продолжают обновляться штатным импортёром."
            )
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
        try:
            metadata = json.loads(json_path.read_text(encoding="utf-8-sig"))
        except Exception as exc:
            raise RuntimeError(f"Ошибка JSON {json_path.name}: {exc}") from exc

        if self.app is None:
            self.status_callback("Инициализация модели распознавания FaceSearch…")
            backend_value = self.config.get("backend", "gpu")
            backend = self.module.BACKEND_CPU if backend_value in {"cpu", "processor", "процессор"} else self.module.BACKEND_GPU
            self.app = self.module.create_face_app(backend=backend)
        self.status_callback(f"Импорт в FaceSearch: {image_path.name}…")

        metadata_validator = getattr(self.module, "_is_standard_metadata_document", None)
        metadata_is_standard = (
            bool(metadata_validator(metadata)) if callable(metadata_validator)
            else isinstance(metadata, dict)
        )
        if metadata_is_standard:
            clean = dict(metadata)
            info = dict(metadata.get("info") or {}) if isinstance(metadata.get("info"), dict) else {}
            for key in (_META_RESOLUTION, _META_VERSION):
                info.pop(key, None)
            clean["info"] = info

            def import_operation():
                return self.module.import_image_bundle(
                    self.conn,
                    self.app,
                    image_path,
                    json_path,
                    delete_source=False,
                    create_crop_files=self.create_crop_files,
                    overwrite_existing_crops=self.overwrite_existing_crops,
                    metadata=clean,
                    source_key=source_key,
                )
        else:
            # Поведение основного GUI: синтаксически корректный JSON, не
            # соответствующий стандартной оболочке, не используется как метаданные.
            # Изображение обрабатывается как локальное, а метаданные очищаются.
            local_import = getattr(self.module, "process_local_image", None)
            if not callable(local_import):
                raise RuntimeError(
                    f"JSON {json_path.name} не соответствует стандарту FaceSearch, "
                    "а функция локального импорта недоступна."
                )
            print(
                f"[DB-METADATA-WARN] {json_path.name}: структура не соответствует "
                "стандарту FaceSearch; изображение будет импортировано без метаданных JSON."
            )

            def import_operation():
                return local_import(
                    self.conn,
                    self.app,
                    image_path,
                    delete_source=False,
                    create_crop_files=self.create_crop_files,
                    overwrite_existing_crops=self.overwrite_existing_crops,
                    store_metadata=False,
                    clear_metadata=True,
                )

        # В основном FaceSearch временные ошибки записи MySQL/MSSQL повторяются
        # с ограничением попыток и backoff. SQLite и другие backend не повторяются.
        backend = getattr(self.conn, "backend", None)
        mysql_backend = getattr(self.module, "DB_TYPE_MYSQL", object())
        mssql_backend = getattr(self.module, "DB_TYPE_MSSQL", object())
        if backend == mysql_backend:
            max_attempts = 6
        elif backend == mssql_backend:
            max_attempts = 4
        else:
            max_attempts = 1

        transient_checker = getattr(self.module, "is_transient_database_conflict", None)
        retry_delay = getattr(self.module, "database_retry_delay", None)
        for attempt in range(max_attempts):
            try:
                result = import_operation()
                self.imported_count += 1
                return result
            except Exception as exc:
                try:
                    self.conn.rollback()
                except Exception:
                    pass
                if attempt + 1 >= max_attempts or not callable(transient_checker):
                    raise
                try:
                    transient = bool(transient_checker(exc))
                except Exception:
                    transient = False
                if not transient:
                    raise

                if callable(retry_delay):
                    try:
                        delay = float(retry_delay(
                            attempt,
                            base_seconds=0.30,
                            max_seconds=3.0,
                            jitter_seconds=0.35,
                        ))
                    except Exception:
                        delay = min(3.0, 0.30 * (2 ** attempt))
                else:
                    delay = min(3.0, 0.30 * (2 ** attempt))
                print(
                    f"[DB-RETRY] {image_path.name}: временный конфликт БД "
                    f"({type(exc).__name__}: {exc}); повтор {attempt + 2}/{max_attempts} "
                    f"через {delay:.2f} с."
                )
                time.sleep(max(0.0, delay))

        raise RuntimeError("Не удалось завершить импорт после повторных попыток.")

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
    if value.lower().startswith(("vk.com/", "www.vk.com/", "vk.ru/", "www.vk.ru/")):
        value = "https://" + value
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


def _format_id_list(values) -> str:
    """Показывает каждый ID отдельно; никогда не сворачивает список в диапазоны."""
    ids = sorted({int(value) for value in values})
    return ", ".join(map(str, ids)) if ids else "—"


def parse_specific_identifiers(raw_value: str) -> list[str]:
    """Разбирает список ID/ссылок/коротких имён VK, разделённых пробелами, запятыми или ;."""
    values = []
    seen = set()
    for raw in re.split(r"[,;\s]+", str(raw_value or "").strip()):
        token = normalize_vk_identifier(raw)
        if token and token not in seen:
            seen.add(token)
            values.append(token)
    return values


def collect_specific_user_ids(raw_value: str, friend_depth: int = 0) -> list[int]:
    """Разрешает введённые ID/ссылки и собирает друзей до заданной глубины без дублей."""
    identifiers = parse_specific_identifiers(raw_value)
    if not identifiers:
        raise ValueError("Введите хотя бы один ID, ссылку на профиль или короткое имя VK.")
    try:
        depth = max(0, int(friend_depth or 0))
    except (TypeError, ValueError):
        depth = 0
    if depth > 10:
        raise ValueError("Глубина обхода друзей должна быть от 0 до 10.")

    _load_tokens_if_needed()
    roots = []
    root_seen = set()
    with requests.Session() as session:
        for identifier in identifiers:
            if STOP_REQUESTED.is_set():
                break
            if re.fullmatch(r"\d+", identifier):
                user_id = int(identifier)
                if user_id <= 0:
                    print(f"[SPECIFIC-ID-SKIP] Пропущен некорректный ID пользователя: {identifier}")
                    continue
            elif re.fullmatch(r"-\d+", identifier):
                print(f"[SPECIFIC-ID-SKIP] {identifier} — ID сообщества; этот режим принимает ID пользователей.")
                continue
            else:
                try:
                    resolved = _vk_get_page(
                        "utils.resolveScreenName", {"screen_name": identifier}, session, timeout=20
                    )
                except Exception as exc:
                    print(f"[SPECIFIC-ID-ERROR] Не удалось разрешить «{identifier}»: {type(exc).__name__}: {exc}")
                    continue
                if not isinstance(resolved, dict) or not resolved.get("object_id"):
                    print(f"[SPECIFIC-ID-NOT-FOUND] VK не разрешил имя/ссылку «{identifier}».")
                    continue
                if str(resolved.get("type", "")).lower() != "user":
                    print(f"[SPECIFIC-ID-SKIP] «{identifier}» разрешён как {resolved.get('type')}; нужны личные профили пользователей.")
                    continue
                try:
                    user_id = int(resolved["object_id"])
                except (TypeError, ValueError):
                    print(f"[SPECIFIC-ID-SKIP] VK вернул некорректный ID для «{identifier}».")
                    continue
                if user_id <= 0:
                    continue
            if user_id not in root_seen:
                roots.append(user_id)
                root_seen.add(user_id)

        if not roots and not STOP_REQUESTED.is_set():
            raise ValueError("VK не разрешил ни одного введённого ID/профиля пользователя.")

        collected = list(roots)
        visited = set(roots)
        frontier = list(roots)
        for level in range(depth):
            if STOP_REQUESTED.is_set() or not frontier:
                break
            next_frontier = []
            print(f"[FRIENDS] Уровень {level + 1}/{depth}: поиск друзей у {len(frontier)} профилей.")
            post_ui_event("status", f"Получение друзей: уровень {level + 1}/{depth}; профилей: {len(frontier)}…")
            for index, uid in enumerate(frontier, start=1):
                if STOP_REQUESTED.is_set():
                    break
                try:
                    friends = get_user_friends(uid, session)
                except Exception as exc:
                    print(f"[FRIENDS-ERROR] ID {uid}: {type(exc).__name__}: {exc}")
                    continue
                added = 0
                for friend in friends:
                    if not isinstance(friend, dict):
                        continue
                    try:
                        friend_id = int(friend.get("id"))
                    except (TypeError, ValueError):
                        continue
                    if friend_id > 0 and friend_id not in visited:
                        visited.add(friend_id)
                        collected.append(friend_id)
                        next_frontier.append(friend_id)
                        added += 1
                print(f"[FRIENDS] {index}/{len(frontier)}: ID {uid}; друзей доступно {len(friends)}, новых {added}.")
            print(f"[FRIENDS] Уровень {level + 1}: добавлено {len(next_frontier)} уникальных профилей.")
            frontier = next_frontier

    print(f"[SPECIFIC-ID] Введено корневых профилей: {len(roots)}; всего ID с учётом глубины {depth}: {len(collected)}.")
    return collected


def run_download_job(
    start_id: int, max_workers: int, output_dir: str,
    importer=None, delete_processed_files: bool = False, import_to_database: bool = True,
    download_albums: bool = False, all_albums: bool = False, album_photo_limit: int = 0,
    selected_system_albums: list[int] | tuple[int, ...] | set[int] | None = None,
    download_tagged_photos: bool = False,
    source_mode: str = "counter", specific_ids: str = "", friend_depth: int = 0,
    auto_workers: bool = False,
):
    """Обрабатывает либо последовательные ID, либо конкретные ID и их друзей."""
    global EXISTING_PROFILE_IDS
    source_mode = str(source_mode or "counter").strip().lower()
    specific_mode = source_mode == "specific"
    try:
        start_id = int(start_id)
    except (TypeError, ValueError):
        start_id = 1
    if not specific_mode and start_id < 1:
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
    requested_worker_count = max(1, min(64, int(max_workers)))
    auto_workers = bool(auto_workers)

    # В автоматическом режиме стартовый и максимальный пределы берём из того же
    # алгоритма, что и в основной форме FaceSearch. Контроллер продолжает
    # наблюдать нагрузку и в ручном режиме, но не меняет выбранное число потоков.
    profile_data = {}
    if importer is not None:
        importer_config = getattr(importer, "config", {})
        if isinstance(importer_config, dict):
            profile_data = importer_config.get("profile") or {}
    db_type = str(
        profile_data.get("type")
        or profile_data.get("backend")
        or profile_data.get("driver")
        or ""
    ).strip().lower() if isinstance(profile_data, dict) else ""
    remote_db = db_type not in ("", "sqlite", "sqlite3", "local", "file")

    worker_controller = None
    try:
        worker_controller = AdaptiveWorkerController(
            initial_workers=0 if auto_workers else requested_worker_count,
            backend="cpu",
            operation="vk_download",
            input_paths=[],
            output_path=output_root,
            input_is_network=True,
            remote_db=remote_db,
            auto_adjust=auto_workers,
        )
    except Exception as exc:
        if auto_workers:
            raise RuntimeError(
                "Не удалось запустить автоматическое определение числа потоков. "
                "Проверьте facesearch/processing/telemetry.py и зависимости FaceSearch."
            ) from exc
        print(f"[WORKERS-WARN] Мониторинг ресурсов недоступен; ручное число потоков сохранено: {exc}")

    worker_count = (
        max(1, int(worker_controller.current_workers))
        if worker_controller is not None else requested_worker_count
    )
    pool_worker_limit = (
        max(worker_count, int(worker_controller.max_workers))
        if worker_controller is not None else worker_count
    )
    adaptive_completed = 0
    if auto_workers and worker_controller is not None:
        initial_worker_status = (
            f"Авто: старт {worker_controller.current_workers} "
            f"из {worker_controller.max_workers} потоков"
        )
    elif worker_controller is not None:
        initial_worker_status = (
            f"Мониторинг (ручной режим): фиксировано {worker_count} поток(ов)"
        )
    else:
        initial_worker_status = f"Ручной режим: фиксировано {worker_count} поток(ов)"
    post_ui_event("workers_status", initial_worker_status)
    print(f"[WORKERS] {initial_worker_status}")

    download_albums = bool(download_albums)
    all_albums = bool(all_albums) and download_albums
    try:
        album_photo_limit = max(0, int(album_photo_limit or 0))
    except (TypeError, ValueError):
        album_photo_limit = 0
    current_id = start_id
    specific_user_ids = []
    specific_cursor = 0
    if specific_mode:
        specific_user_ids = collect_specific_user_ids(specific_ids, friend_depth=friend_depth)
        if not specific_user_ids and not STOP_REQUESTED.is_set():
            raise ValueError("Не удалось получить ID для обработки.")
        print(
            f"[SOURCE] Режим конкретных ID; глубина друзей: {max(0, int(friend_depth or 0))}; "
            f"профилей к обработке: {len(specific_user_ids)}."
        )

    checked_total = 0
    skipped_total = 0
    returned_total = 0
    missing_profiles_total = 0
    deactivated_total = 0
    no_avatar_total = 0
    album_errors_total = 0
    download_errors_total = 0
    import_errors_total = 0
    api_errors_total = 0
    downloaded_total = 0
    # imported_total — число изображений, на которых импортёр завершился без исключения.
    # Это не равно количеству новых/обновлённых строк БД: результат может быть пропуском.
    imported_total = 0
    db_new_records_total = 0
    db_updated_records_total = 0
    db_skipped_existing_total = 0
    db_skipped_duplicate_total = 0
    db_no_faces_total = 0
    db_unclassified_total = 0
    db_removed_records_total = 0
    db_restored_crops_total = 0
    downloaded_without_import_total = 0
    cleanup_errors_total = 0

    print(f"🖼️ VK Downloader — каталог: {output_root}")
    if specific_mode:
        print(f"▶ Источник ID: конкретный список; глубина друзей: {max(0, int(friend_depth or 0))}; потоков: {worker_count}.")
    else:
        print(f"▶ Начальный ID: {start_id}; потоков загрузки: {worker_count}; пачка users.get: до {USER_BATCH_SIZE} ID.")
    try:
        selected_ids_for_log = {
            int(value) for value in (
                DEFAULT_SYSTEM_ALBUM_IDS if selected_system_albums is None else selected_system_albums
            )
        }
    except (TypeError, ValueError):
        selected_ids_for_log = set(DEFAULT_SYSTEM_ALBUM_IDS)
    selected_labels = [label for album_id, label in SYSTEM_ALBUM_OPTIONS if album_id in selected_ids_for_log]
    print(
        f"[НАСТРОЙКИ] Загрузка альбомов: {'включена' if download_albums else 'выключена'}; "
        f"режим: {'все альбомы и фото с отметками' if all_albums else 'выбранные системные альбомы: ' + (', '.join(selected_labels) if selected_labels else 'не выбраны')}; "
        f"фото с отметками: {'да' if download_tagged_photos or all_albums else 'нет'}; "
        f"лимит на альбом: {'без ограничений' if album_photo_limit == 0 else album_photo_limit}."
    )

    if delete_processed_files and import_to_database:
        print(
            "🧹 Очистка включена: после успешного результата импорта удаляются локальные "
            "изображения и JSON, независимо от настройки создания CROP; пустые папки удаляются. "
            "CROP и ссылки источников хранятся/восстанавливаются на стороне FaceSearch."
        )
    print("ℹ️ Режим последовательного обхода работает до нажатия «Остановить»." if not specific_mode else "ℹ️ Режим конкретных ID завершится после обработки списка или нажатия «Остановить».")

    with requests.Session() as session:
        # Сетевые загрузки выполняются параллельно, а импорт в БД остаётся в
        # потоке, где создано соединение FaceSearch (важно для SQLite).
        # AUTO использует пул с верхним пределом контроллера, а фактическое число
        # одновременно подаваемых заданий меняется без пересоздания ThreadPoolExecutor.
        with ThreadPoolExecutor(max_workers=pool_worker_limit, thread_name_prefix="VKDownload") as executor:
            while not STOP_REQUESTED.is_set():
                if specific_mode:
                    if specific_cursor >= len(specific_user_ids):
                        break
                    batch_ids = specific_user_ids[specific_cursor:specific_cursor + USER_BATCH_SIZE]
                    batch_start = specific_cursor + 1
                    batch_end = batch_start + len(batch_ids)
                    batch_label = f"конкретные ID {batch_start}–{batch_end - 1} из {len(specific_user_ids)}"
                else:
                    batch_start = current_id
                    batch_end = batch_start + USER_BATCH_SIZE
                    batch_ids = list(range(batch_start, batch_end))
                    batch_label = f"ID {batch_start}–{batch_end - 1}"

                # Уже сохранённые профили логируем по ID, но не отправляем повторно в VK API.
                with EXISTING_PROFILE_IDS_LOCK:
                    # При загрузке альбомов профили из БД всё равно проверяются в VK: у них
                    # могут быть новые/ещё не импортированные фотографии.
                    skipped_ids = [
                        uid for uid in batch_ids
                        if uid in EXISTING_PROFILE_IDS and not download_albums
                    ]
                skipped_set = set(skipped_ids)
                request_ids = [uid for uid in batch_ids if uid not in skipped_set]
                skipped = len(skipped_ids)
                skipped_total += skipped
                checked_total += len(batch_ids)

                if skipped_ids:
                    print(
                        f"[DB-SKIP] Уже есть в активной БД FaceSearch ({skipped} ID): "
                        f"{_format_id_list(skipped_ids)}"
                    )

                post_ui_event(
                    "status",
                    f"Проверка {batch_label}; "
                    f"уже в БД: {skipped}; ожидает проверки VK: {len(request_ids)}."
                )
                post_ui_event(
                    "progress", 0, 0,
                    f"{batch_label} | проверено: {checked_total:,} | "
                    f"уже в БД: {skipped_total:,}."
                )

                returned_users = []
                if request_ids:
                    try:
                        # POST передаёт полный пакет ID без чрезмерно длинного URL.
                        request_preview = ", ".join(map(str, request_ids[:20]))
                        if len(request_ids) > 20:
                            request_preview += f", … (+{len(request_ids) - 20} ID)"
                        print(
                            f"[VK-REQUEST] users.get: {len(request_ids)} отдельных ID; "
                            f"диапазоны не используются. Список: {request_preview}"
                        )
                        returned_users = get_user_info(request_ids, session)
                    except Exception as exc:
                        api_errors_total += 1
                        print(
                            f"[VK-API-ERROR] Пакет {batch_label}: "
                            f"users.get завершился ошибкой: {type(exc).__name__}: {exc}. "
                            f"Пакет не будет пропущен; повторите задание."
                        )
                        post_ui_event(
                            "status",
                            f"Ошибка VK API для {batch_label}; задание остановлено, повторите его."
                        )
                        if not specific_mode:
                            current_id = batch_start
                        break

                users_by_id = {}
                for user in returned_users:
                    try:
                        uid = int(user.get("id"))
                    except (TypeError, ValueError, AttributeError):
                        continue
                    if uid in request_ids:
                        users_by_id[uid] = user
                returned_total += len(users_by_id)

                missing_ids = [uid for uid in request_ids if uid not in users_by_id]
                missing_count = len(missing_ids)
                missing_profiles_total += missing_count
                if missing_ids:
                    print(
                        f"[VK-NOT-FOUND] Профиль не возвращён users.get для {missing_count} ID "
                        f"(профиль может быть удалён, недоступен или не существовать): "
                        f"{_format_id_list(missing_ids)}"
                    )

                print(
                    f"[BATCH] {batch_label}: "
                    f"уже в БД {skipped}; запрошено у VK {len(request_ids)}; "
                    f"профилей получено {len(users_by_id)}; профиль не получен {missing_count}."
                )

                tasks = []
                name_by_id = {}
                for uid in request_ids:
                    user = users_by_id.get(uid)
                    if user is None:
                        continue
                    display_name = (
                        f"{user.get('first_name', '')} {user.get('last_name', '')}".strip()
                        or user.get("screen_name")
                        or f"user_{uid}"
                    )
                    if user.get("deactivated"):
                        deactivated_total += 1
                        print(
                            f"[VK-DEACTIVATED] ID {uid} | {display_name}: "
                            f"профиль деактивирован ({user.get('deactivated')}); аватар не загружается."
                        )
                        continue

                    folder_name = sanitize_filename(f"{display_name} ({uid})")
                    base_dir = str(output_root / folder_name)
                    # Не создаём папку заранее: при отсутствии аватарки или ошибке сети
                    # не останется пустого каталога.
                    tasks.append((uid, user, base_dir))
                    name_by_id[uid] = display_name

                next_task_index = 0
                pending = {}
                batch_errors = 0
                # При остановке возобновляемся с самого раннего профиля,
                # который не удалось обработать или который ещё не запускался.
                batch_retry_ids = set()
                batch_finished = 0

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
                            download_albums,
                            all_albums,
                            album_photo_limit,
                            selected_system_albums,
                            download_tagged_photos,
                        )
                        pending[future] = uid

                submit_next_tasks()
                while pending:
                    completed_futures, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed_futures:
                        uid = pending.pop(future)

                        # Подстраиваем целевое число активных профилей по фактической
                        # скорости завершения задач и показателям системной нагрузки.
                        adaptive_completed += 1
                        if worker_controller is not None:
                            report = worker_controller.observe(adaptive_completed)
                            if report is not None:
                                worker_count = max(
                                    1, min(pool_worker_limit, int(report.get("workers") or worker_count))
                                )
                                worker_status = worker_controller.status_text(report).replace(
                                    "ф/с", "проф./с"
                                )
                                post_ui_event("workers_status", worker_status)
                                if report.get("changed"):
                                    print(f"[AUTO-WORKERS] {worker_status}")

                        # Сразу запускаем следующую сетевую задачу до импорта
                        # результата: свободный worker не простаивает на записи в БД.
                        submit_next_tasks()
                        try:
                            outcome = future.result()
                            if not outcome:
                                raise RuntimeError("задача завершилась без результата")
                            outcome_status = outcome.get("status")
                            try:
                                album_errors_total += max(0, int(outcome.get("album_errors") or 0))
                            except (TypeError, ValueError):
                                pass

                            if outcome_status == "already_in_db":
                                # Защита от случая, когда профиль появился в БД после построения списка.
                                skipped_total += 1
                                print(f"[DB-SKIP] ID {uid} | {outcome.get('name', name_by_id.get(uid, ''))}: профиль уже есть в БД.")
                                continue

                            if outcome_status == "no_avatar":
                                no_avatar_total += 1
                                continue

                            if outcome_status == "no_media":
                                if outcome.get("avatar_result_kind") == "no_avatar":
                                    no_avatar_total += 1
                                print(
                                    f"[NO-MEDIA] ID {uid} | {outcome.get('name', name_by_id.get(uid, ''))}: "
                                    "не найдено новых файлов для загрузки."
                                )
                                continue

                            if outcome_status == "download_error":
                                download_errors_total += 1
                                batch_errors += 1
                                batch_retry_ids.add(int(uid))
                                continue

                            if outcome_status != "downloaded":
                                raise RuntimeError(f"неизвестный статус результата: {outcome_status}")

                            downloaded_uid = int(outcome["uid"])
                            downloaded_name = str(outcome.get("name") or name_by_id.get(uid) or f"user_{uid}")
                            downloaded_total += 1
                            if outcome.get("avatar_result_kind") == "no_avatar":
                                no_avatar_total += 1

                            file_items = outcome.get("items") or []
                            if not file_items and outcome.get("image_path") and outcome.get("json_path"):
                                file_items = [{
                                    "image_path": outcome["image_path"],
                                    "json_path": outcome["json_path"],
                                    "source_key": f"vk:{downloaded_uid}:avatar",
                                    "kind": "avatar",
                                }]

                            imported_for_user = 0
                            saved_for_user = 0
                            for file_item in file_items:
                                image_path = Path(file_item["image_path"])
                                json_path = Path(file_item["json_path"])
                                source_key = str(file_item.get("source_key") or f"vk:{downloaded_uid}:avatar")
                                media_kind = str(file_item.get("kind") or "photo")

                                if importer is not None and import_to_database:
                                    try:
                                        import_result = importer.import_image(
                                            image_path,
                                            json_path,
                                            source_key=source_key,
                                        )
                                        if not isinstance(import_result, dict):
                                            raise RuntimeError(
                                                f"импортёр вернул неожиданный результат: {type(import_result).__name__}"
                                            )
                                    except Exception as exc:
                                        batch_errors += 1
                                        batch_retry_ids.add(int(downloaded_uid))
                                        import_errors_total += 1
                                        print(
                                            f"[DB-ERROR] ID {downloaded_uid} | {downloaded_name} | {image_path.name}: "
                                            f"файл скачан, но импорт в FaceSearch завершился ошибкой "
                                            f"{type(exc).__name__}: {exc}. Файлы оставлены для повторного импорта: {image_path.parent}"
                                        )
                                        continue

                                    imported_for_user += 1
                                    imported_total += 1
                                    with EXISTING_PROFILE_IDS_LOCK:
                                        EXISTING_PROFILE_IDS.add(abs(downloaded_uid))

                                    def _db_result_count(key):
                                        try:
                                            return max(0, int(import_result.get(key) or 0))
                                        except (TypeError, ValueError):
                                            return 0

                                    new_records = _db_result_count("added")
                                    updated_records = _db_result_count("updated")
                                    removed_records = _db_result_count("removed")
                                    restored_crops = _db_result_count("restored")
                                    created_records = _db_result_count("faces")
                                    detected_raw = import_result.get("detected_faces")
                                    try:
                                        detected_faces = (
                                            None if detected_raw is None
                                            else max(0, int(detected_raw))
                                        )
                                    except (TypeError, ValueError):
                                        detected_faces = None
                                    result_message = str(import_result.get("message") or "").strip()
                                    normalized_message = result_message.casefold()
                                    known_existing_source = (
                                        "исходное изображение уже есть в бд" in normalized_message
                                        or "исправлена привязка локальных crop" in normalized_message
                                    )
                                    db_removed_records_total += removed_records
                                    db_restored_crops_total += restored_crops
                                    should_cleanup_source = False
                                    detected_label = (
                                        "не запускалось/не сообщено"
                                        if detected_faces is None else str(detected_faces)
                                    )
                                    detail = (
                                        f"обнаружено лиц: {detected_label}; "
                                        f"новых записей: {new_records}; обновлено записей: {updated_records}; "
                                        f"удалено устаревших записей: {removed_records}; "
                                        f"восстановлено CROP: {restored_crops}; "
                                        f"источник: {source_key}"
                                    )
                                    if result_message:
                                        detail += f"; результат импортёра: {result_message}"

                                    if new_records or updated_records:
                                        db_new_records_total += new_records
                                        db_updated_records_total += updated_records
                                        should_cleanup_source = True
                                        if new_records and updated_records:
                                            tag = "DB-CHANGED"
                                        elif new_records:
                                            tag = "DB-NEW"
                                        else:
                                            tag = "DB-UPDATED"
                                        print(
                                            f"[{tag}] ID {downloaded_uid} | {downloaded_name} | {media_kind}: {detail}."
                                        )
                                    elif created_records > 0:
                                        # Защита от изменения контракта импортёра: поле faces
                                        # обычно равно added + updated, но если нет, результат
                                        # нужно явно показать, а не называть пропуском.
                                        db_unclassified_total += 1
                                        should_cleanup_source = True
                                        print(
                                            f"[DB-RESULT-WARN] ID {downloaded_uid} | {downloaded_name} | {media_kind}: "
                                            f"импортёр сообщил {created_records} обработанных записей, но не заполнил "
                                            f"added/updated; {detail}."
                                        )
                                    elif restored_crops > 0:
                                        db_skipped_existing_total += 1
                                        should_cleanup_source = True
                                        print(
                                            f"[DB-UPDATED] ID {downloaded_uid} | {downloaded_name} | {media_kind}: "
                                            f"новых/обновлённых строк БД нет, восстановлены CROP; {detail}."
                                        )
                                    elif known_existing_source:
                                        db_skipped_existing_total += 1
                                        should_cleanup_source = True
                                        print(
                                            f"[DB-SKIP-EXISTING] ID {downloaded_uid} | {downloaded_name} | {media_kind}: "
                                            f"источник уже представлен в БД, повторное распознавание пропущено; {detail}."
                                        )
                                    elif detected_faces == 0:
                                        db_no_faces_total += 1
                                        # Ноль лиц — штатный завершённый результат, а не ошибка.
                                        # При включённом удалении не сохраняем заведомо бесполезную пару.
                                        should_cleanup_source = True
                                        print(
                                            f"[DB-NO-FACE] ID {downloaded_uid} | {downloaded_name} | {media_kind}: "
                                            f"детектор не обнаружил лиц; новые/обновлённые записи не созданы; "
                                            f"при включённой очистке исходники будут удалены; {detail}."
                                        )
                                    elif detected_faces is not None and detected_faces > 0:
                                        # Импортёр успешно обработал изображение и обнаружил лицо,
                                        # но запись уже представлена другим CROP. Считаем такой
                                        # результат обработанным: при включённой очистке исходник
                                        # можно удалить так же, как при совпадении source_key.
                                        db_skipped_duplicate_total += 1
                                        should_cleanup_source = True
                                        print(
                                            f"[DB-SKIP-DUPLICATE] ID {downloaded_uid} | {downloaded_name} | {media_kind}: "
                                            f"лицо обнаружено, но новые/обновлённые записи не нужны (вероятный дубликат CROP); "
                                            f"{detail}."
                                        )
                                    else:
                                        db_unclassified_total += 1
                                        print(
                                            f"[DB-SKIP-UNKNOWN] ID {downloaded_uid} | {downloaded_name} | {media_kind}: "
                                            f"импортёр не подтвердил запись или пропуск в БД; исходники сохранены: "
                                            f"{image_path.parent}; {detail}."
                                        )

                                    # Локальное изображение нужно для распознавания и текущего импорта.
                                    # После подтверждённого результата БД его можно удалять независимо
                                    # от create_crop_files: дальнейшее восстановление CROP использует URL
                                    # источника/метаданные FaceSearch, а не временный файл загрузчика.
                                    if delete_processed_files and should_cleanup_source:
                                        try:
                                            # Сначала удаляем JSON: если удаление метаданных не удастся,
                                            # изображение останется для повторного импорта/проверки.
                                            json_path.unlink(missing_ok=True)
                                            image_path.unlink(missing_ok=True)
                                            _remove_empty_directories_upwards(image_path.parent, output_root)
                                            print(
                                                f"[CLEANUP] ID {downloaded_uid}: удалены локальные файлы "
                                                f"{image_path.name} и JSON после успешного результата обработки."
                                            )
                                        except Exception as exc:
                                            cleanup_errors_total += 1
                                            batch_retry_ids.add(int(downloaded_uid))
                                            print(
                                                f"[CLEANUP-ERROR] ID {downloaded_uid}: импорт завершился, "
                                                f"но не удалось удалить {image_path.name} и/или JSON: {exc}"
                                            )
                                    elif delete_processed_files:
                                        print(
                                            f"[CLEANUP-SKIP] ID {downloaded_uid}: пара {image_path.name} не удалена, "
                                            "поскольку результат импортёра не подтвердил успешную обработку."
                                        )
                                else:
                                    saved_for_user += 1
                                    downloaded_without_import_total += 1
                                    print(
                                        f"[SAVED-NO-IMPORT] ID {downloaded_uid} | {downloaded_name} | {media_kind}: "
                                        f"файл сохранён на диск, импорт выключен: {image_path}"
                                    )

                            if imported_for_user or saved_for_user:
                                log_user_processed(downloaded_name, str(downloaded_uid), False)

                        except Exception as exc:
                            batch_errors += 1
                            batch_retry_ids.add(int(uid))
                            download_errors_total += 1
                            print(
                                f"[DOWNLOAD-ERROR] ID {uid} | {name_by_id.get(uid, f'user_{uid}')}: "
                                f"{type(exc).__name__}: {exc}"
                            )
                        finally:
                            batch_finished += 1
                            if tasks:
                                post_ui_event(
                                    "progress", batch_finished, len(tasks),
                                    f"Пакет {batch_label} | "
                                    f"профили: {batch_finished}/{len(tasks)} | "
                                    f"файлов обработано импортёром: {imported_total:,} | новых записей: {db_new_records_total:,} | "
                                    f"обновлено: {db_updated_records_total:,} | без лиц: {db_no_faces_total:,} | "
                                    f"дубликатов CROP: {db_skipped_duplicate_total:,} | ошибок загрузки/импорта: "
                                    f"{download_errors_total + import_errors_total:,}."
                                )
                    # При нажатии «Остановить» новые задания не добавляются в пул.
                    submit_next_tasks()

                if tasks:
                    post_ui_event(
                        "progress", batch_finished, len(tasks),
                        f"Пакет {batch_label} | профили: {batch_finished}/{len(tasks)} | "
                        f"файлов обработано импортёром: {imported_total:,} | новых записей: {db_new_records_total:,} | "
                        f"обновлено: {db_updated_records_total:,} | без лиц: {db_no_faces_total:,} | "
                        f"дубликатов CROP: {db_skipped_duplicate_total:,} | "
                        f"ошибок загрузки/импорта: {download_errors_total + import_errors_total:,}."
                    )

                if STOP_REQUESTED.is_set():
                    # Все уже отправленные задачи завершены. Возобновляемся с самого
                    # раннего ID, который завершился ошибкой или ещё не был отправлен.
                    # Не откатываемся на начало всей пачки только из-за одной ошибки.
                    if not specific_mode:
                        resume_candidates = set(batch_retry_ids)
                        if next_task_index < len(tasks):
                            try:
                                resume_candidates.add(int(tasks[next_task_index][0]))
                            except (TypeError, ValueError, IndexError):
                                pass
                        current_id = min(resume_candidates) if resume_candidates else batch_end
                        print(
                            f"[STOP-CURSOR] Следующий запуск продолжится с ID {current_id}; "
                            f"повторяемых/не запущенных профилей в текущей пачке: {len(resume_candidates)}."
                        )
                    break

                print(
                    f"[BATCH-SUMMARY] {batch_label}: "
                    f"уже в БД {skipped}; профиль не получен {missing_count}; "
                    f"задач на скачивание {len(tasks)}; ошибок загрузки/импорта в пакете {batch_errors}."
                )
                if specific_mode:
                    specific_cursor += len(batch_ids)
                else:
                    current_id = batch_end

    # После Stop все активные futures уже завершены. Делаем финальную безопасную
    # уборку: некоторые профили могли оставить пустые папки без пары image/JSON
    # (например, после ошибки/отсутствия медиа или от прежнего запуска).
    # Удаляются только действительно пустые вложенные каталоги; любые файлы
    # (в том числе сохранённые из-за ошибки импорта) остаются нетронутыми.
    if delete_processed_files and import_to_database:
        removed_empty_dirs = _remove_empty_directories_recursively(output_root)
        print(
            f"[CLEANUP-DIRS] Финальная очистка каталога загрузки: "
            f"удалено пустых папок: {removed_empty_dirs}."
        )

    total_errors = api_errors_total + album_errors_total + download_errors_total + import_errors_total + cleanup_errors_total
    print("\n" + "=" * 68)
    print(
        f"[ИТОГ] Проверено ID: {checked_total:,}; уже есть в БД: {skipped_total:,}; "
        f"профиль не получен от VK: {missing_profiles_total:,}; "
        f"деактивировано: {deactivated_total:,}; без доступной аватарки: {no_avatar_total:,}; "
        f"профилей с загруженными файлами: {downloaded_total:,}; обработано импортёром файлов: {imported_total:,}; "
        f"новых записей CROP/лиц: {db_new_records_total:,}; обновлено записей CROP/лиц: {db_updated_records_total:,}; "
        f"файлов, уже представленных в БД: {db_skipped_existing_total:,}; "
        f"файлов с вероятными дубликатами CROP: {db_skipped_duplicate_total:,}; файлов без обнаруженных лиц: {db_no_faces_total:,}; "
        f"не классифицировано результатов по файлам: {db_unclassified_total:,}; удалено устаревших записей CROP: {db_removed_records_total:,}; "
        f"восстановлено CROP: {db_restored_crops_total:,}; сохранено без импорта: {downloaded_without_import_total:,}; "
        f"ошибок VK API: {api_errors_total:,}; ошибок альбомов/фотографий: {album_errors_total:,}; "
        f"ошибок загрузки профилей/аватаров: {download_errors_total:,}; "
        f"ошибок импорта: {import_errors_total:,}; ошибок очистки: {cleanup_errors_total:,}."
    )
    if specific_mode:
        print(
            f"🏁 Обработка списка конкретных ID {'остановлена' if STOP_REQUESTED.is_set() else 'завершена'}. "
            f"Обработано/проверено профилей: {checked_total:,}; всего ошибок: {total_errors:,}."
        )
    else:
        print(
            f"🏁 Сканирование остановлено. Следующий стартовый ID: {current_id}. "
            f"Всего ошибок: {total_errors:,}."
        )
    if api_errors_total:
        if specific_mode:
            print("[ПОДСКАЗКА] Возникла ошибка VK API. Повторите обработку конкретного списка ID.")
        else:
            print("[ПОДСКАЗКА] Последний пакет не обработан из-за ошибки VK API; повторите запуск с указанного стартового ID.")
    print("=" * 68)
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
    root.title("VK — загрузчик изображений")

    # Две темы: светлая по умолчанию и графитовая ночная.
    theme_palettes = {
        "light": {
            "app": "#F2F5F9", "panel": "#F8FAFD", "white": "#FFFFFF",
            "text": "#202B3C", "muted": "#627187", "border": "#D7E0EB",
            "accent": "#0077FF", "accent_active": "#0066DE", "accent_disabled": "#9ABFF0", "accent_text": "#FFFFFF",
            "danger_bg": "#FDECEC", "danger_text": "#B42318", "danger_border": "#F5C5C5",
            "selection": "#C9E1FF", "log_green": "#16803C", "log_gray": "#687386",
            "log_amber": "#9A6700", "log_blue": "#175CD3", "button": "#E5ECF5",
            "button_active": "#DCE8F6", "button_pressed": "#D5E1EF", "button_disabled": "#E9EEF5",
            "disabled_text": "#99A4B4", "progress_track": "#DFE7F1", "scrollbar": "#D7E0EB",
            "danger_active": "#F9DEDE", "danger_pressed": "#F5D0D0", "danger_disabled": "#F7F2F2",
        },
        "dark": {
            "app": "#1C2026", "panel": "#252A31", "white": "#30363F",
            "text": "#E7EAF0", "muted": "#A8B0BC", "border": "#424A55",
            "accent": "#2D77D4", "accent_active": "#2465BA", "accent_disabled": "#3E5876", "accent_text": "#FFFFFF",
            "danger_bg": "#482D32", "danger_text": "#FFB4AB", "danger_border": "#704148",
            "selection": "#3C5879", "log_green": "#7EE0A3", "log_gray": "#A4ACB8",
            "log_amber": "#F2C66D", "log_blue": "#91B8FF", "button": "#363D47",
            "button_active": "#454E5B", "button_pressed": "#2E343D", "button_disabled": "#2B3037",
            "disabled_text": "#77818E", "progress_track": "#343B45", "scrollbar": "#454E5B",
            "danger_active": "#613B42", "danger_pressed": "#542F37", "danger_disabled": "#332E32",
        },
    }
    theme_state = {"value": "light"}
    palette = dict(theme_palettes[theme_state["value"]])
    style = ttk.Style(root)
    try:
        style.theme_use("clam")
    except tk.TclError:
        pass

    def configure_theme_styles():
        """Перенастраивает ttk-стили с учётом активной палитры."""
        style.configure(".", font=("Segoe UI", 10), foreground=palette["text"], background=palette["panel"])
        style.configure("TFrame", background=palette["panel"])
        style.configure("App.TFrame", background=palette["app"])
        style.configure("TLabelframe", background=palette["panel"], bordercolor=palette["border"],
                        lightcolor=palette["border"], darkcolor=palette["border"], relief="solid", borderwidth=1)
        style.configure("TLabelframe.Label", background=palette["panel"], foreground=palette["accent"],
                        font=("Segoe UI", 10, "bold"), padding=(4, 0))
        style.configure("TLabel", background=palette["panel"], foreground=palette["text"])
        style.configure("Header.TLabel", background=palette["app"], foreground=palette["text"],
                        font=("Segoe UI", 19, "bold"))
        style.configure("Muted.TLabel", background=palette["app"], foreground=palette["muted"],
                        font=("Segoe UI", 9))
        style.configure("TCheckbutton", background=palette["panel"], foreground=palette["text"], padding=(3, 3))
        style.map("TCheckbutton", background=[("active", palette["panel"])],
                  foreground=[("disabled", palette["disabled_text"])])
        style.configure("TRadiobutton", background=palette["panel"], foreground=palette["text"], padding=(3, 3))
        style.map("TRadiobutton", background=[("active", palette["panel"])])
        style.configure("TEntry", fieldbackground=palette["white"], foreground=palette["text"],
                        insertcolor=palette["text"], bordercolor=palette["border"],
                        lightcolor=palette["border"], darkcolor=palette["border"], padding=5)
        style.configure("TSpinbox", fieldbackground=palette["white"], foreground=palette["text"],
                        bordercolor=palette["border"], padding=4, arrowsize=12)
        style.configure("TCombobox", fieldbackground=palette["white"], foreground=palette["text"],
                        background=palette["button"], arrowcolor=palette["text"], bordercolor=palette["border"])
        style.map("TCombobox", fieldbackground=[("readonly", palette["white"])],
                  foreground=[("disabled", palette["disabled_text"])])
        style.configure("TButton", background=palette["button"], foreground=palette["text"],
                        bordercolor=palette["border"], padding=(11, 7), relief="flat")
        style.map("TButton", background=[("disabled", palette["button_disabled"]),
                      ("pressed", palette["button_pressed"]), ("active", palette["button_active"])],
                  foreground=[("disabled", palette["disabled_text"])])
        style.configure("Accent.TButton", background=palette["accent"], foreground=palette["accent_text"],
                        bordercolor=palette["accent"], padding=(14, 8), font=("Segoe UI", 10, "bold"), relief="flat")
        style.map("Accent.TButton", background=[("disabled", palette["accent_disabled"]),
                      ("pressed", palette["accent_active"]), ("active", palette["accent_active"])],
                  foreground=[("disabled", palette["accent_text"]), ("!disabled", palette["accent_text"])])
        style.configure("Danger.TButton", background=palette["danger_bg"], foreground=palette["danger_text"],
                        bordercolor=palette["danger_border"], padding=(14, 8), relief="flat")
        style.map("Danger.TButton", background=[("disabled", palette["danger_disabled"]),
                      ("pressed", palette["danger_pressed"]), ("active", palette["danger_active"])])
        style.configure("TProgressbar", troughcolor=palette["progress_track"], background=palette["accent"],
                        bordercolor=palette["progress_track"])
        style.configure("TScrollbar", background=palette["scrollbar"], troughcolor=palette["app"],
                        bordercolor=palette["app"], arrowsize=12, arrowcolor=palette["text"])

    configure_theme_styles()
    root.configure(bg=palette["app"])

    def apply_theme(theme_name, persist=False):
        """Применяет тему ко всему окну и при необходимости сохраняет выбор."""
        selected = "dark" if str(theme_name).strip().lower() in ("dark", "night", "тёмная", "темная") else "light"
        theme_state["value"] = selected
        palette.clear()
        palette.update(theme_palettes[selected])
        root.configure(bg=palette["app"])
        configure_theme_styles()

        def recolor_widget(widget):
            try:
                if isinstance(widget, tk.Toplevel):
                    widget.configure(bg=palette["app"])
                elif isinstance(widget, tk.Canvas):
                    widget.configure(bg=palette["app"], highlightbackground=palette["app"])
                elif isinstance(widget, tk.Text):
                    widget.configure(
                        bg=palette["white"], fg=palette["text"],
                        insertbackground=palette["text"], selectbackground=palette["selection"],
                        highlightbackground=palette["border"], highlightcolor=palette["accent"],
                    )
                elif isinstance(widget, (tk.Frame, tk.LabelFrame)):
                    widget.configure(bg=palette["panel"])
                elif isinstance(widget, tk.Entry):
                    widget.configure(
                        bg=palette["white"], fg=palette["text"], insertbackground=palette["text"],
                        selectbackground=palette["selection"], disabledbackground=palette["panel"],
                        disabledforeground=palette["disabled_text"], highlightbackground=palette["border"],
                    )
                elif isinstance(widget, tk.Listbox):
                    widget.configure(
                        bg=palette["white"], fg=palette["text"], selectbackground=palette["selection"],
                        selectforeground=palette["text"], highlightbackground=palette["border"],
                    )
                elif isinstance(widget, (tk.Label, tk.Button, tk.Checkbutton, tk.Radiobutton, tk.Spinbox, tk.Menubutton)):
                    widget.configure(
                        bg=palette["panel"], fg=palette["text"],
                        activebackground=palette["button_active"], activeforeground=palette["text"],
                        disabledforeground=palette["disabled_text"], highlightbackground=palette["border"],
                    )
                for child in widget.winfo_children():
                    recolor_widget(child)
            except tk.TclError:
                pass

        recolor_widget(root)
        try:
            log_text.tag_configure("log_added", foreground=palette["log_green"])
            log_text.tag_configure("log_skip", foreground=palette["log_gray"])
            log_text.tag_configure("log_missing", foreground=palette["log_amber"])
            log_text.tag_configure("log_error", foreground=palette["danger_text"])
            log_text.tag_configure("log_info", foreground=palette["log_blue"])
        except (NameError, tk.TclError):
            pass
        try:
            theme_button.configure(text="☀ Светлая тема" if selected == "dark" else "☾ Тёмная тема")
        except (NameError, tk.TclError):
            pass
        if persist:
            try:
                save_gui_settings()
            except NameError:
                pass

    def toggle_theme():
        apply_theme("dark" if theme_state["value"] == "light" else "light", persist=True)

    # На Windows открываем сразу в развёрнутом состоянии; оставляем рамку окна и панель задач.
    root.geometry("1440x900")
    root.minsize(960, 720)
    maximized = False
    try:
        root.state("zoomed")
        maximized = True
    except tk.TclError:
        pass
    if not maximized:
        try:
            root.attributes("-zoomed", True)
            maximized = True
        except tk.TclError:
            pass
    if not maximized:
        try:
            screen_width = root.winfo_screenwidth()
            screen_height = root.winfo_screenheight()
            root.geometry(f"{screen_width}x{screen_height}+0+0")
        except tk.TclError:
            pass

    original_stdout, original_stderr = sys.stdout, sys.stderr
    sys.stdout = QueueStream(events, "log")
    sys.stderr = QueueStream(events, "log")

    # Верхняя часть формы прокручивается; выполнение, журнал и кнопки закреплены снизу.
    main_frame = ttk.Frame(root, padding=12, style="App.TFrame")
    main_frame.pack(fill="both", expand=True)
    main_frame.columnconfigure(0, weight=1)
    main_frame.rowconfigure(0, weight=3)
    main_frame.rowconfigure(1, weight=2)

    form_host = ttk.Frame(main_frame, style="App.TFrame")
    form_host.grid(row=0, column=0, sticky="nsew", pady=(0, 6))
    form_host.columnconfigure(0, weight=1)
    form_host.rowconfigure(0, weight=1)

    form_canvas = tk.Canvas(form_host, highlightthickness=0, borderwidth=0, bg=palette["app"])
    form_scrollbar = ttk.Scrollbar(form_host, orient="vertical", command=form_canvas.yview)
    form_canvas.configure(yscrollcommand=form_scrollbar.set)
    form_canvas.grid(row=0, column=0, sticky="nsew")
    form_scrollbar.grid(row=0, column=1, sticky="ns")

    outer = ttk.Frame(form_canvas, padding=8, style="App.TFrame")
    outer.columnconfigure(0, weight=1, uniform="form_col")
    outer.columnconfigure(1, weight=1, uniform="form_col")
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

    header_frame = ttk.Frame(outer, style="App.TFrame")
    header_frame.grid(row=0, column=0, columnspan=2, sticky="ew", pady=(0, 12))
    header_frame.columnconfigure(0, weight=1)
    ttk.Label(header_frame, text="Загрузчик фотографий VK", style="Header.TLabel").grid(
        row=0, column=0, sticky="w", pady=(0, 3)
    )
    theme_button = ttk.Button(
        header_frame, text="☾ Тёмная тема", command=toggle_theme, style="TButton"
    )
    theme_button.grid(row=0, column=1, rowspan=2, sticky="e", padx=(14, 0), pady=2)
    ttk.Label(
        header_frame,
        text="Изображения профилей и фотографии из альбомов · последовательный счётчик или список профилей · импорт в FaceSearch",
        style="Muted.TLabel", wraplength=1500,
    ).grid(row=1, column=0, sticky="w")

    source_frame = ttk.LabelFrame(outer, text="Источник профилей", padding=8)
    source_frame.grid(row=1, column=0, sticky="nsew", padx=(0, 6), pady=(0, 10))
    source_frame.columnconfigure(1, weight=1)

    acquisition_mode_var = tk.StringVar(value="counter")
    ttk.Radiobutton(
        source_frame, text="Последовательный счётчик ID", value="counter",
        variable=acquisition_mode_var,
        command=lambda: (update_id_source_state(), save_gui_settings()),
    ).grid(row=0, column=0, sticky="w", padx=(0, 16), pady=2)
    ttk.Radiobutton(
        source_frame, text="Конкретные ID / ссылки VK", value="specific",
        variable=acquisition_mode_var,
        command=lambda: (update_id_source_state(), save_gui_settings()),
    ).grid(row=0, column=1, sticky="w", pady=2)

    ttk.Label(source_frame, text="Стартовый ID пользователя:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
    start_id_var = tk.StringVar(value="")
    start_id_entry = ttk.Entry(source_frame, textvariable=start_id_var, width=20)
    start_id_entry.grid(row=1, column=1, sticky="w", pady=3)

    ttk.Label(
        source_frame, text="ID / ссылки / короткие имена пользователей (через запятую, пробел или новую строку):"
    ).grid(row=2, column=0, columnspan=2, sticky="w", pady=(5, 2))
    specific_ids_text = tk.Text(
        source_frame, height=3, wrap="word", undo=True,
        bg=palette["white"], fg=palette["text"], insertbackground=palette["text"],
        selectbackground=palette["selection"], relief="flat", borderwidth=1,
        highlightthickness=1, highlightbackground=palette["border"], highlightcolor=palette["accent"],
        padx=7, pady=6, font=("Segoe UI", 10),
    )
    specific_ids_text.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 4))

    ttk.Label(source_frame, text="Глубина обхода друзей:").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
    friend_depth_var = tk.StringVar(value="0")
    friend_depth_spin = ttk.Spinbox(source_frame, from_=0, to=10, increment=1, textvariable=friend_depth_var, width=8)
    friend_depth_spin.grid(row=4, column=1, sticky="w", pady=3)
    ttk.Label(
        source_frame,
        text="Для конкретных ID: 0 — только введённые профили; 1 — их друзья; 2 — друзья друзей и т. д. До 10 уровней. В режиме счётчика используется стартовый ID и обработка продолжается до остановки.",
        wraplength=620,
    ).grid(row=5, column=0, columnspan=2, sticky="w", pady=(4, 0))

    settings_frame = ttk.LabelFrame(outer, text="Параметры загрузки", padding=8)
    settings_frame.grid(row=1, column=1, sticky="nsew", padx=(6, 0), pady=(0, 10))
    settings_frame.columnconfigure(2, weight=1)
    settings_frame.columnconfigure(3, weight=1)
    ttk.Label(settings_frame, text="Потоков загрузки:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
    workers_var = tk.StringVar(value="8")
    workers_spinbox = ttk.Spinbox(
        settings_frame, from_=1, to=64, textvariable=workers_var, width=8,
    )
    workers_spinbox.grid(row=0, column=1, sticky="w", pady=3)
    auto_workers_var = tk.BooleanVar(value=AUTO_WORKERS_DEFAULT)
    auto_workers_check = ttk.Checkbutton(
        settings_frame,
        text="Автоподстройка",
        variable=auto_workers_var,
        command=lambda: (update_worker_control_state(), save_gui_settings()),
    )
    auto_workers_check.grid(row=0, column=2, sticky="w", padx=(12, 0), pady=3)
    workers_status_var = tk.StringVar(value="Автоподстройка числа потоков включена.")
    workers_status_label = ttk.Label(
        settings_frame,
        textvariable=workers_status_var,
        wraplength=560,
        anchor="w",
        justify="left",
    )
    # Информационная строка занимает всю ширину группы; перенос рассчитывается
    # по доступной ширине, а не по фиксированным 560 пикселям.
    workers_status_label.grid(
        row=6, column=0, columnspan=4, sticky="ew", pady=(8, 1)
    )

    def resize_workers_status_label(event=None):
        width = getattr(event, "width", 0) or settings_frame.winfo_width()
        workers_status_label.configure(wraplength=max(200, int(width) - 24))

    settings_frame.bind("<Configure>", resize_workers_status_label, add="+")

    download_albums_var = tk.BooleanVar(value=False)
    all_albums_var = tk.BooleanVar(value=False)
    download_tagged_photos_var = tk.BooleanVar(value=False)
    album_photo_limit_var = tk.StringVar(value="0")
    system_album_vars = {
        album_id: tk.BooleanVar(value=True)
        for album_id, _label in SYSTEM_ALBUM_OPTIONS
    }
    download_albums_check = ttk.Checkbutton(
        settings_frame,
        text="Дополнительно скачивать фотографии из альбомов пользователя",
        variable=download_albums_var,
        command=lambda: (update_album_settings_state(), save_gui_settings()),
    )
    download_albums_check.grid(row=1, column=0, columnspan=3, sticky="w", pady=(5, 1))
    all_albums_check = ttk.Checkbutton(
        settings_frame,
        text="Все альбомы, включая пользовательские (также фото с отметками)",
        variable=all_albums_var,
        command=lambda: (update_album_settings_state(), save_gui_settings()),
    )
    all_albums_check.grid(row=2, column=0, columnspan=3, sticky="w", padx=(18, 8), pady=1)

    ttk.Label(settings_frame, text="Системные альбомы:").grid(row=3, column=0, sticky="nw", padx=(18, 8), pady=(4, 1))
    system_album_frame = ttk.Frame(settings_frame)
    system_album_frame.grid(row=3, column=1, columnspan=2, sticky="ew", pady=(4, 1))
    system_album_frame.columnconfigure(0, weight=1)
    system_album_frame.columnconfigure(1, weight=1)
    system_album_checks = {}
    for index, (album_id, album_label) in enumerate(SYSTEM_ALBUM_OPTIONS):
        check = ttk.Checkbutton(
            system_album_frame,
            text=album_label,
            variable=system_album_vars[album_id],
            command=lambda: save_gui_settings(),
        )
        check.grid(row=index // 2, column=index % 2, sticky="w", padx=(0, 10), pady=1)
        system_album_checks[album_id] = check

    download_tagged_check = ttk.Checkbutton(
        settings_frame,
        text="Дополнительно загружать «Фото с отметками»",
        variable=download_tagged_photos_var,
        command=lambda: save_gui_settings(),
    )
    download_tagged_check.grid(row=4, column=0, columnspan=3, sticky="w", padx=(18, 8), pady=1)
    ttk.Label(settings_frame, text="Фото на альбом:").grid(row=5, column=0, sticky="w", padx=(18, 8), pady=1)
    album_photo_limit_spin = ttk.Spinbox(
        settings_frame, from_=0, to=100000, increment=1,
        textvariable=album_photo_limit_var, width=9,
    )
    album_photo_limit_spin.grid(row=5, column=1, sticky="w", pady=1)
    ttk.Label(
        settings_frame,
        text="0 — все фотографии; положительное число — максимум фотографий из каждого выбранного альбома.",
        wraplength=540,
    ).grid(row=5, column=2, sticky="w", padx=(12, 0), pady=1)

    output_frame = ttk.LabelFrame(outer, text="Каталог сохранения", padding=8)
    output_frame.grid(row=2, column=0, sticky="nsew", padx=(0, 6), pady=(0, 10))
    output_frame.columnconfigure(0, weight=1)
    output_var = tk.StringVar(value=str(DEFAULT_OUTPUT_DIR))
    ttk.Entry(output_frame, textvariable=output_var).grid(row=0, column=0, sticky="ew", padx=(0, 8))

    def browse_output():
        current = output_var.get().strip()
        selected = filedialog.askdirectory(
            parent=root,
            title="Выберите каталог для скачанных изображений",
            initialdir=current if current and Path(current).expanduser().is_dir() else str(APP_DIR),
            mustexist=False,
        )
        if selected:
            output_var.set(selected)

    ttk.Button(output_frame, text="Обзор…", command=browse_output).grid(row=0, column=1)

    face_frame = ttk.LabelFrame(outer, text="Интеграция с FaceSearch", padding=8)
    face_frame.grid(row=2, column=1, sticky="nsew", padx=(6, 0), pady=(0, 10))
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
        text="Автоматически импортировать загруженные изображения в активный профиль FaceSearch",
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

    auth_frame = ttk.LabelFrame(outer, text="Токены VK API v1", padding=8)
    auth_frame.grid(row=3, column=0, columnspan=2, sticky="ew", pady=(0, 4))
    auth_frame.columnconfigure(0, weight=1)
    token_count_var = tk.StringVar(value=f"Рабочих токенов v1 в {VK_TOKEN_FILE.name}: {len(_read_token_file())}")
    auth_status_var = tk.StringVar(value="Добавляйте только пользовательские токены с префиксом vk1.a… .")
    ttk.Label(auth_frame, textvariable=token_count_var).grid(row=0, column=0, sticky="w", pady=(0, 3))
    ttk.Label(
        auth_frame,
        text="Нажмите «Получить токен через VK ID», подтвердите доступ в обычном браузере и вставьте полный URL страницы blank.html. Загрузчик извлечёт токен v1, проверит его и сохранит; также доступен ручной ввод.",
        wraplength=980,
    ).grid(row=1, column=0, sticky="w", pady=(0, 6))
    auth_buttons = ttk.Frame(auth_frame)
    auth_buttons.grid(row=2, column=0, sticky="ew", pady=(0, 3))
    ttk.Label(auth_frame, textvariable=auth_status_var, wraplength=980).grid(row=3, column=0, sticky="ew", pady=(3, 0))

    def load_gui_settings():
        defaults = {
            "output_dir": str(DEFAULT_OUTPUT_DIR),
            "facesearch_dir": str(DEFAULT_FACESEARCH_DIR),
            "auto_import": True,
            "create_crop_files": True,
            "build_index_after": False,
            "delete_processed_files": False,
            "start_id": "",
            "source_mode": "counter",
            "specific_ids": "",
            "friend_depth": 0,
            "workers": 8,
            "auto_workers": AUTO_WORKERS_DEFAULT,
            "download_albums": False,
            "all_albums": False,
            "system_albums": {str(album_id): True for album_id, _label in SYSTEM_ALBUM_OPTIONS},
            "download_tagged_photos": False,
            "album_photo_limit": 0,
            "theme": "light",
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
        acquisition_mode_var.set(str(defaults.get("source_mode", "counter")))
        specific_ids_text.delete("1.0", "end")
        specific_ids_text.insert("1.0", str(defaults.get("specific_ids", "")))
        try:
            friend_depth_var.set(str(max(0, min(10, int(defaults.get("friend_depth", 0) or 0)))))
        except (TypeError, ValueError):
            friend_depth_var.set("0")
        try:
            configured_workers = max(1, min(64, int(defaults.get("workers", 8) or 8)))
        except (TypeError, ValueError):
            configured_workers = 8
        workers_var.set(str(configured_workers))
        auto_workers_var.set(bool(defaults.get("auto_workers", AUTO_WORKERS_DEFAULT)))
        download_albums_var.set(bool(defaults["download_albums"]))
        all_albums_var.set(bool(defaults["all_albums"]))
        download_tagged_photos_var.set(bool(defaults.get("download_tagged_photos", False)))
        saved_system_albums = defaults.get("system_albums", {})
        if not isinstance(saved_system_albums, dict):
            saved_system_albums = {}
        for album_id, variable in system_album_vars.items():
            variable.set(bool(saved_system_albums.get(str(album_id), True)))
        try:
            saved_limit = max(0, int(defaults["album_photo_limit"] or 0))
        except (TypeError, ValueError):
            saved_limit = 0
        album_photo_limit_var.set(str(saved_limit))
        # Применяем сохранённую тему только после создания всех виджетов.
        apply_theme(defaults.get("theme", "light"), persist=False)

    def save_gui_settings():
        payload = {
            "output_dir": output_var.get().strip(),
            "facesearch_dir": facesearch_dir_var.get().strip(),
            "auto_import": bool(auto_import_var.get()),
            "create_crop_files": bool(create_crop_var.get()),
            "build_index_after": bool(build_index_var.get()),
            "delete_processed_files": bool(delete_processed_var.get()),
            "start_id": start_id_var.get().strip(),
            "source_mode": acquisition_mode_var.get(),
            "specific_ids": specific_ids_text.get("1.0", "end-1c").strip(),
            "friend_depth": friend_depth_var.get().strip() or "0",
            "workers": workers_var.get().strip(),
            "auto_workers": bool(auto_workers_var.get()),
            "download_albums": bool(download_albums_var.get()),
            "all_albums": bool(all_albums_var.get()),
            "system_albums": {str(album_id): bool(variable.get()) for album_id, variable in system_album_vars.items()},
            "download_tagged_photos": bool(download_tagged_photos_var.get()),
            "album_photo_limit": album_photo_limit_var.get().strip() or "0",
            "theme": theme_state["value"],
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

    def update_album_settings_state():
        enabled = bool(download_albums_var.get())
        all_albums_check.configure(state="normal" if enabled else "disabled")
        album_photo_limit_spin.configure(state="normal" if enabled else "disabled")
        # При выборе «все альбомы» точечные чекбоксы не ограничивают выборку.
        system_state = "normal" if enabled and not all_albums_var.get() else "disabled"
        for check in system_album_checks.values():
            check.configure(state=system_state)
        download_tagged_check.configure(state="normal" if enabled and not all_albums_var.get() else "disabled")

    def update_id_source_state():
        counter_mode = acquisition_mode_var.get() == "counter"
        start_id_entry.configure(state="normal" if counter_mode else "disabled")
        specific_ids_text.configure(state="disabled" if counter_mode else "normal")
        friend_depth_spin.configure(state="disabled" if counter_mode else "normal")

    def update_worker_control_state():
        """Как в основной форме: ручное поле недоступно при включённой автоподстройке."""
        auto = bool(auto_workers_var.get())
        workers_spinbox.configure(state="disabled" if auto else "normal")
        if auto:
            workers_status_var.set(
                "Автоподстройка будет менять число потоков по системной нагрузке."
            )
        else:
            workers_status_var.set(
                f"Ручной режим: фиксировано {workers_var.get().strip() or '8'} поток(ов)"
            )

    db_test_running = {"value": False}

    def test_database_connection():
        """Проверяет настройки активной БД в отдельном потоке без импорта и миграций."""
        if db_test_running["value"]:
            return
        facesearch_dir = facesearch_dir_var.get().strip()
        if not facesearch_dir:
            messagebox.showwarning("Проверка БД", "Укажите каталог FaceSearch.", parent=root)
            return

        save_gui_settings()
        db_test_running["value"] = True
        db_test_button.configure(state="disabled", text="Проверка подключения…")
        status_var.set("Проверка подключения к активной базе FaceSearch…")
        print("\n" + "=" * 68)
        print(f"[DB-CONNECTION-TEST] Каталог FaceSearch: {facesearch_dir}")
        print("[DB-CONNECTION-TEST] Проверка выполняется без импорта изображений и изменения схемы БД.")

        def worker():
            importer = None
            try:
                importer = FaceSearchImporter(
                    facesearch_dir,
                    status_callback=lambda message: print(f"[DB-CONNECTION-TEST] {message}"),
                    create_crop_files=False,
                    check_only=True,
                )
                row = importer.conn.execute("SELECT 1").fetchone()
                if not row or row[0] != 1:
                    raise RuntimeError("Контрольный запрос SELECT 1 вернул неожиданный результат.")

                profile = importer.config.get("profile", {})
                profile_type = str(profile.get("type") or "sqlite").strip().lower()
                target = str(profile.get("database") or profile.get("path") or "не указан")
                message = (
                    f"Подключение успешно.\n\nПрофиль: {importer.config['active_name']}\n"
                    f"Тип БД: {profile_type}\nАдрес/путь: {target}\n"
                    "Проверка чтения таблиц и контрольного запроса завершилась успешно."
                )
                print(
                    f"[DB-CONNECTION-TEST-OK] Профиль «{importer.config['active_name']}», "
                    f"тип {profile_type}, адрес/путь: {target}; SELECT 1 OK."
                )
                events.put(("db_test_finished", True, message))
            except Exception as exc:
                message = f"{type(exc).__name__}: {exc}"
                print(f"[DB-CONNECTION-TEST-ERROR] {message}")
                import traceback
                traceback.print_exc()
                events.put(("db_test_finished", False, message))
            finally:
                if importer is not None:
                    try:
                        importer.close()
                    except Exception as exc:
                        print(f"[DB-CONNECTION-TEST-WARN] Ошибка закрытия тестового соединения: {exc}")

        threading.Thread(target=worker, daemon=True, name="FaceSearchDatabaseTest").start()

    db_test_button = ttk.Button(
        face_frame,
        text="Проверить подключение к БД",
        command=test_database_connection,
    )
    db_test_button.grid(row=5, column=0, columnspan=2, sticky="w", pady=(6, 1))
    ttk.Label(
        face_frame,
        text="Проверяет активный профиль без импорта и изменений схемы.",
        style="Muted.TLabel",
        wraplength=460,
    ).grid(row=5, column=2, sticky="w", padx=(8, 0), pady=(6, 1))

    def on_specific_ids_change(_event=None):
        """Сохраняет список конкретных ID при редактировании и уходе из поля."""
        save_gui_settings()

    specific_ids_text.bind("<KeyRelease>", on_specific_ids_change, add="+")
    specific_ids_text.bind("<FocusOut>", on_specific_ids_change, add="+")

    load_gui_settings()
    update_import_state()
    update_album_settings_state()
    update_id_source_state()
    update_worker_control_state()
    for variable in (output_var, facesearch_dir_var, start_id_var, workers_var, album_photo_limit_var, friend_depth_var):
        variable.trace_add("write", lambda *_args: save_gui_settings())

    def refresh_token_count():
        count = reload_token_cache()
        token_count_var.set(f"Рабочих токенов v1 в {VK_TOKEN_FILE.name}: {count}")

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

    token_check_running = {"value": False}
    token_check_button = None

    def start_token_check():
        """Проверяет сохранённые токены по одному в фоновом потоке и пишет результат в журнал."""
        if token_check_running["value"]:
            return

        try:
            saved_tokens = _read_all_token_file()
            valid_format_count = reload_token_cache()
        except Exception as exc:
            auth_status_var.set(f"Не удалось прочитать файл токенов: {exc}")
            print(
                f"[TOKEN-CHECK-ERROR] Не удалось прочитать {VK_TOKEN_FILE.name}: "
                f"{type(exc).__name__}: {exc}"
            )
            return

        token_count_var.set(f"Рабочих токенов v1 в {VK_TOKEN_FILE.name}: {valid_format_count}")
        print("\n" + "=" * 68)
        print(f"[TOKEN-CHECK] Проверка авторизации токенов из {VK_TOKEN_FILE.name}.")
        if not saved_tokens:
            print("[TOKEN-CHECK-SUMMARY] Файл не содержит сохранённых токенов.")
            auth_status_var.set(f"В {VK_TOKEN_FILE.name} нет сохранённых токенов для проверки.")
            print("=" * 68)
            return

        token_check_running["value"] = True
        token_check_button.configure(state="disabled", text="Проверка…")
        auth_status_var.set(f"Начата проверка токенов: {len(saved_tokens)} шт.")

        def worker():
            success_count = 0
            failed_count = 0
            skipped_count = 0
            total = len(saved_tokens)
            try:
                print(
                    f"[TOKEN-CHECK] Всего уникальных записей: {total}; "
                    "формат vk1.a… поддерживается загрузчиком."
                )
                for index, token in enumerate(saved_tokens, start=1):
                    events.put((
                        "token_check_progress", index, total,
                        f"Проверка токена {index}/{total}…",
                    ))
                    if not _is_v1_token(token):
                        skipped_count += 1
                        print(
                            f"[TOKEN-CHECK-SKIP] Токен {index}/{total}: неподдерживаемый формат; "
                            "авторизация не проверялась, загрузчик использует только токены vk1.a… ."
                        )
                        continue

                    try:
                        user_name = validate_access_token(token)
                    except Exception as exc:
                        failed_count += 1
                        error_text = str(exc).replace(token, "[токен скрыт]")
                        print(
                            f"[TOKEN-CHECK-ERROR] Токен {index}/{total}: авторизация не прошла — "
                            f"{type(exc).__name__}: {error_text}"
                        )
                    else:
                        success_count += 1
                        print(
                            f"[TOKEN-CHECK-OK] Токен {index}/{total}: авторизация через VK API успешна; "
                            f"пользователь: {user_name}."
                        )
            except Exception as exc:
                failed_count += 1
                error_text = str(exc)
                for token in saved_tokens:
                    error_text = error_text.replace(token, "[токен скрыт]")
                print(f"[TOKEN-CHECK-ERROR] Проверка прервана: {type(exc).__name__}: {error_text}")
            finally:
                print(
                    f"[TOKEN-CHECK-SUMMARY] Записей: {total}; проверок авторизации завершено: "
                    f"{success_count + failed_count}; успешно: {success_count}; ошибок: {failed_count}; "
                    f"пропущено (неподдерживаемый формат): {skipped_count}."
                )
                print("=" * 68)
                events.put((
                    "token_check_finished", success_count, failed_count,
                    skipped_count, total,
                ))

        try:
            threading.Thread(target=worker, daemon=True, name="VKTokenCheckWorker").start()
        except Exception as exc:
            token_check_running["value"] = False
            token_check_button.configure(state="normal", text="Проверить токены")
            auth_status_var.set(f"Не удалось запустить проверку токенов: {exc}")
            print(f"[TOKEN-CHECK-ERROR] Не удалось запустить проверку: {type(exc).__name__}: {exc}")

    ttk.Button(auth_buttons, text="Получить токен через VK ID…", command=lambda: start_v1_authorization()).pack(side="left", padx=(0, 6))
    ttk.Button(auth_buttons, text="Добавить токен v1 вручную…", command=lambda: open_auth_dialog()).pack(side="left", padx=(0, 6))
    ttk.Button(auth_buttons, text="Открыть vk_token.txt", command=open_token_file).pack(side="left", padx=(0, 6))
    ttk.Button(auth_buttons, text="Обновить список", command=refresh_token_count).pack(side="left", padx=(0, 6))
    token_check_button = ttk.Button(auth_buttons, text="Проверить токены", command=start_token_check)
    token_check_button.pack(side="left")

    def open_auth_dialog():
        dialog = tk.Toplevel(root)
        dialog.title("Добавить токен VK API v1")
        dialog.transient(root)
        dialog.geometry("680x280")
        dialog.minsize(600, 230)
        dialog.columnconfigure(0, weight=1)
        dialog.rowconfigure(1, weight=1)
        ttk.Label(
            dialog,
            text="Вставьте токен vk1.a… или полный адрес страницы blank.html после подтверждения VK ID (URL содержит #access_token=…). Не публикуйте этот адрес.",
            wraplength=640,
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        dialog.configure(bg=palette["app"])
        token_input = tk.Text(
            dialog, height=4, wrap="word", undo=True,
            bg=palette["white"], fg=palette["text"], insertbackground=palette["text"],
            selectbackground=palette["selection"], relief="flat", borderwidth=1,
            highlightthickness=1, highlightbackground=palette["border"], highlightcolor=palette["accent"],
            padx=7, pady=6, font=("Segoe UI", 10),
        )
        token_input.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, sticky="e", padx=10, pady=(0, 10))
        save_button = ttk.Button(buttons, text="Проверить и сохранить")
        save_button.pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right")

        def submit_token():
            pasted_value = token_input.get("1.0", "end").strip()
            token = extract_v1_token_input(pasted_value)
            if not token:
                messagebox.showerror(
                    "Токен VK API v1",
                    "Не найден токен vk1.a… . Вставьте сам токен или полный URL blank.html, содержащий access_token=.",
                    parent=dialog,
                )
                return
            if any(character.isspace() for character in token):
                messagebox.showerror("Токен VK API v1", "В извлечённом токене обнаружены пробелы или переносы строк.", parent=dialog)
                return
            save_button.configure(state="disabled", text="Проверка…")
            auth_status_var.set("Проверяю токен v1 через VK API…")

            def validate_worker():
                try:
                    user_name = validate_access_token(token)
                    added, count = save_access_token(token)
                    events.put(("token_saved", True, user_name, added, count, ""))
                except Exception as exc:
                    events.put(("token_saved", False, "", False, len(_read_token_file()), str(exc)))

            threading.Thread(target=validate_worker, daemon=True, name="VKTokenValidationWorker").start()

        save_button.configure(command=submit_token)
        dialog.protocol("WM_DELETE_WINDOW", dialog.destroy)
        dialog.focus_set()
        root._vk_token_dialog = dialog
        root._vk_token_save_button = save_button

    def start_v1_authorization():
        """Открывает авторизацию VK ID в обычном браузере."""
        auth_status_var.set("Открываю авторизацию VK ID в браузере…")
        try:
            opened = webbrowser.open(VK_AUTH_URL, new=2, autoraise=True)
        except Exception as exc:
            messagebox.showerror("Авторизация VK ID", f"Не удалось открыть браузер: {exc}", parent=root)
            auth_status_var.set("Не удалось открыть браузер для авторизации.")
            return
        if not opened:
            messagebox.showwarning(
                "Авторизация VK ID",
                "Браузер не открылся автоматически. Проверьте браузер по умолчанию и повторите попытку.",
                parent=root,
            )
            auth_status_var.set("Не удалось открыть браузер для авторизации.")
            return
        open_auth_dialog()
        auth_status_var.set("VK ID открыт в браузере. После подтверждения вставьте полный URL blank.html и нажмите «Проверить и сохранить».")

    footer = ttk.Frame(main_frame, style="App.TFrame")
    footer.grid(row=1, column=0, sticky="nsew")
    footer.columnconfigure(0, weight=1)
    footer.rowconfigure(1, weight=1)

    progress_frame = ttk.LabelFrame(footer, text="Выполнение", padding=10)
    progress_frame.grid(row=0, column=0, sticky="ew", pady=(0, 6))
    progress_frame.columnconfigure(0, weight=1)
    status_var = tk.StringVar(value="Готово к запуску.")
    ttk.Label(progress_frame, textvariable=status_var, wraplength=980).grid(row=0, column=0, sticky="ew", pady=(0, 4))
    progress_text_var = tk.StringVar(value="Ожидание")
    ttk.Label(progress_frame, textvariable=progress_text_var).grid(row=1, column=0, sticky="w", pady=(0, 3))
    progress_bar = ttk.Progressbar(progress_frame, mode="determinate", maximum=100, value=0)
    progress_bar.grid(row=2, column=0, sticky="ew")

    log_frame = ttk.LabelFrame(footer, text="Журнал", padding=8)
    log_frame.grid(row=1, column=0, sticky="nsew", pady=(0, 6))
    log_frame.columnconfigure(0, weight=1)
    log_frame.rowconfigure(0, weight=1)
    log_text = tk.Text(
        log_frame, height=8, wrap="word", state="disabled", undo=True,
        bg=palette["white"], fg=palette["text"], insertbackground=palette["text"],
        selectbackground=palette["selection"], relief="flat", borderwidth=1,
        highlightthickness=1, highlightbackground=palette["border"], highlightcolor=palette["accent"],
        padx=8, pady=6, font=("Consolas", 9),
    )
    log_text.tag_configure("log_added", foreground=palette["log_green"])
    log_text.tag_configure("log_skip", foreground=palette["log_gray"])
    log_text.tag_configure("log_missing", foreground=palette["log_amber"])
    log_text.tag_configure("log_error", foreground=palette["danger_text"])
    log_text.tag_configure("log_info", foreground=palette["log_blue"])
    log_text.grid(row=0, column=0, sticky="nsew")
    scroll = ttk.Scrollbar(log_frame, orient="vertical", command=log_text.yview)
    scroll.grid(row=0, column=1, sticky="ns")
    log_text.configure(yscrollcommand=scroll.set)

    install_edit_shortcuts(root)


    action_frame = ttk.Frame(footer, style="App.TFrame")
    action_frame.grid(row=2, column=0, sticky="ew")
    action_frame.columnconfigure(0, weight=1)
    running = {"value": False}
    window_closing = threading.Event()

    def set_running(active: bool):
        running["value"] = active
        start_button.configure(state="disabled" if active else "normal")
        stop_button.configure(state="normal" if active else "disabled")
        auto_workers_check.configure(state="disabled" if active else "normal")
        workers_spinbox.configure(
            state="disabled" if active or auto_workers_var.get() else "normal"
        )

    def start_job():
        try:
            source_mode = acquisition_mode_var.get()
            raw_specific_ids = specific_ids_text.get("1.0", "end-1c").strip()
            if source_mode == "counter":
                raw_start_id = start_id_var.get().strip()
                if not raw_start_id:
                    raise ValueError("Укажите стартовый ID пользователя VK.")
                start_id = int(raw_start_id)
                if start_id < 1:
                    raise ValueError("Стартовый ID должен быть положительным числом.")
            else:
                if not parse_specific_identifiers(raw_specific_ids):
                    raise ValueError("Введите список конкретных ID/ссылок на профили VK.")
                start_id = 1  # Не используется в режиме конкретных ID.
            workers = int(workers_var.get().strip())
            album_photo_limit = int(album_photo_limit_var.get().strip() or "0")
            friend_depth = int(friend_depth_var.get().strip() or "0")
            if album_photo_limit < 0:
                raise ValueError("Лимит фотографий на альбом должен быть 0 или положительным числом.")
            if workers < 1 or workers > 64:
                raise ValueError("Количество потоков должно быть от 1 до 64.")
            if friend_depth < 0 or friend_depth > 10:
                raise ValueError("Глубина обхода друзей должна быть от 0 до 10.")
        except ValueError as exc:
            messagebox.showerror("Параметры", str(exc) or "Проверьте источник ID, глубину и число потоков.", parent=root)
            return
        token_count = len(_read_token_file())
        if token_count == 0:
            saved_rows = _read_all_token_file()
            incompatible_count = sum(1 for saved_token in saved_rows if not _is_v1_token(saved_token))
            message = (
                f"В {VK_TOKEN_FILE.name} нет рабочих токенов v1 с префиксом vk1.a… . "
                "Добавьте токен через «Добавить токен v1»."
            )
            if incompatible_count:
                message += f"\n\nСтрок с неподдерживаемыми токенами: {incompatible_count}. Они не используются."
            messagebox.showwarning("Токены VK API v1", message, parent=root)
            return

        reload_token_cache()
        auto_workers = bool(auto_workers_var.get())
        auto_import = bool(auto_import_var.get())
        create_crop_files = bool(create_crop_var.get())
        build_index_after = bool(build_index_var.get())
        delete_processed_files = bool(delete_processed_var.get())
        download_albums = bool(download_albums_var.get())
        all_albums = bool(all_albums_var.get()) and download_albums
        selected_system_albums = [album_id for album_id, variable in system_album_vars.items() if variable.get()]
        download_tagged_photos = bool(download_tagged_photos_var.get()) and download_albums
        facesearch_dir = facesearch_dir_var.get().strip()
        output_dir = output_var.get().strip() or str(DEFAULT_OUTPUT_DIR)
        STOP_REQUESTED.clear()
        set_running(True)
        progress_bar.configure(mode="indeterminate")
        progress_bar.start(10)
        progress_text_var.set("Подготовка задания…")
        workers_status_var.set(
            "Подготовка автоматической подстройки потоков…"
            if auto_workers else f"Ручной режим: фиксировано {workers} поток(ов)"
        )
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
                print(
                    "⚙️ FaceSearch: создание CROP "
                    f"{'включено' if create_crop_files else 'выключено'}; "
                    "обновление существующих CROP и метаданных "
                    f"{'включено' if importer.overwrite_existing_crops else 'выключено'} "
                    "(по настройке основного приложения)."
                )
                next_start_id = run_download_job(
                    start_id, workers,
                    output_dir,
                    importer=importer,
                    delete_processed_files=delete_processed_files and importer is not None and auto_import,
                    import_to_database=auto_import,
                    download_albums=download_albums,
                    all_albums=all_albums,
                    album_photo_limit=album_photo_limit,
                    selected_system_albums=selected_system_albums,
                    download_tagged_photos=download_tagged_photos,
                    source_mode=source_mode,
                    specific_ids=raw_specific_ids,
                    friend_depth=friend_depth,
                    auto_workers=auto_workers,
                )
                if source_mode == "counter":
                    events.put(("start_id_updated", next_start_id))
                # Stop прекращает запуск новых профилей, но не отменяет явно
                # выбранный финальный этап. run_download_job к этому моменту уже
                # дождался всех отправленных загрузок, импортов и очистки; поэтому
                # при включённом флажке строим индекс по итоговой БД и после Stop.
                if (
                    importer is not None
                    and auto_import
                    and build_index_after
                    and not window_closing.is_set()
                ):
                    events.put(("status", "Загрузки остановлены/завершены; строю FAISS-индекс по итоговой базе…"))
                    importer.build_index()
                if importer is not None and auto_import:
                    print(f"✅ FaceSearch: вызовов импортёра без исключения — {importer.imported_count}; фактические новые/обновлённые записи см. в строках [DB-NEW]/[DB-UPDATED]/[DB-CHANGED] и в [ИТОГ].")
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
            status_var.set(
                "Остановка запрошена: новые профили и альбомы не запускаются; активные сетевые "
                "операции завершатся, все уже скачанные файлы будут импортированы в БД и очищены по настройке."
            )
            stop_button.configure(state="disabled")

    start_button = ttk.Button(action_frame, text="Начать загрузку", command=start_job, style="Accent.TButton")
    start_button.grid(row=0, column=0, sticky="ew", padx=(0, 6), ipady=5)
    stop_button = ttk.Button(action_frame, text="Остановить", command=stop_job, state="disabled", style="Danger.TButton")
    stop_button.grid(row=0, column=1, sticky="ew", ipady=5)

    def drain_events():
        try:
            while True:
                event = events.get_nowait()
                kind = event[0]
                if kind == "log":
                    log_text.configure(state="normal")
                    entry_text = str(event[1])
                    entry_tag = None
                    if "[DB-NEW]" in entry_text or "[DB-UPDATED]" in entry_text or "[DB-CHANGED]" in entry_text or "[DB-IMPORTED]" in entry_text or "[AVATAR-OK]" in entry_text or "[TOKEN-CHECK-OK]" in entry_text:
                        entry_tag = "log_added"
                    elif "[DB-SKIP]" in entry_text or "[DB-SKIP-EXISTING]" in entry_text or "[DB-SKIP-DUPLICATE]" in entry_text or "[DB-SKIP-UNKNOWN]" in entry_text or "[TOKEN-CHECK-SKIP]" in entry_text:
                        entry_tag = "log_skip"
                    elif "[DB-NO-FACE]" in entry_text or "[CLEANUP-SKIP]" in entry_text or "[DB-RESULT-WARN]" in entry_text or "[VK-NOT-FOUND]" in entry_text or "[NO-AVATAR]" in entry_text or "[VK-DEACTIVATED]" in entry_text:
                        entry_tag = "log_missing"
                    elif "[ERROR]" in entry_text or "[AVATAR-ERROR]" in entry_text or "[DOWNLOAD-ERROR]" in entry_text or "[DB-ERROR]" in entry_text or "[VK-API-ERROR]" in entry_text or "[CLEANUP-ERROR]" in entry_text or "[TOKEN-CHECK-ERROR]" in entry_text:
                        entry_tag = "log_error"
                    elif "[CLEANUP]" in entry_text or "[SAVED-NO-IMPORT]" in entry_text or "[TOKEN-CHECK]" in entry_text:
                        entry_tag = "log_info"
                    if entry_tag:
                        log_text.insert("end", entry_text, entry_tag)
                    else:
                        log_text.insert("end", entry_text)
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
                elif kind == "workers_status":
                    workers_status_var.set(str(event[1] or ""))
                elif kind == "token_check_progress":
                    auth_status_var.set(event[3])
                elif kind == "db_test_finished":
                    success, details = event[1:]
                    db_test_running["value"] = False
                    try:
                        db_test_button.configure(state="normal", text="Проверить подключение к БД")
                    except tk.TclError:
                        pass
                    if success:
                        status_var.set("Проверка подключения к БД завершена успешно.")
                        messagebox.showinfo("Проверка подключения к БД", details, parent=root)
                    else:
                        status_var.set("Проверка подключения к БД завершилась ошибкой; подробности в журнале.")
                        messagebox.showerror("Ошибка подключения к БД", details, parent=root)
                elif kind == "token_check_finished":
                    success_count, failed_count, skipped_count, total = event[1:]
                    token_check_running["value"] = False
                    try:
                        token_check_button.configure(state="normal", text="Проверить токены")
                    except tk.TclError:
                        pass
                    auth_status_var.set(
                        f"Проверка завершена: успешно {success_count}, ошибок {failed_count}, "
                        f"пропущено {skipped_count} из {total}. Подробности — в журнале."
                    )
                elif kind == "token_saved":
                    success, user_name, added, count, error = event[1:]
                    token_count_var.set(f"Рабочих токенов v1 в {VK_TOKEN_FILE.name}: {count}")
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
                            auth_status_var.set(f"Токен v1 добавлен. Пользователь: {user_name}; всего токенов: {count}.")
                            messagebox.showinfo("Токен VK сохранён", f"Токен v1 проверен и добавлен в {VK_TOKEN_FILE.name}.\nПользователь: {user_name}\nВсего токенов: {count}", parent=root)
                        else:
                            auth_status_var.set(f"Этот токен v1 уже сохранён. Пользователь: {user_name}; всего токенов: {count}.")
                            messagebox.showinfo("Токен уже есть", f"Этот токен v1 уже присутствует в {VK_TOKEN_FILE.name}.\nПользователь: {user_name}\nВсего токенов: {count}", parent=root)
                    else:
                        error_parent = getattr(root, "_vk_token_dialog", None)
                        try:
                            if error_parent is None or not error_parent.winfo_exists():
                                error_parent = root
                        except tk.TclError:
                            error_parent = root
                        auth_status_var.set(f"Не удалось добавить токен v1: {error}")
                        messagebox.showerror("Не удалось добавить токен v1", error, parent=error_parent)
                elif kind == "job_finished":
                    progress_bar.stop()
                    progress_bar.configure(mode="determinate")
                    set_running(False)
                    workers_status_var.set("")
                    progress_text_var.set("Задание остановлено" if event[1] else "Задание завершено")
                    status_var.set(
                        "Остановлено. Все начатые загрузки, импорты в БД и очистка обработанных файлов завершены."
                        if event[1]
                        else "Задание завершено. Проверьте журнал для итогов."
                    )
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
