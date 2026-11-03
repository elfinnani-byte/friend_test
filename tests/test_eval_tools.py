"""scripts/eval_tools.py 의 채점. 모델을 부르지 않고 고른 결과(ToolChoice)를 직접 만들어 채점한다."""
import importlib.util
import json
from pathlib import Path

import pytest

from app.router import ToolChoice

ROOT = Path(__file__).resolve().parent.parent
spec = importlib.util.spec_from_file_location("eval_tools", ROOT / "scripts" / "eval_tools.py")
eval_tools = importlib.util.module_from_spec(spec)
spec.loader.exec_module(eval_tools)
upload_spec = importlib.util.spec_from_file_location("upload_datasets", ROOT / "scripts" / "upload_datasets.py")
upload_datasets = importlib.util.module_from_spec(upload_spec)
upload_spec.loader.exec_module(upload_datasets)

ROWS = {json.loads(line)["id"]: json.loads(line)
        for line in (ROOT / "data/eval/tool_selection.jsonl").read_text(encoding="utf-8").splitlines() if line.strip()}


def chose(*tools):
    return ToolChoice([{"name": name, "args": args} for name, args in tools], 0.9, "", "model")


def holidays(**args):
    return chose(("get_holidays", args))


# ── 정답 값 ─────────────────────────────────────────────────────────

def test_next_holiday_question_has_expected_date():
    assert ROWS["T26"]["next_holiday"] == "2026-12-25"
    assert [r["id"] for r in ROWS.values() if "next_holiday" in r] == ["T26"]  # 다음 공휴일을 묻는 항목은 T26 하나


def test_dataset_uses_same_next_holiday():
    items = {item_id: expected for item_id, _, expected, _ in upload_datasets.tool_selection_items()}
    assert items["T26"]["next_holiday"] == ROWS["T26"]["next_holiday"]
    assert all("next_holiday" not in e for item_id, e in items.items() if item_id != "T26")


# ── T26 기간 채점 ───────────────────────────────────────────────────

@pytest.mark.parametrize("args", [
    {},                                              # 기간이 비면 도구가 1년을 찾는다
    {"from": "", "to": ""},
    {"from": "2026-10-12", "to": "2027-10-12"},
    {"from": "2026-12-25", "to": "2026-12-25"},      # 양 끝 포함
    {"from": "2026-12-01"},                          # 빈 쪽은 검사하지 않는다
    {"to": "2026-12-31"},
])
def test_range_containing_next_holiday_passes(args):
    assert eval_tools.score_row(ROWS["T26"], holidays(**args)) == (True, [])


@pytest.mark.parametrize("args, shown", [
    ({"from": "2026-10-12", "to": "2026-11-30"}, "2026-10-12~2026-11-30"),
    ({"from": "2026-12-26", "to": "2027-12-31"}, "2026-12-26~2027-12-31"),
    ({"from": "2026-10-12", "to": "2026-12-24"}, "2026-10-12~2026-12-24"),
    ({"to": "2026-11-30"}, "(없음)~2026-11-30"),
    ({"from": "2027-01-01"}, "2027-01-01~(없음)"),
    ({"from": "다음 주", "to": "2026-12-31"}, "다음 주~2026-12-31"),  # 날짜로 읽을 수 없으면 실패
])
def test_range_missing_next_holiday_fails_with_one_line(args, shown):
    tools_right, wrong = eval_tools.score_row(ROWS["T26"], holidays(**args))
    assert tools_right is True  # 도구는 맞았고, 인자(기간)만 틀렸다
    assert wrong == [f"정답 기간에 2026-12-25 포함, 결과 {shown}"]


def test_wrong_tool_on_t26_is_tool_failure_not_range_failure():
    assert eval_tools.score_row(ROWS["T26"], chose(("get_weather", {}))) == (False, [])


# ── 다른 항목의 채점은 그대로 ─────────────────────────────────────────

def test_other_holiday_questions_do_not_check_next_holiday():
    # T27, T29 는 기간이 무엇이든 get_holidays 만 고르면 정답(이전과 같다)
    for row_id in ("T27", "T29"):
        assert eval_tools.score_row(ROWS[row_id], holidays(**{"from": "2027-01-01", "to": "2027-01-31"})) == (True, [])


def test_month_range_question_still_compares_month_only():
    row = ROWS["T28"]
    assert eval_tools.score_row(row, holidays(**{"from": "2026-12-03", "to": "2026-12-20"})) == (True, [])
    assert eval_tools.score_row(row, holidays(**{"from": "2026-11-01", "to": "2026-12-31"})) == \
        (True, ["from: 정답 2026-12-01, 결과 2026-11-01"])


def test_rule_choices_score_same_as_before():
    # 키워드 규칙 선택기의 결과는 이번 변경 전과 같은 37/41, 36/41 이다
    tool_ok = args_ok = 0
    for row in ROWS.values():
        tools_right, wrong = eval_tools.score_row(row, eval_tools.rule_choose(row["message"], eval_tools.NOW,
                                                                              eval_tools.LOCATION))
        tool_ok += tools_right
        args_ok += tools_right and not wrong
    assert (tool_ok, args_ok) == (37, 36)


# ── --show-raw: 틀린 항목에 모델 응답을 함께 출력 ─────────────────────

class ScriptedLLM:
    """모델을 부르지 않는다. T28(12월 연휴)에는 정해 둔 응답을, 나머지에는 '도구 없음'을 돌려준다(부를 때마다 새 dict)."""
    can_reason = True
    model = "scripted"

    def __init__(self, t26):
        self.t26 = t26

    def complete_json(self, system, user):
        if user.endswith("12월에 연휴 있어?"):
            return json.loads(json.dumps(self.t26))
        return {"tools": [], "confidence": 0.9, "reason": "일상 대화"}


def run_eval(monkeypatch, capsys, t26, *flags):
    monkeypatch.setattr(eval_tools, "load_settings", lambda: None)  # .env 를 읽지 않는다
    monkeypatch.setattr(eval_tools, "make_llm", lambda settings: ScriptedLLM(t26))
    monkeypatch.setattr("sys.argv", ["eval_tools.py", *flags])
    eval_tools.main()
    return capsys.readouterr().out.splitlines()


# 기간을 말한 질문(12월에)이라 코드가 고치지 않고 모델 값으로 채점되는 틀린 응답
SAME_DAY = {"tools": [{"name": "get_holidays", "args": {"from": "2026-11-01", "to": "2026-11-30"}}],
            "confidence": 0.9, "reason": "11월을 본다"}


def failure_block(lines, row_id):
    start = next(i for i, line in enumerate(lines) if line.startswith(f"  {row_id} "))
    return lines[start], (lines[start + 1] if start + 1 < len(lines) else "")


def test_show_raw_prints_model_json_under_failed_item(monkeypatch, capsys):
    lines = run_eval(monkeypatch, capsys, SAME_DAY, "--show-raw")
    result, raw = failure_block(lines, "T28")
    assert "from: 정답 2026-12-01, 결과 2026-11-01" in result
    assert raw.strip() == "모델 응답: " + json.dumps(SAME_DAY, ensure_ascii=False)


def test_show_raw_prints_only_for_failed_items(monkeypatch, capsys):
    lines = run_eval(monkeypatch, capsys, SAME_DAY, "--show-raw")
    failed = [line for line in lines if line.startswith("  T")]
    raws = [line for line in lines if line.strip().startswith("모델 응답:")]
    assert len(raws) == len(failed) > 0
    assert not any(line.startswith("  T01 ") for line in lines)  # '도구 없음'이 정답인 항목은 맞았으므로 나오지 않는다


def test_show_raw_shows_response_before_year_fix(monkeypatch, capsys):
    # 모델이 지난 연도로 답하면, 채점 결과는 고친 연도로, 모델 응답은 모델이 낸 그대로 나온다
    last_year = {"tools": [{"name": "get_holidays", "args": {"from": "2025-11-01", "to": "2025-11-30"}}],
                 "confidence": 0.9, "reason": "작년 날짜"}
    result, raw = failure_block(run_eval(monkeypatch, capsys, last_year, "--show-raw"), "T28")
    assert "결과 2026-11-01" in result
    assert '"from": "2025-11-01", "to": "2025-11-30"' in raw


def test_without_show_raw_output_is_unchanged(monkeypatch, capsys):
    lines = run_eval(monkeypatch, capsys, SAME_DAY)
    assert not any("모델 응답:" in line for line in lines)
    result, following = failure_block(lines, "T28")
    assert "결과 2026-11-01" in result and not following.strip().startswith("모델 응답")


def test_show_raw_does_not_print_keys(monkeypatch, capsys):
    monkeypatch.setenv("LLM_API_KEY", "fake-llm-key-for-eval-test")
    monkeypatch.setenv("LANGFUSE_SECRET_KEY", "fake-langfuse-secret-for-eval-test")
    out = "\n".join(run_eval(monkeypatch, capsys, SAME_DAY, "--show-raw"))
    assert "fake-llm-key-for-eval-test" not in out and "fake-langfuse-secret-for-eval-test" not in out


def test_show_raw_with_rule_is_rejected(monkeypatch):
    monkeypatch.setattr("sys.argv", ["eval_tools.py", "--rule", "--show-raw"])
    with pytest.raises(SystemExit):
        eval_tools.main()
