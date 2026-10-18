from __future__ import annotations

import hashlib
import importlib
import os
import json
import queue
import re
import sys
import xml.etree.ElementTree as ET
import threading
import time
from pathlib import Path
from urllib.parse import urlparse, unquote, urlsplit, urlunsplit, urljoin

try:
    from PIL import Image, ImageOps
except ImportError:  # Pillow необязателен: остаётся точное сравнение SHA-256
    Image = None
    ImageOps = None

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError


START_URL = "https://ok.ru"
OUT_DIR = Path(__file__).resolve().parent / "OK_Download"
PROFILE_DIR = Path(__file__).resolve().parent / "OK_Browser_Profile"
APP_SETTINGS_PATH = Path(__file__).resolve().with_name("ok_downloader_settings.json")
DEFAULT_FACESEARCH_DIR = Path(r"C:\FaceSearch")
DEFAULT_SETTINGS_XML = DEFAULT_FACESEARCH_DIR / "settings.xml"
DEFAULT_DB_PROFILES_JSON = DEFAULT_FACESEARCH_DIR / "database_profiles.json"


def default_chrome_executable() -> str:
    """Возвращает стандартный путь Chrome для Windows, предпочитая существующий файл."""
    candidates = [
        Path(os.environ.get("PROGRAMFILES") or r"C:\\Program Files") / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)") or r"C:\\Program Files (x86)") / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[0])


PHOTO_CONTAINER = "div.photo-stream"

NAME_SELECTORS = (
    "a.compact-profile_a",
    "div.compact-profile a",
    "h1.profile-user-info_name",
    "div.user-profile_i_name",
    "span.adaptive_menu_content",
)

ABOUT_SUMMARY_SELECTOR = "div.user-profile.__summary.__redesign"

LIGHTBOX_URL_TEMPLATE = (
    "https://ok.ru/dk?cmd=PopLayerPhoto&st.layer.cmd=PopLayerPhoto"
    "&st.layer.photoId={photo_id}"
)

LIGHTBOX_VIEWPORT = {"width": 2560, "height": 1440}
RESOLUTION_VERSION = 2  # текущая схема выбора оригинала и защиты от ошибочных дублей

# Мусорные фрагменты URL — не являются фотографиями
_SKIP_URL_PATTERNS = (
    "/res/i/", "/static/", "logo", "icon", "avatar",
    "stub", "sprite", "button", "legacy_52",
)


# ============================ Схема JSON ============================

# Верхний уровень JSON-записи строго ограничен этими четырьмя полями.
# Теперь в info хранятся только служебные поля (duplicate_*).
# _resolver_version удалён полностью — старые записи исключены.
_ALLOWED_TOP_LEVEL = ("url_source", "name", "url_image", "info")

_META_DUPLICATE_OF = "_duplicate_of"
_META_DUPLICATE_REASON = "_duplicate_reason"
_META_RESOLUTION = "_url_resolution"
_META_VERSION = "_resolution_version"

_VALID_RESOLUTIONS = {"dom", "preview", "network"}


def _meta_info(metadata: dict) -> dict:
    info = metadata.get("info") if isinstance(metadata, dict) else None
    return info if isinstance(info, dict) else {}


def _meta_get(metadata: dict, key: str, default=None):
    return _meta_info(metadata).get(key, default)


def _meta_version(metadata: dict) -> int:
    try:
        return int(_meta_get(metadata, _META_VERSION, 0) or 0)
    except (TypeError, ValueError):
        return 0


def _meta_set(metadata: dict, key: str, value) -> None:
    info = metadata.get("info")
    if not isinstance(info, dict):
        info = {}
        metadata["info"] = info
    info[key] = value


def build_metadata(photo_id: str, url_source: str, name: str, url_image: str,
                   profile_info: dict | None = None,
                   url_resolution: str = "",
                   duplicate_of: str = "",
                   duplicate_reason: str = "") -> dict:
    """Собирает JSON-запись с четырьмя верхнеуровневыми полями.

    Поля служебной валидации находятся внутри info, верхний уровень JSON
    остаётся совместимым с форматом импорта FaceSearch.
    """
    info: dict = dict(profile_info) if isinstance(profile_info, dict) else {}
    if url_resolution in _VALID_RESOLUTIONS:
        info[_META_RESOLUTION] = url_resolution
        info[_META_VERSION] = RESOLUTION_VERSION
    if duplicate_of:
        info[_META_DUPLICATE_OF] = duplicate_of
    if duplicate_reason:
        info[_META_DUPLICATE_REASON] = duplicate_reason
    return {
        "url_source": url_source,
        "name": name,
        "url_image": url_image,
        "info": info,
    }


# ============================ Утилиты ============================

def safe_filename(value: str, max_len: int = 100) -> str:
    value = unquote(value or "").strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return (value[:max_len] or "ok_photo")


def profile_id_from_url(url: str) -> str:
    path = urlparse(url).path.strip("/")
    parts = [p for p in path.split("/") if p]
    if not parts:
        return ""
    if parts[0].lower() == "profile" and len(parts) >= 2:
        return f"profile/{parts[1]}"
    return parts[0]


def profile_url_from_id(profile_id: str, base: str = "https://ok.ru") -> str:
    return f"{base.rstrip('/')}/{profile_id.strip('/')}"


def absolute_url(src: str) -> str:
    """Преобразует абсолютный, protocol-relative или относительный URL в абсолютный."""
    src = (src or "").strip()
    if not src:
        return ""
    if src.startswith("data:") or src.startswith("blob:") or src.startswith("javascript:"):
        return src
    return urljoin("https://ok.ru/", src)


def is_non_photo_url(url: str) -> bool:
    """Отсекает известные служебные картинки OK, стикеры и смайлы."""
    low = (url or "").strip().lower()
    if not low:
        return True
    if low.startswith(("data:", "blob:", "javascript:")):
        return True
    bad_fragments = (
        "/getsmile", "smileid=", "smiletype=", "/emoji", "/stickers/",
        "/res/i/", "/static/", "legacy_52", "sprite", "toolbar", "avatar",
    )
    return any(fragment in low for fragment in bad_fragments)


# ============================ Имя и /about ============================

def get_profile_name(page) -> str:
    for sel in NAME_SELECTORS:
        try:
            loc = page.locator(sel)
            if loc.count():
                name = (loc.first.inner_text(timeout=2000) or "").strip()
                if name:
                    return name
        except Exception:
            continue
    return ""


def extract_profile_info(page) -> dict[str, str]:
    """Возвращает словарь {поле: значение} со страницы /about.
    Всегда возвращает именно dict (никогда не список)."""
    info: dict[str, str] = {}
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    try:
        page.wait_for_selector(ABOUT_SUMMARY_SELECTOR, timeout=10000, state="attached")
    except PlaywrightTimeoutError:
        print(f"[WARN] Не найден контейнер {ABOUT_SUMMARY_SELECTOR}")
        return info

    data = page.evaluate(
        """(rootSel) => {
            const root = document.querySelector(rootSel);
            if (!root) return [];
            const out = [];
            const push = (k, v) => {
                k = (k || '').trim().replace(/\\s+/g, ' ');
                v = (v || '').trim().replace(/\\s+/g, ' ');
                if (k && v && k.toLowerCase() !== v.toLowerCase()) out.push({key: k, value: v});
            };
            for (const item of root.querySelectorAll('.user-profile_i')) {
                const t = item.querySelector('.user-profile_i_title');
                const v = item.querySelector('.user-profile_i_info');
                if (t && v) push(t.innerText, v.innerText);
            }
            for (const item of root.querySelectorAll('.user-profile_i')) {
                const t = item.querySelector('.user-profile_i_t_inner');
                const v = item.querySelector('.user-profile_i_value');
                if (t && v) push(t.innerText, v.innerText);
            }
            for (const grp of root.querySelectorAll('.user-profile_group_h')) {
                const h = (grp.innerText || '').trim();
                if (!h) continue;
                let nxt = grp.nextElementSibling;
                const parts = [];
                while (nxt && !nxt.classList.contains('user-profile_group_h')) {
                    const t = (nxt.innerText || '').trim().replace(/\\s+/g, ' ');
                    if (t) parts.push(t);
                    nxt = nxt.nextElementSibling;
                }
                if (parts.length) push(h, parts.join(' | '));
            }
            return out;
        }""",
        ABOUT_SUMMARY_SELECTOR,
    )

    seen: set[str] = set()
    for pair in data or []:
        key = re.sub(r"\s+", " ", str(pair.get("key", ""))).strip(" :\t\r\n")
        value = re.sub(r"\s+", " ", str(pair.get("value", ""))).strip(" :\t\r\n")
        if not key or not value or key.lower() == value.lower() or key in seen:
            continue
        seen.add(key)
        info[key[:100]] = value[:1000]
    return info


# ============================ Скачивание ============================

IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def existing_image_for_stem(stem: Path) -> Path | None:
    """Ищет ранее сохранённое изображение по стабильной основе имени."""
    for candidate in stem.parent.glob(stem.name + ".*"):
        if candidate.suffix.lower() in IMAGE_EXTENSIONS:
            try:
                if candidate.is_file() and candidate.stat().st_size > 0:
                    return candidate
            except OSError:
                pass
    return None


def existing_images_by_url(profile_url: str, stable_prefix: str = "") -> dict[str, tuple[Path, Path]]:
    """Индексирует старые пары image+JSON для безопасной миграции имён."""
    found: dict[str, tuple[Path, Path]] = {}
    for json_file in OUT_DIR.glob("*.json"):
        try:
            if stable_prefix and json_file.stem.startswith(stable_prefix):
                continue
            metadata = json.loads(json_file.read_text(encoding="utf-8"))
            if metadata.get("url_source") != profile_url:
                continue
            if _meta_get(metadata, _META_RESOLUTION) not in _VALID_RESOLUTIONS:
                continue
            if _meta_version(metadata) < RESOLUTION_VERSION:
                continue
            url = str(metadata.get("url_image") or "").strip()
            if not url:
                continue
            image_file = existing_image_for_stem(json_file.with_suffix(""))
            if image_file and normalized_url_key(url) not in found:
                found[normalized_url_key(url)] = (image_file, json_file)
        except Exception:
            continue
    return found


def write_metadata(json_path: Path, metadata: dict) -> None:
    """Атомарно пишет JSON ровно с четырьмя верхнеуровневыми полями.

    _resolver_version полностью удалён. Старые записи, которые его содержали,
    будут исключены при первом запуске новой версии.
    """
    clean: dict = {k: metadata.get(k) for k in _ALLOWED_TOP_LEVEL}

    info = clean.get("info")
    if not isinstance(info, dict):
        info = {}
    clean["info"] = info

    # Перенос служебных полей (duplicate_*)
    for legacy_key, new_key in (
        ("duplicate_of", _META_DUPLICATE_OF),
        ("duplicate_reason", _META_DUPLICATE_REASON),
    ):
        if legacy_key in metadata and new_key not in info:
            info[new_key] = metadata[legacy_key]

    if not isinstance(clean.get("url_source"), str):
        clean["url_source"] = clean.get("url_source") or ""
    if not isinstance(clean.get("name"), str):
        clean["name"] = clean.get("name") or ""
    if not isinstance(clean.get("url_image"), str):
        clean["url_image"] = clean.get("url_image") or ""

    tmp_json = json_path.with_name(json_path.name + ".part")
    tmp_json.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp_json.replace(json_path)


def deduplicate_existing_profile_images(profile_url: str) -> int:
    """Убирает старые дубли внутри одного профиля, оставляя JSON-ссылку на оригинал.

    Обрабатываются только записи с точно совпадающим url_source. Изображение
    удаляется лишь тогда, когда совпал SHA-256 или 256-битный визуальный отпечаток.
    """
    seen_sha: dict[str, Path] = {}
    seen_visual: dict[str, Path] = {}
    removed = 0
    for json_file in sorted(OUT_DIR.glob("*.json"), key=lambda p: p.name.lower()):
        try:
            metadata = json.loads(json_file.read_text(encoding="utf-8"))
            if metadata.get("url_source") != profile_url:
                continue
            # Старые записи без корректного url_resolution могли быть сохранены
            # по чужому сетевому ответу. До перепроверки не удаляем их как дубли.
            if _meta_get(metadata, _META_RESOLUTION) not in _VALID_RESOLUTIONS:
                continue
            # Не удаляем файлы из старых версий: у них URL мог быть привязан
            # к соседнему фото ошибочным сетевым ответом.
            if _meta_version(metadata) < RESOLUTION_VERSION:
                continue
            image_path = existing_image_for_stem(json_file.with_suffix(""))
            if image_path is None:
                continue
            sha, visual = image_fingerprints(image_path)
            duplicate_path = seen_sha.get(sha) if sha else None
            if duplicate_path is None and visual:
                duplicate_path = seen_visual.get(visual)
            if duplicate_path is not None and duplicate_path.exists():
                try:
                    image_path.unlink()
                    _meta_set(metadata, _META_DUPLICATE_OF, duplicate_path.name)
                    _meta_set(metadata, _META_DUPLICATE_REASON, "image")
                    write_metadata(json_file, metadata)
                    removed += 1
                    print(f"[OLD-DUPLICATE] {image_path.name} -> {duplicate_path.name}")
                    continue
                except OSError as exc:
                    print(f"[WARN] Не удалось обработать прежний дубль {image_path.name}: {exc}")
            if sha:
                seen_sha.setdefault(sha, image_path)
            if visual:
                seen_visual.setdefault(visual, image_path)
        except Exception as exc:
            print(f"[WARN] Проверка старого изображения {json_file.name}: {exc}")
    return removed


def profile_image_catalog(profile_url: str) -> tuple[dict[str, Path], dict[str, Path], dict[str, Path]]:
    """Строит индексы уже сохранённых изображений этого профиля по URL, SHA и dHash."""
    by_url: dict[str, Path] = {}
    by_sha: dict[str, Path] = {}
    by_visual: dict[str, Path] = {}
    for json_file in OUT_DIR.glob("*.json"):
        try:
            meta = json.loads(json_file.read_text(encoding="utf-8"))
            if meta.get("url_source") != profile_url:
                continue
            if _meta_get(meta, _META_RESOLUTION) not in _VALID_RESOLUTIONS:
                continue
            if _meta_version(meta) < RESOLUTION_VERSION:
                continue
            url_key = normalized_url_key(str(meta.get("url_image") or ""))
            img = existing_image_for_stem(json_file.with_suffix(""))
            if img is None:
                duplicate_name = _meta_get(meta, _META_DUPLICATE_OF)
                if duplicate_name:
                    duplicate_path = Path(str(duplicate_name))
                    if not duplicate_path.is_absolute():
                        duplicate_path = OUT_DIR / duplicate_path
                    if duplicate_path.exists():
                        img = duplicate_path
            if img is None:
                continue
            try:
                sha, visual = image_fingerprints(img)
            except Exception as exc:
                print(f"[WARN] Не удалось просчитать отпечаток {img.name}: {exc}")
                continue
            by_sha.setdefault(sha, img)
            if visual:
                by_visual.setdefault(visual, img)
            if url_key:
                by_url.setdefault(url_key, img)
        except Exception:
            continue
    return by_url, by_sha, by_visual


def normalized_url_key(url: str) -> str:
    """Нормализует URL только для сравнения; параметры изображения не удаляет."""
    url = absolute_url((url or "").strip())
    if not url:
        return ""
    try:
        parts = urlsplit(url)
        return urlunsplit((parts.scheme.lower(), parts.netloc.lower(), parts.path, parts.query, ""))
    except Exception:
        return url


def image_extension(body: bytes, content_type: str, url: str) -> str:
    """Определяет расширение по содержимому, а не только по URL."""
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    if body.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if body.startswith(b"GIF87a") or body.startswith(b"GIF89a"):
        return ".gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return ".webp"
    if body.startswith(b"BM"):
        return ".bmp"
    if len(body) >= 12 and body[4:8] == b"ftyp" and body[8:12] in (b"avif", b"avis"):
        return ".avif"
    by_type = {
        "image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png",
        "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp",
        "image/avif": ".avif",
    }
    if ct in by_type:
        return by_type[ct]
    suffix = Path(urlparse(url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ".jpg"


def image_fingerprints(path: Path) -> tuple[str, str | None]:
    """Возвращает SHA-256 и устойчивый визуальный хеш, если доступен Pillow."""
    raw = path.read_bytes()
    sha = hashlib.sha256(raw).hexdigest()
    if Image is None:
        return sha, None
    try:
        from io import BytesIO
        with Image.open(BytesIO(raw)) as im:
            if ImageOps is not None:
                im = ImageOps.exif_transpose(im)
            im = im.convert("L")
            resampling = getattr(getattr(Image, "Resampling", Image), "LANCZOS", 1)
            im = im.resize((17, 16), resampling)
            get_flattened = getattr(im, "get_flattened_data", None)
            px = list(get_flattened() if callable(get_flattened) else im.getdata())
        bits = 0
        bit_index = 0
        # 256-битный difference hash. Даёт совпадение при другой JPEG-компрессии
        # и не требует считать близкие, но разные фотографии дублями.
        for y in range(16):
            row = y * 17
            for x in range(16):
                if px[row + x] > px[row + x + 1]:
                    bits |= (1 << bit_index)
                bit_index += 1
        mean_level = sum(px) / len(px)
        variance = sum((value - mean_level) ** 2 for value in px) / len(px)
        # Средний тон/контраст уменьшают риск совпадения у однотонных заглушек.
        mean_bucket = min(31, int(mean_level // 8))
        contrast_bucket = min(31, int((variance ** 0.5) // 4))
        return sha, f"{bits:064x}:{mean_bucket:02x}:{contrast_bucket:02x}"
    except Exception:
        return sha, None


def download_one(context, url: str, stem: Path, referer: str = "https://ok.ru/") -> Path | None:
    """Скачивает изображение, проверяет ответ и атомарно сохраняет его.

    В отличие от прежней версии принимает stem без расширения и возвращает
    фактический путь с расширением, определённым по содержимому ответа.
    """
    if not url:
        return None

    headers = {
        "Referer": referer or "https://ok.ru/",
        "Accept": "image/avif,image/webp,image/apng,image/svg+xml,image/*,*/*;q=0.8",
    }
    for attempt in range(3):
        tmp: Path | None = None
        try:
            response = context.request.get(url, timeout=25000, headers=headers)
            if not response.ok:
                status = response.status
                print(f"[HTTP {status}] {url}")
                if status in (408, 425, 429) or status >= 500:
                    if attempt < 2:
                        time.sleep(0.3 * (attempt + 1))
                        continue
                return None

            content_type = (response.headers.get("content-type") or "").lower()
            body = response.body()
            if not body or "text/html" in content_type or body.lstrip()[:32].lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                print(f"[SKIP] Сервер вернул не изображение: {url}")
                return None

            known_signature = body.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF", b"BM"))
            is_avif = len(body) >= 12 and body[4:8] == b"ftyp" and body[8:12] in (b"avif", b"avis")
            if not known_signature and not is_avif and not content_type.startswith("image/"):
                print(f"[SKIP] Неизвестный формат ответа ({content_type or 'без Content-Type'}): {url}")
                return None
            if not known_signature and not is_avif and len(body) < 256:
                print(f"[SKIP] Слишком короткий ответ вместо фотографии ({len(body)} байт): {url}")
                return None

            ext = image_extension(body, content_type, url)
            target = Path(str(stem) + ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(body)
            tmp.replace(target)
            return target
        except Exception as exc:
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            if attempt < 2:
                time.sleep(0.3 * (attempt + 1))
                continue
            print(f"[ERROR] Не удалось скачать {url}: {exc}")
            return None
    return None


# ============================ Поиск вкладки ============================

def find_photos_page(context):
    """Ищет вкладку с открытым разделом «Фото» (div.photo-stream).
    Приоритет — URL с /photos. Никогда не возвращает случайную вкладку."""
    candidates = []
    for p in context.pages:
        if p.is_closed():
            continue
        try:
            url = p.url
        except Exception:
            continue
        if "ok.ru" not in urlparse(url).netloc.lower():
            continue
        try:
            if p.locator(PHOTO_CONTAINER).count() > 0:
                candidates.append(p)
        except Exception:
            continue

    if not candidates:
        return None

    candidates.sort(key=lambda p: 0 if "/photos" in p.url else 1)
    return candidates[0]


# ============================ Сбор photo_id + preview + href ============================

def collect_photo_entries(page) -> dict[str, dict]:
    """Возвращает {photo_id: {"preview": url, "href": "/dk?..."}}.
    href — готовый URL лайтбокса со всеми параметрами."""
    return page.evaluate("""() => {
        const out = {};
        for (const card of document.querySelectorAll('.photo-card')) {
            const id = card.getAttribute('data-id');
            if (!id) continue;

            const img = card.querySelector('img.va_target, img');

            let a = card.querySelector('a[href*="PopLayerPhoto"]');
            if (!a) a = card.querySelector('a.photo-card_cnt');
            if (!a) a = card.querySelector('a');

            const entry = {};
            if (img) {
                entry.preview = img.currentSrc || img.getAttribute('data-original') || img.getAttribute('data-original-src') || img.getAttribute('data-src') || img.getAttribute('data-lazy-src') || img.getAttribute('src') || img.getAttribute('data-url') || '';
            }
            if (a) {
                entry.href = a.getAttribute('href') || '';
            }
            if (entry.preview || entry.href) out[id] = entry;
        }
        return out;
    }""")


def collect_photo_entries_with_scroll(page, status_callback=None) -> dict[str, dict]:
    """Собирает фотографии с ленивой подгрузкой и рано заканчивает на простое."""
    result: dict[str, dict] = {}
    idle_rounds = 0
    max_rounds = 100
    idle_limit = 4

    for i in range(max_rounds):
        try:
            chunk = collect_photo_entries(page)
        except Exception as exc:
            print(f"[WARN] Не удалось прочитать карточки: {exc}")
            break

        before = len(result)
        result.update(chunk)
        added = len(result) - before
        idle_rounds = 0 if added > 0 else idle_rounds + 1

        if status_callback and (i == 0 or added > 0 or i % 8 == 0):
            status_callback(f"Собрано фотографий: {len(result)}")

        if idle_rounds >= idle_limit:
            break

        try:
            known_ids = list(result.keys())
            page.evaluate("window.scrollBy(0, Math.max(900, window.innerHeight * 0.85))")
            # Ждём появления следующего photo_id, но не блокируемся надолго на медленной сети.
            try:
                page.wait_for_function("""(known) => {
                    for (const card of document.querySelectorAll('.photo-card')) {
                        const id = card.getAttribute('data-id');
                        if (id && !known.includes(id)) return true;
                    }
                    return false;
                }""", arg=known_ids, timeout=2200)
            except PlaywrightTimeoutError:
                pass
            page.wait_for_timeout(150)
        except Exception:
            break

    return result


# ============================ Резолв оригинала через лайтбокс ============================

def resolve_fullsize_urls(context, photo_entries: dict[str, dict],
                          status_callback=None) -> dict[str, str]:
    """Получает URL изображения из DOM, а при неудаче — из запросов текущего лайтбокса.

    Важно: сетевые ответы собираются отдельно для каждого photo_id. Смайлы,
    стикеры, аватары и служебные изображения исключаются. Это восстанавливает
    работавший раньше сетевой fallback, но не выбирает произвольный ответ CDN.
    """
    result: dict[str, str] = {}
    lightbox = context.new_page()
    try:
        lightbox.set_viewport_size(LIGHTBOX_VIEWPORT)
    except Exception:
        pass

    # В сетевой fallback допускаются только ответы, появившиеся после перехода
    # к текущему фото; список очищается непосредственно перед каждым goto.
    captured: list[dict] = []

    def on_response(response):
        try:
            url = response.url or ""
            low = url.lower()
            if is_non_photo_url(url):
                return
            if not any(domain in low for domain in ("okcdn.ru", "mycdn.me", "imgsmail.ru")):
                return
            headers = response.headers
            content_type = (headers.get("content-type") or "").lower()
            if "image/" not in content_type and "octet-stream" not in content_type:
                return
            try:
                content_length = int(headers.get("content-length") or 0)
            except (TypeError, ValueError):
                content_length = 0
            # getSmile часто имеет image/* и большой размер, поэтому отсекается
            # по URL выше. Маленькие сетевые ресурсы почти всегда UI/иконки.
            captured.append({
                "time": time.monotonic(),
                "url": url,
                "size": content_length,
                "content_type": content_type,
                "status": response.status,
            })
        except Exception:
            pass

    lightbox.on("response", on_response)

    total = len(photo_entries)
    try:
        for i, (pid, entry) in enumerate(photo_entries.items(), 1):
            if status_callback:
                status_callback(f"Получение оригинала {i}/{total}…")

            href = absolute_url((entry.get("href") or "").strip())
            if not href or ("poplayerphoto" not in href.lower() and "st.layer.photoid=" not in href.lower()):
                href = LIGHTBOX_URL_TEMPLATE.format(photo_id=pid)

            captured.clear()
            nav_started = time.monotonic()
            try:
                lightbox.goto(href, wait_until="domcontentloaded", timeout=18000)
            except Exception as exc:
                print(f"[WARN] Не удалось открыть лайтбокс {pid}: {exc}")

            # Сначала ищем DOM-изображение по всему документу, а не только внутри
            # фиксированных ID: разметка OK меняется между типами аккаунтов/альбомов.
            try:
                lightbox.wait_for_function(r"""() => {
                    const skip = /getSmile|smileId=|smileType=|\/res\/i\/|legacy_52|\/static\/|avatar|emoji|sticker/i;
                    return [...document.images].some(img => {
                        const u = img.currentSrc || img.src || '';
                        return img.complete && img.naturalWidth >= 500 && img.naturalHeight >= 350
                            && !skip.test(u) && /okcdn\.ru|mycdn\.me|imgsmail\.ru/i.test(u);
                    });
                }""", timeout=2200)
            except Exception:
                pass

            image_data = None
            try:
                image_data = lightbox.evaluate(r"""() => {
                    const reject = /getSmile|smileId=|smileType=|\/res\/i\/|legacy_52|\/static\/|avatar|emoji|sticker|sprite|toolbar/i;
                    const toAbs = u => { try { return new URL(u, location.href).href; } catch (_) { return ''; } };
                    const candidates = [];
                    for (const img of document.images) {
                        let el = img, skip = false;
                        while (el && el !== document.body) {
                            const cls = (typeof el.className === 'string') ? el.className : '';
                            if (/avatar|topPanel|toolbar|navbar|reaction|emoji|sticker|sprite/i.test(cls)) { skip = true; break; }
                            el = el.parentElement;
                        }
                        if (skip) continue;
                        const w = img.naturalWidth || 0, h = img.naturalHeight || 0;
                        if (w < 300 || h < 250 || w * h < 100000) continue;
                        let src = img.currentSrc || img.getAttribute('data-original') || img.getAttribute('data-original-src') ||
                                  img.getAttribute('data-src') || img.getAttribute('data-url') || img.src || '';
                        const srcset = img.getAttribute('srcset') || '';
                        if (srcset) {
                            const opts = srcset.split(',').map(item => {
                                const parts = item.trim().split(/\s+/);
                                const descriptor = parts[1] || '1x';
                                const n = parseFloat(descriptor) || 1;
                                return {url: parts[0], score: descriptor.endsWith('w') ? n : n * 10000};
                            }).filter(x => x.url);
                            if (opts.length) { opts.sort((a,b) => b.score-a.score); src = opts[0].url; }
                        }
                        src = toAbs(src);
                        if (!src || reject.test(src) || !/okcdn\.ru|mycdn\.me|imgsmail\.ru/i.test(src)) continue;
                        const rect = img.getBoundingClientRect();
                        const area = Math.max(w * h, rect.width * rect.height);
                        candidates.push({url: src, width: w, height: h, area});
                    }
                    // Если атрибуты скрыты в обёртке, учитываем крупный background-image.
                    for (const el of document.querySelectorAll('[style*="background"]')) {
                        const cls = (typeof el.className === 'string') ? el.className : '';
                        if (/avatar|toolbar|reaction|emoji|sticker|sprite/i.test(cls)) continue;
                        const bg = getComputedStyle(el).backgroundImage || '';
                        const match = bg.match(/url\(["']?(.*?)["']?\)/i);
                        if (!match || !match[1]) continue;
                        const src = toAbs(match[1]);
                        const r = el.getBoundingClientRect();
                        if (!src || reject.test(src) || !/okcdn\.ru|mycdn\.me|imgsmail\.ru/i.test(src) || r.width < 300 || r.height < 250) continue;
                        candidates.push({url: src, width: Math.round(r.width), height: Math.round(r.height), area: r.width*r.height});
                    }
                    candidates.sort((a,b) => b.area-a.area);
                    return candidates[0] || null;
                }""")
            except Exception as exc:
                print(f"[WARN] Ошибка чтения DOM фото {pid}: {exc}")

            if image_data and image_data.get("url"):
                url = absolute_url(str(image_data["url"]))
                if url and not is_non_photo_url(url):
                    result[pid] = url
                    entry["_url_resolution"] = "dom"
                    print(f"[OK] {pid}: DOM {image_data.get('width')}x{image_data.get('height')} — {url[:100]}")
                    continue

            # DOM OK периодически не содержит крупное <img>. В этом случае берём
            # крупный image response именно от загрузки текущего фото, не getSmile.
            try:
                lightbox.wait_for_timeout(450)
            except Exception:
                pass
            fresh = [item for item in captured if item["time"] >= nav_started and item["status"] == 200]
            eligible = [item for item in fresh if item["size"] >= 20_000]
            if not eligible:
                # У некоторых CDN content-length отсутствует. Допускаем только
                # один достаточно свежий image response с домена CDN, если его
                # URL очевидно не относится к иконкам/смайлам.
                eligible = [item for item in fresh if item["size"] == 0]
            if eligible:
                # Сначала предпочитаем ответ с известным наибольшим размером;
                # при равенстве — самый поздний, загруженный при открытии фото.
                chosen = max(eligible, key=lambda item: (item["size"], item["time"]))
                url = absolute_url(chosen["url"])
                if url and not is_non_photo_url(url):
                    result[pid] = url
                    entry["_url_resolution"] = "network"
                    size_text = f"{chosen['size']} байт" if chosen["size"] else "размер неизвестен"
                    print(f"[OK] {pid}: сетевой ответ {size_text} — {url[:100]}")
                    continue

            preview = absolute_url(entry.get("preview", ""))
            result[pid] = "" if is_non_photo_url(preview) else preview
            entry["_url_resolution"] = "preview" if result[pid] else "none"
            if result[pid]:
                print(f"[FALLBACK-PREVIEW] {pid}: оригинал не найден в DOM/сети; будет скачана миниатюра")
            else:
                print(f"[NO-IMAGE-URL] {pid}: не найден ни оригинал, ни пригодная миниатюра")
    finally:
        try:
            lightbox.close()
        except Exception:
            pass

    return result


# ============================ Интеграция FaceSearch ============================


def load_face_search_config(settings_xml: str | Path, profiles_json: str | Path) -> dict:
    """Читает пользовательские настройки и определяет активный профиль FaceSearch."""
    settings_path = Path(settings_xml).expanduser().resolve()
    profiles_path = Path(profiles_json).expanduser().resolve()
    if not settings_path.is_file():
        raise FileNotFoundError(f"Не найден settings.xml: {settings_path}")
    if not profiles_path.is_file():
        raise FileNotFoundError(f"Не найден database_profiles.json: {profiles_path}")

    try:
        xml_root = ET.parse(settings_path).getroot()
    except Exception as exc:
        raise ValueError(f"Не удалось разобрать settings.xml: {exc}") from exc
    try:
        db_data = json.loads(profiles_path.read_text(encoding="utf-8-sig"))
    except Exception as exc:
        raise ValueError(f"Не удалось разобрать database_profiles.json: {exc}") from exc

    raw_profiles = db_data.get("profiles") if isinstance(db_data, dict) else None
    if not isinstance(raw_profiles, list):
        raise ValueError("В database_profiles.json отсутствует список profiles.")
    profiles = [p for p in raw_profiles if isinstance(p, dict) and str(p.get("name") or "").strip()]
    if not profiles:
        raise ValueError("В database_profiles.json нет ни одного профиля с именем.")

    selected_in_settings = str(xml_root.findtext("database/profile") or "").strip()
    # FaceSearch сам считает поле database_profiles.json -> active авторитетным.
    # XML-профиль используется как резерв для старых/неполных конфигураций.
    active_name = str(db_data.get("active") or "").strip() if isinstance(db_data, dict) else ""
    by_name = {str(p.get("name")): p for p in profiles}
    if active_name not in by_name:
        active_name = selected_in_settings if selected_in_settings in by_name else str(profiles[0]["name"])
    profile = dict(by_name[active_name])

    backend = str(xml_root.findtext("compute/backend") or "gpu").strip().lower()
    return {
        "settings_path": settings_path,
        "profiles_path": profiles_path,
        "settings_profile": selected_in_settings,
        "active_name": active_name,
        "profile": profile,
        "backend": backend,
        "root": find_face_search_root(settings_path, profiles_path),
    }


def find_face_search_root(settings_path: Path, profiles_path: Path) -> Path:
    """Ищет каталог FaceSearch, в котором лежат face_search_gui.py и facesearch/."""
    candidates: list[Path] = []
    for base in (profiles_path.parent, settings_path.parent, Path(__file__).resolve().parent):
        for candidate in (base, *base.parents):
            if candidate not in candidates:
                candidates.append(candidate)
    for candidate in candidates:
        if (candidate / "face_search_gui.py").is_file() and (candidate / "facesearch").is_dir():
            return candidate.resolve()
    raise FileNotFoundError(
        "Не найден корень FaceSearch (ожидаются face_search_gui.py и каталог facesearch). "
        "Выберите settings.xml и database_profiles.json из каталога установленного FaceSearch."
    )


class FaceSearchImporter:
    """Тонкий адаптер к штатному импорту FaceSearch; URL проверяется до скачивания."""

    def __init__(
        self,
        settings_xml: str | Path,
        profiles_json: str | Path,
        status_callback,
        *,
        create_crop_files: bool = True,
    ):
        self.status_callback = status_callback
        self.create_crop_files = bool(create_crop_files)
        self.config = load_face_search_config(settings_xml, profiles_json)
        self.module = self._load_module(self.config["root"])
        profile = self.config["profile"]
        profile_type = str(profile.get("type") or "sqlite").strip().lower()
        target = str(profile.get("database") or profile.get("path") or "").strip()
        host = str(profile.get("host") or "").strip()
        description = f"{profile_type}: {target}" if target else profile_type
        if host and profile_type != "sqlite":
            description += f" @ {host}"
        self.profile_description = f"{self.config['active_name']} ({description})"
        self.app = None
        self.imported_count = 0
        self.skipped_count = 0
        self.profile_metadata_updated = False
        self.conn = None

        self.status_callback(f"Подключение к FaceSearch: {self.profile_description}…")
        try:
            if profile_type == "sqlite":
                database_path = Path(
                    str(profile.get("path") or self.config["root"] / "faces.db")
                ).expanduser()
                if not database_path.is_absolute():
                    database_path = self.config["root"] / database_path
                if not database_path.is_file():
                    raise FileNotFoundError(
                        f"Файл активной SQLite-базы не найден: {database_path}. "
                        "Проверьте database_profiles.json."
                    )
                profile["path"] = str(database_path.resolve())
            self.conn = self.module.create_database_for_profile(
                profile,
                maintenance=False,
                create_if_missing=False,
                activate_storage=True,
                initialize=False,
                check_crop_migration=False,
            )
            # Добавляем колонку идентификатора фотографии и в старые БД, даже если
            # пользователь ещё не запускал GUI FaceSearch после обновления программы.
            ensure_columns = getattr(self.module, "_ensure_columns", None)
            if callable(ensure_columns):
                ensure_columns(self.conn, "images", {"source_key": "VARCHAR(255)"})
                self.conn.commit()
            # Ранний контроль схемы предотвращает скачивание, если выбран не тот файл БД.
            self.conn.execute("SELECT id, source_type, url_image, source_key FROM images WHERE 1 = 0").fetchall()
        except Exception:
            if self.conn is not None:
                try:
                    self.conn.close()
                except Exception:
                    pass
            raise

    @staticmethod
    def _load_module(root: Path):
        root = root.resolve()
        existing = sys.modules.get("face_search_gui")
        if existing is not None:
            existing_path = Path(getattr(existing, "__file__", "")).resolve()
            if existing_path.parent != root:
                raise RuntimeError(
                    f"Уже загружен другой FaceSearch ({existing_path.parent}); "
                    f"требуется {root}. Перезапустите загрузчик."
                )
            module = existing
        else:
            pkg = sys.modules.get("facesearch")
            if pkg is not None:
                pkg_path = Path(getattr(pkg, "__file__", "")).resolve()
                if root not in pkg_path.parents and pkg_path.parent != root / "facesearch":
                    raise RuntimeError(
                        f"Уже загружен другой пакет facesearch ({pkg_path}); перезапустите загрузчик."
                    )
            root_text = str(root)
            if root_text not in sys.path:
                sys.path.insert(0, root_text)
            module = importlib.import_module("face_search_gui")
        if Path(module.__file__).resolve().parent != root:
            raise RuntimeError("FaceSearch загружен не из выбранного каталога.")
        return module

    def lookup_existing_urls(self, urls, profile_url: str | None = None) -> set[str]:
        """Пакетно проверяет URL; при известном профиле не смешивает чужие альбомы."""
        keys: set[str] = set()
        values: set[str] = set()
        for value in urls or []:
            raw = str(value or "").strip()
            if not raw:
                continue
            canonical = normalized_url_key(raw)
            if canonical:
                values.add(canonical)
                values.add(raw)
        values = {value for value in values if value}
        if not values or self.conn is None:
            return keys

        ordered = sorted(values)
        chunk_size = 400  # ниже лимита параметров SQL Server
        for offset in range(0, len(ordered), chunk_size):
            chunk = ordered[offset:offset + chunk_size]
            placeholders = ",".join("?" for _ in chunk)
            if profile_url:
                query = (
                    "SELECT i.url_image FROM images i "
                    "JOIN profiles p ON p.id = i.profile_id "
                    f"WHERE i.url_image IN ({placeholders}) AND i.url_image IS NOT NULL "
                    "AND i.source_type = 'import' AND p.source = 'import' AND p.profile_url = ?"
                )
                params = (*chunk, profile_url)
            else:
                query = (
                    "SELECT url_image FROM images "
                    f"WHERE url_image IN ({placeholders}) AND url_image IS NOT NULL"
                )
                params = tuple(chunk)
            try:
                rows = self.conn.execute(query, params).fetchall()
            except Exception as exc:
                raise RuntimeError(f"Не удалось проверить URL изображений в FaceSearch: {exc}") from exc
            for row in rows:
                key = normalized_url_key(str(row[0] or ""))
                if key:
                    keys.add(key)
        return keys

    def sync_existing_photo(
        self,
        *,
        profile_url: str,
        source_key: str,
        old_url: str = "",
        new_url: str = "",
    ) -> int:
        """Связывает существующие CROP с устойчивым ID фото и обновляет URL без inference.

        Сначала ищет ранее сохранённый source_key. Для старых БД без этого поля
        разрешает однократную привязку по прежнему URL, но только внутри того же
        импортированного профиля и только для строк без source_key.
        """
        if self.conn is None:
            raise RuntimeError("Соединение с активной базой FaceSearch уже закрыто.")
        source_key = str(source_key or "").strip()
        profile_url = str(profile_url or "").strip()
        if not source_key or not profile_url:
            return 0
        profile_row = self.conn.execute(
            "SELECT id FROM profiles WHERE source = 'import' AND profile_url = ? LIMIT 1",
            (profile_url,),
        ).fetchone()
        if not profile_row:
            return 0
        profile_id = int(profile_row[0])

        candidates: set[str] = set()
        for value in (old_url, new_url):
            raw = str(value or "").strip()
            if raw:
                candidates.add(raw)
                normalized = normalized_url_key(raw)
                if normalized:
                    candidates.add(normalized)
        candidate_rows = []
        if candidates:
            ordered = sorted(candidates)
            for offset in range(0, len(ordered), 350):
                chunk = ordered[offset:offset + 350]
                placeholders = ",".join("?" for _ in chunk)
                candidate_rows.extend(self.conn.execute(
                    "SELECT i.id, i.url_image, i.source_key FROM images i "
                    "WHERE i.source_type = 'import' AND i.profile_id = ? "
                    "AND EXISTS (SELECT 1 FROM faces f WHERE f.image_id = i.id) "
                    f"AND ((i.source_key = ?) OR ((i.source_key IS NULL OR i.source_key = '') "
                    f"AND i.url_image IN ({placeholders}))) ORDER BY i.id",
                    (profile_id, source_key, *chunk),
                ).fetchall())
        else:
            candidate_rows = self.conn.execute(
                "SELECT i.id, i.url_image, i.source_key FROM images i "
                "WHERE i.source_type = 'import' AND i.profile_id = ? AND i.source_key = ? "
                "AND EXISTS (SELECT 1 FROM faces f WHERE f.image_id = i.id) ORDER BY i.id",
                (profile_id, source_key),
            ).fetchall()

        # The same image id may be selected via both normalized/raw URL candidates.
        unique_rows = {int(row[0]): row for row in candidate_rows}
        if not unique_rows:
            return 0

        timestamp = time.strftime("%Y-%m-%d %H:%M:%S")
        changed = 0
        for image_id, _current_url, _current_key in unique_rows.values():
            self.conn.execute(
                "UPDATE images SET source_key = ?, url_image = ?, source_url = ?, "
                "source_updated_at = ?, metadata_json = NULL WHERE id = ?",
                (source_key, new_url or old_url or _current_url, profile_url, timestamp, int(image_id)),
            )
            changed += 1
        self.conn.commit()
        if changed:
            print(f"[DB-URL-SYNC] {profile_url}: обновлено строк CROP={changed}; source_key={source_key}")
        return changed

    def update_profile_metadata(self, metadata: dict) -> bool:
        """Обновляет только метаданные профиля в FaceSearch, не трогая images/faces.

        Метод намеренно не создаёт FaceAnalysis и не запускает распознавание.
        У существующего профиля сохраняет url_image из raw_json, поскольку это
        поле относится к конкретной фотографии, а не к профилю целиком.
        """
        if self.conn is None:
            raise RuntimeError("Соединение с активной базой FaceSearch уже закрыто.")
        if not isinstance(metadata, dict):
            raise ValueError("Метаданные профиля должны быть JSON-объектом.")
        source_url = str(metadata.get("url_source") or "").strip()
        if not source_url:
            raise ValueError("В метаданных профиля отсутствует url_source.")

        clean = {
            "url_source": source_url,
            "name": str(metadata.get("name") or ""),
            "url_image": str(metadata.get("url_image") or ""),
            "info": dict(metadata.get("info") or {}) if isinstance(metadata.get("info"), dict) else {},
        }
        # Служебные признаки конкретной фотографии не должны попадать в
        # профильные сведения FaceSearch.
        for key in (_META_RESOLUTION, _META_VERSION, _META_DUPLICATE_OF, _META_DUPLICATE_REASON):
            clean["info"].pop(key, None)

        row = self.conn.execute(
            "SELECT raw_json FROM profiles WHERE source = 'import' AND profile_url = ? LIMIT 1",
            (source_url,),
        ).fetchone()
        old_key = None
        if row:
            try:
                old_document = json.loads(str(row[0] or ""))
            except Exception:
                old_document = {}
            if isinstance(old_document, dict):
                old_key_fn = getattr(self.module, "_import_profile_metadata_key", None)
                if callable(old_key_fn):
                    old_key = old_key_fn(old_document)
                else:
                    old_document.pop("url_image", None)
                    old_key = json.dumps(old_document, ensure_ascii=False, sort_keys=True, separators=(",", ":"))
                clean["url_image"] = str(old_document.get("url_image") or clean["url_image"])

        new_key_fn = getattr(self.module, "_import_profile_metadata_key", None)
        if callable(new_key_fn):
            new_key = new_key_fn(clean)
        else:
            candidate = dict(clean)
            candidate.pop("url_image", None)
            new_key = json.dumps(candidate, ensure_ascii=False, sort_keys=True, separators=(",", ":"))

        if row and old_key == new_key:
            return False

        upsert = getattr(self.module, "_upsert_import_profile", None)
        if not callable(upsert):
            raise RuntimeError(
                "В установленном FaceSearch нет штатной функции обновления импортированных профилей."
            )
        upsert(self.conn, clean)
        self.conn.commit()
        self.profile_metadata_updated = True
        print(f"[DB-METADATA] Метаданные профиля обновлены: {source_url}")
        return True

    def import_image(
        self,
        image_path: Path,
        json_path: Path,
        metadata: dict | None = None,
        *,
        source_key: str | None = None,
    ) -> dict:
        if metadata is None:
            metadata = json.loads(json_path.read_text(encoding="utf-8"))
        if not isinstance(metadata, dict):
            raise ValueError(f"Метаданные {json_path.name} должны быть JSON-объектом.")
        if self.app is None:
            self.status_callback("Инициализация модели распознавания FaceSearch…")
            backend_value = self.config.get("backend", "gpu")
            backend = (
                self.module.BACKEND_CPU
                if backend_value in {"cpu", "processor", "процессор"}
                else self.module.BACKEND_GPU
            )
            self.app = self.module.create_face_app(backend=backend)

        self.status_callback(f"Добавление в FaceSearch: {image_path.name}…")
        # Не передаём служебные поля резолвера в метаданные профиля FaceSearch.
        db_metadata = dict(metadata)
        db_info = dict(metadata.get("info") or {}) if isinstance(metadata.get("info"), dict) else {}
        db_info.pop(_META_RESOLUTION, None)
        db_info.pop(_META_VERSION, None)
        db_metadata["info"] = db_info
        result = self.module.import_image_bundle(
            self.conn,
            self.app,
            image_path,
            json_path,
            delete_source=False,
            create_crop_files=self.create_crop_files,
            overwrite_existing_crops=False,
            metadata=db_metadata,
            source_key=source_key,
        )
        self.imported_count += 1
        return result

    def build_index(self, progress_callback=None):
        """Строит индекс FAISS для выбранного активного профиля FaceSearch."""
        if self.conn is None:
            raise RuntimeError("Соединение с активной базой FaceSearch уже закрыто.")
        self.status_callback("Создание индекса FAISS для активного профиля…")
        if progress_callback is not None:
            progress_callback(None, None, "Построение индекса FAISS — операция может занять продолжительное время")
        builder = getattr(self.module, "build_faiss_index", None)
        if not callable(builder):
            raise RuntimeError("В загруженном FaceSearch не найдена функция build_faiss_index().")
        builder(self.conn)
        if progress_callback is not None:
            progress_callback(1, 1, "Индекс FAISS создан")
        self.status_callback("Индекс FAISS создан.")

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            finally:
                self.conn = None


def load_gui_paths() -> dict[str, str]:
    """Загружает один каталог FaceSearch, сохраняя совместимость со старой формой."""
    defaults = {
        "facesearch_dir": str(DEFAULT_FACESEARCH_DIR),
        "auto_import": True,
        "create_crop_files": True,
        "build_index_after": False,
        "browser_profile_dir": str(PROFILE_DIR),
        "chrome_exe_path": default_chrome_executable(),
        "delete_processed_files": False,
    }
    try:
        if APP_SETTINGS_PATH.is_file():
            data = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                saved_dir = str(data.get("facesearch_dir") or "").strip()
                if not saved_dir:
                    # Совместимость с предыдущей версией, где задавались пути к обоим файлам.
                    old_settings = str(data.get("settings_xml") or "").strip()
                    old_profiles = str(data.get("profiles_json") or "").strip()
                    if old_settings and old_profiles:
                        try:
                            if Path(old_settings).expanduser().parent.resolve() == Path(old_profiles).expanduser().parent.resolve():
                                saved_dir = str(Path(old_settings).expanduser().parent)
                        except OSError:
                            pass
                    elif old_settings:
                        saved_dir = str(Path(old_settings).expanduser().parent)
                if saved_dir:
                    defaults["facesearch_dir"] = saved_dir
                defaults["auto_import"] = bool(data.get("auto_import", True))
                defaults["create_crop_files"] = bool(data.get("create_crop_files", True))
                defaults["build_index_after"] = bool(data.get("build_index_after", False))
                defaults["browser_profile_dir"] = str(data.get("browser_profile_dir") or defaults["browser_profile_dir"])
                defaults["chrome_exe_path"] = str(data.get("chrome_exe_path") or defaults["chrome_exe_path"])
                defaults["delete_processed_files"] = bool(data.get("delete_processed_files", False))
    except Exception as exc:
        print(f"[WARN] Не удалось загрузить настройки окна: {exc}")
    return defaults


def save_gui_paths(
    facesearch_dir: str,
    auto_import: bool,
    create_crop_files: bool = True,
    build_index_after: bool = False,
    browser_profile_dir: str = "",
    chrome_exe_path: str = "",
    delete_processed_files: bool = False,
) -> None:
    payload = {
        "facesearch_dir": str(facesearch_dir or "").strip(),
        "auto_import": bool(auto_import),
        "create_crop_files": bool(create_crop_files),
        "build_index_after": bool(build_index_after),
        "browser_profile_dir": str(browser_profile_dir or "").strip(),
        "chrome_exe_path": str(chrome_exe_path or "").strip(),
        "delete_processed_files": bool(delete_processed_files),
    }
    tmp = APP_SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(payload, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(APP_SETTINGS_PATH)


def config_paths_from_directory(facesearch_dir: str | Path) -> tuple[Path, Path]:
    """Фиксированные имена конфигураций внутри выбранного каталога FaceSearch."""
    directory = Path(facesearch_dir).expanduser()
    return directory / "settings.xml", directory / "database_profiles.json"


def launch_browser_context(playwright, profile_directory: str | Path, chrome_executable: str | Path):
    """Запускает установленный Chrome с указанным постоянным профилем."""
    profile_path = Path(profile_directory).expanduser()
    if not str(profile_path).strip():
        raise ValueError("Не задан каталог профиля Chrome.")
    profile_path.mkdir(parents=True, exist_ok=True)

    executable = Path(chrome_executable).expanduser()
    if not executable.is_file():
        raise FileNotFoundError(
            f"Не найден chrome.exe: {executable}. Укажите корректный путь к исполняемому файлу Chrome."
        )
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_path.resolve()),
        executable_path=str(executable.resolve()),
        headless=False,
        viewport=None,
        no_viewport=True,
        locale="ru-RU",
        accept_downloads=True,
        args=[
            "--start-maximized",
            "--window-size=1920,1080",
            "--window-position=0,0",
        ],
    )


def delete_processed_pair(image_path: Path | None, json_path: Path | None, *, enabled: bool) -> int:
    """Удаляет только пару изображения/JSON внутри OUT_DIR после успешной обработки."""
    if not enabled:
        return 0
    root = OUT_DIR.resolve()
    removed = 0
    for candidate in (image_path, json_path):
        if candidate is None:
            continue
        path = Path(candidate)
        try:
            resolved = path.resolve()
            resolved.relative_to(root)
            if resolved.is_file():
                resolved.unlink()
                removed += 1
        except ValueError:
            print(f"[WARN] Не удаляю файл за пределами каталога загрузки: {path}")
        except OSError as exc:
            print(f"[WARN] Не удалось удалить обработанный файл {path}: {exc}")
    if removed:
        print(f"[CLEANUP-PROCESSED] Удалено файлов: {removed}")
    return removed


def import_result_allows_cleanup(result: dict) -> bool:
    """Не удаляет фото, где штатная детекция не нашла лица и импорт не создал запись."""
    message = str(result.get("message") or "").strip().lower()
    return not message.startswith("лицо не найдено")


# ============================ Основной сбор ============================

def collect_photos(
    context,
    status_callback,
    resolve_fullsize: bool = True,
    importer: FaceSearchImporter | None = None,
    build_index_after: bool = False,
    progress_callback=None,
    delete_processed_files: bool = False,
):
    def report_progress(current=None, total=None, message=""):
        if progress_callback is not None:
            try:
                progress_callback(current, total, message)
            except Exception as exc:
                print(f"[WARN] Не удалось обновить индикатор прогресса: {exc}")

    index_built = False

    def finish_successfully():
        nonlocal index_built
        if importer and build_index_after and not index_built:
            importer.build_index(progress_callback=report_progress)
            index_built = True
        report_progress(1, 1, "Обработка завершена")

    report_progress(None, None, "Поиск активной вкладки OK.ru…")
    page = find_photos_page(context)
    if page is None:
        status_callback(
            "Не нашёл вкладку с открытым разделом «Фото». "
            "Откройте профиль → Фото и повторите."
        )
        report_progress(0, 1, "Операция не выполнена: вкладка «Фото» не найдена")
        return

    current_url = page.url
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except PlaywrightTimeoutError:
        pass

    if not page.locator(PHOTO_CONTAINER).count():
        status_callback("Не найден div.photo-stream на активной вкладке.")
        report_progress(0, 1, "Операция не выполнена: раздел «Фото» не найден")
        return

    profile_id = profile_id_from_url(current_url)
    if not profile_id:
        status_callback("Не удалось определить ID профиля из URL.")
        report_progress(0, 1, "Операция не выполнена: неизвестен ID профиля")
        return

    profile_url = profile_url_from_id(profile_id)
    about_url = f"{profile_url}/about"
    profile_name = get_profile_name(page)

    slug = safe_filename(profile_id.replace("/", "_")) or "ok_profile"
    OUT_DIR.mkdir(parents=True, exist_ok=True)

    # Удаляем точные/визуальные дубли, оставшиеся от прошлых запусков этого профиля.
    # Для удалённого дубля JSON сохраняется с служебными метками внутри info.
    old_duplicates = deduplicate_existing_profile_images(profile_url)
    if old_duplicates:
        print(f"[CLEANUP] Удалено старых дублей: {old_duplicates}")

    # 1. Собираем photo_id -> {preview, href}
    status_callback("Собираю список фотографий…")
    report_progress(None, None, "Сбор списка фотографий и прокрутка ленты…")
    entries = collect_photo_entries_with_scroll(page, status_callback)

    if not entries:
        status_callback("Не удалось собрать photo_id с .photo-card.")
        report_progress(0, 1, "Операция не выполнена: фотографии не найдены")
        return

    photo_ids = list(entries.keys())
    preview_map = {pid: absolute_url(e.get("preview", "")) for pid, e in entries.items()}
    status_callback(f"Найдено фотографий: {len(photo_ids)}")
    report_progress(0, max(1, len(photo_ids)), f"Подготовка списка фотографий: 0/{len(photo_ids)}")

    if not profile_name:
        profile_name = get_profile_name(page)
    if not profile_name:
        profile_name = profile_id

    # Обновляем сведения профиля ДО пропуска уже известных URL. Это важно:
    # существующие фотографии не скачиваются и не анализируются повторно,
    # однако изменившиеся name/info должны попасть в sidecar и FaceSearch.
    profile_info_retrieved = False
    info: dict[str, str] = {}
    about_page = None
    status_callback("Получаю актуальные метаданные профиля…")
    report_progress(None, None, "Получение актуальных метаданных профиля…")
    try:
        about_page = context.new_page()
        about_page.goto(about_url, wait_until="domcontentloaded", timeout=20000)
        extracted_info = extract_profile_info(about_page)
        if isinstance(extracted_info, dict) and extracted_info:
            info = extracted_info
            profile_info_retrieved = True
        print(f"[INFO] Извлечено полей: {len(info)}")
    except Exception as exc:
        print(f"[WARN] Не удалось получить /about ({about_url}): {exc}")
    finally:
        if about_page:
            try:
                about_page.close()
            except Exception:
                pass
    if not info:
        # Резерв для новых загрузок. При ошибке /about не используем это
        # значение для перезаписи уже известных метаданных в БД/JSON.
        info = {"Имя": profile_name or profile_id}

    profile_metadata_updated = False
    if importer and profile_info_retrieved:
        profile_metadata = build_metadata(
            photo_id="",
            url_source=profile_url,
            name=profile_name,
            url_image=next((u for u in preview_map.values() if u), ""),
            profile_info=extracted_info,
        )
        try:
            profile_metadata_updated = importer.update_profile_metadata(profile_metadata)
            report_progress(
                None, None,
                "Метаданные профиля FaceSearch обновлены" if profile_metadata_updated
                else "Метаданные профиля FaceSearch не изменились",
            )
        except Exception as exc:
            print(f"[DB-METADATA-ERROR] Не удалось обновить метаданные профиля: {exc}")
            status_callback(f"Не удалось обновить метаданные профиля FaceSearch: {exc}")

    metadata_sidecars_updated = 0

    def refresh_existing_sidecar(
        pid: str,
        image_path: Path | None,
        json_path: Path,
        old_meta: dict,
        fallback_url: str = "",
    ) -> bool:
        """Освежает локальный JSON для уже известного URL, не переимпортируя изображение."""
        nonlocal metadata_sidecars_updated
        if not profile_info_retrieved:
            return False
        if not isinstance(old_meta, dict) or not old_meta:
            if image_path is None or not image_path.is_file():
                return False
            old_meta = {}
        old_info = old_meta.get("info") if isinstance(old_meta.get("info"), dict) else {}
        image_url = str(fallback_url or old_meta.get("url_image") or preview_map.get(pid) or "").strip()
        if not image_url:
            return False
        resolution = _meta_get(old_meta, _META_RESOLUTION)
        if resolution not in _VALID_RESOLUTIONS:
            resolution = "preview" if normalized_url_key(image_url) == normalized_url_key(preview_map.get(pid, "")) else "dom"
        refreshed = build_metadata(
            photo_id=pid,
            url_source=profile_url,
            name=profile_name,
            url_image=image_url,
            profile_info=info,
            url_resolution=resolution,
            duplicate_of=str(old_info.get(_META_DUPLICATE_OF) or ""),
            duplicate_reason=str(old_info.get(_META_DUPLICATE_REASON) or ""),
        )
        if old_meta == refreshed:
            return False
        # Если sidecar отсутствовал, создаём его только при наличии самого файла.
        if not json_path.is_file() and (image_path is None or not image_path.is_file()):
            return False
        write_metadata(json_path, refreshed)
        metadata_sidecars_updated += 1
        print(f"[META-UPDATE] {json_path.name}: обновлены изменившиеся метаданные; изображение не анализировалось")
        return True

    # Имя зависит от photo_id — перемещение карточек в ленте не создаёт копий.
    def photo_stem(pid: str) -> Path:
        return OUT_DIR / f"{slug}_p{safe_filename(str(pid), max_len=70)}"

    completed: set[str] = set()
    db_skipped_ids: set[str] = set()
    pending_entries: dict[str, dict] = {}
    revalidate_ids: set[str] = set()
    cached_metadata: dict[str, dict] = {}
    urls_to_check = list(preview_map.values())
    local_paths: dict[str, tuple[Path | None, Path, dict]] = {}

    # Сначала читаем sidecar локальных файлов и пакетно проверяем URL в БД.
    # Это позволяет не открывать лайтбокс и не скачивать изображения, которые
    # уже импортированы в активный профиль FaceSearch.
    preflight_progress_step = max(1, len(photo_ids) // 100)
    for preflight_index, pid in enumerate(photo_ids, start=1):
        if (
            preflight_index == 1
            or preflight_index % preflight_progress_step == 0
            or preflight_index == len(photo_ids)
        ):
            report_progress(
                preflight_index,
                max(1, len(photo_ids)),
                f"Подготовка файлов и URL: {preflight_index}/{len(photo_ids)}",
            )
        stem = photo_stem(pid)
        image_path = existing_image_for_stem(stem)
        json_path = Path(str(stem) + ".json")
        old_meta = {}
        if json_path.is_file() and json_path.stat().st_size > 0:
            try:
                loaded = json.loads(json_path.read_text(encoding="utf-8"))
                if isinstance(loaded, dict):
                    old_meta = loaded
                    cached_metadata[pid] = loaded
                    if str(loaded.get("url_image") or "").strip():
                        urls_to_check.append(str(loaded.get("url_image")))
            except Exception:
                pass
        local_paths[pid] = (image_path, json_path, old_meta)

    db_existing_url_keys = importer.lookup_existing_urls(urls_to_check, profile_url=profile_url) if importer else set()
    preview_existing_ids = {
        pid for pid, value in preview_map.items()
        if normalized_url_key(value) and normalized_url_key(value) in db_existing_url_keys
    }

    imported_errors = 0
    report_progress(0, max(1, len(photo_ids)), f"Проверка существующих URL: 0/{len(photo_ids)}")
    url_check_progress_step = max(1, len(photo_ids) // 100)
    for check_index, (pid, entry) in enumerate(entries.items(), start=1):
        if (
            check_index == 1
            or check_index % url_check_progress_step == 0
            or check_index == len(photo_ids)
        ):
            report_progress(
                check_index,
                max(1, len(photo_ids)),
                f"Проверка существующих URL: {check_index}/{len(photo_ids)}",
            )
        image_path, json_path, old_meta = local_paths[pid]

        if pid in preview_existing_ids:
            old_url = str(old_meta.get("url_image") or "").strip()
            is_duplicate_marker = bool(_meta_get(old_meta, _META_DUPLICATE_OF))
            if importer and old_url and not is_duplicate_marker:
                # URL миниатюры совпал, но оригинальный URL мог измениться на CDN.
                # Разрешаем текущий URL фото, затем обновляем строки по photo_id.
                pending_entries[pid] = entry
                print(f"[DB-RECHECK-URL] {pid}: найден прежний CROP; перепроверяю текущий URL")
            else:
                refresh_existing_sidecar(pid, image_path, json_path, old_meta, preview_map.get(pid, ""))
                completed.add(pid)
                db_skipped_ids.add(pid)
                if importer:
                    importer.skipped_count += 1
                print(f"[DB-SKIP-PREVIEW] {pid}: URL уже есть в FaceSearch")
            continue

        if image_path and json_path.exists() and json_path.stat().st_size > 0:
            old_resolution = _meta_get(old_meta, _META_RESOLUTION)
            old_version = _meta_version(old_meta)
            if importer and set(old_meta.keys()) == set(_ALLOWED_TOP_LEVEL) and isinstance(old_meta.get("info"), dict):
                old_url_key = normalized_url_key(str(old_meta.get("url_image") or ""))
                if old_url_key and old_url_key in db_existing_url_keys:
                    pending_entries[pid] = entry
                    print(f"[DB-RECHECK-LOCAL] {pid}: CROP уже в FaceSearch; проверяю, изменился ли URL")
                    continue
                try:
                    import_status = importer.import_image(image_path, json_path, old_meta)
                    if delete_processed_files and import_result_allows_cleanup(import_status):
                        delete_processed_pair(image_path, json_path, enabled=True)
                    completed.add(pid)
                    if old_url_key:
                        db_existing_url_keys.add(old_url_key)
                    print(
                        f"[DB-IMPORT-LOCAL] {image_path.name}: "
                        f"лиц={import_status.get('faces', 0)}; {import_status.get('message', 'ok')}"
                    )
                except Exception as exc:
                    imported_errors += 1
                    completed.add(pid)
                    print(f"[DB-IMPORT-ERROR] {image_path.name}: {exc}")
                    status_callback(f"Ошибка импорта существующего файла {image_path.name}: {exc}")
                continue
            if old_resolution in _VALID_RESOLUTIONS and old_version >= RESOLUTION_VERSION:
                completed.add(pid)
                continue
            # Разово перепроверяем файлы от версии, которая могла брать URL
            # случайного сетевого ответа, а не именно открытой фотографии.
            revalidate_ids.add(pid)
            pending_entries[pid] = entry
            print(f"[REVALIDATE-LEGACY] {pid}: источник URL не подтверждён; проверяю заново")
            continue

        if image_path and not json_path.exists():
            revalidate_ids.add(pid)
            pending_entries[pid] = entry
            print(f"[REVALIDATE-ORPHAN] {pid}: файл без JSON, проверяю заново")
            continue

        # У старых duplicate_of-маркеров нет признака надёжного разрешения URL.
        # Их перепроверяем: иначе потерянные из-за неверной привязки фото не восстановить.
        if json_path.exists() and not image_path:
            try:
                old_meta = json.loads(json_path.read_text(encoding="utf-8"))
                if _meta_get(old_meta, _META_DUPLICATE_OF):
                    old_reason = _meta_get(old_meta, _META_DUPLICATE_REASON)
                    old_resolution = _meta_get(old_meta, _META_RESOLUTION)
                    old_version = _meta_version(old_meta)
                    if (old_reason in {"url", "image"}
                            and old_resolution in _VALID_RESOLUTIONS
                            and old_version >= RESOLUTION_VERSION):
                        completed.add(pid)
                        continue
                    revalidate_ids.add(pid)
                    pending_entries[pid] = entry
                    print(f"[RECHECK-OLD-DUPLICATE] {pid}: перепроверяю старый маркер")
                    continue
            except Exception:
                pass
        pending_entries[pid] = entry

    if completed:
        print(f"[CACHE] Уже обработано: {len(completed)}; осталось: {len(pending_entries)}")
    if not pending_entries:
        finish_successfully()
        status_callback(
            f"Готово. Найдено: {len(photo_ids)}; уже обработано: {len(completed)}; "
            f"пропущено по URL в FaceSearch: {len(db_skipped_ids)}; "
            f"новых изображений импортировано: {max(0, (importer.imported_count if importer else 0))}; "
            f"обновлено локальных JSON: {metadata_sidecars_updated}; "
            f"метаданные профиля: {'обновлены' if profile_metadata_updated else 'без изменений/не получены'}; "
            f"ошибок импорта: {imported_errors}. Папка: {OUT_DIR}"
        )
        return

    # Оригинал может быть недоступен прямым HTTP-запросом, хотя виден в браузере.
    # Поэтому при ошибке оригинала ниже автоматически пробуем URL миниатюры.
    if resolve_fullsize:
        status_callback(f"Получаю URL оригиналов: {len(pending_entries)} фотографий…")
        report_progress(None, None, f"Получение URL оригиналов: {len(pending_entries)} фотографий…")
        url_map = resolve_fullsize_urls(context, pending_entries, status_callback)
    else:
        url_map = {pid: preview_map.get(pid, "") for pid in pending_entries}
        for pending_entry in pending_entries.values():
            pending_entry["_url_resolution"] = "preview"

    if importer and pending_entries:
        for pid in list(pending_entries):
            image_path, json_path, old_meta = local_paths.get(pid, (None, Path(str(photo_stem(pid)) + ".json"), {}))
            if _meta_get(old_meta, _META_DUPLICATE_OF):
                # Shared duplicate sidecars do not own separate CROP rows.
                continue
            current_url = absolute_url(url_map.get(pid) or preview_map.get(pid) or "")
            if not current_url or is_non_photo_url(current_url):
                continue
            identity = "ok.ru:" + hashlib.sha256(f"{profile_url}\0{pid}".encode("utf-8")).hexdigest()
            old_url = str(old_meta.get("url_image") or "").strip()
            resolution = str(pending_entries[pid].get("_url_resolution") or "")
            # Preview — только резервная миниатюра, а не подтверждённый новый URL
            # оригинала. При смене URL не понижаем качество ссылки без подтверждения.
            sync_url = current_url
            if (
                resolution == "preview"
                and old_url
                and normalized_url_key(old_url) != normalized_url_key(current_url)
            ):
                sync_url = old_url
            try:
                synced_rows = importer.sync_existing_photo(
                    profile_url=profile_url,
                    source_key=identity,
                    old_url=old_url,
                    new_url=sync_url,
                )
            except Exception as exc:
                print(f"[DB-URL-SYNC-ERROR] {pid}: {exc}")
                synced_rows = 0
            if not synced_rows:
                continue

            # URL и identity уже обновлены в БД. Обновляем sidecar независимо от
            # доступности локального оригинала; при ошибке /about сохраняем старые info.
            old_info = old_meta.get("info") if isinstance(old_meta.get("info"), dict) else {}
            current_info = info if profile_info_retrieved else old_info
            current_name = profile_name if profile_info_retrieved else str(old_meta.get("name") or profile_name)
            if resolution not in _VALID_RESOLUTIONS:
                resolution = "preview" if normalized_url_key(current_url) == normalized_url_key(preview_map.get(pid, "")) else "dom"
            refreshed = build_metadata(
                photo_id=pid,
                url_source=profile_url,
                name=current_name,
                url_image=sync_url,
                profile_info=current_info,
                url_resolution=resolution,
            )
            sidecar_saved = False
            try:
                write_metadata(json_path, refreshed)
                metadata_sidecars_updated += 1
                sidecar_saved = True
            except Exception as exc:
                print(f"[WARN] Не удалось обновить JSON после синхронизации URL {json_path.name}: {exc}")
            if sidecar_saved and delete_processed_files:
                delete_processed_pair(image_path, json_path, enabled=True)
            completed.add(pid)
            pending_entries.pop(pid, None)
            db_skipped_ids.add(pid)
            importer.skipped_count += 1
            print(f"[DB-SKIP-IDENTITY] {pid}: URL обновлён; CROP/embeddings сохранены")

    if importer and pending_entries:
        resolved_candidates = []
        for pid in pending_entries:
            resolved_candidates.extend((url_map.get(pid, ""), preview_map.get(pid, "")))
        resolved_db_urls = importer.lookup_existing_urls(resolved_candidates, profile_url=profile_url)
        for pid in list(pending_entries):
            candidate_keys = {
                normalized_url_key(url_map.get(pid, "")),
                normalized_url_key(preview_map.get(pid, "")),
            } - {""}
            if candidate_keys & resolved_db_urls:
                image_path, json_path, old_meta = local_paths[pid]
                matched_url = next(
                    (candidate for candidate in (url_map.get(pid, ""), preview_map.get(pid, ""))
                     if normalized_url_key(candidate) in resolved_db_urls),
                    "",
                )
                refresh_existing_sidecar(pid, image_path, json_path, old_meta, matched_url)
                if not _meta_get(old_meta, _META_DUPLICATE_OF) and delete_processed_files:
                    delete_processed_pair(image_path, json_path, enabled=True)
                completed.add(pid)
                db_skipped_ids.add(pid)
                importer.skipped_count += 1
                pending_entries.pop(pid, None)
                print(f"[DB-SKIP-ORIGINAL] {pid}: URL уже есть в FaceSearch; скачивание и анализ пропущены")

    # Если часть элементов убрана как уже присутствующая в БД, не нужно
    # перемещать её локальные файлы в резервную папку при перепроверке.
    revalidate_ids.intersection_update(pending_entries)

    if not pending_entries:
        finish_successfully()
        status_callback(
            f"Готово. Найдено: {len(photo_ids)}; скачивание/анализ пропущены для {len(db_skipped_ids)} "
            f"фото, URL которых уже есть в FaceSearch; обновлено локальных JSON: {metadata_sidecars_updated}; "
            f"метаданные профиля: {'обновлены' if profile_metadata_updated else 'без изменений/не получены'}; "
            f"ошибок импорта: {imported_errors}."
        )
        return

    # Актуальные метаданные профиля получены до preflight-проверки URL.

    downloaded = 0
    reused = len(completed) - len(db_skipped_ids)
    duplicates = 0
    errors = 0
    total = len(photo_ids)
    referer = page.url or "https://ok.ru/"

    # Старые изображения временно убираем из каталога сравнения, но сохраняем
    # резервную копию до успешного получения новой версии.
    backup_dir = OUT_DIR / ".recheck_backup"
    backups: dict[str, tuple[Path, Path]] = {}
    if revalidate_ids:
        backup_dir.mkdir(parents=True, exist_ok=True)
        for pid in revalidate_ids:
            old_image = existing_image_for_stem(photo_stem(pid))
            if not old_image:
                continue
            backup = backup_dir / old_image.name
            if backup.exists():
                backup = backup_dir / f"{old_image.stem}_{time.time_ns()}{old_image.suffix}"
            try:
                old_image.replace(backup)
                backups[pid] = (backup, old_image)
                print(f"[BACKUP-RECHECK] {old_image.name}")
            except OSError as exc:
                print(f"[WARN] Не удалось сохранить резервную копию {old_image.name}: {exc}")

    known_urls, known_sha, known_visual = profile_image_catalog(profile_url)
    legacy_by_url = existing_images_by_url(profile_url, stable_prefix=f"{slug}_p")

    def discard_backup(pid: str) -> None:
        item = backups.pop(pid, None)
        if item:
            backup, _original = item
            try:
                backup.unlink(missing_ok=True)
            except OSError:
                pass

    def restore_backup(pid: str) -> None:
        item = backups.pop(pid, None)
        if not item:
            return
        backup, original = item
        if not backup.exists():
            return
        try:
            if not original.exists():
                backup.replace(original)
            else:
                backup.unlink(missing_ok=True)
        except OSError as exc:
            print(f"[WARN] Не удалось восстановить резервный файл {backup.name}: {exc}")

    def save_duplicate_marker(json_path: Path, pid: str, url: str, duplicate_path: Path,
                              reason: str, preview_url: str, resolution: str = "") -> None:
        final_resolution = resolution or (
            "preview" if normalized_url_key(url) == normalized_url_key(preview_url) else "dom"
        )
        metadata = build_metadata(
            photo_id=pid,
            url_source=profile_url,
            name=profile_name,
            url_image=url,
            profile_info=info if isinstance(info, dict) else {},
            url_resolution=final_resolution,
            duplicate_of=duplicate_path.name,
            duplicate_reason=reason,
        )
        write_metadata(json_path, metadata)

    report_progress(0, max(1, total), f"Скачивание и импорт: 0/{total}")
    image_progress_step = max(1, total // 100)
    for index, pid in enumerate(photo_ids, start=1):
        if index == 1 or index % image_progress_step == 0 or index == total:
            report_progress(
                index,
                max(1, total),
                f"Скачивание и импорт: {index}/{total} — {profile_name}",
            )
        if pid in completed:
            continue

        stem = photo_stem(pid)
        json_path = Path(str(stem) + ".json")
        primary_url = absolute_url(url_map.get(pid) or "")
        preview_url = absolute_url(preview_map.get(pid) or "")
        if is_non_photo_url(primary_url):
            primary_url = ""
        if is_non_photo_url(preview_url):
            preview_url = ""
        if not primary_url and not preview_url:
            errors += 1
            restore_backup(pid)
            print(f"[SKIP] Не найден URL для photo_id={pid}")
            continue

        current_url = primary_url or preview_url
        url_key = normalized_url_key(current_url)
        status_callback(f"Скачивание {index}/{total}: {profile_name}")

        image_path = existing_image_for_stem(stem)
        was_existing = image_path is not None

        # Однократная миграция старого файла по URL. Это сохраняет старый файл,
        # вместо повторного скачивания после обновления скрипта.
        if image_path is None:
            legacy_pair = legacy_by_url.get(url_key)
            if legacy_pair:
                old_image, old_json = legacy_pair
                old_is_current_format = old_image.stem.startswith(f"{slug}_p")
                if old_image.exists() and old_json.exists() and not old_is_current_format:
                    try:
                        target_image = Path(str(stem) + old_image.suffix.lower())
                        if not target_image.exists() and not json_path.exists():
                            old_image.replace(target_image)
                            old_json.unlink(missing_ok=True)
                            image_path = target_image
                            was_existing = True
                            print(f"[MIGRATE] {old_image.name} -> {target_image.name}")
                    except OSError as exc:
                        print(f"[WARN] Не удалось перенести старый файл {old_image.name}: {exc}")

        # Одинаковый URL уже сохранён в этом профиле: не скачиваем его второй раз.
        # Если URL неизвестен ранее, дубликат будет проверен по содержимому после скачивания.
        if (image_path is None and url_key and url_key in known_urls
                and (not preview_url or url_key == normalized_url_key(preview_url))):
            duplicate_path = known_urls[url_key]
            save_duplicate_marker(json_path, pid, current_url, duplicate_path, "url", preview_url, pending_entries.get(pid, {}).get("_url_resolution", ""))
            duplicates += 1
            discard_backup(pid)
            print(f"[DUPLICATE-URL] {pid} -> {duplicate_path.name}")
            continue

        used_url = current_url
        if image_path is None:
            # Сначала оригинал, затем preview. Это исправляет случай, когда в лайтбоксе
            # изображение видно, но CDN не отдаёт тот же URL через APIRequestContext.
            candidates = []
            for candidate in (primary_url, preview_url):
                key = normalized_url_key(candidate)
                if candidate and not is_non_photo_url(candidate) and all(normalized_url_key(prev) != key for prev in candidates):
                    candidates.append(candidate)
            for candidate in candidates:
                candidate_key = normalized_url_key(candidate)
                if candidate_key and candidate_key in known_urls:
                    # URL совпадает с уже сохранённым; если другой кандидат (preview)
                    # уникален, пробуем и его, вместо преждевременного пропуска фото.
                    used_url = candidate
                    continue
                saved = download_one(context, candidate, stem, referer=referer)
                if saved is not None:
                    image_path = saved
                    used_url = candidate
                    break
                print(f"[RETRY-PREVIEW] Не удалось загрузить {candidate[:100]}")

        if image_path is None:
            # Если ни оригинал, ни миниатюра не скачались, оставляем без JSON,
            # чтобы этот photo_id автоматически повторился при следующем запуске.
            if used_url and normalized_url_key(used_url) in known_urls:
                duplicate_path = known_urls[normalized_url_key(used_url)]
                save_duplicate_marker(json_path, pid, used_url, duplicate_path, "url", preview_url, pending_entries.get(pid, {}).get("_url_resolution", ""))
                duplicates += 1
                discard_backup(pid)
                continue
            errors += 1
            restore_backup(pid)
            continue

        is_new_download = not was_existing
        if is_new_download:
            try:
                sha, visual = image_fingerprints(image_path)
            except Exception as exc:
                print(f"[WARN] Не удалось проверить изображение {image_path.name}: {exc}")
                sha, visual = "", None

            duplicate_path = known_sha.get(sha) if sha else None
            if duplicate_path is None and visual:
                duplicate_path = known_visual.get(visual)

            if duplicate_path is not None and duplicate_path.exists() and duplicate_path.resolve() != image_path.resolve():
                try:
                    image_path.unlink(missing_ok=True)
                except OSError as exc:
                    print(f"[WARN] Не удалось удалить только что полученный дубль {image_path.name}: {exc}")
                save_duplicate_marker(json_path, pid, used_url, duplicate_path, "image", preview_url, pending_entries.get(pid, {}).get("_url_resolution", ""))
                duplicates += 1
                discard_backup(pid)
                print(f"[DUPLICATE-IMAGE] {pid} совпадает с {duplicate_path.name}; отдельный файл не оставлен")
                if url_key:
                    known_urls.setdefault(url_key, duplicate_path)
                continue

            if sha:
                known_sha.setdefault(sha, image_path)
            if visual:
                known_visual.setdefault(visual, image_path)
            if normalized_url_key(used_url):
                known_urls.setdefault(normalized_url_key(used_url), image_path)
            downloaded += 1
        else:
            reused += 1
            try:
                sha, visual = image_fingerprints(image_path)
                if sha:
                    known_sha.setdefault(sha, image_path)
                if visual:
                    known_visual.setdefault(visual, image_path)
                known_urls[normalized_url_key(used_url)] = image_path
            except Exception:
                pass

        metadata = build_metadata(
            photo_id=pid,
            url_source=profile_url,
            name=profile_name,
            url_image=used_url,
            profile_info=info if isinstance(info, dict) else {},
            url_resolution=pending_entries.get(pid, {}).get("_url_resolution") or (
                "preview" if normalized_url_key(used_url) == normalized_url_key(preview_url) else "dom"
            ),
        )
        try:
            write_metadata(json_path, metadata)
            discard_backup(pid)
        except Exception as exc:
            print(f"[ERROR] JSON {json_path.name}: {exc}")
            errors += 1
            restore_backup(pid)
            continue

        if importer:
            try:
                identity = "ok.ru:" + hashlib.sha256(f"{profile_url}\0{pid}".encode("utf-8")).hexdigest()
                result = importer.import_image(image_path, json_path, metadata, source_key=identity)
                key = normalized_url_key(used_url)
                if key:
                    db_existing_url_keys.add(key)
                status_callback(
                    f"FaceSearch: {image_path.name} добавлено; лиц: {result.get('faces', 0)} "
                    f"({result.get('message', 'ok')})"
                )
                print(
                    f"[DB-IMPORT] {image_path.name}: лица={result.get('faces', 0)}; "
                    f"обновлено={result.get('updated', 0)}; добавлено={result.get('added', 0)}"
                )
                if delete_processed_files and import_result_allows_cleanup(result):
                    delete_processed_pair(image_path, json_path, enabled=True)
            except Exception as exc:
                errors += 1
                imported_errors += 1
                print(f"[DB-IMPORT-ERROR] {image_path.name}: {exc}")
                status_callback(f"Изображение сохранено, но импорт в FaceSearch не выполнен: {exc}")

    # Если часть записей не удалось обработать, не уничтожаем оставшиеся резервные копии.
    # Они лежат в .recheck_backup и могут быть восстановлены вручную.
    finish_successfully()
    db_summary = (
        f"; FaceSearch: профиль «{importer.config['active_name']}», "
        f"новых изображений импортировано: {importer.imported_count}, "
        f"пропущено по URL: {len(db_skipped_ids)}, "
        f"обновлено локальных JSON: {metadata_sidecars_updated}, "
        f"метаданные профиля: {'обновлены' if profile_metadata_updated else 'без изменений/не получены'}, "
        f"ошибок импорта: {imported_errors}"
        if importer else ""
    )
    status_callback(
        f"Готово. Найдено: {total}; скачано: {downloaded}; уже имелось: {reused}; "
        f"дубли пропущены: {duplicates}; ошибок: {errors}{db_summary}. Папка: {OUT_DIR}"
    )


# ============================ GUI ============================

def main():
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    commands: queue.Queue[tuple] = queue.Queue()
    browser_ready = threading.Event()
    browser_state = {"error": None}

    def browser_worker():
        try:
            with sync_playwright() as playwright:
                context = None
                try:
                    context = launch_browser_context(
                        playwright, browser_profile_dir_var.get(), chrome_exe_path_var.get()
                    )
                    if not context.pages:
                        context.new_page().goto(START_URL)
                    elif all("ok.ru" not in p.url for p in context.pages):
                        context.pages[0].goto(START_URL)
                except Exception as exc:
                    browser_state["error"] = str(exc)
                    set_status(f"Ошибка запуска Chrome: {exc}")
                finally:
                    browser_ready.set()

                while True:
                    command = commands.get()
                    if command[0] == "quit":
                        break
                    if command[0] == "restart_browser":
                        browser_ready.clear()
                        try:
                            if context is not None:
                                try:
                                    context.close()
                                except Exception:
                                    pass
                            context = None
                            context = launch_browser_context(playwright, command[1], command[2])
                            if not context.pages:
                                context.new_page().goto(START_URL)
                            elif all("ok.ru" not in p.url for p in context.pages):
                                context.pages[0].goto(START_URL)
                            browser_state["error"] = None
                            set_status("Chrome запущен с выбранными путями. Откройте профиль OK.ru → Фото.")
                            set_progress(0, 1, "Браузер готов")
                        except Exception as exc:
                            context = None
                            browser_state["error"] = str(exc)
                            set_status(f"Ошибка перезапуска Chrome: {exc}")
                            set_progress(0, 1, f"Ошибка перезапуска Chrome: {exc}")
                        finally:
                            browser_ready.set()
                        continue
                    if command[0] == "collect":
                        if context is None:
                            set_status("Chrome не запущен. Проверьте путь к chrome.exe и нажмите «Применить пути».")
                            continue
                        resolve = command[1]
                        settings_xml = command[2]
                        profiles_json = command[3]
                        auto_import = command[4]
                        create_crop_files = command[5]
                        build_index_after = command[6]
                        delete_processed_files = command[7]
                        importer = None
                        try:
                            if auto_import:
                                importer = FaceSearchImporter(
                                    settings_xml,
                                    profiles_json,
                                    set_status,
                                    create_crop_files=create_crop_files,
                                )
                                set_status(
                                    f"FaceSearch: активный профиль «{importer.config['active_name']}». "
                                    "Открыта вкладка OK.ru → Фото — можно начинать."
                                )
                            collect_photos(
                                context,
                                set_status,
                                resolve_fullsize=resolve,
                                importer=importer,
                                build_index_after=build_index_after,
                                progress_callback=set_progress,
                                delete_processed_files=delete_processed_files,
                            )
                        except Exception as exc:
                            print(f"[ERROR] {exc}")
                            set_status(f"Ошибка: {exc}")
                            set_progress(0, 1, f"Ошибка: {exc}")
                        finally:
                            if importer is not None:
                                try:
                                    importer.close()
                                except Exception as exc:
                                    print(f"[WARN] Ошибка закрытия БД FaceSearch: {exc}")
                context.close()
        except Exception as exc:
            browser_state["error"] = str(exc)
            browser_ready.set()

    root = tk.Tk()
    root.title("OK.ru — загрузчик фотографий и импорт FaceSearch")
    root.geometry("920x650")
    root.minsize(820, 610)
    root.resizable(True, True)

    frame = ttk.Frame(root, padding=12)
    frame.pack(fill="both", expand=True)
    frame.columnconfigure(0, weight=1)

    ttk.Label(
        frame,
        text="Откройте профиль OK.ru → Фото. Новые фотографии будут сохранены и импортированы в активный профиль FaceSearch.",
        wraplength=800,
    ).grid(row=0, column=0, sticky="w", pady=(0, 8))

    paths_frame = ttk.LabelFrame(frame, text="Настройки FaceSearch и браузера", padding=8)
    paths_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    paths_frame.columnconfigure(1, weight=1)

    initial_paths = load_gui_paths()
    facesearch_dir_var = tk.StringVar(value=initial_paths["facesearch_dir"])
    auto_import_var = tk.BooleanVar(value=bool(initial_paths.get("auto_import", True)))
    create_crop_files_var = tk.BooleanVar(value=bool(initial_paths.get("create_crop_files", True)))
    build_index_after_var = tk.BooleanVar(value=bool(initial_paths.get("build_index_after", False)))
    browser_profile_dir_var = tk.StringVar(value=initial_paths["browser_profile_dir"])
    chrome_exe_path_var = tk.StringVar(value=initial_paths["chrome_exe_path"])
    delete_processed_files_var = tk.BooleanVar(value=bool(initial_paths.get("delete_processed_files", False)))

    ttk.Label(paths_frame, text="Папка FaceSearch:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(paths_frame, textvariable=facesearch_dir_var).grid(row=0, column=1, sticky="ew", pady=3)
    ttk.Button(
        paths_frame,
        text="Выбрать папку…",
        command=lambda: choose_config_directory(facesearch_dir_var, root),
    ).grid(row=0, column=2, padx=(8, 0), pady=3)

    ttk.Label(
        paths_frame,
        text="Будут использованы файлы settings.xml и database_profiles.json внутри выбранной папки.",
        wraplength=790,
    ).grid(row=1, column=0, columnspan=3, sticky="w", pady=(3, 0))

    config_status_var = tk.StringVar(value="Конфигурация FaceSearch ещё не проверена.")
    ttk.Label(paths_frame, textvariable=config_status_var, wraplength=790).grid(
        row=2, column=0, columnspan=3, sticky="w", pady=(4, 0)
    )

    def persist_settings():
        try:
            save_gui_paths(
                facesearch_dir_var.get(),
                auto_import_var.get(),
                create_crop_files_var.get(),
                build_index_after_var.get(),
                browser_profile_dir_var.get(),
                chrome_exe_path_var.get(),
                delete_processed_files_var.get(),
            )
        except Exception as exc:
            print(f"[WARN] Не удалось сохранить настройки формы: {exc}")

    def choose_config_directory(variable, parent):
        current = variable.get().strip()
        initial_dir = current if current and Path(current).expanduser().is_dir() else str(DEFAULT_FACESEARCH_DIR)
        selected = filedialog.askdirectory(
            parent=parent,
            title="Выберите каталог установленного FaceSearch",
            initialdir=initial_dir,
            mustexist=True,
        )
        if selected:
            variable.set(selected)
            persist_settings()
            config_status_var.set("Путь изменён. Нажмите «Проверить настройки», чтобы прочитать профиль.")

    def choose_browser_profile_directory(variable, parent):
        current = variable.get().strip()
        initial_dir = current if current and Path(current).expanduser().is_dir() else str(Path(__file__).resolve().parent)
        selected = filedialog.askdirectory(
            parent=parent,
            title="Выберите каталог постоянного профиля Chrome",
            initialdir=initial_dir,
            mustexist=False,
        )
        if selected:
            variable.set(selected)
            persist_settings()

    def choose_chrome_executable(variable, parent):
        current = variable.get().strip()
        initial_dir = str(Path(current).expanduser().parent) if current and Path(current).expanduser().parent.is_dir() else str(Path.home())
        selected = filedialog.askopenfilename(
            parent=parent,
            title="Выберите исполняемый файл Google Chrome",
            initialdir=initial_dir,
            filetypes=[("Google Chrome", "chrome.exe"), ("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")],
        )
        if selected:
            variable.set(selected)
            persist_settings()

    def apply_browser_paths():
        profile_text = browser_profile_dir_var.get().strip()
        chrome_text = chrome_exe_path_var.get().strip()
        if not profile_text:
            messagebox.showerror("Путь профиля Chrome", "Укажите каталог профиля браузера.", parent=root)
            return
        if not chrome_text or not Path(chrome_text).expanduser().is_file():
            messagebox.showerror(
                "Путь к Chrome",
                f"Файл chrome.exe не найден:\n{chrome_text or '(путь не указан)'}",
                parent=root,
            )
            return
        persist_settings()
        browser_ready.clear()
        browser_state["error"] = None
        commands.put(("restart_browser", profile_text, chrome_text))
        set_status("Применяю настройки и перезапускаю Chrome…")
        set_progress(None, None, "Перезапуск Chrome с выбранным профилем…")

    def check_configuration(show_dialog: bool = True):
        try:
            settings_xml, profiles_json = config_paths_from_directory(facesearch_dir_var.get())
            config = load_face_search_config(settings_xml, profiles_json)
            profile = config["profile"]
            db_type = str(profile.get("type") or "sqlite")
            db_target = str(profile.get("database") or profile.get("path") or "")
            host = str(profile.get("host") or "").strip()
            backend_text = "CPU" if config["backend"] in {"cpu", "processor", "процессор"} else "GPU/автовыбор"
            location = f"{db_type}: {db_target}" if db_target else db_type
            if host and db_type != "sqlite":
                location += f" @ {host}"
            config_status_var.set(
                f"Активный профиль: {config['active_name']} — {location}; модель: {backend_text}. "
                f"Корень FaceSearch: {config['root']}"
            )
            persist_settings()
            return config
        except Exception as exc:
            config_status_var.set(f"Ошибка конфигурации: {exc}")
            if show_dialog:
                messagebox.showerror("Настройки FaceSearch", str(exc), parent=root)
            return None

    ttk.Label(paths_frame, text="Папка профиля Chrome:").grid(row=3, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(paths_frame, textvariable=browser_profile_dir_var).grid(row=3, column=1, sticky="ew", pady=3)
    ttk.Button(
        paths_frame,
        text="Выбрать папку…",
        command=lambda: choose_browser_profile_directory(browser_profile_dir_var, root),
    ).grid(row=3, column=2, padx=(8, 0), pady=3)

    ttk.Label(paths_frame, text="Путь к chrome.exe:").grid(row=4, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(paths_frame, textvariable=chrome_exe_path_var).grid(row=4, column=1, sticky="ew", pady=3)
    ttk.Button(
        paths_frame,
        text="Выбрать файл…",
        command=lambda: choose_chrome_executable(chrome_exe_path_var, root),
    ).grid(row=4, column=2, padx=(8, 0), pady=3)

    ttk.Button(
        paths_frame,
        text="Проверить настройки FaceSearch",
        command=lambda: check_configuration(True),
    ).grid(row=5, column=0, columnspan=3, sticky="ew", pady=(6, 0))

    ttk.Button(
        paths_frame,
        text="Применить пути и перезапустить Chrome",
        command=apply_browser_paths,
    ).grid(row=6, column=0, columnspan=3, sticky="ew", pady=(4, 0))

    auto_import_check = ttk.Checkbutton(
        frame,
        text="Синхронизировать с FaceSearch: новые для БД изображения импортировать, для известных URL обновлять только метаданные",
        variable=auto_import_var,
        command=persist_settings,
    )
    auto_import_check.grid(row=2, column=0, sticky="w", pady=(0, 6))

    fullsize_var = tk.BooleanVar(value=True)
    ttk.Checkbutton(
        frame,
        text="Получать оригиналы фотографий (медленнее; без галочки — использовать миниатюры)",
        variable=fullsize_var,
    ).grid(row=3, column=0, sticky="w", pady=(0, 5))

    options_frame = ttk.LabelFrame(frame, text="После загрузки", padding=(8, 5))
    options_frame.grid(row=4, column=0, sticky="ew", pady=(0, 6))
    create_crop_check = ttk.Checkbutton(
        options_frame,
        text="Создавать CROP-файлы при импорте",
        variable=create_crop_files_var,
        command=persist_settings,
    )
    create_crop_check.pack(anchor="w", pady=1)
    build_index_check = ttk.Checkbutton(
        options_frame,
        text="Построить индекс FAISS по завершении импорта",
        variable=build_index_after_var,
        command=persist_settings,
    )
    build_index_check.pack(anchor="w", pady=1)
    delete_processed_check = ttk.Checkbutton(
        options_frame,
        text="Удалять изображения и JSON после успешной обработки в FaceSearch",
        variable=delete_processed_files_var,
        command=persist_settings,
    )
    delete_processed_check.pack(anchor="w", pady=1)

    status_var = tk.StringVar(value="Запуск браузера…")
    status_label = ttk.Label(frame, textvariable=status_var, wraplength=800)
    status_label.grid(row=5, column=0, sticky="ew", pady=(0, 4))

    progress_stage_var = tk.StringVar(value="Ожидание задания")
    ttk.Label(frame, textvariable=progress_stage_var, wraplength=800).grid(
        row=6, column=0, sticky="ew", pady=(0, 3)
    )
    progress_bar = ttk.Progressbar(frame, mode="determinate", maximum=100, value=0)
    progress_bar.grid(row=7, column=0, sticky="ew", pady=(0, 8))
    progress_state = {"mode": "determinate"}

    def set_status(message: str):
        try:
            root.after(0, lambda value=message: status_var.set(value))
        except tk.TclError:
            pass

    def set_progress(current=None, total=None, message=""):
        """Обновляет индикатор из worker-потока безопасно для Tkinter."""
        def apply_progress():
            try:
                if current is None or total is None:
                    if progress_state["mode"] != "indeterminate":
                        progress_bar.stop()
                        progress_bar.configure(mode="indeterminate", maximum=100)
                        progress_bar.start(12)
                        progress_state["mode"] = "indeterminate"
                    progress_stage_var.set(message or "Выполняется операция…")
                else:
                    safe_total = max(1, int(total))
                    safe_current = min(safe_total, max(0, int(current)))
                    if progress_state["mode"] != "determinate":
                        progress_bar.stop()
                        progress_bar.configure(mode="determinate", maximum=safe_total)
                        progress_state["mode"] = "determinate"
                    elif int(float(progress_bar.cget("maximum") or 1)) != safe_total:
                        progress_bar.configure(maximum=safe_total)
                    progress_bar["value"] = safe_current
                    percent = int(safe_current * 100 / safe_total)
                    prefix = f"{percent}% — " if safe_total > 1 else ""
                    progress_stage_var.set(prefix + (message or "Обработка"))
            except tk.TclError:
                pass
        try:
            root.after(0, apply_progress)
        except tk.TclError:
            pass

    def update_import_options_state(*_args):
        state = "normal" if auto_import_var.get() else "disabled"
        create_crop_check.configure(state=state)
        build_index_check.configure(state=state)
        delete_processed_check.configure(state=state)
        persist_settings()

    auto_import_var.trace_add("write", update_import_options_state)
    update_import_options_state()

    def start_collect():
        if not browser_ready.is_set():
            status_var.set("Браузер ещё запускается…")
            return
        if browser_state["error"]:
            status_var.set("Ошибка запуска браузера: " + browser_state["error"])
            return
        if auto_import_var.get():
            config = check_configuration(show_dialog=True)
            if config is None:
                return
        persist_settings()
        commands.put((
            "collect",
            fullsize_var.get(),
            str(config_paths_from_directory(facesearch_dir_var.get())[0]),
            str(config_paths_from_directory(facesearch_dir_var.get())[1]),
            auto_import_var.get(),
            create_crop_files_var.get(),
            build_index_after_var.get(),
            delete_processed_files_var.get(),
        ))
        set_progress(None, None, "Задание отправлено; выполняется подготовка…")
        status_var.set("Задание отправлено…")

    ttk.Button(
        frame,
        text="Получить изображения и импортировать в FaceSearch",
        command=start_collect,
    ).grid(row=8, column=0, sticky="ew", ipady=6)

    ttk.Label(
        frame,
        text=f"Папка загрузки: {OUT_DIR}",
        wraplength=800,
    ).grid(row=9, column=0, sticky="w", pady=(8, 0))

    worker = threading.Thread(target=browser_worker, daemon=True)
    worker.start()

    # Без подключения к БД: быстрый разбор двух файлов сразу при открытии окна.
    if auto_import_var.get():
        check_configuration(show_dialog=False)

    def on_close():
        commands.put(("quit",))
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    root.mainloop()

if __name__ == "__main__":
    main()