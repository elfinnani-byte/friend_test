import json
from datetime import datetime, timedelta, timezone

import pytest
from fastapi.testclient import TestClient

from app import agent
from app.llm import FakeLLM
from app.main import app, store
from app.safety import CRISIS_KEYWORDS, CRISIS_REPLY
from app.store.memory_store import MemoryStore

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


def run_turn_with_panel(message):
    """패널 이벤트까지 보려고 run_turn 을 직접 부른다(.env 의 SHOW_TURN_PANEL 과 상관없이)."""
    turn_store = MemoryStore()
    events = list(agent.run_turn(message, "u1", FakeLLM(delay=0), turn_store, show_panel=True))
    return events, turn_store


@pytest.mark.parametrize("keyword", CRISIS_KEYWORDS)
def test_crisis_keyword_stops_turn_with_fixed_reply(keyword):
    events, turn_store = run_turn_with_panel(f"요즘 너무 {keyword}")
    # 정해진 안전 응답만 보내고 끝낸다. 도구 선택, 답 생성, 기억 정리로 넘어가지 않는다
    assert [e["type"] for e in events] == ["turn", "delta", "done"]
    assert events[1]["text"] == CRISIS_REPLY
    turn = events[0]
    assert (turn["action"], turn["decided_by"], turn["tools"]) == ("safety", "rule", [])
    reply = turn_store.recent_messages("u1", 1)[-1]
    assert (reply["role"], reply["content"], reply["meta"]["action"]) == ("assistant", CRISIS_REPLY, "safety")


@pytest.mark.parametrize("message", ["살기 좋은 동네 추천해 줘", "오늘 점심 메뉴 골라 줘", "강아지 이름 지어 줘"])
def test_unrelated_message_is_not_crisis(message):
    events, _ = run_turn_with_panel(message)
    turn = next(e for e in events if e["type"] == "turn")
    assert turn["action"] != "safety"
    assert CRISIS_REPLY not in "".join(e["text"] for e in events if e["type"] == "delta")


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
