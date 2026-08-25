import os
import tempfile

os.environ["MOCK_AI"] = "true"
os.environ["AUTO_PULL_MODEL"] = "false"
os.environ["ADMIN_PASSWORD"] = "test"
os.environ["SESSION_SECRET"] = "test-secret"
os.environ["DATA_DIR"] = tempfile.mkdtemp(prefix="internetboard-test-")

from fastapi.testclient import TestClient
from app.main import app
from app.config import settings


def test_health_and_login():
    with TestClient(app) as c:
        h = c.get("/healthz")
        assert h.status_code == 200
        assert h.json()["version"] == "0.4.0"
        assert h.json()["schedule"] == "03:00"
        assert c.get("/").status_code in (200, 303)
        r = c.post("/login", data={"password": settings.admin_password}, follow_redirects=False)
        assert r.status_code == 303
        # Render the main pages too so Jinja/template regressions are caught before the image is pushed.
        assert c.get("/").status_code == 200
        assert c.get("/settings").status_code == 200
        assert c.get("/topics/sanle").status_code == 200
        assert c.get("/api/runs").status_code == 200
