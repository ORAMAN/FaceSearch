# -*- coding: utf-8 -*-
"""Извлечение полезных метаданных локальных изображений без обязательного EXIF."""
from __future__ import annotations

import json
import os
from datetime import datetime
from pathlib import Path
from typing import Any


def _text(value: Any) -> str | None:
    if value is None:
        return None
    if isinstance(value, bytes):
        try:
            value = value.decode("utf-8", "replace")
        except Exception:
            return None
    result = str(value).strip().replace("\x00", "")
    return result or None


def _ratio(value: Any) -> float | None:
    try:
        if hasattr(value, "numerator") and hasattr(value, "denominator"):
            denominator = float(value.denominator)
            return float(value.numerator) / denominator if denominator else None
        return float(value)
    except (TypeError, ValueError, ZeroDivisionError, OverflowError):
        return None


def _dms_to_decimal(value: Any, ref: Any) -> float | None:
    try:
        parts = [_ratio(item) for item in value]
        if len(parts) != 3 or any(item is None for item in parts):
            return None
        decimal = parts[0] + parts[1] / 60.0 + parts[2] / 3600.0
        direction = _text(ref)
        if direction and direction.upper() in {"S", "W"}:
            decimal = -decimal
        return round(decimal, 7)
    except Exception:
        return None


def extract_image_metadata(path: str | Path) -> dict[str, Any]:
    """Возвращает компактный JSON-совместимый словарь; ошибки EXIF не прерывают импорт."""
    file_path = Path(path).expanduser()
    metadata: dict[str, Any] = {
        "filename": file_path.name,
        "extension": file_path.suffix.lower().lstrip(".") or None,
    }
    try:
        stat = file_path.stat()
        metadata["file_size_bytes"] = int(stat.st_size)
        metadata["file_modified"] = datetime.fromtimestamp(stat.st_mtime).astimezone().isoformat(timespec="seconds")
    except OSError:
        pass

    try:
        from PIL import Image, ExifTags

        with Image.open(file_path) as image:
            metadata["image_width"] = int(image.width)
            metadata["image_height"] = int(image.height)
            metadata["image_format"] = _text(image.format)
            exif = image.getexif()
            tag_names = {int(key): value for key, value in ExifTags.TAGS.items()}
            values = {tag_names.get(int(key), str(key)): value for key, value in exif.items()}
            # Сведения о съёмке обычно находятся во вложенном Exif IFD,
            # а не в основном наборе тегов изображения.
            try:
                exif_ifd = exif.get_ifd(ExifTags.IFD.Exif)
                values.update({
                    tag_names.get(int(key), str(key)): value
                    for key, value in exif_ifd.items()
                })
            except Exception:
                pass

            aliases = {
                "DateTimeOriginal": "date_taken",
                "DateTimeDigitized": "date_digitized",
                "Make": "camera_make",
                "Model": "camera_model",
                "LensModel": "lens_model",
                "Artist": "artist",
                "Copyright": "copyright",
                "ImageDescription": "description",
                "Software": "software",
                "Orientation": "orientation",
                "ExposureTime": "exposure_time",
                "FNumber": "f_number",
                "ISOSpeedRatings": "iso",
                "PhotographicSensitivity": "iso",
                "FocalLength": "focal_length_mm",
            }
            for source, target in aliases.items():
                value = values.get(source)
                if value is not None:
                    if source in {"ExposureTime", "FNumber", "FocalLength"}:
                        number = _ratio(value)
                        if number is not None:
                            metadata[target] = round(number, 4)
                    elif source in {"Orientation", "ISOSpeedRatings", "PhotographicSensitivity"}:
                        try:
                            metadata[target] = int(value)
                        except (TypeError, ValueError):
                            metadata[target] = _text(value)
                    else:
                        metadata[target] = _text(value)

            # EXIF date fields use a local camera clock and generally have no timezone.
            for key in ("date_taken", "date_digitized"):
                value = metadata.get(key)
                if value:
                    try:
                        parsed = datetime.strptime(value, "%Y:%m:%d %H:%M:%S")
                        metadata[key] = parsed.isoformat(sep=" ", timespec="seconds")
                    except (TypeError, ValueError):
                        pass

            try:
                gps_raw = exif.get_ifd(ExifTags.IFD.GPSInfo)
                gps_names = {int(key): value for key, value in ExifTags.GPSTAGS.items()}
                gps = {gps_names.get(int(key), str(key)): value for key, value in gps_raw.items()}
                latitude = _dms_to_decimal(gps.get("GPSLatitude"), gps.get("GPSLatitudeRef"))
                longitude = _dms_to_decimal(gps.get("GPSLongitude"), gps.get("GPSLongitudeRef"))
                if latitude is not None and longitude is not None and -90 <= latitude <= 90 and -180 <= longitude <= 180:
                    metadata["gps_latitude"] = latitude
                    metadata["gps_longitude"] = longitude
                altitude = _ratio(gps.get("GPSAltitude"))
                if altitude is not None:
                    if str(gps.get("GPSAltitudeRef", 0)) == "1":
                        altitude = -altitude
                    metadata["gps_altitude_m"] = round(altitude, 2)
            except Exception:
                pass
    except Exception:
        # Нечитаемый файл/отсутствие EXIF — допустимый случай для обычных фото.
        pass

    return {key: value for key, value in metadata.items() if value is not None and value != ""}


def is_standard_metadata_document(value: Any) -> bool:
    """True only for the exact import envelope: url_source/name/url_image/info."""
    if not isinstance(value, dict):
        return False
    if set(value.keys()) != {"url_source", "name", "url_image", "info"}:
        return False
    return (
        isinstance(value.get("url_source"), str)
        and isinstance(value.get("name"), str)
        and isinstance(value.get("url_image"), str)
        and isinstance(value.get("info"), dict)
    )


def image_metadata_document(path: str | Path) -> dict[str, Any]:
    """Builds local-file metadata using the same strict envelope as sidecar JSON."""
    file_path = Path(path).expanduser().resolve()
    source_url = ""
    try:
        source_url = file_path.as_uri()
    except (OSError, ValueError):
        source_url = str(file_path)

    info = extract_image_metadata(file_path)
    name = str(info.pop("filename", file_path.name) or file_path.name)
    # All file facts live inside info. The document's top-level shape is fixed.
    return {
        "url_source": source_url,
        "name": name,
        "url_image": source_url,
        "info": info,
    }


def normalize_legacy_metadata_document(
    value: dict[str, Any], source_url: str | None
) -> dict[str, Any]:
    """Wraps the former flat EXIF dictionary in the strict import envelope."""
    if is_standard_metadata_document(value):
        return value

    info = dict(value)
    filename = str(info.pop("filename", "") or "").strip()
    # Prevent malformed/old envelope keys from leaking to the top level of info.
    for key in ("url_source", "name", "url_image", "info"):
        info.pop(key, None)

    source = str(source_url or "").strip()
    if not filename:
        filename = Path(source.split("?", 1)[0]).name or "Локальное изображение"

    return {
        "url_source": source,
        "name": filename,
        "url_image": source,
        "info": info,
    }


def metadata_json(path: str | Path) -> str:
    """Serializes local metadata in the canonical four-field JSON format."""
    return json.dumps(
        image_metadata_document(path),
        ensure_ascii=False,
        separators=(",", ":"),
    )
