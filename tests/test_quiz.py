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
def test_out_of_range_choice_is_not_error(solver, answer):
    # 답을 안 내면 0점이므로 502 대신, 다시 물어도 번호가 없으면 1번(choice 0)을 낸다
    solver({"answer": answer, "reason": "x"})
    r = hard(type="choice", choice_count=4)
    assert r.status_code == 200 and r.json() == {"choice": 0, "reason": quiz.FALLBACK_REASON}


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
    for word in ("answer_text", "number_only", "'숫자만'이면", "한 문장"):
        assert word in quiz.SYSTEM
    assert "choices_read" not in quiz.SYSTEM  # 응답을 빠르게 하려고 보기 글 목록 전체는 받지 않는다


def test_hard_mode_without_list_uses_model_number(solver):
    # 보기 글 목록이 없으면 하드모드는 글로 번호를 바꿀 수 없으므로 모델이 말한 번호를 쓴다
    solver({"answer": 2, "answer_text": "[20, 40]", "reason": "r"})
    assert hard(type="choice", choice_count=4).json()["choice"] == 1


# ── 사진 줄이기(QUIZ_IMAGE_MAX_SIDE) ────────────────────────────────

def jpeg(width, height):
    import io as _io

    from PIL import Image
    buf = _io.BytesIO()
    Image.new("RGB", (width, height), (200, 50, 50)).save(buf, format="JPEG")
    return buf.getvalue()


def test_shrink_photo_keeps_ratio_and_long_side():
    import io as _io

    from PIL import Image
    small, kind = quiz.shrink_photo(jpeg(1080, 1920), "image/jpeg", 768)
    assert kind == "image/jpeg" and Image.open(_io.BytesIO(small)).size == (432, 768)


@pytest.mark.parametrize("max_side", [0, 2000])
def test_shrink_photo_leaves_small_or_off(max_side):
    data = jpeg(600, 400)
    assert quiz.shrink_photo(data, "image/png", max_side) == (data, "image/png")


def test_server_shrinks_photo_when_set(solver, monkeypatch):
    llm = solver({"answer": "5", "number_only": True, "reason": "r"})
    monkeypatch.setattr(main.settings, "quiz_image_max_side", 768)
    photo = jpeg(1080, 1920)
    assert hard(photo=photo, media_type="image/jpeg", type="short").json()["text"] == "5"
    sent = llm.calls[0]
    assert sent["image"]["media_type"] == "image/jpeg" and len(sent["image"]["data"]) < len(photo)
    assert sent["trace"]["photo_bytes"] == len(photo) and sent["trace"]["sent_bytes"] == len(sent["image"]["data"])


def test_server_does_not_shrink_by_default(solver, monkeypatch):
    llm = solver({"answer": "5", "reason": "r"})
    monkeypatch.setattr(main.settings, "quiz_image_max_side", 0)
    photo = jpeg(1080, 1920)
    hard(photo=photo, media_type="image/jpeg", type="short")
    assert llm.calls[0]["image"]["data"] == photo


def test_timing_is_logged(solver, capsys):
    import json as _json
    solver({"answer": "5", "reason": "r"})
    hard(type="short")
    line = next(_json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert set(line["ms"]) == {"receive", "photo", "llm", "respond"}
    assert line["total_ms"] == sum(line["ms"].values())


def test_trace_is_flushed_after_response(solver, monkeypatch):
    # 응답 본문을 만든 뒤에 flush 가 불린다(TestClient 는 백그라운드 작업까지 마친 뒤 돌아온다)
    from app import tracing
    order = []
    monkeypatch.setattr(tracing, "flush", lambda: order.append("flush"))
    llm = solver({"answer": "5", "reason": "r"})
    original = llm.complete_vision_json

    def recording(*args):
        order.append("llm")
        return original(*args)

    llm.complete_vision_json = recording
    assert hard(type="short").status_code == 200
    assert order == ["llm", "flush"]


@pytest.mark.parametrize("call", [lambda: easy(type="essay", question="?"), lambda: hard(type="short")])
def test_trace_is_flushed_once_even_on_error(solver, monkeypatch, call):
    from app import tracing
    flushed = []
    monkeypatch.setattr(tracing, "flush", lambda: flushed.append(1))
    solver({"answer": "  ", "reason": "r"})  # 단답이 비어 502
    assert call().status_code in (400, 502) and flushed == [1]


# ── 퀴즈 서버에 답 바로 제출 ─────────────────────────────────────────

QUIZ_SERVER = "https://quiz-battle-abc123-uc.us-central1.run.app"
TEAM_TOKEN = "team-token-for-tests-only-0001"


@pytest.fixture
def quiz_server(monkeypatch):
    """퀴즈 서버 대신 MockTransport. 받은 요청을 기록하고 정해 둔 응답을 돌려준다."""
    import httpx
    seen = []
    reply = {"status": 200, "json": {"ok": True, "elapsed": 3.21}}

    def handler(request):
        seen.append(request)
        return httpx.Response(reply["status"], json=reply["json"])

    submitter = quiz.AnswerSubmitter()
    submitter.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(main, "quiz_submitter", submitter)
    monkeypatch.setattr(main.settings, "quiz_server_allowlist", ["https://quiz-battle*.us-central1.run.app"])
    return seen, reply


def submit_fields(**extra):
    return {"quiz_server": QUIZ_SERVER, "token": TEAM_TOKEN, "question_id": "q-7", **extra}


def test_easy_mode_submits_answer_to_quiz_server(solver, quiz_server):
    import json as _json
    seen, _ = quiz_server
    solver({"answer": 2, "answer_text": "서울", "reason": "서울이 수도다."})
    r = easy(type="choice", question="수도는?", choices=["부산", "서울", "대구"], **submit_fields())
    assert r.status_code == 200
    assert r.json() == {"choice": 1, "reason": "서울이 수도다.", "submitted": True, "answer_status": 200, "elapsed": 3.21}
    req = seen[0]
    assert str(req.url) == f"{QUIZ_SERVER}/api/answer" and req.method == "POST"
    assert req.headers["authorization"] == f"Bearer {TEAM_TOKEN}"
    assert _json.loads(req.content) == {"question_id": "q-7", "choice": 1, "reason": "서울이 수도다."}


def test_hard_mode_submits_text_answer(solver, quiz_server):
    import json as _json
    seen, _ = quiz_server
    solver({"answer": "5개", "number_only": True, "reason": "다섯 개다."})
    r = client.post("/api/quiz/solve", files={"photo": ("q.png", PNG, "image/png")},
                    data={"type": "short", **submit_fields()})
    assert r.json() == {"text": "5", "reason": "다섯 개다.", "submitted": True, "answer_status": 200, "elapsed": 3.21}
    assert _json.loads(seen[0].content) == {"question_id": "q-7", "text": "5", "reason": "다섯 개다."}


def test_quiz_server_rejection_is_reported_not_raised(solver, quiz_server):
    seen, reply = quiz_server
    reply.update(status=409, json={"detail": "이미 답을 냈습니다"})
    solver({"answer": "5", "reason": "r"})
    r = easy(type="short", question="몇 개?", **submit_fields())
    body = r.json()
    assert r.status_code == 200 and body["text"] == "5"  # 답은 그대로 돌려준다
    assert body["submitted"] is False and body["answer_status"] == 409 and body["submit_error"] == "이미 답을 냈습니다"


@pytest.mark.parametrize("server", [
    "http://quiz-battle-abc.us-central1.run.app",           # https 아님
    "https://evil.example.com",                             # 목록 밖
    "https://quiz-battle.evil.com",
    "https://quiz-battle-x.evil.us-central1.run.app.evil.com",
    "https://my-quiz-battle.us-central1.run.app",           # quiz-battle 로 시작하지 않음
    "https://quiz-battle-x.evil.us-central1.run.app",       # * 는 점을 넘지 못한다
    "https://quiz-battle-x.us-east1.run.app",               # 다른 리전
    "https://user@quiz-battle-x.us-central1.run.app",       # 사용자 정보
    "https://quiz-battle-x.us-central1.run.app:8443",       # 다른 포트
    "https://quiz-battle-x.us-central1.run.app/api",        # 경로
    "https://quiz-battle-x.us-central1.run.app?x=1",        # 쿼리
])
def test_disallowed_quiz_server_is_400_without_model_or_submit(solver, quiz_server, server):
    seen, _ = quiz_server
    llm = solver({"answer": "5", "reason": "r"})
    r = easy(type="short", question="몇 개?", **submit_fields(quiz_server=server))
    assert r.status_code == 400 and "허용되지 않은" in r.json()["message"]
    assert llm.calls == [] and seen == []  # 모델도 부르지 않고 제출도 하지 않는다


@pytest.mark.parametrize("server", ["https://quiz-battle.us-central1.run.app", f"{QUIZ_SERVER}/",
                                   "https://QUIZ-BATTLE-x.us-central1.run.app"])
def test_allowed_server_forms(server):
    assert quiz.allowed_server(server, ["https://quiz-battle*.us-central1.run.app"]) is not None


@pytest.mark.parametrize("missing", ["quiz_server", "token", "question_id"])
def test_partial_submit_fields_are_400(solver, quiz_server, missing):
    seen, _ = quiz_server
    llm = solver({"answer": "5", "reason": "r"})
    fields = submit_fields()
    fields.pop(missing)
    r = easy(type="short", question="몇 개?", **fields)
    assert r.status_code == 400 and llm.calls == [] and seen == []


def test_allowlist_env_is_read(monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("QUIZ_SERVER_ALLOWLIST", "https://quiz-battle*.us-central1.run.app, https://my-quiz.example.com")
    assert load_settings().quiz_server_allowlist == ["https://quiz-battle*.us-central1.run.app",
                                                     "https://my-quiz.example.com"]
    monkeypatch.delenv("QUIZ_SERVER_ALLOWLIST")
    assert load_settings().quiz_server_allowlist == ["https://quiz-battle*.us-central1.run.app"]


def test_token_is_not_in_logs_or_trace(solver, quiz_server, capsys):
    llm = solver({"answer": 2, "answer_text": "서울", "reason": "r"})
    easy(type="choice", question="수도는?", choices=["부산", "서울"], **submit_fields())
    client.post("/api/quiz/solve", files={"photo": ("q.png", PNG, "image/png")},
                data={"type": "choice", "choice_count": "4", **submit_fields()})
    easy(type="short", question="?", **submit_fields(quiz_server="https://evil.example.com"))  # 거절되는 경우도
    out = capsys.readouterr().out
    assert TEAM_TOKEN not in out
    assert '"퀴즈 답 제출"' in out and "quiz-battle-abc123-uc.us-central1.run.app" in out  # 호스트와 결과는 남긴다
    assert all(TEAM_TOKEN not in str(call["trace"]) and TEAM_TOKEN not in call["text"] for call in llm.calls)


def test_old_inputs_still_work_without_submit(solver, quiz_server):
    # 첫 APK 처럼 제출 정보 없이 보내면 예전과 똑같이 답만 돌려준다(submitted 같은 새 칸이 없다)
    seen, _ = quiz_server
    solver({"answer": 3, "answer_text": "서울", "reason": "r"})
    assert hard(type="choice", choice_count=4).json() == {"choice": 2, "reason": "r"}
    solver({"answer": "7", "reason": "r"})
    assert easy(type="short", question="몇 개?").json() == {"text": "7", "reason": "r"}
    assert seen == []


def test_submitter_does_not_follow_redirects():
    import httpx
    seen = []

    def handler(request):
        seen.append(str(request.url))
        return httpx.Response(307, headers={"location": "https://evil.example.com/steal"})

    submitter = quiz.AnswerSubmitter()
    submitter.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    status, _, problem = submitter.submit({"base": QUIZ_SERVER, "token": TEAM_TOKEN, "question_id": "q"}, {"text": "1"}, "r")
    assert status == 307 and problem and seen == [f"{QUIZ_SERVER}/api/answer"]
    assert quiz.AnswerSubmitter().http.follow_redirects is False


def test_claude_system_prompt_is_cached():
    llm, plain, _ = claude_with("claude-haiku-5-5")
    llm.complete_vision_json("지시", "[단답]", None, 1024, {})
    system = plain.kwargs["system"]
    assert system[0]["cache_control"] == {"type": "ephemeral"} and system[0]["text"].startswith("지시")
    assert plain.kwargs["messages"][0]["content"] == [{"type": "text", "text": "[단답]"}]  # 매번 다른 글은 캐시 지점 뒤


# ── QUIZ_SERVER_SUBMIT 스위치 ───────────────────────────────────────

def test_submit_switch_off_returns_answer_only(solver, quiz_server, monkeypatch):
    # 꺼 두면 제출 정보가 와도 퀴즈 서버에 내지 않고, 예전 응답 모양 그대로 답만 돌려준다(앱이 직접 낸다)
    seen, _ = quiz_server
    monkeypatch.setattr(main.settings, "quiz_server_submit", False)
    solver({"answer": 2, "answer_text": "서울", "reason": "r"})
    r = easy(type="choice", question="수도는?", choices=["부산", "서울", "대구"], **submit_fields())
    assert r.status_code == 200 and r.json() == {"choice": 1, "reason": "r"}
    solver({"answer": "5", "reason": "r"})
    r = client.post("/api/quiz/solve", files={"photo": ("q.png", PNG, "image/png")}, data={"type": "short", **submit_fields()})
    assert r.json() == {"text": "5", "reason": "r"}
    assert seen == []


def test_submit_switch_off_ignores_disallowed_server(solver, quiz_server, monkeypatch, capsys):
    # 꺼 두면 제출 정보를 검사하지 않으므로, 허용 목록 밖 주소여도 400 없이 답을 돌려준다. 토큰은 여전히 남기지 않는다
    seen, _ = quiz_server
    monkeypatch.setattr(main.settings, "quiz_server_submit", False)
    solver({"answer": "5", "reason": "r"})
    r = easy(type="short", question="몇 개?", **submit_fields(quiz_server="https://evil.example.com"))
    assert r.status_code == 200 and r.json() == {"text": "5", "reason": "r"} and seen == []
    out = capsys.readouterr().out
    assert TEAM_TOKEN not in out and '"퀴즈 답 제출"' not in out


def test_submit_switch_on_still_submits(solver, quiz_server, monkeypatch):
    seen, _ = quiz_server
    monkeypatch.setattr(main.settings, "quiz_server_submit", True)
    solver({"answer": "5", "reason": "r"})
    assert easy(type="short", question="몇 개?", **submit_fields()).json()["submitted"] is True and len(seen) == 1


@pytest.mark.parametrize("value, expected", [(None, True), ("", True), ("true", True), ("false", False),
                                             ("False", False), ("0", False), ("off", False)])
def test_submit_switch_env(monkeypatch, value, expected):
    from app.config import load_settings
    if value is None:
        monkeypatch.delenv("QUIZ_SERVER_SUBMIT", raising=False)
    else:
        monkeypatch.setenv("QUIZ_SERVER_SUBMIT", value)
    assert load_settings().quiz_server_submit is expected


# ── 제출 로그: 보낸 답과 퀴즈 서버 응답 ──────────────────────────────

def submit_log(out):
    import json as _json
    return [_json.loads(x) for x in out.splitlines() if '"퀴즈 답 제출"' in x]


def test_submit_log_has_sent_answer_and_response(solver, quiz_server, capsys):
    _, reply = quiz_server
    reply.update(json={"ok": True, "elapsed": 1.5, "reason": "echo 이유 글은 남기지 않음"})
    solver({"answer": 2, "answer_text": "서울", "reason": "서울이 수도라서 고른 아주 비밀스러운 이유"})
    easy(type="choice", question="수도는?", choices=["부산", "서울", "대구"], **submit_fields())
    solver({"answer": "5개", "number_only": True, "reason": "다섯 개라서 고른 이유"})
    client.post("/api/quiz/solve", files={"photo": ("q.png", PNG, "image/png")}, data={"type": "short", **submit_fields()})
    out = capsys.readouterr().out
    logs = submit_log(out)
    assert logs[0]["sent"] == {"question_id": "q-7", "question_id_type": "str", "choice": 1, "choice_type": "int"}
    assert logs[1]["sent"] == {"question_id": "q-7", "question_id_type": "str", "text": "5", "text_type": "str"}
    assert logs[0]["response"] == {"ok": True, "elapsed": 1.5}  # 응답의 reason 칸은 뺀다
    assert TEAM_TOKEN not in out and "비밀스러운 이유" not in out and "다섯 개라서" not in out and "echo 이유" not in out


def test_submit_log_on_rejection_has_response_body(solver, quiz_server, capsys):
    _, reply = quiz_server
    reply.update(status=409, json={"detail": "이미 답을 냈습니다"})
    solver({"answer": "7", "reason": "r"})
    easy(type="short", question="?", **submit_fields())
    log = submit_log(capsys.readouterr().out)[0]
    assert log["severity"] == "WARNING" and log["status"] == 409 and log["ok"] is False
    assert log["response"] == {"detail": "이미 답을 냈습니다"} and log["sent"]["text"] == "7"


def test_submit_log_with_numeric_question_id_keeps_type(solver, quiz_server, capsys):
    # 타입 비교용: JSON 으로 숫자 question_id 가 오면 그대로 보내고 로그에 int 로 남는다
    import json as _json
    seen, _ = quiz_server
    solver({"answer": "7", "reason": "r"})
    easy(type="short", question="?", **submit_fields(question_id=12))
    assert _json.loads(seen[0].content)["question_id"] == 12
    assert submit_log(capsys.readouterr().out)[0]["sent"]["question_id_type"] == "int"


def test_non_json_response_is_cut():
    assert quiz.response_for_log(None, "x" * 1000) == "x" * 300
    assert quiz.response_for_log({"token": "t", "Reason": "r", "ok": 1}, "") == {"ok": 1}



# ── 객관식 번호를 못 찾을 때: 여러 모양 읽기, 다시 묻기, 1번 내기 ─────────────

class SequenceLLM:
    """부를 때마다 준비한 답을 차례로 돌려준다. 예외를 넣으면 그 예외를 낸다."""
    model = "sequence"

    def __init__(self, *answers):
        self.answers, self.calls = list(answers), []

    def complete_vision_json(self, system, text, image, max_tokens, trace_input):
        self.calls.append({"system": system, "text": text, "image": image})
        answer = self.answers.pop(0)
        if isinstance(answer, Exception):
            raise answer
        return dict(answer)


def use_llm(llm):
    main.app.dependency_overrides[main.get_quiz_llm] = lambda: llm
    return llm


@pytest.mark.parametrize("value, number", [
    (2, 2), ("2", 2), ("2번", 2), ("2.", 2), ("(2)", 2), ("②", 2), ("２", 2), ("❷", 2), ("보기 3", 3),
    (" ④ 번 ", 4), (2.0, 2), ("0번, 아니 2번", 2),
])
def test_choice_number_shapes(value, number):
    assert quiz.choice_number(value, 4) == number


@pytest.mark.parametrize("value", [None, True, 0, 5, "모름", "", 2.5, "⑤"])
def test_choice_number_none(value):
    assert quiz.choice_number(value, 4) is None


@pytest.mark.parametrize("value, choice", [("②", 1), ("3번", 2), ("(4)", 3), ("1.", 0)])
def test_hard_mode_reads_number_shapes_without_retry(solver, value, choice):
    llm = use_llm(SequenceLLM({"answer": value, "reason": "r"}))
    assert hard(type="choice", choice_count=4).json() == {"choice": choice, "reason": "r"}
    assert len(llm.calls) == 1  # 번호를 읽었으므로 다시 묻지 않는다


def test_retry_asks_number_only_once(solver):
    llm = use_llm(SequenceLLM({"answer": "잘 모르겠음", "reason": "화면이 흐리다"}, {"answer": 3}))
    r = hard(type="choice", choice_count=4)
    assert r.status_code == 200 and r.json() == {"choice": 2, "reason": "화면이 흐리다"}
    assert len(llm.calls) == 2 and llm.calls[1]["system"] == quiz.RETRY_SYSTEM
    assert llm.calls[1]["image"] == llm.calls[0]["image"]  # 같은 사진으로 다시 묻는다


def test_retry_after_unreadable_model_output(solver):
    # 첫 답이 JSON 이 아니어서 읽지 못해도(예외) 번호만 다시 묻는다
    llm = use_llm(SequenceLLM(ValueError("JSON 응답을 찾지 못했습니다."), {"answer": "②"}))
    r = hard(type="choice", choice_count=4)
    assert r.json() == {"choice": 1, "reason": "보기 번호만 다시 물어 고른 답입니다."} and len(llm.calls) == 2


def test_fallback_to_first_when_retry_fails(solver, capsys):
    llm = use_llm(SequenceLLM({"answer": "모름", "reason": "r"}, RuntimeError("모델 오류")))
    r = hard(type="choice", choice_count=4)
    assert r.status_code == 200 and r.json() == {"choice": 0, "reason": quiz.FALLBACK_REASON}
    assert len(llm.calls) == 2  # 다시 묻기는 한 번만
    assert '"fallback_first"' in capsys.readouterr().out


def test_fallback_answer_is_submitted(solver, quiz_server):
    # 1번이라도 퀴즈 서버에 낸다(답을 안 내면 0점)
    import json as _json
    seen, _ = quiz_server
    use_llm(SequenceLLM({"answer": None, "reason": "r"}, {"answer": "모름"}))
    r = client.post("/api/quiz/solve", files={"photo": ("q.png", PNG, "image/png")},
                    data={"type": "choice", "choice_count": "4", **submit_fields()})
    assert r.json()["submitted"] is True and r.json()["choice"] == 0
    assert _json.loads(seen[0].content)["choice"] == 0


def test_short_answer_failure_is_still_502(solver):
    use_llm(SequenceLLM(ValueError("JSON 응답을 찾지 못했습니다.")))
    assert hard(type="short").status_code == 502
