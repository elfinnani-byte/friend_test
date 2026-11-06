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


# ── 비밀번호에서 빠진 경로 ──────────────────────────────────────────

BEFORE = {"/login", "/api/login", "/api/logout", "/health"}  # 퀴즈 API 전부터 열려 있던 경로


def test_only_quiz_solve_was_added_to_open_paths():
    assert gate.OPEN_PATHS - BEFORE == {"/api/quiz/solve"}
    assert BEFORE <= gate.OPEN_PATHS


def test_quiz_solve_passes_gate_without_cookie(monkeypatch):
    from app import main
    c = make_client(monkeypatch)
    monkeypatch.setattr(main.settings, "quiz_enabled", False)
    # 비밀번호 쿠키 없이도 문지기를 지나 퀴즈 API 까지 간다(꺼져 있으므로 401 이 아니라 404)
    assert c.post("/api/quiz/solve", json={"type": "short", "question": "?"}).status_code == 404


def test_every_other_route_still_needs_password(monkeypatch):
    # 앱에 등록된 모든 경로를 쿠키 없이 불러, 원래 열린 경로와 퀴즈 API 말고는 모두 막히는지 본다
    c = make_client(monkeypatch)

    def all_routes(routes):
        # include_router 로 붙인 라우터(gate.router)는 FastAPI 0.150 에서 한 덩어리로 들어 있어 안쪽 경로를 펼친다
        for route in routes:
            inner = getattr(route, "original_router", None)
            yield from all_routes(inner.routes) if inner is not None else [route]

    paths = [getattr(r, "path", None) for r in all_routes(app.routes)]
    assert {"/login", "/api/login", "/api/logout"} <= set(paths)  # 펼치기가 제대로 됐는지
    passed = set()
    for route in all_routes(app.routes):
        path, methods = getattr(route, "path", None), getattr(route, "methods", None) or set()
        if not path or path.startswith(("/docs", "/redoc", "/openapi")):
            continue
        url = path.replace("{", "").replace("}", "").replace("memory_id", "1").replace("reminder_id", "1")
        for method in methods - {"HEAD", "OPTIONS"}:
            r = c.request(method, url, follow_redirects=False)
            blocked = r.status_code == 401 or (r.status_code == 303 and r.headers.get("location") == "/login")
            if not blocked:
                passed.add(path)
    assert passed == gate.OPEN_PATHS
