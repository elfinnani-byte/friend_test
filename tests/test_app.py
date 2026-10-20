import json
from datetime import datetime, timedelta, timezone

from fastapi.testclient import TestClient

from app.main import app, store

client = TestClient(app)


def chat(message):
    r = client.post("/api/chat", json={"message": message})
    assert r.status_code == 200
    return [json.loads(line) for line in r.text.splitlines()]


def test_health():
    assert client.get("/health").json() == {"status": "ok"}


def test_weather_question_picks_weather_tool():
    turn = next(e for e in chat("내일 서울 날씨 어때?") if e["type"] == "turn")
    assert [t["name"] for t in turn["tools"]] == ["get_weather"]


def test_crisis_message_gets_fixed_safety_reply():
    events = chat("요즘 너무 죽고 싶어")
    text = "".join(e["text"] for e in events if e["type"] == "delta")
    assert "109" in text


def test_reminder_is_saved_with_timezone():
    chat("내일 아침 8시에 물 마시라고 알려 줘")
    pending = store.list_reminders("local-user", status="pending")
    assert pending and pending[-1]["due_at"].tzinfo is not None


def test_due_reminder_is_delivered_only_once():
    due = datetime.now(timezone.utc) - timedelta(minutes=1)
    store.add_reminder("local-user", "테스트 약속", due)
    now = datetime.now(timezone.utc)
    first = store.claim_due_reminders("local-user", now)
    second = store.claim_due_reminders("local-user", now)
    assert [r["text"] for r in first].count("테스트 약속") == 1
    assert "테스트 약속" not in [r["text"] for r in second]


def test_ocr_is_off_by_default():
    r = client.post("/api/ocr", files={"photo": ("a.png", b"x", "image/png")})
    assert r.status_code == 404
