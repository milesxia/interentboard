import os
os.environ["MOCK_AI"] = "true"
os.environ["AUTO_PULL_MODEL"] = "false"
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["DATA_DIR"] = "/tmp/intelboard-test-data"

from fastapi.testclient import TestClient
from app.main import app


def test_health_and_login():
    with TestClient(app) as c:
        h = c.get("/healthz")
        assert h.status_code == 200
        assert h.json()["schedule"] == "03:00"
        assert c.get("/").status_code in (200, 303)
        r = c.post("/login", data={"password":"test"}, follow_redirects=False)
        assert r.status_code == 303
