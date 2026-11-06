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

from fastapi import Depends, FastAPI, HTTPException, Request, UploadFile
from fastapi.concurrency import run_in_threadpool
from fastapi.responses import HTMLResponse, JSONResponse, StreamingResponse
from pydantic import BaseModel

from app import agent, gate, ocr, quiz, tracing, when
from app.auth import Auth, User
from app.config import QUIZ_MAX_PHOTO_BYTES, QUIZ_MAX_TOKENS, ROOT, load_settings
from app.llm import make_llm
from app.logs import log
from app.persona import DEFAULT_CHARACTER, OPTIONS, build_persona, clean_character
from app.store.base import make_store
from app.tools import CITIES

settings = load_settings()  # 꼭 필요한 값이 없으면 여기서 종료한다
tracing.setup(settings)  # .env 를 읽은 뒤에 켜야 Langfuse 키를 찾는다. 키가 없으면 꺼진다
llm = make_llm(settings)
quiz_llm = make_llm(settings, settings.quiz_model) if settings.quiz_enabled else None
quiz_limiter = quiz.RateLimiter(settings.quiz_rate_per_minute)
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
async def solve_quiz(request: Request, model=Depends(get_quiz_llm)):
    """문제 하나를 푼다. JSON(이지모드) 또는 multipart 사진(하드모드). 크레딧은 차감하지 않는다."""
    if not settings.quiz_enabled or model is None:
        raise HTTPException(404, "Not Found")  # 꺼져 있으면 없는 주소처럼 보인다
    if not quiz_limiter.allow(quiz.client_ip(request)):
        return JSONResponse({"error": "rate_limited", "message": "요청이 너무 많아요. 1분 뒤 다시 보내 주세요."},
                            status_code=429)
    try:
        if request.headers.get("content-type", "").startswith("multipart/form-data"):
            # 본문을 읽기 전에 크기부터 본다(사진 5MB + 폼 여유). 읽은 뒤에도 사진 크기를 다시 확인한다
            if int(request.headers.get("content-length") or 0) > QUIZ_MAX_PHOTO_BYTES + 64 * 1024:
                raise quiz.QuizError(413, "사진은 5MB 이하만 받습니다.")
            form = await request.form()
            photo = form.get("photo")
            if photo is None or not hasattr(photo, "read"):
                raise quiz.QuizError(400, "photo 파일이 필요합니다.")
            data = await photo.read(QUIZ_MAX_PHOTO_BYTES + 1)
            if len(data) > QUIZ_MAX_PHOTO_BYTES:
                raise quiz.QuizError(413, "사진은 5MB 이하만 받습니다.")
            text, image, count, trace = quiz.hard_request(form.get("type"), form.get("choice_count"), data,
                                                          photo.content_type)
        else:
            try:
                body = await request.json()
            except ValueError:
                raise quiz.QuizError(400, "JSON 또는 multipart 사진을 보내 주세요.") from None
            text, image, count, trace = quiz.easy_request(body)

        def call():
            # 모델은 한 번만 부른다. Langfuse 에는 사진 내용 대신 크기와 종류만 남긴다
            with tracing.observe("quiz-solve", "generation", model=model.model, input=trace,
                                 metadata={"mode": trace["mode"], "type": trace["type"]}):
                answer = model.complete_vision_json(quiz.SYSTEM, text, image, QUIZ_MAX_TOKENS, trace)
            tracing.flush()
            return answer

        try:
            answer = await run_in_threadpool(call)
        except quiz.QuizError:
            raise
        except Exception as error:
            log("ERROR", "퀴즈 풀기 실패", mode=trace["mode"], error=type(error).__name__)
            raise quiz.QuizError(502, "문제를 풀지 못했어요. 잠시 뒤 다시 보내 주세요.") from None
        result, notes = quiz.to_result(answer, count, trace.get("choices"), trace.get("question"))
    except quiz.QuizError as error:
        return JSONResponse({"error": "quiz_error", "message": error.message}, status_code=error.status)
    log("INFO", "퀴즈 풀기", mode=trace["mode"], type=trace["type"], fixed=notes)
    return result


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
