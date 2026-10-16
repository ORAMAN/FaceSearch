"""Small, dependency-light helpers for decoding stored face embeddings.

The database stores embeddings as contiguous float32 bytes. For read-only
consumers, return a NumPy view over those bytes instead of allocating an
additional array for every row. Code that needs a mutable array must copy it
explicitly.
"""
from __future__ import annotations


def embedding_blob_to_view(blob: bytes | bytearray | memoryview, dimension: int, *, numpy_module=None):
    """Return a float32 view of one stored embedding without copying its data."""
    if numpy_module is None:
        import numpy as numpy_module

    dimension = int(dimension)
    if dimension <= 0:
        raise ValueError(f"Embedding dimension must be positive, got {dimension}")

    return numpy_module.frombuffer(
        blob,
        dtype=numpy_module.float32,
        count=dimension,
    )
