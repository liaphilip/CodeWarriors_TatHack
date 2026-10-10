from app import config


def test_dotenv_loading_and_environment_precedence(monkeypatch):
    monkeypatch.delenv("GEMINI_API_KEY", raising=False)
    monkeypatch.delenv("GEMINI_MODEL", raising=False)
    paths = []

    def local_values(path):
        paths.append(path)
        return {"GEMINI_API_KEY": "local-test-key", "GEMINI_MODEL": "local-test-model"}

    monkeypatch.setattr(config, "dotenv_values", local_values)
    settings = config.load_settings()
    assert settings.configured
    assert settings.api_key == "local-test-key"
    assert paths == [config.ROOT / ".env"]
    assert "local-test-key" not in repr(settings)
    monkeypatch.setenv("GEMINI_API_KEY", "environment-test-key")
    monkeypatch.setenv("GEMINI_MODEL", "environment-test-model")
    assert config.load_settings().api_key == "environment-test-key"
    assert config.load_settings().model == "environment-test-model"


def test_placeholders_are_not_valid_configuration():
    assert not config.Settings("your_gemini_api_key_here", "your_gemini_model_id_here").configured
    assert not config.Settings("test-key", "").configured
