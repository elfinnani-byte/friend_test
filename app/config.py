"""설정 읽기. 앱이 시작할 때 한 번 실행된다.

꼭 필요한 값이 없으면 오류 로그를 남기고 바로 종료한다.
값 없이 시작해서 요청마다 오류를 돌려주는 것보다, 배포 단계에서 바로 실패하는 편이 문제를 빨리 찾을 수 있다.
"""
import os
import sys
from dataclasses import dataclass
from pathlib import Path

from app.logs import log

ROOT = Path(__file__).resolve().parent.parent

# LLM 제공자별 기본값. 모델 이름은 바뀔 수 있으므로 LLM_MODEL 환경변수로 덮어쓸 수 있다.
PROVIDERS = {
    "openai": {"base_url": None, "model": "gpt-4o-mini"},
    "gemini": {"base_url": "https://generativelanguage.googleapis.com/v1beta/openai/", "model": "gemini-3.8-flash"},
    "claude": {"base_url": None, "model": "claude-opus-5-5"},
    "fake": {"base_url": None, "model": "fake"},
}

# 퀴즈 풀기 API(/api/quiz/solve). 아무나 LLM 을 쓰지 못하게 기본은 꺼 두고, 사진 크기와 IP 당 요청 수를 제한한다
QUIZ_MAX_PHOTO_BYTES = 5 * 1024 * 1024
QUIZ_MAX_TOKENS = 1024            # 출력 상한. Claude 는 생각 토큰도 여기에 들어가므로 답이 잘리지 않을 만큼만 둔다
QUIZ_MAX_TOKENS_TEXT = 256        # 글 문제(ANSWER, REASON 두 줄). 생각을 끈 모델 기준
QUIZ_MAX_TOKENS_IMAGE = 1536      # 그림 문제(SEEN 에 그림 속 글·코드를 옮겨 적으므로 넉넉히. Opus 5.5 는 생각을 끌 수 없다)
QUIZ_REASON_MAX = 20              # 퀴즈 서버에 보내는 근거 글자 수 상한
QUIZ_AUTO_MAX_SECONDS = 30 * 60   # 서버 직접 풀기(auto)는 이 시간이 지나면 저절로 멈춘다


@dataclass
class Settings:
    provider: str
    api_key: str
    model: str
    base_url: str | None
    env: str
    commit: str
    revision: str
    storage: str              # memory, supabase
    auth: str                 # none, supabase
    supabase_url: str
    supabase_publishable_key: str
    supabase_secret_key: str
    google_login: bool
    show_turn_panel: bool
    ocr_enabled: bool
    quiz_enabled: bool        # QUIZ_SOLVER_ENABLED=true 일 때만 /api/quiz/solve 가 열린다
    quiz_model: str           # 이미지를 읽는 모델. 없으면 대화 모델과 같다
    quiz_model_text: str      # 그림 없는 글 문제 모델. 비어 있으면 quiz_model 을 쓴다
    quiz_auto_enabled: bool   # QUIZ_AUTO_ENABLED=true 일 때만 /api/quiz/auto/* 가 열린다
    quiz_rate_per_minute: int # IP 하나가 1분에 보낼 수 있는 퀴즈 요청 수
    quiz_server_allowlist: list  # 답을 대신 제출해도 되는 퀴즈 서버 주소 패턴(https, * 는 점 없는 글자들)
    quiz_server_submit: bool  # false 면 제출 정보가 와도 퀴즈 서버에 내지 않고 답만 돌려준다(앱이 직접 낸다)
    quiz_visible_check: bool  # QUIZ_VISIBLE_CHECK: on(기본)이면 VISIBLE 판단·다시 찍기, off 면 고치기 전처럼 끝까지 풀기


def load_dotenv(path):
    """노트북에서 실행할 때만 .env 파일을 읽는다. 배포할 때는 .env가 올라가지 않는다."""
    if not path.exists():
        return
    for line in path.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, value = line.split("=", 1)
        os.environ.setdefault(key.strip(), value.strip().strip('"').strip("'"))


def env_flag(name, default):
    value = os.environ.get(name, "").strip().lower()
    if not value:
        return default
    return value in ("1", "true", "yes", "on")


def stop(message):
    log("ERROR", message)
    sys.exit(1)


def load_settings():
    load_dotenv(ROOT / ".env")

    provider = os.environ.get("LLM_PROVIDER", "openai").strip().lower()
    if provider not in PROVIDERS:
        stop(f"LLM_PROVIDER 값 '{provider}' 를 알 수 없어 앱을 시작하지 않습니다. openai, gemini, claude, fake 중 하나를 쓰세요.")

    api_key = os.environ.get("LLM_API_KEY", "").strip()
    if provider != "fake" and not api_key:
        stop("LLM_API_KEY 환경변수가 없어 앱을 시작하지 않습니다.")

    env = os.environ.get("ENV", "local").strip()
    storage = os.environ.get("STORAGE", "memory").strip().lower()
    auth = os.environ.get("AUTH", "none").strip().lower()
    supabase_url = os.environ.get("SUPABASE_URL", "").strip().rstrip("/")
    publishable = os.environ.get("SUPABASE_PUBLISHABLE_KEY", "").strip()
    secret = os.environ.get("SUPABASE_SECRET_KEY", "").strip()

    if storage not in ("memory", "supabase"):
        stop(f"STORAGE 값 '{storage}' 를 알 수 없습니다. memory 또는 supabase 를 쓰세요.")
    if auth not in ("none", "supabase"):
        stop(f"AUTH 값 '{auth}' 를 알 수 없습니다. none 또는 supabase 를 쓰세요.")
    if storage == "supabase" and auth != "supabase":
        stop("STORAGE=supabase 는 AUTH=supabase 와 함께 써야 합니다. 로그인 없이 DB에 저장하면 모든 사용자의 데이터가 한 사람 것으로 섞입니다.")
    if auth == "supabase" and not (supabase_url and publishable):
        stop("AUTH=supabase 인데 SUPABASE_URL 또는 SUPABASE_PUBLISHABLE_KEY 가 없어 앱을 시작하지 않습니다.")
    if storage == "supabase" and not secret:
        stop("STORAGE=supabase 인데 SUPABASE_SECRET_KEY 가 없어 앱을 시작하지 않습니다.")

    defaults = PROVIDERS[provider]
    settings = Settings(
        provider=provider,
        api_key=api_key,
        model=os.environ.get("LLM_MODEL", "").strip() or defaults["model"],
        base_url=defaults["base_url"],
        env=env,
        commit=os.environ.get("COMMIT_SHA", "local").strip(),
        revision=os.environ.get("K_REVISION", "local"),  # Cloud Run이 리비전 이름을 넣어 준다
        storage=storage,
        auth=auth,
        supabase_url=supabase_url,
        supabase_publishable_key=publishable,
        supabase_secret_key=secret,
        google_login=env_flag("GOOGLE_LOGIN", False),
        # 개발용 "이번 턴" 패널. production 에서는 기본으로 끈다.
        show_turn_panel=env_flag("SHOW_TURN_PANEL", env != "production"),
        ocr_enabled=env_flag("OCR_ENABLED", False),  # 사진 글자 읽기. tesseract 프로그램이 필요하다
        quiz_enabled=os.environ.get("QUIZ_SOLVER_ENABLED", "").strip().lower() == "true",
        quiz_model=os.environ.get("QUIZ_MODEL", "").strip() or os.environ.get("LLM_MODEL", "").strip() or defaults["model"],
        quiz_model_text=os.environ.get("QUIZ_MODEL_TEXT", "").strip(),
        quiz_auto_enabled=env_flag("QUIZ_AUTO_ENABLED", False),
        quiz_rate_per_minute=int(os.environ.get("QUIZ_RATE_PER_MINUTE", "10") or 10),
        quiz_server_submit=env_flag("QUIZ_SERVER_SUBMIT", True),
        quiz_visible_check=env_flag("QUIZ_VISIBLE_CHECK", True),
        quiz_server_allowlist=[p.strip() for p in (os.environ.get("QUIZ_SERVER_ALLOWLIST", "").strip()
                                                   or "https://quiz-battle*.us-central1.run.app").split(",") if p.strip()],
    )
    log("INFO", "앱을 시작합니다.", provider=settings.provider, model=settings.model, env=settings.env,
        storage=settings.storage, auth=settings.auth, turn_panel=settings.show_turn_panel,
        quiz=settings.quiz_enabled, quiz_model=settings.quiz_model if settings.quiz_enabled else None)
    return settings
