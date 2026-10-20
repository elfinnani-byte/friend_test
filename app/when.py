"""날짜와 시각 표현을 다루는 도구 모음.

모델을 쓸 때는 모델이 "목요일 아침" 같은 표현을 ISO 시각으로 바꿔 주고, 코드는 그 값을 검사만 한다.
fake 모드(모델 없음)에서는 아래 규칙으로 흔한 표현만 읽는다.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = "월화수목금토일"
DAY_WORDS = {"오늘": 0, "내일": 1, "모레": 2, "글피": 3}
PARTS = {"새벽": 5, "아침": 8, "오전": 9, "점심": 12, "오후": 15, "저녁": 19, "밤": 21}


def now_in(tz):
    return datetime.now(ZoneInfo(tz))


def label(dt):
    """10월 15일(목) 08:00 형태."""
    return f"{dt.month}월 {dt.day}일({WEEKDAYS[dt.weekday()]}) {dt:%H:%M}"


def date_label(d):
    return f"{d.month}월 {d.day}일({WEEKDAYS[d.weekday()]})"


def calendar_hint(now, days=14):
    """모델은 요일 계산을 자주 틀린다. 앞으로 2주의 날짜와 요일을 표로 넣어 준다."""
    monday = now.date() - timedelta(days=now.weekday())
    rows = []
    for week, name in ((0, "이번 주"), (1, "다음 주")):
        start = monday + timedelta(days=7 * week)
        dates = ", ".join(f"{d.month}/{d.day}({WEEKDAYS[d.weekday()]})" for d in (start + timedelta(days=i) for i in range(7)))
        rows.append(f"{name}: {dates}")
    return f"오늘은 {date_label(now.date())}이다. 한 주는 월요일에 시작한다.\n" + "\n".join(rows)


def day_offset(text, now):
    """오늘/내일/모레/요일 표현을 오늘로부터 며칠 뒤인지로 바꾼다. 없으면 None."""
    for word, offset in DAY_WORDS.items():
        if word in text:
            return offset
    m = re.search(r"(다음\s*주\s*)?([월화수목금토일])요일", text)
    if m:
        target = WEEKDAYS.index(m.group(2))
        if m.group(1):  # 다음 주 X요일: 다음 주 월요일까지 남은 날 + 요일 순서
            return (7 - now.weekday()) + target
        return (target - now.weekday()) % 7
    return None


def parse_when(text, now):
    """'내일 아침', '목요일 저녁 7시', '30분 뒤' 같은 표현을 시각으로 바꾼다. 못 읽으면 None."""
    m = re.search(r"(\d+)\s*(분|시간)\s*(뒤|후|있다가)", text)
    if m:
        n = int(m.group(1))
        return (now + (timedelta(minutes=n) if m.group(2) == "분" else timedelta(hours=n))).replace(second=0, microsecond=0)

    offset = day_offset(text, now)
    part = next((p for p in PARTS if p in text), None)
    hm = re.search(r"(\d{1,2})\s*시(?:\s*(\d{1,2})\s*분|\s*(반))?", text)
    if offset is None and part is None and hm is None:
        return None

    if hm:
        hour = int(hm.group(1))
        minute = int(hm.group(2) or (30 if hm.group(3) else 0))
        if part in ("오후", "저녁", "밤") and hour < 12:
            hour += 12
        elif part is None and 1 <= hour <= 7:
            hour += 12  # "3시"는 보통 오후 3시
    else:
        hour, minute = (PARTS[part], 0) if part else (9, 0)
    hour %= 24

    base = now + timedelta(days=offset or 0)
    due = base.replace(hour=hour, minute=minute, second=0, microsecond=0)
    if offset is None and due <= now:
        due += timedelta(days=1)
    return due


def fix_year(value, now):
    """모델이 학습 시점의 연도(예: 2023)를 쓰는 경우가 있다. 지난 연도면 올해로 바꾼다. 바꿨으면 True."""
    text = str(value or "")
    m = re.match(r"(\d{4})-", text)
    if not m or int(m.group(1)) >= now.year:
        return value, False
    return f"{now.year}{text[4:]}", True


def check_due(value, now):
    """모델이 준 ISO 시각을 검사한다. 과거이거나 1년보다 먼 시각은 거절한다."""
    try:
        due = datetime.fromisoformat(str(value))
    except ValueError:
        return None, "시각 형식을 읽을 수 없습니다."
    if due.tzinfo is None:
        due = due.replace(tzinfo=now.tzinfo)
    if due <= now:
        return None, "이미 지난 시각입니다."
    if due > now + timedelta(days=365):
        return None, "1년보다 먼 약속은 저장하지 않습니다."
    return due, None
