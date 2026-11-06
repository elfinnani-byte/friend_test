"""퀴즈 대결 에이전트 앱이 쓰는 문제 풀기(POST /api/quiz/solve).

- 이지모드: 문제 글, 보기 목록(객관식), 그림 주소(그림 문제)를 JSON 으로 받는다.
- 하드모드: 퀴즈 화면을 폰으로 찍은 사진을 받는다. 화면의 보기 번호는 1부터 적혀 있다.
모델에게는 두 모드 모두 1부터 센 번호로 답하게 하고, API 응답의 0부터 센 번호로 바꾸는 일은 코드가 한다.
모델은 한 번만 부르고 출력은 짧게 받는다. 크레딧은 차감하지 않는다.
"""
import difflib
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
    "[객관식]",
    "- choices_read 에 보기 글을 번호 순서대로 적힌 그대로 옮겨 쓴다(번호는 빼고 글만).",
    "- answer 에 고른 보기 번호를 숫자 하나로 쓴다. 번호는 1부터 센다(화면 사진이면 화면에 적힌 번호 그대로).",
    "- answer_text 에 고른 보기의 글을 choices_read 에 쓴 그대로 쓴다.",
    '- JSON: {"choices_read": ["보기1 글", "보기2 글", ...], "answer": 번호, "answer_text": "고른 보기 글", "reason": "이유"}',
    "[단답]",
    "- 먼저 문제에 답 형식 지시('숫자만', '숫자로', '한 단어로', '영어로', '단위 포함' 등)가 있는지 찾는다.",
    "- 형식 지시가 있으면 answer 는 반드시 그 형식만 쓴다. '숫자만'이면 단위, 조사, 글자 없이 숫자만 쓴다(예: '7개'가 아니라 '7').",
    "- 형식 지시가 없으면 문제가 묻는 말투에 맞춘 가장 짧은 답을 쓴다. 따옴표나 설명을 붙이지 않는다.",
    "- number_only 에 문제가 숫자만 요구하면 true, 아니면 false 를 쓴다.",
    '- JSON: {"answer": "답", "number_only": true 또는 false, "reason": "이유"}',
    "[공통]",
    "- reason 에 답을 고른 이유를 한국어 한두 문장으로 짧게 쓴다.",
    "- JSON 하나로만 답한다.",
])
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
        lines.append(f"보기는 {count}개이고 화면에 1부터 {count}까지 번호가 적혀 있다. answer 에 그 번호를 쓴다.")
    trace = {"mode": "hard", "type": kind, "choice_count": count, "photo_bytes": len(photo), "media_type": media_type}
    return "\n".join(lines), {"data": photo, "media_type": media_type}, count, trace


# ── 모델 답 해석 ────────────────────────────────────────────────────

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


def to_result(answer, count, choices=None, question=None):
    """모델 답을 API 응답과 고친 내용(notes)으로 바꾼다.

    객관식: 모델이 말한 번호(1부터)보다 고른 보기 글을 믿는다. 이지모드는 받은 보기 목록에서,
    하드모드는 모델이 화면에서 읽은 목록(choices_read)에서 고른 글과 가장 잘 맞는 보기를 찾는다.
    단답: 문제가 숫자만 요구하는데 단위가 붙어 있으면 숫자만 남긴다.
    """
    reason = str(answer.get("reason", "")).strip()[:300]
    value, notes = answer.get("answer"), []
    if count:
        match = re.search(r"\d+", str(value))
        number = int(match.group(0)) if match else 0
        read = choices if choices else answer.get("choices_read")
        read = [str(c) for c in read][:count] if isinstance(read, list) else []
        by_text = match_choice(answer.get("answer_text"), read)
        if by_text is not None and by_text != number - 1:
            notes.append("number_from_text")
            number = by_text + 1
        if not 1 <= number <= count:
            raise QuizError(502, "모델 답에서 보기 번호를 찾지 못했습니다.")
        return {"choice": number - 1, "reason": reason}, notes
    text = str(value if value is not None else "").strip()
    if not text:
        raise QuizError(502, "모델이 답을 내지 않았습니다.")
    wants_number = answer.get("number_only") is True or bool(question and NUMBER_ONLY.search(question))
    if wants_number and not re.fullmatch(r"-?\d+(?:\.\d+)?", text):
        number = only_number(text)
        if number is not None:
            notes.append("unit_removed")
            text = number
    return {"text": text, "reason": reason}, notes  # 숫자만 요구한 게 아니면 형식 그대로("7" 과 "7개" 는 다른 답)


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
