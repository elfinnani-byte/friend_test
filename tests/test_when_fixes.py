"""모델이 자주 틀리는 두 가지를 코드가 메시지를 읽어 바로잡는다(app/when.py, app/router.py model_choose).
모델을 부르지 않고, 정해 둔 응답을 돌려주는 가짜 모델로 확인한다. 지금은 2026-10-12(월) 14:00 한국 시간."""
import importlib.util
import json
from datetime import datetime
from pathlib import Path

import pytest

from app import router, when

NOW = datetime.fromisoformat("2026-10-12T14:00:00+09:00")
ROOT = Path(__file__).resolve().parent.parent
ROWS = {json.loads(line)["id"]: json.loads(line)
        for line in (ROOT / "data/eval/tool_selection.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}


class OneAnswerLLM:
    """도구 선택을 모델에게 맡기는 경로를 타되, 정해 둔 응답만 돌려준다."""
    can_reason = True
    model = "scripted"

    def __init__(self, tools):
        self.tools = tools

    def complete_json(self, system, user):
        return {"tools": json.loads(json.dumps(self.tools)), "confidence": 0.9, "reason": "가짜"}


def model_args(message, tool, args):
    choice = router.model_choose(message, [], NOW, "서울", OneAnswerLLM([{"name": tool, "args": args}]))
    return choice.tools[0]["args"], choice.guards


# ── 1. 기준 요일 + 전날/다음날 ─────────────────────────────────────────

@pytest.mark.parametrize("message, due", [
    ("금요일 시험 전날 밤에 일찍 자라고 알려 줘", "2026-10-15T21:00"),     # T34: 금요일(10/16)의 전날 밤
    ("목요일 발표 전날 저녁에 자료 확인하라고 알려 줘", "2026-10-14T19:00"),
    ("수요일 면접 다음날 아침에 감사 메일 보내라고 알려 줘", "2026-10-15T08:00"),
    ("토요일 여행 다음 날 빨래하라고 알려 줘", "2026-10-18T09:00"),         # 시각 표현이 없으면 09:00
    ("다음 주 월요일 회의 전날 밤 9시에 알려 줘", "2026-10-18T21:00"),     # 다음 주 월요일(10/19)
    ("이번 주 일요일 전날 저녁 7시 반에 장 보라고 알려 줘", "2026-10-17T19:30"),
])
def test_weekday_and_shift_are_computed_like_calendar(message, due):
    assert when.shifted_when(message, NOW) == datetime.fromisoformat(due + ":00+09:00")


@pytest.mark.parametrize("message", [
    "금요일 저녁 7시에 엄마한테 전화하라고 알려 줘",       # 옮기는 말 없음
    "전날 밤에 일찍 자라고 알려 줘",                      # 기준 요일 없음
    "목요일이나 금요일 시험 전날 알려 줘",                 # 요일이 둘
    "금요일 시험 전전날 알려 줘",                         # 이틀 앞
    "금요일 시험 이틀 전에 알려 줘",
    "금요일 시험 하루 전에 알려 줘",
    "금요일 시험 이튿날 알려 줘",
    "내일 금요일 시험 전날이라고 알려 줘",                 # 내일이 함께 있음
    "지난 금요일 전날 일 기억나?",                        # 지난
    "월요일 회의 전날 알려 줘",                           # 오늘(월)과 같은 요일: 오늘인지 다음 주인지 애매
    "전날 금요일 시험 있다고 알려 줘",                     # 요일이 옮기는 말 뒤
    "금요일 전날, 그리고 다음날 둘 다 알려 줘",            # 옮기는 말이 둘
    "3시간 뒤에 약 먹으라고 알려 줘",
    "30분 뒤에 빨래 꺼내라고 알려줘",
])
def test_ambiguous_messages_are_not_computed(message):
    assert when.shifted_when(message, NOW) is None


def test_model_due_at_is_replaced_when_shift_is_clear():
    # 모델이 '금요일 전날'을 수요일로 낸 경우
    args, guards = model_args(ROWS["T34"]["message"], "create_reminder",
                              {"text": "일찍 자기", "due_at": "2026-10-14T21:00:00+09:00"})
    assert args == {"text": "일찍 자기", "due_at": "2026-10-15T21:00:00+09:00"}
    assert guards == ["create_reminder 의 due_at 을 메시지의 날짜 표현으로 계산한 10월 15일(목) 21:00로 고쳤습니다."]


def test_model_due_at_already_right_is_kept_without_guard():
    args, guards = model_args(ROWS["T34"]["message"], "create_reminder",
                              {"text": "일찍 자기", "due_at": "2026-10-15T21:00:00+09:00"})
    assert args["due_at"] == "2026-10-15T21:00:00+09:00" and guards == []


@pytest.mark.parametrize("message", ["금요일 시험 이틀 전에 알려 줘", "월요일 회의 전날 알려 줘",
                                     "목요일 저녁 7시에 엄마한테 전화하라고 리마인드 해 줘"])
def test_ambiguous_or_plain_messages_keep_model_due_at(message):
    model_due = "2026-10-13T08:00:00+09:00"
    args, guards = model_args(message, "create_reminder", {"text": "알림", "due_at": model_due})
    assert args["due_at"] == model_due and guards == []


def test_unreadable_model_due_at_is_replaced_when_shift_is_clear():
    args, _ = model_args(ROWS["T34"]["message"], "create_reminder", {"text": "일찍 자기", "due_at": "금요일 전날"})
    assert args["due_at"] == "2026-10-15T21:00:00+09:00"


def test_parse_when_is_unchanged_for_rule_path():
    # 키워드 규칙이 쓰는 parse_when 은 그대로다(시각 해석만 clock 으로 옮겼다)
    assert when.parse_when("목요일 저녁 7시", NOW) == datetime.fromisoformat("2026-10-15T19:00:00+09:00")
    assert when.parse_when("3시 반", NOW) == datetime.fromisoformat("2026-10-12T15:30:00+09:00")
    assert when.parse_when("내일 아침", NOW) == datetime.fromisoformat("2026-10-13T08:00:00+09:00")
    assert when.parse_when("오늘 아침", NOW) == datetime.fromisoformat("2026-10-12T08:00:00+09:00")
    assert when.parse_when("금요일 시험 전날 밤", NOW) == datetime.fromisoformat("2026-10-16T21:00:00+09:00")
    assert when.parse_when("안녕", NOW) is None


# ── 2. 기간을 말하지 않은 공휴일 질문 ─────────────────────────────────

@pytest.mark.parametrize("message", ["다음 공휴일 언제야?", "공휴일 언제 있어?", "빨간 날 알려 줘", "설날 연휴가 언제야?"])
def test_holiday_question_without_period_searches_one_year(message):
    args, guards = model_args(message, "get_holidays", {"from": "2026-10-12", "to": "2026-10-12"})
    assert args == {}  # 도구가 오늘부터 1년을 찾는다
    assert guards == ["메시지에 기간 표현이 없어 get_holidays 의 기간을 지우고 오늘부터 1년을 찾습니다."]


@pytest.mark.parametrize("message, period", [
    ("12월에 연휴 있어?", {"from": "2026-12-01", "to": "2026-12-31"}),
    ("이번 주에 공휴일 있어?", {"from": "2026-10-12", "to": "2026-10-18"}),
    ("다음 달에 쉬는 날 있어?", {"from": "2026-11-01", "to": "2026-11-30"}),
    ("올해 남은 빨간 날 알려 줘", {"from": "2026-10-12", "to": "2026-12-31"}),
    ("내년 설날 연휴가 언제야?", {"from": "2027-01-01", "to": "2027-12-31"}),
    ("이번 주말에 공휴일 껴 있어?", {"from": "2026-10-17", "to": "2026-10-18"}),
])
def test_holiday_question_with_period_keeps_model_range(message, period):
    args, guards = model_args(message, "get_holidays", dict(period))
    assert args == period and guards == []


def test_holiday_question_without_period_and_without_range_is_untouched():
    args, guards = model_args("다음 공휴일 언제야?", "get_holidays", {})
    assert args == {} and guards == []


# ── 평가 항목으로 확인 ──────────────────────────────────────────────

def score(row_id, tool, args):
    spec = importlib.util.spec_from_file_location("eval_tools", ROOT / "scripts" / "eval_tools.py")
    eval_tools = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(eval_tools)
    row = ROWS[row_id]
    choice = eval_tools.model_choose(row["message"], row["history"], eval_tools.NOW, eval_tools.LOCATION,
                                     OneAnswerLLM([{"name": tool, "args": args}]))
    return eval_tools.score_row(row, choice)


def test_t34_wednesday_answer_now_passes():
    assert score("T34", "create_reminder", {"text": "일찍 자기", "due_at": "2026-10-14T21:00:00+09:00"}) == (True, [])


def test_t26_same_day_answer_now_passes():
    assert score("T26", "get_holidays", {"from": "2026-10-12", "to": "2026-10-12"}) == (True, [])


def test_t28_wrong_month_still_fails():
    # 기간을 말한 질문은 모델 값을 그대로 쓰므로, 틀린 달은 그대로 틀린다
    assert score("T28", "get_holidays", {"from": "2026-11-01", "to": "2026-11-30"}) == \
        (True, ["from: 정답 2026-12-01, 결과 2026-11-01", "to: 정답 2026-12-31, 결과 2026-11-30"])


# ── 3. 코드 리뷰에서 재현한 시각 오류(밤 12시, 점심 1시, 상대 시각, 날짜가 있는 전날, 연말 연도) ──────────

@pytest.mark.parametrize("text, hm", [
    ("밤 12시", (24, 0)), ("저녁 12시", (24, 0)), ("밤 2시", (2, 0)), ("밤 9시", (21, 0)), ("밤 11시 반", (23, 30)),
    ("점심 1시", (13, 0)), ("점심 12시", (12, 0)), ("오후 3시", (15, 0)), ("아침 7시", (7, 0)), ("새벽 4시", (4, 0)),
    ("3시", (15, 0)), ("9시", (9, 0)),
    ("점심 먹고 오후 3시", (15, 0)), ("오전 회의 끝나고 저녁 7시", (19, 0)),  # 시각 바로 앞의 때 표현
])
def test_clock_uses_the_part_of_day_next_to_the_hour(text, hm):
    assert when.clock(text)[:2] == hm


@pytest.mark.parametrize("message, due", [
    ("오늘 밤 12시에 알려줘", "2026-10-13T00:00"),                 # 밤 12시 = 자정(다음 날 0시)
    ("점심 1시에 약 먹으라고 알려줘", "2026-10-13T13:00"),          # 오늘 13시는 지났다 → 내일
    ("점심 먹고 오후 3시에 회의라고 알려줘", "2026-10-12T15:00"),
    ("1시간 30분 뒤에 알려줘", "2026-10-12T15:30"),
    ("2시간 반 뒤에 알려줘", "2026-10-12T16:30"),
    ("2시간 이따가 물 마시라고 알려줘", "2026-10-12T16:00"),
    ("3일 뒤 아침에 약 먹으라고 알려줘", "2026-10-15T08:00"),
    ("내일모레 아침에 약 먹으라고 알려줘", "2026-10-14T08:00"),
    ("뒤에 있는 우산 챙기라고 1시간 뒤에 알려줘", "2026-10-12T15:00"),  # 숫자 없는 '뒤'는 상대 시각이 아니다
])
def test_parse_when_review_cases(message, due):
    assert when.parse_when(message, NOW) == datetime.fromisoformat(due + ":00+09:00")


@pytest.mark.parametrize("message, model_due", [
    ("금요일 시험 전날 밤 12시에 자라고 알려줘", "2026-10-16T00:00:00+09:00"),  # 목요일 밤 12시 = 금요일 0시
    ("금요일 시험 전날 점심 1시에 약 먹으라고 알려줘", "2026-10-15T13:00:00+09:00"),
])
def test_model_due_at_right_answer_is_not_overwritten(message, model_due):
    args, guards = model_args(message, "create_reminder", {"text": "알림", "due_at": model_due})
    assert args["due_at"] == model_due and guards == []


@pytest.mark.parametrize("message", [
    "12월 4일 금요일 시험 전날 밤에 알려줘",     # 날짜가 있다
    "다다음 주 금요일 시험 전날 밤에 알려줘",     # '다음 주'만으로 정할 수 없다
    "다음달 첫 금요일 전날 밤에 알려줘",
    "다음 달 첫째 금요일 전날 밤에 알려줘",
])
def test_dated_or_far_weekday_keeps_model_due_at(message):
    assert when.shifted_when(message, NOW) is None
    model_due = "2026-12-03T21:00:00+09:00"
    args, guards = model_args(message, "create_reminder", {"text": "알림", "due_at": model_due})
    assert args["due_at"] == model_due and guards == []


@pytest.mark.parametrize("value, now, expected", [
    ("2025-01-02T09:00:00+09:00", "2026-12-30T10:00:00+09:00", ("2027-01-02T09:00:00+09:00", True)),  # 연말
    ("2025-12-01", "2026-11-10T10:00:00+09:00", ("2026-12-01", True)),
    ("2024-02-29", "2026-11-10T10:00:00+09:00", ("2024-02-29", False)),  # 없는 날짜가 되면 바꾸지 않는다
    ("2026-03-01", "2026-11-10T10:00:00+09:00", ("2026-03-01", False)),  # 올해는 그대로
])
def test_fix_year_moves_past_dates_to_next_year(value, now, expected):
    assert when.fix_year(value, datetime.fromisoformat(now)) == expected


@pytest.mark.parametrize("message, text", [
    ("1시간 30분 뒤에 약 먹으라고 알려줘", "약 먹기"),
    ("2시간 반 이따가 물 마시라고 알려줘", "물 마시기"),
    ("에어컨 끄라고 30분 뒤에 알려줘", "에어컨 끄기"),
    ("뒤에 있는 우산 챙기라고 1시간 뒤에 알려줘", "뒤에 있는 우산 챙기기"),
])
def test_reminder_text_drops_relative_time_whole(message, text):
    assert router.reminder_text(message) == text


# ── 4. 약속 취소: 다른 약속 이름이면 하나뿐인 약속도 취소하지 않는다 ──────────

@pytest.mark.parametrize("query, cancelled", [
    ("약 먹기", False),      # 다른 약속 이름 → 취소하지 않는다
    ("우산", True),          # 이름이 맞음
    ("알림", True),          # 무엇인지 말하지 않음 + 약속이 하나뿐 → 그것
    ("", True),
])
def test_cancel_reminder_does_not_cancel_a_different_one(query, cancelled):
    from datetime import timedelta

    from app import tools
    from app.store.memory_store import MemoryStore
    store = MemoryStore()
    store.add_reminder("u", "우산 챙기기", NOW + timedelta(days=1))
    result = tools.cancel_reminder({"query": query}, tools.Context(store, "u", "서울", "Asia/Seoul"))
    assert result.ok is cancelled
    assert store.list_reminders("u")[0]["status"] == ("cancelled" if cancelled else "pending")
