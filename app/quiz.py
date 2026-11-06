"""퀴즈 대결 에이전트 앱이 쓰는 문제 풀기(POST /api/quiz/solve).

- 이지모드: 문제 글, 보기 목록(객관식), 그림 주소(그림 문제)를 JSON 으로 받는다.
- 하드모드: 퀴즈 화면을 폰으로 찍은 사진을 받는다. 화면의 보기 번호는 1부터 적혀 있다.
모델에게는 두 모드 모두 1부터 센 번호로 답하게 하고, API 응답의 0부터 센 번호로 바꾸는 일은 코드가 한다.
모델은 한 번만 부르고 출력은 짧게 받는다. 크레딧은 차감하지 않는다.
"""
import re
import threading
import time
from collections import defaultdict, deque

MAX_QUESTION = 2000
MAX_CHOICE_TEXT = 300
MIN_CHOICES, MAX_CHOICES = 2, 10
PHOTO_TYPES = {"image/jpeg", "image/png", "image/webp", "image/gif"}

SYSTEM = "\n".join([
    "너는 퀴즈 대결에서 문제를 푸는 선수다. 문제를 읽고 정답 하나를 고른다.",
    "- 객관식: answer 에 보기 번호를 숫자 하나로 쓴다. 번호는 1부터 센다(화면 사진이면 화면에 적힌 번호 그대로).",
    "- 단답: answer 에 답만 쓴다. 문제가 요구한 형식을 그대로 따른다. 숫자만 요구하면 '7', 단위를 붙이라고 하면 '7개'처럼"
    " 단위, 표기, 띄어쓰기까지 문제에 맞춘다. 형식 지시가 없으면 가장 짧은 답을 쓴다. 따옴표나 설명을 붙이지 않는다.",
    "- reason 에 답을 고른 이유를 한국어 한두 문장으로 짧게 쓴다.",
    '- JSON 하나로만 답한다: {"answer": 번호 또는 답, "reason": "이유"}',
])


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
        lines.append(f"보기는 {count}개이고 화면에 1부터 {count}까지 번호가 적혀 있다. answer 에 그 번호를 쓴다.")
    trace = {"mode": "hard", "type": kind, "choice_count": count, "photo_bytes": len(photo), "media_type": media_type}
    return "\n".join(lines), {"data": photo, "media_type": media_type}, count, trace


# ── 모델 답 해석 ────────────────────────────────────────────────────

def to_result(answer, count):
    """모델 답을 API 응답으로 바꾼다. 객관식은 1부터 센 번호를 0부터로 바꾼다."""
    reason = str(answer.get("reason", "")).strip()[:300]
    value = answer.get("answer")
    if count:
        match = re.search(r"\d+", str(value))
        number = int(match.group(0)) if match else 0
        if not 1 <= number <= count:
            raise QuizError(502, "모델 답에서 보기 번호를 찾지 못했습니다.")
        return {"choice": number - 1, "reason": reason}
    text = str(value if value is not None else "").strip()
    if not text:
        raise QuizError(502, "모델이 답을 내지 않았습니다.")
    return {"text": text, "reason": reason}  # 문제가 요구한 형식 그대로 둔다("7" 과 "7개" 는 다른 답)


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
