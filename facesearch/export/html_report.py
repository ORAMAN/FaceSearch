# -*- coding: utf-8 -*-
"""Генерация автономной HTML-галереи результатов поиска FaceSearch."""
from __future__ import annotations

import html
import json
import os
import re
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import quote, unquote, urlparse
from urllib.request import url2pathname

from ..config import APP_DIR as DEFAULT_APP_DIR


def _default_load_image(path: Path):
    """Локальная загрузка изображения для HTML-экспорта старых записей."""
    try:
        import cv2
        return cv2.imread(str(path))
    except Exception:
        return None

def _safe_file_href(path: Path) -> str:
    try:
        return path.resolve().as_uri()
    except Exception:
        return ""


def _local_file_href_for_html(path: Path, html_path: Path) -> str:
    """Строит ссылку на локальный файл относительно HTML.

    Для локально открываемого HTML относительные file-URL обычно надёжнее
    абсолютных file:///C:/... в браузерах. Пути кодируются как URL, поэтому
    пробелы и кириллица не ломают src.
    """
    try:
        path = path.resolve()
        html_dir = html_path.resolve().parent
        relative = os.path.relpath(path, html_dir).replace(os.sep, "/")

        # urllib.parse.quote кодирует пробелы, кириллицу, # и другие
        # специальные символы, но сохраняет разделители каталогов.
        from urllib.parse import quote
        return quote(relative, safe="/.:~-_()")
    except (OSError, ValueError, TypeError):
        return _safe_file_href(path)


def vk_last_seen_text(timestamp: Any) -> str:
    try:
        value = int(timestamp)
    except (TypeError, ValueError):
        return "неизвестно"

    try:
        return datetime.fromtimestamp(value).strftime(
            "%Y-%m-%d %H:%M:%S"
        )
    except (OverflowError, OSError, ValueError):
        return "неизвестно"


def vk_verified_text(verified: Any, is_verified: Any) -> str:
    return "да" if bool(verified) or bool(is_verified) else "нет"


def _site_display_data(source_url: str | None) -> tuple[str, str, str]:
    """Возвращает (URL источника, отображаемый адрес сайта, favicon URL)."""
    value = str(source_url or "").strip()
    if not value:
        return "", "", ""

    try:
        parsed = urlparse(value)
        if not parsed.scheme or not parsed.netloc:
            return value, value, ""

        host = parsed.netloc.split("@")[-1].split(":")[0]
        display_host = host
        favicon_url = f"{parsed.scheme}://{parsed.netloc}/favicon.ico"
        return value, display_host, favicon_url
    except Exception:
        return value, value, ""


def _display_face_id(result: dict[str, Any]) -> str:
    """Возвращает ID записи лица в виде #00000000, без имени файла."""
    value = result.get("face_id")
    if value is None:
        value = result.get("id")
    try:
        face_id = int(value)
    except (TypeError, ValueError):
        return "#ID неизвестен"
    if face_id < 0:
        return "#ID неизвестен"
    return f"#{face_id:08d}"


def generate_html_gallery(
    results,
    query_image: Path,
    output_file: str = "face_search_results.html",
    use_local_crops: bool = True,
    *,
    crop_dir: Path | str | None = None,
    crop_url: str | None = None,
    app_dir: Path | str | None = None,
    load_image_fn=None,
    cv2_module=None,
    theme: str = "dark",
    retry_missing_crops: bool = False,
):
    APP_DIR = Path(app_dir).resolve() if app_dir else DEFAULT_APP_DIR
    CROP_DIR = Path(crop_dir).resolve() if crop_dir else (APP_DIR / "face_images")
    crop_url_base = str(crop_url or "").strip().rstrip("/")
    if crop_url_base:
        parsed_crop_url = urlparse(crop_url_base)
        if parsed_crop_url.scheme.lower() not in {"http", "https"} or not parsed_crop_url.netloc:
            crop_url_base = ""
    load_image = load_image_fn or _default_load_image
    cv2 = cv2_module
    if cv2 is None and use_local_crops:
        try:
            import cv2 as cv2_imported
            cv2 = cv2_imported
        except ImportError:
            cv2 = None
    query_path = query_image.resolve()
    output_path = Path(output_file).resolve()
    cards = []

    for result in results:
        score = result["score"]

        # images.path для обработанных лиц теперь хранит только путь
        # относительно активного CROP_DIR (например 1000/1234.jpg).
        # Поэтому при формировании HTML сначала разрешаем его относительно
        # CROP_DIR. Старые абсолютные пути и старые APP_DIR-relative пути
        # также поддерживаются для совместимости.
        raw_result_path = str(result.get("path") or "").strip()
        raw_path = Path(raw_result_path).expanduser()
        if raw_path.is_absolute():
            path = raw_path.resolve()
        else:
            crop_candidate = (CROP_DIR / raw_path).resolve()
            if crop_candidate.is_file():
                path = crop_candidate
            else:
                path = (APP_DIR / raw_path).resolve()

        x1, y1, x2, y2 = result["bbox"]
        source_type = str(result.get("source_type") or "local")
        is_local = source_type == "local"
        is_import = source_type == "import"

        # Под изображением показываем ID записи из БД, а не имя файла.
        display_face_id = html.escape(_display_face_id(result), quote=True)
        filename = display_face_id
        filename_html = (
            f'<div class="filename" title="{display_face_id}">{display_face_id}</div>'
        )

        # URL строится из относительного images.path. Абсолютные старые пути
        # разрешены только если они действительно находятся внутри CROP_DIR.
        # Так исходные изображения случайно не подменяются URL-каталогом CROP.
        remote_crop_href = ""
        future_local_crop_href = ""
        future_local_crop_path = None
        crop_relative = None
        normalized_db_path = raw_result_path.replace("\\", "/").strip()
        if use_local_crops and normalized_db_path:
            candidate_posix = Path(normalized_db_path)
            if not candidate_posix.is_absolute() and not re.match(r"^[A-Za-z]:/", normalized_db_path):
                normalized_parts = [part for part in normalized_db_path.split("/") if part not in ("", ".")]
                if normalized_parts and ".." not in normalized_parts:
                    # Основной формат CROP: <bucket>/<image_id>.jpg.
                    if (
                        len(normalized_parts) >= 2
                        and normalized_parts[-2].isdigit()
                        and Path(normalized_parts[-1]).stem.isdigit()
                        and Path(normalized_parts[-1]).suffix.lower() in {".jpg", ".jpeg"}
                    ):
                        crop_relative = "/".join(normalized_parts)
            elif path.is_absolute():
                try:
                    crop_relative = path.resolve().relative_to(CROP_DIR.resolve()).as_posix()
                except (OSError, ValueError):
                    crop_relative = None

            if crop_relative:
                try:
                    future_local_crop_path = (CROP_DIR / Path(crop_relative)).resolve()
                    future_local_crop_path.relative_to(CROP_DIR.resolve())
                    if not future_local_crop_path.is_file():
                        future_local_crop_href = _local_file_href_for_html(
                            future_local_crop_path, output_path
                        )
                except (OSError, ValueError):
                    future_local_crop_path = None
                    future_local_crop_href = ""

            if crop_url_base and crop_relative:
                remote_crop_href = (
                    crop_url_base + "/" + quote(crop_relative, safe="/._~-")
                )

        # В режиме локальных CROP используем физический файл, если он есть.
        # При наличии URL сперва показываем сетевую копию, а локальную используем
        # как запасную, если сетевой файл пока не опубликован.
        crop_href = ""
        local_image_exists = path.is_file()

        if use_local_crops and local_image_exists:
            try:
                crop_dir = output_path.parent / "face_crops"
                crop_dir.mkdir(parents=True, exist_ok=True)
                crop_file = crop_dir / f"result_{result['rank']:04d}.jpg"

                # Новые записи images.path уже указывают непосредственно на
                # сохранённый CROP. Не вырезаем его повторно по bbox: bbox
                # также хранится в координатах CROP и повторная обрезка могла
                # дать пустой/неверный результат. Просто переносим готовый
                # CROP в папку рядом с HTML.
                raw_result_path = str(result.get("path") or "").strip()
                is_relative_crop = (
                    bool(raw_result_path)
                    and not Path(raw_result_path).expanduser().is_absolute()
                    and path == (CROP_DIR / Path(raw_result_path).expanduser()).resolve()
                    and path.is_file()
                )

                if is_relative_crop:
                    shutil.copy2(path, crop_file)
                else:
                    # Старые записи: сохраняем прежнее поведение и строим
                    # CROP по bbox исходного изображения.
                    image = load_image(path)
                    if image is not None:
                        h, w = image.shape[:2]
                        x1i = max(0, int(x1))
                        y1i = max(0, int(y1))
                        x2i = min(w, int(x2))
                        y2i = min(h, int(y2))

                        if x2i > x1i and y2i > y1i:
                            crop = image[y1i:y2i, x1i:x2i]
                            crop_h, crop_w = crop.shape[:2]
                            scale = 300 / max(crop_w, crop_h)
                            crop = cv2.resize(
                                crop,
                                None,
                                fx=scale,
                                fy=scale,
                                interpolation=(
                                    cv2.INTER_CUBIC
                                    if scale > 1
                                    else cv2.INTER_AREA
                                ),
                            )
                            cv2.imwrite(
                                str(crop_file),
                                crop,
                                [cv2.IMWRITE_JPEG_QUALITY, 95],
                            )
                        else:
                            crop_file = None
                    else:
                        crop_file = None

                if crop_file is not None and crop_file.is_file():
                    crop_href = crop_file.relative_to(
                        output_path.parent
                    ).as_posix()

            except Exception as exc:
                print(f"Ошибка создания crop для HTML: {path}: {exc}")

        remote_image_url = str(result.get("url_image") or "").strip()

        # Для старых записей, где url_image ещё не был вынесен в отдельную
        # колонку, используем raw_json как дополнительный fallback.
        if not remote_image_url and is_import:
            raw_json = result.get("raw_json") or ""
            try:
                raw = json.loads(raw_json)
                if isinstance(raw, dict):
                    remote_image_url = str(raw.get("url_image") or "").strip()
            except Exception:
                pass

        # Локальные изображения: если CROP отсутствует (или отключён
        # чекбоксом), проверяем исходный локальный файл, путь к которому
        # хранится в images.source_url. Это НЕ CROP.
        local_source_href = ""
        local_source_path = None
        if is_local:
            source_value = str(result.get("source_url") or "").strip()
            if source_value:
                try:
                    # Для локальных записей source_url обычно хранится как
                    # file:///C:/... URI. Не пытаемся вручную разбирать такой
                    # путь через Path(parsed.path): на Windows это может дать
                    # неверный путь вида \C:\... и изображение будет ошибочно
                    # считаться отсутствующим.
                    if source_value.lower().startswith("file://"):
                        # Надёжно преобразуем file:// URI в локальный путь.
                        # Поддерживаются обычные Windows URI и UNC-пути.
                        from urllib.parse import unquote

                        parsed = urlparse(source_value)
                        uri_path = unquote(parsed.path or "")

                        if os.name == "nt":
                            if parsed.netloc and len(parsed.netloc) == 2 and parsed.netloc[1] == ":":
                                # file://C:/folder/file.jpg
                                candidate_text = parsed.netloc + uri_path
                            elif parsed.netloc:
                                # file://server/share/file.jpg
                                candidate_text = "\\\\" + parsed.netloc + uri_path
                            else:
                                # file:///C:/folder/file.jpg
                                candidate_text = uri_path
                                if (
                                    len(candidate_text) >= 3
                                    and candidate_text[0] == "/"
                                    and candidate_text[2] == ":"
                                ):
                                    candidate_text = candidate_text[1:]
                        else:
                            candidate_text = uri_path
                            if parsed.netloc:
                                candidate_text = "//" + parsed.netloc + candidate_text

                        candidate = Path(candidate_text)
                    else:
                        candidate = Path(source_value)
                        if not candidate.is_absolute():
                            candidate = APP_DIR / candidate

                    if candidate.is_file():
                        local_source_path = candidate.resolve()
                        # Всегда формируем корректный file:// URI заново из
                        # реально существующего файла. Так мы не зависим от
                        # того, в каком формате старый source_url был записан.
                        local_source_href = _safe_file_href(local_source_path)
                except (OSError, ValueError):
                    local_source_path = None
                    local_source_href = ""

        pending_crop = bool(
            retry_missing_crops
            and use_local_crops
            and future_local_crop_href
            and future_local_crop_path is not None
            and not future_local_crop_path.is_file()
        )

        if pending_crop:
            # Если задан сетевой URL, сначала пробуем его, а после ошибки
            # переключаемся на локальный путь CROP и периодически повторяем
            # загрузку до появления файла. Это позволяет уже открытому HTML
            # показать файл, созданный фоновым восстановлением.
            pending_src = remote_crop_href or future_local_crop_href
            local_retry_href = future_local_crop_href if remote_crop_href else ""
            final_fallback_href = local_source_href or remote_image_url
            retry_attrs = (
                'data-crop-retry="true" '
                f'data-crop-local-href="{html.escape(local_retry_href, quote=True)}" '
                f'data-crop-final-fallback="{html.escape(final_fallback_href, quote=True)}"'
            )
            image_html = f"""
                <img src="{html.escape(pending_src, quote=True)}"
                    loading="lazy"
                    alt="{filename}"
                    {retry_attrs}>
            """
        elif remote_crop_href:
            if crop_href:
                remote_crop_onerror = (
                    f"this.onerror=null;this.src='{html.escape(crop_href, quote=True)}';"
                )
            else:
                # Не оставляем значок сломанного изображения. HTML-парсер не
                # должен разбирать встроенную разметку внутри JS-атрибута.
                remote_crop_onerror = "this.style.display='none';"
            image_html = f"""
                <img src="{html.escape(remote_crop_href, quote=True)}"
                    loading="lazy"
                    alt="{filename}"
                    onerror="{remote_crop_onerror}">
            """
        elif crop_href:
            image_html = f"""
                <img src="{html.escape(crop_href)}"
                    loading="lazy"
                    alt="{filename}">
            """
        elif local_source_href:
            # Для local source показываем исходную фотографию, если CROP
            # недоступен.
            image_html = f"""
                <img src="{html.escape(local_source_href)}"
                    loading="lazy"
                    alt="{filename}"
                    onerror="this.parentElement.innerHTML='&lt;div class=\"no-image\"&gt;Изображение недоступно&lt;/div&gt;';">
            """
        elif remote_image_url:
            image_html = f"""
                <img src="{html.escape(remote_image_url)}"
                    loading="lazy"
                    referrerpolicy="no-referrer"
                    alt="{filename}"
                    onerror="this.parentElement.innerHTML='&lt;div class=\"no-image\"&gt;Изображение недоступно&lt;/div&gt;';">
            """
        else:
            image_html = '<div class="no-image">Нет изображения</div>'

        if is_import:
            name = result.get("first_name") or "Без имени"
            info = {}
            raw_json = result.get("raw_json") or ""
            source_url = result.get("source_url") or result.get("profile_url") or ""

            try:
                raw = json.loads(raw_json)
                if isinstance(raw, dict):
                    if isinstance(raw.get("info"), dict):
                        info = raw["info"]
                    if not source_url:
                        source_url = raw.get("url_source") or ""
                    if not remote_image_url:
                        remote_image_url = str(raw.get("url_image") or "").strip()
            except Exception:
                pass

            info_rows = "".join(
                f"<div><b>{html.escape(str(k))}:</b> {html.escape(str(v))}</div>"
                for k, v in info.items()
                if v is not None and str(v).strip()
            )

            profile_html = f"""
                <div class="vk-info">
                    <div class="vk-name">{html.escape(str(name))}</div>
                    {info_rows}
                </div>
            """

            source_href, site_address, favicon_url = _site_display_data(source_url)
            if site_address:
                favicon_html = (
                    f'<img class="favicon" src="{html.escape(favicon_url)}" '
                    f'alt="" loading="lazy" onerror="this.style.display=\'none\';">'
                    if favicon_url else ""
                )
                site_html = f"""
                    <div class="source-site">
                        {favicon_html}
                        <a href="{html.escape(source_href)}" target="_blank" rel="noopener">
                            {html.escape(site_address)}
                        </a>
                    </div>
                """
            else:
                site_html = ""

            # Для изображений с JSON-описанием оригинал открываем именно по
            # images.url_image, а не по url_source (это ссылка на страницу сайта).
            # target="_blank" открывает оригинал в новой вкладке браузера.
            original_image_button = (
                f'<a class="button" href="{html.escape(remote_image_url, quote=True)}" '
                'target="_blank" rel="noopener noreferrer">Открыть изображение</a>'
                if remote_image_url else ""
            )
            crop_button_href = remote_crop_href or (_safe_file_href(path) if local_image_exists else "")
            buttons = f"""
                <div class="buttons">
                    {f'<a class="button" href="{html.escape(source_href, quote=True)}" target="_blank" rel="noopener noreferrer">Источник</a>' if source_href else ''}
                    {original_image_button}
                    {f'<a class="button" href="{html.escape(crop_button_href, quote=True)}" target="_blank" rel="noopener noreferrer">Crop</a>' if use_local_crops and crop_button_href else ''}
                </div>
            """

        else:
            profile_html = ""
            local_original_href = local_source_href or _safe_file_href(path)
            buttons = f"""
                <div class="buttons">
                    <a class="button" href="{html.escape(local_original_href)}">
                        Открыть изображение
                    </a>
                </div>
            """
            site_html = ""

        # Путь под изображением показываем только для локальных изображений
        # без JSON-метаданных. Для импортов вместо него показывается сайт+favicon.
        path_looks_like_generated_crop_id = (
            path.suffix.lower() in {".jpg", ".jpeg"} and path.stem.isdigit()
        )
        local_path_html = (
            f'<div class="path">{html.escape(str(path))}</div>'
            if is_local and not path_looks_like_generated_crop_id
            else ""
        )

        # URL оригинала для полноэкранного просмотра. Для локальных
        # записей это исходный файл, для импортированных — images.url_image.
        # Если оригинал недоступен, JS откроет изображение самой карточки.
        if is_local:
            original_view_href = local_source_href or (
                _safe_file_href(path) if local_image_exists else ""
            )
        else:
            original_view_href = remote_image_url or (
                _safe_file_href(path) if local_image_exists else ""
            )
        original_view_href_html = html.escape(original_view_href, quote=True)

        cards.append(
            f"""
            <div class="card" data-original-src="{original_view_href_html}">
                <div class="rank">#{result["rank"]}</div>
                <div class="score">{score * 100:.2f}%</div>

                <div class="face">
                    {image_html}
                </div>

                {filename_html}
                {profile_html}
                {site_html}
                {local_path_html}
                {buttons}
            </div>
            """
        )

    cards_html = "\n".join(cards)
    query_href = _safe_file_href(query_path)

    retry_script = ""
    if retry_missing_crops:
        retry_script = """<script>
(function () {
    function cropLoadFailed(img) {
        if (img.dataset.cropRetry !== "true") return;

        const localHref = img.dataset.cropLocalHref || "";
        if (localHref && img.dataset.cropLocalTried !== "true") {
            img.dataset.cropLocalTried = "true";
            img.src = localHref;
            return;
        }

        const retries = Number(img.dataset.cropRetryCount || "0");
        if (retries < 180) {
            img.dataset.cropRetryCount = String(retries + 1);
            if (img._cropRetryTimer) clearTimeout(img._cropRetryTimer);
            img._cropRetryTimer = setTimeout(function () {
                if (img.dataset.cropRetry !== "true") return;
                const current = img.getAttribute("src") || "";
                const clean = current.split("?")[0];
                img.src = clean + (clean.includes("?") ? "&" : "?") + "crop_retry=" + Date.now();
            }, 1500);
            return;
        }

        img.dataset.cropRetry = "false";
        const fallback = img.dataset.cropFinalFallback || "";
        if (fallback && img.src !== new URL(fallback, document.baseURI).href) {
            img.onerror = null;
            img.src = fallback;
        } else {
            const placeholder = document.createElement("div");
            placeholder.className = "no-image";
            placeholder.textContent = "Изображение недоступно";
            img.replaceWith(placeholder);
        }
    }

    document.addEventListener("error", function (event) {
        if (event.target && event.target.tagName === "IMG") cropLoadFailed(event.target);
    }, true);
    document.addEventListener("load", function (event) {
        const img = event.target;
        if (img && img.tagName === "IMG" && img.dataset.cropRetry === "true") {
            img.dataset.cropRetry = "false";
            if (img._cropRetryTimer) clearTimeout(img._cropRetryTimer);
        }
    }, true);
})();
</script>"""
    lightbox_script = """<script>
(function () {
    const overlay = document.getElementById("image-lightbox");
    const preview = document.getElementById("lightbox-image");
    const closeButton = document.getElementById("lightbox-close");
    let opener = null;

    function openLightbox(image) {
        const card = image.closest(".card");
        const original = card ? (card.dataset.originalSrc || "") : "";
        const source = original || image.currentSrc || image.src;
        if (!source) return;

        opener = image;
        preview.dataset.fallbackTried = "false";
        preview.referrerPolicy = "no-referrer";
        preview.alt = image.alt || "Оригинал фотографии";
        preview.src = source;

        overlay.classList.add("open");
        overlay.setAttribute("aria-hidden", "false");
        document.body.classList.add("lightbox-open");
        closeButton.focus({ preventScroll: true });
    }

    function closeLightbox() {
        if (!overlay.classList.contains("open")) return;

        overlay.classList.remove("open");
        overlay.setAttribute("aria-hidden", "true");
        document.body.classList.remove("lightbox-open");
        preview.removeAttribute("src");

        const previousOpener = opener;
        opener = null;
        if (previousOpener && typeof previousOpener.focus === "function") {
            previousOpener.focus({ preventScroll: true });
        }
    }

    document.addEventListener("click", function (event) {
        const target = event.target;
        const image = target && target.closest
            ? target.closest(".face img")
            : null;
        if (image) {
            event.preventDefault();
            openLightbox(image);
        }
    });

    closeButton.addEventListener("click", function (event) {
        event.stopPropagation();
        closeLightbox();
    });

    overlay.addEventListener("click", function (event) {
        if (event.target === overlay) closeLightbox();
    });

    preview.addEventListener("error", function () {
        if (!opener) return;

        const fallback = opener.currentSrc || opener.src;
        if (
            fallback &&
            preview.dataset.fallbackTried !== "true" &&
            fallback !== preview.src
        ) {
            preview.dataset.fallbackTried = "true";
            preview.src = fallback;
        } else {
            preview.alt = "Не удалось загрузить оригинал фотографии";
        }
    });

    document.addEventListener("keydown", function (event) {
        if (
            event.key === "Escape" &&
            overlay.classList.contains("open")
        ) {
            closeLightbox();
        }
    });
})();
</script>"""

    document = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Face Search Results</title>
<link rel="icon" href="https://raw.githubusercontent.com/ORAMAN/FaceSearch/refs/heads/main/icons/SearchFace.ico" type="image/x-icon">
<style>
* {{ box-sizing: border-box; }}

body {{
    margin: 0;
    padding: 30px;
    background: {"#151515" if str(theme).lower() == "dark" else "#f4f5f7"};
    color: {"#eeeeee" if str(theme).lower() == "dark" else "#202124"};
    font-family: Arial, Helvetica, sans-serif;
}}

h1 {{
    margin-top: 0;
    font-size: 28px;
}}

.info {{
    margin-bottom: 25px;
    color: {"#aaaaaa" if str(theme).lower() == "dark" else "#62666b"};
}}

.query {{
    display: inline-block;
    margin-bottom: 25px;
    padding: 12px 18px;
    background: {"#222222" if str(theme).lower() == "dark" else "#ffffff"};
    border-radius: 8px;
}}

.query a {{
    color: {"#66b3ff" if str(theme).lower() == "dark" else "#075eb5"};
    text-decoration: none;
}}

.gallery {{
    display: grid;
    grid-template-columns: repeat(auto-fill, minmax(300px, 1fr));
    gap: 20px;
    max-width: 1900px;
    margin: auto;
}}

.card {{
    position: relative;
    background: {"#242424" if str(theme).lower() == "dark" else "#ffffff"};
    border: 1px solid {"#383838" if str(theme).lower() == "dark" else "#c8cfd8"};
    border-radius: 12px;
    overflow: hidden;
    padding-bottom: 15px;
    box-shadow: 0 4px 15px rgba(0,0,0,.35);
}}

.card:hover {{
    transform: translateY(-3px);
}}

.face {{
    width: 100%;
    height: 270px;
    padding: 4px;
    display: flex;
    align-items: center;
    justify-content: center;
    background: {"#111111" if str(theme).lower() == "dark" else "#e9eaec"};
    overflow: hidden;
}}

.face img {{
    width: 100%;
    height: 100%;
    object-fit: contain;
    border-radius: 3px;
    cursor: zoom-in;
}}

.no-image {{
    color: {"#777777" if str(theme).lower() == "dark" else "#62666b"};
}}

.rank {{
    position: absolute;
    top: 10px;
    left: 10px;
    z-index: 2;
    padding: 5px 9px;
    color: #ffffff;
    background: rgba(0,0,0,.82);
    border-radius: 6px;
    text-shadow: 0 1px 2px rgba(0,0,0,.35);
}}

.score {{
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 2;
    padding: 6px 10px;
    color: #ffffff;
    background: rgba(0,120,215,.90);
    border-radius: 6px;
    font-weight: bold;
}}

.filename {{
    display: block;
    min-height: 40px;
    line-height: 20px;
    padding: 9px 15px;
    color: {"#f0f0f0" if str(theme).lower() == "dark" else "#171a1f"};
    font-weight: bold;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
    border-bottom: 1px solid {"#353535" if str(theme).lower() == "dark" else "#c8cfd8"};
}}

.path {{
    padding: 0 15px 10px;
    color: {"#888888" if str(theme).lower() == "dark" else "#62666b"};
    font-size: 11px;
    word-break: break-all;
    max-height: 45px;
    overflow: hidden;
}}

.source-site {{
    display: flex;
    align-items: center;
    gap: 7px;
    padding: 0 15px 10px;
    font-size: 12px;
    min-height: 22px;
}}

.source-site a {{
    color: {"#79bfff" if str(theme).lower() == "dark" else "#075eb5"};
    text-decoration: none;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.source-site a:hover {{
    text-decoration: underline;
}}

.favicon {{
    width: 16px;
    height: 16px;
    object-fit: contain;
    flex: 0 0 16px;
}}

.vk-info {{
    margin: 5px 15px 12px;
    padding: 10px;
    background: {"#1e1e1e" if str(theme).lower() == "dark" else "#e9edf2"};
    border: 1px solid {"#353535" if str(theme).lower() == "dark" else "#c4ccd5"};
    border-radius: 8px;
    line-height: 1.55;
    font-size: 13px;
}}

.vk-name {{
    color: {"#79bfff" if str(theme).lower() == "dark" else "#075eb5"};
    font-size: 17px;
    font-weight: bold;
    margin-bottom: 5px;
}}

.buttons {{
    padding: 0 15px;
    display: flex;
    flex-wrap: wrap;
    gap: 6px;
}}

.button {{
    display: inline-block;
    padding: 8px 12px;
    background: {"#333333" if str(theme).lower() == "dark" else "#e5e7eb"};
    color: {"#eeeeee" if str(theme).lower() == "dark" else "#202124"};
    text-decoration: none;
    border: 1px solid {"#484848" if str(theme).lower() == "dark" else "#b7c0cb"};
    border-radius: 6px;
    font-size: 13px;
}}

.button:hover {{
    background: {"#444444" if str(theme).lower() == "dark" else "#d1d5db"};
}}
.lightbox {{
    display: none;
    position: fixed;
    inset: 0;
    z-index: 9999;
    align-items: center;
    justify-content: center;
    padding: 48px 24px;
    background: rgba(0, 0, 0, .94);
    backdrop-filter: blur(3px);
    cursor: zoom-out;
}}

.lightbox.open {{
    display: flex;
}}

.lightbox img {{
    display: block;
    max-width: 96vw;
    max-height: 88vh;
    width: auto;
    height: auto;
    object-fit: contain;
    border-radius: 4px;
    box-shadow: 0 8px 40px rgba(0, 0, 0, .55);
    cursor: default;
}}

.lightbox-close {{
    position: fixed;
    top: 14px;
    right: 20px;
    z-index: 1;
    width: 48px;
    height: 48px;
    padding: 0;
    border: 1px solid rgba(255, 255, 255, .3);
    border-radius: 50%;
    color: #fff;
    background: rgba(35, 35, 35, .85);
    font-size: 34px;
    line-height: 42px;
    text-align: center;
    cursor: pointer;
}}

.lightbox-close:hover {{
    background: rgba(85, 85, 85, .95);
}}

.lightbox-hint {{
    position: fixed;
    right: 12px;
    bottom: 14px;
    left: 12px;
    color: rgba(255, 255, 255, .72);
    font-size: 12px;
    text-align: center;
    pointer-events: none;
}}

body.lightbox-open {{
    overflow: hidden;
}}

</style>
</head>

<body>
<h1>Результаты поиска лиц</h1>

<div class="info">
    Найдено результатов: <b>{len(results)}</b>
</div>

<div class="query">
    Запрос:
    <a href="{html.escape(query_href)}">
        {html.escape(str(query_path))}
    </a>
</div>

<div class="gallery">
{cards_html}
</div>

<div id="image-lightbox" class="lightbox"
     role="dialog" aria-modal="true" aria-hidden="true"
     aria-label="Полноэкранный просмотр фотографии">
    <button type="button" id="lightbox-close" class="lightbox-close"
            aria-label="Закрыть просмотр">×</button>
    <img id="lightbox-image" alt="">
    <div class="lightbox-hint">Esc — закрыть · Нажмите за пределами фотографии, чтобы закрыть</div>
</div>

{retry_script}
{lightbox_script}
</body>
</html>
"""

    output_path.write_text(
        document,
        encoding="utf-8",
    )

    print(f"HTML-галерея: {output_path}")
    return output_path
