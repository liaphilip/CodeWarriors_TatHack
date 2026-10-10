from fastapi.testclient import TestClient
import pytest
from hashlib import sha256
from pathlib import Path
import re

from app.config import Settings
from app.database import outbox_count
from app.main import create_app
from test_agent import FakeProvider, successful_turns
from app.config import ROOT


def test_homepage_versions_assets_and_does_not_cache_state(tmp_path):
    with TestClient(create_app(Settings(database=tmp_path / "test.db"))) as client:
        page = client.get("/")
        assert page.headers["cache-control"] == "no-store"
        for name in ("app.js", "style.css"):
            digest = sha256((ROOT / "static" / name).read_bytes()).hexdigest()[:16]
            url = f"/static/{name}?v={digest}"
            assert url in page.text
            assert client.get(url).status_code == 200
        assert client.get("/", headers={"If-Modified-Since": "Wed, 01 Jan 2099 00:00:00 GMT"}).status_code == 200
        created = client.post("/api/conversations")
        assert created.headers["cache-control"] == "no-store"
        history = client.get(f"/api/conversations/{created.json()['conversation_id']}")
        assert history.headers["cache-control"] == "no-store"


def test_javascript_change_invalidates_asset_url(tmp_path, monkeypatch):
    with TestClient(create_app(Settings(database=tmp_path / "test.db"))) as client:
        original = client.get("/").text
        read_bytes = Path.read_bytes

        def changed_asset(path):
            contents = read_bytes(path)
            return contents + b"\n// new version" if path.name == "app.js" else contents

        monkeypatch.setattr(Path, "read_bytes", changed_asset)
        updated = client.get("/").text
        pattern = r'/static/app\.js\?v=[a-f0-9]+'
        assert re.search(pattern, original).group() != re.search(pattern, updated).group()


def test_health_and_missing_configuration(tmp_path):
    settings = Settings(database=tmp_path / "test.db")
    with TestClient(create_app(settings)) as client:
        assert client.get("/health").json()["configured"] is False
        assert client.post("/api/run", json={"task": "hello"}).status_code == 503
        assert client.get("/").status_code == 200
        assert client.get("/static/app.js").status_code == 200
        assert client.get("/.env").status_code == 404
        assert client.get("/static/../.env").status_code == 404


@pytest.mark.parametrize("payload", [{}, {"task": " "}, {"task": 1},
                                      {"task": "x" * 2001}, {"task": "hello", "send": True}])
def test_input_validation(tmp_path, payload):
    with TestClient(create_app(Settings(database=tmp_path / "test.db"))) as client:
        assert client.post("/api/run", json=payload).status_code == 422


def test_run_and_outbox_stays_empty(tmp_path):
    settings = Settings("test-only-key", "test-model", tmp_path / "test.db")
    with TestClient(create_app(settings, lambda _: FakeProvider(successful_turns()))) as client:
        response = client.post("/api/run", json={"task": "Find quarterly report for Alice"})
        assert response.status_code == 200
        assert response.json()["proposals"][0]["sent"] is False
        assert client.get("/health").json()["outbox_count"] == 0
    assert outbox_count(settings.database) == 0


def test_unexpected_errors_do_not_expose_secrets(tmp_path):
    def broken_factory(_):
        raise RuntimeError("secret-test-key")
    settings = Settings("secret-test-key", "test-model", tmp_path / "test.db")
    with TestClient(create_app(settings, broken_factory)) as client:
        response = client.post("/api/run", json={"task": "test"})
        assert response.status_code == 502
        assert "secret-test-key" not in response.text
