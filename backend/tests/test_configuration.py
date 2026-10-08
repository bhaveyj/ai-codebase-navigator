from pathlib import Path

from navigator.config import PROJECT_ROOT, Settings


def test_default_env_file_is_absolute():
    assert Settings.model_config["env_file"] == PROJECT_ROOT / ".env"
    assert Path(Settings.model_config["env_file"]).is_absolute()


def test_relative_paths_are_anchored_to_project(monkeypatch, tmp_path):
    monkeypatch.chdir(tmp_path)
    settings = Settings(
        _env_file=None,
        NAVIGATOR_DATA_DIR=".navigator/test-data",
        NAVIGATOR_ANALYZER="analyzers/typescript/src/cli.mjs",
        NAVIGATOR_DEMO_DIR="tests/fixtures/shop",
    )
    assert settings.data_dir == PROJECT_ROOT / ".navigator/test-data"
    assert settings.analyzer == PROJECT_ROOT / "analyzers/typescript/src/cli.mjs"
    assert settings.demo_dir == PROJECT_ROOT / "tests/fixtures/shop"


def test_absolute_path_configuration_is_preserved(tmp_path):
    settings = Settings(_env_file=None, NAVIGATOR_DATA_DIR=tmp_path)
    assert settings.data_dir == tmp_path


def test_explicit_env_file_works_from_unrelated_directory(monkeypatch, tmp_path):
    env_file = tmp_path / "test-settings.env"
    env_file.write_text("NAVIGATOR_MODE=local\nMONGODB_DATABASE=navigator_test\n", encoding="utf-8")
    monkeypatch.delenv("NAVIGATOR_MODE", raising=False)
    monkeypatch.delenv("MONGODB_DATABASE", raising=False)
    monkeypatch.chdir(tmp_path)
    settings = Settings(_env_file=env_file)
    assert settings.mode == "local"
    assert settings.mongodb_database == "navigator_test"
