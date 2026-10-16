import unittest
from pathlib import Path
from unittest.mock import Mock

import numpy as np

from facesearch.indexing.cleanup import image_record_has_recoverable_source
from facesearch.indexing.embeddings import embedding_blob_to_view


class EmbeddingBlobViewTests(unittest.TestCase):
    def test_decodes_float32_bytes_without_allocating_a_copy(self):
        expected = np.linspace(-1.0, 1.0, 512, dtype=np.float32)
        payload = expected.tobytes()

        actual = embedding_blob_to_view(payload, 512, numpy_module=np)

        np.testing.assert_array_equal(actual, expected)
        self.assertEqual(actual.dtype, np.float32)
        self.assertFalse(actual.flags.owndata)
        self.assertFalse(actual.flags.writeable)

    def test_rejects_non_positive_dimensions(self):
        with self.assertRaises(ValueError):
            embedding_blob_to_view(b"", 0, numpy_module=np)

    def test_rejects_a_blob_shorter_than_the_requested_embedding(self):
        payload = np.array([1.0], dtype=np.float32).tobytes()
        with self.assertRaises(ValueError):
            embedding_blob_to_view(payload, 2, numpy_module=np)


class RecoverableImageSourceTests(unittest.TestCase):
    def test_import_with_image_url_skips_local_path_resolution(self):
        resolver = Mock(side_effect=AssertionError("must not be called"))

        kept = image_record_has_recoverable_source(
            "import",
            "https://example.invalid/image.jpg",
            "https://example.invalid/profile",
            resolver,
        )

        self.assertTrue(kept)
        resolver.assert_not_called()

    def test_local_source_skips_crop_path_stat(self):
        resolver = Mock(return_value=Path("original.jpg"))

        kept = image_record_has_recoverable_source(
            "local",
            "",
            "C:/photos/original.jpg",
            resolver,
        )

        self.assertTrue(kept)
        resolver.assert_called_once_with("C:/photos/original.jpg")

    def test_remote_row_without_fallback_still_needs_storage_check(self):
        resolver = Mock(return_value=None)

        kept = image_record_has_recoverable_source(
            "remote",
            "",
            "https://example.invalid/profile",
            resolver,
        )

        self.assertFalse(kept)
        resolver.assert_called_once()

    def test_source_parser_error_does_not_mark_row_recoverable(self):
        resolver = Mock(side_effect=OSError("invalid path"))

        kept = image_record_has_recoverable_source(
            "local",
            "",
            "broken-path",
            resolver,
        )

        self.assertFalse(kept)


if __name__ == "__main__":
    unittest.main()
