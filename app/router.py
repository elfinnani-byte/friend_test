"""메시지마다 쓸 도구를 고르는 도구 선택기.

1. 모델이 메시지와 최근 대화를 읽고 도구를 0~2개 고른다. 확신도(0~1)와 이유를 함께 낸다.
2. 안전장치: 확신도가 기준보다 낮으면 도구를 쓰지 않고 되묻는다(코드 규칙).
fake 모드처럼 모델을 쓰지 않을 때나 모델 응답을 읽지 못했을 때는 키워드 규칙이 같은 형식으로 답한다.
"""
import re
from dataclasses import dataclass, field
from datetime import date, timedelta

from app import when
from app.logs import log
from app.tools import CITIES, TOOLS

CONFIDENCE_THRESHOLD = 0.6
MAX_TOOLS = 2

KEYWORDS = {
    "get_weather": ["날씨", "비 와", "비와", "비 올", "비올", "우산", "더워", "추워", "덥", "춥", "기온", "눈 와", "눈 올", "뭐 입", "쌀쌀", "따뜻"],
    "get_air_quality": ["미세먼지", "초미세", "공기", "마스크", "환기", "황사"],
    "get_holidays": ["공휴일", "휴일", "연휴", "빨간 날", "빨간날", "쉬는 날"],
}
# "토요일 날씨 알려 줘"는 약속이 아니다. 약속은 "~라고 알려 줘", "잊지 않게", "리마인드"처럼 부탁하는 말이 있어야 한다.
ASK_PATTERN = re.compile(r"(라고|으라고|하라고)\s*(꼭\s*)?(알려|말해)|잊지 않게|리마인드|깨워")
CANCEL_WORDS = ["취소", "지워", "없던 걸로", "안 알려 줘도", "안 알려줘도"]
LIST_PATTERN = re.compile(r"(약속|알림|리마인드).*(뭐|목록|있어|있지|있었|보여|확인)")


def router_prompt():
    tools = "\n".join(f"- {name}: {t['desc']}. 인자 {t['args']}" for name, t in TOOLS.items())
    return "\n".join([
        "너는 AI 친구 서비스에서 사용자 메시지에 답하기 전에 필요한 도구를 고르는 선택기다.",
        "쓸 수 있는 도구:",
        tools,
        "",
        "규칙:",
        "- 도구가 필요 없는 일상 대화면 tools 를 빈 배열로 둔다. 대부분의 메시지는 여기에 해당한다.",
        f"- 꼭 필요한 도구만 최대 {MAX_TOOLS}개 고른다. 묻지 않은 정보의 도구는 덧붙이지 않는다(날씨만 물으면 get_weather 하나).",
        "- 날씨나 미세먼지 이야기를 해도 예보를 묻는 게 아니라 경험이나 하소연이면 도구를 쓰지 않는다.",
        "- location 은 메시지에 도시 이름이 있으면 그 도시, 없으면 사용자 지역이다.",
        "- date 는 [달력]을 보고 정한 YYYY-MM-DD 날짜다. 날짜 표현이 없으면 오늘이다.",
        "- create_reminder 는 사용자가 나중에 알려 달라고 분명히 부탁할 때만 고른다. 일정이 있다는 이야기만 하면 고르지 않는다.",
        "- create_reminder 의 text 는 알려 줄 내용만 짧게 쓴다. 예: '우산 챙기기', '일찍 자기'.",
        "- create_reminder 의 due_at 은 [지금] 시각과 시간대를 기준으로 계산한다. 시각 없이 날짜만 있으면 09:00, 아침 08:00, 점심 12:00, 저녁 19:00, 밤 21:00 으로 한다.",
        "- 최근 대화를 보고 '그럼 모레는?' 같은 이어지는 질문의 도구와 인자를 정한다.",
        "- confidence 는 고른 결과가 맞을 확신을 0과 1 사이 숫자로 쓴다.",
        "- reason 은 고른 이유를 한국어 한 문장으로 쓴다.",
        '- 반드시 JSON 하나로만 답한다: {"tools": [{"name": "도구 이름", "args": {인자}}], "confidence": 0과 1 사이 숫자, "reason": "이유"}',
    ])


@dataclass
class ToolChoice:
    tools: list                          # [{"name": ..., "args": {...}}]
    confidence: float
    reason: str
    decided_by: str                      # model, rule
    action: str = "answer"               # answer, clarify
    guards: list = field(default_factory=list)


# ── 키워드 규칙 ─────────────────────────────────────────────────────

def find_city(text):
    return next((c for c in CITIES if c in text), None)


def reminder_text(text):
    """'목요일 아침에 우산 챙기라고 알려 줘' → '우산 챙기기'처럼 알려 줄 내용만 남긴다."""
    t = re.sub(r"(다음\s*주\s*)?[월화수목금토일]요일|오늘|내일|모레|글피|새벽|아침|오전|점심|오후|저녁|밤", " ", text)
    t = re.sub(r"\d+\s*(분|시간)\s*(뒤|후|있다가)|\d{1,2}\s*시(\s*\d{1,2}\s*분|\s*반)?", " ", t)
    t = re.sub(r"\s*(꼭\s*)?(알려\s*줘|알려\s*주라|알려\s*줄래|리마인드\s*해\s*줘|깨워\s*줘|말해\s*줘)[.!?~ ]*$", "", t)
    t = re.sub(r"^[\s,]*(에는|에)\s+", "", t.strip())
    t = re.sub(r"(으라고|라고)$", "기", t.strip())   # 챙기라고 → 챙기기, 먹으라고 → 먹기
    t = re.sub(r"(다고|하고)$", "", t)
    return re.sub(r"\s+", " ", t).strip(" ,.") or text.strip()


def holiday_range(message, now):
    """'12월에 연휴 있어?' → 그 달의 첫날과 마지막 날. 달이 없으면 빈 인자(앞으로 1년)."""
    m = re.search(r"(\d{1,2})\s*월", message)
    if not m or not 1 <= int(m.group(1)) <= 12:
        return {}
    month = int(m.group(1))
    year = now.year if month >= now.month else now.year + 1
    last = (date(year + month // 12, month % 12 + 1, 1) - timedelta(days=1)).day
    return {"from": f"{year}-{month:02d}-01", "to": f"{year}-{month:02d}-{last}"}


def rule_choose(message, now, location):
    picked = []
    if any(w in message for w in CANCEL_WORDS) and re.search(r"약속|알림|리마인드", message):
        picked.append({"name": "cancel_reminder", "args": {"query": reminder_text(message.replace("취소", ""))}})
    elif LIST_PATTERN.search(message):
        picked.append({"name": "list_reminders", "args": {}})
    elif ASK_PATTERN.search(message) and (due := when.parse_when(message, now)):
        picked.append({"name": "create_reminder", "args": {"text": reminder_text(message), "due_at": due.isoformat()}})
    else:
        day = (now + timedelta(days=when.day_offset(message, now) or 0)).date().isoformat()
        for name, words in KEYWORDS.items():
            if any(w in message for w in words):
                args = {"location": find_city(message) or location, "date": day}
                if name == "get_holidays":
                    args = holiday_range(message, now)
                picked.append({"name": name, "args": args})
    picked = picked[:MAX_TOOLS]
    if not picked:
        return ToolChoice([], 0.8, "도구 키워드가 없는 일상 대화입니다.", "rule")
    labels = ", ".join(TOOLS[p["name"]]["label"] for p in picked)
    return ToolChoice(picked, 0.75, f"'{labels}' 관련 표현이 들어 있습니다.", "rule")


# ── 모델 ────────────────────────────────────────────────────────────

def model_choose(message, history, now, location, llm):
    recent = "\n".join(f"{m['role']}: {m['content']}" for m in history[-4:]) or "(없음)"
    user = (f"[지금] {now.isoformat()} ({when.WEEKDAYS[now.weekday()]}요일)\n[달력]\n{when.calendar_hint(now)}\n"
            f"[사용자 지역] {location}\n[최근 대화]\n{recent}\n\n[메시지]\n{message}")
    data = llm.complete_json(router_prompt(), user)
    tools = []
    for t in data.get("tools") or []:
        if isinstance(t, dict) and t.get("name") in TOOLS:
            tools.append({"name": t["name"], "args": t.get("args") if isinstance(t.get("args"), dict) else {}})
    confidence = max(0.0, min(1.0, float(data.get("confidence", 0))))
    result = ToolChoice(tools[:MAX_TOOLS], confidence, str(data.get("reason", "")), "model")

    # 안전장치: 지난 연도로 적힌 날짜는 올해로 고친다
    for t in result.tools:
        for key in ("date", "due_at", "from", "to"):
            if key in t["args"]:
                t["args"][key], fixed = when.fix_year(t["args"][key], now)
                if fixed:
                    result.guards.append(f"{t['name']} 의 {key} 연도가 지난 연도라 {now.year}년으로 고쳤습니다.")
    return result


def choose(message, history, now, location, llm):
    if getattr(llm, "can_reason", False):
        try:
            result = model_choose(message, history, now, location, llm)
        except Exception as error:  # 모델 응답이 형식에 맞지 않으면 규칙으로 대신한다
            log("WARNING", "모델 도구 선택 실패, 키워드 규칙으로 대신함", error=type(error).__name__)
            result = rule_choose(message, now, location)
            result.guards.append("모델 응답을 읽지 못해 키워드 규칙으로 골랐습니다.")
    else:
        result = rule_choose(message, now, location)

    # 안전장치: 확신이 낮으면 도구를 쓰지 않고 되묻는다
    if result.confidence < CONFIDENCE_THRESHOLD:
        result.action = "clarify"
        result.guards.append(f"확신도 {result.confidence:.2f}가 기준 {CONFIDENCE_THRESHOLD} 미만이라 도구를 쓰지 않고 되묻습니다.")
        result.tools = []

    log("INFO", "도구 선택", tools=[t["name"] for t in result.tools], confidence=round(result.confidence, 2),
        decided_by=result.decided_by, action=result.action)
    return result
