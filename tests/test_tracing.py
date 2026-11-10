"""Langfuse 계측(app/tracing.py). 실제 Langfuse 대신 메모리 내보내기를 붙여, 마스킹까지 거친 뒤 나갈 데이터를 확인한다."""
import json
import uuid
from datetime import datetime
from types import SimpleNamespace

import anthropic
import httpx2
import pytest
from langfuse import Langfuse
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import agent, memory, tracing
from app.llm import ClaudeLLM, FakeLLM
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


# ── Anthropic SDK 자체 계측 ─────────────────────────────────────────────

def fake_claude_api(request):
    """Messages API 대신 고정 응답을 돌려준다. 실제 호출과 비용이 없다."""
    return httpx2.Response(200, json={
        "id": "msg_test", "type": "message", "role": "assistant", "model": "claude-haiku-5-5",
        "content": [{"type": "text", "text": "안녕"}], "stop_reason": "end_turn", "stop_sequence": None,
        "usage": {"input_tokens": 10, "output_tokens": 3}})


def fake_http_client():
    return httpx2.Client(transport=httpx2.MockTransport(fake_claude_api))


def sdk_spans(exporter):
    tracing.client().flush()
    return [s for s in exporter.get_finished_spans()
            if s.name.startswith("anthropic.") or s.instrumentation_scope.name == "com.anthropic.sdk.python"]


def test_claude_client_turns_off_sdk_tracing():
    llm = ClaudeLLM("test-key", "claude-haiku-5-5")
    assert llm.client.open_telemetry["traces"]["enabled"] is False


def test_claude_call_exports_only_our_generation(exported):
    llm = ClaudeLLM("test-key", "claude-haiku-5-5")
    llm.client = llm.client.with_options(http_client=fake_http_client())  # 복사본도 꺼진 설정을 물려받는다
    with tracing.observe("choose-tools", "generation", model=llm.model):
        llm.complete_text([{"role": "user", "content": "안녕"}])
    assert sdk_spans(exported) == []
    spans = spans_by_name(exported)
    assert spans["choose-tools"].attributes["langfuse.observation.model.name"] == "claude-haiku-5-5"
    assert json.loads(spans["choose-tools"].attributes["langfuse.observation.usage_details"]) == {"input": 10, "output": 3}


def test_default_anthropic_client_would_export_sdk_span(exported):
    # 대조군: 계측을 끄지 않은 기본 클라이언트는 span을 내보낸다. 위 테스트가 우연히 통과하지 않음을 보장한다
    client = anthropic.Anthropic(api_key="test-key", http_client=fake_http_client())
    client.beta.messages.create(model="claude-haiku-5-5", max_tokens=10, messages=[{"role": "user", "content": "안녕"}])
    assert [s.name for s in sdk_spans(exported)] == ["anthropic.messages.create"]


# ── flush 로그 ──────────────────────────────────────────────────────────

def flush_logs(captured):
    return [json.loads(line) for line in captured.splitlines() if '"Langfuse flush"' in line]


def test_flush_does_nothing_when_tracing_is_off(monkeypatch, capsys):
    monkeypatch.setattr(tracing, "_enabled", False)
    tracing.flush()
    assert flush_logs(capsys.readouterr().out) == []


def test_flush_logs_only_time_and_success(exported, monkeypatch, capsys):
    monkeypatch.setattr(tracing, "_enabled", True)
    with tracing.observe("safety"):
        pass
    tracing.flush()
    [record] = flush_logs(capsys.readouterr().out)
    assert set(record) == {"severity", "message", "ms", "ok"}
    assert record["ok"] is True and isinstance(record["ms"], int)


def test_flush_log_after_turn_has_no_message_text(exported, monkeypatch, capsys):
    monkeypatch.setattr(tracing, "_enabled", True)
    list(agent.run_turn(f"{PII} 비밀메모XY", "user-abc", FakeLLM(delay=0), MemoryStore(), show_panel=False))
    out = capsys.readouterr().out
    records = flush_logs(out)
    assert len(records) == 1 and records[0]["ok"] is True  # 턴이 끝날 때 한 번 flush 한다
    line = json.dumps(records, ensure_ascii=False)
    for secret in ("비밀메모XY", "010-1234-5678", "jisu.kim@example.com", "pk-lf-", "sk-lf-"):
        assert secret not in line


# ── 샘플링: Cloud Run 이 붙이는 traceparent 의 '샘플 안 함' 표시 ─────────────

UNSAMPLED_TRACEPARENT = "00-0af7651916cd43dd8448eb211c80319c-b7ad6b7169203331-00"  # 끝의 00 = 샘플 안 함


def turn_spans_under_unsampled_parent(provider):
    """'샘플 안 함' 원격 부모 문맥 안에서 대화 한 턴을 돌리고, Langfuse로 나갈 span 을 돌려준다."""
    from opentelemetry.context import attach, detach
    from opentelemetry.trace.propagation.tracecontext import TraceContextTextMapPropagator

    exporter = InMemorySpanExporter()
    previous = tracing._client
    key = uuid.uuid4().hex
    tracing._client = Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}", base_url="http://localhost:1",
                               environment="test", span_exporter=exporter, tracer_provider=provider)
    token = attach(TraceContextTextMapPropagator().extract({"traceparent": UNSAMPLED_TRACEPARENT}))
    try:
        list(agent.run_turn("오늘 기분 좋아", "user-abc", FakeLLM(delay=0), MemoryStore(), show_panel=False))
    finally:
        detach(token)
        tracing._client.flush()
        tracing._client = previous
    return exporter.get_finished_spans()


def test_turn_is_recorded_even_if_remote_parent_is_not_sampled():
    spans = turn_spans_under_unsampled_parent(tracing.make_provider())
    names = {s.name for s in spans}
    assert {"chat-turn", "safety", "choose-tools", "reply", "memory-update"} <= names
    # 트레이스 ID 는 들어온 traceparent 를 그대로 잇는다(Cloud Run trace ID 와 같다)
    assert {format(s.context.trace_id, "032x") for s in spans} == {"0af7651916cd43dd8448eb211c80319c"}


def test_default_sampler_drops_turn_under_unsampled_parent():
    # 대조군: 기본 샘플러(ParentBased)는 원격 부모가 '샘플 안 함'이면 아무것도 기록하지 않는다(staging 에서 빠진 원인)
    from opentelemetry.sdk.trace import TracerProvider
    assert turn_spans_under_unsampled_parent(TracerProvider()) == ()


# ── 코드 리뷰에서 재현한 마스킹 빈틈 ──────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("010 - 1234 - 5678 로", "[전화번호] 로"),          # 하이픈 앞뒤 공백
    ("사무실 02)1234-5678", "사무실 [전화번호]"),          # 지역번호 뒤 괄호
    ("010－1234－5678", "[전화번호]"),                   # 전각 하이픈
    ("카드 1234.5678.9012.3456", "카드 [카드번호]"),
    ("카드 1234 - 5678 - 9012 - 3456", "카드 [카드번호]"),
    ("2026.11.02 15:30 회의, v1.2.3, 192.168.0.1", "2026.11.02 15:30 회의, v1.2.3, 192.168.0.1"),  # 날짜·버전·IP 는 그대로
])
def test_mask_text_more_separators(text, expected):
    assert tracing.mask_text(text) == expected


def test_mask_value_masks_bare_number_in_text_attributes_only():
    assert tracing.mask_value("4111111111111111") == "[카드번호]"            # 입력·출력 원문(기본)
    assert tracing.mask_value("4111111111111111", text=False) == "4111111111111111"  # 다른 metadata 숫자는 그대로
    assert tracing.mask_value("42") == "42"


def test_card_number_only_message_is_masked_in_export(exported):
    list(agent.run_turn("4111111111111111", "u9", FakeLLM(delay=0), MemoryStore(), show_panel=False))
    turn = spans_by_name(exported)["chat-turn"]
    assert "4111111111111111" not in json.dumps(dict(turn.attributes), ensure_ascii=False)


# ── 대화 중 '내 데이터 모두 삭제': 진행 중인 턴이 기억·요약을 다시 쓰지 않는다 ──────────

class DeletingLLM(FakeLLM):
    """기억 정리(complete_json)를 하는 사이에 사용자가 전체 삭제를 누른 상황."""

    can_reason = True  # 기억 정리를 모델(complete_json)로 하게 한다

    def __init__(self, store, uid, when_called):
        super().__init__(delay=0)
        self.store, self.uid, self.when_called = store, uid, when_called

    def complete_json(self, system, user):
        if system != memory.EXTRACT_PROMPT:
            return {"tools": [], "confidence": 0.9, "reason": "가짜"}  # 도구 선택
        if self.when_called == "memory":
            self.store.delete_all(self.uid)
        return {"add": [{"kind": "fact", "text": "사용자 이름은 지수"}]}


def test_delete_all_during_memory_update_does_not_resurrect():
    store = MemoryStore()
    events = list(agent.run_turn("내 이름은 지수야", "u1", DeletingLLM(store, "u1", "memory"), store, show_panel=False))
    assert store.list_memories("u1") == [] and store.recent_messages("u1", 10) == []
    assert events[-1] == {"type": "done", "message_id": None}


def test_delete_all_during_reply_skips_memory():
    store = MemoryStore()

    class DeleteWhileReplying(FakeLLM):
        def chat_stream(self, messages, observation=None):
            store.delete_all("u2")
            yield "알겠어"

    events = list(agent.run_turn("내 이름은 지수야", "u2", DeleteWhileReplying(delay=0), store, show_panel=False))
    assert store.list_memories("u2") == [] and store.recent_messages("u2", 10) == []
    assert "memory_start" not in [e["type"] for e in events] and events[-1]["message_id"] is None


def test_normal_turn_still_saves_memory_and_message_id():
    store = MemoryStore()
    events = list(agent.run_turn("내 이름은 지수야", "u3", DeletingLLM(store, "u3", "never"), store, show_panel=False))
    assert [m["text"] for m in store.list_memories("u3")] == ["사용자 이름은 지수"]
    assert events[-1]["type"] == "done" and events[-1]["message_id"] is not None
