# tests/test_reminder_time.py
from fastapi.testclient import TestClient

from app.main import app

client = TestClient(app)


def test_reminder_is_saved_in_korean_time():
    # 예시 E07: "내일 아침 8시에 물 마시라고 알려 줘" → 약속 시각은 한국 시간 08:00
    client.post("/api/chat", json={"message": "내일 아침 8시에 물 마시라고 알려 줘"})
    labels = [r["due_label"] for r in client.get("/api/reminders").json() if r["text"] == "물 마시기"]
    assert labels and labels[-1].endswith("08:00")
