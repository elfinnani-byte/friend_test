"""퀴즈 풀기 API(/api/quiz/solve). 실제 모델 대신 정해 둔 답을 돌려주는 가짜 LLM 으로 확인한다."""
import pytest
from fastapi.testclient import TestClient

from app import main, quiz
from app.config import QUIZ_MAX_PHOTO_BYTES
from app.llm import FakeLLM

client = TestClient(main.app)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100


class ScriptedLLM:
    """모델을 부르지 않는다. 받은 지시문과 이미지를 기록하고 정해 둔 답을 돌려준다."""
    model = "scripted-vision"

    def __init__(self, answer):
        self.answer, self.calls = answer, []

    def complete_vision_json(self, system, text, image, max_tokens, trace_input):
        self.calls.append({"text": text, "image": image, "max_tokens": max_tokens, "trace": trace_input})
        return dict(self.answer)


@pytest.fixture
def solver(monkeypatch):
    """퀴즈 API 를 켜고, 테스트마다 새 분당 제한과 가짜 LLM 을 끼운다."""
    monkeypatch.setattr(main.settings, "quiz_enabled", True)
    monkeypatch.setattr(main, "quiz_limiter", quiz.RateLimiter(100))

    def use(answer):
        llm = ScriptedLLM(answer)
        main.app.dependency_overrides[main.get_quiz_llm] = lambda: llm
        return llm

    yield use
    main.app.dependency_overrides.pop(main.get_quiz_llm, None)


def easy(**body):
    return client.post("/api/quiz/solve", json=body)


def hard(photo=PNG, media_type="image/png", **fields):
    return client.post("/api/quiz/solve", files={"photo": ("quiz.png", photo, media_type)},
                       data={k: str(v) for k, v in fields.items()})


# ── 꺼져 있을 때 ────────────────────────────────────────────────────

def test_disabled_is_404(monkeypatch):
    monkeypatch.setattr(main.settings, "quiz_enabled", False)
    llm = ScriptedLLM({"answer": 1, "reason": "x"})
    main.app.dependency_overrides[main.get_quiz_llm] = lambda: llm
    try:
        r = easy(type="short", question="1+1은?")
        assert r.status_code == 404
        assert hard(type="choice", choice_count=4).status_code == 404
        assert llm.calls == []  # 모델을 부르지 않는다
    finally:
        main.app.dependency_overrides.pop(main.get_quiz_llm, None)


# ── 객관식 번호 변환 ────────────────────────────────────────────────

@pytest.mark.parametrize("screen_number, choice", [(1, 0), (3, 2), (4, 3), ("2번", 1)])
def test_hard_mode_screen_number_becomes_zero_based(solver, screen_number, choice):
    llm = solver({"answer": screen_number, "reason": "화면 문제를 읽었습니다."})
    r = hard(type="choice", choice_count=4)
    assert r.status_code == 200 and r.json() == {"choice": choice, "reason": "화면 문제를 읽었습니다."}
    call = llm.calls[0]
    assert call["image"] == {"data": PNG, "media_type": "image/png"}
    assert "1부터 4까지" in call["text"]


def test_easy_mode_choices_are_shown_from_one_and_answer_is_zero_based(solver):
    llm = solver({"answer": 2, "reason": "서울이 수도입니다."})
    r = easy(type="choice", question="한국의 수도는?", choices=["부산", "서울", "대구"])
    assert r.json() == {"choice": 1, "reason": "서울이 수도입니다."}
    assert "1. 부산\n2. 서울\n3. 대구" in llm.calls[0]["text"]
    assert llm.calls[0]["image"] is None


@pytest.mark.parametrize("answer", [0, 5, "모름", None])
def test_out_of_range_choice_is_error(solver, answer):
    solver({"answer": answer, "reason": "x"})
    r = hard(type="choice", choice_count=4)
    assert r.status_code == 502


def test_easy_mode_image_url_is_passed(solver):
    llm = solver({"answer": 1, "reason": "그림에 고양이가 있다."})
    url = "https://example.com/quiz/cat.png"
    r = easy(type="choice", question="그림 속 동물은?", choices=["고양이", "개"], image_url=url)
    assert r.json()["choice"] == 0 and llm.calls[0]["image"] == {"url": url}


# ── 단답 ────────────────────────────────────────────────────────────

@pytest.mark.parametrize("model_answer", ["7", "7개", "  서울특별시 "])
def test_short_answer_keeps_requested_form(solver, model_answer):
    llm = solver({"answer": model_answer, "reason": "세어 보았습니다."})
    r = easy(type="short", question="사과는 모두 몇 개인가요?")
    assert r.status_code == 200 and r.json() == {"text": model_answer.strip(), "reason": "세어 보았습니다."}
    assert "[단답]" in llm.calls[0]["text"] and "'7개'" in quiz.SYSTEM  # 형식 지시가 지시문에 있다


def test_short_answer_from_photo(solver):
    solver({"answer": 12, "reason": "화면 계산"})
    assert hard(type="short").json() == {"text": "12", "reason": "화면 계산"}


def test_empty_short_answer_is_error(solver):
    solver({"answer": "  ", "reason": "x"})
    assert easy(type="short", question="답은?").status_code == 502


# ── 입력 검사와 제한 ────────────────────────────────────────────────

def test_photo_over_5mb_is_rejected_before_model(solver):
    llm = solver({"answer": 1, "reason": "x"})
    r = hard(photo=b"\x00" * (QUIZ_MAX_PHOTO_BYTES + 1), type="choice", choice_count=4)
    assert r.status_code == 413 and llm.calls == []


def test_photo_exactly_5mb_is_accepted(solver):
    solver({"answer": 1, "reason": "x"})
    assert hard(photo=b"\x00" * QUIZ_MAX_PHOTO_BYTES, type="choice", choice_count=4).status_code == 200


@pytest.mark.parametrize("call", [
    lambda: easy(type="choice", question="보기 없음"),
    lambda: easy(type="essay", question="?"),
    lambda: easy(type="short", question=""),
    lambda: easy(type="short", question="q", image_url="file:///etc/passwd"),
    lambda: hard(type="choice"),                                   # choice_count 없음
    lambda: hard(type="choice", choice_count=4, media_type="text/plain"),
])
def test_bad_input_is_rejected_without_model(solver, call):
    llm = solver({"answer": 1, "reason": "x"})
    assert call().status_code in (400, 415) and llm.calls == []


def test_rate_limit_per_ip(solver, monkeypatch):
    llm = solver({"answer": "1", "reason": "x"})
    monkeypatch.setattr(main, "quiz_limiter", quiz.RateLimiter(2))
    codes = [easy(type="short", question="1+0은?").status_code for _ in range(3)]
    assert codes == [200, 200, 429] and len(llm.calls) == 2


def test_rate_limiter_window_and_ip():
    now = [0.0]
    limiter = quiz.RateLimiter(2, clock=lambda: now[0])
    assert [limiter.allow("1.1.1.1") for _ in range(3)] == [True, True, False]
    assert limiter.allow("2.2.2.2")            # IP 마다 따로 센다
    now[0] = 60.0
    assert limiter.allow("1.1.1.1")            # 1분이 지나면 다시 된다


def test_client_ip_uses_last_forwarded_value():
    class Req:
        headers = {"x-forwarded-for": "6.6.6.6, 203.0.113.7"}
        client = None
    assert quiz.client_ip(Req()) == "203.0.113.7"


# ── 크레딧, 트레이스 입력, 가짜 LLM ──────────────────────────────────

def test_trace_input_has_no_photo_bytes(solver):
    llm = solver({"answer": 1, "reason": "x"})
    hard(type="choice", choice_count=4)
    trace = llm.calls[0]["trace"]
    assert trace == {"mode": "hard", "type": "choice", "choice_count": 4, "photo_bytes": len(PNG), "media_type": "image/png"}


def test_does_not_touch_credits_or_messages(solver):
    solver({"answer": "x", "reason": "x"})
    before = main.store.recent_messages("local-user", 1000)
    easy(type="short", question="아무 문제")
    assert main.store.recent_messages("local-user", 1000) == before


def test_fake_llm_answers_quiz():
    fake = FakeLLM(delay=0)
    assert fake.complete_vision_json(quiz.SYSTEM, "[객관식]\n문제", None, 100, {})["answer"] == 1
    assert fake.complete_vision_json(quiz.SYSTEM, "[단답]\n문제", None, 100, {})["answer"] == "가짜 답"


# ── 실제 모델로 보낼 요청 모양(모델은 부르지 않는다) ─────────────────

class Capture:
    """SDK 의 create 를 대신해 보낸 인자를 기록하고 정해 둔 응답을 돌려준다."""

    def __init__(self, response):
        self.kwargs, self.response = None, response

    def create(self, **kwargs):
        self.kwargs = kwargs
        return self.response


def claude_with(model):
    from types import SimpleNamespace

    from app.llm import ClaudeLLM
    llm = ClaudeLLM.__new__(ClaudeLLM)
    llm.model = model
    reply = SimpleNamespace(stop_reason="end_turn", model=model, usage=None,
                            content=[SimpleNamespace(type="text", text='{"answer": 2, "reason": "r"}')])
    plain, beta = Capture(reply), Capture(reply)
    llm.client = SimpleNamespace(messages=plain, beta=SimpleNamespace(messages=beta))
    return llm, plain, beta


def test_claude_request_has_base64_image_and_short_output():
    llm, plain, beta = claude_with("claude-haiku-5-5")
    answer = llm.complete_vision_json("지시", "[객관식]", {"data": PNG, "media_type": "image/png"}, 1024, {"mode": "hard"})
    assert answer == {"answer": 2, "reason": "r"}
    sent = plain.kwargs  # Haiku 는 대체 모델(fallbacks) 없이 보낸다
    assert beta.kwargs is None and sent["max_tokens"] == 1024 and sent["output_config"] == {"effort": "low"}
    image, text = sent["messages"][0]["content"]
    assert image["type"] == "image" and image["source"]["type"] == "base64" and image["source"]["media_type"] == "image/png"
    assert text == {"type": "text", "text": "[객관식]"}


def test_claude_request_with_url_image_and_fallback_model():
    llm, plain, beta = claude_with("claude-opus-5-5")
    llm.complete_vision_json("지시", "[단답]", {"url": "https://example.com/a.png"}, 1024, {})
    assert plain.kwargs is None and beta.kwargs["fallbacks"] == "default"
    assert beta.kwargs["messages"][0]["content"][0]["source"] == {"type": "url", "url": "https://example.com/a.png"}


def test_openai_request_has_data_url_image():
    from types import SimpleNamespace

    from app.llm import OpenAICompatibleLLM
    llm = OpenAICompatibleLLM.__new__(OpenAICompatibleLLM)
    llm.model, llm.stream_usage = "gpt-4o-mini", True
    reply = SimpleNamespace(model="gpt-4o-mini", usage=None,
                            choices=[SimpleNamespace(message=SimpleNamespace(content='{"answer": "7", "reason": "r"}'))])
    cap = Capture(reply)
    llm.client = SimpleNamespace(chat=SimpleNamespace(completions=cap))
    llm.complete_vision_json("지시", "[단답]", {"data": PNG, "media_type": "image/png"}, 1024, {})
    assert cap.kwargs["max_completion_tokens"] == 1024
    image = cap.kwargs["messages"][1]["content"][1]
    assert image["type"] == "image_url" and image["image_url"]["url"].startswith("data:image/png;base64,")


def test_langfuse_trace_is_one_generation_without_photo(solver, monkeypatch):
    # 실제 Langfuse SDK 로 메모리 내보내기에 남겨, quiz-solve generation 하나가 생기고 사진 내용은 없는지 본다
    import base64 as b64
    import uuid

    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import tracing
    exporter = InMemorySpanExporter()
    key = uuid.uuid4().hex
    monkeypatch.setattr(tracing, "_client", Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}",
                                                     base_url="http://localhost:1", span_exporter=exporter))
    monkeypatch.setattr(tracing, "_enabled", True)
    main.app.dependency_overrides[main.get_quiz_llm] = lambda: FakeLLM(delay=0)
    assert hard(type="choice", choice_count=3).json()["choice"] == 0
    tracing.client().flush()
    spans = [s for s in exporter.get_finished_spans() if s.name == "quiz-solve"]
    assert len(spans) == 1
    attrs = str(spans[0].attributes)
    assert '"photo_bytes"' in attrs and b64.b64encode(PNG).decode() not in attrs


# ── 객관식: 고른 보기 글을 번호보다 믿는다 ───────────────────────────

SLICE = ["[20, 30, 40]", "[20, 40]", "[10, 30, 50]", "[20, 30]"]


def test_hard_mode_number_is_fixed_by_chosen_text(solver):
    # gpt-4o-mini 가 실제로 낸 모양: 이유와 글은 [20, 40](2번)인데 번호는 4
    solver({"choices_read": SLICE, "answer": 4, "answer_text": "[20, 40]", "reason": "20, 40 이 출력된다."})
    assert hard(type="choice", choice_count=4).json() == {"choice": 1, "reason": "20, 40 이 출력된다."}


def test_text_match_ignores_spaces(solver):
    solver({"choices_read": SLICE, "answer": 1, "answer_text": "[20,40]", "reason": "r"})
    assert hard(type="choice", choice_count=4).json()["choice"] == 1


def test_number_and_text_agree_is_unchanged(solver):
    solver({"choices_read": ["부산", "인천", "서울", "대구"], "answer": 3, "answer_text": "서울", "reason": "r"})
    assert hard(type="choice", choice_count=4).json()["choice"] == 2


def test_easy_mode_matches_against_given_choices(solver):
    # 이지모드는 모델이 읽은 목록이 아니라 받은 보기 목록에서 찾는다
    solver({"answer": 1, "answer_text": "서울", "reason": "r"})
    r = easy(type="choice", question="수도는?", choices=["부산", "서울", "대구"])
    assert r.json()["choice"] == 1


@pytest.mark.parametrize("answer_text, expected", [
    (None, 3),            # 글이 없으면 번호를 쓴다
    ("광주", 3),           # 목록에 없는 글이면 번호를 쓴다
])
def test_falls_back_to_number_when_text_is_unclear(solver, answer_text, expected):
    solver({"choices_read": ["부산", "인천", "서울", "대구"], "answer": 4, "answer_text": answer_text, "reason": "r"})
    assert hard(type="choice", choice_count=4).json()["choice"] == expected


def test_duplicate_choice_texts_fall_back_to_number():
    assert quiz.match_choice("A", ["A", "B", "A"]) is None
    result, notes = quiz.to_result({"choices_read": ["A", "B", "A"], "answer": 3, "answer_text": "A"}, 3)
    assert result["choice"] == 2 and notes == []


def test_close_text_matches_only_when_clear():
    assert quiz.match_choice("[20, 40 ]", SLICE) == 1
    assert quiz.match_choice("[20, 30", SLICE) == 3        # 닫는 괄호를 빠뜨려도 가장 가까운 보기가 뚜렷하면 찾는다
    assert quiz.match_choice("[40, 50]", SLICE) is None    # 닮은 보기가 없으면 번호를 쓴다
    assert quiz.match_choice("[20, 30, 4", SLICE) is None  # [20, 30, 40] 과 [20, 30] 사이에서 애매하면 번호를 쓴다


def test_fixed_number_is_noted():
    _, notes = quiz.to_result({"choices_read": SLICE, "answer": 4, "answer_text": "[20, 40]"}, 4)
    assert notes == ["number_from_text"]


# ── 단답: 숫자만 요구하면 숫자만 ─────────────────────────────────────

@pytest.mark.parametrize("model_answer, expected", [("5개", "5"), ("5 개", "5"), ("약 1,000원", "1000"), ("5", "5"),
                                                     ("-3도", "-3"), ("2.5kg", "2.5")])
def test_number_only_strips_unit(solver, model_answer, expected):
    solver({"answer": model_answer, "number_only": True, "reason": "r"})
    assert hard(type="short").json()["text"] == expected


def test_number_only_from_easy_question_text(solver):
    # 모델이 number_only 를 빠뜨려도 이지모드 문제 글에 '숫자만'이 있으면 코드가 숫자만 남긴다
    solver({"answer": "7개", "reason": "r"})
    assert easy(type="short", question="사과는 몇 개? (숫자만 입력)").json()["text"] == "7"


@pytest.mark.parametrize("model_answer", ["7개", "서울"])
def test_without_number_only_answer_is_kept(solver, model_answer):
    solver({"answer": model_answer, "number_only": False, "reason": "r"})
    assert easy(type="short", question="사과는 모두 몇 개인가요?").json()["text"] == model_answer


def test_number_only_with_two_numbers_is_kept(solver):
    solver({"answer": "3개와 4개", "number_only": True, "reason": "r"})
    assert hard(type="short").json()["text"] == "3개와 4개"  # 어느 숫자인지 모르면 고치지 않는다


def test_prompt_asks_for_choice_texts_and_strict_format():
    for word in ("choices_read", "answer_text", "number_only", "'숫자만'이면"):
        assert word in quiz.SYSTEM
