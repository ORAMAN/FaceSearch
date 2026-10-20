"""Текстовый фильтр результатов поиска по значениям полей карточки.

Сопоставление — частичное и нечувствительное к регистру. Вложенные JSON-строки
(профильные данные и metadata_json) разбираются рекурсивно, чтобы проверить
их значения, не создавая ложных совпадений только по именам полей.
"""
from __future__ import annotations

import json
from typing import Any, Iterable

MAX_CARD_TEXT_FILTER_LENGTH = 500
TEXT_FILTER_CANDIDATE_MULTIPLIER = 20
TEXT_FILTER_MIN_CANDIDATES = 1000
TEXT_FILTER_MAX_CANDIDATES = 10000


def candidate_count_for_text_filter(top_k: int, query: str | None) -> int:
    """Возвращает увеличенный пул кандидатов только для активного фильтра."""
    requested = max(1, int(top_k))
    if not str(query or "").strip():
        return requested
    return min(
        TEXT_FILTER_MAX_CANDIDATES,
        max(TEXT_FILTER_MIN_CANDIDATES, requested * TEXT_FILTER_CANDIDATE_MULTIPLIER),
    )


def _iter_card_values(value: Any):
    """Обходит значения словаря/списка и разбирает вложенные JSON-строки."""
    if isinstance(value, dict):
        for nested in value.values():
            yield from _iter_card_values(nested)
        return
    if isinstance(value, (list, tuple, set)):
        for nested in value:
            yield from _iter_card_values(nested)
        return
    if value is None:
        return

    if isinstance(value, str):
        candidate = value.lstrip()
        if len(candidate) <= 1_000_000 and candidate.startswith(("{", "[")):
            try:
                parsed = json.loads(candidate)
            except (TypeError, ValueError, RecursionError):
                parsed = None
            if isinstance(parsed, (dict, list)):
                yield from _iter_card_values(parsed)
                return
    yield str(value)


def filter_results_by_card_text(
    results: Iterable[dict[str, Any]],
    query: str | None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Оставляет карточки, в значениях полей которых встречается подстрока."""
    needle = str(query or "").strip().casefold()
    if not needle:
        selected = list(results)
    else:
        selected = []
        for result in results:
            searchable = " ".join(_iter_card_values(result)).casefold()
            if needle in searchable:
                selected.append(dict(result))

    if limit is not None:
        selected = selected[:max(0, int(limit))]
    if needle:
        # Отображаем непрерывную нумерацию после фильтрации, сохраняя порядок сходства.
        for rank, result in enumerate(selected, start=1):
            result["rank"] = rank
    return selected
