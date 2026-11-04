"""크레딧 원장. 가짜 LLM(fake)과 메모리 저장소로 /api/chat 을 불러 확인한다.
테스트마다 새 메모리 저장소를 끼워, 다른 테스트의 대화가 잔액에 섞이지 않게 한다."""
import json

import pytest
from fastapi.testclient import TestClient

from app import main
from app.config import CREDIT_COST_PER_MESSAGE, FREE_SIGNUP_CREDITS
from app.llm import FakeLLM
from app.safety import CRISIS_REPLY
from app.store.base import DuplicateReference
from app.store.memory_store import MemoryStore

UID = "local-user"  # AUTH=none 이면 모든 요청이 이 사용자다
client = TestClient(main.app)


@pytest.fixture
def store(monkeypatch):
    fresh = MemoryStore()
    monkeypatch.setattr(main, "store", fresh)
    main.app.dependency_overrides[main.get_llm] = lambda: FakeLLM(delay=0)
    yield fresh
    main.app.dependency_overrides.clear()


def send(message="오늘 기분 좋아"):
    return client.post("/api/chat", json={"message": message})


def events(response):
    return [json.loads(line) for line in response.text.splitlines()]


def balance():
    return client.get("/api/credits").json()["balance"]


def use_up(store):
    """잔액을 0으로 만든다(원장에 기록을 더해서)."""
    store.add_credit(UID, -balance(), "message", "test:use-up")
    assert balance() == 0


class BrokenLLM(FakeLLM):
    """답을 만들다 실패한다."""

    def chat_stream(self, messages, observation=None):
        raise RuntimeError("모델 오류")
        yield  # noqa: 생성기로 만든다


# ── 처음 온 사용자 ──────────────────────────────────────────────────

def test_new_user_gets_free_credits_once(store):
    assert balance() == FREE_SIGNUP_CREDITS
    assert balance() == FREE_SIGNUP_CREDITS  # 두 번 물어도 한 번만 준다
    rows = client.get("/api/credits").json()["recent"]
    assert [(r["delta"], r["reason"], r["ref"]) for r in rows] == [(FREE_SIGNUP_CREDITS, "signup", f"signup:{UID}")]


# ── 메시지를 보내면 줄어든다 ─────────────────────────────────────────

def test_message_costs_one_credit(store):
    before = balance()
    r = send()
    assert r.status_code == 200 and events(r)[-1]["type"] == "done"
    assert balance() == before - CREDIT_COST_PER_MESSAGE == FREE_SIGNUP_CREDITS - 1
    latest = client.get("/api/credits").json()["recent"][0]
    assert latest["delta"] == -CREDIT_COST_PER_MESSAGE and latest["reason"] == "message"
    assert latest["ref"].startswith("msg:")


def test_each_message_has_its_own_reference(store):
    send(), send()
    refs = [r["ref"] for r in client.get("/api/credits").json()["recent"] if r["reason"] == "message"]
    assert len(refs) == len(set(refs)) == 2
    assert balance() == FREE_SIGNUP_CREDITS - 2 * CREDIT_COST_PER_MESSAGE


def test_recent_shows_last_ten_newest_first(store):
    for _ in range(12):
        send()
    rows = client.get("/api/credits").json()["recent"]
    assert len(rows) == 10 and all(r["reason"] == "message" for r in rows)
    assert rows[0]["created_at"] >= rows[-1]["created_at"]


# ── 잔액 0 이면 402 ─────────────────────────────────────────────────

def test_zero_balance_is_rejected_with_402(store):
    use_up(store)
    r = send()
    assert r.status_code == 402
    assert r.json() == {"error": "insufficient_credits",
                        "message": "크레딧을 다 썼어요. 충전하면 대화를 이어갈 수 있어요.", "balance": 0}
    assert balance() == 0  # 거절한 메시지는 차감하지 않는다
    assert store.recent_messages(UID, 10) == []  # 대화도 저장하지 않는다


def test_last_credit_can_be_used_then_rejected(store):
    store.add_credit(UID, -(balance() - 1), "message", "test:leave-one")
    assert send().status_code == 200 and balance() == 0
    assert send().status_code == 402


def test_crisis_message_gets_safety_reply_even_at_zero(store):
    # 위기 표현은 크레딧과 상관없이 정해진 안전 응답을 받는다(차감하지 않는다)
    use_up(store)
    r = send("요즘 너무 죽고 싶어")
    assert r.status_code == 200
    assert "".join(e["text"] for e in events(r) if e["type"] == "delta") == CRISIS_REPLY
    assert balance() == 0


# ── 답 만들기가 실패하면 되돌린다 ─────────────────────────────────────

def test_failed_reply_keeps_balance(store):
    main.app.dependency_overrides[main.get_llm] = lambda: BrokenLLM(delay=0)
    before = balance()
    r = send()
    assert r.status_code == 200 and events(r)[-1]["type"] == "error"
    assert balance() == before
    rows = client.get("/api/credits").json()["recent"]
    charge, refund = rows[1], rows[0]
    assert (charge["delta"], refund["delta"]) == (-CREDIT_COST_PER_MESSAGE, CREDIT_COST_PER_MESSAGE)
    assert refund["reason"] == "refund" and refund["ref"] == "refund:" + charge["ref"].removeprefix("msg:")


# ── 원장 규칙(메모리 저장소도 데이터베이스와 같이) ──────────────────────

def test_duplicate_reference_is_rejected():
    s = MemoryStore()
    s.add_credit("u1", 30, "signup", "signup:u1")
    with pytest.raises(DuplicateReference):
        s.add_credit("u1", 30, "signup", "signup:u1")
    with pytest.raises(DuplicateReference):  # 다른 사용자라도 같은 ref 는 안 된다(전체에서 유일)
        s.add_credit("u2", -1, "message", "signup:u1")
    assert s.credit_balance("u1") == 30 and s.credit_balance("u2") == 0


def test_balance_is_per_user():
    s = MemoryStore()
    s.add_credit("u1", 30, "signup", "signup:u1")
    s.add_credit("u2", 30, "signup", "signup:u2")
    s.add_credit("u1", -1, "message", "msg:a")
    assert (s.credit_balance("u1"), s.credit_balance("u2")) == (29, 30)
    assert [e["ref"] for e in s.recent_credits("u2", 10)] == ["signup:u2"]


def test_delete_all_keeps_ledger(store):
    # 내 데이터 삭제로 원장이 지워지면 무료 크레딧을 다시 받을 수 있다
    send()
    client.delete("/api/data")
    assert balance() == FREE_SIGNUP_CREDITS - CREDIT_COST_PER_MESSAGE
