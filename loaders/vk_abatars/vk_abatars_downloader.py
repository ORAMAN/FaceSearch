# Загрузчик аватарок VK: ручное добавление пользовательских API-токенов v1.

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
from urllib.parse import urlparse
from concurrent.futures import ThreadPoolExecutor, as_completed, wait, FIRST_COMPLETED

import requests
import tkinter as tk
from tkinter import ttk, filedialog, messagebox

# === ПАРАМЕТРЫ === #
APP_DIR = Path(__file__).resolve().parent
VK_TOKEN_FILE = APP_DIR / "vk_token.txt"
VK_FAVICON_URL = "https://vk.ru/favicon.ico"
VK_FAVICON_PATH = APP_DIR / "vk_favicon.ico"
LOG_FILE = APP_DIR / "Log.txt"
DEFAULT_OUTPUT_DIR = APP_DIR / "VK_Avatars_Downloads"
APP_SETTINGS_PATH = APP_DIR / "vk_abatars_settings.json"
DEFAULT_FACESEARCH_DIR = Path(r"C:\FaceSearch")
_META_RESOLUTION = "_url_resolution"
_META_VERSION = "_resolution_version"
_META_DUPLICATE_OF = "_duplicate_of"
_META_DUPLICATE_REASON = "_duplicate_reason"
API_VERSION = "5.199"
BASE_URL = "https://api.vk.ru/method"
FRIENDS_PAGE_SIZE = 5000
USER_BATCH_SIZE = 1000
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


def apply_vk_window_icon(root) -> None:
    """Загружает и кеширует иконку окна из vk.ru/favicon.ico."""
    try:
        if not VK_FAVICON_PATH.is_file() or VK_FAVICON_PATH.stat().st_size == 0:
            response = requests.get(VK_FAVICON_URL, timeout=4)
            response.raise_for_status()
            data = response.content
            if len(data) < 8:
                raise ValueError("vk.ru вернул слишком короткий favicon.ico")
            temporary = VK_FAVICON_PATH.with_suffix(".ico.tmp")
            temporary.write_bytes(data)
            temporary.replace(VK_FAVICON_PATH)
        root.iconbitmap(str(VK_FAVICON_PATH))
    except Exception:
        # При недоступности favicon форма всё равно должна запускаться.
        pass

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

    # В параметр VK API передаётся только явный список, например "101,102,103".
    user_ids_param = ",".join(normalized_ids)
    params = {
        "user_ids": user_ids_param,
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

def process_entity(
    entity: dict, base_dir: str, is_group: bool, MAX_WORKERS: int,
    importer=None, delete_processed_files: bool = False, import_to_database: bool = True,
):
    """Скачивает только аватар; импорт в БД выполняет вызывающий поток."""
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
        print(f"[DB-SKIP] ID {abs(uid)} | {name}: профиль уже есть в активной БД FaceSearch.")
        return {"status": "already_in_db", "uid": abs(uid), "name": name}

    print(f"\n{'=' * 60}")
    print(f"👤 {name} | ID: {display_id}")
    post_ui_event("status", f"Загрузка аватарки VK ID {display_id} ({name})…")

    success, reason, result_kind = save_avatar_to_root(entity, base_dir, is_group=is_group)
    if not success:
        if result_kind == "no_avatar":
            print(f"[NO-AVATAR] ID {display_id} | {name}: {reason}.")
            post_ui_event("status", f"VK ID {display_id}: у профиля нет доступной аватарки.")
        else:
            print(f"[AVATAR-ERROR] ID {display_id} | {name}: {reason}.")
            post_ui_event("status", f"VK ID {display_id}: ошибка загрузки аватарки.")
        _remove_dir_if_empty(base_dir)
        return {
            "status": result_kind,
            "uid": abs(uid),
            "name": name,
            "reason": reason,
            "base_dir": str(base_dir),
        }

    avatar_path = Path(base_dir) / "Avatar.jpg"
    avatar_json = Path(base_dir) / "Avatar.json"
    print(f"[AVATAR-OK] ID {display_id} | {name}: аватар скачан.")
    post_ui_event("status", f"VK ID {display_id}: аватар скачан; передача в FaceSearch…")
    return {
        "status": "downloaded",
        "image_path": avatar_path,
        "json_path": avatar_json,
        "uid": abs(uid),
        "name": name,
        "base_dir": str(base_dir),
    }


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


def _format_id_list(values) -> str:
    """Показывает каждый ID отдельно; никогда не сворачивает список в диапазоны."""
    ids = sorted({int(value) for value in values})
    return ", ".join(map(str, ids)) if ids else "—"


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
    missing_profiles_total = 0
    deactivated_total = 0
    no_avatar_total = 0
    download_errors_total = 0
    import_errors_total = 0
    api_errors_total = 0
    downloaded_total = 0
    imported_total = 0
    downloaded_without_import_total = 0
    cleanup_errors_total = 0

    print(f"🖼️ VK Avatar Downloader — каталог: {output_root}")
    print(
        f"▶ Начальный ID: {start_id}; потоков загрузки: {worker_count}; "
        f"пачка users.get: до {USER_BATCH_SIZE} ID."
    )
    if delete_processed_files and import_to_database:
        print("🧹 Очистка включена: после успешного импорта будут удаляться оба файла и пустая папка пользователя.")
    print("ℹ️ Перебор непрерывный и работает до нажатия «Остановить».")

    with requests.Session() as session:
        with ThreadPoolExecutor(max_workers=worker_count, thread_name_prefix="VKAvatar") as executor:
            while not STOP_REQUESTED.is_set():
                batch_start = current_id
                batch_end = batch_start + USER_BATCH_SIZE
                batch_ids = list(range(batch_start, batch_end))

                # Уже сохранённые профили логируем по ID, но не отправляем повторно в VK API.
                with EXISTING_PROFILE_IDS_LOCK:
                    skipped_ids = [uid for uid in batch_ids if uid in EXISTING_PROFILE_IDS]
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
                    f"Проверка ID {batch_start}–{batch_end - 1}; "
                    f"уже в БД: {skipped}; ожидает проверки VK: {len(request_ids)}."
                )
                post_ui_event(
                    "progress", 0, 0,
                    f"ID {batch_start}–{batch_end - 1} | проверено: {checked_total:,} | "
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
                            f"[VK-API-ERROR] Пакет ID {batch_start}–{batch_end - 1}: "
                            f"users.get завершился ошибкой: {type(exc).__name__}: {exc}. "
                            f"Диапазон не будет пропущен; следующий стартовый ID — {batch_start}."
                        )
                        post_ui_event(
                            "status",
                            f"Ошибка VK API для ID {batch_start}–{batch_end - 1}; "
                            "сканирование остановлено, диапазон можно повторить."
                        )
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
                    f"[BATCH] ID {batch_start}–{batch_end - 1}: "
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
                        )
                        pending[future] = uid

                submit_next_tasks()
                while pending:
                    completed_futures, _ = wait(pending, return_when=FIRST_COMPLETED)
                    for future in completed_futures:
                        uid = pending.pop(future)
                        try:
                            outcome = future.result()
                            if not outcome:
                                raise RuntimeError("задача завершилась без результата")
                            outcome_status = outcome.get("status")

                            if outcome_status == "already_in_db":
                                # Защита от случая, когда профиль появился в БД после построения списка.
                                skipped_total += 1
                                print(f"[DB-SKIP] ID {uid} | {outcome.get('name', name_by_id.get(uid, ''))}: профиль уже есть в БД.")
                                continue

                            if outcome_status == "no_avatar":
                                no_avatar_total += 1
                                continue

                            if outcome_status == "download_error":
                                # Причина подробно записана process_entity; здесь обновляем только счётчики.
                                download_errors_total += 1
                                batch_errors += 1
                                continue

                            if outcome_status != "downloaded":
                                raise RuntimeError(f"неизвестный статус результата: {outcome_status}")

                            image_path = Path(outcome["image_path"])
                            json_path = Path(outcome["json_path"])
                            downloaded_uid = int(outcome["uid"])
                            downloaded_name = str(outcome.get("name") or name_by_id.get(uid) or f"user_{uid}")
                            downloaded_total += 1

                            if importer is not None and import_to_database:
                                try:
                                    import_result = importer.import_image(
                                        image_path,
                                        json_path,
                                        source_key=f"vk:{downloaded_uid}:avatar",
                                    )
                                except Exception as exc:
                                    batch_errors += 1
                                    import_errors_total += 1
                                    print(
                                        f"[DB-ERROR] ID {downloaded_uid} | {downloaded_name}: "
                                        f"аватар скачан, но импорт в FaceSearch завершился ошибкой "
                                        f"{type(exc).__name__}: {exc}. Файлы оставлены для повторного импорта: {image_path.parent}"
                                    )
                                    continue

                                with EXISTING_PROFILE_IDS_LOCK:
                                    EXISTING_PROFILE_IDS.add(abs(downloaded_uid))
                                imported_total += 1
                                print(
                                    f"[DB-IMPORTED] ID {downloaded_uid} | {downloaded_name}: "
                                    f"импорт успешно завершён; лиц: {import_result.get('faces', 0)}."
                                )

                                if delete_processed_files:
                                    try:
                                        image_path.unlink(missing_ok=True)
                                        json_path.unlink(missing_ok=True)
                                        if _remove_dir_if_empty(image_path.parent):
                                            print(
                                                f"[CLEANUP] ID {downloaded_uid}: исходные файлы и пустая папка пользователя удалены."
                                            )
                                        else:
                                            cleanup_errors_total += 1
                                            print(
                                                f"[CLEANUP-ERROR] ID {downloaded_uid}: файлы удалены, "
                                                f"но каталог остался (не пуст или занят): {image_path.parent}"
                                            )
                                    except Exception as exc:
                                        cleanup_errors_total += 1
                                        print(
                                            f"[CLEANUP-ERROR] ID {downloaded_uid}: импорт прошёл успешно, "
                                            f"но не удалось полностью удалить исходные файлы: {exc}"
                                        )

                            else:
                                downloaded_without_import_total += 1
                                print(
                                    f"[SAVED-NO-IMPORT] ID {downloaded_uid} | {downloaded_name}: "
                                    f"аватар сохранён на диск, автоматический импорт в FaceSearch выключен: {image_path}"
                                )

                            # Запись в Log.txt делается только после успешного импорта
                            # либо после успешного скачивания в режиме без импорта.
                            log_user_processed(downloaded_name, str(downloaded_uid), False)

                        except Exception as exc:
                            batch_errors += 1
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
                                    f"Пакет ID {batch_start}–{batch_end - 1} | "
                                    f"аватары: {batch_finished}/{len(tasks)} | "
                                    f"импортировано: {imported_total:,} | "
                                    f"без фото: {no_avatar_total:,} | ошибок загрузки/импорта: "
                                    f"{download_errors_total + import_errors_total:,}."
                                )
                    # При нажатии «Остановить» новые задания не добавляются в пул.
                    submit_next_tasks()

                if STOP_REQUESTED.is_set():
                    # Если часть задач пачки ещё не была отправлена, повторяем её с начала.
                    current_id = batch_start if (next_task_index < len(tasks) or batch_errors) else batch_end
                    break

                print(
                    f"[BATCH-SUMMARY] ID {batch_start}–{batch_end - 1}: "
                    f"уже в БД {skipped}; профиль не получен {missing_count}; "
                    f"задач на скачивание {len(tasks)}; ошибок загрузки/импорта в пакете {batch_errors}."
                )
                current_id = batch_end

    total_errors = api_errors_total + download_errors_total + import_errors_total + cleanup_errors_total
    print("\n" + "=" * 68)
    print(
        f"[ИТОГ] Проверено ID: {checked_total:,}; уже есть в БД: {skipped_total:,}; "
        f"профиль не получен от VK: {missing_profiles_total:,}; "
        f"деактивировано: {deactivated_total:,}; без доступной аватарки: {no_avatar_total:,}; "
        f"аватаров скачано: {downloaded_total:,}; успешно импортировано в FaceSearch: {imported_total:,}; "
        f"сохранено без импорта: {downloaded_without_import_total:,}; "
        f"ошибок VK API: {api_errors_total:,}; ошибок скачивания: {download_errors_total:,}; "
        f"ошибок импорта: {import_errors_total:,}; ошибок очистки: {cleanup_errors_total:,}."
    )
    print(
        f"🏁 Сканирование остановлено. Следующий стартовый ID: {current_id}. "
        f"Всего ошибок: {total_errors:,}."
    )
    if api_errors_total:
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
    apply_vk_window_icon(root)
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

    auth_frame = ttk.LabelFrame(outer, text="Токены VK API v1", padding=8)
    auth_frame.grid(row=5, column=0, sticky="ew", pady=(0, 8))
    auth_frame.columnconfigure(0, weight=1)
    token_count_var = tk.StringVar(value=f"Рабочих токенов v1 в {VK_TOKEN_FILE.name}: {len(_read_token_file())}")
    auth_status_var = tk.StringVar(value="Добавляйте только пользовательские токены с префиксом vk1.a… .")
    ttk.Label(auth_frame, textvariable=token_count_var).grid(row=0, column=0, sticky="w", pady=(0, 3))
    ttk.Label(
        auth_frame,
        text="Автоматический вход через VK ID и Chrome удалён. Вставьте готовый токен v1 вручную; перед сохранением он проверяется через VK API.",
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
            "workers": 8,
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

    ttk.Button(auth_buttons, text="Добавить токен v1…", command=lambda: open_auth_dialog()).pack(side="left", padx=(0, 6))
    ttk.Button(auth_buttons, text="Открыть vk_token.txt", command=open_token_file).pack(side="left", padx=(0, 6))
    ttk.Button(auth_buttons, text="Обновить список", command=refresh_token_count).pack(side="left")

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
            text="Вставьте готовый пользовательский API-токен версии v1 (начинается с vk1.a…). Вставляйте только сам токен, не URL и не код авторизации.",
            wraplength=640,
        ).grid(row=0, column=0, sticky="ew", padx=10, pady=(10, 6))
        token_input = tk.Text(dialog, height=4, wrap="word", undo=True)
        token_input.grid(row=1, column=0, sticky="nsew", padx=10, pady=(0, 8))
        buttons = ttk.Frame(dialog)
        buttons.grid(row=2, column=0, sticky="e", padx=10, pady=(0, 10))
        save_button = ttk.Button(buttons, text="Проверить и сохранить")
        save_button.pack(side="right", padx=(6, 0))
        ttk.Button(buttons, text="Отмена", command=dialog.destroy).pack(side="right")

        def submit_token():
            token = token_input.get("1.0", "end").strip()
            if not token or not _is_v1_token(token):
                messagebox.showerror(
                    "Токен VK API v1",
                    "Принимается только готовый пользовательский токен с префиксом vk1.a… . "
                    "Автоматический вход и обмен кода не поддерживаются.",
                    parent=dialog,
                )
                return
            if any(character.isspace() for character in token):
                messagebox.showerror("Токен VK API v1", "Вставьте один токен без пробелов и переносов строк.", parent=dialog)
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
    log_text.tag_configure("log_added", foreground="#16803c")
    log_text.tag_configure("log_skip", foreground="#606770")
    log_text.tag_configure("log_missing", foreground="#9a6700")
    log_text.tag_configure("log_error", foreground="#b42318")
    log_text.tag_configure("log_info", foreground="#1d5fa7")
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
                    entry_text = str(event[1])
                    entry_tag = None
                    if "[DB-IMPORTED]" in entry_text or "[AVATAR-OK]" in entry_text:
                        entry_tag = "log_added"
                    elif "[DB-SKIP]" in entry_text:
                        entry_tag = "log_skip"
                    elif "[VK-NOT-FOUND]" in entry_text or "[NO-AVATAR]" in entry_text or "[VK-DEACTIVATED]" in entry_text:
                        entry_tag = "log_missing"
                    elif "[ERROR]" in entry_text or "[AVATAR-ERROR]" in entry_text or "[DOWNLOAD-ERROR]" in entry_text or "[DB-ERROR]" in entry_text or "[VK-API-ERROR]" in entry_text or "[CLEANUP-ERROR]" in entry_text:
                        entry_tag = "log_error"
                    elif "[CLEANUP]" in entry_text or "[SAVED-NO-IMPORT]" in entry_text:
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
