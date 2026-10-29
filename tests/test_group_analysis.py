from __future__ import annotations

import tempfile
import unittest

from unittest.mock import patch

from facesearch.group_analysis import (
    analyze_group,
    normalize_options,
    _load_friend_sets,
    _request_vk_friends,
    _gender_mismatch_penalty,
    _profile_gender_mismatch_penalty,
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

    def test_socially_connected_anna_beats_higher_similarity_darya(self):
        # Дарья имеет чуть более высокий visual score для второго лица, но
        # Елена и Анна подтверждённо связаны, а с Дарьей связи нет.
        candidates = [
            [
                {
                    "face_id": 1001,
                    "name": "Елена",
                    "profile_url": "https://vk.com/id101",
                    "similarity": 88.0,
                },
            ],
            [
                {
                    "face_id": 2002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/id202",
                    "similarity": 88.0,
                },
                {
                    "face_id": 2001,
                    "name": "Анна",
                    "profile_url": "https://vk.com/id303",
                    "similarity": 72.0,
                },
            ],
        ]
        friends = {
            101: {303, 9001, 9002},
            303: {101, 9001, 9002},
            202: set(),
        }
        options = {
            "min_similarity": 43.0,
            "candidates_per_face": 5,
            "friend_weight": 8.0,
            "common_friend_weight": 2.0,
            "city_weight": 1.5,
            "check_vk_friends": True,
        }
        with tempfile.TemporaryDirectory() as app_dir, patch(
            "facesearch.group_analysis._load_friend_sets",
            return_value=(friends, {"status": "ok", "message": "test"}),
        ):
            result = analyze_group(candidates, options, app_dir)

        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[0]["name"], "Елена")
        self.assertEqual(by_face[1]["name"], "Анна")
        self.assertGreaterEqual(best["connected_pairs"], 1)
        self.assertEqual(best["disconnected_pairs"], 0)

    def test_unrelated_profile_is_left_unmatched_when_no_social_evidence_exists(self):
        # Если список друзей подтверждает, что связь отсутствует, нельзя
        # назначать Дарью Анне только из-за похожести фотографии.
        candidates = [
            [
                {
                    "face_id": 1001,
                    "name": "Елена",
                    "profile_url": "https://vk.com/id101",
                    "similarity": 92.0,
                },
            ],
            [
                {
                    "face_id": 2002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/id202",
                    "similarity": 65.0,
                },
            ],
        ]
        friends = {101: {8001}, 202: {9001}}
        options = {
            "min_similarity": 43.0,
            "candidates_per_face": 5,
            "friend_weight": 8.0,
            "common_friend_weight": 2.0,
            "city_weight": 1.5,
            "check_vk_friends": True,
        }
        with tempfile.TemporaryDirectory() as app_dir, patch(
            "facesearch.group_analysis._load_friend_sets",
            return_value=(friends, {"status": "ok", "message": "test"}),
        ):
            result = analyze_group(candidates, options, app_dir)

        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[0]["name"], "Елена")
        self.assertTrue(by_face[1]["unmatched"])
        self.assertEqual(best["disconnected_pairs"], 0)
        self.assertNotEqual(by_face[1]["name"], "Дарья")

    def test_confident_gender_mismatch_prefers_candidate_compatible_with_source_face(self):
        candidates = [
            [{
                "face_id": 1001,
                "name": "Елена",
                "profile_url": "https://vk.com/id101",
                "similarity": 88.0,
            }],
            [
                {
                    "face_id": 2002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/id202",
                    "similarity": 88.0,
                },
                {
                    "face_id": 2001,
                    "name": "Анна",
                    "profile_url": "https://vk.com/id303",
                    "similarity": 85.0,
                },
            ],
        ]
        # Numeric labels are the actual InsightFace contract: 0=female, 1=male.
        predictions = {
            1001: {"gender": 0, "confidence": 0.96},
            2002: {"gender": 1, "confidence": 0.97},
            2001: {"gender": 0, "confidence": 0.94},
        }

        with tempfile.TemporaryDirectory() as app_dir:
            result = analyze_group(
                candidates,
                {
                    "check_vk_friends": False,
                    "check_gender": True,
                    "gender_weight": 4.0,
                    "candidates_per_face": 5,
                },
                app_dir,
                query_genders=[
                    {"gender": 0, "confidence": 0.96},
                    {"gender": 0, "confidence": 0.95},
                ],
                gender_classifier=lambda candidate: predictions[int(candidate["face_id"])],
            )

        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[0]["name"], "Елена")
        self.assertEqual(by_face[1]["name"], "Анна")
        self.assertEqual(by_face[1]["gender_penalty"], 0.0)
        self.assertEqual(by_face[1]["gender"], "female")
        self.assertGreater(
            _gender_mismatch_penalty(
                {"gender": 0, "confidence": 0.96},
                {"gender": 1, "confidence": 0.97},
                {"check_gender": True, "gender_weight": 4.0},
            ),
            0.0,
        )

    def test_profile_sex_mismatch_beats_visual_candidate_with_matching_face_classifier(self):
        candidates = [
            [
                {
                    "face_id": 1001,
                    "name": "Анастасия",
                    "profile_url": "https://vk.com/id101",
                    "similarity": 82.0,
                    "profile_sex": 1,  # VK: 1 = female
                },
                {
                    "face_id": 1002,
                    "name": "Виталий",
                    "profile_url": "https://vk.com/id102",
                    "similarity": 77.0,
                    "profile_sex": 2,  # VK: 2 = male
                },
            ],
            [{
                "face_id": 2001,
                "name": "Андрей",
                "profile_url": "https://vk.com/id201",
                "similarity": 80.0,
                "profile_sex": 2,
            }],
        ]
        # All candidate crops look male to InsightFace. Only profile sex
        # distinguishes the false female-profile match from the alternative.
        predictions = {
            1001: {"gender": 1, "confidence": 0.99},
            1002: {"gender": 1, "confidence": 0.98},
            2001: {"gender": 1, "confidence": 0.98},
        }

        with tempfile.TemporaryDirectory() as app_dir:
            result = analyze_group(
                candidates,
                {
                    "check_vk_friends": False,
                    "check_gender": True,
                    "gender_weight": 4.0,
                    "profile_gender_weight": 16.0,
                    "candidates_per_face": 5,
                },
                app_dir,
                query_genders=[
                    {"gender": 1, "confidence": 0.99},
                    {"gender": 1, "confidence": 0.98},
                ],
                gender_classifier=lambda candidate: predictions[int(candidate["face_id"])],
            )

        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[0]["name"], "Виталий")
        self.assertEqual(by_face[0]["profile_gender"], "male")
        self.assertEqual(by_face[0]["gender"], "male")
        self.assertGreater(by_face[0]["profile_gender_confidence"], 0.9)
        self.assertEqual(by_face[0]["profile_gender_penalty"], 0.0)

        false_match = next(
            item for item in result["all_candidates"] if item["name"] == "Анастасия"
        )
        self.assertEqual(false_match["profile_gender"], "female")
        self.assertGreater(
            _profile_gender_mismatch_penalty(
                {"gender": 1, "confidence": 0.99},
                {"profile_sex": 1},
                {"check_gender": True, "profile_gender_weight": 16.0},
            ),
            10.0,
        )

    def test_uncertain_gender_prediction_does_not_penalize_visual_candidate(self):
        candidates = [
            [{
                "face_id": 1001,
                "name": "Елена",
                "profile_url": "https://vk.com/id101",
                "similarity": 88.0,
            }],
            [
                {
                    "face_id": 2002,
                    "name": "Дарья",
                    "profile_url": "https://vk.com/id202",
                    "similarity": 88.0,
                },
                {
                    "face_id": 2001,
                    "name": "Анна",
                    "profile_url": "https://vk.com/id303",
                    "similarity": 85.0,
                },
            ],
        ]
        # Numeric labels are the actual InsightFace contract: 0=female, 1=male.
        predictions = {
            1001: {"gender": 0, "confidence": 0.96},
            2002: {"gender": 1, "confidence": 0.97},
            2001: {"gender": 0, "confidence": 0.94},
        }

        with tempfile.TemporaryDirectory() as app_dir:
            result = analyze_group(
                candidates,
                {
                    "check_vk_friends": False,
                    "check_gender": True,
                    "gender_weight": 4.0,
                    "candidates_per_face": 5,
                },
                app_dir,
                query_genders=[
                    {"gender": 0, "confidence": 0.96},
                    {"gender": 0, "confidence": 0.58},
                ],
                gender_classifier=lambda candidate: predictions[int(candidate["face_id"])],
            )

        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[1]["name"], "Дарья")
        self.assertEqual(by_face[1]["gender_penalty"], 0.0)

    def test_options_are_clamped(self):
        options = normalize_options({
            "min_similarity": 150,
            "candidates_per_face": 99,
            "friend_weight": -5,
            "common_friend_weight": 100,
            "city_weight": 2.5,
            "check_vk_friends": False,
            "gender_weight": 99,
            "profile_gender_weight": 99,
            "check_gender": False,
        })
        self.assertEqual(options["min_similarity"], 100.0)
        self.assertEqual(options["candidates_per_face"], 10)
        self.assertEqual(options["friend_weight"], 0.0)
        self.assertEqual(options["common_friend_weight"], 10.0)
        self.assertEqual(options["city_weight"], 2.5)
        self.assertFalse(options["check_vk_friends"])
        self.assertEqual(options["gender_weight"], 10.0)
        self.assertEqual(options["profile_gender_weight"], 30.0)
        self.assertFalse(options["check_gender"])

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
