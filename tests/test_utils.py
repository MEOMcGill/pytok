import os

import pytest

from pytok import utils


@pytest.mark.parametrize("json_file_path", [os.path.join(".", "tests", "data", "20230915-200856_error_videos.json")])
def test_get_video_df(json_file_path):
    if not os.path.exists(json_file_path):
        pytest.skip("fixture data is not committed to the repo")
    csv_file_path = json_file_path.replace(".json", ".csv")
    video_df = utils.try_load_video_df_from_file(csv_file_path, file_paths=[json_file_path])

    assert video_df is not None
    assert len(video_df) > 0

@pytest.mark.parametrize("json_file_path", [os.path.join(".", "tests", "data", "20230915-200856_error_users.json")])
def test_get_user_df(json_file_path):
    if not os.path.exists(json_file_path):
        pytest.skip("fixture data is not committed to the repo")
    csv_file_path = json_file_path.replace(".json", ".csv")
    user_df = utils.try_load_user_df_from_file(csv_file_path, file_paths=[json_file_path])

    assert user_df is not None
    assert len(user_df) > 0

if __name__ == "__main__":
    pytest.main([__file__])
