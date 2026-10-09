import json
import socket
import sys
import threading
import time
from pathlib import Path
from urllib.error import HTTPError
from urllib.request import Request, urlopen

import pytest
import uvicorn

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))

from app import create_app
from database import init_database
from schemas import DEMO_PROPOSAL


@pytest.fixture
def db_path(tmp_path):
    path = tmp_path / "test.db"
    init_database(path)
    return path


@pytest.fixture
def proposal():
    return DEMO_PROPOSAL.model_copy()


@pytest.fixture
def api(db_path):
    """Exercise real HTTP without adding a test-only HTTP client dependency."""
    sock = socket.socket()
    sock.bind(("127.0.0.1", 0))
    port = sock.getsockname()[1]
    server = uvicorn.Server(uvicorn.Config(create_app(db_path), log_level="error"))
    thread = threading.Thread(target=server.run, kwargs={"sockets": [sock]}, daemon=True)
    thread.start()
    deadline = time.monotonic() + 10
    while not server.started:
        if not thread.is_alive() or time.monotonic() > deadline:
            server.should_exit = True
            thread.join(timeout=5)
            sock.close()
            pytest.fail("Test HTTP server failed to start")
        time.sleep(0.01)

    def request(method, path, data=None):
        body = None if data is None else json.dumps(data).encode()
        req = Request(f"http://127.0.0.1:{port}{path}", data=body, method=method,
                      headers={"Content-Type": "application/json"})
        try:
            response = urlopen(req, timeout=5)
        except HTTPError as error:
            response = error
        with response:
            raw = response.read().decode()
            result = json.loads(raw) if "application/json" in response.headers.get("Content-Type", "") else raw
            return response.status, result

    try:
        yield request
    finally:
        server.should_exit = True
        thread.join(timeout=10)
        sock.close()
