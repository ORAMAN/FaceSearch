"""Быстрый текстовый фильтр результатов поиска по содержимому карточки.

Поиск выполняется как нечувствительное к регистру частичное совпадение
по JSON-представлению всей записи результата, включая поля профиля,
вложенные info/metadata и исходные JSON-метаданные.
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


def filter_results_by_card_text(
    results: Iterable[dict[str, Any]],
    query: str | None,
    *,
    limit: int | None = None,
) -> list[dict[str, Any]]:
    """Оставляет карточки с подстрокой в любом поле/значении записи."""
    needle = str(query or "").strip().casefold()
    if not needle:
        selected = list(results)
    else:
        selected = []
        for result in results:
            try:
                searchable = json.dumps(
                    result, ensure_ascii=False, default=str, separators=(",", ":")
                )
            except (TypeError, ValueError, OverflowError):
                searchable = str(result)
            if needle in searchable.casefold():
                selected.append(dict(result))

    if limit is not None:
        selected = selected[:max(0, int(limit))]
    if needle:
        # Отображаем непрерывную нумерацию после фильтрации, сохраняя порядок сходства.
        for rank, result in enumerate(selected, start=1):
            result["rank"] = rank
    return selected
