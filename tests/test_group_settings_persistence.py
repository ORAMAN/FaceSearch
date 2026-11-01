from __future__ import annotations

import tempfile
import unittest
from pathlib import Path
from unittest.mock import patch

from facesearch import settings as settings_module


class GroupAnalysisSettingsPersistenceTests(unittest.TestCase):
    def test_all_group_analysis_options_survive_settings_xml_round_trip(self):
        expected = {
            "group_min_similarity": 67.0,
            "group_candidates_per_face": 50,
            "group_friend_weight": 17.5,
            "group_common_friend_weight": 8.5,
            "group_city_weight": 6.5,
            "group_education_weight": 7.5,
            "group_workplace_weight": 9.0,
            "group_check_vk_friends": False,
            "group_gender_weight": 9.5,
            "group_profile_gender_weight": 27.0,
            "group_check_gender": False,
        }

        with tempfile.TemporaryDirectory() as temp_dir:
            settings_file = Path(temp_dir) / "settings.xml"
            with patch.object(settings_module, "SETTINGS_FILE", settings_file):
                settings_module._save_gui_settings_data(expected)
                restored = settings_module._load_gui_settings()

        for key, value in expected.items():
            with self.subTest(setting=key):
                self.assertEqual(restored[key], value)


if __name__ == "__main__":
    unittest.main()
