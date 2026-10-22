"""입장 비밀번호(app/gate.py) 테스트. 비밀번호는 테스트 안에서만 켠다."""
from fastapi.testclient import TestClient

from app import gate
from app.main import app


def make_client(monkeypatch, password="open-sesame"):
    monkeypatch.setattr(gate, "APP_PASSWORD", password)
    return TestClient(app)


def test_off_by_default():
    gate.APP_PASSWORD = ""
    c = TestClient(app)
    assert c.get("/api/config").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200


def test_blocks_without_password(monkeypatch):
    c = make_client(monkeypatch)
    assert c.get("/", follow_redirects=False).headers["location"] == "/login"
    assert c.get("/api/config").status_code == 401
    assert c.post("/api/chat", json={"message": "안녕"}).status_code == 401
    assert c.get("/health").status_code == 200
    assert c.get("/login").status_code == 200


def test_wrong_password(monkeypatch):
    c = make_client(monkeypatch)
    r = c.post("/api/login", json={"password": "nope"})
    assert r.status_code == 401
    assert "fa_gate" not in r.cookies
    assert c.get("/api/config").status_code == 401


def test_right_password_lets_in(monkeypatch):
    c = make_client(monkeypatch)
    r = c.post("/api/login", json={"password": "open-sesame"})
    assert r.status_code == 200
    assert "open-sesame" not in r.cookies.get("fa_gate", "")
    assert c.get("/api/config").status_code == 200
    assert c.get("/", follow_redirects=False).status_code == 200
    assert c.get("/login", follow_redirects=False).headers["location"] == "/"


def test_changing_password_logs_everyone_out(monkeypatch):
    c = make_client(monkeypatch)
    c.post("/api/login", json={"password": "open-sesame"})
    monkeypatch.setattr(gate, "APP_PASSWORD", "new-one")
    assert c.get("/api/config").status_code == 401


def test_logout(monkeypatch):
    c = make_client(monkeypatch)
    c.post("/api/login", json={"password": "open-sesame"})
    c.post("/api/logout")
    assert c.get("/api/config").status_code == 401
