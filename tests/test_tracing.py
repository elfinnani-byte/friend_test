"""Langfuse 계측(app/tracing.py). 실제 Langfuse 대신 메모리 내보내기를 붙여, 마스킹까지 거친 뒤 나갈 데이터를 확인한다."""
import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import agent, memory, tracing
from app.llm import FakeLLM
from app.store.memory_store import MemoryStore

PII = "제 번호는 010-1234-5678, 메일은 jisu.kim@example.com, 카드는 4111 1111 1111 1111 이에요"


@pytest.fixture
def exported():
    exporter = InMemorySpanExporter()
    previous = tracing._client
    key = uuid.uuid4().hex  # 공개키마다 클라이언트 자원을 다시 쓰므로 테스트마다 다른 가짜 키로 새로 붙인다
    tracing._client = Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}", base_url="http://localhost:1",
                               environment="test", span_exporter=exporter, mask_otel_spans=tracing.mask_pii)
    yield exporter
    tracing._client = previous


def spans_by_name(exporter):
    tracing.client().flush()
    return {s.name: s for s in exporter.get_finished_spans()}


def kind(span):
    return span.attributes["langfuse.observation.type"]


def test_turn_has_named_observations(exported):
    list(agent.run_turn("오늘 기분이 좋아", "u1", FakeLLM(delay=0), MemoryStore(), show_panel=False))
    spans = spans_by_name(exported)
    assert {"chat-turn", "safety", "choose-tools", "reply", "memory-update"} <= set(spans)
    assert [kind(spans[n]) for n in ("safety", "choose-tools", "reply", "memory-update")] == \
        ["span", "generation", "generation", "generation"]
    assert len({s.context.trace_id for s in spans.values()}) == 1
    assert spans["chat-turn"].parent is None
    for name in ("choose-tools", "reply", "memory-update"):
        assert spans[name].attributes["langfuse.observation.model.name"] == "fake"
        assert spans[name].parent.span_id == spans["chat-turn"].context.span_id
    assert "langfuse.observation.completion_start_time" in spans["reply"].attributes
    picked = {k: spans["choose-tools"].attributes[f"langfuse.observation.metadata.{k}"] for k in ("tools", "decided_by")}
    assert picked == {"tools": "[]", "decided_by": '"rule"'}
    assert spans["chat-turn"].attributes["langfuse.observation.input"] == "오늘 기분이 좋아"
    assert "가짜 응답" in spans["chat-turn"].attributes["langfuse.observation.output"]


def test_tool_span_records_success(exported):
    list(agent.run_turn("내일 오후 3시 병원 가는 거 잊지 않게 알려 줘", "u2", FakeLLM(delay=0), MemoryStore(), show_panel=False))
    spans = spans_by_name(exported)
    assert kind(spans["tool:create_reminder"]) == "span"
    assert spans["tool:create_reminder"].attributes["langfuse.observation.metadata.ok"] == "true"


def test_pii_is_masked_before_export(exported):
    list(agent.run_turn(PII, "u3", FakeLLM(delay=0), MemoryStore(), show_panel=False))
    spans = spans_by_name(exported)
    sent = json.dumps([dict(s.attributes) for s in spans.values()], ensure_ascii=False)
    for raw in ("010-1234-5678", "jisu.kim@example.com", "4111 1111 1111 1111"):
        assert raw not in sent
    assert spans["chat-turn"].attributes["langfuse.observation.input"] == \
        "제 번호는 [전화번호], 메일은 [이메일], 카드는 [카드번호] 이에요"


def test_greeting_is_one_generation_trace(exported):
    agent.greet("u4", FakeLLM(delay=0), MemoryStore())
    spans = spans_by_name(exported)
    assert set(spans) == {"greeting"}
    assert kind(spans["greeting"]) == "generation"
    assert spans["greeting"].attributes["langfuse.observation.model.name"] == "fake"


def test_memory_summary_only_when_summarizing(exported):
    store, llm = MemoryStore(), FakeLLM(delay=0)
    turn = tracing.start("chat-turn")
    assert memory.maybe_summarize(store, "u5", llm, parent=turn) is False
    for i in range(memory.SUMMARIZE_AFTER + 1):
        store.add_message("u5", "user" if i % 2 == 0 else "assistant", f"메시지 {i}")
    assert memory.maybe_summarize(store, "u5", llm, parent=turn) is True
    turn.end()
    tracing.client().flush()
    spans = [s for s in exported.get_finished_spans() if s.name == "memory-summary"]
    assert len(spans) == 1 and kind(spans[0]) == "generation"


@pytest.mark.parametrize("text, expected", [
    ("연락은 010 1234 5678 로", "연락은 [전화번호] 로"),
    ("02-123-4567 사무실", "[전화번호] 사무실"),
    ("+82 10-1234-5678", "[전화번호]"),
    ("a_b+c@mail.co.kr 로 보내", "[이메일] 로 보내"),
    ("카드 5500-0000-0000-0004", "카드 [카드번호]"),
    ("2026-11-02 15:30 회의, 상담전화 109, 1577-0199", "2026-11-02 15:30 회의, 상담전화 109, 1577-0199"),
])
def test_mask_text(text, expected):
    assert tracing.mask_text(text) == expected


def test_mask_value_keeps_escaped_json_valid():
    # 한글 "는"이 는 로 바뀌면 그 숫자와 바로 뒤 전화번호가 이어져 카드 번호로 잘못 잡히던 경우
    value = json.dumps([{"role": "user", "content": "병원 번호는 010-1234-5678"}])
    assert "\\ub294 010" in value
    masked = mask_value_and_load(value)
    assert masked == [{"role": "user", "content": "병원 번호는 [전화번호]"}]


def mask_value_and_load(value):
    return json.loads(tracing.mask_value(value))


def test_masked_messages_stay_valid_json(exported):
    with tracing.observe("reply", "generation", model="fake") as observation:
        tracing.record(observation, model="fake", input=[{"role": "user", "content": PII}], output="네")
    spans = spans_by_name(exported)
    messages = json.loads(spans["reply"].attributes["langfuse.observation.input"])
    assert messages == [{"role": "user", "content": "제 번호는 [전화번호], 메일은 [이메일], 카드는 [카드번호] 이에요"}]


def test_tracing_off_without_keys(monkeypatch, capsys):
    monkeypatch.setenv("LANGFUSE_PUBLIC_KEY", "")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "")
    previous = tracing._client
    try:
        tracing.setup(SimpleNamespace(env="test", revision="local", commit="local"))
        assert '"enabled": false' in capsys.readouterr().out
        list(agent.run_turn("안녕", "u6", FakeLLM(delay=0), MemoryStore(), show_panel=False))  # 꺼져 있어도 그대로 돈다
    finally:
        tracing._client = previous


def today():
    return f"{datetime.now(tracing.KST):%Y-%m-%d}"


def test_turn_attributes_on_every_observation(exported):
    list(agent.run_turn("내일 오후 3시 병원 가는 거 잊지 않게 알려 줘", "user-abc", FakeLLM(delay=0), MemoryStore(),
                        show_panel=False))
    spans = spans_by_name(exported)
    for span in spans.values():
        assert span.attributes["user.id"] == "user-abc"
        assert span.attributes["session.id"] == f"user-abc-{today()}"
        assert span.attributes["langfuse.version"] == "local"
        assert span.attributes["langfuse.environment"] == "test"
        assert span.attributes["langfuse.trace.metadata.commit"] == '"local"'
    # 도구를 고른 뒤 만든 관찰에는 고른 도구, 확신도, 안전 응답 여부와 태그가 붙는다
    for name in ("tool:create_reminder", "reply", "memory-update"):
        a = spans[name].attributes
        assert a["langfuse.trace.metadata.tools"] == '["create_reminder"]'
        assert a["langfuse.trace.metadata.confidence"] == "0.75"
        assert a["langfuse.trace.metadata.safety"] == "false"
        assert a["langfuse.trace.tags"] == ("create_reminder",)
    root = spans["chat-turn"].attributes
    assert root["langfuse.observation.metadata.tools"] == '["create_reminder"]'
    assert root["langfuse.observation.metadata.safety"] == "false"


def test_crisis_turn_is_tagged_safety(exported):
    list(agent.run_turn("죽고 싶어", "user-abc", FakeLLM(delay=0), MemoryStore(), show_panel=False))
    spans = spans_by_name(exported)
    assert spans["safety"].attributes["langfuse.trace.tags"] == ("safety",)
    assert spans["safety"].attributes["langfuse.trace.metadata.safety"] == "true"
    assert spans["chat-turn"].attributes["langfuse.observation.metadata.safety"] == "true"


def test_greeting_attributes(exported):
    agent.greet("user-abc", FakeLLM(delay=0), MemoryStore())
    a = spans_by_name(exported)["greeting"].attributes
    assert (a["user.id"], a["session.id"], a["langfuse.version"]) == ("user-abc", f"user-abc-{today()}", "local")
    assert a["langfuse.trace.metadata.tools"] == "[]"
    assert a["langfuse.trace.metadata.safety"] == "false"
    assert a["langfuse.trace.metadata.commit"] == '"local"'
