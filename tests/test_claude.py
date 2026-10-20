"""ClaudeLLM 이 메시지를 Claude 형식으로 바꾸고 답을 읽는 부분만 확인한다. 실제 Claude API는 부르지 않는다."""
from types import SimpleNamespace

from app.llm import ClaudeLLM


def block(text):
    return SimpleNamespace(type="text", text=text)


class FakeStream:
    def __init__(self, chunks, stop_reason="end_turn"):
        self.text_stream = iter(chunks)
        self.stop_reason = stop_reason

    def __enter__(self):
        return self

    def __exit__(self, *args):
        return False

    def get_final_message(self):
        return SimpleNamespace(stop_reason=self.stop_reason)


class FakeMessages:
    def __init__(self, response=None, stream=None):
        self.response = response
        self._stream = stream
        self.calls = []

    def create(self, **kwargs):
        self.calls.append(kwargs)
        return self.response

    def stream(self, **kwargs):
        self.calls.append(kwargs)
        return self._stream


def make(messages):
    llm = ClaudeLLM(api_key="test-key", model="claude-opus-5-5")
    llm.client = SimpleNamespace(beta=SimpleNamespace(messages=messages))
    return llm


def test_to_claude_moves_system_and_starts_with_user():
    system, turns = ClaudeLLM.to_claude([
        {"role": "system", "content": "캐릭터 지시문"},
        {"role": "assistant", "content": "안녕!"},
        {"role": "user", "content": "반가워"},
        {"role": "user", "content": "오늘 뭐 해?"},
    ])
    assert system == "캐릭터 지시문"
    assert turns[0]["role"] == "user"
    assert [t["role"] for t in turns] == ["user", "assistant", "user"]
    assert turns[-1]["content"] == "반가워\n\n오늘 뭐 해?"


def test_complete_json_reads_object_and_sends_fallback_settings():
    messages = FakeMessages(SimpleNamespace(stop_reason="end_turn", content=[block('결과: {"tools": [], "confidence": 0.9}')]))
    llm = make(messages)
    assert llm.complete_json("도구를 고른다", "안녕") == {"tools": [], "confidence": 0.9}
    call = messages.calls[0]
    assert call["fallbacks"] == "default"
    assert call["betas"] == ["server-side-fallback-2026-07-01"]
    assert "JSON 객체 하나만" in call["system"]


def test_refusal_returns_notice():
    llm = make(FakeMessages(SimpleNamespace(stop_reason="refusal", content=[])))
    assert llm.complete_text([{"role": "user", "content": "안녕"}]) == ClaudeLLM.REFUSAL_NOTICE


def test_chat_stream_yields_text_chunks():
    llm = make(FakeMessages(stream=FakeStream(["안녕", "하세요"])))
    assert "".join(llm.chat_stream([{"role": "user", "content": "안녕"}])) == "안녕하세요"
