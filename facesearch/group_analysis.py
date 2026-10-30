# -*- coding: utf-8 -*-
"""Экспериментальный анализ групп лиц по сходству и социальным связям.

Модуль намеренно отделён от server_http.py: поисковый backend остаётся прежним,
а этот код ранжирует варианты сопоставления лиц после обычного поиска.
"""
from __future__ import annotations

import hashlib
import json
import math
import re
import threading
import time
import unicodedata
from datetime import datetime
from concurrent.futures import ThreadPoolExecutor, as_completed
from pathlib import Path
from typing import Any, Callable
from urllib.parse import urlparse


DEFAULTS = {
    "min_similarity": 43.0,
    "candidates_per_face": 5,
    "friend_weight": 8.0,
    "common_friend_weight": 2.0,
    "city_weight": 1.5,
    "check_vk_friends": True,
    "check_gender": True,
    "gender_weight": 4.0,
    "profile_gender_weight": 16.0,
    "education_weight": 2.0,
    "workplace_weight": 2.0,
}
MAX_CANDIDATES_PER_FACE = 10
MAX_UNIQUE_FRIEND_LOOKUPS = 80
FRIEND_CACHE_TTL_SECONDS = 15 * 60
FRIEND_REQUEST_INTERVAL_SECONDS = 0.30

_FRIEND_CACHE: dict[int, tuple[float, set[int], str]] = {}
_FRIEND_CACHE_LOCK = threading.RLock()
_VK_REQUEST_LOCK = threading.Lock()
_VK_LAST_REQUEST_AT = 0.0
_VK_LOADER_CACHE_LOCK = threading.RLock()
_VK_TOKEN_FILE_SIGNATURE: tuple[int, int] | None = None


def normalize_options(options: dict[str, Any] | None) -> dict[str, Any]:
    """Проверяет параметры запроса и ограничивает нагрузку."""
    raw = options if isinstance(options, dict) else {}
    result = dict(DEFAULTS)
    try:
        result["min_similarity"] = max(0.0, min(100.0, float(raw.get("min_similarity", DEFAULTS["min_similarity"]))))
        result["candidates_per_face"] = max(1, min(
            MAX_CANDIDATES_PER_FACE,
            int(raw.get("candidates_per_face", DEFAULTS["candidates_per_face"])),
        ))
        result["friend_weight"] = max(0.0, min(20.0, float(raw.get("friend_weight", DEFAULTS["friend_weight"]))))
        result["common_friend_weight"] = max(0.0, min(10.0, float(raw.get("common_friend_weight", DEFAULTS["common_friend_weight"]))))
        result["city_weight"] = max(0.0, min(10.0, float(raw.get("city_weight", DEFAULTS["city_weight"]))))
        result["gender_weight"] = max(0.0, min(10.0, float(raw.get("gender_weight", DEFAULTS["gender_weight"]))))
        result["profile_gender_weight"] = max(0.0, min(30.0, float(raw.get("profile_gender_weight", DEFAULTS["profile_gender_weight"]))))
        result["education_weight"] = max(0.0, min(10.0, float(raw.get("education_weight", DEFAULTS["education_weight"]))))
        result["workplace_weight"] = max(0.0, min(10.0, float(raw.get("workplace_weight", DEFAULTS["workplace_weight"]))))
    except (TypeError, ValueError) as exc:
        raise ValueError("Параметры группового анализа имеют неверный формат.") from exc
    result["check_vk_friends"] = bool(raw.get("check_vk_friends", DEFAULTS["check_vk_friends"]))
    result["check_gender"] = bool(raw.get("check_gender", DEFAULTS["check_gender"]))
    return result


def vk_user_id(candidate: dict[str, Any]) -> int | None:
    """Возвращает числовой VK ID только для профиля, который можно проверить через API."""
    try:
        value = int(candidate.get("vk_user_id") or 0)
        if value > 0:
            return value
    except (TypeError, ValueError):
        pass
    for field in ("profile_url", "source_url", "url_source"):
        value = str(candidate.get(field) or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value if "://" in value else "https://" + value)
        except ValueError:
            continue
        host = (parsed.hostname or "").lower()
        if host not in ("vk.com", "www.vk.com", "m.vk.com", "vk.ru", "www.vk.ru"):
            continue
        match = re.fullmatch(r"/id([1-9]\d*)/?", parsed.path)
        if match:
            return int(match.group(1))
        # Непрозрачный screen name не преобразуем в ID сетевым запросом.
    return None


def _get_vk_loader():
    """Возвращает штатный VK-клиент и синхронизирует кэш токенов с файлом загрузчика."""
    global _VK_TOKEN_FILE_SIGNATURE
    try:
        # Не дублируем OAuth, валидацию, хранение или ротацию токенов.
        from loaders.vk import vk_downloader
    except Exception as exc:
        return None, (
            "Не удалось подключить VK-клиент загрузчика: "
            + type(exc).__name__ + ". Проверьте зависимости загрузчика VK."
        )

    try:
        token_path = Path(vk_downloader.VK_TOKEN_FILE)
        stat = token_path.stat()
        signature = (int(stat.st_mtime_ns), int(stat.st_size))
    except OSError:
        signature = None

    try:
        with _VK_LOADER_CACHE_LOCK:
            cached_tokens = list(getattr(vk_downloader, "_TOKEN_LIST", []) or [])
            if signature != _VK_TOKEN_FILE_SIGNATURE or not cached_tokens:
                token_count = int(vk_downloader.reload_token_cache())
                _VK_TOKEN_FILE_SIGNATURE = signature
            else:
                token_count = len(cached_tokens)
    except Exception as exc:
        return None, (
            "Не удалось загрузить токены через менеджер загрузчика VK: "
            + str(exc).strip()[:250]
        )
    if token_count < 1:
        return None, (
            "В loaders/vk/vk_token.txt нет сохранённых рабочих токенов v1 (vk1.a…). "
            "Добавьте токен в окне загрузчика VK кнопкой «Получить токен через VK ID…» "
            "или «Добавить токен v1 вручную…», затем повторите анализ."
        )
    return vk_downloader, ""

def _request_vk_friends(user_id: int, vk_client) -> tuple[set[int], str]:
    """Получает ID друзей через API-обёртку загрузчика, с его ротацией токенов и повторами."""
    now = time.monotonic()
    with _FRIEND_CACHE_LOCK:
        cached = _FRIEND_CACHE.get(int(user_id))
        if cached and now - cached[0] < FRIEND_CACHE_TTL_SECONDS:
            return set(cached[1]), "cache"

    # Публичный helper загрузчика использует reload_tokens(), штатную ротацию
    # проверенных токенов v1 и повторы запросов; здесь не реализуем API-вызов повторно.
    try:
        with vk_client.requests.Session() as session:
            result = vk_client.get_user_friend_ids(
                int(user_id),
                session,
                limit=int(vk_client.FRIENDS_PAGE_SIZE),
            )
    except Exception as exc:
        message = str(exc).strip()
        # Не включаем access token или параметры запроса в журнал/результат.
        return set(), "error:" + (message[:220] or type(exc).__name__)

    with _FRIEND_CACHE_LOCK:
        _FRIEND_CACHE[int(user_id)] = (time.monotonic(), set(result), "ok")
        while len(_FRIEND_CACHE) > 2000:
            oldest = min(_FRIEND_CACHE, key=lambda key: _FRIEND_CACHE[key][0])
            _FRIEND_CACHE.pop(oldest, None)
    return result, "ok"


def _load_friend_sets(
    candidates_by_face: list[list[dict[str, Any]]],
    app_dir: str | Path,
    enabled: bool,
    progress: Callable[[int, str], None],
) -> tuple[dict[int, set[int]], dict[str, Any]]:
    all_candidates = {
        item["profile_key"]: item
        for face_items in candidates_by_face
        for item in face_items
        if not item.get("unmatched")
    }
    id_to_key: dict[int, str] = {}
    for key, candidate in all_candidates.items():
        user_id = candidate.get("vk_user_id")
        if user_id:
            id_to_key[int(user_id)] = key

    if not enabled:
        return {}, {"status": "disabled", "message": "Проверка друзей VK отключена в параметрах."}

    vk_client, loader_error = _get_vk_loader()
    if vk_client is None:
        return {}, {"status": "no_token", "message": loader_error}

    user_ids = list(id_to_key)
    if not user_ids:
        return {}, {
            "status": "no_vk_candidates",
            "message": "Среди выбранных кандидатов нет профилей с числовым VK ID.",
        }
    truncated = len(user_ids) > MAX_UNIQUE_FRIEND_LOOKUPS
    user_ids = user_ids[:MAX_UNIQUE_FRIEND_LOOKUPS]
    friend_ids: dict[int, set[int]] = {}
    errors: list[str] = []
    completed = 0
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="FaceSearch-VKFriends") as pool:
        futures = {pool.submit(_request_vk_friends, uid, vk_client): uid for uid in user_ids}
        for future in as_completed(futures):
            uid = futures[future]
            try:
                friends, state = future.result()
            except Exception:
                friends, state = set(), "error:worker"
            if state.startswith("error:"):
                errors.append(state[6:])
            else:
                friend_ids[uid] = friends
            completed += 1
            progress(
                35 + int(35 * completed / max(1, len(user_ids))),
                "Проверка друзей VK через загрузчик: "
                + str(completed) + " / " + str(len(user_ids)),
            )

    # Ошибки не содержат токены, но очищаем известные секретные значения на всякий случай.
    try:
        tokens_to_hide = list(getattr(vk_client, "_TOKEN_LIST", []) or [])
    except Exception:
        tokens_to_hide = []
    safe_errors = []
    for error in errors[:3]:
        for secret in tokens_to_hide:
            if secret:
                error = error.replace(secret, "[токен скрыт]")
        safe_errors.append(error[:180])
    friend_status = {
        "status": "partial" if errors or truncated else "ok",
        "message": (
            "Проверка друзей VK через штатный менеджер загрузчика завершена: "
            + str(len(friend_ids)) + " из " + str(len(user_ids)) + " профилей проверено."
            + (" Ошибки API: " + "; ".join(safe_errors) + "." if errors else "")
            + (" Достигнут лимит числа проверяемых профилей." if truncated else "")
            + " Для каждого профиля использованы доступные первые 5000 друзей."
        ),
        "checked_profiles": len(friend_ids),
        "failed_profiles": len(errors),
        "truncated": truncated,
    }
    return friend_ids, friend_status


def _stable_profile_key(candidate: dict[str, Any]) -> str:
    user_id = vk_user_id(candidate)
    if user_id:
        return "vk:" + str(user_id)

    # Идентификатор строки profiles надёжно объединяет разные face.id,
    # даже когда у старых/импортированных записей отсутствует профильный URL.
    try:
        profile_id = int(candidate.get("profile_id") or 0)
    except (TypeError, ValueError):
        profile_id = 0
    if profile_id > 0:
        return "profile:" + str(profile_id)

    for field in ("profile_url", "source_url", "url_source"):
        value = str(candidate.get(field) or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value if "://" in value else "https://" + value)
            if parsed.scheme.lower() in ("http", "https") and parsed.hostname:
                # Результаты на одной странице профиля с разными query-параметрами
                # должны сводиться в один профиль.
                return (parsed.hostname.lower() + parsed.path.rstrip("/")).lower()
        except ValueError:
            pass
    return "face:" + str(int(candidate.get("face_id") or 0))


def _has_internet_profile(candidate: dict[str, Any]) -> bool:
    """Return True only for candidates tied to an online profile, not just a local face/image."""
    if not isinstance(candidate, dict):
        return False
    if vk_user_id(candidate):
        return True

    # source_url is intentionally excluded: it commonly points to the image/file,
    # not to the page of the person/profile.
    for field in ("profile_url", "url_source"):
        value = str(candidate.get(field) or "").strip()
        if not value:
            continue
        try:
            url = value if "://" in value else "https://" + value
            parsed = urlparse(url)
            hostname = (parsed.hostname or "").strip(".").lower()
            if parsed.scheme.lower() not in ("http", "https") or not hostname:
                continue
            if hostname in {"localhost", "localhost.localdomain"} or hostname.endswith(".local"):
                continue
            return True
        except (TypeError, ValueError):
            continue
    return False


_ENTITY_FIELD_ALIASES = {
    "education": {
        "education", "educations", "educationname", "school", "schools", "schoolname",
        "schooltitle", "university", "universities", "universityname", "institution",
        "institutionname", "institute", "institutename", "college", "faculty",
        "placeofstudy", "educationinstitution", "study", "учебноезаведение", "школа",
        "школы", "местоучебы", "местоучёбы", "образование", "университет",
        "институт", "вуз", "учреждение", "учеба", "учёба",
    },
    "workplaces": {
        "work", "works", "workplace", "workplaces", "employer", "employers", "company",
        "companyname", "organization", "organisation", "organizationname",
        "organisationname", "career", "employment", "placeofwork", "работа",
        "местоработы", "работодатель", "организация", "компания", "предприятие",
    },
}


def _normalise_match_text(value: Any) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    # U+2116 has a compatibility decomposition to the letters "No"; treat it as punctuation.
    text = text.replace("№", " ")
    text = unicodedata.normalize("NFKD", text)
    text = "".join(char for char in text if not unicodedata.combining(char))
    text = text.casefold().replace("ё", "е")
    text = re.sub(r"[^\w]+", " ", text, flags=re.UNICODE)
    text = " ".join(text.split())
    if len(text) < 3 or text.isdigit():
        return ""
    if text in {
        "не указано", "не указан", "нет данных", "неизвестно", "unknown",
        "высшее", "среднее", "среднее специальное", "начальное",
        "в процессе обучения", "учащийся", "студент",
    }:
        return ""
    return text


def _normalise_person_name(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower().startswith(("http://", "https://")):
        return ""
    return _normalise_match_text(text)


def _candidate_name(candidate: dict[str, Any]) -> str:
    for field in ("name", "profile_name", "display_name", "metadata_name", "full_name"):
        value = str(candidate.get(field) or "").strip()
        if value and not value.lower().startswith(("http://", "https://")):
            return value[:180]
    metadata = candidate.get("metadata")
    info = candidate.get("info")
    for source in (metadata, info):
        if isinstance(source, dict):
            for key in ("name", "full_name", "fio", "ФИО", "имя"):
                value = str(source.get(key) or "").strip()
                if value and not value.lower().startswith(("http://", "https://")):
                    return value[:180]
    return ""


def _flatten_entity_value(value: Any, kind: str) -> list[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple, set)):
        output = []
        for item in value:
            output.extend(_flatten_entity_value(item, kind))
        return output
    if isinstance(value, dict):
        aliases = (
            ("name", "title", "school_name", "university_name", "institution", "university", "school", "label")
            if kind == "education"
            else ("company_name", "company", "employer", "organization", "organisation", "organization_name", "name", "title", "workplace")
        )
        output = []
        for key in aliases:
            if value.get(key) not in (None, ""):
                output.extend(_flatten_entity_value(value.get(key), kind))
        return output
    text = str(value).strip()
    if not text or text.isdigit() or text in {"-", "—", "0"}:
        return []
    # Некоторые sidecar JSON хранят списки/объекты внутри текстового поля.
    if text[:1] in ("{", "["):
        try:
            parsed = json.loads(text)
        except (TypeError, ValueError):
            parsed = None
        if isinstance(parsed, (dict, list)):
            return _flatten_entity_value(parsed, kind)
    return [text[:250]]


def _metadata_sources(candidate: dict[str, Any]) -> list[dict[str, Any]]:
    sources: list[dict[str, Any]] = []
    for key in ("profile", "metadata", "info", "raw_meta", "details"):
        source = candidate.get(key)
        if isinstance(source, dict):
            sources.append(source)
            for nested_key in ("info", "profile", "details"):
                nested = source.get(nested_key)
                if isinstance(nested, dict):
                    sources.append(nested)
    # In the combined server the profile's descriptive metadata may also be
    # carried as "Key: value · Key: value" in slot 11 of the legacy face row.
    face = candidate.get("face")
    if isinstance(face, (list, tuple)) and len(face) > 11 and isinstance(face[11], str):
        parsed: dict[str, str] = {}
        for part in re.split(r"\s*[·|]\s*", face[11]):
            if ":" in part:
                key, value = part.split(":", 1)
                if key.strip() and value.strip():
                    parsed[key.strip()] = value.strip()
        if parsed:
            sources.append(parsed)
    sources.append(candidate)
    return sources


def _extract_profile_entities(candidate: dict[str, Any], kind: str) -> list[str]:
    aliases = _ENTITY_FIELD_ALIASES[kind]
    values: dict[str, str] = {}
    for source in _metadata_sources(candidate):
        for key, raw_value in source.items():
            normalized_key = re.sub(r"[^\w]+", "", unicodedata.normalize("NFKD", str(key)).casefold())
            normalized_key = "".join(char for char in normalized_key if not unicodedata.combining(char)).replace("ё", "е")
            if normalized_key not in aliases:
                continue
            for item in _flatten_entity_value(raw_value, kind):
                normalized = _normalise_match_text(item)
                if normalized:
                    values.setdefault(normalized, str(item).strip()[:250])
    return [values[key] for key in sorted(values)]


def _normalise_birth_date(value: Any) -> str:
    text = re.sub(r"\s+", "", str(value or "").strip().replace("/", ".").replace("-", "."))
    for fmt in ("%d.%m.%Y", "%Y.%m.%d"):
        try:
            return datetime.strptime(text, fmt).date().isoformat()
        except ValueError:
            pass
    return ""


def _extract_birth_date(candidate: dict[str, Any]) -> str:
    aliases = {
        "birthdate", "birth_date", "birthday", "date_of_birth", "dateofbirth",
        "bdate", "датарождения", "деньрождения",
    }
    for source in _metadata_sources(candidate):
        for key, value in source.items():
            normalized_key = re.sub(r"[^\w]+", "", str(key).casefold()).replace("ё", "е")
            if normalized_key in aliases and value not in (None, ""):
                normalized = _normalise_birth_date(value)
                if normalized:
                    return normalized
    return ""


def _candidate_gender_for_merge(candidate: dict[str, Any]) -> str:
    gender = str(candidate.get("profile_gender") or "unknown").strip().casefold()
    if gender in ("male", "female"):
        return gender
    gender = str(candidate.get("gender") or "unknown").strip().casefold()
    try:
        confidence = float(candidate.get("gender_confidence") or candidate.get("confidence") or 0.0)
    except (TypeError, ValueError, OverflowError):
        confidence = 0.0
    return gender if gender in ("male", "female") and confidence >= 0.65 else "unknown"


def _candidate_source_key(candidate: dict[str, Any]) -> str:
    for field in ("profile_url", "url_source"):
        value = str(candidate.get(field) or "").strip()
        if not value:
            continue
        try:
            parsed = urlparse(value if "://" in value else "https://" + value)
        except ValueError:
            continue
        host = (parsed.hostname or "").casefold().strip(".")
        if not host:
            continue
        if host.startswith("www."):
            host = host[4:]
        if host.startswith("m."):
            host = host[2:]
        if host in {"vk.ru", "www.vk.ru"}:
            host = "vk.com"
        elif host.endswith("ok.ru") or host.endswith("odnoklassniki.ru"):
            host = "ok.ru"
        elif host.endswith("instagram.com"):
            host = "instagram.com"
        return host
    source = str(candidate.get("source_name") or candidate.get("social_network") or candidate.get("source") or "").strip().casefold()
    known = {
        "vk": "vk.com", "вконтакте": "vk.com", "ok": "ok.ru",
        "одноклассники": "ok.ru", "instagram": "instagram.com",
        "facebook": "facebook.com", "фейсбук": "facebook.com",
        "linkedin": "linkedin.com", "тик ток": "tiktok.com", "tiktok": "tiktok.com",
    }
    return known.get(source, "")


def _source_profile(candidate: dict[str, Any]) -> dict[str, Any]:
    return {
        "source": _candidate_source_key(candidate),
        "database": str(candidate.get("database") or ""),
        "name": _candidate_name(candidate),
        "profile_url": str(candidate.get("profile_url") or candidate.get("url_source") or ""),
        "image_url": str(candidate.get("image_url") or candidate.get("url_image") or ""),
        "similarity": round(float(candidate.get("similarity") or 0.0), 2),
        "birthdate": str(candidate.get("birthdate") or ""),
        "education": list(candidate.get("education") or []),
        "workplaces": list(candidate.get("workplaces") or []),
    }


def _unique_source_profiles(values: list[dict[str, Any]]) -> list[dict[str, Any]]:
    output, seen = [], set()
    for item in values:
        if not isinstance(item, dict):
            continue
        key = (
            str(item.get("source") or ""),
            str(item.get("database") or ""),
            str(item.get("profile_url") or ""),
            str(item.get("name") or ""),
        )
        if key in seen or not (key[0] or key[1] or key[2] or key[3]):
            continue
        seen.add(key)
        output.append(dict(item))
    return output


def _merge_value_lists(first: Any, second: Any) -> list[str]:
    output: dict[str, str] = {}
    for value in list(first or []) + list(second or []):
        normalized = _normalise_match_text(value)
        if normalized:
            output.setdefault(normalized, str(value).strip()[:250])
    return [output[key] for key in sorted(output)]


def _matching_entities(first: Any, second: Any) -> list[str]:
    a = {_normalise_match_text(value): str(value).strip() for value in (first or []) if _normalise_match_text(value)}
    b = {_normalise_match_text(value): str(value).strip() for value in (second or []) if _normalise_match_text(value)}
    return [a[key] for key in sorted(a.keys() & b.keys())]


def _merge_candidates_across_sources(
    prepared: list[list[dict[str, Any]]],
) -> tuple[list[list[dict[str, Any]]], dict[str, Any]]:
    """Объединяет один и тот же профиль между источниками по имени и известному полу."""
    catalog: dict[str, dict[str, Any]] = {}
    for candidates in prepared:
        for candidate in candidates:
            if candidate.get("unmatched"):
                continue
            key = str(candidate.get("profile_key") or "")
            if not key:
                continue
            existing = catalog.get(key)
            source_profiles = _unique_source_profiles(list(candidate.get("source_profiles") or [_source_profile(candidate)]))
            if existing is None:
                catalog[key] = {**candidate, "source_profiles": source_profiles}
                continue
            existing["source_profiles"] = _unique_source_profiles(list(existing.get("source_profiles") or []) + source_profiles)
            existing["education"] = _merge_value_lists(existing.get("education"), candidate.get("education"))
            existing["workplaces"] = _merge_value_lists(existing.get("workplaces"), candidate.get("workplaces"))
            if not existing.get("birthdate") and candidate.get("birthdate"):
                existing["birthdate"] = candidate["birthdate"]
            if float(candidate.get("similarity") or 0.0) > float(existing.get("similarity") or 0.0):
                for field in ("name", "profile_name", "profile_url", "url_source", "image_url", "url_image", "city", "source_type", "database", "record", "info", "metadata"):
                    if candidate.get(field) not in (None, "", {}):
                        existing[field] = candidate[field]
                existing["similarity"] = candidate.get("similarity")

    groups: dict[tuple[str, str], list[str]] = {}
    for key, candidate in catalog.items():
        name_key = _normalise_person_name(_candidate_name(candidate))
        gender = _candidate_gender_for_merge(candidate)
        if name_key and gender in ("male", "female"):
            groups.setdefault((name_key, gender), []).append(key)

    aliases = {key: key for key in catalog}
    merged_groups = 0
    for (name_key, gender), keys in groups.items():
        keys = list(dict.fromkeys(keys))
        if len(keys) < 2:
            continue
        dated: dict[str, list[str]] = {}
        for key in keys:
            birthdate = _normalise_birth_date(catalog[key].get("birthdate") or _extract_birth_date(catalog[key]))
            if birthdate:
                dated.setdefault(birthdate, []).append(key)
        if len(dated) > 1:
            cohorts = [(birthday, members) for birthday, members in dated.items() if len(members) > 1]
        else:
            cohorts = [(next(iter(dated), ""), keys)]
        for birthday, cohort in cohorts:
            if len(cohort) < 2:
                continue
            sources: dict[str, int] = {}
            for key in cohort:
                source = _candidate_source_key(catalog[key])
                if source:
                    sources[source] = sources.get(source, 0) + 1
            # A source with multiple same-name profiles is ambiguous: keep them separate.
            if len(sources) < 2 or any(count > 1 for count in sources.values()) or sum(sources.values()) != len(cohort):
                continue
            canonical = "cross-source:" + hashlib.sha1(
                json.dumps([name_key, gender, birthday], ensure_ascii=False).encode("utf-8")
            ).hexdigest()
            for key in cohort:
                aliases[key] = canonical
            merged_groups += 1

    merged_catalog: dict[str, dict[str, Any]] = {}
    for key, candidate in catalog.items():
        canonical = aliases.get(key, key)
        target = merged_catalog.get(canonical)
        if target is None:
            merged_catalog[canonical] = {
                **candidate, "profile_key": canonical,
                "source_profiles": _unique_source_profiles(list(candidate.get("source_profiles") or [])),
            }
            continue
        target["source_profiles"] = _unique_source_profiles(
            list(target.get("source_profiles") or []) + list(candidate.get("source_profiles") or [])
        )
        target["education"] = _merge_value_lists(target.get("education"), candidate.get("education"))
        target["workplaces"] = _merge_value_lists(target.get("workplaces"), candidate.get("workplaces"))
        for field in ("birthdate", "profile_gender", "profile_gender_confidence", "gender", "gender_confidence", "gender_model", "vk_user_id"):
            if target.get(field) in (None, "", "unknown", 0) and candidate.get(field) not in (None, "", "unknown", 0):
                target[field] = candidate[field]
        if float(candidate.get("similarity") or 0.0) > float(target.get("similarity") or 0.0):
            for field in ("name", "profile_name", "profile_url", "url_source", "image_url", "url_image", "city", "source_type", "database", "record", "info", "metadata"):
                if candidate.get(field) not in (None, "", {}):
                    target[field] = candidate[field]
            target["similarity"] = candidate.get("similarity")

    output: list[list[dict[str, Any]]] = []
    for candidates in prepared:
        unique: dict[str, dict[str, Any]] = {}
        unmatched: list[dict[str, Any]] = []
        for candidate in candidates:
            if candidate.get("unmatched"):
                unmatched.append(candidate)
                continue
            old_key = str(candidate.get("profile_key") or "")
            canonical = aliases.get(old_key, old_key)
            registry = merged_catalog.get(canonical, {})
            enriched = dict(candidate)
            enriched["profile_key"] = canonical
            enriched["source_profiles"] = _unique_source_profiles(
                list(candidate.get("source_profiles") or [_source_profile(candidate)])
                + list(registry.get("source_profiles") or [])
            )
            enriched["education"] = _merge_value_lists(candidate.get("education"), registry.get("education"))
            enriched["workplaces"] = _merge_value_lists(candidate.get("workplaces"), registry.get("workplaces"))
            for field in ("name", "profile_name", "profile_url", "url_source", "birthdate", "profile_gender", "profile_gender_confidence", "gender", "gender_confidence", "gender_model", "vk_user_id"):
                if registry.get(field) not in (None, "", "unknown", 0):
                    enriched[field] = registry[field]
            current = unique.get(canonical)
            if current is None:
                unique[canonical] = enriched
            else:
                current["source_profiles"] = _unique_source_profiles(
                    list(current.get("source_profiles") or []) + list(enriched.get("source_profiles") or [])
                )
                current["education"] = _merge_value_lists(current.get("education"), enriched.get("education"))
                current["workplaces"] = _merge_value_lists(current.get("workplaces"), enriched.get("workplaces"))
                if float(enriched.get("similarity") or 0.0) > float(current.get("similarity") or 0.0):
                    for field in ("face_id", "name", "profile_name", "profile_url", "url_source", "image_url", "url_image", "city", "similarity", "source_type", "database"):
                        if enriched.get(field) not in (None, "", {}):
                            current[field] = enriched[field]
        output.append(sorted(unique.values(), key=lambda item: float(item.get("similarity") or 0.0), reverse=True) + unmatched)
    message = (
        f"Объединено групп профилей из разных источников: {merged_groups}."
        if merged_groups else
        "Профили с одинаковыми именем и известным полом из разных источников не обнаружены."
    )
    return output, {"merged_groups": merged_groups, "message": message}



def _prepare_candidates(
    candidates_by_face: list[list[dict[str, Any]]],
    options: dict[str, Any],
) -> list[list[dict[str, Any]]]:
    """Оставляет лучшие уникальные профили-кандидаты для каждого лица."""
    result: list[list[dict[str, Any]]] = []
    limit = int(options["candidates_per_face"])
    minimum = float(options["min_similarity"])
    for face_index, original in enumerate(candidates_by_face):
        seen: set[str] = set()
        current: list[dict[str, Any]] = []
        original_items = [item for item in original if isinstance(item, dict)]
        ordered = sorted(
            (item for item in original_items if _has_internet_profile(item)),
            key=lambda item: float(item.get("similarity") or 0.0),
            reverse=True,
        )
        had_local_only_candidates = any(
            not _has_internet_profile(item) for item in original_items
        )
        for item in ordered:
            try:
                score = float(item.get("similarity") or 0.0)
            except (TypeError, ValueError):
                continue
            if not math.isfinite(score) or score < minimum:
                continue
            candidate = dict(item)
            candidate["similarity"] = round(max(0.0, min(100.0, score)), 2)
            candidate["city"] = str(candidate.get("city") or "").strip()
            candidate["name_key"] = _normalise_person_name(_candidate_name(candidate))
            candidate["birthdate"] = _extract_birth_date(candidate)
            candidate["education"] = _extract_profile_entities(candidate, "education")
            candidate["workplaces"] = _extract_profile_entities(candidate, "workplaces")
            candidate["source_profiles"] = _unique_source_profiles(
                list(candidate.get("source_profiles") or []) + [_source_profile(candidate)]
            )
            profile_gender = _normalize_profile_gender_prediction({
                "profile_gender": candidate.get("profile_gender"),
                "profile_gender_confidence": candidate.get("profile_gender_confidence"),
                "profile_sex": candidate.get("profile_sex", candidate.get("sex")),
            })
            candidate["profile_gender"] = profile_gender["gender"]
            candidate["profile_gender_confidence"] = profile_gender["confidence"]
            candidate["profile_key"] = _stable_profile_key(candidate)
            if candidate["profile_key"] in seen:
                continue
            seen.add(candidate["profile_key"])
            candidate["face_index"] = face_index
            candidate["candidate_rank"] = len(current) + 1
            candidate["vk_user_id"] = vk_user_id(candidate)
            current.append(candidate)
            if len(current) >= limit:
                break
        current.append({
            "profile_key": "unmatched:" + str(face_index),
            "face_index": face_index,
            "candidate_rank": 0,
            "face_id": 0,
            "name": "Нет надёжного совпадения",
            "profile_url": "",
            "city": "",
            "similarity": 0.0,
            "vk_user_id": None,
            "unmatched_reason": (
                "Найдены локальные лица без интернет-профиля; они исключены из оценки связей."
                if had_local_only_candidates and not current
                else "Ни один кандидат не получил достаточного подтверждения в общем наборе профилей."
            ),
            "unmatched": True,
        })
        result.append(current)
    return result

def _pair_relation(a: dict[str, Any], b: dict[str, Any], friend_ids: dict[int, set[int]]) -> dict[str, Any]:
    """Сравнивает социальные связи двух профилей, не считая отсутствие данных доказательством."""
    a_id = a.get("vk_user_id")
    b_id = b.get("vk_user_id")
    direct = False
    common_count = 0
    friend_data_available = False
    if a_id and b_id and int(a_id) != int(b_id):
        a_id, b_id = int(a_id), int(b_id)
        direct = b_id in friend_ids.get(a_id, set()) or a_id in friend_ids.get(b_id, set())
        # Общие друзья можно оценить только если список успешно загружен для обоих.
        friend_data_available = a_id in friend_ids and b_id in friend_ids
        if friend_data_available:
            common_count = len(friend_ids[a_id].intersection(friend_ids[b_id]))
    city_a = str(a.get("city") or "").strip().casefold()
    city_b = str(b.get("city") or "").strip().casefold()
    same_city = bool(city_a and city_b and city_a == city_b)
    education_matches = _matching_entities(a.get("education"), b.get("education"))
    workplace_matches = _matching_entities(a.get("workplaces"), b.get("workplaces"))
    return {
        "friend": direct,
        "common_friends": common_count,
        "same_city": same_city,
        "same_education": bool(education_matches),
        "education_matches": education_matches,
        "same_workplace": bool(workplace_matches),
        "workplace_matches": workplace_matches,
        "friend_data_available": friend_data_available,
    }

def _pair_bonus(relation: dict[str, Any], options: dict[str, Any]) -> float:
    bonus = 0.0
    if relation["friend"]:
        bonus += float(options["friend_weight"])
    if relation["common_friends"]:
        # Ограничиваем вклад большего количества общих друзей, чтобы не позволить
        # одному признаку полностью перекрыть визуальное сходство.
        bonus += float(options["common_friend_weight"]) * min(
            1.0, math.log1p(int(relation["common_friends"])) / math.log(6.0)
        )
    if relation["same_city"]:
        bonus += float(options["city_weight"])
    if relation.get("same_education"):
        bonus += float(options.get("education_weight", DEFAULTS["education_weight"]))
    if relation.get("same_workplace"):
        bonus += float(options.get("workplace_weight", DEFAULTS["workplace_weight"]))
    return bonus



def _normalize_gender_prediction(value: Any) -> dict[str, Any]:
    """Нормализует атрибут InsightFace к male/female/unknown и уверенности 0..1."""
    if not isinstance(value, dict):
        return {"gender": "unknown", "confidence": 0.0}
    raw_gender = value.get("gender")
    if raw_gender is None:
        raw_gender = value.get("sex")
    if isinstance(raw_gender, bool):
        # bool не является контрактным форматом классификатора — не угадываем смысл.
        gender = "unknown"
    elif isinstance(raw_gender, (int, float)):
        # InsightFace genderage: 0 = female, 1 = male.
        gender = "female" if int(raw_gender) == 0 else ("male" if int(raw_gender) == 1 else "unknown")
    else:
        text = str(raw_gender or "").strip().casefold()
        if text in ("1", "m", "male", "man", "м", "муж", "мужской"):
            gender = "male"
        elif text in ("0", "f", "female", "woman", "ж", "жен", "женский"):
            gender = "female"
        else:
            gender = "unknown"
    try:
        confidence = max(0.0, min(1.0, float(value.get("confidence", value.get("gender_confidence", 0.0)))))
    except (TypeError, ValueError):
        confidence = 0.0
    if gender == "unknown" or not math.isfinite(confidence):
        return {"gender": "unknown", "confidence": 0.0}
    return {"gender": gender, "confidence": confidence}


def _normalize_profile_gender_prediction(value: Any) -> dict[str, Any]:
    """Нормализует пол владельца профиля; VK sex=1 означает Ж, sex=2 — М."""
    if isinstance(value, dict):
        raw_gender = value.get("profile_gender")
        if raw_gender is None:
            raw_gender = value.get("profile_sex", value.get("sex"))
        raw_confidence = value.get("profile_gender_confidence")
    else:
        raw_gender = value
        raw_confidence = None

    if isinstance(raw_gender, bool) or raw_gender is None:
        gender = "unknown"
    elif isinstance(raw_gender, (int, float)):
        # VK API sex values differ from InsightFace labels:
        # VK: 1=female, 2=male; InsightFace: 0=female, 1=male.
        code = int(raw_gender)
        gender = "female" if code == 1 else ("male" if code == 2 else "unknown")
    else:
        text = str(raw_gender or "").strip().casefold()
        if text in ("1", "f", "female", "woman", "ж", "жен", "женский", "девушка"):
            gender = "female"
        elif text in ("2", "m", "male", "man", "м", "муж", "мужской", "мужчина"):
            gender = "male"
        else:
            gender = "unknown"

    if gender == "unknown":
        return {"gender": "unknown", "confidence": 0.0}
    try:
        confidence = 0.95 if raw_confidence is None else max(0.0, min(1.0, float(raw_confidence)))
    except (TypeError, ValueError):
        confidence = 0.95
    if not math.isfinite(confidence):
        confidence = 0.0
    return {"gender": gender, "confidence": confidence}


def _profile_gender_mismatch_penalty(
    query_gender: dict[str, Any],
    candidate: dict[str, Any],
    options: dict[str, Any],
) -> float:
    """Мягко штрафует противоречие пола исходного лица и явного пола в карточке профиля."""
    if not options.get("check_gender", True):
        return 0.0
    query = _normalize_gender_prediction(query_gender)
    profile = _normalize_profile_gender_prediction({
        "profile_gender": candidate.get("profile_gender"),
        "profile_gender_confidence": candidate.get("profile_gender_confidence"),
        "profile_sex": candidate.get("profile_sex", candidate.get("sex")),
    })
    if query["gender"] == "unknown" or profile["gender"] == "unknown":
        return 0.0
    if query["gender"] == profile["gender"]:
        return 0.0
    query_conf = float(query["confidence"])
    profile_conf = float(profile["confidence"])
    if query_conf < 0.65 or profile_conf < 0.65:
        return 0.0
    query_strength = max(0.0, min(1.0, (query_conf - 0.5) * 2.0))
    profile_strength = max(0.0, min(1.0, (profile_conf - 0.5) * 2.0))
    return float(options.get("profile_gender_weight", DEFAULTS["profile_gender_weight"])) * query_strength * profile_strength


def _gender_mismatch_penalty(
    query_gender: dict[str, Any],
    candidate: dict[str, Any],
    options: dict[str, Any],
) -> float:
    """Мягко штрафует несовпадение классификации, только когда обе оценки уверенные."""
    if not options.get("check_gender", True):
        return 0.0
    query = _normalize_gender_prediction(query_gender)
    target = _normalize_gender_prediction(candidate)
    if query["gender"] == "unknown" or target["gender"] == "unknown":
        return 0.0
    if query["gender"] == target["gender"]:
        return 0.0
    query_conf = float(query["confidence"])
    target_conf = float(target["confidence"])
    # Уверенность ниже 0.65 остаётся нейтральной, чтобы не превращать
    # неустойчивый атрибут с фотографии в жёсткий фильтр.
    if query_conf < 0.65 or target_conf < 0.65:
        return 0.0
    query_strength = max(0.0, min(1.0, (query_conf - 0.5) * 2.0))
    target_strength = max(0.0, min(1.0, (target_conf - 0.5) * 2.0))
    return float(options["gender_weight"]) * query_strength * target_strength


def _hungarian_maximize(weights: list[list[float]]) -> list[int]:
    """Pure-Python Hungarian assignment for rectangular matrices (rows <= columns)."""
    if not weights:
        return []
    row_count = len(weights)
    column_count = len(weights[0])
    if row_count > column_count or any(len(row) != column_count for row in weights):
        raise ValueError("Матрица назначения должна иметь не меньше столбцов, чем строк.")

    # The standard potential-based Hungarian algorithm minimizes cost;
    # negating weights turns it into a maximum-weight assignment.
    costs = [[-float(value) for value in row] for row in weights]
    u = [0.0] * (row_count + 1)
    v = [0.0] * (column_count + 1)
    p = [0] * (column_count + 1)
    way = [0] * (column_count + 1)
    infinity = float("inf")

    for i in range(1, row_count + 1):
        p[0] = i
        j0 = 0
        minv = [infinity] * (column_count + 1)
        used = [False] * (column_count + 1)
        while True:
            used[j0] = True
            i0 = p[j0]
            delta = infinity
            j1 = 0
            for j in range(1, column_count + 1):
                if used[j]:
                    continue
                cur = costs[i0 - 1][j - 1] - u[i0] - v[j]
                if cur < minv[j]:
                    minv[j] = cur
                    way[j] = j0
                if minv[j] < delta:
                    delta = minv[j]
                    j1 = j
            for j in range(column_count + 1):
                if used[j]:
                    u[p[j]] += delta
                    v[j] -= delta
                else:
                    minv[j] -= delta
            j0 = j1
            if p[j0] == 0:
                break
        while True:
            j1 = way[j0]
            p[j0] = p[j1]
            j0 = j1
            if j0 == 0:
                break

    assigned_columns = [-1] * row_count
    for j in range(1, column_count + 1):
        if p[j]:
            assigned_columns[p[j] - 1] = j - 1
    if any(column < 0 for column in assigned_columns):
        raise RuntimeError("Алгоритм назначения не сопоставил все исходные лица.")
    return assigned_columns


def _rank_combinations(
    candidates_by_face: list[list[dict[str, Any]]],
    friend_ids: dict[int, set[int]],
    options: dict[str, Any],
    limit: int = 10,
    beam_width: int = 120,
    query_genders: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Оптимизирует единое назначение профилей, затем уточняет его по социальному графу.

    Сначала Hungarian assignment находит оптимальное назначение по числу
    сопоставленных лиц, уверенности гендерных сигналов и визуальному сходству.
    Затем локальный поиск по одиночным переназначениям и парам лиц улучшает
    общую согласованность социальных связей, не нарушая уникальность профилей.
    Социальные данные, которых нет или которые не удалось загрузить, нейтральны.
    """
    if not candidates_by_face:
        return []

    unmatched_by_face: list[dict[str, Any]] = []
    candidate_maps: list[dict[str, dict[str, Any]]] = []
    profile_keys: set[str] = set()
    for face_index, items in enumerate(candidates_by_face):
        mapping: dict[str, dict[str, Any]] = {}
        unmatched = None
        for candidate in items:
            if candidate.get("unmatched"):
                unmatched = candidate
                continue
            key = str(candidate.get("profile_key") or _stable_profile_key(candidate))
            if key not in mapping:
                candidate["profile_key"] = key
                mapping[key] = candidate
                profile_keys.add(key)
        if unmatched is None:
            unmatched = {
                "profile_key": "unmatched:" + str(face_index),
                "face_index": face_index,
                "candidate_rank": 0,
                "face_id": 0,
                "name": "Нет назначенного профиля",
                "profile_url": "",
                "city": "",
                "similarity": 0.0,
                "vk_user_id": None,
                "unmatched": True,
                "unmatched_reason": "Для этого лица нет доступных интернет-профилей-кандидатов.",
            }
        unmatched_by_face.append(unmatched)
        candidate_maps.append(mapping)

    global_profile_keys = sorted(profile_keys)
    profile_column = {key: idx for idx, key in enumerate(global_profile_keys)}
    face_count = len(candidates_by_face)
    unmatched_column_offset = len(global_profile_keys)
    invalid_weight = -1.0e15
    # Encode the three unary priorities without depending on arbitrary small
    # weights: (1) matched-face count, (2) gender penalty to 0.001, (3) visual
    # similarity to 0.01. Scales depend on group size so one priority cannot
    # be outweighed by the sum of all lower-priority scores.
    max_penalty_units = int(round((
        float(options.get("gender_weight", 0.0))
        + float(options.get("profile_gender_weight", 0.0))
    ) * 1000.0))
    visual_span_units = 10000 * face_count
    gender_scale = visual_span_units + 1
    match_bonus = (
        (max_penalty_units * face_count + 1) * gender_scale
        + visual_span_units + 1
    )

    normalized_genders = [
        _normalize_gender_prediction(value)
        for value in (query_genders or [])
    ]

    penalty_cache: dict[tuple[int, str], tuple[float, float, float]] = {}

    def candidate_penalties(face_index: int, candidate: dict[str, Any]) -> tuple[float, float, float]:
        key = str(candidate.get("profile_key") or "")
        cache_key = (face_index, key)
        if cache_key in penalty_cache:
            return penalty_cache[cache_key]
        query_gender = (
            normalized_genders[face_index]
            if face_index < len(normalized_genders)
            else {"gender": "unknown", "confidence": 0.0}
        )
        face_penalty = _gender_mismatch_penalty(query_gender, candidate, options)
        profile_penalty = _profile_gender_mismatch_penalty(query_gender, candidate, options)
        value = (face_penalty + profile_penalty, face_penalty, profile_penalty)
        penalty_cache[cache_key] = value
        return value

    def row_choices(face_index: int, forbidden: set[tuple[int, str]]) -> list[dict[str, Any]]:
        choices = [
            candidate for key, candidate in candidate_maps[face_index].items()
            if (face_index, key) not in forbidden
        ]
        choices.sort(
            key=lambda item: (float(item.get("similarity") or 0.0), str(item.get("profile_key") or "")),
            reverse=True,
        )
        choices.append(unmatched_by_face[face_index])
        return choices

    def initial_assignment(forbidden: set[tuple[int, str]]) -> list[dict[str, Any]]:
        # Every face has its own dummy column so unmatched is always feasible.
        column_count = len(global_profile_keys) + face_count
        weights = [[invalid_weight] * column_count for _ in range(face_count)]
        for face_index, mapping in enumerate(candidate_maps):
            for key, candidate in mapping.items():
                if (face_index, key) in forbidden:
                    continue
                penalty = candidate_penalties(face_index, candidate)[0]
                similarity = float(candidate.get("similarity") or 0.0)
                penalty_units = int(round(penalty * 1000.0))
                similarity_units = int(round(max(0.0, min(100.0, similarity)) * 100.0))
                weights[face_index][profile_column[key]] = (
                    match_bonus - penalty_units * gender_scale + similarity_units
                )
            weights[face_index][unmatched_column_offset + face_index] = 0.0

        columns = _hungarian_maximize(weights)
        assignment: list[dict[str, Any]] = []
        for face_index, column in enumerate(columns):
            if column < unmatched_column_offset:
                key = global_profile_keys[column]
                candidate = candidate_maps[face_index].get(key)
                if candidate is not None and (face_index, key) not in forbidden:
                    assignment.append(candidate)
                    continue
            assignment.append(unmatched_by_face[face_index])
        return assignment

    disconnected_penalty = max(4.0, min(20.0, float(options["friend_weight"])))

    def evaluate(
        assignment: list[dict[str, Any]],
        *,
        prioritize_visual: bool = False,
    ) -> dict[str, Any]:
        matched = [
            (index, item) for index, item in enumerate(assignment)
            if not item.get("unmatched")
        ]
        gender_penalty_sum = 0.0
        face_gender_penalty_sum = 0.0
        profile_gender_penalty_sum = 0.0
        visual_sum = 0.0
        for face_index, candidate in matched:
            total, face_penalty, profile_penalty = candidate_penalties(face_index, candidate)
            gender_penalty_sum += total
            face_gender_penalty_sum += face_penalty
            profile_gender_penalty_sum += profile_penalty
            visual_sum += float(candidate.get("similarity") or 0.0)

        social_score_sum = 0.0
        checked_social_pairs = 0
        connected_social_pairs = 0
        disconnected_social_pairs = 0
        relations: list[dict[str, Any]] = []
        for offset, (face_index_a, candidate_a) in enumerate(matched):
            for face_index_b, candidate_b in matched[offset + 1:]:
                relation = _pair_relation(candidate_a, candidate_b, friend_ids)
                social_score_sum += _pair_bonus(relation, options)
                has_friend = bool(relation.get("friend"))
                has_common = int(relation.get("common_friends") or 0) > 0
                has_education = bool(relation.get("same_education"))
                has_workplace = bool(relation.get("same_workplace"))
                if has_friend or has_common or has_education or has_workplace:
                    checked_social_pairs += 1
                    connected_social_pairs += 1
                elif relation.get("friend_data_available"):
                    social_score_sum -= disconnected_penalty
                    checked_social_pairs += 1
                    disconnected_social_pairs += 1
                if has_friend or has_common or relation.get("same_city") or has_education or has_workplace:
                    relations.append({
                        "from": candidate_a["profile_key"],
                        "to": candidate_b["profile_key"],
                        **relation,
                    })

        matched_count = len(matched)
        # В обоих режимах сначала максимизируем число назначенных лиц и
        # учитываем уверенные противоречия пола. Далее приоритет различается:
        # основной вариант ищет социально согласованную группу, отдельный
        # визуальный вариант — максимальное сходство лиц.
        if prioritize_visual:
            objective = (
                matched_count,
                -round(gender_penalty_sum, 6),
                round(visual_sum, 6),
                round(social_score_sum, 6),
            )
        else:
            objective = (
                matched_count,
                -round(gender_penalty_sum, 6),
                round(social_score_sum, 6),
                round(visual_sum, 6),
            )
        return {
            "objective": objective,
            "matched_faces": matched_count,
            "gender_penalty_sum": gender_penalty_sum,
            "face_gender_penalty_sum": face_gender_penalty_sum,
            "profile_gender_penalty_sum": profile_gender_penalty_sum,
            "visual_sum": visual_sum,
            "social_score_sum": social_score_sum,
            "checked_social_pairs": checked_social_pairs,
            "connected_social_pairs": connected_social_pairs,
            "disconnected_social_pairs": disconnected_social_pairs,
            "relations": relations,
        }

    def solve(
        forbidden: set[tuple[int, str]],
        *,
        prioritize_visual: bool = False,
    ) -> tuple[list[dict[str, Any]], dict[str, Any]]:
        choices = [row_choices(i, forbidden) for i in range(face_count)]
        assignment = initial_assignment(forbidden)
        best_metrics = evaluate(assignment, prioritize_visual=prioritize_visual)

        def is_better(metrics: dict[str, Any], current: dict[str, Any]) -> bool:
            return metrics["objective"] > current["objective"]

        # Coordinate ascent over one face and then pairs of faces. Pair moves
        # allow reassignment when the best profile for one face is currently
        # owned by another face; profile keys remain globally unique.
        for _pass in range(3):
            current_metrics = evaluate(assignment, prioritize_visual=prioritize_visual)
            best_assignment = assignment
            best_metrics = current_metrics

            for i in range(face_count):
                used_by_others = {
                    str(item.get("profile_key") or "")
                    for j, item in enumerate(assignment)
                    if j != i and not item.get("unmatched")
                }
                for candidate in choices[i]:
                    key = str(candidate.get("profile_key") or "")
                    if not candidate.get("unmatched") and key in used_by_others:
                        continue
                    variant = list(assignment)
                    variant[i] = candidate
                    metrics = evaluate(variant)
                    if is_better(metrics, best_metrics):
                        best_assignment, best_metrics = variant, metrics

            for i in range(face_count):
                for j in range(i + 1, face_count):
                    used_elsewhere = {
                        str(item.get("profile_key") or "")
                        for k, item in enumerate(assignment)
                        if k not in (i, j) and not item.get("unmatched")
                    }
                    for candidate_i in choices[i]:
                        key_i = str(candidate_i.get("profile_key") or "")
                        if not candidate_i.get("unmatched") and key_i in used_elsewhere:
                            continue
                        for candidate_j in choices[j]:
                            key_j = str(candidate_j.get("profile_key") or "")
                            if not candidate_j.get("unmatched") and key_j in used_elsewhere:
                                continue
                            if (
                                not candidate_i.get("unmatched")
                                and not candidate_j.get("unmatched")
                                and key_i == key_j
                            ):
                                continue
                            variant = list(assignment)
                            variant[i] = candidate_i
                            variant[j] = candidate_j
                            metrics = evaluate(variant)
                            if is_better(metrics, best_metrics):
                                best_assignment, best_metrics = variant, metrics

            if not is_better(best_metrics, current_metrics):
                break
            assignment = best_assignment

        return assignment, evaluate(assignment, prioritize_visual=prioritize_visual)

    best_assignment, _best_metrics = solve(set())
    solutions: list[tuple[list[dict[str, Any]], dict[str, Any], str]] = [
        (best_assignment, _best_metrics, "Социальная согласованность")
    ]
    seen_signatures = {
        tuple(str(item.get("profile_key") or "") for item in best_assignment)
    }

    # Generate alternatives by forbidding each best face->profile edge in turn.
    # Each run re-solves the global assignment, instead of emitting arbitrary
    # beam-search states that may violate the strongest group constraints.
    for face_index, candidate in enumerate(best_assignment):
        if candidate.get("unmatched"):
            continue
        forbidden = {(face_index, str(candidate.get("profile_key") or ""))}
        alternative_assignment, alternative_metrics = solve(forbidden)
        signature = tuple(str(item.get("profile_key") or "") for item in alternative_assignment)
        if signature in seen_signatures:
            continue
        seen_signatures.add(signature)
        solutions.append((alternative_assignment, alternative_metrics, "Альтернативный состав"))
        if len(solutions) >= max(10, min(10, int(limit))):
            break

    # Стабильное ранжирование социальных вариантов при равной оценке.
    # Затем отдельно добавляем визуально лучший вариант: повторный запуск
    # с теми же социальными приоритетами не позволяет показать сильное
    # индивидуальное совпадение, если оно уступает по социальным связям.
    solutions.sort(key=lambda item: item[1]["objective"], reverse=True)

    visual_assignment, visual_metrics = solve(set(), prioritize_visual=True)
    visual_signature = tuple(
        str(item.get("profile_key") or "") for item in visual_assignment
    )
    known_signatures = {
        tuple(str(item.get("profile_key") or "") for item in assignment)
        for assignment, _metrics, _label in solutions
    }
    if visual_signature not in known_signatures:
        solutions.append((
            visual_assignment,
            visual_metrics,
            "Максимум визуального сходства",
        ))

    combinations: list[dict[str, Any]] = []
    pair_count = max(1, face_count * (face_count - 1) / 2)
    for assignment, metrics, strategy_label in solutions[:max(1, int(limit))]:
        matched_count = int(metrics["matched_faces"])
        visual_average = (
            float(metrics["visual_sum"]) / matched_count if matched_count else 0.0
        )
        relation_bonus = float(metrics["social_score_sum"]) / pair_count
        checked_pairs = int(metrics["checked_social_pairs"])
        social_quality = (
            float(metrics["social_score_sum"]) / checked_pairs if checked_pairs else 0.0
        )
        used_profile_keys = {
            str(item.get("profile_key") or "")
            for item in assignment if not item.get("unmatched")
        }

        profiles = []
        for face_index, item in enumerate(assignment):
            unmatched = bool(item.get("unmatched"))
            face_candidates = [
                candidate for candidate in candidates_by_face[face_index]
                if isinstance(candidate, dict) and not candidate.get("unmatched")
                and _has_internet_profile(candidate)
            ]
            query_gender = (
                normalized_genders[face_index]
                if face_index < len(normalized_genders)
                else {"gender": "unknown", "confidence": 0.0}
            )
            candidate_gender = _normalize_gender_prediction(item)
            selected_key = str(item.get("profile_key") or "")
            if unmatched:
                prepared_reason = str(item.get("unmatched_reason") or "").strip()
                if not face_candidates and "локальн" in prepared_reason.casefold():
                    # Сохраняем точную причину, заданную при фильтрации локальных записей.
                    unmatched_reason = prepared_reason
                elif face_candidates and {
                    str(candidate.get("profile_key") or _stable_profile_key(candidate))
                    for candidate in face_candidates
                }.issubset(used_profile_keys):
                    unmatched_reason = (
                        "Все доступные интернет-профили-кандидаты уже назначены другим "
                        "лицам в этой комбинации. Проверьте другие варианты группы."
                    )
                elif not face_candidates:
                    unmatched_reason = (
                        prepared_reason
                        or "В активном индексе нет интернет-профиля выше заданного порога сходства."
                    )
                else:
                    unmatched_reason = (
                        "Кандидат не выбран в этой комбинации. Проверьте альтернативы "
                        "группового анализа и индивидуальные результаты поиска."
                    )
                similarity = 0.0
                face_id = 0
                profile_url = ""
                source_url = ""
                city = ""
                profile_key = selected_key
                name = item.get("name") or "Нет надёжного совпадения"
                penalty, face_penalty, profile_penalty = 0.0, 0.0, 0.0
                assignment_confidence = "none"
                confidence_reason = unmatched_reason
                visual_margin = None
            else:
                unmatched_reason = ""
                similarity = float(item.get("similarity") or 0.0)
                face_id = int(item.get("face_id") or 0)
                profile_url = str(item.get("profile_url") or "")
                source_url = str(item.get("source_url") or "")
                city = str(item.get("city") or "")
                profile_key = selected_key
                name = item.get("name") or "Без имени"
                penalty, face_penalty, profile_penalty = candidate_penalties(face_index, item)
                other_scores = [
                    float(candidate.get("similarity") or 0.0)
                    for candidate in face_candidates
                    if str(candidate.get("profile_key") or _stable_profile_key(candidate)) != selected_key
                ]
                best_alternative_similarity = max(other_scores) if other_scores else None
                visual_margin = (
                    similarity - best_alternative_similarity
                    if best_alternative_similarity is not None else None
                )
                strong_gender_conflict = penalty > 0.0
                if strong_gender_conflict:
                    assignment_confidence = "low"
                    confidence_reason = "Есть уверенное противоречие по полу; проверьте результат вручную."
                elif similarity >= 90.0 and (visual_margin is None or visual_margin >= 5.0):
                    assignment_confidence = "high"
                    confidence_reason = "Высокое сходство и заметный отрыв от следующего визуального кандидата."
                elif similarity >= 75.0 and (visual_margin is None or visual_margin >= 2.0):
                    assignment_confidence = "medium"
                    confidence_reason = "Сходство достаточно высокое, но назначение требует контекстной проверки."
                else:
                    assignment_confidence = "low"
                    confidence_reason = "Недостаточный отрыв от альтернатив или невысокое сходство."
            profiles.append({
                "face_index": face_index,
                "name": name,
                "profile_url": profile_url,
                "source_url": source_url,
                "city": city,
                "education": list(item.get("education") or []),
                "workplaces": list(item.get("workplaces") or []),
                "similarity": similarity,
                "face_id": face_id,
                "profile_key": profile_key,
                "vk_user_id": item.get("vk_user_id"),
                "unmatched": unmatched,
                "unmatched_reason": unmatched_reason,
                "gender": candidate_gender.get("gender", "unknown"),
                "gender_confidence": candidate_gender.get("confidence", 0.0),
                "profile_gender": item.get("profile_gender", "unknown"),
                "profile_gender_confidence": float(item.get("profile_gender_confidence") or 0.0),
                "query_gender": query_gender,
                "face_gender_penalty": round(face_penalty, 2),
                "profile_gender_penalty": round(profile_penalty, 2),
                "gender_penalty": round(penalty, 2),
                "assignment_confidence": assignment_confidence,
                "confidence_reason": confidence_reason,
                "visual_margin": round(visual_margin, 2) if visual_margin is not None else None,
            })

        combinations.append({
            "rank": len(combinations) + 1,
            "strategy_label": strategy_label,
            "score": round(
                visual_average + relation_bonus
                - float(metrics["gender_penalty_sum"]) / max(1, matched_count),
                2,
            ),
            "visual_average": round(visual_average, 2),
            "gender_penalty": round(float(metrics["gender_penalty_sum"]), 2),
            "profile_gender_penalty": round(float(metrics["profile_gender_penalty_sum"]), 2),
            "social_bonus": round(relation_bonus, 2),
            "social_evidence": round(social_quality, 2),
            "connected_pairs": int(metrics["connected_social_pairs"]),
            "disconnected_pairs": int(metrics["disconnected_social_pairs"]),
            "checked_social_pairs": checked_pairs,
            "matched_faces": matched_count,
            "profiles": profiles,
            "relations": metrics["relations"],
        })
        if len(combinations) >= max(1, int(limit)):
            break
    return combinations


def _graph_payload(combination: dict[str, Any]) -> dict[str, Any]:
    profiles = combination.get("profiles", [])
    edges = []
    for relation in combination.get("relations", []):
        kinds = []
        if relation.get("friend"):
            kinds.append("friend")
        if relation.get("common_friends"):
            kinds.append("common_friends")
        if relation.get("same_city"):
            kinds.append("same_city")
        if relation.get("same_education"):
            kinds.append("education")
        if relation.get("same_workplace"):
            kinds.append("workplace")
        if kinds:
            edges.append({
                "from": relation["from"],
                "to": relation["to"],
                "types": kinds,
                "common_friends": int(relation.get("common_friends") or 0),
                "education_matches": list(relation.get("education_matches") or []),
                "workplace_matches": list(relation.get("workplace_matches") or []),
            })
    return {"nodes": profiles, "edges": edges}


def analyze_group(
    candidates_by_face: list[list[dict[str, Any]]],
    options: dict[str, Any] | None,
    app_dir: str | Path,
    progress: Callable[[int, str], None] | None = None,
    query_genders: list[dict[str, Any]] | None = None,
    gender_classifier: Callable[[dict[str, Any]], dict[str, Any]] | None = None,
) -> dict[str, Any]:
    """Ранжирует наборы кандидатов и создаёт граф связей для лучшего варианта."""
    options = normalize_options(options)
    progress = progress or (lambda percent, message: None)
    if len(candidates_by_face) < 2:
        raise ValueError("Для группового анализа нужно обнаружить не менее двух лиц.")

    progress(5, "Подготовка кандидатов для каждого лица…")
    prepared = _prepare_candidates(candidates_by_face, options)
    totals = [sum(1 for item in items if not item.get("unmatched")) for items in prepared]
    progress(22, "Кандидаты подготовлены: " + ", ".join(str(n) for n in totals) + " на лицо.")

    normalized_query_genders = [
        _normalize_gender_prediction(value)
        for value in (query_genders or [])
    ]
    gender_checked = 0
    gender_unknown = 0
    if options["check_gender"]:
        classifier_cache: dict[int, dict[str, Any]] = {}
        if gender_classifier is not None:
            all_face_ids = list(dict.fromkeys(
                int(item.get("face_id") or 0)
                for items in prepared for item in items
                if not item.get("unmatched") and int(item.get("face_id") or 0) > 0
            ))
            for index, face_id in enumerate(all_face_ids, start=1):
                matching = next(
                    item for items in prepared for item in items
                    if not item.get("unmatched") and int(item.get("face_id") or 0) == face_id
                )
                try:
                    prediction = _normalize_gender_prediction(gender_classifier(matching))
                except Exception:
                    prediction = {"gender": "unknown", "confidence": 0.0}
                classifier_cache[face_id] = prediction
                if prediction["gender"] == "unknown":
                    gender_unknown += 1
                else:
                    gender_checked += 1
                if index % 5 == 0 or index == len(all_face_ids):
                    progress(22 + int(10 * index / max(1, len(all_face_ids))),
                             "Определение пола кандидатов: " + str(index) + " / " + str(len(all_face_ids)))
        for items in prepared:
            for item in items:
                if item.get("unmatched"):
                    continue
                face_id = int(item.get("face_id") or 0)
                if face_id in classifier_cache:
                    item.update(classifier_cache[face_id])
                else:
                    prediction = _normalize_gender_prediction(item)
                    item.update(prediction)
                    if prediction["gender"] == "unknown":
                        gender_unknown += 1
                    else:
                        gender_checked += 1
    else:
        for items in prepared:
            for item in items:
                item.update({"gender": "unknown", "confidence": 0.0})
    query_gender_known = sum(1 for value in normalized_query_genders if value["gender"] != "unknown")
    gender_status = {
        "enabled": bool(options["check_gender"]),
        "query_faces_classified": query_gender_known,
        "query_faces_total": len(candidates_by_face),
        "candidate_faces_classified": gender_checked,
        "candidate_faces_unknown": gender_unknown,
        "message": (
            "Оценка пола лиц включена: распознано лиц на исходном фото "
            + str(query_gender_known) + "/" + str(len(candidates_by_face))
            + ", лиц-кандидатов " + str(gender_checked)
            + (", неопределённых " + str(gender_unknown) if gender_unknown else "") + "."
            if options["check_gender"]
            else "Оценка пола лиц отключена в параметрах."
        ),
    }

    prepared, merge_status = _merge_candidates_across_sources(prepared)
    totals = [sum(1 for item in items if not item.get("unmatched")) for items in prepared]

    progress(35, "Проверка социальных связей кандидатов…")
    friend_ids, friend_status = _load_friend_sets(
        prepared, app_dir, bool(options["check_vk_friends"]), progress
    )
    progress(75, "Сопоставление связей, пола и сходства…")
    combinations = _rank_combinations(
        prepared, friend_ids, options, limit=10,
        query_genders=normalized_query_genders,
    )
    best = combinations[0] if combinations else None
    graph = _graph_payload(best) if best else {"nodes": [], "edges": []}

    all_candidates = []
    seen = set()
    for face_index, items in enumerate(prepared):
        for item in items:
            key = item["profile_key"]
            if key in seen or item.get("unmatched"):
                continue
            seen.add(key)
            all_candidates.append({
                "face_index": face_index,
                "profile_key": key,
                "name": item.get("name") or "Без имени",
                "profile_url": item.get("profile_url") or "",
                "source_url": item.get("source_url") or "",
                "city": item.get("city") or "",
                "similarity": float(item.get("similarity") or 0.0),
                "face_id": int(item.get("face_id") or 0),
                "vk_user_id": item.get("vk_user_id"),
                "profile_gender": item.get("profile_gender", "unknown"),
                "profile_gender_confidence": float(item.get("profile_gender_confidence") or 0.0),
                "birthdate": item.get("birthdate") or "",
                "education": list(item.get("education") or []),
                "workplaces": list(item.get("workplaces") or []),
                "source_profiles": _unique_source_profiles(list(item.get("source_profiles") or [])),
                "cross_source_merged": str(item.get("profile_key") or "").startswith("cross-source:"),
            })

    social_state = str(friend_status.get("status") or "")
    if social_state in ("disabled", "no_token", "no_vk_candidates"):
        social_notice = (
            "Социальная проверка не выполнена; этот результат нельзя считать выводом о связях. "
            "Профили ранжированы преимущественно по сходству лиц."
        )
    elif social_state == "partial":
        social_notice = (
            "Социальные данные получены не для всех кандидатов. "
            "Связь между профилями без полного набора данных считается неизвестной."
        )
    else:
        social_notice = (
            "При ранжировании использованы доступные связи друзей VK и общие друзья; "
            "пары без подтверждённых связей по успешно полученным спискам получили отрицательную оценку."
        )

    progress(100, "Анализ завершён.")
    return {
        "ok": True,
        "options": options,
        "face_count": len(candidates_by_face),
        "candidate_counts": totals,
        "friend_status": friend_status,
        "gender_status": gender_status,
        "merge_status": merge_status,
        "combinations": combinations,
        "best_graph": graph,
        "candidates": all_candidates,
        "notice": (
            social_notice + " Общий рейтинг — эвристическая оценка, "
            "а не вероятность правильного определения личности. Владелец профиля-источника "
            "не обязательно является человеком на исходной фотографии."
        ),
    }
