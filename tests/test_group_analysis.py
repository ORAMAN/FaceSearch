from __future__ import annotations

import tempfile
import unittest

from unittest.mock import patch

from facesearch.group_analysis import (
    analyze_group,
    normalize_options,
    _load_friend_sets,
    _request_vk_friends,
)


class GroupAnalysisTests(unittest.TestCase):

    def test_friend_lookup_uses_public_vk_loader_api_helper(self):
        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        class FakeRequests:
            @staticmethod
            def Session():
                return FakeSession()

        class FakeVkLoader:
            requests = FakeRequests
            FRIENDS_PAGE_SIZE = 5000
            calls = []

            @classmethod
            def get_user_friend_ids(cls, user_id, session, limit):
                cls.calls.append((user_id, limit))
                return {7001, 7002, 7003}

        user_id = 987654321
        result, state = _request_vk_friends(user_id, FakeVkLoader)

        self.assertEqual(state, "ok")
        self.assertEqual(result, {7001, 7002, 7003})
        self.assertEqual(FakeVkLoader.calls, [(user_id, 5000)])


    def test_friend_sets_use_shared_vk_loader_without_legacy_token_reader(self):
        class FakeSession:
            def __enter__(self):
                return self

            def __exit__(self, exc_type, exc_value, traceback):
                return False

        class FakeRequests:
            @staticmethod
            def Session():
                return FakeSession()

        class FakeVkLoader:
            requests = FakeRequests
            FRIENDS_PAGE_SIZE = 5000
            _TOKEN_LIST = []
            calls = []

            @classmethod
            def get_user_friend_ids(cls, user_id, session, limit):
                cls.calls.append((user_id, limit))
                return {7770001, 7770002}

        user_id = 1999000021
        candidates = [[{
            "profile_key": f"vk:{user_id}",
            "vk_user_id": user_id,
            "name": "Тестовый профиль",
            "similarity": 70.0,
            "city": "",
        }]]
        FakeVkLoader.calls.clear()

        with patch(
            "facesearch.group_analysis._get_vk_loader",
            return_value=(FakeVkLoader, ""),
        ):
            friends, status = _load_friend_sets(
                candidates,
                ".",
                True,
                lambda percent, message: None,
            )

        self.assertEqual(friends, {user_id: {7770001, 7770002}})
        self.assertEqual(status["status"], "ok")
        self.assertEqual(FakeVkLoader.calls, [(user_id, 5000)])

    def test_group_analysis_keeps_anna_when_daryas_stored_elena_face_scores_higher_for_elena(self):
        # Один и тот же face_id из фотографии в профиле Дарьи подходит Елене
        # заметно лучше, чем Анне. Анна при этом сама присутствует в индексе.
        candidates = [
            [
                {
                    "face_id": 1001,
                    "name": "Елена",
                    "profile_url": "https://vk.com/elena",
                    "similarity": 89.0,
                },
                {
                    "face_id": 1002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/darya",
                    "similarity": 88.0,
                },
            ],
            [
                {
                    "face_id": 1002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/darya",
                    "similarity": 55.0,
                },
                {
                    "face_id": 2001,
                    "name": "Анна",
                    "profile_url": "https://vk.com/anna",
                    "similarity": 83.0,
                },
            ],
        ]
        options = {
            "min_similarity": 43.0,
            "candidates_per_face": 5,
            "check_vk_friends": False,
        }
        with tempfile.TemporaryDirectory() as app_dir:
            result = analyze_group(candidates, options, app_dir)

        self.assertTrue(result["ok"])
        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[0]["name"], "Елена")
        self.assertEqual(by_face[1]["name"], "Анна")
        self.assertGreaterEqual(result["cross_face_filtered"], 1)

    def test_group_analysis_leaves_anna_unmatched_when_only_daryas_elena_face_is_found(self):
        # Если в базе нет Анны, не выдаём Дарью за совпадение для Анны:
        # обнаруженное лицо из профиля Дарьи гораздо лучше совпадает с Еленой.
        candidates = [
            [
                {
                    "face_id": 1001,
                    "name": "Елена",
                    "profile_url": "https://vk.com/elena",
                    "similarity": 91.0,
                },
                {
                    "face_id": 1002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/darya",
                    "similarity": 88.0,
                },
            ],
            [
                {
                    "face_id": 1002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/darya",
                    "similarity": 55.0,
                },
            ],
        ]
        options = {
            "min_similarity": 43.0,
            "candidates_per_face": 5,
            "check_vk_friends": False,
        }
        with tempfile.TemporaryDirectory() as app_dir:
            result = analyze_group(candidates, options, app_dir)

        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[0]["name"], "Елена")
        self.assertTrue(by_face[1]["unmatched"])
        self.assertEqual(by_face[1]["name"], "Нет отдельного совпадения")
        self.assertIn("другим лицом", by_face[1]["unmatched_reason"])
        self.assertGreaterEqual(result["cross_face_filtered"], 1)

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

        self.assertGreaterEqual(len(result["combinations"]), 1)
        for combination in result["combinations"]:
            keys = [
                p["profile_key"] for p in combination["profiles"]
                if not p.get("unmatched")
            ]
            self.assertEqual(len(keys), len(set(keys)))


if __name__ == "__main__":
    unittest.main()
