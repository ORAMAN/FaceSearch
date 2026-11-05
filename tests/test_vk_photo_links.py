from __future__ import annotations

import unittest

from facesearch.metadata import vk_photo_permalink_from_source_key


class VKPhotoPermalinkTests(unittest.TestCase):
    def test_user_photo_source_key_builds_permalink(self):
        self.assertEqual(
            vk_photo_permalink_from_source_key("vk:user:228823394:photo:228823394_457239184"),
            "https://vk.com/photo228823394_457239184",
        )

    def test_group_photo_source_key_preserves_negative_owner_id(self):
        self.assertEqual(
            vk_photo_permalink_from_source_key("vk:group:12345:photo:-12345_987654321"),
            "https://vk.com/photo-12345_987654321",
        )

    def test_avatar_and_non_vk_source_keys_do_not_guess_photo_id(self):
        for value in (
            "vk:user:12345:avatar",
            "instagram:user:12345:photo:1",
            "vk:user:12345:photo:12345_0",
            "vk:user:0:photo:12345_10",
            "",
            None,
        ):
            with self.subTest(source_key=value):
                self.assertIsNone(vk_photo_permalink_from_source_key(value))


if __name__ == "__main__":
    unittest.main()
