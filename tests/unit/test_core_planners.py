import pytest

@pytest.mark.unit
def test_string_sanitizer():
    from utils.formatter import truncate_message
    assert truncate_message("12345", 3) == "123"
    assert truncate_message("12", 5) == "12"

@pytest.mark.unit
def test_mock_planning_prompt_render():
    # Insert logic to test prompt string rendering isolated from MLX engine
    pass
