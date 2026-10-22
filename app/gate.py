"""비밀번호 입력 화면(입장 비밀번호).

APP_PASSWORD 환경변수가 있으면, 비밀번호를 맞게 넣은 브라우저만 화면과 API를 쓸 수 있다.
- 배포할 때는 Secret Manager 의 시크릿을 APP_PASSWORD 로 연결한다. 비밀번호를 코드나 명령에 적지 않는다.
- APP_PASSWORD 가 비어 있으면(노트북 실행, 테스트) 지금처럼 비밀번호 없이 동작한다.
- 맞게 넣으면 HttpOnly 쿠키를 준다. 쿠키 값은 비밀번호로 서명한 값이라, 비밀번호를 바꾸면 기존 입장은 모두 풀린다.
- 비밀번호, 쿠키 값은 로그·오류 메시지·이번 턴 패널 어디에도 남기지 않는다.
"""
import asyncio
import hashlib
import hmac
import os

from fastapi import APIRouter, Request
from fastapi.responses import HTMLResponse, JSONResponse, RedirectResponse
from pydantic import BaseModel

from app.config import ROOT
from app.logs import log

APP_PASSWORD = os.environ.get("APP_PASSWORD", "").strip()
COOKIE = "fa_gate"
MAX_AGE = 60 * 60 * 24 * 7  # 7일 동안 다시 묻지 않는다
OPEN_PATHS = {"/login", "/api/login", "/api/logout", "/health"}

router = APIRouter()


def enabled() -> bool:
    return bool(APP_PASSWORD)


def _token() -> str:
    return hmac.new(APP_PASSWORD.encode(), b"friend-agent-gate-v1", hashlib.sha256).hexdigest()


def _passed(request: Request) -> bool:
    value = request.cookies.get(COOKIE, "")
    return bool(value) and hmac.compare_digest(value, _token())


async def gate(request: Request, call_next):
    """비밀번호를 통과하지 않은 요청은 화면이면 /login 으로, API면 401 로 돌려보낸다."""
    if not enabled() or request.url.path in OPEN_PATHS or _passed(request):
        return await call_next(request)
    if request.url.path.startswith("/api/"):
        return JSONResponse({"detail": "비밀번호를 먼저 입력해 주세요."}, status_code=401)
    return RedirectResponse("/login", status_code=303)


class LoginRequest(BaseModel):
    password: str


@router.get("/login", response_class=HTMLResponse)
def login_page(request: Request):
    if not enabled() or _passed(request):
        return RedirectResponse("/", status_code=303)
    return (ROOT / "static" / "login.html").read_text(encoding="utf-8")


@router.post("/api/login")
async def login(req: LoginRequest, request: Request):
    if not enabled():
        return {"ok": True}
    if not hmac.compare_digest(req.password.encode(), APP_PASSWORD.encode()):
        await asyncio.sleep(1)  # 마구 대입해 보는 시도를 느리게 만든다
        log("WARNING", "입장 비밀번호 확인 실패")  # 입력값은 남기지 않는다
        return JSONResponse({"detail": "비밀번호가 맞지 않아요."}, status_code=401)
    response = JSONResponse({"ok": True})
    https = request.headers.get("x-forwarded-proto", request.url.scheme) == "https"
    response.set_cookie(COOKIE, _token(), max_age=MAX_AGE, httponly=True, secure=https, samesite="lax")
    log("INFO", "입장 비밀번호 확인 성공")
    return response


@router.post("/api/logout")
def logout():
    response = JSONResponse({"ok": True})
    response.delete_cookie(COOKIE)
    return response
