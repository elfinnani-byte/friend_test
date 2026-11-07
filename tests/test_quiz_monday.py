"""월요일 대회용 퀴즈 풀이: 스트리밍(답이 근거 끝나자마자 제출), 문제 종류별 모델, 근거 20자, 서버 직접 풀기, 미리 깨우기.
실제 모델과 퀴즈 서버를 부르지 않는다. 가짜 스트리밍 LLM 과 MockTransport 를 쓴다."""
import json
import logging

import httpx
import pytest
from fastapi.testclient import TestClient

from app import main, quiz, quiz_auto, quiz_flow
from app.llm import FakeLLM

client = TestClient(main.app)
PNG = b"\x89PNG\r\n\x1a\n" + b"\x00" * 100
QUIZ_SERVER = "https://quiz-battle-abc123-uc.us-central1.run.app"
TEAM_TOKEN = "team-token-for-monday-tests-0001"


class StreamLLM:
    """정해 둔 글 조각을 차례로 흘려보낸다. 무엇을 언제 했는지 events 에 적는다."""

    def __init__(self, chunks, events=None, model="stream-fake", retry=None):
        self.chunks, self.events, self.model, self.retry = chunks, events if events is not None else [], model, retry
        self.calls = []

    def stream_quiz(self, system, text, image, max_tokens, trace_input):
        self.calls.append({"system": system, "text": text, "image": image, "max_tokens": max_tokens})
        for chunk in self.chunks:
            self.events.append(("chunk", chunk))
            yield chunk
        self.events.append(("stream_end",))

    def complete_vision_json(self, system, text, image, max_tokens, trace_input):
        self.events.append(("retry",))
        if isinstance(self.retry, Exception):
            raise self.retry
        return dict(self.retry or {})

    def warm(self, system, max_tokens=16):
        self.events.append(("warm", system))
        return {"cache_write": 600}


@pytest.fixture
def quiz_on(monkeypatch):
    monkeypatch.setattr(main.settings, "quiz_enabled", True)
    monkeypatch.setattr(main.settings, "quiz_server_submit", True)
    monkeypatch.setattr(main.settings, "quiz_server_allowlist", ["https://quiz-battle*.us-central1.run.app"])
    monkeypatch.setattr(main, "quiz_limiter", quiz.RateLimiter(1000))
    yield
    main.app.dependency_overrides.clear()


def use(image_model, text_model=None):
    main.app.dependency_overrides[main.get_quiz_llm] = lambda: image_model
    main.app.dependency_overrides[main.get_quiz_text_llm] = lambda: text_model


@pytest.fixture
def quiz_server(monkeypatch):
    """퀴즈 서버 /api/answer 대신. 받은 시점을 events 에도 적을 수 있게 events 목록을 바꿔 끼울 수 있다."""
    record = {"seen": [], "events": None}

    def handler(request):
        body = json.loads(request.content)
        record["seen"].append(body)
        if record["events"] is not None:
            record["events"].append(("submit", body))
        return httpx.Response(200, json={"accepted": True, "elapsed": 1.1})

    submitter = quiz.AnswerSubmitter()
    submitter.http = httpx.Client(transport=httpx.MockTransport(handler), follow_redirects=False)
    monkeypatch.setattr(main, "quiz_submitter", submitter)
    return record


def submit_fields(**extra):
    return {"quiz_server": QUIZ_SERVER, "token": TEAM_TOKEN, "question_id": "q-1", **extra}


def easy(**body):
    return client.post("/api/quiz/solve", json=body)


def hard(**fields):
    return client.post("/api/quiz/solve", files={"photo": ("q.png", PNG, "image/png")},
                       data={k: str(v) for k, v in fields.items()})


# ── 1. 답 먼저(근거가 끝나는 즉시 한 번 제출) ─────────────────────────

def test_submits_as_soon_as_reason_line_ends(quiz_on, quiz_server):
    events = []
    quiz_server["events"] = events
    llm = StreamLLM(["ANSWER: 2\n", "REASON: 200은 성공", " 코드라 2번\n", "뒤에 더 오는 글\n", "또 오는 글\n"], events)
    use(llm, llm)
    r = easy(type="choice", question="성공 코드는?", choices=["404", "200", "500"], **submit_fields())
    assert r.status_code == 200
    body = r.json()
    assert (body["choice"], body["reason"], body["submitted"]) == (1, "200은 성공 코드라 2번", True)
    kinds = [e[0] for e in events]
    # 근거 줄이 끝난 조각 바로 다음이 제출이고, 뒤에 오는 글은 읽지도 않는다
    assert kinds == ["chunk", "chunk", "chunk", "submit"]
    assert events[-1][1] == {"question_id": "q-1", "choice": 1, "reason": "200은 성공 코드라 2번"}


def test_reason_without_newline_is_cut_at_20_and_submitted(quiz_on, quiz_server):
    events = []
    quiz_server["events"] = events
    llm = StreamLLM(["ANSWER: 1\nREASON: ", "가나다라마바사아자차", "카타파하거너더러머버서어", "줄바꿈이 끝내 안 온다"], events)
    use(llm, llm)
    body = easy(type="short", question="?", **submit_fields()).json()
    assert len(body["reason"]) == 20 and body["reason"] == "가나다라마바사아자차카타파하거너더러머버"
    assert [e[0] for e in events] == ["chunk", "chunk", "chunk", "submit"]  # 20자에 닿은 조각에서 바로 제출


@pytest.mark.parametrize("reason, expected", [
    ("짧은 근거", "짧은 근거"),
    ('"따옴표 근거"', "따옴표 근거"),
    ("200은 성공 코드", "200은 성공 코드"),
    ("정확히 스무 글자로 맞춘 근거입니다요", "정확히 스무 글자로 맞춘 근거입니다요"),        # 20자는 그대로
    ("200은 요청 성공을 뜻하는 상태 코드라서", "200은 요청 성공을 뜻하는 상태"),          # 단어 중간(코|드)이 아니라 띄어쓰기 앞
    ("200은 성공 응답을 뜻하는 상태 코드", "200은 성공 응답을 뜻하는 상태"),
    ("가나다라마 바사아자차 카타파하거너더러 버서", "가나다라마 바사아자차 카타파하거너더러"),  # 20자째에서 단어가 끝나면 20자 그대로
    ("띄어쓰기가전혀없는아주긴근거는스무글자에서자른다", "띄어쓰기가전혀없는아주긴근거는스무글자에"),  # 띄어쓰기가 없으면 20자
    ("줄\n바꿈이   여러 칸", "줄 바꿈이 여러 칸"),
])
def test_reason_is_clipped_at_word_boundary(reason, expected):
    clipped = quiz.clip_reason(reason)
    assert clipped == expected and len(clipped) <= 20


def test_old_app_without_submit_gets_old_fields_plus_timing(quiz_on, quiz_server):
    llm = StreamLLM(["ANSWER: 3\n", "REASON: 서울이 수도\n"])
    use(llm, llm)
    body = hard(type="choice", choice_count=4).json()
    assert body["choice"] == 2 and body["reason"] == "서울이 수도" and "submitted" not in body
    assert set(body) == {"choice", "reason", "timing"} and quiz_server["seen"] == []


def test_timing_order(quiz_on, quiz_server):
    llm = StreamLLM(["ANSWER: 1\n", "REASON: 근거\n"])
    use(llm, llm)
    t = easy(type="short", question="?", **submit_fields()).json()["timing"]
    assert set(t) == {"first_token", "answer", "submitted", "reason_sent", "total"}
    assert t["first_token"] <= t["answer"] <= t["submitted"] == t["reason_sent"] <= t["total"]


# ── 2. 문제 종류별 모델 ───────────────────────────────────────────────

def test_text_question_uses_text_model(quiz_on):
    image_llm, text_llm = StreamLLM(["ANSWER: 1\nREASON: r\n"], model="opus"), StreamLLM(["ANSWER: 1\nREASON: r\n"], model="haiku")
    use(image_llm, text_llm)
    easy(type="choice", question="수도는?", choices=["서울", "부산"])
    assert len(text_llm.calls) == 1 and image_llm.calls == []
    assert text_llm.calls[0]["system"] == quiz_flow.TEXT_SYSTEM


@pytest.mark.parametrize("call", [
    lambda: easy(type="choice", question="그림 속 동물은?", choices=["고양이", "개"], image_url="https://example.com/a.png"),
    lambda: hard(type="choice", choice_count=4),
])
def test_image_question_uses_image_model(quiz_on, call):
    image_llm = StreamLLM(["SEEN: 글\n", "THINK: 생각\n", "ANSWER: 1\n", "REASON: r\n"], model="opus")
    text_llm = StreamLLM(["ANSWER: 1\nREASON: r\n"], model="haiku")
    use(image_llm, text_llm)
    assert call().status_code == 200
    assert len(image_llm.calls) == 1 and text_llm.calls == []
    assert image_llm.calls[0]["system"] == quiz_flow.IMAGE_SYSTEM


def test_without_text_model_uses_quiz_model(quiz_on):
    image_llm = StreamLLM(["ANSWER: 1\nREASON: r\n"], model="opus")
    use(image_llm, None)
    easy(type="short", question="1+0은?")
    assert len(image_llm.calls) == 1 and image_llm.calls[0]["system"] == quiz_flow.TEXT_SYSTEM


def test_text_model_setting(monkeypatch):
    from app.config import load_settings
    monkeypatch.setenv("QUIZ_MODEL_TEXT", "claude-haiku-5-5")
    assert load_settings().quiz_model_text == "claude-haiku-5-5"
    monkeypatch.delenv("QUIZ_MODEL_TEXT")
    assert load_settings().quiz_model_text == ""


# ── 3. 그림 문제: SEEN, THINK 는 로그에만 ──────────────────────────────

def test_seen_and_think_go_to_log_not_quiz_server(quiz_on, quiz_server, capsys):
    llm = StreamLLM(["SEEN: print(len(\"hello\") * 2)\nTHINK: 길이 5에 2를 곱한다\n", "ANSWER: 10\n", "REASON: 5의 두 배\n"])
    use(llm, llm)
    body = hard(type="short", **submit_fields()).json()
    assert body["text"] == "10" and quiz_server["seen"] == [{"question_id": "q-1", "text": "10", "reason": "5의 두 배"}]
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert 'print(len("hello") * 2)' in line["seen"] and "길이 5" in line["think"]
    assert "seen" not in body and "think" not in body


def test_prompts_say_20_chars_with_example():
    for system in (quiz_flow.TEXT_SYSTEM, quiz_flow.IMAGE_SYSTEM):
        assert "15자 안팎 한국어 한 구절" in system and "예: 200은 성공 코드" in system
        assert "200은 성공 코드라 2번" not in system
    assert quiz_flow.IMAGE_SYSTEM.index("SEEN:") < quiz_flow.IMAGE_SYSTEM.index("THINK:") \
        < quiz_flow.IMAGE_SYSTEM.index("ANSWER: 답") < quiz_flow.IMAGE_SYSTEM.index("REASON: 근거")
    assert "SEEN" not in quiz_flow.TEXT_SYSTEM


# ── 줄 해석 ──────────────────────────────────────────────────────────

def parse(*chunks):
    lines = quiz_flow.QuizLines()
    for chunk in chunks:
        lines.feed(chunk)
    lines.finish()
    return lines


def test_parser_handles_markdown_and_fullwidth_colon():
    lines = parse("**ANSWER:** ②\n", "REASON： 두 번째가 맞다\n")
    assert lines.text("ANSWER") == "②" and lines.text("REASON") == "두 번째가 맞다"


def test_parser_keeps_multiline_seen():
    lines = parse("SEEN: x = [1, 2]\n", "y = x\n", "print(y)\n", "THINK: 같은 리스트\n", "ANSWER: 2\nREASON: r\n")
    assert lines.text("SEEN") == "x = [1, 2]\ny = x\nprint(y)" and lines.text("THINK") == "같은 리스트"


def test_parser_reason_on_next_line():
    lines = quiz_flow.QuizLines()
    lines.feed("ANSWER: 1\nREASON:\n")
    assert not lines.reason_done
    lines.feed("다음 줄 근거\n")
    assert lines.reason_done and lines.text("REASON") == "다음 줄 근거"


# ── 번호 파싱 실패 ───────────────────────────────────────────────────

def test_unreadable_number_retries_without_waiting_for_reason(quiz_on, quiz_server):
    events = []
    quiz_server["events"] = events
    llm = StreamLLM(["ANSWER: 잘 모르겠다\n", "REASON: 흐림\n"], events, retry={"answer": "③"})
    use(llm, llm)
    body = hard(type="choice", choice_count=4, **submit_fields()).json()
    assert body["choice"] == 2 and body["submitted"] is True
    assert [e[0] for e in events] == ["chunk", "retry", "submit"]  # 번호가 없으면 근거를 기다리지 않고 다시 묻는다


def test_retry_fails_then_first_choice_is_submitted(quiz_on, quiz_server):
    llm = StreamLLM(["ANSWER: ?\n"], retry=RuntimeError("모델 오류"))
    use(llm, llm)
    body = hard(type="choice", choice_count=4, **submit_fields()).json()
    assert (body["choice"], body["reason"], body["submitted"]) == (0, quiz.FALLBACK_REASON, True)
    assert len(quiz.FALLBACK_REASON) <= 20 and len(quiz.RETRY_REASON) <= 20


def test_stream_error_before_answer_still_answers_choice(quiz_on, quiz_server):
    class Broken(StreamLLM):
        def stream_quiz(self, *args):
            yield "SEEN: 화면"
            raise RuntimeError("끊김")

    llm = Broken([], retry={"answer": 2})
    use(llm, llm)
    body = hard(type="choice", choice_count=4, **submit_fields()).json()
    assert body["choice"] == 1 and body["submitted"] is True


def test_short_answer_missing_is_502(quiz_on):
    llm = StreamLLM(["REASON: 답이 없다\n"])
    use(llm, llm)
    assert easy(type="short", question="?").status_code == 502


def test_easy_choice_text_answer_matches_choices(quiz_on):
    llm = StreamLLM(["ANSWER: 서울\n", "REASON: 수도\n"])
    use(llm, llm)
    assert easy(type="choice", question="수도는?", choices=["부산", "서울"]).json()["choice"] == 1


# ── Claude 요청 모양(모델은 부르지 않는다) ────────────────────────────

class FakeStream:
    def __init__(self, pieces):
        self.text_stream, self.closed = iter(pieces), False

    def __enter__(self):
        return self

    def __exit__(self, *exc):
        self.closed = True

    def get_final_message(self):
        from types import SimpleNamespace
        return SimpleNamespace(model="m", usage=None, stop_reason="end_turn")


def claude(model):
    from types import SimpleNamespace

    from app.llm import ClaudeLLM
    llm = ClaudeLLM.__new__(ClaudeLLM)
    llm.model = model
    seen = {}

    def stream(**kwargs):
        seen.update(kwargs)
        seen["stream"] = FakeStream(["ANSWER: 1\n", "REASON: r\n", "뒤"])
        return seen["stream"]

    llm.client = SimpleNamespace(messages=SimpleNamespace(stream=stream), beta=SimpleNamespace(messages=SimpleNamespace(stream=stream)))
    return llm, seen


def test_claude_haiku_streams_with_thinking_off_and_cache():
    llm, seen = claude("claude-haiku-5-5")
    gen = llm.stream_quiz("지시", "[단답]", None, 256, {})
    assert next(gen) == "ANSWER: 1\n"
    gen.close()  # 답을 내고 일찍 그만 읽으면 스트림을 닫는다
    assert seen["stream"].closed and seen["thinking"] == {"type": "disabled"} and seen["max_tokens"] == 256
    assert seen["system"][0]["cache_control"] == {"type": "ephemeral"} and "speed" not in seen


def test_claude_opus_55_keeps_thinking_param_off_the_request():
    # Opus 5.5 는 thinking 을 끄면 400 이라 thinking 칸을 보내지 않고 effort low 만 둔다
    llm, seen = claude("claude-opus-5-5")
    list(llm.stream_quiz("지시", "[객관식]", {"data": PNG, "media_type": "image/png"}, 1536, {}))
    assert "thinking" not in seen and seen["output_config"] == {"effort": "low"} and "speed" not in seen
    assert seen["messages"][0]["content"][0]["type"] == "image"


# ── 6. 미리 깨우기 ───────────────────────────────────────────────────

def test_warmup_warms_image_and_text_models(quiz_on):
    events = []
    image_llm, text_llm = StreamLLM([], events, model="opus"), StreamLLM([], events, model="haiku")
    use(image_llm, text_llm)
    body = client.post("/api/quiz/warmup").json()
    assert [w["kind"] for w in body["warmed"]] == ["image", "text"] and all(w["ok"] for w in body["warmed"])
    assert events == [("warm", quiz_flow.IMAGE_SYSTEM), ("warm", quiz_flow.TEXT_SYSTEM)]
    assert [w["model"] for w in body["warmed"]] == ["opus", "haiku"]


def test_warmup_off_is_404(monkeypatch):
    monkeypatch.setattr(main.settings, "quiz_enabled", False)
    assert client.post("/api/quiz/warmup").status_code == 404


# ── 5. 서버 직접 풀기(auto) ──────────────────────────────────────────

def question(state="open", qid="t1", hidden=False, text="수도는?", my_answer=None, index=0):
    q = {"id": qid, "type": "choice", "choices": ["부산", "서울"]}
    if hidden:
        q.update(hidden=True, choice_count=2)
    else:
        q["text"] = text
    data = {"state": state, "index": index, "total": 3}
    if state in ("open", "closed"):
        data["question"] = q
    if my_answer is not None:
        data["my_answer"] = my_answer
    return data


def run_with(responses, solved=None, **options):
    """퀴즈 서버 응답을 차례로 돌려주는 가짜로 AutoRun 하나를 끝까지 돌린다."""
    queue = list(responses)
    asked = []

    def handler(request):
        asked.append(request.headers.get("authorization"))
        item = queue.pop(0) if queue else question(state="finished")
        return httpx.Response(item.pop("_status", 200), json=item)

    solved = solved if solved is not None else []

    def solve_question(base, token, q):
        solved.append(q["id"])
        return {"choice": 1, "reason": "r", "submitted": True}

    manager = quiz_auto.AutoManager(solve_question, httpx.Client(transport=httpx.MockTransport(handler)),
                                    sleep=lambda s: None, **options)
    started, _ = manager.start(QUIZ_SERVER, TEAM_TOKEN, background=False)
    return manager, solved, asked


def test_auto_solves_easy_skips_hidden_and_stops_at_finished():
    manager, solved, asked = run_with([
        question(state="lobby"),
        question(qid="t1"), question(qid="t1"),            # 같은 문제는 한 번만 푼다
        question(qid="h1", hidden=True, index=1),          # 하드모드는 건너뛴다(앱이 사진으로)
        question(qid="t2", my_answer={"choice": 0}, index=2),  # 이미 낸 문제도 건너뛴다
        question(state="finished", index=3),
        question(qid="t9"),                                # finished 뒤에는 더 보지 않는다
    ])
    status = manager.status(TEAM_TOKEN)
    assert solved == ["t1"] and status["running"] is False and status["stop_reason"] == "finished"
    skipped = {r["question_id"]: r.get("skipped") for r in status["results"]}
    assert skipped == {"t1": None, "h1": "hidden", "t2": "already_answered"}
    assert all(a == f"Bearer {TEAM_TOKEN}" for a in asked) and TEAM_TOKEN not in json.dumps(status)


def test_auto_stops_on_401_and_timeout():
    manager, _, _ = run_with([{"_status": 401, "detail": "x"}])
    assert manager.status(TEAM_TOKEN)["stop_reason"] == "unauthorized"
    ticks = iter(range(0, 10_000, 100))
    manager, _, _ = run_with([question(state="lobby")] * 1000, clock=lambda: next(ticks), max_seconds=1800)
    assert manager.status(TEAM_TOKEN)["stop_reason"] == "timeout"


def test_auto_one_run_per_token_and_stop():
    import threading
    gate = threading.Event()

    def handler(request):
        gate.wait(2)
        return httpx.Response(200, json=question(state="lobby"))

    manager = quiz_auto.AutoManager(lambda *a: {}, httpx.Client(transport=httpx.MockTransport(handler)), poll=0.01)
    first, _ = manager.start(QUIZ_SERVER, TEAM_TOKEN)
    second, snapshot = manager.start(QUIZ_SERVER, TEAM_TOKEN)
    assert first is True and second is False and snapshot["running"] is True
    assert manager.stop(TEAM_TOKEN) is True
    gate.set()
    run = manager.runs[quiz_auto.token_key(TEAM_TOKEN)]
    for _ in range(200):
        if not run.running():
            break
        threading.Event().wait(0.01)
    assert manager.status(TEAM_TOKEN)["stop_reason"] == "stopped"


def test_auto_endpoints_off_by_default(quiz_on, monkeypatch):
    monkeypatch.setattr(main.settings, "quiz_auto_enabled", False)
    assert client.post("/api/quiz/auto/start", json={"quiz_server": QUIZ_SERVER, "token": TEAM_TOKEN}).status_code == 404
    assert client.post("/api/quiz/auto/stop", json={"token": TEAM_TOKEN}).status_code == 404
    assert client.get("/api/quiz/auto/status", headers={"Authorization": f"Bearer {TEAM_TOKEN}"}).status_code == 404


def test_auto_start_checks_allowlist_and_hides_token(quiz_on, monkeypatch, capsys):
    monkeypatch.setattr(main.settings, "quiz_auto_enabled", True)
    llm = StreamLLM([])
    use(llm, llm)
    r = client.post("/api/quiz/auto/start", json={"quiz_server": "https://evil.example.com", "token": TEAM_TOKEN})
    assert r.status_code == 400

    finished = httpx.Client(transport=httpx.MockTransport(lambda req: httpx.Response(200, json=question(state="finished"))))
    monkeypatch.setattr(main, "quiz_auto_manager", quiz_auto.AutoManager(http=finished, sleep=lambda s: None))
    body = client.post("/api/quiz/auto/start", json={"quiz_server": QUIZ_SERVER, "token": TEAM_TOKEN}).json()
    assert body["started"] is True
    run = main.quiz_auto_manager.runs[quiz_auto.token_key(TEAM_TOKEN)]
    for _ in range(200):
        if not run.running():
            break
        __import__("time").sleep(0.01)
    by_header = client.get("/api/quiz/auto/status", headers={"Authorization": f"Bearer {TEAM_TOKEN}"}).json()
    by_query = client.get("/api/quiz/auto/status", params={"token": TEAM_TOKEN}).json()
    assert by_header["stop_reason"] == by_query["stop_reason"] == "finished"
    assert ("warm", quiz_flow.IMAGE_SYSTEM) in llm.events  # 시작할 때 미리 깨운다
    out = capsys.readouterr().out
    assert TEAM_TOKEN not in out and TEAM_TOKEN not in json.dumps(body) + json.dumps(by_header)


def test_auto_solver_fetches_image_with_token_and_submits(quiz_on, quiz_server, monkeypatch):
    seen_image = []

    def handler(request):
        seen_image.append((str(request.url), request.headers.get("authorization")))
        return httpx.Response(200, content=PNG, headers={"content-type": "image/png"})

    monkeypatch.setattr(main, "quiz_auto_manager",
                        quiz_auto.AutoManager(http=httpx.Client(transport=httpx.MockTransport(handler))))
    image_llm = StreamLLM(["SEEN: 고양이\nTHINK: t\nANSWER: 1\nREASON: 고양이가 보임\n"], model="opus")
    text_llm = StreamLLM(["ANSWER: 2\nREASON: 서울이 수도\n"], model="haiku")
    solve = main.auto_solver(image_llm, text_llm)
    out = solve(QUIZ_SERVER, TEAM_TOKEN, {"id": "i1", "type": "choice", "text": "그림 속 동물은?", "choices": ["고양이", "개"],
                                         "mode": "image", "image_url": "/api/image/i1"})
    assert out["choice"] == 0 and out["submitted"] is True and image_llm.calls[0]["image"]["data"] == PNG
    assert seen_image == [(f"{QUIZ_SERVER}/api/image/i1", f"Bearer {TEAM_TOKEN}")]
    out = solve(QUIZ_SERVER, TEAM_TOKEN, {"id": "t1", "type": "choice", "text": "수도는?", "choices": ["부산", "서울"]})
    assert out["choice"] == 1 and len(text_llm.calls) == 1
    assert quiz_server["seen"][-1] == {"question_id": "t1", "choice": 1, "reason": "서울이 수도"}


def test_access_log_hides_token_query():
    from app.logs import MaskAccessLog, mask_query
    assert mask_query(f"/api/quiz/auto/status?token={TEAM_TOKEN}") == "/api/quiz/auto/status?token=***"
    logger = logging.getLogger("uvicorn.access")
    assert sum(isinstance(f, MaskAccessLog) for f in logger.filters) == 1
    record = logger.makeRecord("uvicorn.access", logging.INFO, "", 0, '%s - "%s %s HTTP/%s" %d',
                               ("1.2.3.4:5", "GET", f"/api/quiz/auto/status?token={TEAM_TOKEN}", "1.1", 200), None)
    for f in logger.filters:
        f.filter(record)
    assert TEAM_TOKEN not in record.getMessage()


def test_fake_llm_streams_lines():
    fake = FakeLLM(delay=0)
    out = "".join(fake.stream_quiz("s", "[객관식]", {"data": PNG, "media_type": "image/png"}, 100, {}))
    assert out.startswith("SEEN:") and "ANSWER: 1" in out and "REASON:" in out


# ── 8. 시험지 스크립트(모델은 가짜로) ────────────────────────────────

def load_eval():
    import importlib.util
    from pathlib import Path
    path = Path(__file__).resolve().parent.parent / "scripts" / "quiz_eval.py"
    spec = importlib.util.spec_from_file_location("quiz_eval", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def test_quiz_eval_scores_with_fake_models(tmp_path):
    quiz_eval = load_eval()
    (tmp_path / "shot.png").write_bytes(PNG)
    (tmp_path / "questions.json").write_text(json.dumps([
        {"id": "hard-1", "mode": "hard", "type": "choice", "image": "shot.png", "choice_count": 4, "answer": 2},
        {"id": "easy-1", "mode": "easy", "type": "short", "question": "1+7은? (숫자만)", "answer": "8", "answers": ["여덟"]},
        # 글 모델 답 "여덟" 은 보기에 없어 다시 묻고, 그래도 없으면 1번(0)을 낸다 → 정답(1)과 달라 틀림
        {"id": "easy-2", "mode": "easy", "type": "choice", "question": "q", "choices": ["a", "b"], "answer": 1},
    ], ensure_ascii=False), encoding="utf-8")
    image_llm = StreamLLM(["SEEN: s\nTHINK: t\nANSWER: 3\nREASON: 화면 3번\n"], model="opus")
    text_llm = StreamLLM(["ANSWER: 여덟\nREASON: 더하기\n"], model="haiku")
    rows = quiz_eval.run(quiz_eval.load_items(tmp_path), tmp_path, image_llm, text_llm, repeat=1)
    assert [(r["id"], r["model"], r["ok"]) for r in rows] == [("hard-1", "opus", True), ("easy-1", "haiku", True),
                                                              ("easy-2", "haiku", False)]
    assert "| hard-1 | 1 | hard | opus | 2 | 2 | O |" in quiz_eval.table(rows)
    assert quiz_eval.summary(rows).startswith("정답률 2/3")


def test_quiz_eval_example_file_is_valid():
    quiz_eval = load_eval()
    from pathlib import Path
    folder = Path(__file__).resolve().parent / "fixtures" / "quiz_eval"
    for item in json.loads((folder / "questions.example.json").read_text(encoding="utf-8")):
        quiz_eval.build(item, folder)  # 형식이 맞으면 오류 없이 풀이 입력으로 바뀐다
