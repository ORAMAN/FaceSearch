# -*- coding: utf-8 -*-
"""Экспериментальный анализ групп лиц по сходству и социальным связям.

Модуль намеренно отделён от server_http.py: поисковый backend остаётся прежним,
а этот код ранжирует варианты сопоставления лиц после обычного поиска.
"""
from __future__ import annotations

import json
import math
import re
import threading
import time
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
    return {
        "friend": direct,
        "common_friends": common_count,
        "same_city": same_city,
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

def _rank_combinations(
    candidates_by_face: list[list[dict[str, Any]]],
    friend_ids: dict[int, set[int]],
    options: dict[str, Any],
    limit: int = 10,
    beam_width: int = 120,
    query_genders: list[dict[str, Any]] | None = None,
) -> list[dict[str, Any]]:
    """Ищет согласованный набор профилей: социальные связи важнее небольших различий сходства.

    Если для обоих VK-профилей успешно получены списки друзей, отсутствие прямой
    дружбы и общих друзей считается отрицательным свидетельством. Если список
    не удалось получить, отношение считается неизвестным, а не отрицательным.
    """
    disconnected_penalty = max(4.0, min(20.0, float(options["friend_weight"])))

    def state_key(state: dict[str, Any]):
        checked_pairs = int(state["checked_social_pairs"])
        social_quality = (
            float(state["social_score_sum"]) / checked_pairs
            if checked_pairs else 0.0
        )
        matched_faces = int(state["matched_faces"])
        # Сначала максимизируем число сопоставленных исходных лиц. Отсутствие
        # VK-связей не должно само по себе превращать хороший face-match в
        # «нет кандидата». Далее учитываем уверенные противоречия по полу,
        # а социальные связи используем для выбора между оставшимися вариантами.
        gender_penalty = float(state.get("gender_penalty_sum", 0.0)) / max(
            1, matched_faces
        )
        return (
            matched_faces,
            -gender_penalty,
            social_quality,
            int(state["connected_social_pairs"]),
            -int(state["disconnected_social_pairs"]),
            float(state["visual_sum"]) + float(state["bonus_sum"])
            - float(state["gender_penalty_sum"]),
        )

    states: list[dict[str, Any]] = [{
        "chosen": [],
        "used": set(),
        "visual_sum": 0.0,
        "bonus_sum": 0.0,
        "pair_relations": [],
        "social_score_sum": 0.0,
        "checked_social_pairs": 0,
        "connected_social_pairs": 0,
        "disconnected_social_pairs": 0,
        "matched_faces": 0,
        "gender_penalty_sum": 0.0,
        "profile_gender_penalty_sum": 0.0,
    }]
    for face_items in candidates_by_face:
        expanded = []
        for state in states:
            for candidate in face_items:
                key = candidate["profile_key"]
                if not candidate.get("unmatched") and key in state["used"]:
                    continue
                bonus = 0.0
                pair_relations = list(state["pair_relations"])
                social_score_sum = float(state["social_score_sum"])
                checked_social_pairs = int(state["checked_social_pairs"])
                connected_social_pairs = int(state["connected_social_pairs"])
                disconnected_social_pairs = int(state["disconnected_social_pairs"])
                gender_penalty_sum = float(state["gender_penalty_sum"])
                profile_gender_penalty_sum = float(state.get("profile_gender_penalty_sum", 0.0))
                current_query_gender = (
                    query_genders[int(candidate.get("face_index", 0))]
                    if query_genders is not None
                    and 0 <= int(candidate.get("face_index", 0)) < len(query_genders)
                    else {"gender": "unknown", "confidence": 0.0}
                )
                face_gender_penalty = (
                    _gender_mismatch_penalty(current_query_gender, candidate, options)
                    if not candidate.get("unmatched")
                    else 0.0
                )
                profile_gender_penalty = (
                    _profile_gender_mismatch_penalty(current_query_gender, candidate, options)
                    if not candidate.get("unmatched")
                    else 0.0
                )
                candidate_gender_penalty = face_gender_penalty + profile_gender_penalty
                gender_penalty_sum += candidate_gender_penalty
                profile_gender_penalty_sum += profile_gender_penalty
                for previous in state["chosen"]:
                    if candidate.get("unmatched") or previous.get("unmatched"):
                        continue
                    relation = _pair_relation(previous, candidate, friend_ids)
                    bonus += _pair_bonus(relation, options)

                    has_friend_edge = bool(relation["friend"])
                    has_common_friend_evidence = int(relation["common_friends"]) > 0
                    if has_friend_edge or has_common_friend_evidence:
                        # Прямая дружба сильнее, чем просто общий круг знакомых.
                        evidence_score = (
                            max(1.0, float(options["friend_weight"]))
                            if has_friend_edge
                            else max(0.5, float(options["common_friend_weight"]))
                            * min(
                                1.0,
                                math.log1p(int(relation["common_friends"])) / math.log(6.0),
                            )
                        )
                        social_score_sum += evidence_score
                        checked_social_pairs += 1
                        connected_social_pairs += 1
                    elif relation["friend_data_available"]:
                        # Отрицательный вывод допустим только когда оба списка друзей
                        # реально загружены; при сбое API неизвестность не штрафуется.
                        social_score_sum -= disconnected_penalty
                        checked_social_pairs += 1
                        disconnected_social_pairs += 1

                    if has_friend_edge or has_common_friend_evidence or relation["same_city"]:
                        pair_relations.append({
                            "from": previous["profile_key"],
                            "to": candidate["profile_key"],
                            **relation,
                        })
                chosen = state["chosen"] + [candidate]
                used = set(state["used"])
                if not candidate.get("unmatched"):
                    used.add(key)
                expanded.append({
                    "chosen": chosen,
                    "used": used,
                    "visual_sum": state["visual_sum"] + float(candidate.get("similarity") or 0.0),
                    "bonus_sum": state["bonus_sum"] + bonus,
                    "pair_relations": pair_relations,
                    "social_score_sum": social_score_sum,
                    "checked_social_pairs": checked_social_pairs,
                    "connected_social_pairs": connected_social_pairs,
                    "disconnected_social_pairs": disconnected_social_pairs,
                    "matched_faces": int(state["matched_faces"]) + int(not candidate.get("unmatched")),
                    "gender_penalty_sum": gender_penalty_sum,
                    "profile_gender_penalty_sum": profile_gender_penalty_sum,
                })
        expanded.sort(key=state_key, reverse=True)
        states = expanded[:beam_width]
        if not states:
            break

    combinations = []
    pair_count = max(1, len(candidates_by_face) * (len(candidates_by_face) - 1) / 2)
    seen = set()
    states.sort(key=state_key, reverse=True)
    for state in states:
        chosen = state["chosen"]
        signature = tuple(item["profile_key"] for item in chosen)
        if signature in seen:
            continue
        seen.add(signature)
        count = max(1, len(chosen))
        visual_average = state["visual_sum"] / count
        relation_bonus = state["bonus_sum"] / pair_count
        checked_pairs = int(state["checked_social_pairs"])
        social_quality = (
            float(state["social_score_sum"]) / checked_pairs
            if checked_pairs else 0.0
        )
        used_profile_keys = {
            item["profile_key"] for item in chosen
            if not item.get("unmatched")
        }

        def explain_unmatched(item: dict[str, Any], face_index: int) -> str:
            original_reason = str(item.get("unmatched_reason") or "").strip()
            face_candidates = (
                candidates_by_face[face_index]
                if 0 <= face_index < len(candidates_by_face) else []
            )
            original_candidates = [
                candidate for candidate in face_candidates
                if isinstance(candidate, dict) and not candidate.get("unmatched")
            ]
            available = [
                candidate for candidate in original_candidates
                if _has_internet_profile(candidate)
            ]
            if not available:
                if original_candidates:
                    return (
                        "Найдены локальные лица без интернет-профиля; они исключены "
                        "из оценки связей. Для назначения нужен URL страницы профиля."
                    )
                return original_reason or (
                    "В активном индексе нет кандидата выше заданного порога сходства."
                )
            keys = {
                str(candidate.get("profile_key") or _stable_profile_key(candidate))
                for candidate in available
            }
            if keys and keys.issubset(used_profile_keys):
                return (
                    "Все доступные профили-кандидаты уже назначены другим лицам "
                    "в этой комбинации. Проверьте другие комбинации или увеличьте "
                    "параметр «Кандидатов на лицо»."
                )
            return (
                "Кандидат не выбран в этой комбинации. Проверьте другие варианты "
                "группы и индивидуальные результаты поиска."
            )

        combinations.append({
            "rank": len(combinations) + 1,
            "score": round(visual_average + relation_bonus - float(state["gender_penalty_sum"]) / count, 2),
            "visual_average": round(visual_average, 2),
            "gender_penalty": round(float(state["gender_penalty_sum"]), 2),
            "profile_gender_penalty": round(float(state.get("profile_gender_penalty_sum", 0.0)), 2),
            "social_bonus": round(relation_bonus, 2),
            "social_evidence": round(social_quality, 2),
            "connected_pairs": int(state["connected_social_pairs"]),
            "disconnected_pairs": int(state["disconnected_social_pairs"]),
            "checked_social_pairs": checked_pairs,
            "matched_faces": sum(1 for item in chosen if not item.get("unmatched")),
            "profiles": [
                {
                    "face_index": int(item.get("face_index", index)),
                    "name": item.get("name") or "Без имени",
                    "profile_url": item.get("profile_url") or "",
                    "source_url": item.get("source_url") or "",
                    "city": item.get("city") or "",
                    "similarity": float(item.get("similarity") or 0.0),
                    "face_id": int(item.get("face_id") or 0),
                    "profile_key": item["profile_key"],
                    "vk_user_id": item.get("vk_user_id"),
                    "unmatched": bool(item.get("unmatched")),
                    "unmatched_reason": (
                        explain_unmatched(item, int(item.get("face_index", index)))
                        if item.get("unmatched") else ""
                    ),
                    "gender": _normalize_gender_prediction(item).get("gender", "unknown"),
                    "gender_confidence": _normalize_gender_prediction(item).get("confidence", 0.0),
                    "profile_gender": item.get("profile_gender", "unknown"),
                    "profile_gender_confidence": float(item.get("profile_gender_confidence") or 0.0),
                    "query_gender": (
                        query_genders[int(item.get("face_index", index))]
                        if query_genders is not None
                        and 0 <= int(item.get("face_index", index)) < len(query_genders)
                        else {"gender": "unknown", "confidence": 0.0}
                    ),
                    "face_gender_penalty": (
                        round(_gender_mismatch_penalty(
                            query_genders[int(item.get("face_index", index))], item, options
                        ), 2)
                        if query_genders is not None
                        and 0 <= int(item.get("face_index", index)) < len(query_genders)
                        and not item.get("unmatched")
                        else 0.0
                    ),
                    "profile_gender_penalty": (
                        round(_profile_gender_mismatch_penalty(
                            query_genders[int(item.get("face_index", index))], item, options
                        ), 2)
                        if query_genders is not None
                        and 0 <= int(item.get("face_index", index)) < len(query_genders)
                        and not item.get("unmatched")
                        else 0.0
                    ),
                    "gender_penalty": round(
                        (
                            _gender_mismatch_penalty(
                                query_genders[int(item.get("face_index", index))], item, options
                            )
                            + _profile_gender_mismatch_penalty(
                                query_genders[int(item.get("face_index", index))], item, options
                            )
                        ), 2
                    ) if query_genders is not None
                    and 0 <= int(item.get("face_index", index)) < len(query_genders)
                    and not item.get("unmatched") else 0.0,
                }
                for index, item in enumerate(chosen)
            ],
            "relations": state["pair_relations"],
        })
        if len(combinations) >= limit:
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
        if kinds:
            edges.append({
                "from": relation["from"],
                "to": relation["to"],
                "types": kinds,
                "common_friends": int(relation.get("common_friends") or 0),
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
        "combinations": combinations,
        "best_graph": graph,
        "candidates": all_candidates,
        "notice": (
            social_notice + " Общий рейтинг — эвристическая оценка, "
            "а не вероятность правильного определения личности. Владелец профиля-источника "
            "не обязательно является человеком на исходной фотографии."
        ),
    }
