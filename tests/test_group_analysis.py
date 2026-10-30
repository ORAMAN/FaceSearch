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
    _pair_bonus,
    _prepare_candidates,
    _hungarian_maximize,
    _pair_relation,
    _merge_candidates_across_sources,
)


class GroupAnalysisTests(unittest.TestCase):


    def test_cross_source_profiles_with_same_name_and_known_gender_merge(self):
        options = normalize_options({"min_similarity": 0, "candidates_per_face": 5})
        prepared = _prepare_candidates([
            [{
                "face_id": 101, "profile_id": 501,
                "profile_url": "https://vk.com/id100", "name": "Алексей Иванов",
                "similarity": 94.0, "profile_gender": "male",
            }],
            [{
                "face_id": 202, "profile_id": 601,
                "profile_url": "https://ok.ru/profile/200", "name": "Алексей Иванов",
                "similarity": 92.0, "profile_gender": "male",
            }],
        ], options)

        merged, status = _merge_candidates_across_sources(prepared)
        first = next(item for item in merged[0] if not item.get("unmatched"))
        second = next(item for item in merged[1] if not item.get("unmatched"))

        self.assertEqual(first["profile_key"], second["profile_key"])
        self.assertTrue(first["profile_key"].startswith("cross-source:"))
        self.assertEqual(len(first["source_profiles"]), 2)
        self.assertEqual(status["merged_groups"], 1)

    def test_conflicting_birth_dates_block_cross_source_merge(self):
        options = normalize_options({"min_similarity": 0, "candidates_per_face": 5})
        prepared = _prepare_candidates([
            [{
                "face_id": 101, "profile_id": 501,
                "profile_url": "https://vk.com/id100", "name": "Алексей Иванов",
                "similarity": 94.0, "profile_gender": "male", "birthdate": "1980-02-01",
            }],
            [{
                "face_id": 202, "profile_id": 601,
                "profile_url": "https://ok.ru/profile/200", "name": "Алексей Иванов",
                "similarity": 92.0, "profile_gender": "male", "birthdate": "1985-06-17",
            }],
        ], options)

        merged, status = _merge_candidates_across_sources(prepared)
        first = next(item for item in merged[0] if not item.get("unmatched"))
        second = next(item for item in merged[1] if not item.get("unmatched"))

        self.assertNotEqual(first["profile_key"], second["profile_key"])
        self.assertEqual(status["merged_groups"], 0)

    def test_shared_education_and_workplace_are_profile_relationships(self):
        options = normalize_options({"min_similarity": 0, "candidates_per_face": 5})
        prepared = _prepare_candidates([[
            {
                "face_id": 101, "profile_id": 501,
                "profile_url": "https://vk.com/id100", "name": "Иван Первый",
                "similarity": 94.0,
                "info": {"Школа": "Школа № 12", "Место работы": "ООО Ромашка"},
            },
            {
                "face_id": 202, "profile_id": 601,
                "profile_url": "https://ok.ru/profile/200", "name": "Пётр Второй",
                "similarity": 92.0,
                "metadata": {"school": "Школа 12", "organization": "ооо ромашка"},
            },
        ]], options)[0]
        first, second = [item for item in prepared if not item.get("unmatched")]
        relation = _pair_relation(first, second, {})

        self.assertTrue(relation["same_education"])
        self.assertTrue(relation["same_workplace"])
        self.assertEqual(relation["education_matches"], ["Школа № 12"])
        self.assertEqual(relation["workplace_matches"], ["ООО Ромашка"])

    def test_group_result_exposes_shared_education_and_workplace(self):
        result = analyze_group(
            [
                [{
                    "face_id": 101, "profile_id": 501,
                    "profile_url": "https://vk.com/id100", "name": "Иван Первый",
                    "similarity": 94.0,
                    "info": {"Школа": "Школа № 12", "Место работы": "ООО Ромашка"},
                }],
                [{
                    "face_id": 202, "profile_id": 601,
                    "profile_url": "https://ok.ru/profile/200", "name": "Пётр Второй",
                    "similarity": 92.0,
                    "metadata": {"school": "Школа 12", "organization": "ООО Ромашка"},
                }],
            ],
            {
                "min_similarity": 0, "candidates_per_face": 5,
                "check_vk_friends": False, "check_gender": False,
                "education_weight": 3.5, "workplace_weight": 4.5,
            },
            ".",
        )
        combination = result["combinations"][0]
        profiles = sorted(combination["profiles"], key=lambda item: item["face_index"])

        self.assertEqual(profiles[0]["education"], ["Школа № 12"])
        self.assertEqual(profiles[0]["workplaces"], ["ООО Ромашка"])
        self.assertEqual(profiles[1]["education"], ["Школа 12"])
        self.assertEqual(profiles[1]["workplaces"], ["ООО Ромашка"])
        self.assertEqual(len(combination["relations"]), 1)
        relation = combination["relations"][0]
        self.assertTrue(relation["same_education"])
        self.assertTrue(relation["same_workplace"])
        self.assertEqual(relation["education_matches"], ["Школа № 12"])
        self.assertEqual(relation["workplace_matches"], ["ООО Ромашка"])
        self.assertEqual(_pair_bonus(relation, {
            "friend_weight": 8, "common_friend_weight": 2, "city_weight": 1.5,
            "education_weight": 3.5, "workplace_weight": 4.5,
        }), 8.0)

    def test_candidate_limit_counts_unique_database_profiles_not_face_rows(self):
        # Несколько фотографий/face.id принадлежат одному профилю profiles.id.
        # Даже если у строк разные URL, они должны занимать только одно место.
        candidates = [[
            {
                "face_id": 101, "profile_id": 501,
                "profile_url": "https://example.test/profile/501", "source_url": "",
                "name": "Тот же профиль, лицо 1", "similarity": 96.0,
            },
            {
                "face_id": 102, "profile_id": 501,
                "profile_url": "https://example.test/profile/501", "source_url": "",
                "name": "Тот же профиль, лицо 2", "similarity": 95.0,
            },
            {
                "face_id": 103, "profile_id": 501,
                "profile_url": "https://example.test/profile/501",
                "name": "Тот же профиль, лицо 3", "similarity": 94.0,
            },
            {
                "face_id": 201, "profile_id": 502,
                "profile_url": "https://example.test/profile/502",
                "name": "Другой профиль", "similarity": 90.0,
            },
        ]]
        prepared = _prepare_candidates(
            candidates,
            normalize_options({"candidates_per_face": 2, "min_similarity": 0}),
        )
        selected = [item for item in prepared[0] if not item.get("unmatched")]

        self.assertEqual(len(selected), 2)
        self.assertEqual(
            [item["profile_key"] for item in selected],
            ["profile:501", "profile:502"],
        )

    def test_local_face_without_online_profile_is_excluded_even_with_higher_similarity(self):
        candidates = [[
            {
                "face_id": 99, "profile_id": 900,
                "profile_url": "", "source_url": "https://images.example.test/local-copy.jpg",
                "name": "Локальное лицо без интернет-профиля", "similarity": 99.5,
            },
            {
                "face_id": 100, "profile_id": 901,
                "profile_url": "https://social.example.test/profile/901",
                "name": "Профиль из интернета", "similarity": 91.0,
            },
        ]]
        prepared = _prepare_candidates(
            candidates,
            normalize_options({"candidates_per_face": 5, "min_similarity": 0}),
        )
        selected = [item for item in prepared[0] if not item.get("unmatched")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["face_id"], 100)
        self.assertEqual(selected[0]["name"], "Профиль из интернета")

    def test_face_with_vk_id_is_treated_as_online_profile(self):
        prepared = _prepare_candidates([[
            {
                "face_id": 12, "vk_user_id": 123456,
                "profile_url": "", "name": "Профиль VK", "similarity": 89.0,
            },
        ]], normalize_options({"candidates_per_face": 5, "min_similarity": 0}))
        selected = [item for item in prepared[0] if not item.get("unmatched")]
        self.assertEqual(len(selected), 1)
        self.assertEqual(selected[0]["vk_user_id"], 123456)

    def test_hungarian_assignment_finds_global_optimum_instead_of_greedy_choice(self):
        weights = [
            [10.0, 9.0, 0.0, 0.0],
            [9.0, 0.0, 0.0, 0.0],
        ]
        assignment = _hungarian_maximize(weights)
        self.assertEqual(len(set(assignment)), 2)
        self.assertEqual(sum(weights[row][column] for row, column in enumerate(assignment)), 18.0)

    def test_global_assignment_uses_second_best_profile_to_avoid_unmatched_face(self):
        candidates = [
            [
                {
                    "face_id": 101, "profile_id": 501,
                    "profile_url": "https://social.example.test/profile/501",
                    "name": "Общий лучший кандидат", "similarity": 96.0,
                },
                {
                    "face_id": 102, "profile_id": 502,
                    "profile_url": "https://social.example.test/profile/502",
                    "name": "Альтернативный кандидат", "similarity": 94.0,
                },
            ],
            [
                {
                    "face_id": 201, "profile_id": 501,
                    "profile_url": "https://social.example.test/profile/501",
                    "name": "Общий лучший кандидат", "similarity": 95.0,
                },
            ],
        ]
        result = analyze_group(
            candidates,
            {"check_vk_friends": False, "check_gender": False, "candidates_per_face": 5},
            ".",
        )
        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(best["matched_faces"], 2)
        self.assertEqual(by_face[0]["name"], "Альтернативный кандидат")
        self.assertEqual(by_face[1]["name"], "Общий лучший кандидат")
        self.assertFalse(any(item["unmatched"] for item in best["profiles"]))

    def test_all_local_only_candidates_produce_explanatory_unmatched_reason(self):
        candidates = [
            [{
                "face_id": 10, "profile_id": 10, "profile_url": "",
                "source_url": "https://images.example.test/one.jpg",
                "name": "Только локальное лицо", "similarity": 98.0,
            }],
            [{
                "face_id": 20, "profile_id": 20,
                "profile_url": "https://social.example.test/profile/20",
                "name": "Интернет-профиль", "similarity": 90.0,
            }],
        ]
        result = analyze_group(
            candidates,
            {"check_vk_friends": False, "check_gender": False},
            ".",
        )
        unmatched = next(
            profile for profile in result["combinations"][0]["profiles"]
            if profile["face_index"] == 0
        )
        self.assertTrue(unmatched["unmatched"])
        self.assertIn("без интернет-профиля", unmatched["unmatched_reason"])

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

        # Более сильный индивидуальный визуальный кандидат должен оставаться
        # доступным отдельной вкладкой, даже если социально лучший вариант другой.
        visual_variant = next(
            (
                item for item in result["combinations"]
                if item.get("strategy_label") == "Максимум визуального сходства"
            ),
            None,
        )
        self.assertIsNotNone(visual_variant)
        visual_by_face = {
            item["face_index"]: item for item in visual_variant["profiles"]
        }
        self.assertEqual(visual_by_face[1]["name"], "Дарья")
        self.assertEqual(visual_by_face[1]["similarity"], 88.0)

    def test_user_pinned_profile_is_forced_and_unique_in_every_combination(self):
        pinned = {
            "face_id": 1001,
            "profile_id": 501,
            "name": "Закреплённый профиль",
            "profile_url": "https://vk.com/id101",
            "similarity": 55.0,
            "pinned": True,
        }
        candidates = [
            [dict(pinned)],
            [
                {
                    "face_id": 2001,
                    "profile_id": 501,
                    "name": "Тот же закреплённый профиль",
                    "profile_url": "https://vk.com/id101",
                    "similarity": 99.0,
                },
                {
                    "face_id": 2002,
                    "profile_id": 502,
                    "name": "Другой профиль",
                    "profile_url": "https://vk.com/id202",
                    "similarity": 95.0,
                },
            ],
        ]
        with tempfile.TemporaryDirectory() as app_dir, patch(
            "facesearch.group_analysis._load_friend_sets",
            return_value=({}, {"status": "disabled", "message": "test"}),
        ):
            result = analyze_group(
                candidates,
                {
                    "min_similarity": 90.0,
                    "candidates_per_face": 2,
                    "check_vk_friends": False,
                    "check_gender": False,
                },
                app_dir,
            )

        self.assertTrue(result["combinations"])
        for combination in result["combinations"]:
            by_face = {
                item["face_index"]: item for item in combination["profiles"]
            }
            self.assertEqual(by_face[0]["name"], "Закреплённый профиль")
            self.assertTrue(by_face[0]["pinned"])
            self.assertEqual(by_face[0]["assignment_confidence_percent"], 100)
            self.assertEqual(by_face[0]["assignment_confidence"], "certain")
            self.assertEqual(by_face[0]["similarity"], 55.0)
            self.assertNotEqual(
                by_face[0]["profile_key"],
                by_face[1]["profile_key"],
            )
            matched_keys = [
                item["profile_key"]
                for item in combination["profiles"]
                if not item["unmatched"]
            ]
            self.assertEqual(len(matched_keys), len(set(matched_keys)))

    def test_distinct_user_pinned_profiles_are_not_cross_source_merged(self):
        candidates = [
            [{
                "face_id": 1001,
                "profile_id": 501,
                "name": "Алексей Иванов",
                "profile_url": "https://vk.com/id101",
                "profile_gender": "male",
                "similarity": 70.0,
                "pinned": True,
            }],
            [{
                "face_id": 2001,
                "profile_id": 502,
                "name": "Алексей Иванов",
                "profile_url": "https://ok.ru/profile/202",
                "profile_gender": "male",
                "similarity": 68.0,
                "pinned": True,
            }],
        ]
        with tempfile.TemporaryDirectory() as app_dir, patch(
            "facesearch.group_analysis._load_friend_sets",
            return_value=({}, {"status": "disabled", "message": "test"}),
        ):
            result = analyze_group(
                candidates,
                {"check_vk_friends": False, "check_gender": False},
                app_dir,
            )

        selected = result["combinations"][0]["profiles"]
        self.assertTrue(all(item["pinned"] for item in selected))
        self.assertEqual(
            len({item["profile_key"] for item in selected}),
            2,
        )

    def test_same_profile_cannot_be_pinned_to_two_faces(self):
        candidates = [
            [{
                "face_id": 1001,
                "name": "Профиль А",
                "profile_url": "https://vk.com/id101",
                "similarity": 94.0,
                "pinned": True,
            }],
            [{
                "face_id": 2001,
                "name": "Профиль А",
                "profile_url": "https://vk.com/id101",
                "similarity": 92.0,
                "pinned": True,
            }],
        ]
        with tempfile.TemporaryDirectory() as app_dir, patch(
            "facesearch.group_analysis._load_friend_sets",
            return_value=({}, {"status": "disabled", "message": "test"}),
        ):
            with self.assertRaisesRegex(ValueError, "нельзя закрепить одновременно"):
                analyze_group(
                    candidates,
                    {"check_vk_friends": False, "check_gender": False},
                    app_dir,
                )

    def test_strong_visual_match_is_not_discarded_only_for_missing_social_links(self):
        # Отсутствие дружбы VK не должно отменять сильное индивидуальное совпадение.
        candidates = [
            [{"face_id": 1001, "name": "Елена", "profile_url": "https://vk.com/id101", "similarity": 96.0}],
            [{"face_id": 2002, "name": "Дарья", "profile_url": "https://vk.com/id202", "similarity": 95.0}],
        ]
        friends = {101: {8001}, 202: {9001}}
        options = {
            "min_similarity": 43.0, "candidates_per_face": 5,
            "friend_weight": 8.0, "common_friend_weight": 2.0,
            "city_weight": 1.5, "check_vk_friends": True,
        }
        with tempfile.TemporaryDirectory() as app_dir, patch(
            "facesearch.group_analysis._load_friend_sets",
            return_value=(friends, {"status": "ok", "message": "test"}),
        ):
            result = analyze_group(candidates, options, app_dir)

        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[0]["name"], "Елена")
        self.assertEqual(by_face[1]["name"], "Дарья")
        self.assertFalse(by_face[1]["unmatched"])
        self.assertEqual(best["disconnected_pairs"], 1)

    def test_profile_sex_mismatch_does_not_beat_gender_compatible_candidate_on_social_bonus(self):
        # Уверенное определение пола исходного лица важнее социальной связи
        # с кандидатом, пол которого противоречит исходному.
        candidates = [
            [{"face_id": 1001, "name": "Елена", "profile_url": "https://vk.com/id101", "profile_sex": 2, "similarity": 94.0}],
            [
                {"face_id": 2002, "name": "Дарья", "profile_url": "https://vk.com/id202", "profile_sex": 1, "similarity": 96.0},
                {"face_id": 2001, "name": "Алексей", "profile_url": "https://vk.com/id303", "profile_sex": 2, "similarity": 82.0},
            ],
        ]
        friends = {101: {202, 8001}, 202: {101, 8001}, 303: {9002}}
        predictions = {
            1001: {"gender": "male", "confidence": 0.99},
            2002: {"gender": "female", "confidence": 0.99},
            2001: {"gender": "male", "confidence": 0.99},
        }
        with tempfile.TemporaryDirectory() as app_dir, patch(
            "facesearch.group_analysis._load_friend_sets",
            return_value=(friends, {"status": "ok", "message": "test"}),
        ):
            result = analyze_group(
                candidates,
                {
                    "check_vk_friends": True, "check_gender": True,
                    "gender_weight": 4.0, "profile_gender_weight": 16.0,
                    "candidates_per_face": 5,
                },
                app_dir,
                query_genders=[
                    {"gender": "male", "confidence": 0.99},
                    {"gender": "male", "confidence": 0.99},
                ],
                gender_classifier=lambda candidate: predictions[int(candidate["face_id"])],
            )
        best = result["combinations"][0]
        by_face = {item["face_index"]: item for item in best["profiles"]}
        self.assertEqual(by_face[1]["name"], "Алексей")
        self.assertEqual(by_face[1]["profile_gender"], "male")
        self.assertGreater(by_face[1]["similarity"], 80.0)

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
            item for item in result["candidates"] if item["name"] == "Анастасия"
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
