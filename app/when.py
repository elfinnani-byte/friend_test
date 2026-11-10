"""날짜와 시각 표현을 다루는 도구 모음.

모델을 쓸 때는 모델이 "목요일 아침" 같은 표현을 ISO 시각으로 바꿔 주고, 코드는 그 값을 검사한다.
다만 모델이 자주 틀리는 두 가지는 코드가 사용자 메시지를 읽어 바로잡는다(app/router.py model_choose).
  - "금요일 시험 전날 밤"처럼 기준 날에서 앞뒤로 옮기는 표현: shifted_when 이 [달력]과 같은 방식으로 계산한다.
  - 기간을 말하지 않은 공휴일 질문: has_period 가 없다고 보면 모델이 넣은 기간을 쓰지 않는다.
fake 모드(모델 없음)에서는 아래 규칙으로 흔한 표현만 읽는다.
"""
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

WEEKDAYS = "월화수목금토일"
DAY_WORDS = {"내일모레": 2, "오늘": 0, "내일": 1, "모레": 2, "글피": 3}  # 긴 말을 먼저 본다(내일모레 ≠ 내일)
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


_PART = re.compile("|".join(PARTS))
# "2시간"의 "2시"를 시각으로 읽지 않는다
_HOUR_MINUTE = re.compile(r"(\d{1,2})\s*시(?!\s*간)(?:\s*(\d{1,2})\s*분|\s*(반))?")


def _part_for(text, hm):
    """시각에 붙는 때 표현. 시각 바로 앞의 것(없으면 뒤의 첫 것), 시각이 없으면 마지막 것.
    '점심 먹고 오후 3시' → 오후, '오전 회의 끝나고 저녁 7시' → 저녁."""
    parts = list(_PART.finditer(text))
    if not parts:
        return None
    if hm is None:
        return parts[-1].group(0)
    before = [p for p in parts if p.start() < hm.start()]
    return (before[-1] if before else parts[0]).group(0)


def to_24h(part, hour):
    """때 표현과 시(1~12)를 0~24시로. 24는 다음 날 0시('밤 12시')."""
    if part in ("밤", "저녁"):
        if hour == 12:
            return 24             # 밤 12시 = 그날 자정(다음 날 0시)
        if part == "밤" and hour <= 5:
            return hour           # 밤 2시 = 새벽 2시
        return hour + 12 if hour < 12 else hour
    if part == "오후":
        return hour + 12 if hour < 12 else hour
    if part == "점심":
        return hour + 12 if 1 <= hour <= 5 else hour  # 점심 1시 = 13시
    if part is None and 1 <= hour <= 7:
        return hour + 12          # "3시"는 보통 오후 3시
    return hour                   # 새벽·아침·오전


def clock(text):
    """'밤', '저녁 7시', '3시 반' 같은 표현을 (시, 분, 시각 표현이 있었는지)로 바꾼다. 없으면 09:00.
    시는 0~24 이고 24 는 다음 날 0시다(밤 12시). 날짜에 붙일 때는 at() 을 쓴다."""
    hm = _HOUR_MINUTE.search(text)
    part = _part_for(text, hm)
    if hm:
        hour = int(hm.group(1))
        minute = int(hm.group(2) or (30 if hm.group(3) else 0))
        hour = to_24h(part, hour) if hour <= 12 else hour
    else:
        hour, minute = (PARTS[part], 0) if part else (9, 0)
    return min(hour, 24), minute, bool(part or hm)


def at(day, hour, minute):
    """그날의 hour:minute. hour 24 는 다음 날 0시."""
    return day.replace(hour=0, minute=minute, second=0, microsecond=0) + timedelta(hours=hour)


# 기준 요일에서 하루 앞뒤로 옮기는 표현. 이 두 가지만 코드가 계산한다.
SHIFT_PATTERN = re.compile(r"전날|다음\s?날")
WEEKDAY_PATTERN = re.compile(r"(다음\s*주\s*|이번\s*주\s*)?([월화수목금토일])요일")
# 이런 말이 함께 있으면 무엇을 기준으로 옮기는지 애매하다. 그때는 모델이 낸 시각을 그대로 쓴다.
# 날짜(12월 4일), 다다음 주, 다음 달처럼 요일만으로는 기준 날을 정할 수 없는 말도 애매하다로 본다
AMBIGUOUS_PATTERN = re.compile(r"전전날|이튿날|\d+\s*일\s*(전|뒤|후)|(하루|이틀|사흘|나흘)\s*(전|뒤|후)|"
                               r"오늘|내일|모레|글피|지난|저번|매주|격주|"
                               r"\d{1,2}\s*월|\d{1,2}\s*일|다다음|다음\s*달|담\s*달|첫\s*째?\s*[월화수목금토일]|"
                               r"마지막\s*[월화수목금토일]|\d{4}\s*년|내년|\d+\s*주\s*(뒤|후)")


def shifted_when(text, now):
    """'금요일 시험 전날 밤' → 그 금요일의 하루 전 21:00. 확실할 때만 계산하고, 애매하면 None(모델 값을 쓴다).

    확실하다고 보는 경우: 요일 표현이 하나, '전날'이나 '다음날'이 하나, 요일이 그 말보다 앞에 있고,
    다른 날짜 표현(오늘·내일, 이틀 전, 지난 주 등)이 없으며, 그 요일이 오늘과 같은 요일이 아닐 때.
    기준 요일은 [달력]과 같은 방식(day_offset: 이번 주에 그 요일이 지났으면 다음 주)으로 정하고,
    시각은 parse_when 과 같은 규칙(clock: 밤 21:00, 시각 표현이 없으면 09:00)으로 정한다."""
    shifts = SHIFT_PATTERN.findall(text)
    weekdays = list(WEEKDAY_PATTERN.finditer(text))
    if len(shifts) != 1 or len(weekdays) != 1 or AMBIGUOUS_PATTERN.search(text):
        return None
    shift_at = SHIFT_PATTERN.search(text).start()
    if weekdays[0].end() > shift_at:
        return None
    offset = day_offset(weekdays[0].group(0), now)
    if offset == 0:  # 오늘과 같은 요일: 오늘인지 다음 주인지 애매하다
        return None
    shift = -1 if shifts[0] == "전날" else 1
    hour, minute, _ = clock(text)
    return at(now + timedelta(days=offset + shift), hour, minute)


# 공휴일 질문의 기간 표현. 이런 말이 없으면 기간을 정하지 않은 질문으로 본다("다음 공휴일 언제야?").
PERIOD_PATTERN = re.compile(
    r"(이번|다음|지난|저번)\s*(주|달)|\d{1,2}\s*월|\d{1,2}\s*일|\d{4}\s*년|\d+\s*(개월|주)|"
    r"올해|금년|내년|작년|주말|연말|연초|상반기|하반기|오늘|내일|모레|[월화수목금토일]요일|\d{4}-\d{1,2}")


def has_period(text):
    return bool(PERIOD_PATTERN.search(text))


def parse_when(text, now):
    """'내일 아침', '목요일 저녁 7시', '30분 뒤' 같은 표현을 시각으로 바꾼다. 못 읽으면 None."""
    later = _RELATIVE.search(text)
    if later:
        hours = int(later.group("h") or 0) + (0.5 if later.group("half") else 0)
        delta = timedelta(hours=hours, minutes=int(later.group("m") or later.group("m2") or 0))
        return (now + delta).replace(second=0, microsecond=0)

    days = _DAYS_LATER.search(text)
    offset = int(days.group(1)) if days else day_offset(text, now)
    hour, minute, has_time = clock(text)
    if offset is None and not has_time:
        return None

    due = at(now + timedelta(days=offset or 0), hour, minute)
    if offset is None and due <= now:
        due += timedelta(days=1)
    return due


_LATER = r"(?:뒤|후|있다가|있다|이따가|이따)"
# '30분 뒤', '2시간 뒤', '1시간 30분 뒤', '2시간 반 이따가'
_RELATIVE = re.compile(r"(?:(?P<h>\d+)\s*시간\s*(?P<half>반)?\s*(?:(?P<m>\d+)\s*분\s*)?|(?P<m2>\d+)\s*분\s*)"
                       + _LATER)  # 숫자가 반드시 있다('뒤에 있는'의 '뒤'는 아니다)
_DAYS_LATER = re.compile(r"(\d+)\s*일\s*(?:뒤|후)")
# 알림 내용에서 지울 상대 시각 표현(router.reminder_text)
RELATIVE_WORDS = re.compile(_RELATIVE.pattern + r"|\d+\s*일\s*(?:뒤|후)")


def fix_year(value, now):
    """모델이 학습 시점의 연도(예: 2023)를 쓰는 경우가 있다. 지난 연도면 올해로 바꾼다. 바꿨으면 True.
    올해로 바꾼 날짜가 이미 지났으면 내년으로 바꾼다(12월 30일에 '1월 2일'). 없는 날짜(윤일)가 되면 바꾸지 않는다."""
    text = str(value or "")
    m = re.match(r"(\d{4})-(\d{2})-(\d{2})", text)
    if not m or int(m.group(1)) >= now.year:
        return value, False
    for year in (now.year, now.year + 1):
        try:
            day = datetime(year, int(m.group(2)), int(m.group(3))).date()
        except ValueError:
            continue
        if day >= now.date() or year > now.year:
            return f"{year}{text[4:]}", True
    return value, False


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
