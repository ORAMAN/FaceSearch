from __future__ import annotations

"""Instagram image downloader for FaceSearch.

Uses a persistent Chrome profile (Playwright) and the rendered page DOM. It supports
profile posts and the /tagged/ tab without calling Instagram's private JSON endpoints.
"""

import hashlib
import importlib
import json
import os
import queue
import re
import sys
import threading
import time
from datetime import datetime, timezone
from email.utils import parsedate_to_datetime
import xml.etree.ElementTree as ET
from pathlib import Path
from urllib.parse import urljoin, urlsplit, urlunsplit, unquote

import tkinter as tk
from tkinter import ttk, filedialog, messagebox

try:
    from playwright.sync_api import sync_playwright, TimeoutError as PlaywrightTimeoutError
except ImportError:
    sync_playwright = None
    PlaywrightTimeoutError = TimeoutError


BASE_URL = "https://www.instagram.com/"
OUT_DIR = Path(__file__).resolve().parent / "Instagram_Download"
PROFILE_DIR = Path(__file__).resolve().parent / "Instagram_Browser_Profile"
APP_SETTINGS_PATH = Path(__file__).resolve().with_name("instagram_downloader_settings.json")
DEFAULT_FACESEARCH_DIR = Path(r"C:\FaceSearch")
IMAGE_EXTENSIONS = {".jpg", ".jpeg", ".png", ".webp", ".gif", ".bmp", ".avif"}


def default_chrome_executable() -> str:
    candidates = [
        Path(os.environ.get("PROGRAMFILES") or "C:/Program Files") / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("PROGRAMFILES(X86)") or "C:/Program Files (x86)") / "Google" / "Chrome" / "Application" / "chrome.exe",
        Path(os.environ.get("LOCALAPPDATA") or (Path.home() / "AppData" / "Local")) / "Google" / "Chrome" / "Application" / "chrome.exe",
    ]
    for candidate in candidates:
        if candidate.is_file():
            return str(candidate)
    return str(candidates[0])


def safe_filename(value: str, max_len: int = 100) -> str:
    value = unquote(str(value or "")).strip()
    value = re.sub(r'[<>:"/\\|?*\x00-\x1f]', "_", value)
    value = re.sub(r"\s+", " ", value).strip(" ._")
    return value[:max_len] or "instagram_image"


def absolute_url(value: str, base: str = BASE_URL) -> str:
    value = str(value or "").strip()
    if not value or value.startswith(("data:", "blob:", "javascript:")):
        return ""
    return urljoin(base, value)


def normalize_instagram_url(value: str) -> tuple[str, str, str]:
    """Return (requested page URL, profile URL, mode) after validating supported paths."""
    value = str(value or "").strip()
    if not value:
        raise ValueError("Откройте в Chrome профиль Instagram или его вкладку /tagged/.")
    if not re.match(r"^https?://", value, re.I):
        value = "https://www.instagram.com/" + value.lstrip("/")
    parts = urlsplit(value)
    host = parts.netloc.lower().split(":", 1)[0]
    if host not in {"instagram.com", "www.instagram.com"}:
        raise ValueError("Поддерживаются только ссылки www.instagram.com/имя_пользователя и /имя_пользователя/tagged/.")
    path_parts = [unquote(p) for p in parts.path.split("/") if p]
    if not path_parts:
        raise ValueError("Укажите имя пользователя Instagram в ссылке.")
    username = path_parts[0]
    if username.lower() in {"accounts", "explore", "direct", "reels", "stories", "p", "reel", "tv", "about", "developer"}:
        raise ValueError("Нужна ссылка на профиль, а не служебную страницу Instagram.")
    if len(path_parts) == 1:
        mode = "posts"
    elif len(path_parts) == 2 and path_parts[1].lower() == "tagged":
        mode = "tagged"
    else:
        raise ValueError("Допустимы только страницы профиля и /имя_пользователя/tagged/.")
    profile_url = f"https://www.instagram.com/{username}/"
    requested_url = profile_url if mode == "posts" else profile_url + "tagged/"
    return requested_url, profile_url, mode


def normalize_instagram_page(page) -> tuple[str, str, str]:
    """Resolve a profile page from its current URL, then canonical/og:url metadata.

    Instagram occasionally uses transient client-side routes while the profile DOM
    is already present. As with the OK downloader's DOM-first tab lookup, inspect
    the page itself before rejecting it solely because `page.url` is transient.
    """
    current_url = _open_page_url(page)
    try:
        return normalize_instagram_url(current_url)
    except ValueError as direct_error:
        try:
            host = urlsplit(current_url).netloc.lower().split(":", 1)[0]
        except Exception:
            host = ""
        if host not in {"instagram.com", "www.instagram.com"}:
            raise direct_error
        try:
            page_metadata = page.evaluate("""() => ({
                canonical: document.querySelector('link[rel=canonical]')?.href || '',
                ogUrl: document.querySelector('meta[property=\"og:url\"]')?.content || '',
                location: window.location.href || ''
            })""") or {}
        except Exception:
            page_metadata = {}
        alternatives = []
        for key in ("canonical", "ogUrl", "location"):
            value = str(page_metadata.get(key) or "").strip()
            if value and value != current_url and value not in alternatives:
                alternatives.append(value)
        for candidate in alternatives:
            try:
                return normalize_instagram_url(candidate)
            except ValueError:
                continue
        raise direct_error


def _open_page_url(page) -> str:
    """Safely returns the current URL for a Playwright page."""
    try:
        return str(page.url or "").strip()
    except Exception:
        return ""


def _tab_debug_description(page, index: int) -> str:
    url = _open_page_url(page) or "<URL недоступен>"
    try:
        title = str(page.title() or "").strip()
    except Exception:
        title = ""
    if title:
        return f"{index}. {title[:70]} — {url[:180]}"
    return f"{index}. {url[:220]}"


def open_instagram_home_tab(context):
    """Open instagram.com in the controlled Chrome context without clobbering profile tabs.

    If the persistent profile already has the Instagram home tab, bring it to front.
    Otherwise reuse an about:blank tab, or create a new one so any restored profile
    tabs remain open. `wait_until='commit'` avoids waiting for Instagram's long-running
    client navigation and avoids the previous DOMContentLoaded interruption race.
    """
    pages = [page for page in context.pages if not page.is_closed()]
    for page in reversed(pages):
        current = _open_page_url(page)
        try:
            parsed = urlsplit(current)
        except Exception:
            continue
        host = parsed.netloc.lower().split(":", 1)[0]
        if host in {"instagram.com", "www.instagram.com"} and parsed.path.rstrip("/") == "":
            try:
                page.bring_to_front()
            except Exception:
                pass
            return page

    blank_page = next((p for p in reversed(pages) if _open_page_url(p) in {"", "about:blank"}), None)
    page = blank_page or context.new_page()
    navigation_error = None
    try:
        page.goto(BASE_URL, wait_until="commit", timeout=30000)
    except Exception as exc:
        # Meta login redirects may interrupt the navigation promise although the
        # browser has already left about:blank. Keep that page available to the user.
        navigation_error = exc
    current = _open_page_url(page)
    if not current or current == "about:blank":
        raise RuntimeError(f"Не удалось открыть {BASE_URL}: {navigation_error or 'страница осталась пустой'}")
    try:
        page.bring_to_front()
    except Exception:
        pass
    if navigation_error:
        print(f"[WARN] Навигация к Instagram была прервана перенаправлением: {navigation_error}; текущая страница: {current}")
    return page


def _has_instagram_profile_grid(page) -> bool:
    """DOM-first profile test analogous to OK downloader's photo-container check.

    Do not rely on Instagram's volatile CSS class names. In particular, keep the
    selector strings valid JavaScript (the previous nested single quotes made the
    querySelector expression throw and silently returned False for every page).
    """
    try:
        return bool(page.evaluate(r"""() => {
            const root = document.querySelector('main') || document;
            const hasPostLink = [...root.querySelectorAll('a[href]')].some(a => {
                try { return /\/(p|reel|tv)\/[^/]+(?:\/|$)/i.test(new URL(a.href, location.href).pathname); }
                catch (_) { return false; }
            });
            const hasPostImage = [...root.querySelectorAll('img')].some(img => {
                const src = img.currentSrc || img.src || img.getAttribute('src') || '';
                if (!/cdninstagram\.com|fbcdn\.net/i.test(src)) return false;
                if (/t51\.2885-19|profile_pic/i.test(src)) return false;
                if (img.closest('nav, [role="banner"], main > header')) return false;
                return true;
            });
            return hasPostLink || hasPostImage;
        }"""))
    except Exception:
        return False


def find_open_instagram_page(context):
    """Find the profile tab using DOM evidence first, then its URL like the OK downloader.

    Every tab in the controlled persistent context is examined, newest first. A loaded
    profile grid outranks a URL-only candidate; a URL candidate is still accepted while
    its grid is loading. The selected tab is never navigated from this function.
    """
    deadline = time.monotonic() + 3.0
    last_pages = []
    rejected_instagram_tabs = []
    while True:
        try:
            pages = [page for page in context.pages if not page.is_closed()]
        except Exception:
            pages = []
        last_pages = pages
        rejected_instagram_tabs = []
        candidates = []
        for page in reversed(pages):
            url = _open_page_url(page)
            if not url:
                continue
            try:
                parsed = urlsplit(url)
            except Exception:
                continue
            host = parsed.netloc.lower().split(":", 1)[0]
            if host not in {"instagram.com", "www.instagram.com"}:
                if "facebook.com" in host or "accountscenter.facebook.com" in host:
                    rejected_instagram_tabs.append(url)
                continue
            try:
                normalize_instagram_page(page)
                candidates.append((page, _has_instagram_profile_grid(page)))
            except ValueError:
                rejected_instagram_tabs.append(url)

        # As in OK's find_photos_page(), prefer the tab with the actual target DOM.
        # If Instagram is still rendering the grid, use the most recent valid profile URL.
        grid_candidates = [page for page, has_grid in candidates if has_grid]
        if grid_candidates:
            return grid_candidates[0]
        if candidates:
            return candidates[0][0]

        if time.monotonic() >= deadline:
            break
        time.sleep(0.25)

    tab_lines = [_tab_debug_description(page, i + 1) for i, page in enumerate(last_pages[-8:])]
    tabs_report = "\n".join(tab_lines) if tab_lines else "(в контролируемом окне Chrome нет открытых вкладок)"
    if rejected_instagram_tabs:
        last_url = rejected_instagram_tabs[0]
        raise RuntimeError(
            "Не найдена вкладка профиля Instagram или /tagged/ среди вкладок, которыми управляет загрузчик. "
            f"Обнаружена страница Instagram/Meta, не похожая на профиль: {last_url}.\n\n"
            "Проверьте, что профиль открыт именно в окне Chrome, запущенном загрузчиком, а не в другом окне Chrome. "
            "Откройте профиль заново в этом окне и повторите.\n\nОткрытые вкладки:\n" + tabs_report
        )
    raise RuntimeError(
        "Не найдена вкладка профиля Instagram. Загрузчик видит только вкладки своего отдельного профиля Chrome. "
        "Откройте профиль или его /tagged/ именно в окне, запущенном загрузчиком; обычный Chrome может быть другим процессом.\n\n"
        "Открытые вкладки, которые видит загрузчик:\n" + tabs_report
    )


def normalized_url_key(url: str) -> str:
    """Create a stable media key without expiring Instagram CDN query tokens.

    Instagram commonly rotates signed query parameters for the same CDN asset.
    For media CDN hosts the path is used as the stable key; non-CDN URLs retain
    their query string so unrelated resources are not accidentally conflated.
    """
    url = absolute_url(url)
    if not url:
        return ""
    p = urlsplit(url)
    host = (p.hostname or "").lower()
    is_media_cdn = host.endswith("cdninstagram.com") or host.endswith("fbcdn.net")
    query = "" if is_media_cdn else p.query
    return urlunsplit((p.scheme.lower(), p.netloc.lower(), p.path, query, ""))


def image_extension(body: bytes, content_type: str, url: str) -> str:
    if body.startswith(b"\xff\xd8\xff"):
        return ".jpg"
    if body.startswith(b"\x89PNG\r\n\x1a\n"):
        return ".png"
    if body.startswith((b"GIF87a", b"GIF89a")):
        return ".gif"
    if body.startswith(b"RIFF") and body[8:12] == b"WEBP":
        return ".webp"
    if body.startswith(b"BM"):
        return ".bmp"
    if len(body) >= 12 and body[4:8] == b"ftyp" and body[8:12] in (b"avif", b"avis"):
        return ".avif"
    ct = (content_type or "").lower().split(";", 1)[0].strip()
    mapping = {"image/jpeg": ".jpg", "image/jpg": ".jpg", "image/png": ".png", "image/webp": ".webp", "image/gif": ".gif", "image/bmp": ".bmp", "image/avif": ".avif"}
    if ct in mapping:
        return mapping[ct]
    suffix = Path(urlsplit(url).path).suffix.lower()
    return suffix if suffix in IMAGE_EXTENSIONS else ".jpg"


def find_existing_image(stem: Path) -> Path | None:
    for path in stem.parent.glob(stem.name + ".*"):
        if path.suffix.lower() in IMAGE_EXTENSIONS:
            try:
                if path.is_file() and path.stat().st_size > 0:
                    return path
            except OSError:
                pass
    return None


def build_existing_image_url_cache(directory: Path, profile_url: str) -> dict[str, Path]:
    """Index already-downloaded images for this profile by stable CDN URL key.

    This lets repeated runs avoid an image request when Instagram has only rotated
    expiring CDN parameters. Entries are accepted only when both metadata and image
    file still exist.
    """
    index: dict[str, Path] = {}
    if not directory.is_dir():
        return index
    for metadata_path in directory.glob("*.json"):
        try:
            metadata = json.loads(metadata_path.read_text(encoding="utf-8"))
        except Exception:
            continue
        if not isinstance(metadata, dict) or str(metadata.get("url_source") or "") != profile_url:
            continue
        key = normalized_url_key(str(metadata.get("url_image") or ""))
        if not key or key in index:
            continue
        image_path = find_existing_image(metadata_path.with_suffix(""))
        if image_path is not None:
            index[key] = image_path
    return index


def fingerprint_image(path: Path) -> tuple[str, int | None, float | None]:
    """Return an exact SHA-256 and, when Pillow is available, a perceptual dHash.

    The exact hash detects byte-for-byte duplicates. dHash additionally catches the
    same picture served at another size or with a different JPEG encoding. It is
    deliberately conservative and also checks the aspect ratio before matching.
    """
    digest = hashlib.sha256()
    with path.open("rb") as source:
        for chunk in iter(lambda: source.read(1024 * 1024), b""):
            digest.update(chunk)
    sha256 = digest.hexdigest()
    try:
        from PIL import Image, ImageOps
        with Image.open(path) as raw:
            image = ImageOps.exif_transpose(raw)
            width, height = image.size
            if width <= 0 or height <= 0:
                return sha256, None, None
            aspect = width / height
            pixels = list(image.convert("L").resize((9, 8)).getdata())
        d_hash = 0
        bit = 0
        for y in range(8):
            row = y * 9
            for x in range(8):
                if pixels[row + x] > pixels[row + x + 1]:
                    d_hash |= 1 << bit
                bit += 1
        return sha256, d_hash, aspect
    except Exception:
        # Downloading still works in installations that do not have Pillow.
        return sha256, None, None


def find_duplicate_fingerprint(
    sha256: str,
    d_hash: int | None,
    aspect: float | None,
    exact_index: dict[str, Path],
    dhash_buckets: dict[tuple[int, int], list[tuple[str, int, float, Path]]],
) -> Path | None:
    """Find the same file content or a very close perceptual duplicate."""
    exact_match = exact_index.get(sha256)
    if exact_match is not None:
        return exact_match
    if d_hash is None or aspect is None:
        return None

    possible: dict[str, tuple[str, int, float, Path]] = {}
    for band_index in range(4):
        band = (d_hash >> (band_index * 16)) & 0xFFFF
        for record in dhash_buckets.get((band_index, band), ()):
            possible[record[0]] = record
    for record_sha, other_hash, other_aspect, other_path in possible.values():
        if abs(aspect - other_aspect) > max(0.02, aspect * 0.015):
            continue
        if (d_hash ^ other_hash).bit_count() <= 3:
            return other_path
    return None


def remember_fingerprint(
    sha256: str,
    d_hash: int | None,
    aspect: float | None,
    path: Path,
    exact_index: dict[str, Path],
    dhash_buckets: dict[tuple[int, int], list[tuple[str, int, float, Path]]],
) -> None:
    exact_index[sha256] = path
    if d_hash is None or aspect is None:
        return
    record = (sha256, d_hash, aspect, path)
    for band_index in range(4):
        band = (d_hash >> (band_index * 16)) & 0xFFFF
        dhash_buckets.setdefault((band_index, band), []).append(record)


def write_metadata(path: Path, metadata: dict) -> None:
    clean = {
        "url_source": str(metadata.get("url_source") or ""),
        "name": str(metadata.get("name") or ""),
        "url_image": str(metadata.get("url_image") or ""),
        "info": metadata.get("info") if isinstance(metadata.get("info"), dict) else {},
    }
    path.parent.mkdir(parents=True, exist_ok=True)
    tmp = path.with_name(path.name + ".part")
    tmp.write_text(json.dumps(clean, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(path)


_DOWNLOAD_COOLDOWN_UNTIL = 0.0
_DOWNLOAD_COOLDOWN_LOCK = threading.Lock()
_DOWNLOAD_429_STREAK = 0


def _retry_after_seconds(headers) -> float | None:
    """Parse Retry-After as either seconds or an HTTP-date."""
    value = str((headers or {}).get("retry-after") or "").strip()
    if not value:
        return None
    try:
        return max(0.0, float(value))
    except ValueError:
        pass
    try:
        when = parsedate_to_datetime(value)
        if when.tzinfo is None:
            when = when.replace(tzinfo=timezone.utc)
        return max(0.0, (when - datetime.now(timezone.utc)).total_seconds())
    except (TypeError, ValueError, OverflowError):
        return None


def _set_download_cooldown(seconds: float) -> None:
    """Set a shared cooldown so subsequent image requests respect HTTP 429."""
    global _DOWNLOAD_COOLDOWN_UNTIL
    deadline = time.monotonic() + max(0.0, seconds)
    with _DOWNLOAD_COOLDOWN_LOCK:
        _DOWNLOAD_COOLDOWN_UNTIL = max(_DOWNLOAD_COOLDOWN_UNTIL, deadline)


def _wait_for_download_cooldown() -> None:
    while True:
        with _DOWNLOAD_COOLDOWN_LOCK:
            remaining = _DOWNLOAD_COOLDOWN_UNTIL - time.monotonic()
        if remaining <= 0:
            return
        print(f"[RATE LIMIT] Instagram ограничил частоту запросов; ожидание {remaining:.1f} с.")
        time.sleep(remaining)


def _register_http_429(headers) -> tuple[float, float | None]:
    """Register a rate-limit response and choose adaptive backoff.

    Successful downloads reset the fallback streak. If Retry-After is supplied,
    the server's delay is followed; otherwise backoff grows only while 429s persist.
    No fixed delay is added to normal successful requests.
    """
    global _DOWNLOAD_429_STREAK
    retry_after = _retry_after_seconds(headers)
    with _DOWNLOAD_COOLDOWN_LOCK:
        _DOWNLOAD_429_STREAK += 1
        streak = _DOWNLOAD_429_STREAK
    fallback = min(10.0 * (2 ** min(streak - 1, 4)), 160.0)
    delay = retry_after if retry_after is not None else fallback
    _set_download_cooldown(delay)
    return delay, retry_after


def _register_download_success() -> None:
    global _DOWNLOAD_429_STREAK
    with _DOWNLOAD_COOLDOWN_LOCK:
        _DOWNLOAD_429_STREAK = 0


def download_one(context, url: str, stem: Path, referer: str) -> tuple[Path | None, str | None]:
    """Download one image; adapt only after 429 and share cooldown between URLs."""
    if not url:
        return None, "Пустой URL изображения"
    headers = {
        "Referer": referer or BASE_URL,
        "Accept": "image/avif,image/webp,image/apng,image/*,*/*;q=0.8",
    }
    last_error = "Неизвестная ошибка загрузки"
    # A 429 gets one retry after the advertised/adaptive cooldown. Repeated 429
    # responses extend the shared cooldown instead of hammering the same endpoint.
    attempts = 3
    retried_after_429 = False
    for attempt in range(attempts):
        _wait_for_download_cooldown()
        tmp = None
        try:
            response = context.request.get(url, timeout=30000, headers=headers)
            if not response.ok:
                status = int(response.status)
                last_error = f"HTTP {status}"
                if status == 429:
                    delay, retry_after = _register_http_429(response.headers)
                    if not retried_after_429:
                        retried_after_429 = True
                        print(
                            f"[RATE LIMIT HTTP 429] Одна повторная попытка после {delay:.1f} с; "
                            f"Retry-After={'заголовок сервера' if retry_after is not None else 'адаптивная пауза'}: {url}"
                        )
                        continue
                    print(f"[HTTP 429] Повторный отказ; новые запросы будут ждать {delay:.1f} с: {url}")
                    return None, last_error
                retryable = status in (408, 425) or status >= 500
                if retryable and attempt < 2:
                    delay = (1.5, 4.0)[attempt]
                    print(f"[RETRY {last_error}] Попытка {attempt + 2}/{attempts} через {delay} с: {url}")
                    time.sleep(delay)
                    continue
                print(f"[HTTP {status}] Не удалось скачать изображение: {url}")
                return None, last_error
            body = response.body()
            content_type = str(response.headers.get("content-type") or "").lower()
            if not body or "text/html" in content_type or body.lstrip()[:20].lower().startswith((b"<!doctype html", b"<html", b"<head", b"<body")):
                last_error = f"Вместо изображения получен не графический ответ (Content-Type: {content_type or 'не указан'})"
                print(f"[SKIP] {last_error}: {url}")
                return None, last_error
            valid_signature = body.startswith((b"\xff\xd8\xff", b"\x89PNG\r\n\x1a\n", b"GIF87a", b"GIF89a", b"RIFF", b"BM"))
            valid_avif = len(body) >= 12 and body[4:8] == b"ftyp" and body[8:12] in (b"avif", b"avis")
            if not valid_signature and not valid_avif and not content_type.startswith("image/"):
                last_error = f"Неизвестный формат ответа (Content-Type: {content_type or 'не указан'})"
                print(f"[SKIP] {last_error}: {url}")
                return None, last_error
            ext = image_extension(body, content_type, url)
            target = Path(str(stem) + ext)
            target.parent.mkdir(parents=True, exist_ok=True)
            tmp = target.with_name(target.name + ".part")
            tmp.write_bytes(body)
            tmp.replace(target)
            _register_download_success()
            return target, None
        except Exception as exc:
            last_error = f"{type(exc).__name__}: {exc}"
            if tmp is not None:
                try:
                    tmp.unlink(missing_ok=True)
                except OSError:
                    pass
            if attempt < 2:
                delay = (1.5, 4.0)[attempt]
                print(f"[RETRY] Ошибка загрузки; попытка {attempt + 2}/{attempts} через {delay} с: {last_error}")
                time.sleep(delay)
                continue
            print(f"[ERROR] Не удалось скачать {url}: {last_error}")
    return None, last_error


# Instagram changes its class names frequently; regular /p/ post URLs are the primary key.
COLLECT_GRID_JS = r"""(targetUsername) => {
  const root = document.querySelector('main') || document;
  // Prefer the profile name resolved by Python from the originally selected profile.
  // The page URL is only a fallback (it may currently be on /tagged/).
  const pathParts = location.pathname.split('/').filter(Boolean);
  const profileUsername = String(targetUsername || pathParts[0] || '').trim().replace(/^@/, '');
  const permalinkFor = (code) => profileUsername
    ? `https://www.instagram.com/${encodeURIComponent(profileUsername)}/p/${code}/`
    : `https://www.instagram.com/p/${code}/`;
  const found = new Map();
  const isMediaUrl = (value) => {
    try {
      const u = new URL(value, location.href);
      if (!/cdninstagram\.com|fbcdn\.net/i.test(u.hostname)) return false;
      if (/t51\.2885-19|profile_pic|s\d+x\d+.*profile/i.test(u.pathname + ' ' + u.search)) return false;
      return true;
    } catch (_) { return false; }
  };
  const bestSrc = (img) => {
    const srcset = img.getAttribute('srcset') || '';
    if (srcset) {
      const options = srcset.split(',').map(part => {
        const bits = part.trim().split(/\s+/);
        const d = bits[1] || '1x';
        const value = parseFloat(d) || 1;
        return {url: bits[0], score: d.endsWith('w') ? value : value * 10000};
      }).filter(x => x.url && !x.url.startsWith('data:'));
      if (options.length) {
        options.sort((a,b) => b.score - a.score);
        return options[0].url;
      }
    }
    return img.currentSrc || img.getAttribute('data-original') || img.getAttribute('data-src') || img.src || img.getAttribute('src') || '';
  };
  const postLink = (element) => {
    let current = element;
    while (current && current !== root.parentElement && current !== document.body) {
      if (current.tagName === 'A' && current.href) {
        try {
          const u = new URL(current.href, location.href);
          const m = u.pathname.match(/\/p\/([^/]+)(?:\/|$)/i);
          if (m) return {url: permalinkFor(m[1]), kind: 'p', code: m[1]};
        } catch (_) {}
      }
      current = current.parentElement;
    }
    return null;
  };
  const codeFromImageUrl = (raw) => {
    try {
      const u = new URL(raw, location.href);
      const cache = u.searchParams.get('ig_cache_key') || u.searchParams.get('_nc_ig_cache_key') || '';
      if (cache) {
        const payload = cache.split('.')[0];
        try {
          const decoded = atob(payload.replace(/-/g, '+').replace(/_/g, '/'));
          if (/^\d+$/.test(decoded)) return 'media_' + decoded;
        } catch (_) {}
      }
      const file = u.pathname.split('/').pop() || '';
      const fromFilename = file.match(/^(\d+)_/);
      if (fromFilename) return 'media_' + fromFilename[1] + '_' + file.replace(/\.[^.]+$/, '');
      // Fall back to a deterministic hash of the CDN pathname, excluding expiring query tokens.
      let h = 2166136261;
      const value = u.hostname + u.pathname;
      for (let i = 0; i < value.length; i++) h = Math.imul(h ^ value.charCodeAt(i), 16777619);
      return 'media_' + (h >>> 0).toString(16);
    } catch (_) { return ''; }
  };
  const put = (code, entry) => {
    if (!code) return;
    const old = found.get(code);
    if (!old || (!old.preview && entry.preview) || (!old.permalink && entry.permalink)) {
      found.set(code, {...old, ...entry, preview: entry.preview || old?.preview || '', permalink: entry.permalink || old?.permalink || ''});
    }
  };

  // Only collect regular post permalinks (/p/). This downloader imports still images
  // and carousel photos, not Reels (/reel/) or IGTV (/tv/) recommendations.
  // Match path components, allowing query strings and trailing routes.
  for (const a of root.querySelectorAll('a[href]')) {
    let u;
    try { u = new URL(a.href, location.href); } catch (_) { continue; }
    const match = u.pathname.match(/\/p\/([^/]+)(?:\/|$)/i);
    if (!match) continue;
    const code = match[1];
    const imgs = [...a.querySelectorAll('img')];
    imgs.sort((x,y) => ((y.naturalWidth||0)*(y.naturalHeight||0)) - ((x.naturalWidth||0)*(x.naturalHeight||0)));
    let preview = '', alt = '';
    for (const img of imgs) {
      const candidate = bestSrc(img);
      if (candidate && isMediaUrl(candidate)) { preview = candidate; alt = (img.getAttribute('alt') || '').trim(); break; }
    }
    put(code, {
      shortcode: code,
      kind: 'p',
      permalink: permalinkFor(code),
      preview, alt
    });
  }

  // Second pass: recover images inside a /p/ card if the first pass missed its wrapper.
  // Unlinked CDN images are deliberately ignored: they may belong to suggested content,
  // avatars, or unrelated blocks outside the selected profile's publication grid.
  for (const img of root.querySelectorAll('img')) {
    const raw = bestSrc(img);
    if (!raw || !isMediaUrl(raw)) continue;
    if (img.closest('nav, [role="banner"], main > header')) continue;
    const width = img.naturalWidth || 0, height = img.naturalHeight || 0;
    const rect = img.getBoundingClientRect();
    if (Math.max(width, rect.width) < 100 || Math.max(height, rect.height) < 100) continue;
    const alt = (img.getAttribute('alt') || '').trim();
    if (/profile picture|фото профиля|profile photo|avatar/i.test(alt)) continue;
    const linked = postLink(img);
    if (!linked || linked.kind !== 'p' || !linked.code) continue;
    const code = linked.code;
    put(code, {
      shortcode: code,
      kind: 'p',
      permalink: linked.url,
      preview: raw,
      alt
    });
  }
  return [...found.values()];
}"""

PROFILE_DATA_JS = r"""() => {
  const text = (el) => (el && (el.innerText || el.textContent) || '').trim().replace(/\s+/g, ' ');
  const title = document.querySelector('meta[property="og:title"]')?.content || document.title || '';
  const h1 = document.querySelector('main header h1, main h1');
  const h2 = document.querySelector('main header h2');
  const desc = document.querySelector('meta[property="og:description"]')?.content || '';
  return {title, heading: text(h1) || text(h2), description: desc};
}"""

POST_MEDIA_JS = r"""() => {
  const abs = (v) => { try { return new URL(v, location.href).href; } catch (_) { return ''; } };
  const main = document.querySelector('main') || document;
  const articles = [...main.querySelectorAll('article')].filter(a => a.querySelector('img'));
  const root = articles[0] || main;
  const nextPattern = /next|forward|следующ|далее|siguiente|pr[oó]xim|nächste|suivant|avanti/i;

  const selectBest = (img) => {
    const srcset = img.getAttribute('srcset') || '';
    const options = srcset.split(',').map(part => {
      const bits = part.trim().split(/\s+/);
      const descriptor = bits[1] || '1x';
      const value = parseFloat(descriptor) || 1;
      return {url: bits[0], score: descriptor.endsWith('w') ? value : value * 10000};
    }).filter(item => item.url && !item.url.startsWith('data:'));
    if (options.length) {
      options.sort((a, b) => b.score - a.score);
      return abs(options[0].url);
    }
    return abs(img.currentSrc || img.getAttribute('src') || img.src || '');
  };

  const visibleRect = (el) => {
    const rect = el.getBoundingClientRect();
    let left = Math.max(0, rect.left);
    let top = Math.max(0, rect.top);
    let right = Math.min(window.innerWidth || document.documentElement.clientWidth, rect.right);
    let bottom = Math.min(window.innerHeight || document.documentElement.clientHeight, rect.bottom);
    let parent = el.parentElement;
    while (parent && parent !== document.documentElement) {
      const style = getComputedStyle(parent);
      if (/(hidden|clip|scroll|auto)/i.test(style.overflowX)) {
        const pr = parent.getBoundingClientRect();
        left = Math.max(left, pr.left);
        right = Math.min(right, pr.right);
      }
      if (/(hidden|clip|scroll|auto)/i.test(style.overflowY)) {
        const pr = parent.getBoundingClientRect();
        top = Math.max(top, pr.top);
        bottom = Math.min(bottom, pr.bottom);
      }
      parent = parent.parentElement;
    }
    return {left, top, right, bottom, width: Math.max(0, right - left), height: Math.max(0, bottom - top)};
  };

  const candidates = [];
  for (const img of root.querySelectorAll('img')) {
    const naturalWidth = img.naturalWidth || 0;
    const naturalHeight = img.naturalHeight || 0;
    const url = selectBest(img);
    if (!url || !/cdninstagram\.com|fbcdn\.net/i.test(url)) continue;
    if (/t51\.2885-19|profile_pic/i.test(url)) continue;
    if (/profile picture|фото профиля|profile photo|avatar/i.test(img.getAttribute('alt') || '')) continue;
    if (img.closest('nav, header[role="banner"]')) continue;
    const style = getComputedStyle(img);
    if (style.display === 'none' || style.visibility === 'hidden' || Number(style.opacity) === 0) continue;
    const rect = visibleRect(img);
    if (rect.width < 100 || rect.height < 100) continue;
    if (naturalWidth && naturalWidth < 240) continue;
    if (naturalHeight && naturalHeight < 240) continue;
    const visibleArea = rect.width * rect.height;
    const fullArea = Math.max(1, (img.getBoundingClientRect().width || 1) * (img.getBoundingClientRect().height || 1));
    const visibleRatio = Math.min(1, visibleArea / fullArea);
    candidates.push({
      url, width: naturalWidth, height: naturalHeight,
      score: visibleArea * (0.7 + 0.3 * visibleRatio),
      alt: (img.getAttribute('alt') || '').trim(),
      element: img
    });
  }
  candidates.sort((a, b) => b.score - a.score);

  const metaDescription = document.querySelector('meta[property="og:description"]')?.content || '';
  const pageDescription = document.querySelector('meta[name="description"]')?.content || '';
  const reservedPaths = new Set(['p','reel','tv','stories','explore','accounts','direct','about','developer']);
  const usernameFromHref = (href) => {
    try {
      const parts = new URL(href, location.href).pathname.split('/').filter(Boolean);
      if (parts.length !== 1 || reservedPaths.has(parts[0].toLowerCase())) return '';
      return parts[0].toLowerCase();
    } catch (_) { return ''; }
  };
  const author = [...root.querySelectorAll('header a[href]')]
    .map(a => usernameFromHref(a.href)).find(Boolean) || '';

  // Prefer the caption visible in the post DOM. Open Graph descriptions sometimes
  // contain only counts or a legacy "likes, comments - username on date: caption" string.
  const cleanMetaCaption = (raw) => {
    let value = String(raw || '').trim().replace(/\s+/g, ' ');
    if (!value || /^see instagram photos and videos from\b/i.test(value)) return '';
    const legacy = value.match(/^\s*\d[\d., \u00a0]*\s+likes?,\s*\d[\d., \u00a0]*\s+comments?\s*[-–—]\s*[^:]{1,180}:\s*([\s\S]*)$/i);
    if (legacy) value = legacy[1].trim();
    else {
      value = value.replace(/^\s*\d[\d., \u00a0]*\s+likes?,\s*\d[\d., \u00a0]*\s+comments?\s*[-–—]\s*/i, '').trim();
    }
    if (/^\d[\d., \s\u00a0]*\s+likes?,\s*\d[\d., \s\u00a0]*\s+comments?$/i.test(value)) return '';
    if (author && value.toLowerCase() === author.toLowerCase()) return '';
    return value.replace(/^["“‘]\s*/, '').replace(/\s*["”’]$/, '').trim();
  };
  const captionNode = root.querySelector('[data-testid*="caption"]') || root.querySelector('h1');
  let caption = '';
  if (captionNode) {
    let visibleCaption = text(captionNode);
    if (author && visibleCaption.toLowerCase().startsWith(author.toLowerCase())) {
      visibleCaption = visibleCaption.slice(author.length).replace(/^[\s:·–—-]+/, '').trim();
    }
    if (visibleCaption && (!author || visibleCaption.toLowerCase() !== author.toLowerCase())) {
      caption = visibleCaption;
    }
  }
  if (!caption) caption = cleanMetaCaption(metaDescription) || cleanMetaCaption(pageDescription);

  // Counts are taken only from visible DOM/metadata; if Instagram hides them,
  // the fields remain empty rather than being guessed.
  const metricText = [
    metaDescription, pageDescription, root.innerText || '',
    ...[...root.querySelectorAll('[aria-label], [title]')].map(el =>
      (el.getAttribute('aria-label') || '') + ' ' + (el.getAttribute('title') || '')
    )
  ].join(' ');
  const likeMatch = metricText.match(/(\d[\d., \u00a0]*(?:[KMB]|тыс\.?|млн)?)[\s\u00a0]*(?:likes?|like count|отмет(?:ка|ки|ок)?\s+нравится|нравится|me gusta|j'aime)/i)
    || metricText.match(/liked by[\s\S]{0,180}?(\d[\d., \s\u00a0]*(?:[KMB]|тыс\.?|млн)?)\s+others/i);
  const commentMatch = metricText.match(/(\d[\d., \u00a0]*(?:[KMB]|тыс\.?|млн)?)[\s\u00a0]*(?:comments?|комментар(?:ий|ия|иев|ии)?)/i);
  const likesCount = likeMatch ? String(likeMatch[1] || '').trim() : '';
  const commentsCount = commentMatch ? String(commentMatch[1] || '').trim() : '';

  const timeNode = [...root.querySelectorAll('time[datetime]')].find(el => {
    const value = el.getAttribute('datetime') || '';
    return !!value && Number.isFinite(Date.parse(value));
  });
  const publishedAt = (timeNode?.getAttribute('datetime') ||
    document.querySelector('meta[property="article:published_time"]')?.content ||
    document.querySelector('meta[property="og:updated_time"]')?.content || '').trim();

  const locationLink = [...root.querySelectorAll('a[href]')].find(a => {
    try { return /\/(?:explore\/)?locations?\//i.test(new URL(a.href, location.href).pathname); }
    catch (_) { return false; }
  });
  const locationName = locationLink ? text(locationLink) : '';

  const buttonIsVisible = (button) => {
    const r = button.getBoundingClientRect();
    const style = getComputedStyle(button);
    return r.width > 0 && r.height > 0 && style.visibility !== 'hidden' && style.display !== 'none' && Number(style.opacity) !== 0;
  };
  const buttonLabel = (button) => ((button.getAttribute('aria-label') || '') + ' ' +
    (button.getAttribute('title') || '') + ' ' + (button.getAttribute('data-testid') || '') + ' ' +
    (button.innerText || '')).trim();
  const controls = [...document.querySelectorAll('button,[role="button"]')];
  const matchesNext = (button) => {
    return nextPattern.test(buttonLabel(button)) && !button.disabled &&
      button.getAttribute('aria-disabled') !== 'true' && buttonIsVisible(button);
  };

  const counterText = [root.innerText || '', ...[...root.querySelectorAll('[aria-label]')]
    .map(el => el.getAttribute('aria-label') || '')].join(' ');
  const counter = counterText.match(/(?:^|\D)(\d{1,3})\s*(?:of|из|\/|\u0438\u0437)\s*(\d{1,3})(?:\D|$)/i);
  const slideIndicator = counter ? `${counter[1]}/${counter[2]}` : '';
  const counterPosition = counter ? Number(counter[1]) : 0;
  const counterTotal = counter ? Number(counter[2]) : 0;
  const primaryRect = candidates[0]?.element?.getBoundingClientRect() || null;
  const centerY = primaryRect ? primaryRect.top + primaryRect.height / 2 : 0;
  const centerX = primaryRect ? primaryRect.left + primaryRect.width / 2 : 0;

  // First choose controls by accessible label inside the post, then in main.
  // The main fallback is spatially tied to the displayed post image to avoid clicking
  // a recommended carousel lower on the page.
  let nextButton = [...root.querySelectorAll('button,[role="button"]')].find(matchesNext) || null;
  if (!nextButton) {
    const labelledMainControls = [...main.querySelectorAll('button,[role="button"]')].filter(matchesNext);
    if (primaryRect && labelledMainControls.length) {
      labelledMainControls.sort((a, b) => {
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        const score = r => Math.abs((r.top + r.height / 2) - centerY) + Math.abs((r.left + r.width / 2) - (primaryRect.right - 10));
        return score(ra) - score(rb);
      });
      const r = labelledMainControls[0].getBoundingClientRect();
      if (Math.abs((r.top + r.height / 2) - centerY) <= primaryRect.height * 0.45) nextButton = labelledMainControls[0];
    }
  }

  // Some Instagram layouts expose the arrow as an unlabeled role=button. If the
  // counter proves there are more slides, use the visible control near the right
  // edge and vertical center of the primary media as a careful fallback.
  if (!nextButton && counterPosition > 0 && counterTotal > counterPosition && primaryRect) {
    const unlabeled = [...root.querySelectorAll('button,[role="button"]')].filter(button => {
      if (button.disabled || button.getAttribute('aria-disabled') === 'true' || !buttonIsVisible(button)) return false;
      const label = buttonLabel(button);
      if (label && !/^(button|arrow|next slide|slide navigation)$/i.test(label)) return false;
      const r = button.getBoundingClientRect();
      const x = r.left + r.width / 2, y = r.top + r.height / 2;
      return y >= primaryRect.top + primaryRect.height * 0.20 &&
             y <= primaryRect.bottom - primaryRect.height * 0.20 &&
             x >= centerX && x <= primaryRect.right + 60;
    });
    if (unlabeled.length) {
      unlabeled.sort((a, b) => {
        const ra = a.getBoundingClientRect(), rb = b.getBoundingClientRect();
        const score = r => Math.abs((r.left + r.width / 2) - (primaryRect.right - 8)) + Math.abs((r.top + r.height / 2) - centerY);
        return score(ra) - score(rb);
      });
      nextButton = unlabeled[0];
    }
  }

  return {
    media: candidates[0] ? {url: candidates[0].url, width: candidates[0].width,
      height: candidates[0].height, score: candidates[0].score, alt: candidates[0].alt} : null,
    caption,
    author,
    publishedAt,
    likesCount,
    commentsCount,
    location: locationName,
    hasNext: !!nextButton,
    nextLabel: nextButton ? (buttonLabel(nextButton) || 'Next') : '',
    nextButtonIndex: nextButton ? controls.indexOf(nextButton) : -1,
    slideIndicator
  };
}"""


def collect_grid_entries_with_scroll(page, status_callback, profile_username: str = "") -> tuple[list[dict], str | None]:
    """Collect permalink cards and direct CDN images while scrolling in increments.

    Scrolling directly to scrollHeight repeatedly can stall on Instagram's lazy
    loader/virtualized grid. Move by about 80% of a viewport, harvest DOM after
    each move, and stop only after both media count and document height stay idle.
    """
    collected: dict[str, dict] = {}
    idle_rounds = 0
    stable_height_rounds = 0
    previous_height = 0
    read_errors = 0
    last_read_error = ""
    max_rounds = 300
    scan_warning = None
    stopped_by_stability = False
    for turn in range(max_rounds):
        try:
            batch = page.evaluate(COLLECT_GRID_JS, profile_username) or []
        except Exception as exc:
            read_errors += 1
            last_read_error = f"{type(exc).__name__}: {exc}"
            print(f"[WARN] Ошибка чтения публикаций/изображений Instagram: {exc}")
            batch = []
        before = len(collected)
        for item in batch:
            code = str(item.get("shortcode") or "").strip()
            preview = str(item.get("preview") or "").strip()
            if not code or not preview:
                continue
            old = collected.get(code)
            if old is None:
                collected[code] = item
            elif (not old.get("permalink") and item.get("permalink")) or (not old.get("preview") and preview):
                merged = dict(old)
                merged.update({k: v for k, v in item.items() if v not in (None, "")})
                collected[code] = merged
        added = len(collected) - before
        idle_rounds = 0 if added else idle_rounds + 1
        if turn == 0 or added or turn % 6 == 0:
            linked = sum(1 for row in collected.values() if row.get("permalink"))
            direct = len(collected) - linked
            status_callback(f"Сканирование публикаций профиля: {len(collected)} (обычные посты /p/, без Reels)…")
        try:
            metrics = page.evaluate("""() => ({
                top: window.scrollY || document.documentElement.scrollTop || 0,
                viewport: window.innerHeight || 700,
                height: Math.max(document.body?.scrollHeight || 0, document.documentElement?.scrollHeight || 0)
            })""") or {}
            height = int(metrics.get("height") or 0)
            top = float(metrics.get("top") or 0)
            viewport = float(metrics.get("viewport") or 700)
            if height and height == previous_height:
                stable_height_rounds += 1
            else:
                stable_height_rounds = 0
            previous_height = height
            at_bottom = top + viewport >= height - 30 if height else False
            # Wait longer after reaching the current bottom so newly-triggered requests
            # have time to append the next batch before we decide the feed is exhausted.
            page.evaluate("window.scrollBy(0, Math.max(500, Math.floor((window.innerHeight || 700) * 0.82)))")
            page.wait_for_timeout(750 if at_bottom else 500)
        except Exception as exc:
            scan_warning = f"Прокрутка профиля прервалась до подтверждения конца ленты: {type(exc).__name__}: {exc}"
            break
        if idle_rounds >= 12 and stable_height_rounds >= 6:
            stopped_by_stability = True
            break
    else:
        scan_warning = f"Достигнут лимит сканирования ленты ({max_rounds} циклов); профиль может быть собран не полностью."
    if not stopped_by_stability and scan_warning is None:
        scan_warning = "Не удалось подтвердить, что прокрутка профиля дошла до конца ленты."
    if read_errors:
        read_warning = f"Ошибок чтения DOM при сканировании: {read_errors}; последняя: {last_read_error}."
        scan_warning = f"{scan_warning + ' ' if scan_warning else ''}{read_warning}"
    if scan_warning:
        print(f"[INCOMPLETE-SCAN] {scan_warning}")
        status_callback("ВНИМАНИЕ: " + scan_warning)
    else:
        status_callback("Прокрутка остановлена после нескольких циклов без новых публикаций; это эвристика, а не гарантия полноты профиля.")
    return list(collected.values()), scan_warning


def resolve_post_metadata(post_page, entry: dict, status_callback, index: int, total: int,
                          expected_owner: str | None = None) -> dict | None:
    """Read caption/date/counts from a post page without traversing its carousel."""
    url = str(entry.get("permalink") or "").strip()
    if not url:
        return {}

    status_callback(f"Читаю описание и статистику публикации {index}/{total} — {entry.get('shortcode', '')}")
    try:
        post_page.goto(url, wait_until="domcontentloaded", timeout=45000)
    except Exception as exc:
        # A redirect or slow document can interrupt goto even when the DOM is usable.
        print(f"[WARN] Не удалось открыть публикацию для чтения метаданных {url}: {exc}")
    try:
        post_page.wait_for_function(
            "() => !!document.querySelector('article img, main img')",
            timeout=9000,
        )
    except Exception:
        pass
    try:
        post_page.wait_for_timeout(250)
        data = post_page.evaluate(POST_MEDIA_JS) or {}
    except Exception as exc:
        print(f"[WARN] Не удалось прочитать метаданные публикации {url}: {exc}")
        return {}

    actual_owner = str(data.get("author") or "").strip().lstrip("@").lower()
    if expected_owner and actual_owner and actual_owner != expected_owner.lower():
        message = (
            f"Публикация {entry.get('shortcode', '')} принадлежит @{actual_owner}, "
            f"а не @{expected_owner}; пропускаю."
        )
        print(f"[SKIP-OTHER-OWNER] {url}: {message}")
        status_callback(message)
        return None

    indicator = str(data.get("slideIndicator") or "").strip()
    counter_match = re.search(r"(\d+)\s*/\s*(\d+)", indicator)
    expected_slides = int(counter_match.group(2)) if counter_match else 0
    return {
        "caption": str(data.get("caption") or "").strip(),
        "alt": str((data.get("media") or {}).get("alt") or entry.get("alt") or "").strip(),
        "author": actual_owner,
        "published_at": str(data.get("publishedAt") or "").strip(),
        "likes_count": str(data.get("likesCount") or "").strip(),
        "comments_count": str(data.get("commentsCount") or "").strip(),
        "location": str(data.get("location") or "").strip(),
        "expected_slides": expected_slides or None,
    }


def resolve_post_media(post_page, entry: dict, status_callback, index: int, total: int,
                       expected_owner: str | None = None) -> list[dict] | None:
    """Collect every accessible image slide from a post/carousel.

    The active image is detected by its visible area inside clipping ancestors. After
    pressing Next, the function waits for the media URL or slide indicator to change
    instead of assuming a fixed delay is sufficient.
    """
    found: list[dict] = []
    seen_urls: set[str] = set()
    visited_states: set[str] = set()
    url = str(entry.get("permalink") or "")
    current_data = None
    expected_slides = 0
    carousel_warning = ""
    try:
        status_callback(f"Получаю оригиналы публикаций: {index}/{total} — {entry.get('shortcode', '')}")
        post_page.goto(url, wait_until="domcontentloaded", timeout=45000)
        try:
            post_page.wait_for_function(
                "() => [...(document.querySelector('main')||document).querySelectorAll('img')].some(i => i.complete && i.naturalWidth >= 240 && i.naturalHeight >= 240)",
                timeout=12000,
            )
        except Exception:
            pass
        post_page.wait_for_timeout(500)

        # Instagram carousels may contain more slides than the old 20-iteration
        # loop allowed, so use a higher safety cap and stop based on observed state.
        for _ in range(100):
            try:
                if current_data is None:
                    current_data = post_page.evaluate(POST_MEDIA_JS) or {}
            except Exception:
                current_data = {}

            actual_owner = str(current_data.get("author") or "").strip().lstrip("@").lower()
            if expected_owner and actual_owner and actual_owner != expected_owner.lower():
                message = f"Публикация {entry.get('shortcode', '')} принадлежит @{actual_owner}, а не @{expected_owner}; пропускаю."
                print(f"[SKIP-OTHER-OWNER] {url}: {message}")
                status_callback(message)
                return None

            media = current_data.get("media") or {}
            media_url = absolute_url(str(media.get("url") or ""), url)
            caption = str(current_data.get("caption") or "").strip()
            alt = str(media.get("alt") or entry.get("alt") or "").strip()
            indicator = str(current_data.get("slideIndicator") or "").strip()
            counter_match = re.search(r"(\d+)\s*/\s*(\d+)", indicator)
            if counter_match:
                expected_slides = max(expected_slides, int(counter_match.group(2)))
            state = f"url:{media_url}|counter:{indicator}"

            if not media_url:
                carousel_warning = "Не удалось определить URL текущего изображения."
                print(f"[WARN] {carousel_warning} Публикация: {url}")
                break
            if state in visited_states:
                carousel_warning = "Обход остановлен из-за повторения состояния слайда."
                print(f"[WARN] {carousel_warning} Публикация: {url}")
                break
            visited_states.add(state)

            if media_url not in seen_urls:
                found.append({
                    "url": media_url,
                    "caption": caption,
                    "alt": alt,
                    "slide": len(found) + 1,
                    "author": str(current_data.get("author") or "").strip(),
                    "published_at": str(current_data.get("publishedAt") or "").strip(),
                    "likes_count": str(current_data.get("likesCount") or "").strip(),
                    "comments_count": str(current_data.get("commentsCount") or "").strip(),
                    "location": str(current_data.get("location") or "").strip(),
                })
                seen_urls.add(media_url)

            if not current_data.get("hasNext"):
                break

            # Use a real browser pointer action instead of HTMLElement.click().
            # Some Instagram carousel controls do not advance reliably on synthetic clicks.
            clicked = False
            button_index = current_data.get("nextButtonIndex", -1)
            try:
                button_index = int(button_index)
            except (TypeError, ValueError):
                button_index = -1
            if button_index >= 0:
                controls = post_page.locator("button, [role='button']")
                try:
                    controls.nth(button_index).click(timeout=5000)
                    clicked = True
                except Exception as click_error:
                    # DOM may have re-rendered between inspection and the click. Refresh
                    # the selector once and retry with the newly reported control index.
                    try:
                        refreshed = post_page.evaluate(POST_MEDIA_JS) or {}
                        refreshed_index = int(refreshed.get("nextButtonIndex", -1))
                        if refreshed_index >= 0 and refreshed.get("hasNext"):
                            controls = post_page.locator("button, [role='button']")
                            controls.nth(refreshed_index).click(timeout=3500, force=True)
                            clicked = True
                    except Exception:
                        print(f"[WARN] Не удалось нажать следующую стрелку карусели: {click_error}")
            if not clicked:
                carousel_warning = "Не удалось нажать кнопку следующего слайда; проверьте разметку карусели в Instagram."
                print(f"[WARN] {carousel_warning} Публикация: {url}; подпись кнопки: {current_data.get('nextLabel', '')!r}")
                break

            # Wait for a real state transition. A stale DOM read after a short fixed
            # sleep was previously interpreted as end-of-carousel after the first slide.
            deadline = time.monotonic() + 12.0
            next_data = None
            old_indicator = indicator
            while time.monotonic() < deadline:
                try:
                    post_page.wait_for_timeout(250)
                    observed = post_page.evaluate(POST_MEDIA_JS) or {}
                except Exception:
                    continue
                observed_media = observed.get("media") or {}
                observed_url = absolute_url(str(observed_media.get("url") or ""), url)
                observed_indicator = str(observed.get("slideIndicator") or "").strip()
                if ((observed_url and observed_url != media_url) or
                        (observed_indicator and observed_indicator != old_indicator)):
                    next_data = observed
                    break

            if next_data is None:
                carousel_warning = "После нажатия кнопки перехода следующий слайд не отобразился за 12 секунд."
                print(f"[WARN] {carousel_warning} Публикация: {url}")
                break

            next_media = next_data.get("media") or {}
            next_url = absolute_url(str(next_media.get("url") or ""), url)
            next_indicator = str(next_data.get("slideIndicator") or "").strip()
            next_state = f"url:{next_url}|counter:{next_indicator}"
            if next_state in visited_states:
                carousel_warning = "Карусель вернулась к уже обработанному слайду."
                print(f"[WARN] {carousel_warning} Публикация: {url}")
                break
            current_data = next_data
        else:
            carousel_warning = "Достигнут защитный лимит в 100 шагов обхода карусели."

    except Exception as exc:
        carousel_warning = f"Ошибка чтения публикации: {type(exc).__name__}: {exc}"
        print(f"[WARN] Не удалось получить оригинал публикации {url}: {exc}")

    if expected_slides and len(found) < expected_slides and not carousel_warning:
        carousel_warning = f"По счётчику ожидается {expected_slides} слайдов, получено {len(found)}."
    if found:
        for asset in found:
            asset["expected_slides"] = expected_slides or None
            asset["carousel_warning"] = carousel_warning
        status_callback(f"Публикация {entry.get('shortcode', '')}: найдено слайдов {len(found)}" + (f"; предупреждение: {carousel_warning}" if carousel_warning else ""))
    return found


def load_face_search_config(directory: str | Path) -> dict:
    folder = Path(directory).expanduser().resolve()
    settings_path = folder / "settings.xml"
    profiles_path = folder / "database_profiles.json"
    if not settings_path.is_file():
        raise FileNotFoundError(f"Не найден settings.xml: {settings_path}")
    if not profiles_path.is_file():
        raise FileNotFoundError(f"Не найден database_profiles.json: {profiles_path}")
    xml_root = ET.parse(settings_path).getroot()
    data = json.loads(profiles_path.read_text(encoding="utf-8-sig"))
    profiles = data.get("profiles") if isinstance(data, dict) else None
    if not isinstance(profiles, list) or not profiles:
        raise ValueError("В database_profiles.json нет списка профилей БД.")
    valid = [p for p in profiles if isinstance(p, dict) and str(p.get("name") or "").strip()]
    active = str(data.get("active") or "").strip() if isinstance(data, dict) else ""
    if active not in {str(p.get("name")) for p in valid}:
        configured = str(xml_root.findtext("database/profile") or "").strip()
        active = configured if configured in {str(p.get("name")) for p in valid} else str(valid[0]["name"])
    profile = dict(next(p for p in valid if str(p.get("name")) == active))
    backend = str(xml_root.findtext("compute/backend") or "gpu").strip().lower()
    if not (folder / "face_search_gui.py").is_file() or not (folder / "facesearch").is_dir():
        raise FileNotFoundError("В выбранной папке не найдены face_search_gui.py и каталог facesearch.")
    return {"root": folder, "profile": profile, "active_name": active, "backend": backend}


class FaceSearchImporter:
    """Adapter to FaceSearch's supported database/import APIs."""
    def __init__(self, facesearch_dir: str, status_callback, create_crop_files: bool = True,
                 prepare_schema: bool = True):
        self.status_callback = status_callback
        self.create_crop_files = bool(create_crop_files)
        self.config = load_face_search_config(facesearch_dir)
        self.module = self._load_module(self.config["root"])
        self.conn = None
        self.app = None
        self.imported_count = 0
        profile = self.config["profile"]
        kind = str(profile.get("type") or "sqlite").lower().strip()
        if kind == "sqlite":
            db_path = Path(str(profile.get("path") or self.config["root"] / "faces.db")).expanduser()
            if not db_path.is_absolute():
                db_path = self.config["root"] / db_path
            if not db_path.is_file():
                raise FileNotFoundError(f"Не найдена SQLite-база FaceSearch: {db_path}")
            profile["path"] = str(db_path.resolve())
        self.status_callback(f"Подключение к FaceSearch: профиль «{self.config['active_name']}»…")
        try:
            self.conn = self.module.create_database_for_profile(
                profile, maintenance=False, create_if_missing=False,
                activate_storage=True, initialize=False, check_crop_migration=False,
            )
            if prepare_schema:
                ensure = getattr(self.module, "_ensure_columns", None)
                if callable(ensure):
                    ensure(self.conn, "images", {"source_key": "VARCHAR(255)"})
                    self.conn.commit()
                self.conn.execute("SELECT id, source_type, url_image, source_key FROM images WHERE 1 = 0").fetchall()
            else:
                # Connection test must not create columns or alter the selected database.
                self.conn.execute("SELECT 1").fetchone()
        except Exception:
            self.close()
            raise

    @staticmethod
    def _load_module(root: Path):
        existing = sys.modules.get("face_search_gui")
        if existing is not None and Path(getattr(existing, "__file__", "")).resolve().parent != root.resolve():
            raise RuntimeError("Уже загружен другой экземпляр FaceSearch. Закройте другие загрузчики и повторите.")
        if existing is None:
            root_text = str(root.resolve())
            if root_text not in sys.path:
                sys.path.insert(0, root_text)
            existing = importlib.import_module("face_search_gui")
        return existing

    def existing_source_keys(self, profile_url: str, source_keys: list[str]) -> set[str]:
        found: set[str] = set()
        unique = sorted(set(k for k in source_keys if k))
        for start in range(0, len(unique), 300):
            chunk = unique[start:start + 300]
            marks = ",".join("?" for _ in chunk)
            try:
                rows = self.conn.execute(
                    "SELECT i.source_key FROM images i JOIN profiles p ON p.id=i.profile_id "
                    f"WHERE i.source_type='import' AND p.source='import' AND p.profile_url=? AND i.source_key IN ({marks})",
                    (profile_url, *chunk),
                ).fetchall()
                found.update(str(row[0]) for row in rows if row and row[0])
            except Exception:
                # Old installations may have a different schema; URL is checked by importer on insert.
                break
        return found

    def import_image(self, image_path: Path, json_path: Path, metadata: dict, source_key: str) -> dict:
        if self.app is None:
            backend_value = self.config.get("backend", "gpu")
            backend = self.module.BACKEND_CPU if backend_value in {"cpu", "processor", "процессор"} else self.module.BACKEND_GPU
            self.status_callback("Инициализация модели FaceSearch…")
            self.app = self.module.create_face_app(backend=backend)
        return self.module.import_image_bundle(
            self.conn, self.app, image_path, json_path,
            delete_source=False, create_crop_files=self.create_crop_files,
            overwrite_existing_crops=False, metadata=metadata, source_key=source_key,
        )

    def build_index(self, progress_callback=None):
        builder = getattr(self.module, "build_faiss_index", None)
        if not callable(builder):
            raise RuntimeError("В установленной версии FaceSearch не найдена build_faiss_index().")
        if progress_callback:
            progress_callback(None, None, "Построение индекса FAISS…")
        builder(self.conn)
        if progress_callback:
            progress_callback(1, 1, "Индекс FAISS создан")

    def close(self):
        if self.conn is not None:
            try:
                self.conn.close()
            except Exception:
                pass
            self.conn = None


def load_settings() -> dict:
    defaults = {
        "facesearch_dir": str(DEFAULT_FACESEARCH_DIR),
        "auto_import": True,
        "create_crop_files": True,
        "build_index_after": False,
        "delete_processed_files": False,
        "browser_profile_dir": str(PROFILE_DIR),
        "chrome_exe_path": default_chrome_executable(),
        "fullsize": True,
    }
    try:
        if APP_SETTINGS_PATH.is_file():
            data = json.loads(APP_SETTINGS_PATH.read_text(encoding="utf-8"))
            if isinstance(data, dict):
                defaults.update({k: data[k] for k in defaults if k in data})
    except Exception as exc:
        print(f"[WARN] Не удалось прочитать настройки окна: {exc}")
    return defaults


def save_settings(values: dict) -> None:
    tmp = APP_SETTINGS_PATH.with_suffix(".tmp")
    tmp.write_text(json.dumps(values, ensure_ascii=False, indent=2), encoding="utf-8")
    tmp.replace(APP_SETTINGS_PATH)


def launch_browser_context(playwright, profile_directory: str, chrome_executable: str):
    profile_path = Path(profile_directory).expanduser()
    profile_path.mkdir(parents=True, exist_ok=True)
    executable = Path(chrome_executable).expanduser()
    if not executable.is_file():
        raise FileNotFoundError(f"Не найден chrome.exe: {executable}")
    return playwright.chromium.launch_persistent_context(
        user_data_dir=str(profile_path.resolve()), executable_path=str(executable.resolve()),
        headless=False, viewport=None, no_viewport=True, locale="ru-RU", accept_downloads=True,
        args=["--start-maximized", "--window-size=1920,1080", "--window-position=0,0"],
    )


def collect_instagram(context, status_callback, progress_callback,
                      fullsize: bool, importer: FaceSearchImporter | None,
                      build_index_after: bool, delete_processed_files: bool) -> dict:
    """Collects images from the profile tab already open in Chrome; does not navigate it."""
    OUT_DIR.mkdir(parents=True, exist_ok=True)
    page = find_open_instagram_page(context)
    try:
        requested_url, profile_url, mode = normalize_instagram_page(page)
    except ValueError as exc:
        raise RuntimeError(f"Открытая вкладка больше не является страницей профиля Instagram: {page.url}. {exc}") from exc
    status_callback(f"Использую открытую вкладку Instagram: {requested_url}")
    progress_callback(None, None, "Ожидаю полной загрузки уже открытой страницы…")
    try:
        page.wait_for_load_state("domcontentloaded", timeout=15000)
    except Exception:
        pass
    # Instagram may redirect an unauthenticated session while the current page loads.
    # Re-check the URL after waiting rather than starting another page.goto() and
    # interrupting Meta's login/redirect chain.
    try:
        requested_url, profile_url, mode = normalize_instagram_page(page)
    except ValueError as exc:
        raise RuntimeError(
            "Instagram перенаправил открытую вкладку на другую страницу "
            f"({page.url}). Вернитесь в Chrome, завершите вход в аккаунт и откройте профиль "
            "или /tagged/ вручную. После этого повторите загрузку."
        ) from exc
    try:
        page.wait_for_load_state("networkidle", timeout=9000)
    except Exception:
        pass
    try:
        requested_url, profile_url, mode = normalize_instagram_page(page)
    except ValueError as exc:
        raise RuntimeError(
            "После загрузки Instagram перенаправил открытую вкладку на другую страницу "
            f"({page.url}). Завершите вход в Instagram в Chrome и откройте профиль или /tagged/ вручную."
        ) from exc
    try:
        page.wait_for_timeout(700)
    except Exception:
        pass

    profile_data = {}
    try:
        profile_data = page.evaluate(PROFILE_DATA_JS) or {}
    except Exception:
        pass
    username = urlsplit(profile_url).path.strip("/").split("/", 1)[0]
    title = str(profile_data.get("title") or "").strip()
    display_name = str(profile_data.get("heading") or "").strip()
    if not display_name and title:
        display_name = re.split(r"\s*\(@", title, maxsplit=1)[0].strip()
    if not display_name:
        display_name = username
    description = str(profile_data.get("description") or "").strip()
    cached_images_by_url = build_existing_image_url_cache(OUT_DIR, profile_url)
    if cached_images_by_url:
        status_callback(f"Локальный кеш найден: {len(cached_images_by_url)} ранее сохранённых URL изображений.")

    entries, scan_warning = collect_grid_entries_with_scroll(page, status_callback, username)
    if not entries:
        current_url = page.url
        try:
            diagnostics = page.evaluate(r"""() => {
              const root = document.querySelector('main') || document;
              const anchors = [...root.querySelectorAll('a[href]')];
              const postLinks = anchors.filter(a => /\/(p|reel|tv)\/[^/]+(?:\/|$)/i.test(new URL(a.href, location.href).pathname));
              const mediaImgs = [...root.querySelectorAll('img')].filter(i => /cdninstagram\.com|fbcdn\.net/i.test(i.currentSrc || i.src || i.getAttribute('src') || ''));
              return {
                hasMain: !!document.querySelector('main'),
                anchors: anchors.length,
                postLinks: postLinks.length,
                mediaImages: mediaImgs.length,
                sampleHref: postLinks[0]?.href || '',
                sampleImage: mediaImgs[0]?.currentSrc || mediaImgs[0]?.src || ''
              };
            }""") or {}
        except Exception as exc:
            diagnostics = {"diagnosticError": str(exc)}
        raise RuntimeError(
            "Не удалось извлечь публикации или прямые ссылки на изображения из DOM Instagram. "
            "Загрузчик проверяет обычные публикации /p/ с изображениями; Reels /reel/ и IGTV /tv/ намеренно пропускаются. "
            "Если счётчики ниже равны нулю, вероятно, Instagram показал заглушку/авторизацию "
            "или ещё не отрисовал контент.\n"
            f"Текущая страница: {current_url}\nДиагностика DOM: {json.dumps(diagnostics, ensure_ascii=False)}"
        )

    status_callback(f"Найдено публикаций: {len(entries)}. Подготавливаю URL изображений…")
    progress_callback(0, max(1, len(entries)), f"Найдено публикаций: {len(entries)}")
    candidates: list[dict] = []
    candidate_url_keys: set[str] = set()
    duplicate_url_candidates = 0
    cached_url_duplicates_skipped = 0
    incomplete_posts: list[dict] = []
    incomplete_post_codes: set[str] = set()
    post_page = None
    try:
        if entries:
            try:
                post_page = context.new_page()
            except Exception as exc:
                print(f"[WARN] Не удалось создать служебную вкладку для чтения публикаций: {exc}")
            # Keep the user's selected profile tab in front while the helper tab
            # navigates through post URLs in the background.
            try:
                page.bring_to_front()
            except Exception:
                pass
        foreign_posts_skipped = 0
        for pos, entry in enumerate(entries, 1):
            code = str(entry.get("shortcode") or "")
            raw_preview = absolute_url(str(entry.get("preview") or ""), requested_url)
            assets = []
            if post_page is not None and str(entry.get("permalink") or "").strip():
                if fullsize:
                    assets = resolve_post_media(
                        post_page, entry, status_callback, pos, len(entries),
                        expected_owner=username if mode == "posts" else None,
                    )
                    # None means the opened /p/ page clearly belongs to another account.
                    # Do not fall back to its grid thumbnail or import it.
                    if assets is None:
                        foreign_posts_skipped += 1
                        progress_callback(pos, max(1, len(entries)), f"Чужая публикация пропущена: {pos}/{len(entries)}")
                        continue
                    if assets:
                        for key in ("caption", "author", "published_at", "likes_count", "comments_count", "location", "expected_slides"):
                            if assets[0].get(key) not in (None, ""):
                                entry[key] = assets[0][key]
                else:
                    post_metadata = resolve_post_metadata(
                        post_page, entry, status_callback, pos, len(entries),
                        expected_owner=username if mode == "posts" else None,
                    )
                    if post_metadata is None:
                        foreign_posts_skipped += 1
                        progress_callback(pos, max(1, len(entries)), f"Чужая публикация пропущена: {pos}/{len(entries)}")
                        continue
                    entry.update({key: value for key, value in post_metadata.items() if value not in (None, "")})
            if not assets:
                if fullsize and entry.get("permalink") and code not in incomplete_post_codes:
                    incomplete_post_codes.add(code)
                    incomplete_posts.append({
                        "shortcode": code,
                        "permalink": entry.get("permalink", ""),
                        "reason": "Не удалось извлечь слайды публикации; использована только миниатюра из сетки профиля."
                    })
                assets = [{
                    "url": raw_preview,
                    "caption": str(entry.get("caption") or ""),
                    "alt": str(entry.get("alt") or ""),
                    "slide": 1,
                    "author": str(entry.get("author") or ""),
                    "published_at": str(entry.get("published_at") or ""),
                    "likes_count": str(entry.get("likes_count") or ""),
                    "comments_count": str(entry.get("comments_count") or ""),
                    "location": str(entry.get("location") or ""),
                    "expected_slides": entry.get("expected_slides"),
                }]
            for asset in assets:
                warning = str(asset.get("carousel_warning") or "").strip()
                if warning and code not in incomplete_post_codes:
                    incomplete_post_codes.add(code)
                    incomplete_posts.append({
                        "shortcode": code,
                        "permalink": entry.get("permalink", ""),
                        "expected_slides": asset.get("expected_slides"),
                        "collected_slides": len(assets),
                        "reason": warning,
                    })
            local_seen: set[str] = set()
            for asset in assets:
                img_url = absolute_url(str(asset.get("url") or ""), requested_url)
                if not img_url or img_url in local_seen:
                    continue
                if not re.search(r"cdninstagram\.com|fbcdn\.net", urlsplit(img_url).netloc, re.I):
                    # Keep only Instagram's media CDN. Avoid avatars and UI resources.
                    continue
                local_seen.add(img_url)
                url_key = normalized_url_key(img_url)
                # Identical CDN URL can be referenced by more than one grid tile/post.
                # Keep the first entry and avoid downloading the exact same URL again.
                if url_key in candidate_url_keys:
                    duplicate_url_candidates += 1
                    continue
                candidate_url_keys.add(url_key)
                asset.update({"shortcode": code, "kind": entry.get("kind", "p"), "permalink": entry.get("permalink", ""),
                              "preview": raw_preview, "entry_alt": str(entry.get("alt") or "")})
                candidates.append(asset)
            progress_callback(pos, max(1, len(entries)), f"Разбор публикаций: {pos}/{len(entries)}")
    finally:
        if post_page is not None:
            try:
                post_page.close()
            except Exception:
                pass
        try:
            page.bring_to_front()
        except Exception:
            pass

    if not candidates:
        raise RuntimeError("Instagram показал список публикаций, но URL изображений получить не удалось.")

    # Stable key makes repeated runs idempotent even when signed CDN URLs rotate.
    for item in candidates:
        base_key = f"{profile_url}\0{item['shortcode']}\0{item.get('slide', 1)}"
        item["source_key"] = "instagram:" + hashlib.sha256(base_key.encode("utf-8")).hexdigest()
    db_existing = set()
    if importer:
        db_existing = importer.existing_source_keys(profile_url, [p["source_key"] for p in candidates])

    user_slug = safe_filename(username)
    total = len(candidates)
    downloaded = reused = imported = db_skipped = failed = 0
    duplicate_images = duplicate_url_candidates
    db_added_faces = db_updated_records = db_no_face = db_noop = import_failed = 0
    import_error_samples: list[str] = []
    seen_exact_hashes: dict[str, Path] = {}
    seen_dhash_buckets: dict[tuple[int, int], list[tuple[str, int, float, Path]]] = {}
    download_failures: list[dict] = []
    run_report_path = OUT_DIR / f"{user_slug}_instagram_run_report.json"

    def save_run_report(final: bool = False) -> None:
        report = {
            "username": username,
            "profile_url": profile_url,
            "generated_at_utc": datetime.now(timezone.utc).isoformat(),
            "finished": bool(final),
            "profile_posts_found": len(entries),
            "candidate_images": total,
            "url_key_policy": "Instagram CDN query tokens are ignored for stable cache identity; non-CDN queries are preserved.",
            "downloaded_this_run": downloaded,
            "reused_from_cache": reused,
            "cache_entries_found_at_start": len(cached_images_by_url),
            "cached_url_duplicates_skipped": cached_url_duplicates_skipped,
            "duplicate_images_skipped": duplicate_images,
            "download_failures_count": len(download_failures),
            "download_failures": download_failures,
            "incomplete_posts_count": len(incomplete_posts),
            "incomplete_posts": incomplete_posts,
            "profile_scan_warning": scan_warning,
            "profile_scan_note": (
                "Остановка по стабильной высоте страницы и отсутствию новых URL является эвристикой; "
                "Instagram не предоставляет этому сборщику подтверждённый полный список всех публикаций."
                if not scan_warning else "Сканирование завершено с предупреждением; проверьте журнал и число найденных публикаций."
            ),
            "import_attempts": imported,
            "db_added_faces": db_added_faces,
            "db_updated_records": db_updated_records,
            "db_no_face": db_no_face,
            "db_noop": db_noop,
            "db_skipped_existing_source": db_skipped,
            "db_import_failures": import_failed,
            "db_import_error_samples": import_error_samples,
        }
        tmp_report = run_report_path.with_suffix(".tmp")
        tmp_report.write_text(json.dumps(report, ensure_ascii=False, indent=2), encoding="utf-8")
        tmp_report.replace(run_report_path)

    save_run_report(final=False)
    for idx, item in enumerate(candidates, 1):
        code = str(item.get("shortcode") or "unknown")
        slide = int(item.get("slide") or 1)
        # Zero-padded slide number ensures stable filename ordering in Explorer.
        stem = OUT_DIR / f"{user_slug}_{safe_filename(code, 40)}_{slide:02d}"
        json_path = stem.with_suffix(".json")
        previous_image = find_existing_image(stem)
        previous_metadata = {}
        if json_path.is_file():
            try:
                previous_metadata = json.loads(json_path.read_text(encoding="utf-8"))
            except Exception:
                previous_metadata = {}

        current_url = str(item.get("url") or "")
        current_url_key = normalized_url_key(current_url)
        image_path = previous_image
        previous_json_existed = json_path.is_file()
        had_image_before = bool(previous_image and previous_image.is_file())
        downloaded_now = False
        same_cached_image = bool(
            image_path and previous_metadata.get("url_source") == profile_url
            and normalized_url_key(str(previous_metadata.get("url_image") or "")) == current_url_key
        )
        status_callback(f"Изображения: {idx}/{total} — {code}, слайд {slide}")
        if same_cached_image:
            reused += 1
        else:
            cached_match = cached_images_by_url.get(current_url_key)
            if image_path is None and cached_match is not None and cached_match.is_file():
                # The same stable CDN asset was already saved under another post/slide
                # filename in a previous run. Do not download it again or import it twice.
                duplicate_images += 1
                cached_url_duplicates_skipped += 1
                status_callback(f"Повтор из локального кеша пропущен: {code}, слайд {slide}")
                progress_callback(idx, total, f"Дубликат из кеша: {idx}/{total}")
                save_run_report(final=False)
                continue
            image_path, download_error = download_one(context, current_url, stem, requested_url)
            if image_path is None:
                failed += 1
                download_failures.append({
                    "shortcode": code,
                    "slide": slide,
                    "permalink": str(item.get("permalink") or ""),
                    "image_url": current_url,
                    "error": download_error or "Причина не определена",
                })
                save_run_report(final=False)
                progress_callback(idx, total, f"Ошибка загрузки {idx}/{total}: {download_error or 'неизвестная ошибка'}")
                continue
            downloaded += 1
            downloaded_now = True
            if previous_image and previous_image != image_path:
                try:
                    previous_image.unlink(missing_ok=True)
                except OSError:
                    pass
        # Keep the URL cache current during the run.
        if image_path and image_path.is_file() and current_url_key:
            cached_images_by_url.setdefault(current_url_key, image_path)

        # De-duplicate by actual image bytes, plus a conservative perceptual hash
        # for the same picture served in a different size/encoding. This catches
        # duplicates across separate posts, not only repeated URLs in one carousel.
        content_sha, perceptual_hash, aspect_ratio = fingerprint_image(image_path)
        duplicate_path = find_duplicate_fingerprint(
            content_sha, perceptual_hash, aspect_ratio,
            seen_exact_hashes, seen_dhash_buckets,
        )
        if duplicate_path is not None:
            duplicate_images += 1
            # Remove only a redundant file created during this run. Keep old cached
            # files intact in case they are already referenced by a prior database import.
            if downloaded_now and not had_image_before:
                try:
                    image_path.unlink(missing_ok=True)
                except OSError:
                    pass
                if not previous_json_existed:
                    try:
                        json_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            status_callback(f"Повтор изображения пропущен: {code}, слайд {slide}")
            progress_callback(idx, total, f"Дубликат пропущен: {idx}/{total}")
            continue
        remember_fingerprint(
            content_sha, perceptual_hash, aspect_ratio, image_path,
            seen_exact_hashes, seen_dhash_buckets,
        )

        caption = str(item.get("caption") or "").strip()
        alt = str(item.get("alt") or item.get("entry_alt") or "").strip()
        info = {}
        author = str(item.get("author") or "").strip().lstrip("@")
        # Do not repeat the profile username on ordinary profile posts. The author
        # is useful for /tagged/ results or collaborative posts by another account.
        if author and (mode == "tagged" or author.lower() != username.lower()):
            info["Автор публикации"] = "@" + author

        published_at = str(item.get("published_at") or "").strip()
        if published_at:
            try:
                parsed_date = datetime.fromisoformat(published_at.replace("Z", "+00:00"))
                if parsed_date.tzinfo is not None:
                    parsed_date = parsed_date.astimezone()
                info["Дата публикации"] = parsed_date.strftime("%d.%m.%Y %H:%M")
            except (TypeError, ValueError, OverflowError):
                info["Дата публикации"] = published_at[:40]

        likes_count = str(item.get("likes_count") or "").strip()
        if likes_count:
            info["Отметки «Нравится»"] = likes_count
        comments_count = str(item.get("comments_count") or "").strip()
        if comments_count:
            info["Комментарии"] = comments_count

        if caption:
            info["Описание публикации"] = caption[:3000]
        if alt and alt.casefold() != caption.casefold():
            info["Описание изображения"] = alt[:1000]

        location = str(item.get("location") or "").strip()
        if location:
            info["Место публикации"] = location[:250]

        hashtags = list(dict.fromkeys(re.findall(r"(?<!\w)#[\w]+", caption, flags=re.UNICODE)))
        if hashtags:
            info["Хэштеги"] = " ".join(hashtags[:40])

        expected_slides = item.get("expected_slides")
        if expected_slides:
            try:
                expected_count = int(expected_slides)
            except (TypeError, ValueError):
                expected_count = 0
            if expected_count > 1:
                info["Слайд"] = f"{slide} из {expected_count}"
        metadata = {"url_source": profile_url, "name": display_name, "url_image": current_url, "info": info}
        write_metadata(json_path, metadata)

        if importer:
            if item["source_key"] in db_existing:
                db_skipped += 1
                status_callback(f"Уже есть в FaceSearch: {code}, слайд {slide}")
                if delete_processed_files:
                    try:
                        image_path.unlink(missing_ok=True)
                        json_path.unlink(missing_ok=True)
                    except OSError:
                        pass
            else:
                try:
                    import_status = importer.import_image(image_path, json_path, metadata, item["source_key"])
                    imported += 1
                    db_existing.add(item["source_key"])
                    added_records = int(import_status.get("added", 0) or 0)
                    updated_records = int(import_status.get("updated", 0) or 0)
                    # Current FaceSearch reports newly-created face rows in `added`;
                    # older compatible versions may expose only `faces`.
                    if "added" not in import_status:
                        added_records = int(import_status.get("faces", 0) or 0)
                    db_added_faces += added_records
                    db_updated_records += updated_records
                    message = str(import_status.get("message") or "").strip()
                    detected_faces = import_status.get("detected_faces")
                    if detected_faces == 0 or "лицо не найдено" in message.lower():
                        db_no_face += 1
                    elif added_records == 0 and updated_records == 0:
                        db_noop += 1
                    print(f"[DB-IMPORT] {image_path.name}: {import_status}")
                    if delete_processed_files:
                        image_path.unlink(missing_ok=True)
                        json_path.unlink(missing_ok=True)
                except Exception as exc:
                    import_failed += 1
                    if len(import_error_samples) < 5:
                        import_error_samples.append(f"{image_path.name}: {exc}")
                    print(f"[DB-IMPORT-ERROR] {image_path.name}: {exc}")
                    status_callback(f"Ошибка импорта {image_path.name}: {exc}")
        progress_callback(idx, total, f"Изображения: {idx}/{total}")

    if importer and build_index_after and (db_added_faces or db_updated_records):
        importer.build_index(progress_callback=progress_callback)
    save_run_report(final=True)
    progress_callback(1, 1, "Готово")
    return {
        "posts": len(entries), "media": len(candidates), "downloaded": downloaded,
        "reused": reused, "duplicate_images": duplicate_images,
        "imported": imported, "db_added_faces": db_added_faces,
        "db_updated_records": db_updated_records, "db_no_face": db_no_face,
        "db_noop": db_noop, "db_skipped": db_skipped, "failed": failed,
        "foreign_posts_skipped": foreign_posts_skipped,
        "import_failed": import_failed, "import_error_samples": import_error_samples,
        "incomplete_posts": incomplete_posts, "scan_warning": scan_warning,
        "run_report_path": str(run_report_path),
        "username": username, "mode": mode,
    }


def main():
    if sync_playwright is None:
        root = tk.Tk()
        root.withdraw()
        messagebox.showerror("Playwright не установлен", "Установите зависимости через setup.bat в папке загрузчика.")
        root.destroy()
        return

    OUT_DIR.mkdir(parents=True, exist_ok=True)
    saved = load_settings()
    commands: queue.Queue[tuple] = queue.Queue()
    browser_ready = threading.Event()
    browser_state = {"error": None}

    root = tk.Tk()
    root.title("Instagram — загрузчик изображений и импорт FaceSearch")
    root.geometry("980x790")
    root.minsize(850, 700)
    root.columnconfigure(0, weight=1)
    root.rowconfigure(0, weight=1)
    frame = ttk.Frame(root, padding=12)
    frame.grid(row=0, column=0, sticky="nsew")
    frame.columnconfigure(0, weight=1)

    ttk.Label(frame, text=(
        "Откройте нужный профиль Instagram в запущенном Chrome — обычную страницу профиля "
        "или вкладку /tagged/. Загрузчик возьмёт адрес из уже открытой вкладки и не будет "
        "переходить по ссылке сам, чтобы не прерывать авторизацию или редиректы Instagram."
    ), wraplength=900).grid(row=0, column=0, sticky="w", pady=(0, 8))

    paths_frame = ttk.LabelFrame(frame, text="Настройки FaceSearch и браузера", padding=8)
    paths_frame.grid(row=1, column=0, sticky="ew", pady=(0, 8))
    paths_frame.columnconfigure(1, weight=1)
    facesearch_dir_var = tk.StringVar(value=str(saved.get("facesearch_dir") or DEFAULT_FACESEARCH_DIR))
    browser_profile_dir_var = tk.StringVar(value=str(saved.get("browser_profile_dir") or PROFILE_DIR))
    chrome_exe_path_var = tk.StringVar(value=str(saved.get("chrome_exe_path") or default_chrome_executable()))
    auto_import_var = tk.BooleanVar(value=bool(saved.get("auto_import", True)))
    create_crop_files_var = tk.BooleanVar(value=bool(saved.get("create_crop_files", True)))
    build_index_after_var = tk.BooleanVar(value=bool(saved.get("build_index_after", False)))
    delete_processed_files_var = tk.BooleanVar(value=bool(saved.get("delete_processed_files", False)))
    fullsize_var = tk.BooleanVar(value=bool(saved.get("fullsize", True)))

    ttk.Label(paths_frame, text="Папка FaceSearch:").grid(row=0, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(paths_frame, textvariable=facesearch_dir_var).grid(row=0, column=1, sticky="ew", pady=3)
    ttk.Button(paths_frame, text="Выбрать…", command=lambda: _choose_dir(facesearch_dir_var, root, "Выберите папку FaceSearch")).grid(row=0, column=2, padx=(8, 0), pady=3)
    ttk.Label(paths_frame, text="Папка профиля Chrome:").grid(row=1, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(paths_frame, textvariable=browser_profile_dir_var).grid(row=1, column=1, sticky="ew", pady=3)
    ttk.Button(paths_frame, text="Выбрать…", command=lambda: _choose_dir(browser_profile_dir_var, root, "Выберите папку профиля Chrome")).grid(row=1, column=2, padx=(8, 0), pady=3)
    ttk.Label(paths_frame, text="Путь к chrome.exe:").grid(row=2, column=0, sticky="w", padx=(0, 8), pady=3)
    ttk.Entry(paths_frame, textvariable=chrome_exe_path_var).grid(row=2, column=1, sticky="ew", pady=3)
    ttk.Button(paths_frame, text="Выбрать файл…", command=lambda: _choose_file(chrome_exe_path_var, root)).grid(row=2, column=2, padx=(8, 0), pady=3)
    ttk.Button(paths_frame, text="Проверить подключение к базе данных FaceSearch", command=lambda: test_database_connection()).grid(
        row=3, column=0, columnspan=3, sticky="ew", pady=(6, 2)
    )

    options = ttk.LabelFrame(frame, text="Параметры загрузки и импорта", padding=8)
    options.grid(row=2, column=0, sticky="ew", pady=(0, 8))
    ttk.Checkbutton(options, text="Получать все изображения каруселей в максимальном доступном разрешении (медленнее)", variable=fullsize_var).pack(anchor="w", pady=1)
    ttk.Checkbutton(options, text="Импортировать загруженные изображения в активный профиль FaceSearch", variable=auto_import_var).pack(anchor="w", pady=1)
    crop_check = ttk.Checkbutton(options, text="Создавать CROP-файлы при импорте", variable=create_crop_files_var)
    crop_check.pack(anchor="w", pady=1)
    index_check = ttk.Checkbutton(options, text="Построить индекс FAISS после импорта", variable=build_index_after_var)
    index_check.pack(anchor="w", pady=1)
    delete_check = ttk.Checkbutton(options, text="Удалять локальные изображения и JSON после успешного импорта/если источник уже в БД", variable=delete_processed_files_var)
    delete_check.pack(anchor="w", pady=1)

    config_status_var = tk.StringVar(value="Папка FaceSearch: " + str(facesearch_dir_var.get()))
    ttk.Label(frame, textvariable=config_status_var, wraplength=900).grid(row=3, column=0, sticky="ew", pady=(0, 5))
    status_var = tk.StringVar(value="Запускается Chrome…")
    ttk.Label(frame, textvariable=status_var, wraplength=900).grid(row=4, column=0, sticky="ew", pady=(0, 3))
    progress_label_var = tk.StringVar(value="Ожидание задания")
    ttk.Label(frame, textvariable=progress_label_var, wraplength=900).grid(row=5, column=0, sticky="ew", pady=(0, 3))
    progress_bar = ttk.Progressbar(frame, mode="determinate", maximum=100, value=0)
    progress_bar.grid(row=6, column=0, sticky="ew", pady=(0, 8))
    progress_state = {"mode": "determinate"}

    ttk.Label(frame, text=f"Папка загрузки: {OUT_DIR}", wraplength=900).grid(row=8, column=0, sticky="w", pady=(6, 0))

    def persist_settings():
        save_settings({
            "facesearch_dir": facesearch_dir_var.get().strip(),
            "auto_import": auto_import_var.get(),
            "create_crop_files": create_crop_files_var.get(),
            "build_index_after": build_index_after_var.get(),
            "delete_processed_files": delete_processed_files_var.get(),
            "browser_profile_dir": browser_profile_dir_var.get().strip(),
            "chrome_exe_path": chrome_exe_path_var.get().strip(),
            "fullsize": fullsize_var.get(),
        })

    def set_status(message: str):
        try:
            root.after(0, lambda v=message: status_var.set(v))
        except tk.TclError:
            pass

    def set_config_status(message: str):
        try:
            root.after(0, lambda v=message: config_status_var.set(v))
        except tk.TclError:
            pass

    def show_database_test_result(success: bool, message: str):
        def display():
            try:
                if success:
                    messagebox.showinfo("Проверка базы FaceSearch", message, parent=root)
                else:
                    messagebox.showerror("Проверка базы FaceSearch", message, parent=root)
            except tk.TclError:
                pass
        try:
            root.after(0, display)
        except tk.TclError:
            pass

    def test_database_connection():
        if not facesearch_dir_var.get().strip():
            messagebox.showerror("Проверка базы FaceSearch", "Укажите каталог FaceSearch.", parent=root)
            return
        persist_settings()
        commands.put(("test_db", facesearch_dir_var.get().strip()))
        set_status("Проверяю настоящее подключение к активной базе FaceSearch…")

    def set_progress(current=None, total=None, message=""):
        def update():
            try:
                if current is None or total is None:
                    if progress_state["mode"] != "indeterminate":
                        progress_bar.stop()
                        progress_bar.configure(mode="indeterminate")
                        progress_bar.start(12)
                        progress_state["mode"] = "indeterminate"
                    progress_label_var.set(message or "Выполняется операция…")
                else:
                    total_safe = max(1, int(total))
                    current_safe = min(total_safe, max(0, int(current)))
                    if progress_state["mode"] != "determinate":
                        progress_bar.stop()
                        progress_bar.configure(mode="determinate", maximum=total_safe)
                        progress_state["mode"] = "determinate"
                    elif int(float(progress_bar.cget("maximum") or 1)) != total_safe:
                        progress_bar.configure(maximum=total_safe)
                    progress_bar["value"] = current_safe
                    pct = int(100 * current_safe / total_safe)
                    progress_label_var.set(f"{pct}% — {message or 'Обработка'}")
            except tk.TclError:
                pass
        try:
            root.after(0, update)
        except tk.TclError:
            pass

    def update_options_state(*_):
        state = "normal" if auto_import_var.get() else "disabled"
        for widget in (crop_check, index_check, delete_check):
            widget.configure(state=state)
        persist_settings()
    auto_import_var.trace_add("write", update_options_state)
    update_options_state()

    def check_configuration(show_dialog=True):
        try:
            cfg = load_face_search_config(facesearch_dir_var.get())
            profile = cfg["profile"]
            kind = str(profile.get("type") or "sqlite")
            target = str(profile.get("database") or profile.get("path") or "")
            host = str(profile.get("host") or "")
            text = f"FaceSearch: активный профиль «{cfg['active_name']}» — {kind}: {target}"
            if host and kind.lower() != "sqlite":
                text += f" @ {host}"
            config_status_var.set(text)
            persist_settings()
            return cfg
        except Exception as exc:
            config_status_var.set(f"Ошибка конфигурации FaceSearch: {exc}")
            if show_dialog:
                messagebox.showerror("Настройки FaceSearch", str(exc), parent=root)
            return None

    def start_download():
        if not browser_ready.is_set():
            messagebox.showinfo("Chrome", "Chrome ещё запускается. Повторите немного позже.", parent=root)
            return
        if browser_state.get("error"):
            messagebox.showerror("Chrome", str(browser_state["error"]), parent=root)
            return
        if auto_import_var.get() and check_configuration(show_dialog=True) is None:
            return
        persist_settings()
        commands.put(("collect", fullsize_var.get(), facesearch_dir_var.get().strip(),
                      auto_import_var.get(), create_crop_files_var.get(), build_index_after_var.get(), delete_processed_files_var.get()))
        set_status("Задание отправлено. Проверяю открытую вкладку Instagram…")
        set_progress(None, None, "Подготовка к загрузке…")

    ttk.Button(frame, text="Загрузить с открытой страницы и импортировать в FaceSearch", command=start_download).grid(row=7, column=0, sticky="ew", ipady=7)

    def browser_worker():
        try:
            with sync_playwright() as playwright:
                context = None
                try:
                    context = launch_browser_context(playwright, browser_profile_dir_var.get(), chrome_exe_path_var.get())
                    open_instagram_home_tab(context)
                    browser_state["error"] = None
                    set_status("Chrome готов, открыта https://www.instagram.com/. Откройте нужный профиль или /tagged/ именно в этом окне.")
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
                            if context:
                                context.close()
                            context = launch_browser_context(playwright, command[1], command[2])
                            open_instagram_home_tab(context)
                            browser_state["error"] = None
                            set_status("Chrome перезапущен, открыта https://www.instagram.com/. Откройте профиль или /tagged/ в этом окне.")
                        except Exception as exc:
                            context = None
                            browser_state["error"] = str(exc)
                            set_status(f"Ошибка запуска Chrome: {exc}")
                        finally:
                            browser_ready.set()
                        continue
                    if command[0] == "test_db":
                        probe = None
                        try:
                            fs_dir = command[1]
                            probe = FaceSearchImporter(fs_dir, set_status, create_crop_files=False, prepare_schema=False)
                            probe.conn.execute("SELECT 1").fetchone()
                            profile = probe.config["profile"]
                            kind = str(profile.get("type") or "sqlite").strip()
                            target = str(profile.get("database") or profile.get("path") or "")
                            host = str(profile.get("host") or "").strip()
                            detail = f"Профиль: {probe.config['active_name']}\nТип БД: {kind}\nБаза: {target or '(по настройкам профиля)'}"
                            if host and kind.lower() != "sqlite":
                                detail += f"\nСервер: {host}"
                            detail += "\n\nПодключение и тестовый запрос SELECT 1 выполнены успешно."
                            set_config_status(f"Подключение к БД успешно: {probe.config['active_name']} ({kind}: {target or host}).")
                            set_status("Проверка подключения к FaceSearch завершена успешно.")
                            show_database_test_result(True, detail)
                        except Exception as exc:
                            set_config_status(f"Ошибка подключения к БД FaceSearch: {exc}")
                            set_status(f"Проверка подключения к БД не пройдена: {exc}")
                            show_database_test_result(False, f"Не удалось подключиться к активной базе FaceSearch.\n\n{exc}")
                        finally:
                            if probe:
                                probe.close()
                        continue
                    if command[0] == "collect":
                        if context is None:
                            set_status("Chrome не запущен. Проверьте путь к chrome.exe.")
                            continue
                        _, fullsize, fs_dir, auto_import, create_crop, build_index, delete_files = command
                        importer = None
                        try:
                            if auto_import:
                                importer = FaceSearchImporter(fs_dir, set_status, create_crop_files=create_crop)
                                set_status(f"FaceSearch подключён: профиль «{importer.config['active_name']}».")
                            result = collect_instagram(context, set_status, set_progress, fullsize,
                                                       importer, build_index, delete_files)
                            summary = (
                                f"Готово: публикаций {result['posts']}, кандидатов на изображения {result['media']}; "
                                f"скачиваний {result['downloaded']}, из кеша {result['reused']}, "
                                f"повторов изображений пропущено {result['duplicate_images']}; "
                                f"публикаций других аккаунтов пропущено {result.get('foreign_posts_skipped', 0)}; "
                                f"ошибок загрузки {result['failed']}; "
                                f"публикаций с предупреждениями о полноте {len(result.get('incomplete_posts', []))}."
                            )
                            if result.get('scan_warning'):
                                summary += " ВНИМАНИЕ по сканированию: " + result['scan_warning']
                            summary += f" Отчёт полноты и ошибок: {result['run_report_path']}."
                            if auto_import:
                                summary += (
                                    f" FaceSearch: новых записей лиц {result['db_added_faces']}, "
                                    f"обновлено записей {result['db_updated_records']}, "
                                    f"уже в БД по источнику {result['db_skipped']}, "
                                    f"без лиц {result['db_no_face']}, без новых записей {result['db_noop']}, "
                                    f"ошибок импорта {result['import_failed']}."
                                )
                                if result['import_error_samples']:
                                    summary += " Первая ошибка импорта: " + result['import_error_samples'][0][:220]
                            else:
                                summary += " Импорт в FaceSearch выключен."
                            summary += f" Папка: {OUT_DIR}"
                            set_status(summary)
                            set_progress(1, 1, "Загрузка завершена")
                        except Exception as exc:
                            print(f"[ERROR] {exc}")
                            set_status(f"Ошибка: {exc}")
                            set_progress(0, 1, f"Ошибка: {exc}")
                        finally:
                            if importer:
                                importer.close()
                if context:
                    context.close()
        except Exception as exc:
            browser_state["error"] = str(exc)
            browser_ready.set()
            set_status(f"Ошибка браузера: {exc}")

    def on_close():
        persist_settings()
        commands.put(("quit",))
        root.destroy()

    root.protocol("WM_DELETE_WINDOW", on_close)
    worker = threading.Thread(target=browser_worker, daemon=True)
    worker.start()
    if auto_import_var.get():
        check_configuration(show_dialog=False)
    root.mainloop()


def _choose_dir(variable, parent, title):
    current = variable.get().strip()
    initial = current if current and Path(current).expanduser().is_dir() else str(Path.home())
    selected = filedialog.askdirectory(parent=parent, title=title, initialdir=initial, mustexist=False)
    if selected:
        variable.set(selected)


def _choose_file(variable, parent):
    current = variable.get().strip()
    initial = str(Path(current).expanduser().parent) if current and Path(current).expanduser().parent.is_dir() else str(Path.home())
    selected = filedialog.askopenfilename(parent=parent, title="Выберите chrome.exe", initialdir=initial,
                                          filetypes=[("Google Chrome", "chrome.exe"), ("Исполняемые файлы", "*.exe"), ("Все файлы", "*.*")])
    if selected:
        variable.set(selected)


if __name__ == "__main__":
    main()
