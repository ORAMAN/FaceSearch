# -*- coding: utf-8 -*-
"""Генерация автономной HTML-галереи результатов поиска FaceSearch."""
from __future__ import annotations

import html
import json
import os
import shutil
from datetime import datetime
from pathlib import Path
from typing import Any
from urllib.parse import unquote, urlparse
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


def generate_html_gallery(
    results,
    query_image: Path,
    output_file: str = "face_search_results.html",
    use_local_crops: bool = True,
    *,
    crop_dir: Path | str | None = None,
    app_dir: Path | str | None = None,
    load_image_fn=None,
    cv2_module=None,
):
    APP_DIR = Path(app_dir).resolve() if app_dir else DEFAULT_APP_DIR
    CROP_DIR = Path(crop_dir).resolve() if crop_dir else (APP_DIR / "face_images")
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

        filename = html.escape(str(result.get("filename") or ""))

        # В режиме локальных CROP используем физический файл из face_images.
        # При отключённом чекбоксе local CROP вообще не используется:
        # изображение будет взято из сохранённого url_image ниже.
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
                        from urllib.parse import unquote, urlparse

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

        if crop_href:
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
            buttons = f"""
                <div class="buttons">
                    {f'<a class="button" href="{html.escape(source_href, quote=True)}" target="_blank" rel="noopener noreferrer">Источник</a>' if source_href else ''}
                    {original_image_button}
                    {f'<a class="button" href="{html.escape(_safe_file_href(path), quote=True)}">Crop</a>' if use_local_crops and local_image_exists else ''}
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
        local_path_html = (
            f'<div class="path">{html.escape(str(path))}</div>'
            if is_local
            else ""
        )

        cards.append(
            f"""
            <div class="card">
                <div class="rank">#{result["rank"]}</div>
                <div class="score">{score * 100:.2f}%</div>

                <div class="face">
                    {image_html}
                </div>

                <div class="filename">{filename}</div>
                {profile_html}
                {site_html}
                {local_path_html}
                {buttons}
            </div>
            """
        )

    cards_html = "\n".join(cards)
    query_href = _safe_file_href(query_path)

    document = f"""<!DOCTYPE html>
<html lang="ru">
<head>
<meta charset="UTF-8">
<meta name="viewport" content="width=device-width, initial-scale=1.0">
<title>Face Search Results</title>

<style>
* {{ box-sizing: border-box; }}

body {{
    margin: 0;
    padding: 30px;
    background: #151515;
    color: #eeeeee;
    font-family: Arial, Helvetica, sans-serif;
}}

h1 {{
    margin-top: 0;
    font-size: 28px;
}}

.info {{
    margin-bottom: 25px;
    color: #aaaaaa;
}}

.query {{
    display: inline-block;
    margin-bottom: 25px;
    padding: 12px 18px;
    background: #222222;
    border-radius: 8px;
}}

.query a {{
    color: #66b3ff;
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
    background: #222222;
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
    display: flex;
    align-items: center;
    justify-content: center;
    background: #111111;
    overflow: hidden;
}}

.face img {{
    width: 100%;
    height: 100%;
    object-fit: contain;
}}

.no-image {{
    color: #777777;
}}

.rank {{
    position: absolute;
    top: 10px;
    left: 10px;
    z-index: 2;
    padding: 5px 9px;
    background: rgba(0,0,0,.75);
    border-radius: 6px;
}}

.score {{
    position: absolute;
    top: 10px;
    right: 10px;
    z-index: 2;
    padding: 6px 10px;
    background: rgba(0,120,215,.90);
    border-radius: 6px;
    font-weight: bold;
}}

.filename {{
    padding: 12px 15px 4px;
    font-weight: bold;
    overflow: hidden;
    text-overflow: ellipsis;
    white-space: nowrap;
}}

.path {{
    padding: 0 15px 10px;
    color: #888888;
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
    color: #79bfff;
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
    background: #1b1b1b;
    border-radius: 8px;
    line-height: 1.55;
    font-size: 13px;
}}

.vk-name {{
    color: #79bfff;
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
    background: #333333;
    color: #eeeeee;
    text-decoration: none;
    border-radius: 6px;
    font-size: 13px;
}}

.button:hover {{
    background: #444444;
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
</body>
</html>
"""

    output_path.write_text(
        document,
        encoding="utf-8",
    )

    print(f"HTML-галерея: {output_path}")
    return output_path
