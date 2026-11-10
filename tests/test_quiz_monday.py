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
    monkeypatch.setattr(main.settings, "quiz_visible_check", True)
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
    assert set(body) == {"choice", "reason", "timing", "timings"} and quiz_server["seen"] == []


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
        assert "3~4단어 명사구" in system and "문장으로 끝내지 않는다" in system and "자 안팎" not in system
        for example in ("200은 성공 코드", "y가 x를 참조", "빨간 원 7개", "막대가 가장 긴 B"):
            assert example in system
        for label in ("상식:", "코드:", "개수 세기:", "그림 해석:"):
            assert f"{label} " not in system.split("머리말")[0]  # 예시에는 분류 이름이 없다
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



# ── 근거가 잘렸는지 로그에 남기기 ─────────────────────────────────────

def solve_log(capsys):
    return next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)


def test_reason_cut_is_logged_when_limit_reached(quiz_on, capsys):
    llm = StreamLLM(["ANSWER: 1\nREASON: 200은 성공을 뜻하는 HTTP 상태 코드", "라서 2번\n"])
    use(llm, llm)
    body = easy(type="short", question="?").json()
    assert body["reason"] == "200은 성공을 뜻하는 HTTP 상태"  # 띄어쓰기 앞에서 자른다(규칙 그대로)
    assert "reason_cut" in solve_log(capsys)["fixed"]


def test_short_reason_is_not_marked_cut(quiz_on, capsys):
    llm = StreamLLM(["ANSWER: 1\n", "REASON: 200은 성공 코드\n"])
    use(llm, llm)
    assert easy(type="short", question="?").json()["reason"] == "200은 성공 코드"
    assert "reason_cut" not in solve_log(capsys)["fixed"]


def test_quiz_eval_remote_mode_does_not_submit(tmp_path):
    # --server 모드: 풀이 서버에 사진과 type, choice_count 만 보내고(제출 정보 없음), 429 면 기다렸다 한 번 더
    quiz_eval = load_eval()
    (tmp_path / "shot.png").write_bytes(PNG)
    items = [{"id": "h1", "mode": "hard", "type": "choice", "image": "shot.png", "choice_count": 4, "answer": 2}]
    sent, waits = [], []
    replies = [httpx.Response(429, json={"error": "rate_limited"}),
               httpx.Response(200, json={"choice": 2, "reason": "r", "timing": {"first_token": 900, "answer": 1500}})]

    def post(url, json=None, files=None, data=None):
        sent.append({"url": url, "json": json, "data": data, "files": sorted(files or {})})
        return replies.pop(0)

    rows = quiz_eval.run_remote(items, tmp_path, "https://eval-sonnet---svc-uc.a.run.app", post=post, sleep=waits.append)
    assert waits == [61] and len(sent) == 2
    assert sent[0]["url"] == "https://eval-sonnet---svc-uc.a.run.app/api/quiz/solve"
    assert sent[0]["data"] == {"type": "choice", "choice_count": "4"} and sent[0]["files"] == ["photo"]
    assert not any(k in json.dumps(sent, default=str) for k in ("quiz_server", "token", "question_id"))
    assert rows[0]["ok"] is True and rows[0]["model"] == "eval-sonnet" and rows[0]["timing"]["first_token"] == 900


@pytest.mark.parametrize("reason, expected", [
    ("상식: 대한민국 수도는 서울", "대한민국 수도는 서울"),
    ("개수 세기: 빨간 원 5개", "빨간 원 5개"),
    ("그림 해석：막대가 가장 긴 B", "막대가 가장 긴 B"),
    ("코드: y가 x를 참조", "y가 x를 참조"),
    ("REASON: 200은 성공 코드", "200은 성공 코드"),
    ("수도는 서울", "수도는 서울"),
    ("y가 x를 참조: 같은 리스트", "y가 x를 참조: 같은 리스트"),  # 머리말이 아닌 콜론은 그대로
])
def test_reason_label_prefix_is_removed(reason, expected):
    assert quiz.clip_reason(reason) == expected


# ── 객관식: 번호와 고른 보기 글을 함께 받아 맞춰 보기 ───────────────────

NUMERIC = ["10", "3", "7", "2"]


@pytest.mark.parametrize("answer, choices, number, fixed", [
    ("2 (3)", NUMERIC, 2, None),                         # 번호와 글이 맞음
    ("3 (3)", NUMERIC, 2, "number_from_text"),           # 번호가 틀림(보기 글 "3" 을 번호로 씀) → 글을 믿는다
    ("3", NUMERIC, 3, None),                             # 글이 없으면 번호 그대로
    ("2 ([20, 40])", ["[20, 30, 40]", "[20, 40]"], 2, None),
    ("1 ([20, 40])", ["[20, 30, 40]", "[20, 40]"], 2, "number_from_text"),
    ("2 [20, 40]", ["[20, 30, 40]", "[20, 40]"], 2, None),
    ("②（서울）", ["부산", "서울"], 2, None),             # 동그라미 숫자, 전각 괄호
    ("서울", ["부산", "서울"], 2, "number_from_text"),     # 번호 없이 글만
    ("4 (광주)", ["부산", "인천", "서울", "대구"], 4, None),  # 목록에 없는 글이면 번호
    ("3 (아무 글)", None, 3, None),                         # 하드모드: 보기 목록이 없어 번호
    ("(모름)", None, None, None),
])
def test_choice_number_with_text(answer, choices, number, fixed):
    assert quiz_flow._choice(answer, 4, choices) == (number, fixed)


def test_numeric_choices_trust_text_end_to_end(quiz_on, capsys):
    llm = StreamLLM(["ANSWER: 3 (3)\n", "REASON: 가장 작은 홀수\n"])
    use(llm, llm)
    body = easy(type="choice", question="가장 작은 홀수는?", choices=NUMERIC).json()
    assert body["choice"] == 1  # 보기 "3" 은 두 번째(0부터 1)
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert "number_from_text" in line["fixed"] and line["answer_raw"] == "3 (3)"


def test_prompt_asks_number_with_choice_text():
    for system in (quiz_flow.TEXT_SYSTEM, quiz_flow.IMAGE_SYSTEM):
        assert "'번호 (고른 보기 글)'" in system and "ANSWER: 2 (200)" in system


# ── 직접 풀기: 실패한 문제는 한 번 더 ────────────────────────────────

def run_auto(responses, outcomes):
    """퀴즈 서버 응답과, 풀 때마다 돌려줄 결과(예외면 냄)를 차례로 써서 AutoRun 하나를 끝까지 돌린다."""
    queue, results, solved = list(responses), list(outcomes), []

    def handler(request):
        return httpx.Response(200, json=queue.pop(0) if queue else question(state="finished"))

    def solve_question(base, token, q):
        solved.append(q["id"])
        outcome = results.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return outcome

    manager = quiz_auto.AutoManager(solve_question, httpx.Client(transport=httpx.MockTransport(handler)),
                                    sleep=lambda s: None)
    manager.start(QUIZ_SERVER, TEAM_TOKEN, background=False)
    return solved, manager.status(TEAM_TOKEN)["results"]


OK = {"choice": 1, "reason": "r", "submitted": True, "answer_status": 200}


def test_auto_retries_once_after_error():
    solved, results = run_auto([question(qid="t1"), question(qid="t1"), question(qid="t1")],
                               [quiz.QuizError(502, "x"), OK])
    assert solved == ["t1", "t1"]  # 실패 뒤 한 번 더 풀고, 성공했으므로 세 번째 확인에서는 풀지 않는다
    assert [r.get("attempt") for r in reversed(results)] == [1, 2] and results[0]["submitted"] is True


def test_auto_gives_up_after_two_attempts():
    solved, results = run_auto([question(qid="t1")] * 4, [RuntimeError("a"), RuntimeError("b")])
    assert solved == ["t1", "t1"] and results[0]["attempt"] == 2 and "retry" not in results[0]


@pytest.mark.parametrize("submit, retried", [
    ({"submitted": False, "answer_status": None, "submit_error": "연결 실패"}, True),
    ({"submitted": False, "answer_status": 503, "submit_error": "서버 오류"}, True),
    ({"submitted": False, "answer_status": 409, "submit_error": "이미 냄"}, False),
    ({"submitted": False, "answer_status": 422, "submit_error": "형식"}, False),
])
def test_auto_retries_only_when_submit_may_succeed(submit, retried):
    solved, _ = run_auto([question(qid="t1"), question(qid="t1")], [{"choice": 1, "reason": "r", **submit}, OK])
    assert solved == (["t1", "t1"] if retried else ["t1"])


def test_auto_does_not_retry_when_answer_already_recorded():
    # 앞 시도가 실패로 보였어도 퀴즈 서버에 이미 답이 있으면(my_answer) 다시 내지 않는다
    solved, results = run_auto([question(qid="t1"), question(qid="t1", my_answer={"choice": 1})],
                               [RuntimeError("timeout")])
    assert solved == ["t1"] and results[0]["skipped"] == "already_answered"


def test_every_llm_class_keeps_all_methods():
    # 정리하다 메서드를 지우면(예: OpenAI chat_stream) 가짜 모델만 쓰는 다른 테스트로는 드러나지 않으므로 직접 확인한다
    from app.llm import ClaudeLLM, FakeLLM, OpenAICompatibleLLM
    for cls in (OpenAICompatibleLLM, ClaudeLLM, FakeLLM):
        for name in ("complete_text", "chat_stream", "complete_vision_json", "stream_quiz", "warm"):
            assert callable(getattr(cls, name, None)), f"{cls.__name__}.{name}"
    for cls in (OpenAICompatibleLLM, ClaudeLLM):
        assert callable(getattr(cls, "complete_json", None)), f"{cls.__name__}.complete_json"


# ── 앱이 읽는 시간 칸(timings) ────────────────────────────────────────

def test_app_timings_names_and_integers():
    t = {"first_token": 812, "answer": 1300, "submitted": 1650, "reason_sent": 1650, "total": 1660}
    assert quiz_flow.app_timings(t) == {"first_token_ms": 812, "submit_ms": 1650, "reason_ms": 1650}
    assert quiz_flow.app_timings({"first_token": 5.0, "answer": 9}) == {"first_token_ms": 5}  # 제출 안 하면 그 칸은 없다
    assert all(isinstance(v, int) for v in quiz_flow.app_timings({"first_token": 5.0}).values())


def test_solve_response_has_timing_and_timings(quiz_on, quiz_server):
    llm = StreamLLM(["ANSWER: 1\n", "REASON: 근거\n"])
    use(llm, llm)
    body = easy(type="short", question="?", **submit_fields()).json()
    t, ts = body["timing"], body["timings"]
    assert set(t) == {"first_token", "answer", "submitted", "reason_sent", "total"}  # 기존 칸은 그대로
    assert ts == {"first_token_ms": t["first_token"], "submit_ms": t["submitted"], "reason_ms": t["reason_sent"]}
    body = easy(type="short", question="?").json()  # 제출 정보가 없으면 제출 시간 칸은 없다
    assert set(body["timings"]) == {"first_token_ms"}


def test_auto_results_have_timings(quiz_on, quiz_server, monkeypatch):
    monkeypatch.setattr(main, "quiz_auto_manager", quiz_auto.AutoManager(http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)))))
    llm = StreamLLM(["ANSWER: 2\nREASON: 서울이 수도\n"])
    out = main.auto_solver(llm, llm)(QUIZ_SERVER, TEAM_TOKEN, {"id": "t1", "type": "choice", "text": "수도는?",
                                                              "choices": ["부산", "서울"]})
    assert set(out["timing"]) >= {"first_token", "submitted"}
    assert out["timings"] == quiz_flow.app_timings(out["timing"]) and set(out["timings"]) == {
        "first_token_ms", "submit_ms", "reason_ms"}


# ── 사진 추측 답: VISIBLE 로 다시 찍기 ────────────────────────────────

@pytest.mark.parametrize("text, expected", [
    ("yes", (True, "")), ("YES", (True, "")), ("예", (True, "")),
    ("no (표 아래 두 줄 잘림)", (False, "표 아래 두 줄 잘림")),
    ("no - 그래프 아래쪽 잘림", (False, "그래프 아래쪽 잘림")),
    ("아니오（격자 아랫줄）", (False, "격자 아랫줄")),
    ("", (None, "")), ("모르겠다", (None, "")),
])
def test_parse_visible(text, expected):
    assert quiz_flow.parse_visible(text) == expected


def test_image_prompt_has_visible_between_seen_and_think():
    s = quiz_flow.IMAGE_SYSTEM
    assert s.index("SEEN:") < s.index("VISIBLE:") < s.index("THINK:") < s.index("ANSWER: 답") < s.index("REASON: 근거")
    assert "일부라도 잘리거나 읽을 수 없으면 no" in s and "VISIBLE" not in quiz_flow.TEXT_SYSTEM


CUT = ["SEEN: 표 위 두 줄\n", "VISIBLE: no (표 아래 두 줄 잘림)\n", "THINK: 보이는 것만 더한다\n", "ANSWER: 45000\n",
       "REASON: 노트와 펜 합계\n"]


def test_visible_no_stops_and_does_not_submit(quiz_on, quiz_server, capsys):
    events = []
    quiz_server["events"] = events
    llm = StreamLLM(CUT, events)
    use(llm, llm)
    body = hard(type="short", supports_retake="true", **submit_fields()).json()
    assert body["retake"] is True and body["missing"] == "표 아래 두 줄 잘림"
    assert "text" not in body and "submitted" not in body and quiz_server["seen"] == []
    assert [e[0] for e in events] == ["chunk", "chunk"]  # VISIBLE 줄에서 바로 멈춘다(THINK 이후는 읽지 않음)
    assert {"visible", "first_token", "total"} <= set(body["timing"]) and "answer" not in body["timing"]
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert (line["visible"], line["retake"], line["submit"]) == ("no", True, False)


def test_old_app_request_does_not_get_retake(quiz_on, quiz_server, capsys):
    # 옛 앱은 supports_retake 를 보내지 않는다 → retake 를 모르므로 예전처럼 끝까지 풀어 제출한다
    llm = StreamLLM(CUT)
    use(llm, llm)
    body = hard(type="short", **submit_fields()).json()
    assert "retake" not in body and body["text"] == "45000" and body["submitted"] is True
    assert quiz_server["seen"][0]["text"] == "45000"
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert line["visible"] == "no" and line["retake"] is False and line["supports_retake"] is False
    assert "guessed_not_visible" in line["fixed"]


def test_allow_guess_solves_and_submits_even_if_not_visible(quiz_on, quiz_server, capsys):
    llm = StreamLLM(CUT)
    use(llm, llm)
    body = hard(type="short", supports_retake="true", allow_guess="true", **submit_fields()).json()
    assert body["text"] == "45000" and body["submitted"] is True and "retake" not in body
    assert quiz_server["seen"][0]["text"] == "45000"
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert line["visible"] == "no" and "guessed_not_visible" in line["fixed"] and line["allow_guess"] is True


UNKNOWN_CUT = ["SEEN: 그래프 오른쪽 잘림\n", "VISIBLE: no (E 쪽 간선 잘림)\n", "THINK: E 로 가는 간선이 안 보인다\n",
               "ANSWER: 알 수 없음\n", "REASON: E 연결 간선 잘림\n"]


def test_allow_guess_uses_guess_prompt_that_forbids_unknown(quiz_on, quiz_server):
    llm = StreamLLM(CUT)
    use(llm, llm)
    hard(type="short", supports_retake="true", allow_guess="true", **submit_fields())
    system = llm.calls[0]["system"]
    assert system == quiz_flow.GUESS_SYSTEM and "알 수 없음" in system and "가장 그럴듯" in system
    assert "짐작해서 채우지 않는다" not in system and "VISIBLE" in system  # VISIBLE 은 그대로 사실대로 쓴다
    llm = StreamLLM(CUT)
    use(llm, llm)
    hard(type="short", supports_retake="true", **submit_fields())  # allow_guess 가 아니면 예전 지시문
    assert llm.calls[0]["system"] == quiz_flow.IMAGE_SYSTEM


def test_allow_guess_unknown_answer_is_asked_again_and_submitted(quiz_on, quiz_server, capsys):
    events = []
    llm = StreamLLM(UNKNOWN_CUT, events, retry={"answer": "11", "reason": "보이는 간선으로 추정"})
    use(llm, llm)
    body = hard(type="short", supports_retake="true", allow_guess="true", **submit_fields()).json()
    assert body["text"] == "11" and body["submitted"] is True and quiz_server["seen"][0]["text"] == "11"
    assert ("retry",) in events
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert "retry_guess" in line["fixed"] and "guessed_not_visible" in line["fixed"]


def test_allow_guess_retry_still_unknown_keeps_answer_and_marks_it(quiz_on, quiz_server, capsys):
    llm = StreamLLM(UNKNOWN_CUT, retry={"answer": "모름"})
    use(llm, llm)
    body = hard(type="short", supports_retake="true", allow_guess="true", **submit_fields()).json()
    assert body["submitted"] is True  # 답을 안 내면 0점이라 낸다
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert "unknown_answer" in line["fixed"]


def test_unknown_answer_without_allow_guess_is_not_asked_again(quiz_on, quiz_server):
    events = []
    llm = StreamLLM(UNKNOWN_CUT, events, retry={"answer": "11"})
    use(llm, llm)
    hard(type="short", **submit_fields())  # 옛 앱(allow_guess 없음)은 예전 그대로
    assert ("retry",) not in events


@pytest.mark.parametrize("answer, expected", [
    ("알 수 없음", True), ("알수없음", True), ("모름", True), ("모르겠다", True), ("판단 불가", True),
    ("답을 정할 수 없다", True), ("unknown", True), ("N/A", True), ("?", True),
    ("11", False), ("QUIZ 1109", False), ("없음", False), ("79000", False), ("", False),
])
def test_is_unknown(answer, expected):
    assert quiz.is_unknown(answer) is expected

def test_visible_yes_goes_on_as_before(quiz_on, quiz_server):
    llm = StreamLLM(["SEEN: 표 전체\nVISIBLE: yes\nTHINK: 다 더한다\nANSWER: 79000\nREASON: 품목별 금액 합계\n"])
    use(llm, llm)
    body = hard(type="short", supports_retake="true", **submit_fields()).json()
    assert body["text"] == "79000" and body["submitted"] is True


def test_easy_image_is_not_gated(quiz_on, quiz_server):
    # 이지모드 그림은 다시 찍을 사진이 없으므로 VISIBLE: no 여도 끝까지 풀어 낸다
    llm = StreamLLM(CUT)
    use(llm, llm)
    body = easy(type="short", question="합계는?", image_url="https://example.com/t.png", **submit_fields()).json()
    assert body["text"] == "45000" and body["submitted"] is True


def test_missing_visible_line_is_treated_as_before(quiz_on):
    llm = StreamLLM(["SEEN: s\nTHINK: t\nANSWER: 3\nREASON: r\n"])  # VISIBLE 을 빠뜨린 모델
    use(llm, llm)
    assert hard(type="choice", choice_count=4, supports_retake="true").json()["choice"] == 2


@pytest.mark.parametrize("fields, expected", [
    ({"eval": "after"}, {"supports_retake": False, "allow_guess": False, "eval": "after"}),
    ({"allow_guess": "true", "eval": "before"}, {"supports_retake": False, "allow_guess": True, "eval": "before"}),
    ({"allow_guess": "1"}, {"supports_retake": False, "allow_guess": True, "eval": None}),
    ({"supports_retake": "true"}, {"supports_retake": True, "allow_guess": False, "eval": None}),
    ({"supports_retake": "false"}, {"supports_retake": False, "allow_guess": False, "eval": None}),
    ({"eval": "나쁜 값 <script>"}, {"supports_retake": False, "allow_guess": False, "eval": None}),
    ({}, {"supports_retake": False, "allow_guess": False, "eval": None}),
])
def test_solve_options(fields, expected):
    assert quiz.solve_options(fields) == expected


def test_langfuse_metadata_has_eval_visible_retake(quiz_on, monkeypatch):
    import uuid

    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import tracing
    exporter = InMemorySpanExporter()
    key = uuid.uuid4().hex
    monkeypatch.setattr(tracing, "_client", Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}",
                                                     base_url="http://localhost:1", span_exporter=exporter))
    monkeypatch.setattr(tracing, "_enabled", True)
    llm = StreamLLM(CUT)
    use(llm, llm)
    hard(type="short", eval="after", supports_retake="true")
    tracing.client().flush()
    span = next(s for s in exporter.get_finished_spans() if s.name == "quiz-solve")
    attrs = span.attributes
    assert json.loads(attrs["langfuse.observation.metadata.eval"]) == "after"
    assert json.loads(attrs["langfuse.observation.metadata.visible"]) == "no"
    assert json.loads(attrs["langfuse.observation.metadata.retake"]) is True


def test_quiz_eval_counts_retake(tmp_path):
    quiz_eval = load_eval()
    (tmp_path / "shot.png").write_bytes(PNG)
    items = [{"id": "c1", "mode": "hard", "type": "short", "image": "shot.png", "answer": "79000", "label": "problem",
              "problem": "cut"}]
    sent = []

    def post(url, json=None, files=None, data=None):
        sent.append(data)
        return httpx.Response(200, json={"retake": True, "missing": "표 아래 잘림", "timing": {"first_token": 900, "visible": 1500}})

    rows = quiz_eval.run_remote(items, tmp_path, "https://eval---svc.a.run.app", post=post, eval_tag="after",
                                supports_retake=True)
    assert sent[0]["eval"] == "after" and sent[0]["supports_retake"] == "true" and "allow_guess" not in sent[0]
    assert rows[0]["retake"] is True and rows[0]["ok"] is False and rows[0]["missing"] == "표 아래 잘림"
    assert "다시 찍기" in quiz_eval.table(rows) and "다시 찍기 1번" in quiz_eval.summary(rows)


# ── 서버 직접 풀기: 그림 주소의 호스트가 퀴즈 서버와 같을 때만 팀 토큰을 붙인다 ──────────

@pytest.mark.parametrize("url, same", [
    (QUIZ_SERVER + "/img/1.png", True),
    (QUIZ_SERVER, True),
    (QUIZ_SERVER + ":443/img/1.png", True),
    (QUIZ_SERVER + ".evil.example/x.png", False),         # 앞부분만 같은 다른 호스트
    (QUIZ_SERVER.replace("https://", "https://user@") + "/x.png", False),
    (QUIZ_SERVER + ":8443/x.png", False),
    (QUIZ_SERVER.replace("https://", "http://") + "/x.png", False),
    ("https://evil.example/x.png", False),
    ("", False), (None, False),
])
def test_same_server(url, same):
    assert quiz.same_server(url, QUIZ_SERVER) is same


def test_auto_image_from_other_host_gets_no_token(quiz_on, monkeypatch):
    seen = []

    def handler(request):
        seen.append((request.url.host, request.headers.get("authorization")))
        return httpx.Response(200, headers={"content-type": "image/png"}, content=PNG)

    monkeypatch.setattr(main, "quiz_auto_manager", quiz_auto.AutoManager(http=httpx.Client(
        transport=httpx.MockTransport(handler))))
    llm = StreamLLM(["SEEN: s\nVISIBLE: yes\nTHINK: t\nANSWER: 1\nREASON: r\n"])
    solve = main.auto_solver(llm, llm)
    monkeypatch.setattr(main, "make_submit", lambda target: None)
    solve(QUIZ_SERVER, TEAM_TOKEN, {"id": "q1", "type": "short", "text": "?",
                                    "image_url": QUIZ_SERVER + ".evil.example/x.png"})
    assert seen == []  # 다른 호스트에는 아예 요청하지 않는다(토큰도 보내지 않음)
    solve(QUIZ_SERVER, TEAM_TOKEN, {"id": "q2", "type": "short", "text": "?", "image_url": "/img/1.png"})
    assert seen and seen[-1][1] == f"Bearer {TEAM_TOKEN}"  # 같은 퀴즈 서버 그림은 예전처럼 토큰으로 받는다

# ── QUIZ_VISIBLE_CHECK 와 Langfuse version(배포 뒤 트레이스를 version 으로 나눠 보기) ──────────

def langfuse_spans(monkeypatch):
    """quiz-solve 관찰을 메모리에 모은다. (exporter, flush)"""
    import uuid

    from langfuse import Langfuse
    from opentelemetry.sdk.trace.export.in_memory_span_exporter import InMemorySpanExporter

    from app import tracing
    exporter = InMemorySpanExporter()
    key = uuid.uuid4().hex
    monkeypatch.setattr(tracing, "_client", Langfuse(public_key=f"pk-lf-{key}", secret_key=f"sk-lf-{key}",
                                                     base_url="http://localhost:1", span_exporter=exporter))
    monkeypatch.setattr(tracing, "_enabled", True)
    return exporter, lambda: tracing.client().flush()


@pytest.mark.parametrize("value, expected", [("", True), ("on", True), ("off", False), ("false", False),
                                             ("true", True)])
def test_quiz_visible_check_setting(monkeypatch, value, expected):
    from app.config import load_settings
    monkeypatch.setenv("QUIZ_VISIBLE_CHECK", value)
    assert load_settings().quiz_visible_check is expected


def test_version_names():
    assert quiz_flow.quiz_version(True) == "quiz-v2-visible" and quiz_flow.quiz_version(False) == "quiz-v1"
    v1 = quiz_flow.IMAGE_SYSTEM_V1
    assert "VISIBLE" not in v1 and v1.index("SEEN:") < v1.index("THINK:") < v1.index("ANSWER: 답")


def test_visible_check_on_traces_v2_and_keeps_retake(quiz_on, quiz_server, monkeypatch, capsys):
    exporter, flush = langfuse_spans(monkeypatch)
    llm = StreamLLM(CUT)
    use(llm, llm)
    body = hard(type="short", supports_retake="true", eval="version-compare", **submit_fields()).json()
    assert body["retake"] is True and quiz_server["seen"] == []
    assert llm.calls[0]["system"] == quiz_flow.IMAGE_SYSTEM
    flush()
    attrs = next(s for s in exporter.get_finished_spans() if s.name == "quiz-solve").attributes
    assert attrs["langfuse.version"] == "quiz-v2-visible"
    assert json.loads(attrs["langfuse.observation.metadata.version"]) == "quiz-v2-visible"
    assert json.loads(attrs["langfuse.observation.metadata.eval"]) == "version-compare"  # eval 은 그대로
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert line["version"] == "quiz-v2-visible"


def test_visible_check_off_solves_to_the_end_like_v1(quiz_on, quiz_server, monkeypatch, capsys):
    monkeypatch.setattr(main.settings, "quiz_visible_check", False)
    exporter, flush = langfuse_spans(monkeypatch)
    v1_stream = ["SEEN: 표 위 두 줄\n", "THINK: 보이는 것만 더한다\n", "ANSWER: 45000\n", "REASON: 노트와 펜 합계\n"]
    llm = StreamLLM(v1_stream, retry={"answer": "11"})
    use(llm, llm)
    body = hard(type="short", supports_retake="true", allow_guess="true", eval="version-compare",
                **submit_fields()).json()
    assert "retake" not in body and body["text"] == "45000" and body["submitted"] is True
    assert llm.calls[0]["system"] == quiz_flow.IMAGE_SYSTEM_V1  # allow_guess 여도 v1 지시문
    flush()
    attrs = next(s for s in exporter.get_finished_spans() if s.name == "quiz-solve").attributes
    assert attrs["langfuse.version"] == "quiz-v1"
    assert json.loads(attrs["langfuse.observation.metadata.version"]) == "quiz-v1"
    assert json.loads(attrs["langfuse.observation.metadata.eval"]) == "version-compare"
    line = next(json.loads(x) for x in capsys.readouterr().out.splitlines() if '"퀴즈 풀기"' in x)
    assert line["version"] == "quiz-v1" and line["retake"] is False and line["visible"] is None


def test_visible_check_off_ignores_visible_no_even_if_model_writes_it(quiz_on, quiz_server, monkeypatch):
    # off 인데 모델이 VISIBLE: no 를 써도 멈추지 않고 '알 수 없음' 재시도도 하지 않는다(고치기 전 동작)
    monkeypatch.setattr(main.settings, "quiz_visible_check", False)
    events = []
    llm = StreamLLM(UNKNOWN_CUT, events, retry={"answer": "11"})
    use(llm, llm)
    body = hard(type="short", supports_retake="true", allow_guess="true", **submit_fields()).json()
    assert "retake" not in body and body["submitted"] is True and ("retry",) not in events


def test_auto_solver_uses_visible_check_setting(quiz_on, quiz_server, monkeypatch):
    monkeypatch.setattr(main.settings, "quiz_visible_check", False)
    monkeypatch.setattr(main, "quiz_auto_manager", quiz_auto.AutoManager(http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)))))
    seen = {}
    real = quiz_flow.solve

    def spy(*a, **k):
        seen.update(k)
        return real(*a, **k)

    monkeypatch.setattr(quiz_flow, "solve", spy)
    llm = StreamLLM(["ANSWER: 2\nREASON: 서울이 수도\n"])
    main.auto_solver(llm, llm)(QUIZ_SERVER, TEAM_TOKEN, {"id": "t1", "type": "choice", "text": "수도는?",
                                                     "choices": ["부산", "서울"]})
    assert seen["visible_check"] is False


# ── 서버 직접 풀기 안정성(코드 리뷰에서 재현한 것) ──────────────────────────

def wait_until(check, seconds=3):
    import threading
    for _ in range(int(seconds / 0.01)):
        if check():
            return True
        threading.Event().wait(0.01)
    return False


@pytest.mark.parametrize("bad", [
    httpx.Response(200, text="<html>점검 중</html>"),        # JSON 이 아님
    httpx.Response(200, json=["x"]),                         # dict 가 아님
    httpx.Response(200, json={"state": "open", "question": "x"}),  # question 모양이 다름
])
def test_auto_survives_bad_response_and_keeps_polling(bad):
    queue = [bad, httpx.Response(200, json=question(qid="t1")), httpx.Response(200, json=question(state="finished"))]
    solved = []

    def handler(request):
        return queue.pop(0) if queue else httpx.Response(200, json=question(state="finished"))

    manager = quiz_auto.AutoManager(lambda b, t, q: solved.append(q["id"]) or {"submitted": True},
                                    httpx.Client(transport=httpx.MockTransport(handler)), sleep=lambda s: None)
    manager.start(QUIZ_SERVER, TEAM_TOKEN, background=False)
    status = manager.status(TEAM_TOKEN)
    assert solved == ["t1"] and status["running"] is False and status["stop_reason"] == "finished"


def test_auto_unexpected_error_does_not_stay_running(monkeypatch):
    manager = quiz_auto.AutoManager(lambda *a: {}, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=question(qid="t1")))), sleep=lambda s: None)
    monkeypatch.setattr(quiz_auto.AutoRun, "step", lambda self, data: 1 / 0)
    manager.start(QUIZ_SERVER, TEAM_TOKEN, background=False)
    status = manager.status(TEAM_TOKEN)
    assert status["running"] is False and status["stop_reason"] == "error"
    again, _ = manager.start(QUIZ_SERVER, TEAM_TOKEN, background=False)
    assert again is True  # 다시 시작할 수 있다


def test_auto_start_right_after_stop_starts_new_run():
    manager = quiz_auto.AutoManager(lambda *a: {}, httpx.Client(transport=httpx.MockTransport(
        lambda r: httpx.Response(200, json=question(state="lobby")))), poll=5)  # 확인 사이에 오래 기다리는 중
    assert manager.start(QUIZ_SERVER, TEAM_TOKEN)[0] is True
    old = manager.runs[quiz_auto.token_key(TEAM_TOKEN)]
    assert wait_until(lambda: old.state == "lobby")
    assert manager.stop(TEAM_TOKEN) is True
    restarted, snapshot = manager.start(QUIZ_SERVER, TEAM_TOKEN)
    assert restarted is True and snapshot["running"] is True  # 끝나는 중인 실행 때문에 막히지 않는다
    assert wait_until(lambda: not old.running()) and old.stop_reason == "stopped"  # 5초 기다리지 않고 바로 멈춘다
    new = manager.runs[quiz_auto.token_key(TEAM_TOKEN)]
    assert new is not old and new.running()
    manager.stop(TEAM_TOKEN)


def test_auto_solver_treats_empty_image_url_as_text_question(quiz_on, quiz_server, monkeypatch):
    monkeypatch.setattr(main, "quiz_auto_manager", quiz_auto.AutoManager(http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)))))
    llm = StreamLLM(["ANSWER: 2\nREASON: 서울이 수도\n"])
    out = main.auto_solver(llm, llm)(QUIZ_SERVER, TEAM_TOKEN, {"id": "t1", "type": "choice", "text": "수도는?",
                                                             "choices": ["부산", "서울"], "image_url": ""})
    assert out["choice"] == 1 and out["submitted"] is True and quiz_server["seen"][0]["choice"] == 1


def test_auto_solver_does_not_wait_for_langfuse_flush(quiz_on, quiz_server, monkeypatch):
    import threading
    import time as _time
    release = threading.Event()
    monkeypatch.setattr(main.tracing, "flush", lambda: release.wait(2))  # Langfuse 가 느린 상황
    monkeypatch.setattr(main, "quiz_auto_manager", quiz_auto.AutoManager(http=httpx.Client(
        transport=httpx.MockTransport(lambda r: httpx.Response(404)))))
    llm = StreamLLM(["ANSWER: 2\nREASON: 서울이 수도\n"])
    started = _time.perf_counter()
    main.auto_solver(llm, llm)(QUIZ_SERVER, TEAM_TOKEN, {"id": "t1", "type": "choice", "text": "수도는?",
                                                     "choices": ["부산", "서울"]})
    assert _time.perf_counter() - started < 1.0
    release.set()
