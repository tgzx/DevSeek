import sys
from pathlib import Path

from fastapi.testclient import TestClient

ROOT = Path(__file__).parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from bridge_server import create_app


class TestBridgeServerAuth:
    def test_state_is_open_when_auth_disabled(self, tmp_project):
        app = create_app(str(tmp_project))
        client = TestClient(app)

        status = client.get("/api/auth/status")
        assert status.status_code == 200
        assert status.json() == {"enabled": False, "authenticated": False}
        assert client.get("/api/state").status_code == 200

    def test_login_is_required_when_auth_enabled(self, tmp_project):
        app = create_app(
            str(tmp_project),
            auth_user="tiago",
            auth_password="segredo-forte",
            auth_secret="teste-seguro",
        )
        client = TestClient(app)

        status = client.get("/api/auth/status")
        assert status.status_code == 200
        assert status.json() == {"enabled": True, "authenticated": False}
        assert client.get("/api/state").status_code == 401

        login = client.post(
            "/api/auth/login",
            json={"username": "tiago", "password": "segredo-forte"},
        )
        assert login.status_code == 200
        assert login.json() == {"enabled": True, "authenticated": True}
        assert client.get("/api/state").status_code == 200

    def test_logout_clears_session_cookie(self, tmp_project):
        app = create_app(
            str(tmp_project),
            auth_user="tiago",
            auth_password="segredo-forte",
            auth_secret="teste-seguro",
        )
        client = TestClient(app)

        client.post("/api/auth/login", json={"username": "tiago", "password": "segredo-forte"})
        assert client.get("/api/state").status_code == 200

        logout = client.post("/api/auth/logout")
        assert logout.status_code == 200
        assert logout.json() == {"authenticated": False}
        assert client.get("/api/state").status_code == 401
