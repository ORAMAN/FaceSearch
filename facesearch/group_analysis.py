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
    """Оставляет лучшие уникальные профили-кандидаты для каждого лица."""
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
            if not math.isfinite(score) or score < minimum:
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
                "Ни один кандидат не получил достаточного подтверждения в общем наборе профилей."
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


def _rank_combinations(
    candidates_by_face: list[list[dict[str, Any]]],
    friend_ids: dict[int, set[int]],
    options: dict[str, Any],
    limit: int = 10,
    beam_width: int = 120,
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
        # Сначала согласованность социальных связей, затем полнота, и только затем
        # визуальный рейтинг. Это предотвращает подстановку постороннего профиля
        # с высоким сходством вместо связанного кандидата или «нет совпадения».
        return (
            social_quality,
            int(state["connected_social_pairs"]),
            -int(state["disconnected_social_pairs"]),
            int(state["matched_faces"]),
            float(state["visual_sum"]) + float(state["bonus_sum"]),
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
        combinations.append({
            "rank": len(combinations) + 1,
            "score": round(visual_average + relation_bonus, 2),
            "visual_average": round(visual_average, 2),
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
                    "unmatched_reason": item.get("unmatched_reason") or "",
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
        "combinations": combinations,
        "best_graph": graph,
        "candidates": all_candidates,
        "notice": (
            social_notice + " Общий рейтинг — эвристическая оценка, "
            "а не вероятность правильного определения личности. Владелец профиля-источника "
            "не обязательно является человеком на исходной фотографии."
        ),
    }
