from pathlib import Path


def test_config_has_no_import_time_directory_creation():
    source = Path(__file__).resolve().parents[1] / "app" / "config.py"
    text = source.read_text(encoding="utf-8")
    assert ".mkdir(" not in text
    assert "def get_settings()" in text
