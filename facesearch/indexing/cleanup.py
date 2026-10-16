"""Helpers for deciding whether image storage checks can be skipped."""
from __future__ import annotations

from collections.abc import Callable
from typing import Any


def image_record_has_recoverable_source(
    source_type: Any,
    url_image: Any,
    source_url: Any,
    source_url_to_local_path: Callable[[Any], Any],
) -> bool:
    """Return whether a DB image row is retained independently of its CROP path.

    This matches cleanup_missing_images() semantics: imported rows with a remote
    image URL, and rows whose source_url identifies a local source, are retained
    even if their stored CROP path is absent. The caller can safely avoid a
    filesystem stat for these rows.
    """
    if str(source_type or "") == "import" and bool(str(url_image or "").strip()):
        return True

    try:
        return source_url_to_local_path(source_url) is not None
    except Exception:
        return False
