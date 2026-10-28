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
}
MAX_CANDIDATES_PER_FACE = 10
MAX_UNIQUE_FRIEND_LOOKUPS = 80
FRIEND_CACHE_TTL_SECONDS = 15 * 60
FRIEND_REQUEST_INTERVAL_SECONDS = 0.30

_FRIEND_CACHE: dict[int, tuple[float, set[int], str]] = {}
_FRIEND_CACHE_LOCK = threading.RLock()
_VK_REQUEST_LOCK = threading.Lock()
_VK_LAST_REQUEST_AT = 0.0


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
    except (TypeError, ValueError) as exc:
        raise ValueError("Параметры группового анализа имеют неверный формат.") from exc
    result["check_vk_friends"] = bool(raw.get("check_vk_friends", DEFAULTS["check_vk_friends"]))
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
    """Возвращает существующий VK-клиент загрузчика вместе с его менеджером токенов."""
    try:
        # Не дублируем OAuth, валидацию, хранение или ротацию токенов.
        # Используется тот же модуль, что и в loaders/vk/vk_downloader.py.
        from loaders.vk import vk_downloader
    except Exception as exc:
        return None, (
            "Не удалось подключить VK-клиент загрузчика: "
            + type(exc).__name__ + ". Проверьте зависимости загрузчика VK."
        )
    try:
        token_count = int(vk_downloader.reload_token_cache())
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

    # _vk_get_page использует reload_tokens(), то есть штатный пул токенов v1
    # загрузчика, ротацию токенов и повтор запросов при временных ошибках VK.
    try:
        with vk_client.requests.Session() as session:
            response = vk_client._vk_get_page(
                "friends.get",
                {
                    "user_id": int(user_id),
                    "count": int(vk_client.FRIENDS_PAGE_SIZE),
                    "offset": 0,
                },
                session,
                timeout=25,
            )
    except Exception as exc:
        message = str(exc).strip()
        # Не включаем access token или параметры запроса в журнал/результат.
        return set(), "error:" + (message[:220] or type(exc).__name__)

    if isinstance(response, list):
        items = response
    elif isinstance(response, dict):
        items = response.get("items", []) or []
    else:
        items = []

    result: set[int] = set()
    for item in items:
        value = item.get("id") if isinstance(item, dict) else item
        try:
            friend_id = int(value)
        except (TypeError, ValueError):
            continue
        if friend_id > 0:
            result.add(friend_id)

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


def _prepare_candidates(
    candidates_by_face: list[list[dict[str, Any]]],
    options: dict[str, Any],
) -> list[list[dict[str, Any]]]:
    result: list[list[dict[str, Any]]] = []
    limit = int(options["candidates_per_face"])
    minimum = float(options["min_similarity"])
    for face_index, original in enumerate(candidates_by_face):
        seen: set[str] = set()
        current: list[dict[str, Any]] = []
        ordered = sorted(
            (item for item in original if isinstance(item, dict)),
            key=lambda item: float(item.get("similarity") or 0.0),
            reverse=True,
        )
        for item in ordered:
            try:
                score = float(item.get("similarity") or 0.0)
            except (TypeError, ValueError):
                continue
            if score < minimum:
                continue
            candidate = dict(item)
            candidate["similarity"] = round(max(0.0, min(100.0, score)), 2)
            candidate["city"] = str(candidate.get("city") or "").strip()
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
        # Вариант «не сопоставлено» доступен всегда: если один профиль
        # подходит сразу нескольким лицам, алгоритм может оставить одно лицо
        # без кандидата вместо того, чтобы назначить один профиль дважды.
        current.append({
            "profile_key": "unmatched:" + str(face_index),
            "face_index": face_index,
            "candidate_rank": 0,
            "face_id": 0,
            "name": "Нет совпадения выше порога",
            "profile_url": "",
            "city": "",
            "similarity": 0.0,
            "vk_user_id": None,
            "unmatched": True,
        })
        result.append(current)
    return result


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
    token = load_vk_token(app_dir)
    if not token:
        return {}, {
            "status": "no_token",
            "message": "Токен VK не найден. Анализ продолжен по сходству лиц и совпадению городов.",
        }
    user_ids = list(id_to_key)
    if not user_ids:
        return {}, {
            "status": "no_vk_candidates",
            "message": "Среди выбранных кандидатов нет профилей с числовым VK ID.",
        }
    truncated = len(user_ids) > MAX_UNIQUE_FRIEND_LOOKUPS
    user_ids = user_ids[:MAX_UNIQUE_FRIEND_LOOKUPS]
    friend_ids: dict[int, set[int]] = {}
    failures = 0
    completed = 0
    with ThreadPoolExecutor(max_workers=3, thread_name_prefix="FaceSearch-VKFriends") as pool:
        futures = {pool.submit(_request_vk_friends, uid, token): uid for uid in user_ids}
        for future in as_completed(futures):
            uid = futures[future]
            try:
                friends, state = future.result()
            except Exception:
                friends, state = set(), "error:worker"
            if state.startswith("error:"):
                failures += 1
            else:
                friend_ids[uid] = friends
            completed += 1
            progress(
                35 + int(35 * completed / max(1, len(user_ids))),
                "Проверка друзей VK: " + str(completed) + " / " + str(len(user_ids)),
            )

    friend_status = {
        "status": "partial" if failures or truncated else "ok",
        "message": (
            "Проверка друзей VK завершена: получены списки для "
            + str(len(friend_ids)) + " из " + str(len(user_ids)) + " профилей."
            + (" Не все запросы к VK завершились успешно." if failures else "")
            + (" Достигнут лимит числа проверяемых профилей." if truncated else "")
            + " Используются доступные первые 5000 друзей каждого профиля."
        ),
        "checked_profiles": len(friend_ids),
        "failed_profiles": failures,
        "truncated": truncated,
    }
    # Преобразуем user_id -> friend IDs в профильные ключи для быстрого сравнения.
    return friend_ids, friend_status


def _pair_relation(a: dict[str, Any], b: dict[str, Any], friend_ids: dict[int, set[int]]) -> dict[str, Any]:
    a_id = a.get("vk_user_id")
    b_id = b.get("vk_user_id")
    direct = False
    if a_id and b_id and int(a_id) != int(b_id):
        direct = int(b_id) in friend_ids.get(int(a_id), set()) or int(a_id) in friend_ids.get(int(b_id), set())
    common_count = 0
    if a_id and b_id and int(a_id) != int(b_id):
        left = friend_ids.get(int(a_id), set())
        right = friend_ids.get(int(b_id), set())
        common_count = len(left.intersection(right))
    city_a = str(a.get("city") or "").strip().casefold()
    city_b = str(b.get("city") or "").strip().casefold()
    same_city = bool(city_a and city_b and city_a == city_b)
    return {
        "friend": direct,
        "common_friends": common_count,
        "same_city": same_city,
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


def _rank_combinations(
    candidates_by_face: list[list[dict[str, Any]]],
    friend_ids: dict[int, set[int]],
    options: dict[str, Any],
    limit: int = 10,
    beam_width: int = 120,
) -> list[dict[str, Any]]:
    """Beam search: выбирает по одному уникальному профилю на лицо."""
    states: list[dict[str, Any]] = [{
        "chosen": [],
        "used": set(),
        "visual_sum": 0.0,
        "bonus_sum": 0.0,
        "pair_relations": [],
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
                for previous in state["chosen"]:
                    if candidate.get("unmatched") or previous.get("unmatched"):
                        continue
                    relation = _pair_relation(previous, candidate, friend_ids)
                    if relation["friend"] or relation["common_friends"] or relation["same_city"]:
                        pair_relations.append({
                            "from": previous["profile_key"],
                            "to": candidate["profile_key"],
                            **relation,
                        })
                    bonus += _pair_bonus(relation, options)
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
                })
        expanded.sort(
            key=lambda state: state["visual_sum"] + state["bonus_sum"],
            reverse=True,
        )
        states = expanded[:beam_width]
        if not states:
            break

    combinations = []
    pair_count = max(1, len(candidates_by_face) * (len(candidates_by_face) - 1) / 2)
    seen = set()
    for state in states:
        chosen = state["chosen"]
        signature = tuple(item["profile_key"] for item in chosen)
        if signature in seen:
            continue
        seen.add(signature)
        count = max(1, len(chosen))
        visual_average = state["visual_sum"] / count
        relation_bonus = state["bonus_sum"] / pair_count
        combinations.append({
            "rank": len(combinations) + 1,
            "score": round(visual_average + relation_bonus, 2),
            "visual_average": round(visual_average, 2),
            "social_bonus": round(relation_bonus, 2),
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
) -> dict[str, Any]:
    """Ранжирует наборы кандидатов и создаёт граф связей для лучшего варианта."""
    options = normalize_options(options)
    progress = progress or (lambda percent, message: None)
    if len(candidates_by_face) < 2:
        raise ValueError("Для группового анализа нужно обнаружить не менее двух лиц.")

    progress(5, "Подготовка кандидатов для каждого лица…")
    prepared = _prepare_candidates(candidates_by_face, options)
    totals = [sum(1 for item in items if not item.get("unmatched")) for items in prepared]
    progress(25, "Кандидаты подготовлены: " + ", ".join(str(n) for n in totals) + " на лицо.")

    friend_ids, friend_status = _load_friend_sets(
        prepared, app_dir, bool(options["check_vk_friends"]), progress
    )
    progress(75, "Сопоставление связей и ранжирование комбинаций…")
    combinations = _rank_combinations(prepared, friend_ids, options, limit=10)
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
            })

    progress(100, "Анализ завершён.")
    return {
        "ok": True,
        "options": options,
        "face_count": len(candidates_by_face),
        "candidate_counts": totals,
        "friend_status": friend_status,
        "combinations": combinations,
        "best_graph": graph,
        "candidates": all_candidates,
        "notice": (
            "Итоговый рейтинг — эвристическая оценка согласованности вариантов, "
            "а не вероятность правильного определения личности. Отсутствие данных о связях "
            "не трактуется как доказательство отсутствия связи."
        ),
    }
