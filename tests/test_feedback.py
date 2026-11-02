"""사용자 반응(좋아요/별로예요)과 신고를 Langfuse 점수로 보내기. 실제 전송 없이 create_score 호출을 기록해 확인한다."""
import json
import uuid

import pytest
from fastapi.testclient import TestClient
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import agent, tracing
from app.llm import FakeLLM
from app.main import app, store
from app.store.memory_store import MemoryStore

client = TestClient(app)
ME = "local-user"  # AUTH=none 에서 로그인 확인이 돌려주는 사용자


class RecordingClient:
    """create_score 를 보내지 않고 기록만 한다."""

    def __init__(self):
        self.scores = []

    def create_score(self, **kwargs):
        self.scores.append(kwargs)

    def flush(self):
        pass


@pytest.fixture
def scores(monkeypatch):
    recorder = RecordingClient()
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "_client", recorder)
    return recorder.scores


def reply(text="답입니다", meta=None, uid=ME):
    return store.add_message(uid, "assistant", text, meta or {})["id"]


# ── 키가 없을 때(계측 꺼짐) ──────────────────────────────────────────

def test_feedback_without_langfuse_skips_score():
    events = [json.loads(line) for line in client.post("/api/chat", json={"message": "안녕"}).text.splitlines()]
    message_id = next(e for e in events if e["type"] == "done")["message_id"]
    r = client.post("/api/feedback", json={"message_id": message_id, "liked": True})
    assert r.status_code == 200 and r.json() == {"ok": True, "scored": False}


def test_report_without_langfuse_keeps_response():
    r = client.post("/api/report", json={"message_id": reply(), "reason": "이상해요"})
    assert r.status_code == 200 and r.json() == {"ok": True}


# ── 내 답 메시지인지 서버에서 확인 ────────────────────────────────────

def test_feedback_on_other_users_message_is_rejected(scores):
    other = reply(meta={"trace_id": "t-other"}, uid="someone-else")
    r = client.post("/api/feedback", json={"message_id": other, "liked": True})
    assert r.status_code == 404 and scores == []


def test_feedback_on_user_message_or_missing_id_is_rejected(scores):
    mine = store.add_message(ME, "user", "내가 쓴 말", {})["id"]
    assert client.post("/api/feedback", json={"message_id": mine, "liked": True}).status_code == 404
    assert client.post("/api/feedback", json={"message_id": 999999, "liked": True}).status_code == 404
    assert scores == []


def test_report_on_other_users_message_keeps_response_but_sends_no_score(scores):
    other = reply(meta={"trace_id": "t-other"}, uid="someone-else")
    r = client.post("/api/report", json={"message_id": other, "reason": "이상해요"})
    assert r.json() == {"ok": True} and scores == []


# ── 점수 이름과 값 ───────────────────────────────────────────────────

@pytest.mark.parametrize("liked, value", [(True, 1), (False, 0)])
def test_feedback_sends_boolean_user_feedback_score(scores, liked, value):
    message_id = reply("비밀스러운 답 내용", {"trace_id": "abc123"})
    r = client.post("/api/feedback", json={"message_id": message_id, "liked": liked})
    assert r.json() == {"ok": True, "scored": True}
    assert scores == [{"name": "user-feedback", "value": value, "trace_id": "abc123", "score_id": "abc123-user-feedback",
                       "data_type": "BOOLEAN", "comment": None}]
    assert "비밀스러운" not in json.dumps(scores, ensure_ascii=False)  # 대화 내용은 점수에 넣지 않는다


def test_report_sends_report_score_with_masked_reason(scores):
    message_id = reply("비밀스러운 답 내용", {"trace_id": "abc123"})
    r = client.post("/api/report", json={"message_id": message_id, "reason": "연락처 010-1234-5678 를 알려 줬어요"})
    assert r.json() == {"ok": True}
    assert scores == [{"name": "report", "value": 1, "trace_id": "abc123", "score_id": "abc123-report",
                       "data_type": "BOOLEAN", "comment": "연락처 [전화번호] 를 알려 줬어요"}]
    assert "비밀스러운" not in json.dumps(scores, ensure_ascii=False)


def test_message_without_trace_id_skips_score(scores):
    r = client.post("/api/feedback", json={"message_id": reply(), "liked": True})
    assert r.json() == {"ok": True, "scored": False} and scores == []


# ── 답을 저장할 때 트레이스 ID 를 meta 에 남긴다 ──────────────────────

def test_reply_meta_has_trace_id_of_chat_turn(monkeypatch):
    exporter = InMemorySpanExporter()
    key = uuid.uuid4().hex
    monkeypatch.setattr(tracing, "_client", Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}",
                                                     base_url="http://localhost:1", span_exporter=exporter))
    monkeypatch.setattr(tracing, "_enabled", True)
    turn_store = MemoryStore()
    list(agent.run_turn("오늘 기분 좋아", "u1", FakeLLM(delay=0), turn_store, show_panel=False))
    tracing.client().flush()
    root = next(s for s in exporter.get_finished_spans() if s.name == "chat-turn")
    saved = turn_store.recent_messages("u1", 1)[-1]
    assert saved["role"] == "assistant"
    assert saved["meta"]["trace_id"] == format(root.context.trace_id, "032x")


def test_reply_meta_has_no_trace_id_when_tracing_is_off():
    turn_store = MemoryStore()
    list(agent.run_turn("오늘 기분 좋아", "u1", FakeLLM(delay=0), turn_store, show_panel=False))
    assert "trace_id" not in turn_store.recent_messages("u1", 1)[-1]["meta"]
