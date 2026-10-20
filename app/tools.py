"""캐릭터가 쓸 수 있는 도구.

외부 도구(날씨, 미세먼지, 공휴일)는 모두 키 없이 부를 수 있는 공개 API를 쓴다.
약속 도구는 외부를 부르지 않고 저장소에만 쓴다.
각 도구는 모델이 읽을 한 문장 요약(summary)과 화면에 보여 줄 원본(data)을 함께 돌려준다.
"""
from dataclasses import dataclass, field
from datetime import date, timedelta

import httpx

from app import when
from app.logs import log

TIMEOUT = 3.0  # 외부 API 한 번에 기다리는 최대 시간(초)

# 지역 이름 → 좌표. 공개 지오코딩 API는 한국어 지명을 잘못 찾는 경우가 있어 주요 도시를 직접 적어 둔다.
CITIES = {
    "서울": (37.5665, 126.9780), "부산": (35.1796, 129.0756), "인천": (37.4563, 126.7052),
    "대구": (35.8714, 128.6014), "대전": (36.3504, 127.3845), "광주": (35.1595, 126.8526),
    "울산": (35.5384, 129.3114), "세종": (36.4800, 127.2890), "수원": (37.2636, 127.0286),
    "성남": (37.4200, 127.1267), "고양": (37.6584, 126.8320), "용인": (37.2411, 127.1776),
    "창원": (35.2280, 128.6811), "청주": (36.6424, 127.4890), "전주": (35.8242, 127.1480),
    "천안": (36.8151, 127.1139), "포항": (36.0190, 129.3435), "제주": (33.4996, 126.5312),
    "서귀포": (33.2541, 126.5600), "강릉": (37.7519, 128.8761), "춘천": (37.8813, 127.7298),
    "원주": (37.3422, 127.9202), "여수": (34.7604, 127.6622), "경주": (35.8562, 129.2247),
    "목포": (34.8118, 126.3922), "속초": (38.2070, 128.5918),
}

WEATHER_CODES = [
    (0, "맑음"), (3, "구름 조금"), (48, "안개"), (57, "이슬비"), (67, "비"),
    (77, "눈"), (82, "소나기"), (86, "눈 소나기"), (99, "뇌우"),
]


@dataclass
class ToolResult:
    name: str
    ok: bool
    summary: str
    data: dict = field(default_factory=dict)


@dataclass
class Context:
    """도구가 실행될 때 필요한 사용자 정보."""
    store: object
    user_id: str
    location: str
    timezone: str


def weather_text(code):
    for limit, text in WEATHER_CODES:
        if code <= limit:
            return text
    return "알 수 없음"


def resolve_city(name, default):
    name = (name or "").replace("시", "").strip() or default
    for city in CITIES:
        if city in name:
            return city
    return None


def day_word(offset):
    return {0: "오늘", 1: "내일", 2: "모레"}.get(offset, f"{offset}일 뒤")


def day_index(args, ctx, last):
    """args 의 date(YYYY-MM-DD)를 오늘로부터 며칠 뒤인지로 바꾼다. 예보 범위(0~last일) 밖이면 None."""
    today = when.now_in(ctx.timezone).date()
    try:
        offset = (date.fromisoformat(str(args.get("date") or today.isoformat())) - today).days
    except ValueError:
        offset = 0
    return offset if 0 <= offset <= last else None


# ── 외부 정보 도구 ────────────────────────────────────────────────

def get_weather(args, ctx):
    city = resolve_city(args.get("location"), ctx.location)
    if not city:
        return ToolResult("get_weather", False, f"'{args.get('location')}' 지역의 날씨는 지원하지 않습니다.")
    offset = day_index(args, ctx, 6)
    if offset is None:
        return ToolResult("get_weather", False, "날씨 예보는 오늘부터 6일 뒤까지만 볼 수 있습니다.")
    lat, lon = CITIES[city]
    r = httpx.get("https://api.open-meteo.com/v1/forecast", timeout=TIMEOUT, params={
        "latitude": lat, "longitude": lon, "timezone": ctx.timezone, "forecast_days": 7,
        "daily": "weather_code,temperature_2m_max,temperature_2m_min,precipitation_probability_max"})
    r.raise_for_status()
    d = r.json()["daily"]
    i = offset
    data = {"city": city, "date": d["time"][i], "weather": weather_text(d["weather_code"][i]),
            "min": d["temperature_2m_min"][i], "max": d["temperature_2m_max"][i],
            "rain_probability": d["precipitation_probability_max"][i]}
    summary = (f"{city} {day_word(offset)}({data['date']}) 날씨: {data['weather']}, "
               f"최저 {data['min']:.0f}도 최고 {data['max']:.0f}도, 강수 확률 {data['rain_probability']}%.")
    return ToolResult("get_weather", True, summary, data)


def grade(value, cuts):
    for limit, text in zip(cuts, ["좋음", "보통", "나쁨"]):
        if value <= limit:
            return text
    return "매우 나쁨"


def get_air_quality(args, ctx):
    city = resolve_city(args.get("location"), ctx.location)
    if not city:
        return ToolResult("get_air_quality", False, f"'{args.get('location')}' 지역의 미세먼지는 지원하지 않습니다.")
    offset = day_index(args, ctx, 4)
    if offset is None:
        return ToolResult("get_air_quality", False, "미세먼지 예보는 오늘부터 4일 뒤까지만 볼 수 있습니다.")
    lat, lon = CITIES[city]
    r = httpx.get("https://air-quality-api.open-meteo.com/v1/air-quality", timeout=TIMEOUT, params={
        "latitude": lat, "longitude": lon, "timezone": ctx.timezone, "forecast_days": 5, "hourly": "pm10,pm2_5"})
    r.raise_for_status()
    h = r.json()["hourly"]
    day = h["time"][offset * 24][:10]
    rows = [(p10, p25) for t, p10, p25 in zip(h["time"], h["pm10"], h["pm2_5"]) if t.startswith(day) and p10 is not None]
    if not rows:
        return ToolResult("get_air_quality", False, f"{city} {day_word(offset)} 미세먼지 예보가 아직 없습니다.")
    pm10 = round(sum(r[0] for r in rows) / len(rows))
    pm25 = round(sum(r[1] for r in rows) / len(rows))
    # 환경부 예보 등급 기준(㎍/㎥): 미세먼지 30/80/150, 초미세먼지 15/35/75
    data = {"city": city, "date": day, "pm10": pm10, "pm10_grade": grade(pm10, [30, 80, 150]),
            "pm2_5": pm25, "pm2_5_grade": grade(pm25, [15, 35, 75])}
    summary = (f"{city} {day_word(offset)}({day}) 미세먼지 {data['pm10_grade']}({pm10}㎍/㎥), "
               f"초미세먼지 {data['pm2_5_grade']}({pm25}㎍/㎥). 하루 평균 예보값.")
    return ToolResult("get_air_quality", True, summary, data)


_holiday_cache = {}


def holidays_of(year):
    if year not in _holiday_cache:
        r = httpx.get(f"https://date.nager.at/api/v3/PublicHolidays/{year}/KR", timeout=TIMEOUT)
        r.raise_for_status()
        _holiday_cache[year] = [(date.fromisoformat(h["date"]), h["localName"]) for h in r.json()]
    return _holiday_cache[year]


def get_holidays(args, ctx):
    today = when.now_in(ctx.timezone).date()
    try:
        start = date.fromisoformat(args.get("from") or today.isoformat())
        end = date.fromisoformat(args.get("to") or (today + timedelta(days=365)).isoformat())
    except ValueError:
        start, end = today, today + timedelta(days=365)
    found = []
    for year in range(start.year, end.year + 1):
        found += [(d, n) for d, n in holidays_of(year) if start <= d <= end]
    data = {"from": start.isoformat(), "to": end.isoformat(),
            "holidays": [{"date": d.isoformat(), "name": n} for d, n in found]}
    if not found:
        return ToolResult("get_holidays", True, f"{start}부터 {end}까지 공휴일이 없습니다.", data)
    listed = ", ".join(f"{when.date_label(d)} {n}" for d, n in found[:6])
    more = f" 외 {len(found) - 6}일" if len(found) > 6 else ""
    return ToolResult("get_holidays", True, f"{start}~{end} 공휴일: {listed}{more}.", data)


# ── 약속 도구 ──────────────────────────────────────────────────────

def create_reminder(args, ctx):
    now = when.now_in(ctx.timezone)
    text = str(args.get("text") or "").strip()[:100]
    if not text:
        return ToolResult("create_reminder", False, "무엇을 알려 줄지 몰라 약속을 저장하지 않았습니다.")
    due, error = when.check_due(args.get("due_at"), now)
    if error:
        return ToolResult("create_reminder", False, f"약속을 저장하지 않았습니다: {error}")
    reminder = ctx.store.add_reminder(ctx.user_id, text, due)
    local = due.astimezone(now.tzinfo)
    return ToolResult("create_reminder", True, f"약속을 저장했습니다: {when.label(local)}에 '{text}'.",
                      {"id": reminder["id"], "text": text, "due_at": local.isoformat()})


def list_reminders(args, ctx):
    now = when.now_in(ctx.timezone)
    items = ctx.store.list_reminders(ctx.user_id, status="pending")
    if not items:
        return ToolResult("list_reminders", True, "저장된 약속이 없습니다.", {"reminders": []})
    shown = [f"{when.label(r['due_at'].astimezone(now.tzinfo))} '{r['text']}'" for r in items[:5]]
    return ToolResult("list_reminders", True, "저장된 약속: " + ", ".join(shown) + ".",
                      {"reminders": [{"id": r["id"], "text": r["text"], "due_at": r["due_at"].isoformat()} for r in items]})


def cancel_reminder(args, ctx):
    query = str(args.get("query") or "").strip()
    items = ctx.store.list_reminders(ctx.user_id, status="pending")
    match = next((r for r in items if query and (query in r["text"] or r["text"] in query)), None)
    if not match and len(items) == 1:
        match = items[0]
    if not match:
        return ToolResult("cancel_reminder", False, f"'{query}'에 해당하는 약속을 찾지 못했습니다.")
    ctx.store.cancel_reminder(ctx.user_id, match["id"])
    return ToolResult("cancel_reminder", True, f"약속을 취소했습니다: '{match['text']}'.", {"id": match["id"]})


TOOLS = {
    "get_weather": {
        "run": get_weather, "label": "날씨",
        "desc": "지역의 오늘~6일 뒤 날씨, 기온, 강수 확률. 우산, 옷차림 질문도 여기에 해당",
        "args": '{"location": "도시 이름", "date": "YYYY-MM-DD"}',
    },
    "get_air_quality": {
        "run": get_air_quality, "label": "미세먼지",
        "desc": "지역의 오늘~4일 뒤 미세먼지, 초미세먼지 예보. 마스크, 환기, 야외 운동 질문도 여기에 해당",
        "args": '{"location": "도시 이름", "date": "YYYY-MM-DD"}',
    },
    "get_holidays": {
        "run": get_holidays, "label": "공휴일",
        "desc": "기간 안의 한국 공휴일. 연휴, 빨간 날 질문",
        "args": '{"from": "YYYY-MM-DD", "to": "YYYY-MM-DD"}',
    },
    "create_reminder": {
        "run": create_reminder, "label": "약속 저장",
        "desc": "사용자가 나중에 알려 달라고 한 일을 시각과 함께 저장",
        "args": '{"text": "알려 줄 내용", "due_at": "ISO 8601 시각, 사용자 시간대 오프셋 포함"}',
    },
    "list_reminders": {
        "run": list_reminders, "label": "약속 보기",
        "desc": "저장된 약속 목록을 확인",
        "args": "{}",
    },
    "cancel_reminder": {
        "run": cancel_reminder, "label": "약속 취소",
        "desc": "저장된 약속을 취소",
        "args": '{"query": "취소할 약속 내용의 일부"}',
    },
}


def run_tool(name, args, ctx):
    try:
        return TOOLS[name]["run"](args, ctx)
    except httpx.HTTPError as error:
        log("WARNING", "외부 도구 호출 실패", tool=name, error=type(error).__name__)
        return ToolResult(name, False, f"{TOOLS[name]['label']} 정보를 지금은 불러오지 못했습니다.")
