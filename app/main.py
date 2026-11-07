"""AI 친구 에이전트 서비스.

화면(/)과 API를 FastAPI 하나가 함께 제공한다.
- /health, /version: 서비스 상태와 배포 버전
- /api/config: 화면이 쓰는 설정(로그인 방식, 개발용 패널 표시 여부)
- /api/character, /api/settings: 캐릭터와 사용자 설정
- /api/chat: 메시지 한 턴. 결과를 이벤트(JSON 한 줄씩)로 스트리밍한다
- /api/greeting: 앱을 열 때 먼저 말 걸기
- /api/ocr: 사진 속 글자 읽기(OCR_ENABLED=true 일 때)
- /api/memories, /api/reminders, /api/data: 기억, 약속, 내 데이터 관리
- /api/quiz/solve: 퀴즈 대결 앱의 문제 풀기(QUIZ_SOLVER_ENABLED=true 일 때만, quiz.py)
"""
import html
import json
import os
import time
from urllib.parse import urlsplit

import httpx

from fastapi import BackgroundTasks, Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import agent, gate, ocr, quiz, quiz_auto, quiz_flow, tracing, when
from app.auth import Auth, User
from app.config import QUIZ_AUTO_MAX_SECONDS, QUIZ_MAX_PHOTO_BYTES, ROOT, load_settings
from app.llm import make_llm
from app.logs import log, mask_access_log
from app.persona import DEFAULT_CHARACTER, OPTIONS, build_persona, clean_character
from app.store.base import make_store
from app.tools import CITIES

mask_access_log()  # 접속 로그 주소의 token= 값을 가린다
settings = load_settings()  # 꼭 필요한 값이 없으면 여기서 종료한다
tracing.setup(settings)  # .env 를 읽은 뒤에 켜야 Langfuse 키를 찾는다. 키가 없으면 꺼진다
llm = make_llm(settings)
quiz_llm = make_llm(settings, settings.quiz_model) if settings.quiz_enabled else None  # 그림 문제 모델
# 글 문제 모델(QUIZ_MODEL_TEXT). 비어 있으면 None → 그림 문제 모델을 같이 쓴다
quiz_text_llm = make_llm(settings, settings.quiz_model_text) if settings.quiz_enabled and settings.quiz_model_text else None
quiz_limiter = quiz.RateLimiter(settings.quiz_rate_per_minute)
quiz_auto_manager = quiz_auto.AutoManager(max_seconds=QUIZ_AUTO_MAX_SECONDS)  # 서버 직접 풀기(인스턴스 메모리)
quiz_submitter = quiz.AnswerSubmitter()  # 퀴즈 서버 연결을 재사용한다(만들 때 연결하지 않는다)
for _pattern in settings.quiz_server_allowlist:
    quiz.server_pattern(_pattern)  # 허용 목록이 잘못 적혔으면 시작할 때 바로 알린다
store = make_store(settings)
current_user = Auth(settings)

app = FastAPI(title="AI 친구 에이전트")

# 시작 지연 스위치(배포 실습용). 포트는 바로 열지만 정한 시간 동안은 준비 중이라 모든 요청에 503을 돌려준다.
# AI 서비스가 시작할 때 모델 설정이나 색인을 불러오느라 늦게 준비되는 상황을 흉내 낸다. 기본값 0(끔).
SLOW_START_SECONDS = float(os.environ.get("SLOW_START_SECONDS", "0") or 0)
READY_AT = time.time() + SLOW_START_SECONDS
if SLOW_START_SECONDS:
    log("WARNING", f"시작 지연 스위치가 켜져 있습니다. {SLOW_START_SECONDS:.0f}초 동안 준비 중으로 응답합니다.")


@app.middleware("http")
async def password_gate(request, call_next):
    return await gate.gate(request, call_next)


@app.middleware("http")
async def not_ready_yet(request, call_next):
    if time.time() < READY_AT:
        return JSONResponse({"status": "starting"}, status_code=503)
    return await call_next(request)


app.include_router(gate.router)
if gate.enabled():
    log("INFO", "입장 비밀번호가 켜져 있습니다.")


def get_llm():
    """테스트에서는 이 함수를 가짜 LLM을 돌려주는 함수로 바꿔 끼운다."""
    return llm


def get_quiz_llm():
    return quiz_llm


def get_quiz_text_llm():
    return quiz_text_llm


class ChatRequest(BaseModel):
    message: str


class SettingsRequest(BaseModel):
    location: str | None = None
    timezone: str | None = None


class ReportRequest(BaseModel):
    message_id: int
    reason: str = ""


class FeedbackRequest(BaseModel):
    message_id: int
    liked: bool  # 좋아요 true, 별로예요 false


@app.get("/", response_class=HTMLResponse)
def home():
    page = (ROOT / "static" / "index.html").read_text(encoding="utf-8")
    banner = "" if settings.env in ("production", "local") else f'<div class="banner">테스트 환경 ({settings.env})</div>'
    if os.environ.get("BANNER"):  # 이미지를 새로 만들지 않고 환경변수만 바꿔 새 리비전을 만들 때 쓰는 화면 문구
        banner += f'<div class="banner">{html.escape(os.environ["BANNER"][:80])}</div>'
    return page.replace("<!--ENV_BANNER-->", banner)


@app.get("/health")
def health():
    return {"status": "ok"}


@app.get("/version")
def version():
    return {"commit": settings.commit, "revision": settings.revision, "env": settings.env,
            "llm": settings.provider, "model": settings.model, "storage": settings.storage, "auth": settings.auth,
            "banner": os.environ.get("BANNER", "")}


@app.get("/api/config")
def config():
    # publishable key 는 브라우저에 공개해도 되는 키다. secret key 는 절대 여기에 넣지 않는다.
    return {"env": settings.env, "auth": settings.auth, "storage": settings.storage,
            "supabase_url": settings.supabase_url if settings.auth == "supabase" else "",
            "supabase_publishable_key": settings.supabase_publishable_key if settings.auth == "supabase" else "",
            "google_login": settings.google_login, "show_turn_panel": settings.show_turn_panel,
            "ocr_enabled": settings.ocr_enabled,
            "options": OPTIONS, "cities": list(CITIES)}


@app.get("/api/me")
def me(user: User = Depends(current_user)):
    return {"id": user.id, "anonymous": user.anonymous, "email": user.email,
            "has_character": store.get_character(user.id) is not None}


# ── 캐릭터와 설정 ──────────────────────────────────────────────────

@app.get("/api/character")
def get_character(user: User = Depends(current_user)):
    c = store.get_character(user.id)
    return {"character": c or DEFAULT_CHARACTER, "saved": c is not None}


@app.put("/api/character")
def put_character(data: dict, user: User = Depends(current_user)):
    c = clean_character(data)
    store.save_character(user.id, c)
    log("INFO", "캐릭터 저장")
    return {"character": c}


@app.post("/api/character/preview")
def preview_character(data: dict):
    return {"prompt": build_persona(clean_character(data))}


@app.get("/api/settings")
def get_settings(user: User = Depends(current_user)):
    return store.get_settings(user.id)


@app.put("/api/settings")
def put_settings(req: SettingsRequest, user: User = Depends(current_user)):
    changes = {}
    if req.location in CITIES:
        changes["location"] = req.location
    if req.timezone:
        changes["timezone"] = req.timezone if req.timezone == "Asia/Seoul" else "Asia/Seoul"  # 지금은 한국 시간만
    store.save_settings(user.id, changes)
    return store.get_settings(user.id)


# ── 대화 ───────────────────────────────────────────────────────────

@app.get("/api/messages")
def messages(user: User = Depends(current_user)):
    show = settings.show_turn_panel
    return [{"id": m["id"], "role": m["role"], "content": m["content"],
             "meta": m["meta"] if show else {}, "created_at": m["created_at"].isoformat()}
            for m in store.recent_messages(user.id, 50)]


@app.post("/api/chat")
def chat(req: ChatRequest, user: User = Depends(current_user), model=Depends(get_llm)):
    message = req.message.strip()[:1000]
    if not message:
        raise HTTPException(400, "메시지가 비어 있습니다")

    def events():
        try:
            for event in agent.run_turn(message, user.id, model, store, settings.show_turn_panel):
                yield json.dumps(event, ensure_ascii=False, default=str) + "\n"
        except Exception as error:
            log("ERROR", "대화 처리 실패", error=type(error).__name__)
            yield json.dumps({"type": "error", "message": "답을 만들지 못했어요. 잠시 뒤 다시 보내 주세요."}, ensure_ascii=False) + "\n"

    return StreamingResponse(events(), media_type="application/x-ndjson")


@app.post("/api/greeting")
def greeting(user: User = Depends(current_user), model=Depends(get_llm)):
    return {"greeting": agent.greet(user.id, model, store)}


@app.post("/api/ocr")
async def read_photo(photo: UploadFile, user: User = Depends(current_user)):
    if not settings.ocr_enabled:
        raise HTTPException(404, "사진 글자 읽기가 꺼져 있습니다")
    try:
        return {"text": ocr.read_text(await photo.read())}
    except ocr.OcrError as error:
        raise HTTPException(500, str(error)) from error


@app.post("/api/quiz/solve")
async def solve_quiz(request: Request, background: BackgroundTasks, model=Depends(get_quiz_llm),
                     text_model=Depends(get_quiz_text_llm)):
    """문제 하나를 푼다. JSON(이지모드) 또는 multipart 사진(하드모드). 크레딧은 차감하지 않는다.

    quiz_server, token, question_id 를 함께 보내면 답을 정한 뒤 퀴즈 서버에 바로 제출하고
    submitted, answer_status, elapsed 를 함께 돌려준다. 셋 다 없으면 예전처럼 답만 돌려준다.

    Langfuse 트레이스는 응답을 보낸 뒤 보낸다(답이 0.2초쯤 빨라진다). Cloud Run 은 CPU 를 요청 중에만 주는 설정이면
    응답 뒤 작업이 멈출 수 있으므로, 이 API 를 쓰는 동안 서비스에 --no-cpu-throttling 을 건다.
    """
    if not settings.quiz_enabled or model is None:
        raise HTTPException(404, "Not Found")  # 꺼져 있으면 없는 주소처럼 보인다
    if not quiz_limiter.allow(quiz.client_ip(request)):
        return JSONResponse({"error": "rate_limited", "message": "요청이 너무 많아요. 1분 뒤 다시 보내 주세요."},
                            status_code=429)
    # 단계별 시간(ms): receive 본문 받기, photo 사진 읽기·줄이기. 풀이 시간은 timing(요청 도착 기준)
    background.add_task(flush_quiz_trace)  # 성공이든 실패든 응답 뒤에 한 번 보낸다
    arrived = clock = time.perf_counter()
    ms = {}

    def lap(name):
        nonlocal clock
        now = time.perf_counter()
        ms[name] = round((now - clock) * 1000)
        clock = now

    try:
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            # 본문을 읽기 전에 크기부터 본다(사진 5MB + 폼 여유). 읽은 뒤에도 사진 크기를 다시 확인한다
            if int(request.headers.get("content-length") or 0) > QUIZ_MAX_PHOTO_BYTES + 64 * 1024:
                raise quiz.QuizError(413, "사진은 5MB 이하만 받습니다.")
            form = await request.form()
            lap("receive")
            photo = form.get("photo")
            if photo is None or not hasattr(photo, "read"):
                raise quiz.QuizError(400, "photo 파일이 필요합니다.")
            data = await photo.read(QUIZ_MAX_PHOTO_BYTES + 1)
            if len(data) > QUIZ_MAX_PHOTO_BYTES:
                raise quiz.QuizError(413, "사진은 5MB 이하만 받습니다.")
            # 제출 정보는 모델을 부르기 전에 검사한다(허용되지 않은 주소면 모델 비용 없이 400)
            target = submit_target(form)
            text, image, count, trace = quiz.hard_request(form.get("type"), form.get("choice_count"), data,
                                                          photo.content_type)
            if settings.quiz_image_max_side:
                small, kind = await run_in_threadpool(quiz.shrink_photo, data, image["media_type"],
                                                      settings.quiz_image_max_side)
                image = {"data": small, "media_type": kind}
                trace["sent_bytes"] = len(small)
            lap("photo")
        else:
            try:
                body = await request.json()
            except ValueError:
                raise quiz.QuizError(400, "JSON 또는 multipart 사진을 보내 주세요.") from None
            lap("receive")
            target = submit_target(body if isinstance(body, dict) else {})
            text, image, count, trace = quiz.easy_request(body)

        # 그림이 있으면 그림 모델(QUIZ_MODEL), 없으면 글 모델(QUIZ_MODEL_TEXT). 스트리밍으로 받아 근거가 끝나면 바로 제출
        chosen = quiz_flow.pick_models(image, model, text_model)
        submit = make_submit(target) if target is not None else None
        done = await run_in_threadpool(lambda: quiz_flow.solve(
            chosen, text, image, count, trace, choices=trace.get("choices"), question=trace.get("question"),
            submit=submit, started=arrived))
    except quiz.QuizError as error:
        return JSONResponse({"error": "quiz_error", "message": error.message}, status_code=error.status)
    result = dict(done["result"])
    if done["submission"] is not None:
        result.update(done["submission"])
    result["timing"] = done["timing"]  # 새 칸(예전 앱은 무시한다)
    log("INFO", "퀴즈 풀기", mode=trace["mode"], type=trace["type"], model=chosen.model, fixed=done["notes"], ms=ms,
        timing=done["timing"], photo_bytes=trace.get("photo_bytes"), sent_bytes=trace.get("sent_bytes"),
        submit=target is not None, seen=done["seen"][:1000], think=done["think"][:500])
    return result


# ── 미리 깨우기, 서버 직접 풀기 ───────────────────────────────────

def warm_models(image_model, text_model):
    """그림 문제용·글 문제용 모델에 아주 짧은 요청을 한 번씩 보내 연결과 지시문 캐시를 준비한다."""
    jobs = [(image_model, quiz_flow.IMAGE_SYSTEM, "image"), (text_model or image_model, quiz_flow.TEXT_SYSTEM, "text")]
    results = []
    for model, system, kind in jobs:
        started = time.perf_counter()
        try:
            info = model.warm(system) or {}
            results.append({"kind": kind, "model": model.model, "ok": True, "ms": quiz_flow.elapsed_ms(started), **info})
        except Exception as error:
            results.append({"kind": kind, "model": model.model, "ok": False, "ms": quiz_flow.elapsed_ms(started),
                            "error": type(error).__name__})
    log("INFO", "퀴즈 모델 미리 깨움", results=results)
    return results


@app.post("/api/quiz/warmup")
async def quiz_warmup(request: Request, model=Depends(get_quiz_llm), text_model=Depends(get_quiz_text_llm)):
    if not settings.quiz_enabled or model is None:
        raise HTTPException(404, "Not Found")
    if not quiz_limiter.allow(quiz.client_ip(request)):
        return JSONResponse({"error": "rate_limited", "message": "요청이 너무 많아요. 1분 뒤 다시 보내 주세요."},
                            status_code=429)
    return {"warmed": await run_in_threadpool(warm_models, model, text_model)}


def auto_solver(image_model, text_model):
    """서버 직접 풀기가 이지모드 문제 하나를 푸는 함수. 그림이 있으면 퀴즈 서버에서 토큰으로 받아 함께 보낸다."""

    def solve_question(base, token, question):
        started = time.perf_counter()
        image_url = question.get("image_url")
        full_url = image_url if not image_url or image_url.startswith("http") else f"{base}/{image_url.lstrip('/')}"
        text, image, count, trace = quiz.easy_request({"type": question.get("type"), "question": question.get("text"),
                                                       "choices": question.get("choices"), "image_url": full_url})
        if full_url and full_url.startswith(base):
            # 퀴즈 서버 그림은 토큰이 필요할 수 있어, 서버가 받아 바이트로 넘긴다(받지 못하면 주소 그대로)
            try:
                r = quiz_auto_manager.http.get(full_url, headers={"Authorization": f"Bearer {token}"})
                kind = r.headers.get("content-type", "").split(";")[0].strip()
                if r.is_success and kind in quiz.PHOTO_TYPES:
                    image = {"data": r.content, "media_type": kind}
            except httpx.HTTPError as error:
                log("WARNING", "퀴즈 그림 받기 실패", error=type(error).__name__)
        trace["mode"] = "auto"
        chosen = quiz_flow.pick_models(image, image_model, text_model)
        target = {"base": base, "token": token, "question_id": question["id"]}
        done = quiz_flow.solve(chosen, text, image, count, trace, choices=trace.get("choices"),
                               question=trace.get("question"), submit=make_submit(target), started=started)
        tracing.flush()
        log("INFO", "퀴즈 풀기", mode="auto", type=trace["type"], model=chosen.model, fixed=done["notes"],
            timing=done["timing"], submit=True, seen=done["seen"][:1000], think=done["think"][:500])
        return {**done["result"], **(done["submission"] or {}), "timing": done["timing"]}

    return solve_question


class AutoStart(BaseModel):
    quiz_server: str
    token: str


class AutoStop(BaseModel):
    token: str


def auto_enabled():
    if not (settings.quiz_enabled and settings.quiz_auto_enabled):
        raise HTTPException(404, "Not Found")


def auto_token(token):
    if not token or len(token) > 1000 or any(c in token for c in "\r\n"):
        raise HTTPException(400, "token 형식이 올바르지 않습니다.")
    return token


@app.post("/api/quiz/auto/start")
async def quiz_auto_start(req: AutoStart, request: Request, background: BackgroundTasks,
                          model=Depends(get_quiz_llm), text_model=Depends(get_quiz_text_llm)):
    """서버가 퀴즈 서버를 직접 확인하며 이지모드 문제를 풀어 낸다. 같은 token 으로는 하나만 돈다."""
    auto_enabled()
    if model is None:
        raise HTTPException(404, "Not Found")
    if not quiz_limiter.allow(quiz.client_ip(request)):
        return JSONResponse({"error": "rate_limited", "message": "요청이 너무 많아요. 1분 뒤 다시 보내 주세요."},
                            status_code=429)
    base = quiz.allowed_server(req.quiz_server, settings.quiz_server_allowlist)
    if base is None:
        return JSONResponse({"error": "quiz_error", "message": "허용되지 않은 퀴즈 서버 주소입니다."}, status_code=400)
    started, snapshot = quiz_auto_manager.start(base, auto_token(req.token), auto_solver(model, text_model))
    if started:
        background.add_task(warm_models, model, text_model)  # 시작할 때 한 번 미리 깨운다(응답 뒤)
    return {"started": started, **snapshot}


@app.post("/api/quiz/auto/stop")
async def quiz_auto_stop(req: AutoStop):
    auto_enabled()
    return {"stopped": quiz_auto_manager.stop(auto_token(req.token))}


@app.get("/api/quiz/auto/status")
async def quiz_auto_status(request: Request, token: str | None = None):
    """token 은 쿼리(?token=) 또는 Authorization: Bearer 헤더로 받는다. 헤더 쪽이 주소 로그에 남지 않아 안전하다."""
    auto_enabled()
    header = request.headers.get("authorization", "")
    token = token or (header[7:].strip() if header.lower().startswith("bearer ") else "")
    snapshot = quiz_auto_manager.status(auto_token(token))
    return snapshot if snapshot is not None else {"running": False, "stop_reason": None, "results": []}


def make_submit(target):
    """퀴즈 서버 제출 함수. 응답에 붙일 submitted, answer_status, elapsed(, submit_error) 를 돌려준다.
    토큰과 근거 글은 로그에 남기지 않는다. 호스트, 보낸 답(값과 타입), 퀴즈 서버 응답 본문, 결과만 남긴다."""

    def submit(answer_part, reason):
        started = time.perf_counter()
        status, elapsed, problem, response = quiz_submitter.submit_with_body(target, answer_part, reason)
        log("INFO" if problem is None else "WARNING", "퀴즈 답 제출", host=urlsplit(target["base"]).hostname,
            sent=quiz.answer_for_log(target["question_id"], answer_part), response=response,
            status=status, ok=problem is None, ms=round((time.perf_counter() - started) * 1000))
        out = {"submitted": problem is None, "answer_status": status, "elapsed": elapsed}
        if problem:
            out["submit_error"] = problem
        return out

    return submit


def submit_target(fields):
    """제출 정보를 검사한다. QUIZ_SERVER_SUBMIT=false 면 제출 정보를 쓰지 않고 None(예전처럼 답만 돌려준다).
    그때는 응답에 submitted 칸이 없으므로 앱이 직접 퀴즈 서버에 낸다."""
    if not settings.quiz_server_submit:
        return None
    return quiz.submit_target(fields, settings.quiz_server_allowlist)


def flush_quiz_trace():
    """응답을 보낸 뒤 퀴즈 트레이스를 Langfuse 로 보낸다. 걸린 시간만 로그로 남긴다."""
    started = time.perf_counter()
    tracing.flush()
    log("INFO", "퀴즈 트레이스 보냄", flush_ms=round((time.perf_counter() - started) * 1000))


def own_reply(user, message_id):
    """로그인한 사용자의 답(assistant) 메시지만 돌려준다. 남의 메시지나 없는 메시지면 None."""
    message = store.get_message(user.id, message_id)
    return message if message and message["role"] == "assistant" else None


@app.post("/api/report")
def report(req: ReportRequest, user: User = Depends(current_user)):
    log("WARNING", "부적절한 답변 신고", message_id=req.message_id, reason_length=len(req.reason))
    # 응답은 지금처럼 늘 ok. 내 답 메시지일 때만 Langfuse 점수(report=1)를 보낸다. 사유는 comment 로
    message = own_reply(user, req.message_id)
    if message:
        tracing.score("report", 1, message["meta"].get("trace_id"), comment=req.reason.strip() or None)
    return {"ok": True}


@app.post("/api/feedback")
def feedback(req: FeedbackRequest, user: User = Depends(current_user)):
    """답 말풍선의 좋아요(1)/별로예요(0)를 Langfuse 점수 user-feedback 으로 보낸다."""
    message = own_reply(user, req.message_id)
    if not message:
        raise HTTPException(404, "메시지를 찾을 수 없습니다")
    scored = tracing.score("user-feedback", 1 if req.liked else 0, message["meta"].get("trace_id"))
    log("INFO", "사용자 반응", message_id=req.message_id, liked=req.liked, scored=scored)
    return {"ok": True, "scored": scored}


# ── 기억, 약속, 내 데이터 ───────────────────────────────────────────

@app.get("/api/memories")
def list_memories(user: User = Depends(current_user)):
    return [{"id": m["id"], "kind": m["kind"], "text": m["text"]} for m in store.list_memories(user.id)]


@app.delete("/api/memories/{memory_id}")
def delete_memory(memory_id: int, user: User = Depends(current_user)):
    store.delete_memory(user.id, memory_id)
    return {"ok": True}


@app.delete("/api/memories")
def clear_memories(user: User = Depends(current_user)):
    store.clear_memories(user.id)
    return {"ok": True}


@app.get("/api/reminders")
def list_reminders(user: User = Depends(current_user)):
    tz = when.now_in(store.get_settings(user.id)["timezone"]).tzinfo
    return [{"id": r["id"], "text": r["text"], "status": r["status"],
             "due_label": when.label(r["due_at"].astimezone(tz))} for r in store.list_reminders(user.id)]


@app.delete("/api/reminders/{reminder_id}")
def cancel_reminder(reminder_id: int, user: User = Depends(current_user)):
    store.cancel_reminder(user.id, reminder_id)
    return {"ok": True}


@app.delete("/api/data")
def delete_data(user: User = Depends(current_user)):
    store.delete_all(user.id)
    log("INFO", "사용자 데이터 전체 삭제")
    return {"ok": True}
