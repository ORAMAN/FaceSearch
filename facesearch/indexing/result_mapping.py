"""Database result hydration for FAISS search results.

This module deliberately keeps result ordering tied to FAISS-ranked face IDs,
not the order in which the database returns rows.
"""
from __future__ import annotations

from typing import Any

from facesearch.config import DB_RESULT_BATCH_SIZE

_RESULT_SELECT_SQL = """
    SELECT
        f.id,
        i.path,
        i.filename,
        f.x1, f.y1, f.x2, f.y2,
        f.det_score,
        i.source_type,
        i.source_url,
        i.url_image,
        i.metadata_json,
        p.vk_user_id,
        p.screen_name,
        p.domain,
        p.first_name,
        p.last_name,
        p.maiden_name,
        p.bdate,
        p.site,
        p.status,
        p.followers_count,
        p.online,
        p.last_seen_time,
        p.last_seen_platform,
        p.verified,
        p.is_verified,
        p.occupation_type,
        p.occupation_id,
        p.occupation_name,
        p.city_title,
        p.home_town,
        p.sex,
        p.has_photo,
        p.has_mobile,
        p.can_access_closed,
        p.is_closed,
        p.deactivated,
        p.profile_url,
        p.raw_json
    FROM faces f
    JOIN images i ON i.id = f.image_id
    LEFT JOIN profiles p ON p.id = i.profile_id
    WHERE f.id IN ({placeholders})
"""

def _fetch_result_rows_by_face_ids(conn: DatabaseConnection, face_ids) -> dict[int, Any]:
    """Получает данные карточек пакетами и индексирует их по face.id.

    SQL-ответы могут приходить в произвольном порядке — порядок результатов
    FAISS восстанавливается отдельно в _resolve_ranked_results().
    """
    unique_ids = list(dict.fromkeys(int(face_id) for face_id in face_ids if int(face_id) >= 0))
    rows_by_id: dict[int, Any] = {}
    if not unique_ids:
        return rows_by_id

    for offset in range(0, len(unique_ids), DB_RESULT_BATCH_SIZE):
        chunk = unique_ids[offset:offset + DB_RESULT_BATCH_SIZE]
        placeholders = ",".join("?" for _ in chunk)
        sql = _RESULT_SELECT_SQL.format(placeholders=placeholders)
        for row in conn.execute(sql, tuple(chunk)).fetchall():
            if row is not None:
                try:
                    rows_by_id[int(row[0])] = row
                except (TypeError, ValueError, IndexError):
                    continue
    return rows_by_id

def _resolve_ranked_results(
    conn: DatabaseConnection,
    ranked_matches,
    min_similarity: float,
) -> tuple[list[dict[str, Any]], int]:
    """Гидратирует top-k одной/несколькими SQL-пачками, сохраняя ранжирование FAISS."""
    filtered = []
    for score, face_id in ranked_matches:
        face_id = int(face_id)
        score = float(score)
        if face_id < 0 or score < float(min_similarity):
            continue
        filtered.append((score, face_id))

    rows_by_id = _fetch_result_rows_by_face_ids(conn, (face_id for _, face_id in filtered))
    results: list[dict[str, Any]] = []
    stale_ids = 0
    for score, face_id in filtered:
        row = rows_by_id.get(face_id)
        if row is None:
            stale_ids += 1
            continue
        results.append(_row_to_result(row, len(results) + 1, score))
    return results, stale_ids

def _result_row(conn, face_id: int):
    """Совместимый одиночный helper; основной поиск использует пакетную выборку."""
    rows = _fetch_result_rows_by_face_ids(conn, [int(face_id)])
    return rows.get(int(face_id))

def _row_to_result(row, rank: int, score: float) -> dict[str, Any]:
    (
        face_id,
        path,
        filename,
        x1, y1, x2, y2,
        det_score,
        source_type,
        source_url,
        url_image,
        metadata_json,
        vk_user_id,
        screen_name,
        domain,
        first_name,
        last_name,
        maiden_name,
        bdate,
        site,
        status_text,
        followers_count,
        online,
        last_seen_time,
        last_seen_platform,
        verified,
        is_verified,
        occupation_type,
        occupation_id,
        occupation_name,
        city_title,
        home_town,
        sex,
        has_photo,
        has_mobile,
        can_access_closed,
        is_closed,
        deactivated,
        profile_url,
        raw_json,
    ) = row

    return {
        "rank": rank,
        "score": float(score),
        "face_id": int(face_id),
        "path": path,
        "filename": filename,
        "bbox": (x1, y1, x2, y2),
        "det_score": float(det_score),

        "source_type": source_type or "local",
        "source_url": source_url,
        "url_image": url_image,
        "metadata_json": metadata_json,

        "vk_user_id": vk_user_id,
        "screen_name": screen_name,
        "domain": domain,
        "first_name": first_name,
        "last_name": last_name,
        "maiden_name": maiden_name,
        "bdate": bdate,
        "site": site,
        "status": status_text,
        "followers_count": followers_count,
        "online": online,
        "last_seen_time": last_seen_time,
        "last_seen_platform": last_seen_platform,
        "verified": verified,
        "is_verified": is_verified,
        "occupation_type": occupation_type,
        "occupation_id": occupation_id,
        "occupation_name": occupation_name,
        "city_title": city_title,
        "home_town": home_town,
        "sex": sex,
        "has_photo": has_photo,
        "has_mobile": has_mobile,
        "can_access_closed": can_access_closed,
        "is_closed": is_closed,
        "deactivated": deactivated,
        "profile_url": profile_url,
        "raw_json": raw_json,
    }
