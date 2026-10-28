from __future__ import annotations

import tempfile
import unittest

from facesearch.group_analysis import analyze_group, normalize_options


class GroupAnalysisTests(unittest.TestCase):
    def test_options_are_clamped(self):
        options = normalize_options({
            "min_similarity": 150,
            "candidates_per_face": 99,
            "friend_weight": -5,
            "common_friend_weight": 100,
            "city_weight": 2.5,
            "check_vk_friends": False,
        })
        self.assertEqual(options["min_similarity"], 100.0)
        self.assertEqual(options["candidates_per_face"], 10)
        self.assertEqual(options["friend_weight"], 0.0)
        self.assertEqual(options["common_friend_weight"], 10.0)
        self.assertEqual(options["city_weight"], 2.5)
        self.assertFalse(options["check_vk_friends"])

    def test_city_evidence_ranks_a_coherent_unique_pair_higher(self):
        candidates = [
            [
                {
                    "face_id": 101,
                    "name": "Кандидат А",
                    "profile_url": "https://vk.com/id101",
                    "city": "Москва",
                    "similarity": 60.0,
                },
                {
                    "face_id": 102,
                    "name": "Кандидат В",
                    "profile_url": "https://vk.com/id102",
                    "city": "Казань",
                    "similarity": 60.0,
                },
            ],
            [
                {
                    "face_id": 201,
                    "name": "Кандидат Б",
                    "profile_url": "https://vk.com/id201",
                    "city": "Москва",
                    "similarity": 60.0,
                },
                {
                    "face_id": 202,
                    "name": "Кандидат Г",
                    "profile_url": "https://vk.com/id202",
                    "city": "Казань",
                    "similarity": 60.0,
                },
            ],
        ]
        options = {
            "min_similarity": 43.0,
            "candidates_per_face": 2,
            "friend_weight": 8.0,
            "common_friend_weight": 2.0,
            "city_weight": 1.5,
            "check_vk_friends": False,
        }
        with tempfile.TemporaryDirectory() as app_dir:
            result = analyze_group(candidates, options, app_dir)

        self.assertTrue(result["ok"])
        self.assertGreaterEqual(len(result["combinations"]), 1)
        best = result["combinations"][0]
        self.assertEqual(best["matched_faces"], 2)
        self.assertEqual(len({p["profile_key"] for p in best["profiles"]}), 2)
        self.assertTrue(all(p["city"] == "Москва" for p in best["profiles"]))
        self.assertEqual(result["friend_status"]["status"], "disabled")

    def test_one_profile_is_not_assigned_to_two_faces(self):
        common = {
            "face_id": 301,
            "name": "Общий профиль",
            "profile_url": "https://vk.com/id301",
            "city": "Москва",
            "similarity": 80.0,
        }
        candidates = [
            [dict(common), {
                "face_id": 302, "name": "Другой профиль",
                "profile_url": "https://vk.com/id302", "city": "Тула",
                "similarity": 52.0,
            }],
            [dict(common), {
                "face_id": 303, "name": "Третий профиль",
                "profile_url": "https://vk.com/id303", "city": "Пермь",
                "similarity": 51.0,
            }],
        ]
        options = {"check_vk_friends": False, "candidates_per_face": 2}
        with tempfile.TemporaryDirectory() as app_dir:
            result = analyze_group(candidates, options, app_dir)

        for combination in result["combinations"]:
            keys = [
                p["profile_key"] for p in combination["profiles"]
                if not p.get("unmatched")
            ]
            self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
