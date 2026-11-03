"""도구 선택 지시문(router)을 Langfuse 프롬프트 관리에서 가져오기. 실제 Langfuse 대신 가짜 클라이언트와 메모리 내보내기를 쓴다."""
import uuid
from datetime import datetime

import pytest
from langfuse import Langfuse
from langfuse.model import Prompt_Text, TextPromptClient
from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

from app import router, tracing
from app.tools import TOOLS

NOW = datetime.fromisoformat("2026-10-12T14:00:00+09:00")


def old_router_prompt():
    """옮기기 전 app/router.py 의 router_prompt 그대로. 옮긴 뒤에도 같은 문장이 나와야 한다."""
    tools = "\n".join(f"- {name}: {t['desc']}. 인자 {t['args']}" for name, t in TOOLS.items())
    return "\n".join([
        "너는 AI 친구 서비스에서 사용자 메시지에 답하기 전에 필요한 도구를 고르는 선택기다.",
        "쓸 수 있는 도구:",
        tools,
        "",
        "규칙:",
        "- 도구가 필요 없는 일상 대화면 tools 를 빈 배열로 둔다. 대부분의 메시지는 여기에 해당한다.",
        f"- 꼭 필요한 도구만 최대 {router.MAX_TOOLS}개 고른다. 묻지 않은 정보의 도구는 덧붙이지 않는다(날씨만 물으면 get_weather 하나).",
        "- 날씨나 미세먼지 이야기를 해도 예보를 묻는 게 아니라 경험이나 하소연이면 도구를 쓰지 않는다.",
        "- location 은 메시지에 도시 이름이 있으면 그 도시, 없으면 사용자 지역이다.",
        "- date 는 [달력]을 보고 정한 YYYY-MM-DD 날짜다. 날짜 표현이 없으면 오늘이다.",
        "- create_reminder 는 사용자가 나중에 알려 달라고 분명히 부탁할 때만 고른다. 일정이 있다는 이야기만 하면 고르지 않는다.",
        "- create_reminder 의 text 는 알려 줄 내용만 짧게 쓴다. 예: '우산 챙기기', '일찍 자기'.",
        "- create_reminder 의 due_at 은 [지금] 시각과 시간대를 기준으로 계산한다. 시각 없이 날짜만 있으면 09:00, 아침 08:00, 점심 12:00, 저녁 19:00, 밤 21:00 으로 한다.",
        "- 최근 대화를 보고 '그럼 모레는?' 같은 이어지는 질문의 도구와 인자를 정한다.",
        "- confidence 는 고른 결과가 맞을 확신을 0과 1 사이 숫자로 쓴다.",
        "- reason 은 고른 이유를 한국어 한 문장으로 쓴다.",
        '- 반드시 JSON 하나로만 답한다: {"tools": [{"name": "도구 이름", "args": {인자}}], "confidence": 0과 1 사이 숫자, "reason": "이유"}',
    ])


def langfuse_prompt(text=router.ROUTER_TEMPLATE, version=1, is_fallback=False):
    """Langfuse 에서 가져온 것과 같은 모양의 프롬프트."""
    return TextPromptClient(Prompt_Text(name="router", prompt=text, version=version, config={},
                                        labels=["production", "staging"], tags=[]), is_fallback=is_fallback)


class ReasoningLLM:
    """도구 선택을 모델에게 맡기는 경로(can_reason=True)를 타되 실제로 부르지 않고, 받은 지시문을 기록한다."""
    can_reason = True
    model = "test-model"

    def __init__(self):
        self.systems = []

    def complete_json(self, system, user):
        self.systems.append(system)
        tracing.record(None, model=self.model, input=user, output="{}")
        return {"tools": [{"name": "get_weather", "args": {"location": "서울"}}], "confidence": 0.9, "reason": "날씨"}


class RecordingPrompts:
    """get_prompt 호출 인자를 기록하고 정해 둔 프롬프트를 돌려준다."""

    def __init__(self, result=None, error=None):
        self.calls, self.result, self.error = [], result, error

    def get_prompt(self, name, **kwargs):
        self.calls.append({"name": name, **kwargs})
        if self.error:
            raise self.error
        return self.result

    def update_current_generation(self, **kwargs):
        pass


# ── 문장이 바뀌지 않았는지 ───────────────────────────────────────────

def test_code_template_compiles_to_same_text_as_before():
    assert router.router_prompt() == old_router_prompt()


def test_langfuse_compile_of_template_gives_same_text():
    # Langfuse SDK 의 {{변수}} 채우기로도 같은 문장이 나온다(JSON 의 { } 는 건드리지 않는다)
    assert router.router_prompt(langfuse_prompt()) == old_router_prompt()


def test_template_has_only_service_variables():
    assert sorted(langfuse_prompt().variables) == ["max_tools", "tools"]


# ── 라벨 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("env, label", [("production", "production"), ("staging", "staging"), ("local", "staging")])
def test_label_follows_env(env, label):
    assert tracing.prompt_label(env) == label


def test_get_prompt_uses_label_chosen_from_env(monkeypatch):
    prompts = RecordingPrompts(result=langfuse_prompt())
    monkeypatch.setattr(tracing, "_enabled", True)
    monkeypatch.setattr(tracing, "_client", prompts)
    monkeypatch.setattr(tracing, "_label", "production")
    router.load_prompt()
    assert prompts.calls[0]["name"] == "router" and prompts.calls[0]["label"] == "production"
    assert prompts.calls[0]["fallback"] == router.ROUTER_TEMPLATE


# ── 가져오지 못할 때 ─────────────────────────────────────────────────

def test_tracing_off_does_not_call_langfuse(monkeypatch):
    prompts = RecordingPrompts(error=AssertionError("부르면 안 된다"))
    monkeypatch.setattr(tracing, "_client", prompts)
    assert router.load_prompt() is None and prompts.calls == []
    llm = ReasoningLLM()
    router.choose("내일 날씨 어때?", [], NOW, "서울", llm)
    assert llm.systems == [old_router_prompt()]


def test_unreachable_langfuse_uses_code_text(monkeypatch):
    # 실제 SDK 로 닿지 않는 주소에서 가져오게 해, SDK 의 fallback 이 코드의 문장을 돌려주는지 본다
    key = uuid.uuid4().hex
    monkeypatch.setattr(tracing, "_client", Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}",
                                                     base_url="http://localhost:1", tracing_enabled=False))
    monkeypatch.setattr(tracing, "_enabled", True)
    prompt = router.load_prompt()
    assert prompt.is_fallback
    llm = ReasoningLLM()
    router.choose("내일 날씨 어때?", [], NOW, "서울", llm)
    assert llm.systems == [old_router_prompt()]


def test_unexpected_error_uses_code_text(monkeypatch):
    monkeypatch.setattr(tracing, "_client", RecordingPrompts(error=RuntimeError("SDK 오류")))
    monkeypatch.setattr(tracing, "_enabled", True)
    llm = ReasoningLLM()
    choice = router.choose("내일 날씨 어때?", [], NOW, "서울", llm)
    assert llm.systems == [old_router_prompt()] and choice.decided_by == "model"


# ── 가져온 프롬프트를 쓰고 choose-tools 에 연결 ─────────────────────────

@pytest.fixture
def exported(monkeypatch):
    exporter = InMemorySpanExporter()
    key = uuid.uuid4().hex
    monkeypatch.setattr(tracing, "_client", Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}",
                                                     base_url="http://localhost:1", span_exporter=exporter))
    monkeypatch.setattr(tracing, "_enabled", True)
    return exporter


def choose_in_generation(llm):
    """agent.py 처럼 choose-tools generation 안에서 도구를 고른다."""
    with tracing.observe("choose-tools", "generation", model=llm.model, input="내일 날씨 어때?"):
        router.choose("내일 날씨 어때?", [], NOW, "서울", llm)
    tracing.client().flush()


def choose_tools_span(exporter):
    return next(s for s in exporter.get_finished_spans() if s.name == "choose-tools")


def test_fetched_prompt_is_used_and_linked_to_choose_tools(exported, monkeypatch):
    edited = router.ROUTER_TEMPLATE.replace("선택기다.", "선택기다. (버전 3 실험)")
    monkeypatch.setattr(tracing.client(), "get_prompt", lambda name, **kw: langfuse_prompt(edited, version=3))
    llm = ReasoningLLM()
    choose_in_generation(llm)
    assert "(버전 3 실험)" in llm.systems[0] and "{{" not in llm.systems[0]
    span = choose_tools_span(exported)
    assert span.attributes["langfuse.observation.prompt.name"] == "router"
    assert span.attributes["langfuse.observation.prompt.version"] == 3


def test_fallback_prompt_is_not_linked(exported, monkeypatch):
    monkeypatch.setattr(tracing.client(), "get_prompt", lambda name, **kw: langfuse_prompt(is_fallback=True, version=0))
    choose_in_generation(ReasoningLLM())
    assert "langfuse.observation.prompt.name" not in choose_tools_span(exported).attributes


def test_rule_choice_does_not_fetch_or_link_prompt(exported, monkeypatch):
    # fake 처럼 모델을 쓰지 않으면 프롬프트를 쓰지 않으므로 가져오지도, 연결하지도 않는다
    calls = []
    monkeypatch.setattr(tracing.client(), "get_prompt", lambda name, **kw: calls.append(name))
    llm = ReasoningLLM()
    llm.can_reason = False
    choose_in_generation(llm)
    assert calls == [] and "langfuse.observation.prompt.name" not in choose_tools_span(exported).attributes


def test_eval_prompt_argument_overrides_fetch(monkeypatch):
    # scripts/eval_tools.py --label 은 고른 라벨의 프롬프트를 model_choose 에 넘긴다
    monkeypatch.setattr(tracing, "_client", RecordingPrompts(error=AssertionError("부르면 안 된다")))
    monkeypatch.setattr(tracing, "_enabled", True)
    llm = ReasoningLLM()
    router.model_choose("내일 날씨 어때?", [], NOW, "서울", llm, prompt=langfuse_prompt("평가용 {{max_tools}}"))
    assert llm.systems == [f"평가용 {router.MAX_TOOLS}"]
