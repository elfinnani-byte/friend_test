"""퀴즈 대결 에이전트 앱이 쓰는 문제 풀기(POST /api/quiz/solve).

- 이지모드: 문제 글, 보기 목록(객관식), 그림 주소(그림 문제)를 JSON 으로 받는다.
- 하드모드: 퀴즈 화면을 폰으로 찍은 사진을 받는다. 화면의 보기 번호는 1부터 적혀 있다.
모델에게는 두 모드 모두 1부터 센 번호로 답하게 하고, API 응답의 0부터 센 번호로 바꾸는 일은 코드가 한다.
모델은 한 번만 부르고 출력은 짧게 받는다. 크레딧은 차감하지 않는다.
"""
import difflib
import re
import unicodedata
import threading
import time
from collections import defaultdict, deque
from urllib.parse import urlsplit

import httpx

MAX_QUESTION = 2000
MAX_CHOICE_TEXT = 300
MIN_CHOICES, MAX_CHOICES = 2, 10
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

# 객관식에서 첫 답에 보기 번호가 없을 때 한 번 더 짧게 묻는 지시문
RETRY_SYSTEM = "\n".join([
    "너는 퀴즈 대결에서 객관식 문제를 푸는 선수다. 정답 보기 번호 하나만 고른다.",
    "- 번호는 1부터 센다(화면 사진이면 화면에 적힌 번호 그대로). 확실하지 않아도 가장 그럴듯한 번호 하나를 고른다.",
    '- JSON 하나로만 답한다: {"answer": 번호}',
])
FALLBACK_REASON = "번호를 못 읽어 1번을 냄"   # 20자 이내
RETRY_REASON = "번호만 다시 물어 고름"


# NFKC 가 바꾸지 않는 동그라미 숫자(❶~❿, ➀~➉, ➊~➓)
DINGBAT_DIGITS = {start + i: f" {i + 1} " for start in (0x2776, 0x2780, 0x278A) for i in range(10)}


def choice_number(value, count):
    """모델이 낸 보기 번호(1부터)를 읽는다. 2, "2", "2번", "2.", "(2)", "②", "２" 모두 2. 범위 안 번호가 없으면 None."""
    if isinstance(value, bool) or value is None:
        return None
    if isinstance(value, (int, float)):
        return int(value) if value == int(value) and 1 <= value <= count else None
    text = unicodedata.normalize("NFKC", str(value)).translate(DINGBAT_DIGITS)  # ① → 1, 전각 ２ → 2, ❷ → 2
    for found in re.findall(r"\d+", text):
        if 1 <= int(found) <= count:
            return int(found)
    return None


NUMBER_ONLY = re.compile(r"숫자\s*(만|로)|아라비아\s*숫자")


class QuizError(Exception):
    def __init__(self, status, message):
        super().__init__(message)
        self.status, self.message = status, message


# ── 입력 정리 ───────────────────────────────────────────────────────

def easy_request(body):
    """이지모드 JSON 을 검사해 (모델에게 줄 글, 이미지, 보기 수, 트레이스 입력)으로 바꾼다."""
    if not isinstance(body, dict):
        raise QuizError(400, "JSON 객체를 보내 주세요.")
    kind = body.get("type")
    question = body.get("question")
    if kind not in ("choice", "short"):
        raise QuizError(400, "type 은 choice 또는 short 입니다.")
    if not isinstance(question, str) or not question.strip() or len(question) > MAX_QUESTION:
        raise QuizError(400, f"question 은 1~{MAX_QUESTION}자 글이어야 합니다.")
    image_url = body.get("image_url")
    if image_url is not None and (not isinstance(image_url, str) or not re.match(r"^https?://\S+$", image_url)):
        raise QuizError(400, "image_url 은 http(s) 로 시작하는 전체 주소여야 합니다.")
    lines = ["[객관식]" if kind == "choice" else "[단답]", f"문제: {question.strip()}"]
    count = None
    if kind == "choice":
        choices = body.get("choices")
        if (not isinstance(choices, list) or not MIN_CHOICES <= len(choices) <= MAX_CHOICES
                or not all(isinstance(c, str) and 0 < len(c) <= MAX_CHOICE_TEXT for c in choices)):
            raise QuizError(400, f"객관식은 choices 에 보기 글 {MIN_CHOICES}~{MAX_CHOICES}개가 필요합니다.")
        count = len(choices)
        lines.append("보기:")
        lines += [f"{i}. {c}" for i, c in enumerate(choices, start=1)]  # 모델에게는 1부터 보여 준다
    if image_url:
        lines.append("그림이 함께 주어졌다. 그림을 보고 푼다.")
    trace = {"mode": "easy", "type": kind, "question": question.strip(), "choices": body.get("choices") if count else None,
             "image_url": image_url}
    return "\n".join(lines), ({"url": image_url} if image_url else None), count, trace


def hard_request(kind, choice_count, photo, media_type):
    """하드모드 사진을 검사해 (모델에게 줄 글, 이미지, 보기 수, 트레이스 입력)으로 바꾼다."""
    if kind not in ("choice", "short"):
        raise QuizError(400, "type 은 choice 또는 short 입니다.")
    if media_type not in PHOTO_TYPES:
        raise QuizError(415, "사진은 JPEG, PNG, WebP, GIF 만 받습니다.")
    if not photo:
        raise QuizError(400, "photo 가 비어 있습니다.")
    count = None
    if kind == "choice":
        try:
            count = int(choice_count)
        except (TypeError, ValueError):
            count = 0
        if not MIN_CHOICES <= count <= MAX_CHOICES:
            raise QuizError(400, f"객관식은 choice_count 에 보기 수({MIN_CHOICES}~{MAX_CHOICES})가 필요합니다.")
    lines = ["[객관식]" if kind == "choice" else "[단답]",
             "사진은 퀴즈 화면을 폰으로 찍은 것이다. 화면의 문제를 읽고 푼다. 화면 밖의 글은 무시한다."]
    if count:
        lines.append(f"보기는 {count}개이고 화면에 1부터 {count}까지 번호가 적혀 있다. 답에 그 번호를 쓴다.")
    trace = {"mode": "hard", "type": kind, "choice_count": count, "photo_bytes": len(photo), "media_type": media_type}
    return "\n".join(lines), {"data": photo, "media_type": media_type}, count, trace


# 지시문 예시를 따라 근거 앞에 붙는 문제 종류 머리말(예: "상식: 대한민국 수도는 서울")
_REASON_LABEL = re.compile(r"^\s*(상식|코드|개수\s*세기|그림\s*해석|근거|REASON)\s*[:：]\s*", re.IGNORECASE)


def clip_reason(text, limit=20):
    """퀴즈 서버에 보내는 근거: 앞뒤 공백·따옴표를 빼고 limit 자 안으로 줄인다.
    넘치면 단어 중간이 아니라 limit 자 안의 마지막 띄어쓰기 앞에서 자른다. 띄어쓰기가 없으면 limit 자에서 자른다."""
    text = " ".join(_REASON_LABEL.sub("", str(text or "").strip().strip('"\'*` ')).split())
    if len(text) <= limit:
        return text
    head = text[:limit + 1]  # limit 번째 다음 글자가 띄어쓰기면 limit 자 전체가 한 단어 단위로 끝난다
    cut = head.rfind(" ")
    return (text[:cut] if cut > 0 else text[:limit]).strip()


def normalize(text):
    """보기 글 비교용: 공백을 지우고 소문자로."""
    return re.sub(r"\s+", "", str(text)).lower()


def match_choice(answer_text, choices):
    """고른 보기 글과 가장 잘 맞는 보기의 0부터 센 번호. 확실하지 않으면 None."""
    if not answer_text or not choices:
        return None
    target = normalize(answer_text)
    exact = [i for i, c in enumerate(choices) if normalize(c) == target]
    if len(exact) == 1:
        return exact[0]
    if exact:
        return None  # 같은 글의 보기가 둘 이상이면 글로는 정할 수 없다
    scores = sorted(((difflib.SequenceMatcher(None, target, normalize(c)).ratio(), i) for i, c in enumerate(choices)),
                    reverse=True)
    best, second = scores[0], scores[1] if len(scores) > 1 else (0, None)
    if best[0] >= 0.8 and best[0] - second[0] >= 0.1:
        return best[1]
    return None


def only_number(text):
    """'5개', '약 1,000원' 처럼 숫자가 하나뿐이면 그 숫자만 남긴다. 숫자가 없거나 여럿이면 None."""
    numbers = re.findall(r"-?\d+(?:\.\d+)?", re.sub(r"(?<=\d),(?=\d{3})", "", text))
    return numbers[0] if len(numbers) == 1 else None


def short_answer(text, question=None):
    """단답을 정리한다. (답, 고친 내용). 문제(또는 하드모드 화면 글)가 숫자만 요구하는데 단위가 붙어 있으면 숫자만 남긴다.
    숫자만 요구한 게 아니면 형식 그대로 둔다("7" 과 "7개" 는 다른 답)."""
    text, notes = str(text or "").strip(), []
    if question and NUMBER_ONLY.search(question) and not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        number = only_number(text)
        if number is not None:
            notes.append("unit_removed")
            text = number
    return text, notes


_EVAL_TAG = re.compile(r"^[A-Za-z0-9_.=-]{1,40}$")


def solve_options(fields):
    """풀이 옵션.
    supports_retake: 앱이 다시 찍기 응답을 알아듣는다(없으면 옛 앱이므로 안 보여도 끝까지 풀어 제출).
    allow_guess: 안 보여도 끝까지 풀어 제출(다시 찍은 두 번째 사진용). eval: 측정 구분 표시(Langfuse metadata)."""
    flag = lambda name: str(fields.get(name, "")).strip().lower() in ("1", "true", "yes", "on")
    tag = str(fields.get("eval") or "").strip()
    return {"supports_retake": flag("supports_retake"), "allow_guess": flag("allow_guess"),
            "eval": tag if _EVAL_TAG.match(tag) else None}


# ── 퀴즈 서버에 답 바로 제출 ────────────────────────────────────────
# quiz_server, token, question_id 셋이 함께 오면 답을 정한 뒤 서버가 {quiz_server}/api/answer 로 제출한다.
# 셋 다 없으면 예전처럼 답만 돌려준다(첫 APK 호환). 팀 토큰은 로그, Langfuse, 응답 어디에도 남기지 않는다.

SUBMIT_FIELDS = ("quiz_server", "token", "question_id")


def server_pattern(pattern):
    """'https://quiz-battle*.us-central1.run.app' → 호스트 정규식. * 는 점이 없는 영문·숫자·하이픈만 뜻한다."""
    parts = urlsplit(pattern)
    if parts.scheme != "https" or not parts.hostname:
        raise ValueError(f"QUIZ_SERVER_ALLOWLIST 항목은 https 주소여야 합니다: {pattern}")
    return re.compile("^" + re.escape(parts.hostname.lower()).replace(r"\*", "[a-z0-9-]*") + "$")


def allowed_server(url, allowlist):
    """허용 목록에 맞는 https 주소면 'https://호스트' 를, 아니면 None 을 돌려준다.
    사용자 정보, 다른 포트, 경로, 쿼리가 붙은 주소는 받지 않는다(다른 곳으로 토큰을 보내지 않게)."""
    if not isinstance(url, str) or len(url) > 300:
        return None
    try:
        parts = urlsplit(url.strip())
        port = parts.port
    except ValueError:
        return None
    if (parts.scheme != "https" or not parts.hostname or parts.username or parts.password
            or port not in (None, 443) or parts.path not in ("", "/") or parts.query or parts.fragment):
        return None
    host = parts.hostname.lower()
    if any(server_pattern(p).match(host) for p in allowlist):
        return f"https://{host}"
    return None


def submit_target(fields, allowlist):
    """요청의 제출 정보를 검사한다. 셋 다 없으면 None, 일부만 있거나 주소가 허용되지 않으면 QuizError(400)."""
    given = {k: fields.get(k) for k in SUBMIT_FIELDS if fields.get(k) not in (None, "")}
    if not given:
        return None
    if len(given) < len(SUBMIT_FIELDS):
        raise QuizError(400, "답을 바로 제출하려면 quiz_server, token, question_id 를 모두 보내 주세요.")
    base = allowed_server(given["quiz_server"], allowlist)
    if base is None:
        raise QuizError(400, "허용되지 않은 퀴즈 서버 주소입니다.")
    token, question_id = given["token"], given["question_id"]
    if not isinstance(token, str) or len(token) > 1000 or any(c in token for c in "\r\n"):
        raise QuizError(400, "token 형식이 올바르지 않습니다.")
    if not isinstance(question_id, (str, int)) or len(str(question_id)) > 200:
        raise QuizError(400, "question_id 형식이 올바르지 않습니다.")
    return {"base": base, "token": token, "question_id": question_id}


class AnswerSubmitter:
    """퀴즈 서버에 답을 낸다. 같은 http 클라이언트를 계속 써서 연결을 재사용한다(TLS 연결 비용을 줄인다)."""

    def __init__(self, timeout=5.0):
        self.http = httpx.Client(timeout=timeout, follow_redirects=False,  # 리다이렉트로 토큰이 다른 곳에 가지 않게
                                 limits=httpx.Limits(max_keepalive_connections=10, keepalive_expiry=60))

    def submit(self, target, answer, reason):
        """(상태 코드 또는 None, elapsed 또는 None, 오류 문구 또는 None)"""
        return self.submit_with_body(target, answer, reason)[:3]

    def submit_with_body(self, target, answer, reason):
        """submit 과 같고, 로그용으로 퀴즈 서버 응답 본문(줄인 것)을 네 번째로 함께 돌려준다."""
        body = {"question_id": target["question_id"], **answer, "reason": reason}
        try:
            r = self.http.post(f"{target['base']}/api/answer", json=body,
                               headers={"Authorization": f"Bearer {target['token']}"})
        except httpx.HTTPError as error:
            return None, None, f"퀴즈 서버에 연결하지 못했습니다({type(error).__name__}).", None
        try:
            data = r.json()
        except ValueError:
            data = {}
        response = response_for_log(data, r.text)
        elapsed = data.get("elapsed") if isinstance(data, dict) and isinstance(data.get("elapsed"), (int, float)) else None
        if r.is_success:
            return r.status_code, elapsed, None, response
        detail = data.get("detail") or data.get("message") if isinstance(data, dict) else None
        problem = str(detail)[:200] if detail else f"퀴즈 서버가 답을 받지 않았습니다(HTTP {r.status_code})."
        return r.status_code, elapsed, problem, response


def answer_for_log(question_id, answer):
    """제출 로그에 남길 보낸 답: 값과 타입. 토큰과 이유 글은 넣지 않는다."""
    kind = "choice" if "choice" in answer else "text"
    value = answer[kind]
    return {"question_id": question_id, "question_id_type": type(question_id).__name__,
            kind: value if not isinstance(value, str) else value[:100], f"{kind}_type": type(value).__name__}


def response_for_log(data, text):
    """퀴즈 서버 응답 본문을 로그용으로 줄인다. JSON 이면 reason, token 같은 칸은 빼고, 아니면 앞 300자만."""
    if isinstance(data, dict) and data:
        return {k: v for k, v in data.items() if k.lower() not in ("reason", "token", "authorization")}
    return (text or "")[:300]


# ── IP 당 분당 요청 수 ──────────────────────────────────────────────

class RateLimiter:
    """IP 마다 최근 60초 요청 시각을 들고, 한도를 넘으면 거절한다. 인스턴스 메모리라 인스턴스마다 따로 센다."""

    def __init__(self, per_minute, clock=time.monotonic):
        self.per_minute, self.clock = per_minute, clock
        self.hits = defaultdict(deque)
        self.lock = threading.Lock()

    def allow(self, key):
        now = self.clock()
        with self.lock:
            hits = self.hits[key]
            while hits and now - hits[0] >= 60:
                hits.popleft()
            if len(hits) >= self.per_minute:
                return False
            hits.append(now)
            if len(self.hits) > 10000:  # 오래된 IP 정리
                for k in [k for k, v in self.hits.items() if not v or now - v[-1] >= 60]:
                    del self.hits[k]
            return True


def client_ip(request):
    """요청한 IP. 앞단 프록시가 붙인 X-Forwarded-For 의 마지막 값을 쓴다(사용자가 앞쪽에 넣은 값은 믿지 않는다)."""
    forwarded = request.headers.get("x-forwarded-for", "")
    if forwarded:
        return forwarded.split(",")[-1].strip()
    return request.client.host if request.client else "unknown"
